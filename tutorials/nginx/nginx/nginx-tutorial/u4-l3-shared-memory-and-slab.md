# 共享内存、slab 分配器与进程间锁

## 1. 本讲目标

本讲要解决一个核心问题：**nginx 的多个 worker 进程各自拥有独立的地址空间，它们凭什么能共享同一份限流计数、负载均衡状态、缓存元数据？**

学完本讲，你应当能够：

1. 说清楚 nginx 是怎样用 `mmap` 申请一块「跨进程共享」的内存，并用 `ngx_shared_memory_add` 在配置阶段把它「登记」进 cycle 的。
2. 说清楚 `ngx_slab_alloc` / `ngx_slab_alloc_locked` 是如何在这块共享内存上做分层（small/exact/big/page）内存管理的，以及它与 u2-l1 讲过的进程私有内存池 `ngx_pool_t` 的本质区别。
3. 说清楚 `ngx_shmtx_trylock` / `ngx_shmtx_lock` 自旋锁是如何用一条原子变量在多 worker 之间做互斥的，以及 `ngx_rwlock` 读写锁用在什么地方。
4. 能把「共享内存 + slab + 自旋锁 + 红黑树」这四件套串起来，解释 `limit_req_zone` 这类指令的跨 worker 状态到底存放在哪里、由谁保护。

---

## 2. 前置知识

本讲建立在前面几讲已经建立的概念之上，先做一次最小回顾：

- **进程模型（u4-l1）**：nginx 是一个 master + N 个 worker 的多进程程序。每个 worker 是一个独立的进程，拥有**独立的虚拟地址空间**。这意味着 worker A 里 `malloc` 返回的指针，worker B 是看不到的。
- **fork 的语义（u4-l2）**：worker 由 master 经 `fork()` 派生。`fork` 之后父子进程的变量虽然初值相同，但此后任意一方写都会触发「写时复制」，二者就此分道扬镳。所以普通全局变量无法用来在 worker 之间传递动态状态。
- **内存池（u2-l1）**：`ngx_pool_t` 是「一个请求一个池」的进程私有分配器，整池销毁回收。它**不能**跨进程共享。
- **红黑树（u2-l3）**：nginx 自带的有序容器，是定时器、限流等模块查找状态用的底座。

那么问题来了：限流要统计「这个 IP 最近一秒发了几次请求」，而请求是被随机分到某个 worker 上的，这次在 worker 0，下次可能在 worker 3。计数必须放在**所有 worker 都能读到、也都能写到**的同一块物理内存里。这就是本讲的全部出发点。

跨进程共享内存的经典做法是：内核帮我把同一块物理内存**映射**到每个进程各自的虚拟地址空间里，于是不同进程里同一个虚拟地址（或不同虚拟地址）背后其实是同一块物理页，一个进程写，其它进程立刻能读到。在 Linux 上这件事由 `mmap(..., MAP_SHARED, ...)` 系统调用完成。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/os/unix/ngx_shmem.c` | 共享内存的**后端**：用 `mmap`/`shmget` 真正申请一段跨进程共享的内存 |
| `src/os/unix/ngx_shmem.h` | `ngx_shm_t` 结构体：一块共享内存的描述符（地址、大小、名字） |
| `src/core/ngx_cycle.c` | `ngx_shared_memory_add`（登记）与 `ngx_init_zone_pool`（物化 + 装 slab）；`ngx_init_cycle` 里调用它们的装配线 |
| `src/core/ngx_cycle.h` | `ngx_shm_zone_t` 结构体：一个共享区的「登记条目」 |
| `src/core/ngx_slab.c` | **slab 分配器**：在共享内存上做分层内存管理 |
| `src/core/ngx_slab.h` | `ngx_slab_pool_t`、`ngx_slab_page_t`、`ngx_slab_stat_t` 结构体 |
| `src/core/ngx_shmtx.c` | **进程间自旋锁** shmtx：原子变量 + 自旋退避 + 信号量回退 |
| `src/core/ngx_shmtx.h` | `ngx_shmtx_sh_t`（共享部分）、`ngx_shmtx_t`（每进程句柄） |
| `src/core/ngx_rwlock.c` | 读写锁 rwlock：读多写少场景 |
| `src/event/ngx_event.c` / `src/event/ngx_event_accept.c` | accept 互斥锁：shmtx 的典型用例 |
| `src/http/modules/ngx_http_limit_req_module.c` | `limit_req_zone`：shm + slab + shmtx + rbtree 的综合范例 |

记住一条导航规律：**`src/os/unix` 提供「怎么向 OS 要共享内存」，`src/core` 提供「怎么管理和加锁」，`src/http/modules` 等业务层把它们组装成具体功能。**

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，按「先有内存 → 再管内存 → 再给内存加锁 → 读多写少的优化」的顺序推进。

### 4.1 共享内存区：mmap 后端与 ngx_shared_memory_add 注册

#### 4.1.1 概念说明

要把一段内存共享给多个 worker，需要两步，这两步在 nginx 里被刻意分离开：

1. **向 OS 要内存（后端）**：调一次 `mmap(..., MAP_SHARED, ...)`，内核返回一段虚拟地址，并且承诺它对应的物理页在所有进程里都是同一份。这是 `ngx_shm_alloc` 干的事。
2. **向 nginx 登记（注册）**：nginx 不允许业务模块直接 `mmap`，而是要求每个模块在配置解析阶段，用一个**名字 + 大小 + tag** 去注册一个共享区，登记进 `cycle->shared_memory` 链表。等 `ngx_init_cycle` 走到固定阶段，再统一调用 `ngx_shm_alloc` 真正分配，并回调该模块的 `init` 钩子去初始化区里的内容。这是 `ngx_shared_memory_add` 干的事。

为什么要把「登记」和「分配」分开？因为：

- **reload 友好**：nginx 平滑重载（`nginx -s reload`）时要构造一个全新的 cycle。新 cycle 会逐个比对老 cycle 的 `shared_memory` 链表，若发现「同名、同 tag、同 size」的区，就直接**复用老的物理内存地址**（`shm.addr = oshm_zone.addr`），而不是重新分配。这样限流计数、缓存元数据在 reload 时就不会丢。
- **配置校验**：同名共享区如果被两个不同 tag 声明，或两次声明的 size 不一致，注册阶段就能直接报错，避免运行时崩。

#### 4.1.2 核心流程

共享区从「配置文本」到「活内存」的生命周期：

```text
配置阶段： limit_req_zone ... zone=myzone:10m
            └─ 模块 set 回调调用 ngx_shared_memory_add(name="myzone", size=10m, tag=&模块)
                 └─ 在 cycle->shared_memory 链表里查重（名字 + tag + size）
                 └─ 不存在则 push 一个新 ngx_shm_zone_t 条目（此时 addr 仍为 NULL，init 待定）

init_cycle 阶段： 遍历 cycle->shared_memory
            └─ reload 复用？ → 复用 oshm_zone.addr，调 init(zone, old_data)
            └─ 否则 ngx_shm_alloc(shm)        ← 真正 mmap
                 └─ ngx_init_zone_pool(zone)   ← 把 slab_pool_t 放到区首部，初始化它
                 └─ zone->init(zone, data)     ← 模块钩子：建红黑树、建 hash 等
```

注意：**配置阶段只登记不分配**；真正的 `mmap` 发生在 `ngx_init_cycle` 里。这与 u3-l2 讲过的「解析只填登记表，物化在解析之后」是同一条原则。

#### 4.1.3 源码精读

**后端：`ngx_shm_alloc` 用 `mmap` 申请共享内存**

nginx 按编译期探测到的 OS 能力，提供三种后端，优先级从高到低：`MAP_ANON`、`/dev/zero`、System V `shmget`。Linux 上几乎总是走第一种：

[src/os/unix/ngx_shmem.c:L14-L28](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_shmem.c#L14-L28) — 用 `mmap(NULL, size, PROT_READ|PROT_WRITE, MAP_ANON|MAP_SHARED, -1, 0)` 申请一段匿名共享内存。关键标志是 `MAP_SHARED`：它告诉内核「这块内存在 fork 出的子进程里要共享同一份物理页」。`MAP_ANON` 表示不关联任何文件，纯内存。失败返回 `MAP_FAILED`。

[src/os/unix/ngx_shmem.c:L31-L38](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_shmem.c#L31-L38) — `ngx_shm_free` 用 `munmap` 归还，仅在不正常退出路径才会调到。

这块内存的描述符就是简单的 `ngx_shm_t`：

[src/os/unix/ngx_shmem.h:L16-L22](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_shmem.h#L16-L22) — 只有 `addr`（起始地址）、`size`、`name`、`log`、`exists`（reload 复用时标记地址已存在）五个字段，没有任何锁或分配器——锁和分配器是后面才「装」上去的。

**登记：`ngx_shared_memory_add`**

[src/core/ngx_cycle.c:L1326-L1334](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L1326-L1334) — 遍历 `cycle->shared_memory` 链表，按 `name` 字符串精确匹配找老条目（链表遍历用的是 nginx 通用的「分块逐个比」模式）。

[src/core/ngx_cycle.c:L1336-L1356](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L1336-L1356) — 找到同名条目后的两道校验：`tag != shm_zone[i].tag` 直接报 EMERG「already declared for a different use」（同名区被不同模块声明）；`size != shm_zone[i].shm.size` 报「size conflicts」。两道都过则返回老条目指针，让同一个区可以被多次引用（例如同一 zone 名被多条 `limit_req` 共用）。

[src/core/ngx_cycle.c:L1359-L1375](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L1359-L1375) — 没找到则 `ngx_list_push` 一个新条目，把 `addr=NULL`、`size`、`name`、`init=NULL`、`tag` 都填好，`noreuse=0`。注意此刻**地址是空的、init 钩子也是空的**——init 由调用方在拿到 `shm_zone` 后自己赋值（4.4 节综合实践会看到 limit_req 怎么做）。

> tag 的取值约定：调用方把**自己模块结构体的地址**（如 `&ngx_http_limit_req_module`）当 tag 传进来。因为每个模块结构体在进程里地址唯一，这就天然成了「这个区归谁管」的身份证。

**物化：`ngx_init_cycle` 里真正分配并装 slab**

[src/core/ngx_cycle.c:L493-L503](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L493-L503) — 三步走：`ngx_shm_alloc` 真正 `mmap` → `ngx_init_zone_pool` 在区首部装上 slab 池头并初始化 → 调 `zone->init` 回调让业务模块建它自己的数据结构。reload 复用路径在 [L473-L488](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L473-L488)，那里直接把老 `addr` 赋给新 zone，跳过 `ngx_shm_alloc`。

[src/core/ngx_cycle.c:L1001-L1025](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L1001-L1025) — `ngx_init_zone_pool` 的核心几行：把整个共享区的结尾指针 `sp->end = addr + size`、最小分配粒度 `sp->min_shift = 3`（即 8 字节）记下，然后 `ngx_shmtx_create(&sp->mutex, &sp->lock, file)` 给这个池子造一把自旋锁（锁字就嵌在共享区里），最后 `ngx_slab_init(sp)` 把空闲页链表、slots 数组都建好。这一步是「共享内存」与「slab/锁」的接合点。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 nginx 真的 `mmap` 出了共享内存区。

1. 编译并启动一个带 `limit_req_zone` 的 nginx（配置见第 5 节综合实践）。
2. 启动后查 master 进程的内存映射：

```bash
cat /proc/$(cat logs/nginx.pid)/maps | grep -E 'rw-s' | head -40
```

3. **需要观察的现象**：输出里能看到若干行权限为 `rw-s`（末尾 `s` = SHARED）的条目，且通常不关联文件名（匿名映射）。

```
7f2b3c000000-7f2b3c0a00000 rw-s 00000000 00:00 12345
```

4. **预期结果**：你会看到一块与 `zone` 大小（如 `10m` 即 `0xa00000`）相当的 `rw-s` 匿名段，且 master 和每个 worker 进程的 maps 里都有**相同**的一段（物理页共享）。

> 上述 `/proc/.../maps` 命令依赖 Linux。其它平台或无 `/proc` 的容器内行为不同；若无法运行，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ngx_shared_memory_add` 要用 `tag` 而不光用 `name` 区分共享区？

**答案**：`name` 是用户在配置里起的字符串，可能撞车（两个不同功能恰好都叫 `one`）；`tag` 是模块结构体地址，进程内唯一。`name + tag` 一起才能确定「这块区是哪个模块的哪个命名区」，避免不同模块误共享同一块内存导致数据互相踩踏。

**练习 2**：用户在配置里把一个已存在的 zone 的 size 从 `10m` 改成了 `20m`，然后 `nginx -s reload`，会发生什么？

**答案**：注册阶段 `ngx_shared_memory_add` 走到 [L1348](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L1348) 的 size 校验，发现新声明的 `20m` 与老登记的 `10m` 不一致，报 EMERG「size ... conflicts」，reload 失败、回退到老配置。共享区 size 在 reload 中不可变。

---

### 4.2 slab 分配器：在共享内存上做内存管理

#### 4.2.1 概念说明

`mmap` 只给我们一整块「裸」内存，从 `addr` 到 `addr+size`。但 `limit_req` 要频繁地「为一个新 IP 分配一个节点、为过期 IP 释放节点」。如果直接像 `malloc/free` 那样在这块裸内存上管理，既要防碎片又要跨进程加锁，复杂度很高。

nginx 的解法是 **slab 分配器**：把一整块共享内存划分成「页」（页大小就是 OS 的 `ngx_pagesize`，通常是 4096），再用一套**分层**策略分配不同大小的对象：

| 层级 | 适用对象大小 | 管理方式 |
| --- | --- | --- |
| **page** | 大于 `ngx_slab_max_size`（= `pagesize/2`，即 2048） | 直接分配连续若干整页 |
| **big** | 介于 exact 和 max 之间 | 一页切成等大块，用 `slab` 字段高位做位图 |
| **exact** | 恰好 `ngx_slab_exact_size`（= `pagesize / (8*sizeof(uintptr_t))`，64 位下 64 字节） | 一页切成 64 块，`slab` 字段一个位图恰好 64 位 |
| **small** | 小于 exact（最小 `min_shift=3` 即 8 字节） | 一页切成等小块，**页内开头放一张真位图** |

slab 的妙处：**固定大小的对象从同一个「页」里切，永不碎片；空闲位用位图记录，分配释放都是 O(1) 位运算**。这跟 u2-l1 的 `ngx_pool_t` 是两套完全不同的设计：

- `ngx_pool_t`：进程私有、小对象 bump 分配、**不支持单对象 free**、整池销毁回收。生命周期简单。
- `ngx_slab_pool_t`：进程共享、按大小分层、**支持精确 free 单对象**、必须加锁。生命周期复杂、对象频繁增删。

一句话区分：内存池是为了「省事地一次性回收」，slab 是为了「在共享内存里精细地反复分配回收」。

#### 4.2.2 核心流程

`ngx_slab_init` 把一块裸共享内存组织成下图布局（地址从低到高）：

```text
区首部 ┌─────────────────────────────┐  ← shm.addr = sp
       │ ngx_slab_pool_t 头          │   (含 mutex、free 链表头、pages 指针...)
       ├─────────────────────────────┤
       │ slots[n] 数组               │   每个 slot 是一个 ngx_slab_page_t 链表头
       ├─────────────────────────────│    （按对象 shift 分桶，部分满页挂这里）
       │ stats[n] 数组               │   每桶的统计：total/used/reqs/fails
       ├─────────────────────────────┤
       │ pages[pages] 数组           │   每个数据页一个 ngx_slab_page_t 描述符
       ├─────────────────────────────┤  ← 对齐到 ngx_pagesize
       │ start                       │
       │   ┌── 数据页 0 (4096B) ──┐  │
       │   ├── 数据页 1 ─────────┤  │   真正放对象的数据区
       │   ├── ...                ──┤  │
       │   └── 数据页 N ─────────┘  │
       └─────────────────────────────┘  ← sp->end = addr + size
```

`ngx_slab_alloc_locked(pool, size)` 的分配决策：

```text
if size > ngx_slab_max_size:                    # 大对象
    分配 ceil(size/pagesize) 个连续整页           #   ngx_slab_alloc_pages
else:
    算 shift = ceil(log2(size))                  # 2 的幂次对齐
    slot  = shift - min_shift
    到 slots[slot] 链表里找一个「部分满」的页
        ├─ 有 → 在该页位图里找一个空位，置位，返回
        └─ 无 → ngx_slab_alloc_pages(1) 拿一个新页，切成等大块，挂进 slots[slot]
```

`slots[slot]` 链表只挂「**部分占用**」的页；全满的页会从链表里摘下（`page->next = NULL`），free 一个对象后又会被挂回去。这是 slab 经典的「部分满页缓存」设计，避免每次分配都从头扫描所有页。

为什么 64 字节（`exact_size`）是个分界？一页 4096 字节切成 64 字节的槽，正好 \(4096/64 = 64\) 个槽，一个 64 位的 `uintptr_t`（每位代表一个槽）恰好能管完整一页：

\[
\text{一页对象数} = \frac{\text{pagesize}}{2^{\text{shift}}},\quad
\text{一个字位数} = 8 \cdot \text{sizeof(uintptr\_t)}
\]

当对象数等于字位数（size = exact_size）时位图刚好塞满一个字（EXACT 档）；对象更少（size 更大）位图只用低位，剩余位可放 shift 值（BIG 档）；对象更多（size 更小）一个字不够，位图只能放到页内数据区首部（SMALL 档）。

#### 4.2.3 源码精读

**关键阈值：`ngx_slab_sizes_init`**

[src/core/ngx_slab.c:L85-L95](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L85-L95) — 在程序启动早期（早于任何 slab 初始化）算出三个全局阈值：

- `ngx_slab_max_size = ngx_pagesize / 2`：超过它就走整页分配（64 位 Linux 默认 2048）。
- `ngx_slab_exact_size = ngx_pagesize / (8 * sizeof(uintptr_t))`：64 位下是 `4096/64 = 64` 字节。
- `ngx_slab_exact_shift`：`exact_size` 对应的移位数（6）。

这三个值把所有请求大小切成 small / exact / big / page 四段。

**初始化：`ngx_slab_init`**

[src/core/ngx_slab.c:L107-L123](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L107-L123) — `min_size = 1 << min_shift`（8 字节）；接着把 `slots[0..n)` 每个初始化成「自指循环链表头」（`next = &slots[i]`），这是 nginx 链表头的通用约定（空链表头指向自己）。

[src/core/ngx_slab.c:L134-L160](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L134-L160) — 用 `size / (pagesize + sizeof(ngx_slab_page_t))` 算出能放多少个数据页（每个数据页配一个描述符），把首个页描述符 `pool->pages[0]` 标记为「连续 `pages` 个空闲页」并挂到 `pool->free` 链表；记下数据区起点 `pool->start`（按 pagesize 对齐）、末页 `pool->last`、空闲页计数 `pool->pfree`。

**加锁外壳 vs 核心算法**

[src/core/ngx_slab.c:L168-L180](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L168-L180) — `ngx_slab_alloc` 是「加锁外壳」：`shmtx_lock` → `alloc_locked` → `shmtx_unlock`。它把跨进程互斥和分配算法解耦。

[src/core/ngx_slab.c:L183-L206](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L183-L206) — `ngx_slab_alloc_locked` 开头的大对象分支：`size > ngx_slab_max_size` 时，按 `ceil(size/pagesize)` 调 `ngx_slab_alloc_pages` 拿连续整页，再用 `ngx_slab_page_addr` 反算出数据区地址。

[src/core/ngx_slab.c:L208-L224](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L208-L224) — 小对象路径：把 `size` 向上取整到 2 的幂次得 `shift`，桶号 `slot = shift - min_shift`，统计该桶请求数 `stats[slot].reqs++`，然后到 `slots[slot].next` 找部分满页。注意 `slots` 是用宏 `ngx_slab_slots(pool)`（[L44-L45](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L44-L45)）从池首部按偏移算出来的。

`exact` 分支（最易读，建议先读它理解 slab 位图思想）：

[src/core/ngx_slab.c:L271-L294](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L271-L294) — 当对象大小恰好是 `exact_size` 时，一页切成 `8*sizeof(uintptr_t)` 块（64 位下 64 块），`page->slab` 这个 `uintptr_t` 字段直接当 64 位位图：第 `i` 位为 0 表示第 `i` 块空闲。分配时找到第一个 0 位、置 1、用 `ngx_slab_page_addr + (i<<shift)` 算出对象地址。当位图变全 1（`NGX_SLAB_BUSY`），就把这一页从 `slots[slot]` 链表摘下（L280-287）。

**整页分配：`ngx_slab_alloc_pages`**

[src/core/ngx_slab.c:L677-L710](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L677-L710) — 在 `pool->free` 链表里找第一个「空闲页数 ≥ 需求」的连续段。若该段比需求大，就把多余部分**重新挂回** free 链表（L686-695，buddy 风格的切分）；把拿到的第一页标 `NGX_SLAB_PAGE_START`，后续页标 `NGX_SLAB_PAGE_BUSY`，并把全局空闲计数 `pool->pfree` 减掉。

[src/core/ngx_slab.c:L733-L810](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L733-L810) — `ngx_slab_free_pages` 释放时还会尝试与**物理相邻**的空闲页「合并」（L753-798），对抗碎片。这是 slab 比朴素位图更复杂也更强大的地方。

> 用 `ngx_slab_free_locked` 释放（[src/core/ngx_slab.c:L461-L475](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L461-L475)）时，先校验指针是否落在 `[pool->start, pool->end]` 区间，再据它属于哪一页的哪一块，反查位图清位。

#### 4.2.4 代码实践

**实践目标**：通过 slab 的 debug 日志，直观看到对象按桶分配。

1. 用 `--with-debug` 编译 nginx（见 u1-l2），`error_log` 加 `debug` 级别。
2. 配置一个 `limit_req_zone $binary_remote_addr zone=one:10m rate=10r/s;`，在某 location 启用 `limit_req zone=one;`。
3. 用 `ab` 或 `wrk` 从多个不同源 IP 打出数百个请求（简单起见可用局域网多客户端，或容器内多 IP）。
4. 在 debug 日志里 grep `slab alloc`。
5. **需要观察的现象**：会反复出现 `ngx_slab.c` 里 `ngx_log_debug2(... "slab alloc: %uz slot: %ui", size, slot)` 这一行（[src/core/ngx_slab.c:L220-L221](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L220-L221)）。
6. **预期结果**：你会看到所有 `slot` 都集中在同一个值（因为 limit_req 节点大小固定），印证「固定大小对象命中同一桶」。若 zone 配得太小、对象过多，还会看到 `slab alloc failed` 与 `ngx_slab_alloc() failed: no memory`（[src/core/ngx_slab.c:L724-L727](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L724-L727)），这正是「共享内存耗尽」的真相。

> 若没有 debug 版 nginx，本实践可降级为「源码阅读型」：在 L218、L365、L381、L397 处看 `stats[slot].total/reqs` 如何累加，理解每个桶的统计含义。具体 size/slot 数值「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：申请 100 字节、申请 3000 字节，分别走 slab 的哪条路径？

**答案**：100 字节 < 2048（`ngx_slab_max_size`），走槽位分配：`shift = ceil(log2 100) = 7`（128 字节档），`slot = 7 - 3 = 4`。3000 字节 > 2048，走 [L191-L206](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L191-L206) 的整页分配 `ngx_slab_alloc_pages(1)`（3000 字节不到一页，向上取整 1 页）。

**练习 2**：slab 分配器内部，为什么 `ngx_slab_alloc` 要调 `shmtx_lock`，而 `ngx_slab_alloc_locked` 不调？

**答案**：`alloc` 是给「只分配一次」的外部调用者的便利函数，自动加锁解锁；`alloc_locked` 假定调用者**已经持有锁**，供「一次临界区里要分配/查找/插入多个对象」的场景使用，避免重复加锁开销。`limit_req` 在 handler 里就是先 `shmtx_lock`，再在临界区内连查带 `alloc_locked`，最后 `unlock`。

---

### 4.3 进程间锁：shmtx 自旋锁

#### 4.3.1 概念说明

光有共享内存还不够。设想 worker 0 和 worker 3 同时给同一个红黑树插入节点——两个进程同时改同一组指针，数据结构瞬间被写坏。共享数据必须**加锁**。

但这里的锁有个硬约束：它保护的资源在共享内存里，被多个进程争用，而 `pthread_mutex` 是线程级的（同进程内），派不上用场。nginx 需要一把**进程级**的锁。

nginx 的 `shmtx`（shared mutex）用一条**原子变量**实现自旋锁：

- 锁字 `*mtx->lock` 是一个 `ngx_atomic_t`，存在共享内存里（所有进程都能读写）。
- **0 表示空闲**，**非 0（实际是持有者的 `ngx_pid`）表示被占用**。
- 抢锁就是用原子 CAS（compare-and-swap）把 0 改成自己的 pid；成功就持有，失败就说明别人先拿了。

shmtx 还有两层优化：

1. **自旋退避**：抢锁失败时不立刻睡觉，而是「自旋」——CPU 空转一小段再试，因为临界区通常极短，对方马上就释放了。自旋量按 `1, 2, 4, 8, ...` 指数增长，给一点退避；中途插 `ngx_cpu_pause()` 降低功耗、减少总线争用。仅当 `ngx_ncpu > 1`（多核）才自旋，单核自旋毫无意义（持有者没机会运行）。
2. **信号量回退**：自旋到底还没拿到？若编译期启用了 `NGX_HAVE_POSIX_SEM`，就 `sem_wait` 真正睡觉，由释放者 `sem_post` 唤醒；否则退化为 `sched_yield()` 让出 CPU。睡觉路径用 `mtx->wait` 计数器记录有多少人在等，释放者据此决定要不要 `sem_post`。

#### 4.3.2 核心流程

`ngx_shmtx_trylock`（非阻塞试锁，accept 互斥用）：

```text
return (*lock == 0 且 CAS(lock, 0, pid) 成功)
# 抢到返回 1，没抢到返回 0，绝不等待
```

`ngx_shmtx_lock`（阻塞锁，共享数据保护用）：

```text
for (;;) {
    if CAS(lock, 0, pid) 成功: return      # 0. 直接抢
    if 多核:
        for n = 1, 2, 4, ... < spin:        # 1. 自旋退避（总次数约 2048）
            pause n 次
            if CAS 成功: return
    if 有信号量:                             # 2. 睡觉等唤醒
        wait++ ; 若此时抢到则 wait-- 返回
        sem_wait(sem)   # 阻塞，直到 unlock 唤醒
        continue
    else:
        sched_yield()   # 退化：让出 CPU 后再循环
}
```

指数退避的总自旋次数约为 \(\sum_{k=0}^{10} 2^k \approx 2048\) 次 `pause`，与 `mtx->spin = 2048` 对应。`ngx_shmtx_unlock` 把锁字 CAS 回 0，若有信号量则 `sem_post` 唤醒一个等待者。

注意一个关键设计：**锁字里存的是持有者 pid**，而不是简单的 1。这样 `ngx_shmtx_force_unlock(mtx, pid)` 能在某个 worker 崩溃后，由其它进程用它记录的 pid 强制释放这把「孤儿锁」。

#### 4.3.3 源码精读

**两段式结构：共享的锁字 vs 每进程的句柄**

[src/core/ngx_shmtx.h:L16-L37](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_shmtx.h#L16-L37) — 这是理解 shmtx 最重要的地方：

- `ngx_shmtx_sh_t`（L16-21）：**放在共享内存里**的部分，只有一个 `lock` 原子字（有信号量时加一个 `wait` 计数）。
- `ngx_shmtx_t`（L24-37）：**每进程的句柄**，含一个 `lock` 指针（指向共享内存里那个字）、`spin` 自旋次数、可选的 `sem`/`wait`/`semaphore`。

> 在 slab 池里，`ngx_slab_pool_t` 把这俩都嵌进自己（`ngx_shmtx_sh_t lock` 在 [slab.h L35](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.h#L35)、`ngx_shmtx_t mutex` 在 [slab.h L50](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.h#L50)），而整个池都在共享内存里，所以 `mutex.lock` 指针指向的锁字、以及 `mutex.sem` 信号量，对所有 worker 都是同一份。

**创建：`ngx_shmtx_create`**

[src/core/ngx_shmtx.c:L18-L43](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_shmtx.c#L18-L43) — 让 `mtx->lock` 指向共享区里的锁字（`mtx->lock = &addr->lock`）；设默认自旋次数 `mtx->spin = 2048`。一个特殊值：若调用方先把 `mtx->spin` 设成 `(ngx_uint_t)-1`，函数直接返回——这表示「**这是一把只用 trylock 的锁，不要自旋也不要信号量**」。accept 互斥锁就用了这个技巧。

**试锁：`ngx_shmtx_trylock`**

[src/core/ngx_shmtx.c:L62-L66](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_shmtx.c#L62-L66) — 一行核心：`*mtx->lock == 0 && ngx_atomic_cmp_set(mtx->lock, 0, ngx_pid)`。注意这是「短路 &&」：先读到 0 才尝试 CAS，避免无谓的总线 CAS 风暴；CAS 把 0 改成自己的 pid。

**阻塞锁：`ngx_shmtx_lock`（全篇精华）**

[src/core/ngx_shmtx.c:L76-L96](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_shmtx.c#L76-L96) — 先无脑试一次 CAS（L78）；失败后若多核，进入指数退避自旋：`for (n = 1; n < mtx->spin; n <<= 1)` 把自旋轮数翻倍，每轮 `ngx_cpu_pause()` n 次（L86-88）再试 CAS。

[src/core/ngx_shmtx.c:L98-L131](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_shmtx.c#L98-L131) — 自旋到底还没拿到，走睡觉路径：`wait` 计数 +1，再抢一次（防「刚要睡对方就释放」的竞态，L103-105）；真抢不到才 `sem_wait` 阻塞。无信号量支持时退化 `sched_yield()`（L131）。

**释放与唤醒：`ngx_shmtx_unlock` + `ngx_shmtx_wakeup`**

[src/core/ngx_shmtx.c:L136-L146](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_shmtx.c#L136-L146) — `unlock` 用 `CAS(lock, ngx_pid, 0)` 把锁字改回 0（**只有持有者能改成功**，因为 CAS 要求旧值是自己的 pid）。成功后调 `wakeup`。

[src/core/ngx_shmtx.c:L149-L161](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_shmtx.c#L149-L161) — `force_unlock(mtx, pid)`：只有当 `*lock == pid` 才 CAS 回 0。用于持有锁的 worker 异常退出后的清理。

[src/core/ngx_shmtx.c:L164-L196](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_shmtx.c#L164-L196) — `wakeup` 用 CAS 把 `wait` 减 1（只有确实有人在等时才减），然后 `sem_post` 唤醒一个睡眠者。这种「CAS 减计数 + 有条件 post」避免了「虚假唤醒没人等」的浪费。

#### 4.3.4 代码实践

**实践目标**：观察 accept 互斥锁与共享数据锁的不同加锁姿态。

1. 用 `--with-debug` 编译 nginx，配置 `worker_processes 4;` 和一个 `limit_req_zone`，`error_log` 加 `debug`。
2. 用压力工具并发打请求。
3. **需要观察的现象**：日志里会同时出现两类 shmtx 调试行：
   - `accept mutex locked` / `accept mutex lock failed`（来自 [ngx_event_accept.c:L349-L368](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L349-L368)，trylock 姿态）。
   - `shmtx lock` / `shmtx unlock`（来自 [ngx_shmtx.c:L74](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_shmtx.c#L74) 与 [L140](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_shmtx.c#L140)，阻塞锁姿态，由 limit_req 等共享数据操作触发）。
4. **预期结果**：你能明显看到前者只「试一次就走」（成功或失败都立刻返回，从不等待），后者会成对出现（lock 后必然有 unlock）。这正是 trylock 与 lock 两种使用模式的区别。

> 无 debug 版时，可改为「源码阅读型」：对比 `ngx_shmtx_trylock`（L62）与 `ngx_shmtx_lock`（L69）的循环结构，说明为何前者没有 for 循环。

#### 4.3.5 小练习与答案

**练习 1**：`ngx_shmtx_lock` 里，为什么在 `sem_wait` 之前要先 `wait++`，然后**再**试一次 CAS 抢锁？

**答案**：在「`wait++`」与「`sem_wait`」之间存在窗口——对方可能恰在此刻释放锁。如果不重试就直接睡，会错过这次释放、可能睡死（释放者 `sem_post` 只在 `wait>0` 时才发，但若释放发生在你 `wait++` 之前、对方看到 `wait==0` 就没 post）。先增计数再抢一次，抢到就 `wait--` 返回，没抢到才安全入睡。

**练习 2**：accept 互斥锁为什么把 `spin` 设成 `-1`，而不是像 slab 锁那样用 2048？

**答案**：accept 锁用 `trylock`——抢不到就**立刻**放弃本轮 accept、去处理已有连接，绝不等待。设 `spin=-1` 既让 `ngx_shmtx_create` 跳过信号量初始化（accept 锁不需要睡觉），也作为「禁止自旋/禁止阻塞」的语义标记。worker 不应为了抢 accept 权而空转 CPU 或睡觉，那会违背 accept 互斥「轻量、非阻塞」的设计初衷。

---

### 4.4 读写锁 rwlock：读多写少的优化

#### 4.4.1 概念说明

自旋锁是「互斥」的——无论读还是写都串行。但有些共享数据**读远多于写**（例如 upstream 的 peer 状态：每次请求都要读，只在 peer 故障时才写）。这种场景下，让多个读并发、只对写互斥，能显著降低争用。这就是**读写锁**（rwlock）。

nginx 的 `ngx_rwlock` 同样用一条原子变量 `*lock` 巧妙编码两种模式：

- `*lock == 0`：空闲。
- `*lock == NGX_RWLOCK_WLOCK`（即 `(ngx_atomic_uint_t)-1`，全 1）：被**写锁**持有。
- `*lock == 正整数 n`：被 **n 个读者**同时持有。

于是：「想读」只要确认当前不是写锁，就把 `*lock` 加 1；「想写」必须确认 `*lock == 0`，才 CAS 改成全 1。读者计数让多读并发，写者独占。

> 读写锁在 nginx 里目前主要服务于 `NGX_HTTP_UPSTREAM_ZONE` / `NGX_STREAM_UPSTREAM_ZONE`（upstream 共享 peer 状态）。普通 HTTP 请求路径上用得更多的是 shmtx。`ngx_rwlock.c` 末尾的编译断言（[L111-L115](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rwlock.c#L111-L115)）也印证了这一点——只有开了 upstream zone 才强制要求原子操作。

#### 4.4.2 核心流程

**写锁** `ngx_rwlock_wlock`：

```text
for (;;):
    if *lock==0 且 CAS(lock, 0, WLOCK): return   # 抢到
    if 多核: 指数退避自旋
    sched_yield()                                 # 让出 CPU 再循环
```

**读锁** `ngx_rwlock_rlock`：

```text
for (;;):
    readers = *lock
    if readers != WLOCK 且 CAS(lock, readers, readers+1): return  # 抢到（加一个读者）
    if 多核: 指数退避自旋（每次重新读 readers）
    sched_yield()
```

**解锁** `ngx_rwlock_unlock`：

```text
if *lock == WLOCK:  CAS(lock, WLOCK, 0)     # 是写锁，直接清零
else:               fetch_add(lock, -1)      # 是读锁，读者数减 1
```

注意读锁用 `ngx_atomic_fetch_add` 原子减 1，因为可能多个读者并发释放。

#### 4.4.3 源码精读

**编码常量**

[src/core/ngx_rwlock.c:L15-L16](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rwlock.c#L15-L16) — `NGX_RWLOCK_SPIN = 2048`（与 shmtx 同量级）、`NGX_RWLOCK_WLOCK = (ngx_atomic_uint_t)-1`（写锁标记，全 1）。

**写锁与读锁**

[src/core/ngx_rwlock.c:L19-L48](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rwlock.c#L19-L48) — `wlock`：只有 `*lock` 恰为 0 才 CAS 成 WLOCK；多核下指数退避自旋（结构与 shmtx 如出一辙），单核或自旋耗尽则 `sched_yield`。

[src/core/ngx_rwlock.c:L51-L86](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rwlock.c#L51-L86) — `rlock`：先读 `readers = *lock`；只要它不是 WLOCK（即没有写者），就 CAS 把它加 1。多个读者可并发 +1，故读不互斥；但有写者（WLOCK）时读者必须等。

**解锁与降级**

[src/core/ngx_rwlock.c:L89-L97](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rwlock.c#L89-L97) — `unlock` 按当前锁值分支：写锁用 CAS 清零，读锁用 `fetch_add(-1)`。

[src/core/ngx_rwlock.c:L100-L106](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rwlock.c#L100-L106) — `downgrade`：把已持有的**写锁直接降级为读锁**（`*lock = 1`，即一个读者）。这避免「先 unlock 写锁、再 rlock」之间其它写者插队的窗口，常用于「写完想接着读」的情景。

#### 4.4.4 代码实践

**实践目标**：在 upstream zone 场景下定位 rwlock 的真实调用方（源码阅读型）。

1. 在 nginx 源码里搜索 rwlock 的调用点：

```text
grep -rn "ngx_rwlock_" src/http/modules/ngx_http_upstream_zone_module.c \
                      src/stream/ngx_stream_upstream_zone_module.c
```

2. **需要观察的现象**：会看到 upstream zone 模块在更新/读取共享 peer 链表时，分别用 `ngx_rwlock_wlock` / `ngx_rwlock_rlock` 保护。
3. **预期结果**：你能说清楚「配置了 `zone` 指令的 upstream，其 peer 状态被多 worker 共享，读（选 peer）用读锁、写（标记 peer 故障）用写锁」。
4. 若未启用 upstream zone，本实践为纯源码阅读：直接阅读上述两个 zone 模块，理解 rwlock 把「读多写少」的 peer 状态争用降到最低。

#### 4.4.5 小练习与答案

**练习 1**：`ngx_rwlock_rlock` 里，为什么 CAS 的「期望值」是刚才读到的 `readers`，而不是固定值 0？

**答案**：因为读锁允许并发——可能同时有多个读者，`*lock` 是「当前读者数」。要把读者数 +1，必须用「我读到的那个数」作期望值做 CAS，确保从「我读到」到「我 CAS」之间没有别人（包括写者或别的读者增减）改动它。写者期望值固定为 0；读者期望值随当前读者数变化。

**练习 2**：`ngx_rwlock_downgrade` 把 `*lock` 从 WLOCK 直接写成 1，而不是先 CAS。这样安全吗？

**答案**：安全。能调 `downgrade` 的前提是当前进程持有写锁，而写锁是独占的——此刻没有任何其它读者或写者，`*lock` 必定就是 WLOCK，普通赋值即可，无需 CAS。降级后变成「1 个读者」，新读者可立即加入。

---

## 5. 综合实践：limit_req_zone 把四件套串起来

本讲的 practice task 是：**说明 `limit_req_zone` 这类需要跨 worker 共享状态的指令，是如何借助「共享内存 + slab 分配器 + 进程间锁 + 红黑树」实现的，并画出数据存放的位置。**

### 5.1 配置

```nginx
http {
    limit_req_zone $binary_remote_addr zone=myzone:10m rate=10r/s;
    server {
        listen 80;
        location / {
            limit_req zone=myzone burst=20;
            proxy_pass http://backend;
        }
    }
}
```

### 5.2 四件套各自的职责

下面四步，正好对应本讲四个模块（共享内存 / slab / shmtx）加上 u2-l3 的红黑树。请逐条对照源码理解。

**第 1 步：注册共享区（ngx_shared_memory_add）**

[src/http/modules/ngx_http_limit_req_module.c:L941-L957](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_limit_req_module.c#L941-L957) — `limit_req_zone` 指令的 set 回调算出 `size=10m`（`10m` 经 `ngx_parse_size` 解析，见 u2-l2），调 `ngx_shared_memory_add(cf, &name="myzone", 10m, &ngx_http_limit_req_module)` 登记一个区；再把 `shm_zone->init = ngx_http_limit_req_init_zone` 钩子挂上。此刻区还没分配，只是登记。

**第 2 步：物化 + 装 slab（ngx_init_cycle → ngx_init_zone_pool）**

如 4.1 所述，`ngx_init_cycle` 走到固定阶段会 `ngx_shm_alloc`（mmap 出 10m）→ `ngx_init_zone_pool`（在区首部装 `ngx_slab_pool_t`、初始化自旋锁、初始化空闲页链表）→ 调 `init` 钩子。

**第 3 步：用 slab 在共享区里建红黑树（init 钩子）**

[src/http/modules/ngx_http_limit_req_module.c:L728-L746](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_limit_req_module.c#L728-L746) — `ngx_http_limit_req_init_zone` 里：

- `ctx->shpool = (ngx_slab_pool_t *) shm_zone->shm.addr;` —— 把区首部直接当 slab 池句柄。
- `ctx->sh = ngx_slab_alloc(ctx->shpool, sizeof(...shctx_t));` —— 从 slab 池里**分配**一块放共享上下文（含红黑树根、sentinel、LRU 队列）。
- `ngx_rbtree_init(&ctx->sh->rbtree, ...)`、`ngx_queue_init(&ctx->sh->queue)` —— 初始化红黑树与过期队列。

注意：连 `log_ctx`（[L750](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_limit_req_module.c#L750)）都是从 slab 池里 `ngx_slab_alloc` 出来的——所有跨 worker 共享的结构，一律来自 slab。

**第 4 步：请求时加锁 → 查/插红黑树（shmtx + slab_alloc_locked）**

[src/http/modules/ngx_http_limit_req_module.c:L246-L251](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_limit_req_module.c#L246-L251) — 每个请求到来时，handler 先 `ngx_shmtx_lock(&ctx->shpool->mutex)` 进入临界区，再调 `ngx_http_limit_req_lookup` 在共享红黑树里查这个 IP 的上次访问时间。

[src/http/modules/ngx_http_limit_req_module.c:L494-L516](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_limit_req_module.c#L494-L516) — 新 IP 没找到？用 `ngx_slab_alloc_locked` 在 slab 池里分配一个节点（**因为已在临界区，用 _locked 版本避免重锁**），`ngx_rbtree_insert` 插入共享红黑树，并挂进 LRU 队列。

[src/http/modules/ngx_http_limit_req_module.c:L688-L693](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_limit_req_module.c#L688-L693) — 过期节点用 `ngx_slab_free_locked` 释放回 slab 池。

### 5.3 数据存放位置图

把上面的链路画成一张「内存地图」，这是本综合实践要求你交的「图」：

```text
                一块 10m 的共享内存（mmap MAP_SHARED，所有 worker 同一物理页）
  shm.addr ────┬───────────────────────────────────────────────────────────────┐
               │ ngx_slab_pool_t  sp                                          │
               │   ├─ lock (ngx_shmtx_sh_t) ──┐                               │
               │   │     .lock ───────────────┼──→ 锁字 (ngx_atomic_t, CAS)   │ ← 4.3 shmtx
               │   ├─ mutex (ngx_shmtx_t)     │                               │
               │   ├─ free / pages[] / slots[]/ stats[]  (slab 元数据)        │ ← 4.2 slab
               │   ├─ start ──┐                                               │
               │   └─ data ───┼──→ ctx->sh  (ngx_slab_alloc 分配出来)         │
               ├──────────────┼──────────────────────────────────────────────┤
               │ 数据区 start │                                               │
               │   ┌──────────┴──────────┐  ← slab_alloc 分配出的节点        │
               │   │ ngx_rbtree_node_t   │      （每个 IP 一个，挂在红黑树）  │
               │   │   + limit_req 数据  │                                    │
               │   ├─────────────────────┤                                    │
               │   │ ... 更多节点 ...    │                                    │
               │   └─────────────────────┘                                    │
               └─────────────────────────────── shm.end (addr+10m) ──────────┘
                         ↑                          ↑
             master 与每个 worker 进程的虚拟地址空间里，
             都映射到【同一块物理内存】，所以 worker 0 写入的节点，
             worker 3 立刻能在红黑树里读到——由 shmtx 保证不会同时写坏。
```

### 5.4 你需要做的

1. 阅读上述四段源码，确认每一步分别落在「共享内存（4.1）/ slab（4.2）/ shmtx（4.3）/ 红黑树（u2-l3）」哪一层。
2. 用 `--with-debug` 编译 nginx，配好上面的 `limit_req_zone`，从多个源 IP 并发请求，在 debug 日志里找出 `slab alloc`、`shmtx lock`、`shmtx unlock` 三类行，验证它们确实在一次请求处理中按 `lock → slab alloc → unlock` 的顺序出现。
3. 把 5.3 的内存地图按你实际的 zone 名、size、节点数填一遍，标注「哪些字段是 mmap 出来的、哪些是 slab 分配出来的、哪条原子变量是锁」。
4. 思考：如果把 `zone=myzone:10m` 改成 `1m`，压力足够大时会发生什么？预期会看到 `ngx_slab_alloc() failed: no memory`（4.2.4 节），`limit_req` 会因此返回 `503`——共享内存耗尽直接体现为限流行为变化。

> 若本地无法编译/压测，本任务可降级为纯源码阅读：仅完成第 1、3 步即可达成「说明它如何借助共享内存 + slab 实现」的目标，并明确标注「运行现象待本地验证」。

---

## 6. 本讲小结

- **共享内存是跨 worker 共享状态的唯一通道**：worker 各有独立地址空间，只有 `mmap MAP_SHARED` 出来的区才是「同一块物理内存」。nginx 把「登记」（`ngx_shared_memory_add`，配置阶段、只填表）与「分配」（`ngx_shm_alloc`，`ngx_init_cycle` 阶段、真正 mmap）分离，并用 `name + tag + size` 支撑 reload 复用与冲突检测。
- **slab 是共享内存上的内存管理器**：把裸内存分成 small/exact/big/page 四层，固定大小对象命中同桶、位图 O(1) 分配，支持精确 free 与相邻页合并，专为「共享内存里频繁增删对象」而生，与进程私有的 `ngx_pool_t` 是两套设计。
- **shmtx 是进程级自旋锁**：一条原子变量（0=空闲、pid=持有者）+ CAS + 指数退避自旋 + 信号量回退。`trylock` 非阻塞（accept 互斥用，spin=-1）、`lock` 阻塞（共享数据用）。锁字存 pid 还能支持崩溃后的 `force_unlock`。
- **rwlock 是读多写少的读写锁**：一条原子变量同时编码「写锁（全 1）」和「读者计数（正整数）」，多读并发、写独占，主要服务 upstream 共享 peer 状态。
- **四件套缺一不可**：以 `limit_req_zone` 为代表的功能 = 共享内存（装数据）+ slab（管分配）+ shmtx（防并发写坏）+ 红黑树/队列（做查找与过期）。看懂这条链，就看懂了 nginx 一大类「跨 worker 有状态」功能（limit_req/limit_conn/ssl session cache/upstream zone/cache 等）的共同骨架。

---

## 7. 下一步学习建议

- **横向对比**：回到 u2-l1，把 `ngx_pool_t` 与本讲的 `ngx_slab_pool_t` 做一张对照表（私有/共享、是否支持单对象 free、加锁、生命周期），巩固「为什么需要两套分配器」。
- **顺锁往下**：本讲只讲了「锁是什么」。下一讲（u5-l1 事件模型总览）会进入「锁怎么用」——尤其是 `ngx_process_events_and_timers` 里 accept 互斥锁如何与事件循环配合避免惊群（u5-l5）。
- **看更多用例**：挑一个你感兴趣的共享区功能通读其 `shm_zone->init` 钩子与 handler，例如 `src/http/modules/ngx_http_limit_conn_module.c`（连接数限流）、`src/http/ngx_http_file_cache.c`（u10-l1 缓存元数据）、`src/event/ngx_event_openssl_stapling.c`（OCSP 状态）。它们都是本讲这套「四件套」的具体实例。
- **动手实验**：如果你打算写自定义模块（u10-l4），试着在模块里用 `ngx_shared_memory_add` 注册一个自己的小 zone，在 `init` 钩子里 `ngx_slab_alloc` 一个结构、`ngx_rbtree_init` 一棵树，handler 里 `shmtx_lock`/`unlock` 做一次计数——这是检验你是否真懂本讲的最佳方式。
