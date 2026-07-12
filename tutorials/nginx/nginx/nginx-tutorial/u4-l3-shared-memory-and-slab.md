# 共享内存、slab 分配器与进程间锁

## 1. 本讲目标

nginx 采用 master + 多个 worker 的进程模型（见 u4-l1、u4-l2）。每个 worker 是一个独立进程，拥有**自己独立的地址空间和自己的 `ngx_pool_t` 内存池**。这就带来一个根本问题：

> 限流计数（`limit_req`）、负载均衡的 peer 状态、缓存元数据、SSL 会话……这些数据必须**所有 worker 看到同一份**。进程私有内存做不到。

本讲要解决的就是「worker 之间如何安全地共享一份数据」。读完本讲你应当掌握：

1. nginx 如何向操作系统申请一段**所有进程共享**的内存（共享内存区）。
2. 如何在这段共享内存里做**精细化的分配/回收**（slab 分配器），而不是粗暴地一刀切。
3. 多个 worker 同时操作这段共享内存时，如何用**进程间锁**（shmtx 自旋锁、rwlock 读写锁）保证不踩坏数据。
4. 把三者串起来：能讲清 `limit_req_zone` 这类指令的数据到底放在哪里、被谁保护。

## 2. 前置知识

在进入本讲前，你需要先建立几个概念（部分来自前几讲）：

- **进程与内存**：Linux 下 `fork()` 产生的子进程默认拥有「写时复制」的独立内存。要让多个进程看到同一块内存，必须显式申请「共享映射」。本讲讲的「共享内存」专指这种跨进程共享的段，**不是** `ngx_pool_t`（后者是进程私有的，见 u2-l1）。
- **虚拟地址 vs 物理地址**：每个进程有自己的虚拟地址空间。共享内存的本质是：操作系统把同一块**物理页**映射进多个进程的（可能不同的）虚拟地址，于是对一个进程的写入，另一个进程立即可见。
- **原子操作与 CAS**：「比较并交换」（Compare-And-Set）是一条不可被打断的 CPU 指令。nginx 用 `ngx_atomic_cmp_set(lock, old, new)` 表示「当 `*lock == old` 时把它改成 `new` 并返回真，否则返回假」。这是无锁自旋锁的地基。
- **`ngx_cycle_t`**：nginx 的全局上下文容器（见 u3-l2）。共享内存区以一个链表 `cycle->shared_memory` 的形式登记在它上面。
- **mmap**：Linux 把文件或匿名内存映射进进程地址空间的系统调用，`MAP_SHARED` 标志使映射可被 fork 出的子进程继承共享。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/os/unix/ngx_shmem.c` | 向 OS 申请/释放一段共享内存（mmap 或 SysV shmget 三种后端） |
| `src/core/ngx_cycle.c` | `ngx_shared_memory_add` 注册共享区；`ngx_init_cycle` 物化并初始化它们；`ngx_init_zone_pool` 在段首建立 slab 池 |
| `src/core/ngx_cycle.h` | `ngx_shm_zone_t` 与 `ngx_cycle_t.shared_memory` 的结构定义 |
| `src/core/ngx_slab.c` / `ngx_slab.h` | slab 分配器：小块用位图管理、大块按整页分配，并带统计 |
| `src/core/ngx_shmtx.c` / `ngx_shmtx.h` | 共享自旋锁（原子 CAS + 可选 POSIX 信号量） |
| `src/core/ngx_rwlock.c` | 共享读写锁（读多写少场景，如 upstream zone） |
| `src/http/modules/ngx_http_limit_req_module.c` | 综合实践对象：共享内存 + slab + shmtx 的真实用例 |
| `src/event/ngx_event.c`、`src/event/ngx_event_accept.c` | `accept_mutex` 防惊群：shmtx 的另一个经典用法 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**(4.1) 共享内存区** → **(4.2) slab 分配器** → **(4.3) shmtx 自旋锁** → **(4.4) rwlock 读写锁**。三者是层层递进的：先有一块大共享内存，再用 slab 在里面切分，最后用锁保护对它的并发访问。

### 4.1 共享内存区：从 mmap 到 ngx_shared_memory_add

#### 4.1.1 概念说明

nginx 对「共享内存」的使用分两个阶段：

- **配置阶段（解析 nginx.conf 时）**：模块遇到需要共享区的指令（如 `limit_req_zone ... zone=one:10m`），只是「登记」一个名字和大小，**并不真正分配内存**。登记函数就是 `ngx_shared_memory_add`。
- **初始化阶段（`ngx_init_cycle` 中）**：cycle 装配线走到「create shared memory」这一步，才遍历登记表，对每个区真正调用 `ngx_shm_alloc`（即 mmap）拿到一段共享内存，然后调用该区专属的 `init` 回调把它初始化成有用的数据结构。

为什么分两步？因为配置可能 reload，nginx 需要在新旧 cycle 之间按「同名同 tag 同 size」**复用**已有的共享段（连同里面的数据），从而做到 reload 不丢限流计数、不丢缓存。这种复用只有在「登记」与「物化」分离后才可能实现（详见 u3-l2 的装配线）。

#### 4.1.2 核心流程

一段共享内存从无到有的流程：

```
模块指令解析
   │  ngx_shared_memory_add(cf, name="one", size=10m, tag=&模块)
   ▼
登记进 cycle->shared_memory 链表（此时 addr=NULL，只记名/大小/tag/init）
   │
   ……reload 或启动……
   ▼
ngx_init_cycle 「create shared memory」阶段
   │  遍历链表，对每个 zone：
   │    1) 与 old_cycle 同名同 tag 同 size？ → 直接复用旧 addr，跳过 mmap
   │    2) 否则 ngx_shm_alloc() → mmap(MAP_ANON|MAP_SHARED) 拿到 addr
   │    3) ngx_init_zone_pool() → 在 addr 处建立 slab 池 + shmtx 锁
   │    4) 调 zone->init(zone, data) → 模块自定义初始化（如建红黑树）
   ▼
worker fork 后继承这份映射 → 所有 worker 共享同一物理页
```

#### 4.1.3 源码精读

**底层分配：`ngx_shm_alloc` 用 mmap。** nginx 优先使用匿名的 `MAP_ANON|MAP_SHARED`：

[src/os/unix/ngx_shmem.c:L14-L28](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_shmem.c#L14-L28) — 用 `mmap(NULL, size, PROT_READ|PROT_WRITE, MAP_ANON|MAP_SHARED, -1, 0)` 申请一段匿名共享内存；`MAP_SHARED` 是关键，它保证 `fork()` 出的 worker 继承同一物理页。失败时 `addr == MAP_FAILED`。

该文件用条件编译提供三种后端（[L12](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_shmem.c#L12) 起）：`MAP_ANON`（现代 Linux/FreeBSD 默认）、退而求其次打开 `/dev/zero` 映射（[L40-L69](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_shmem.c#L40-L69)）、再老的 System V `shmget`/`shmat`（[L81-L114](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_shmem.c#L81-L114)）。三种后端函数名相同，由 `./configure` 探测到的宏决定编译哪一份。

**登记函数：`ngx_shared_memory_add`。** 它的核心是「按名字去重」：

[src/core/ngx_cycle.c:L1326-L1356](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L1326-L1356) — 遍历已有的 `shared_memory` 链表，按 `name` 字节比较。若找到同名区：还要校验 `tag`（通常传「模块结构体地址」，如 `&ngx_http_limit_req_module`）一致；tag 不一致却同名会报 `the shared memory zone "..." is already declared for a different use` 并返回 NULL。同名同 tag 则**返回已有 zone**（并补齐 size），实现多模块/多次引用同一区的去重。

[src/core/ngx_cycle.c:L1359-L1375](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L1359-L1375) — 没找到则 `ngx_list_push` 新增一项，把 `addr=NULL`、`size`、`name`、`tag` 填好，`init=NULL`（留给模块自己设）。注意此刻**完全没有分配内存**。

**物化与 slab 池建立：`ngx_init_cycle` 的共享内存段。**

[src/core/ngx_cycle.c:L431-L503](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L431-L503) — 遍历登记表：先校验 `size != 0`；接着与 `old_cycle` 比对，若「同 tag 同 size」则直接复用旧 `addr` 并调 `init`（reload 复用路径，[L473-L488](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L473-L488)）；否则 `ngx_shm_alloc` 真正 mmap（[L493](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L493)），再 `ngx_init_zone_pool` 建池（[L497](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L497)），最后调模块的 `init` 回调（[L501](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L501)）。

[src/core/ngx_cycle.c:L1001-L1025](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L1001-L1025) — `ngx_init_zone_pool` 把刚 mmap 出的裸段首部当作 `ngx_slab_pool_t`：设 `end = addr + size`、`min_shift = 3`（最小分配 8 字节）、`addr` 指回自己；然后 `ngx_shmtx_create` 在段里建一把锁，最后 `ngx_slab_init(sp)` 初始化 slab 分配器。这一步是「共享内存」与「slab/锁」的接合点。

**结构定义：**

[src/core/ngx_cycle.h:L29-L36](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.h#L29-L36) — `ngx_shm_zone_t`：`shm`（含 `addr/size/name`）、`init` 回调、`tag`、`data`（模块挂在区上的私有数据指针）、`noreuse`（reload 时是否禁止复用）。`shm_zone->data` 是模块取回自己上下文的把手。

#### 4.1.4 代码实践

**目标**：验证「同名同 tag 的共享区会被去重，不同 tag 同名会冲突」。

**步骤**：

1. 在 `nginx.conf` 的 `http {}` 里写两条 `limit_req_zone`，用**同一个 zone 名**但不同的 key，例如：
   ```nginx
   limit_req_zone $binary_remote_addr zone=one:10m rate=1r/s;
   limit_req_zone $request_uri zone=one:10m rate=1r/s;
   ```
2. 运行 `nginx -t`。

**预期现象**：第二条会报类似 `limit_req_zone "one" is already bound to key "$binary_remote_addr"`。这是因为第二次进入 `ngx_shared_memory_add` 时，同名同 tag（都是 `&ngx_http_limit_req_module`）命中了去重分支返回**同一个** `shm_zone`，但 `shm_zone->data` 已被第一次绑定，于是在 [ngx_http_limit_req_module.c:L947-L954](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_limit_req_module.c#L947-L954) 报「already bound to key」。

**预期结果**：`nginx -t` 失败，配置不合法。这反向证明了 `ngx_shared_memory_add` 的去重语义。**待本地验证**具体报错文案。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ngx_shared_memory_add` 要传 `tag`，而不是只用 `name` 去重？
**答**：不同模块可能碰巧用了相同的 zone 名字（如都叫 `cache`）。`tag` 取模块结构体地址（指针唯一），只有「同一个模块 + 同一个名字」才算同一个区；否则报「declared for a different use」，防止两个不相关模块误共享同一块内存导致数据互相踩踏。

**练习 2**：reload 时，共享内存里的限流计数会清零吗？
**答**：不会。reload 走 [ngx_cycle.c:L473-L488](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L473-L488) 的复用路径：只要新配置里该 zone 的 name/tag/size 都没变，就直接沿用旧 `addr`，`init` 回调会拿到旧 `data` 继续用，计数得以保留。

---

### 4.2 slab 分配器：在共享内存里做精细化管理

#### 4.2.1 概念说明

4.1 拿到的是一整块（比如 10MB）共享内存。但模块需要的是「一个一个的小对象」：限流要存红黑树节点（几十字节），缓存要存元数据，负载均衡要存 peer 状态。直接把整段当裸内存用会很快碎片化，而且没法回收。

**slab 分配器**就是为共享内存量身定做的「内存池」。它的灵感来自经典 SLAB/SLUB 思想，但 nginx 做了大幅简化：

- 把内存按**页**（page，通常是 4096 字节）管理。
- 每页切成**等大小的对象槽**（chunk），用**位图**记录每个槽是否占用——一页同尺寸对象的集合叫一个 `slot`。
- 按对象大小分档：**小块（SMALL）**、**精确块（EXACT）**、**大块（BIG）**，三档用不同的位图编码以节省元数据；**超过半页**的请求则直接按整页分配。

这样既能高效分配小对象（O(1) 置位），又能避免碎片，还能精确回收——比 u2-l1 的 `ngx_pool_t`（只整体销毁、不单独回收）更适合「长期运行、对象频繁生灭」的共享区。

> 与 u2-l1 内存池的区别：`ngx_pool_t` 是**进程私有**的、随请求一次性销毁；slab 池位于**共享内存**，对象会被**单独 free 回收**，且多 worker 并发访问，必须配锁。

#### 4.2.2 核心流程

slab 池在 `ngx_slab_init` 后的内存布局（自顶向下）：

```
共享段起始 addr
┌───────────────────────────────┐
│ ngx_slab_pool_t  (池头:锁/指针) │
├───────────────────────────────┤
│ slots[]   n 个槽头(链表头)      │  n = pagesize_shift - min_shift
├───────────────────────────────┤     min_shift=3 → n=9 (8B..2048B)
│ stats[]   n 个统计块            │
├───────────────────────────────┤
│ pages[]    页描述符数组         │  每个页一个 ngx_slab_page_t
├───────────────────────────────┤  ← pool->start (页对齐)
│                               │
│   实际可分配的页数据区          │  切成 page_size 个页
│                               │
└───────────────────────────────┘ ← pool->end = addr + size
```

一次 `ngx_slab_alloc_locked(pool, size)` 的决策：

```
size > ngx_slab_max_size (= pagesize/2) ?
│ yes → ngx_slab_alloc_pages( ceil(size/pagesize) )   【整页分配】
│ no
└→ 由 size 算出 shift = ceil(log2 size)，slot = shift - min_shift
   │
   ├─ 查 slots[slot] 链表有没有「未满的页」
   │    有 → 在该页位图里找一个空闲位，置位，返回对应地址
   │         若置位后页满了 → 把页从 slot 链表摘下
   │    无 → ngx_slab_alloc_pages(1) 拿一个新页，按 shift 切槽，挂到 slot 链表
   │
   └─ 三档位图编码（由 shift 与 exact_shift 比较）：
        shift < exact_shift → SMALL：对象多，位图放页内首部
        shift == exact_shift → EXACT：一个字位图恰好管一页
        shift >  exact_shift → BIG：对象少，位图与 shift 共存于 page->slab
```

几个关键尺寸（64 位、页 4096 字节时）：

- `ngx_slab_max_size = pagesize / 2 = 2048`：超过它就走整页分配。
- `ngx_slab_exact_size = pagesize / (8 * sizeof(uintptr_t)) = 4096/64 = 64`：这是「一个 `uintptr_t`（64 位）的位图恰好能管完整一页」的对象尺寸。

为什么 64 字节是分界？一页 4096 字节切成 64 字节的槽，正好 \(4096/64 = 64\) 个槽，用一个 64 位字（每位代表一个槽）就能完整记录占用情况——这就是 EXACT 档，位图直接存进页描述符的 `slab` 字段，无需额外界外位图。数学上：

\[
\text{一页对象数} = \frac{\text{pagesize}}{2^{\text{shift}}},\quad
\text{一个字位数} = 8 \cdot \text{sizeof(uintptr\_t)}
\]

当对象数 ≤ 字位数（即 size ≥ exact_size）时位图能塞进一个字；对象数更小（size 更大、shift 更大）也能塞进一个字，于是高 shift 的位图可与 shift 值共享 `slab` 字段（BIG 档）；对象数更多（size 更小、shift 更小）则一个字不够，位图只能放到页内数据区首部（SMALL 档）。

#### 4.2.3 源码精读

**池结构与页描述符：**

[src/core/ngx_slab.h:L34-L59](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.h#L34-L59) — `ngx_slab_pool_t`：`lock`（`ngx_shmtx_sh_t`，锁的共享部分）、`min_size/min_shift`（最小档）、`pages/last/free`（页描述符数组与空闲链表）、`stats`（每档统计）、`pfree`（剩余页数）、`start/end`（数据区起止）、`mutex`（锁的私有部分）、`data`（模块挂在池上的根对象）。

[src/core/ngx_slab.h:L16-L22](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.h#L16-L22) — `ngx_slab_page_t` 是页描述符，只有三个字段：`slab`（多重含义：空闲页数 / 位图 / shift）、`next`、`prev`（低 2 位被复用为页类型标记，见 `NGX_SLAB_PAGE_MASK`）。

**关键阈值初始化：**

[src/core/ngx_slab.c:L85-L95](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L85-L95) — `ngx_slab_sizes_init` 算出 `ngx_slab_max_size = ngx_pagesize/2` 与 `ngx_slab_exact_size`、`ngx_slab_exact_shift`。这三个全局量是 alloc 时分类的标尺。

**池布局初始化：**

[src/core/ngx_slab.c:L98-L165](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L98-L165) — `ngx_slab_init`：在池头之后依次摆好 `slots[]`（每档一个自环链表头）、`stats[]`、`pages[]`（页描述符数组），再把对齐后的剩余空间作为数据区，初始化 `free` 空闲链表包含全部页。注意它**根据 `pool->end - p` 反算能放多少页**（[L134](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L134)），页描述符本身也要占空间。

**带锁包装 vs 不带锁核心：**

[src/core/ngx_slab.c:L168-L180](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L168-L180) — `ngx_slab_alloc` = 加池锁 → `ngx_slab_alloc_locked` → 解锁。对外建议用这个，它自己保证线程/进程安全。

[src/core/ngx_slab.c:L183-L417](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L183-L417) — `ngx_slab_alloc_locked` 是真正的分配核心（**调用者必须已持锁**）。它的分类逻辑：

- [L191-L206](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L191-L206)：`size > ngx_slab_max_size` → `ngx_slab_alloc_pages`，整页分配。
- [L208-L216](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L208-L216)：由 size 算 `shift` 与 `slot` 索引。
- [L226-L330](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L226-L330)：优先在 `slots[slot]` 链表的「未满页」里找空位，按 SMALL/EXACT/BIG 三种位图编码分别处理。
- [L332-L405](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L332-L405)：链表里没有可用页 → `ngx_slab_alloc_pages(pool, 1)` 拿一个新页，按 shift 切槽并挂入 `slots[slot]`。

**整页分配：**

[src/core/ngx_slab.c:L677-L730](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L677-L730) — `ngx_slab_alloc_pages`：在 `free` 链表里找第一段 `≥ pages` 的空闲块，需要时把它劈成两段（前半分配、后半留作空闲），给分配出的页打上 `NGX_SLAB_PAGE_START` 标记并记录页数。这是 slab 内部的「物理页分配器」，相当于一个迷你 buddy。

**为什么 limit_req 用 `_locked` 版本？** 因为它要在「持锁」的临界区里**先查红黑树、再决定分配节点**，整个查询+插入必须原子，所以它自己加锁并调用 `_locked` 变体（见 4.3.3 与综合实践）。

#### 4.2.4 代码实践

**目标**：用 debug 日志观察 slab 分配的档位与 slot。

**步骤**：

1. 用 `--with-debug` 编译 nginx（见 u1-l2）。
2. 在 `nginx.conf` 顶层写 `error_log /path/to/debug.log debug;`（或 `debug_slab`）。
3. 配置一个 `limit_req_zone $binary_remote_addr zone=one:10m rate=1r/s;` 并在某 location 启用 `limit_req zone=one;`。
4. 启动 nginx，用 `ab` 或 `curl` 连发几个请求触发新限流节点分配。
5. 在 debug.log 里 grep `slab alloc`。

**预期现象**：会看到形如 `slab alloc: 80 slot: 3` 的行（见 [ngx_slab.c:L220-L221](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L220-L221) 的 `ngx_log_debug2(... "slab alloc: %uz slot: %ui", size, slot)`）。`slot` 是档位索引，反映这次分配落在哪一档。

**预期结果**：每次首个来自某 IP 的请求会触发一次 `slab alloc`（新建红黑树节点）；同一 IP 的后续请求命中已有节点则不再分配。**待本地验证**确切的 size/slot 数值（取决于红黑树节点结构大小）。

#### 4.2.5 小练习与答案

**练习 1**：申请 100 字节、申请 3000 字节，分别走 slab 的哪条路径？
**答**：100 字节 < 2048（`ngx_slab_max_size`），走槽位分配：shift = ceil(log2 100) = 7（128 字节档），slot = 7 - 3 = 4。3000 字节 > 2048，走 [L191-L206](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L191-L206) 的整页分配 `ngx_slab_alloc_pages(1)`（3000 字节不到一页，向上取整 1 页）。

**练习 2**：为什么 nginx 要区分 SMALL/EXACT/BIG 三档，而不是统一用一种位图？
**答**：为了在不同对象尺寸下都节省元数据。EXACT 档把位图塞进页描述符的 `slab` 字段，零额外开销；BIG 档（对象少）位图短，可与 shift 共享 `slab` 字段；只有 SMALL 档（对象太多、位图超过一个字）才在页内数据区放位图。这样每种尺寸都用了最省的编码。

---

### 4.3 shmtx 自旋锁：保护共享数据的互斥

#### 4.3.1 概念说明

有了共享内存和 slab，多个 worker 会**同时**来读写。例如两个 worker 同时给同一个 IP 的限流节点 `ngx_slab_alloc_locked` 插入，slab 的位图/链表会立刻被踩坏。必须有**互斥锁**。

`ngx_shmtx_t`（shared mutex）是 nginx 的进程间自旋锁，专为共享内存设计：

- 锁变量本身放在**共享内存**里（`ngx_shmtx_sh_t`，仅一个原子字 `lock`），所以所有进程看到同一把锁。
- 用 **CAS 原子指令**抢占：谁先把 `lock` 从 0 改成自己的 pid，谁就拿到锁。
- 抢不到就**自旋等待**（CPU 空转一段时间再重试），自旋许久仍抢不到再退化成 `sched_yield()` 让出 CPU，或（若启用 POSIX 信号量）`sem_wait` 睡眠。
- 锁值存的是 **pid 而非 1**，于是 `ngx_shmtx_force_unlock` 能在持有者进程崩溃后**按 pid 强制释放**，避免死锁。

它有两个最常用的用法：**(a) 给 slab 池当 `mutex`**（保护分配/释放），**(b) `accept_mutex`** 防止多 worker 同时 accept 造成惊群（见 u5-l5）。

#### 4.3.2 核心流程

`ngx_shmtx_lock` 的等待策略（核心是「先忙等、后让出/睡眠」）：

```
for ( ;; ) {
    if (*lock == 0 && CAS(lock, 0, my_pid)) return;   // 抢到
    if (多核) {
        for (n = 1; n < spin(=2048); n <<= 1) {       // 指数退避自旋
            for (i = 0; i < n; i++) cpu_pause();      // 1,2,4,...1024 次 pause
            if (*lock == 0 && CAS(lock,0,my_pid)) return;
        }
    }
    if (有信号量) { wait++; sem_wait(); continue; }    // 睡眠等唤醒
    sched_yield();                                    // 让出 CPU
}
```

指数退避的总自旋次数约为 \( \sum_{n=1}^{1024} n \approx 2 \times 1024 = 2048 \) 次 `pause`，与 `mtx->spin = 2048` 对应。`pause`（`ngx_cpu_pause`）是一条提示 CPU「我在等内存」的指令，降低功耗并减少流水线争用。

解锁时 `ngx_shmtx_unlock` 把 `lock` 从 pid CAS 回 0，若有信号量则 `sem_post` 唤醒一个等待者（`ngx_shmtx_wakeup`）。

#### 4.3.3 源码精读

**锁结构（共享部分 + 私有部分分离）：**

[src/core/ngx_shmtx.h:L16-L21](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_shmtx.h#L16-L21) — `ngx_shmtx_sh_t`：放在共享内存里的部分，只有原子字 `lock`（启用信号量时还有 `wait`）。这是「锁本身」。

[src/core/ngx_shmtx.h:L24-L37](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_shmtx.h#L24-L37) — `ngx_shmtx_t`：每个进程私有的部分，`lock` 是指向共享 `lock` 的指针、`spin` 是自旋次数、（可选）`sem` 信号量。`shmtx_sh_t` 与 `shmtx_t` 的分离，正是「共享数据 vs 私有数据」的清晰划分。

**trylock / lock / unlock：**

[src/core/ngx_shmtx.c:L62-L66](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_shmtx.c#L62-L66) — `ngx_shmtx_trylock`：一次 CAS 尝试，`*lock == 0 && ngx_atomic_cmp_set(lock, 0, ngx_pid)`。不等待，拿不到立即返回假。`accept_mutex` 用它（拿不到就本轮不 accept）。

[src/core/ngx_shmtx.c:L69-L133](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_slab.c#L69-L133) — `ngx_shmtx_lock`：自旋 + 退避 + 信号量/`sched_yield` 的完整等待循环，逻辑同 4.3.2 伪代码。`mtx->spin` 默认在 `ngx_shmtx_create` 里设为 2048（[L27](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_shmtx.c#L27)）。

> 注：上条链接指向 `ngx_shmtx.c` 同名行段（`ngx_shmtx_lock` 实际定义在 `src/core/ngx_shmtx.c` 第 69–133 行）。

[src/core/ngx_shmtx.c:L136-L146](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_shmtx.c#L136-L146) — `ngx_shmtx_unlock`：CAS 把 `lock` 从 `ngx_pid` 改回 0，成功则唤醒一个等待者。

[src/core/ngx_shmtx.c:L149-L161](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_shmtx.c#L149-L161) — `ngx_shmtx_force_unlock(mtx, pid)`：按指定 pid 强制释放（只有当 `*lock == pid` 才 CAS 回 0）。用于持有锁的 worker 异常退出后的清理。

[src/core/ngx_shmtx.c:L164-L196](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_shmtx.c#L164-L196) — `ngx_shmtx_wakeup`：原子地把 `wait` 计数减一并 `sem_post`，唤醒一个睡眠在 `sem_wait` 的等待者，避免「锁释放了但没人被叫醒」。

**用法之一：slab 池的 mutex。** 4.1.3 已看到 `ngx_init_zone_pool` 调 `ngx_shmtx_create(&sp->mutex, &sp->lock, file)`，于是每个共享 slab 池自带一把锁；`ngx_slab_alloc`/`ngx_slab_free` 自动加解锁。

**用法之二：limit_req 在更大临界区里手动加锁。** 因为它要「查树 + 分配 + 插入 + 计费」一气呵成，不能让 `ngx_slab_alloc` 自己的锁只保护分配那一瞬间，所以它直接持池锁并调用 `_locked`：

[src/http/modules/ngx_http_limit_req_module.c:L246-L251](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_limit_req_module.c#L246-L251) — `ngx_shmtx_lock(&ctx->shpool->mutex)` → `ngx_http_limit_req_lookup(...)`（内部 [L494](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_limit_req_module.c#L494) 用 `ngx_slab_alloc_locked` 分配红黑树节点）→ `ngx_shmtx_unlock`。整段是原子临界区。

**用法之三：accept_mutex 防惊群。**

[src/event/ngx_event.c:L580-L588](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L580-L588) — nginx 启动时在一段专用共享内存上 `ngx_shmtx_create(&ngx_accept_mutex, ...)` 建立全局 accept 锁，`spin = (ngx_uint_t) -1`（[L581](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L581)）表示**纯 trylock、不自旋**——accept 锁只该被一个 worker 短暂持有，抢不到就干活去。

[src/event/ngx_event_accept.c:L345-L379](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L345-L379) — `ngx_trylock_accept_mutex`：`ngx_shmtx_trylock` 抢锁，抢到则 `ngx_enable_accept_events`（把监听 fd 加入 epoll），抢不到则若自己之前持有过就 `ngx_disable_accept_events`。这样任意时刻只有一个 worker 在 accept，杜绝惊群。（详见 u5-l5。）

#### 4.3.4 代码实践

**目标**：感受 shmtx 在 slab 分配中的串行化效果。

**步骤**：

1. 配置 `limit_req_zone $binary_remote_addr zone=one:10m rate=100r/s;`（给一个较宽的速率，方便压测）。
2. `worker_processes 4;`。
3. 用 `ab -n 100000 -c 50` 高并发压测。
4. 同时在另一终端用 `perf top -p $(cat logs/nginx.pid)` 或 `pidstat -t 1` 观察 worker。

**预期现象**：高并发下，多个 worker 争抢 `ctx->shpool->mutex`，部分时间会出现在 `ngx_shmtx_lock`/`ngx_cpu_pause` 附近（自旋）。这正是锁在工作的证据。

**预期结果**：能看到 worker 在 shmtx 自旋路径上消耗少量 CPU；若把 zone 调到极小（如 `1m`）加剧争用，现象更明显。**待本地验证**（需要 perf 工具权限）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ngx_shmtx_lock` 存的是 `ngx_pid` 而不是固定值 1？
**答**：为了支持 `ngx_shmtx_force_unlock(mtx, pid)`。当某 worker 持锁期间被异常终止，锁里残留它的 pid；master/其他进程发现后可按该 pid 强制 CAS 回 0，释放这把孤儿锁。若存固定值 1，就无法判断该不该释放。

**练习 2**：accept_mutex 用 `trylock`（不自旋），而 slab 的 mutex 用 `lock`（自旋 2048）。为什么策略不同？
**答**：accept 锁的目的是「选一个 worker 去 accept」，抢不到的 worker 应当**立即去处理已有连接**，自旋纯属浪费；而 slab 锁保护的临界区很短，且申请内存的 worker **必须**等到内存才能继续，自旋一会儿比让出 CPU（触发重新调度、缓存失效）更高效。

---

### 4.4 rwlock 读写锁：读多写少的共享状态

#### 4.4.1 概念说明

`ngx_shmtx_t` 是**互斥锁**：任何时刻只有一个进程能进入，不管是读还是写。但有些共享数据**读远多于写**——比如 upstream 的 peer 状态：每来一个请求都要「读」peer 列表选一台，只有偶尔的健康检查失败才「写」标记宕机。互斥锁会让大量并发的读互相阻塞，浪费多核。

`ngx_rwlock_t`（read/write lock）解决这个问题：

- **多个读可并发**：只要没人写，任意数量的进程可同时持有读锁。
- **写独占**：有人写时，其他读和写都等待；写也要等所有读结束。

nginx 的 rwlock 同样基于一个共享原子字，靠 CAS 自己实现，无系统调用，适合在共享内存里保护高频读的状态。

#### 4.4.2 核心流程

用一个原子字 `lock` 编码三种状态：

- `lock == 0`：空闲。
- `lock == NGX_RWLOCK_WLOCK`（即全 1，`(ngx_atomic_uint_t)-1`）：被写锁持有。
- `lock == 正数 N`：被 N 个读者并发持有。

加锁/解锁逻辑：

```
wlock: CAS(lock, 0, WLOCK)            // 必须从 0 一步抢成全 1
rlock: 若 lock != WLOCK，CAS(lock, r, r+1)   // 只在无写时把读者数 +1
unlock: 若原值是 WLOCK → CAS 回 0；否则读者数 -1
downgrade: 把写锁(WLOCK)直接降级成「1 个读者」(置 1)
```

读写都带与 shmtx 同款的指数退避自旋（`NGX_RWLOCK_SPIN = 2048`），多核下先忙等、再 `sched_yield`。

#### 4.4.3 源码精读

[src/core/ngx_rwlock.c:L15-L16](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rwlock.c#L15-L16) — `NGX_RWLOCK_SPIN = 2048`、`NGX_RWLOCK_WLOCK = (ngx_atomic_uint_t)-1`（写锁标记）。

[src/core/ngx_rwlock.c:L19-L48](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rwlock.c#L19-L48) — `ngx_rwlock_wlock`：循环尝试 `*lock == 0 && CAS(lock, 0, WLOCK)`，多核时指数退避自旋，否则 `sched_yield`。

[src/core/ngx_rwlock.c:L51-L86](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rwlock.c#L51-L86) — `ngx_rwlock_rlock`：读取当前 `readers`，只要 `readers != WLOCK`（即无写锁），就 `CAS(lock, readers, readers+1)` 成功；否则自旋重试。多个读者可同时把计数往上加。

[src/core/ngx_rwlock.c:L89-L97](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rwlock.c#L89-L97) — `ngx_rwlock_unlock`：若原值是 `WLOCK`（写锁）则 CAS 回 0；否则 `fetch_add(lock, -1)` 把读者数减一。

[src/core/ngx_rwlock.c:L100-L106](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rwlock.c#L100-L106) — `ngx_rwlock_downgrade`：把写锁直接写成 1（降为「一个读者」），用于「写完后想继续读、但允许其他读者进来」的场景，避免先释放写锁再加读锁的空窗。

**典型用户**：当配置了 `zone` 指令的 upstream（`ngx_http_upstream_zone_module`）把 peer 状态放进共享内存、供多 worker 共享时，读 peer 列表用 rlock、修改 peer 状态用 wlock/downgrade。文件末尾的编译断言（[L111-L115](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rwlock.c#L111-L115)）说明 rwlock 仅在启用 upstream zone 时被需要——没有原子 CAS 就直接 `#error`。

#### 4.4.4 代码实践

**目标**：对比 rwlock 与 shmtx 在读多场景下的语义差异（源码阅读型）。

**步骤**：

1. 打开 [src/core/ngx_rwlock.c:L51-L86](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rwlock.c#L51-L86)（`rlock`）与 [src/core/ngx_shmtx.c:L62-L66](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_shmtx.c#L62-L66)（`trylock`）。
2. 想象 4 个 worker 同时「读」同一份 peer 状态：
   - 用 shmtx：第 1 个拿到，其余 3 个自旋等待，串行进入。
   - 用 rlock：4 个都看到 `readers` 非负，各自 `CAS(+1)` 成功，**4 个并发读**。

**预期结果**：在只读路径上，rwlock 的并发度是 shmtx 的 N 倍（N = worker 数）。这就是 upstream zone 等读多写少场景选择 rwlock 的原因。

#### 4.4.5 小练习与答案

**练习 1**：`ngx_rwlock_rlock` 里为什么是「读取 readers → CAS(readers, readers+1)」两步，而不是直接 `fetch_add(lock, 1)`？
**答**：必须先判断 `readers != WLOCK`（没有写者）。若直接 +1，可能在写者持有（`lock == WLOCK`）时把计数加乱，破坏写锁标记。两步 CAS 保证了「只在确认无写锁」的前提下才增加读者，CAS 失败（说明中间状态变了）就重读重试。

**练习 2**：`ngx_rwlock_downgrade` 有什么用？
**答**：持有写锁的进程写完后，若想**继续读**这块数据，可以先 `downgrade` 把写锁（`WLOCK`）直接改成「一个读者」（置 1）。这样它自己继续读，同时**立即放行其他等待的读者**，避免「先 unlock 写锁、再 rlock」之间被别人抢去写锁的空窗，减少抖动。

---

## 5. 综合实践：画出 limit_req_zone 的数据存放位置

本任务贯穿本讲三大模块（共享内存 + slab + shmtx），把 `limit_req_zone` 这条指令的数据「物理位置」与「保护机制」彻底讲清楚。

### 配置

```nginx
http {
    limit_req_zone $binary_remote_addr zone=one:10m rate=10r/s;

    server {
        listen 80;
        location / {
            limit_req zone=one burst=20;
            proxy_pass http://backend;
        }
    }
}
```

### 任务

请在一张图上标出：10MB 共享区是怎么被切分的？红黑树根、每个限流节点、锁分别放在哪里？请求来时谁加锁、谁分配？把下面的骨架补全并对着源码核对。

### 数据存放位置图（参考答案）

```
┌─ cycle->shared_memory 链表（ngx_cycle.c:68） ─────────────────────┐
│  zone{name="one", size=10m, tag=&ngx_http_limit_req_module,        │
│       init=ngx_http_limit_req_init_zone, addr=...}                  │
└────────────────────────────────────────────────────────────────────┘
                          │ ngx_init_cycle 物化：
                          │  ngx_shm_alloc → mmap 10MB (ngx_shmem.c:14)
                          │  ngx_init_zone_pool → 段首建 slab 池 + 锁
                          ▼
   共享段 addr ──────────────────────────────────────────────── addr+10m
   ┌─────────────────────────────────────────────────────────────────┐
   │ ngx_slab_pool_t 池头                                            │
   │   ├ lock     (ngx_shmtx_sh_t)  ← 「锁本身」在共享内存         │
   │   ├ mutex    (ngx_shmtx_t)     ← spin/CAS 指针，私有           │
   │   ├ data ──────────────────────┐  ← 指向模块根对象 sh          │
   │   ├ pages[] / free / start/end │                                │
   │   └ ...                         │                                │
   ├─────────────────────────────────┼──────────────────────────────┤
   │ slots[] / stats[] / pages[] 描述符                              │
   ├─────────────────────────────────┼──────────────────────────────┤  ← start
   │  ↑ sh = ngx_slab_alloc(...) 分配出的 ngx_http_limit_req_shctx_t│
   │      ├ rbtree 根 + sentinel  (ngx_http_limit_req_module.c:743)  │
   │      └ queue (LRU 队列)                                          │
   │                                                                  │
   │  ↑ 各个红黑树节点 = ngx_slab_alloc_locked(shpool, size)          │
   │      含 key 哈希、excess（漏桶水量）、data($binary_remote_addr)  │
   │      (ngx_http_limit_req_module.c:494)                          │
   │                                                                  │
   │            ……其余 slab 管理的页帧……                              │
   └─────────────────────────────────────────────────────────────────┘  ← end
```

### 关键源码对应（请逐一核对）

1. **登记**：[ngx_http_limit_req_module.c:L941-L942](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_limit_req_module.c#L941-L942) — `ngx_shared_memory_add(cf, &name, size, &ngx_http_limit_req_module)`，登记 10MB 区。`size` 来自 `zone=one:10m` 的 `10m`（经 `ngx_parse_size` 解析，见 u2-l2）。

2. **init 回调绑定**：[ngx_http_limit_req_module.c:L956](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_limit_req_module.c#L956) — `shm_zone->init = ngx_http_limit_req_init_zone;`。

3. **物化 + 建池**：[ngx_cycle.c:L493-L501](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L493-L501) — `ngx_shm_alloc`（mmap）→ `ngx_init_zone_pool`（段首建 slab 池）→ 调 `init`。

4. **slab 池即段首**：[ngx_http_limit_req_module.c:L728](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_limit_req_module.c#L728) — `ctx->shpool = (ngx_slab_pool_t *) shm_zone->shm.addr;`，模块直接把共享段首部当 slab 池用。

5. **从 slab 分配根对象**：[ngx_http_limit_req_module.c:L736-L746](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_limit_req_module.c#L736-L746) — `ctx->sh = ngx_slab_alloc(shpool, sizeof(...shctx_t))` 分配含红黑树的结构，`ngx_rbtree_init` 初始化它，并把它存到 `shpool->data`。

6. **请求路径：加锁 → 查/插 → 解锁**：[ngx_http_limit_req_module.c:L246-L251](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_limit_req_module.c#L246-L251) — `shmtx_lock` → `limit_req_lookup`（未命中时 [L494](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_limit_req_module.c#L494) `ngx_slab_alloc_locked` 分配新节点并插入红黑树）→ `shmtx_unlock`。

### 需要观察的现象 / 预期结果

- 所有 worker 的 `ctx->shpool` 指向**同一物理地址**（共享映射），故红黑树、节点、计数全部跨 worker 共享——这正是「同一个 IP 无论被哪个 worker 收到，限流都累计」的根本原因。
- 因为整段「查树+分配+插入+计费」被 `shmtx_lock`/`unlock` 包住，并发写不会踩坏 slab 与红黑树。
- `reload` 后（zone 名/大小未变）走 [ngx_cycle.c:L473-L488](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L473-L488) 复用路径，计数不归零。

**待本地验证**：用 `nginx -s reload` 后立即压测，对比 reload 前后的限流行为是否连续（计数未清零）。

## 6. 本讲小结

- nginx 用 **mmap(`MAP_SHARED`)** 申请跨进程共享的内存段（`ngx_shmem.c`），worker 经 `fork` 继承同一物理页，从而看到同一份数据。
- **`ngx_shared_memory_add`** 只在配置阶段「登记」一个区（名/大小/tag/init），真正的 mmap 与建池发生在 `ngx_init_cycle`；reload 时按「同名同 tag 同 size」**复用**旧段，保住限流计数与缓存。
- **slab 分配器**在共享段内做精细分配：小块用位图（SMALL/EXACT/BIG 三档编码以省元数据），超半页走整页分配；`ngx_slab_alloc` 自带池锁，`_locked` 变体供已持锁的临界区使用。
- **`ngx_shmtx_t`** 是基于 CAS 的进程间自旋锁，锁值存 pid 以支持 `force_unlock`；trylock 不等待（accept_mutex 用），lock 自旋 2048 再让出/睡眠（slab 用）。
- **`ngx_rwlock_t`** 用一个原子字编码「写锁=全 1 / N 个读者=正数 N」，读多写少时可让多 worker 并发读，典型用户是 upstream zone。
- 三者关系：共享内存提供「场地」，slab 在场地里「切分」,shmtx/rwlock 在切分时「排队」——`limit_req_zone` 是三者协同的范例。

## 7. 下一步学习建议

- **u5-l4（定时器与 posted 事件）**：定时器底座是 u2-l3 讲过的红黑树，而限流漏桶的「过期淘汰」正是用 limit_req 自己的 LRU 队列 + 定时回收，可对照阅读。
- **u5-l5（事件主循环）**：会详细讲 `ngx_trylock_accept_mutex` 在 `ngx_process_events_and_timers` 里的调度位置，把本讲的 accept_mutex 用法放进完整循环理解。
- **u7-l4（upstream 调度算法）**：`upstream_zone` 把 peer 状态放进共享内存并用 rwlock 保护，是本讲读写锁的直接进阶用例。
- **u10-l1（共享文件缓存）**：`proxy_cache` 的元数据同样存在共享内存 + slab 上，并多了 cache manager/loader 进程，可看作本讲模式的大型应用。
- 建议继续精读 `src/core/ngx_slab.c` 的 `ngx_slab_free_locked`（本讲未展开），体会位图回收与「页全空后归还 free 链表并尝试合并相邻空闲页」的逻辑。
