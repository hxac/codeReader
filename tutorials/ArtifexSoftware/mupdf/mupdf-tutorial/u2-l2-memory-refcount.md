# 内存管理与引用计数

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 MuPDF 的「内存分配器回调」`fz_alloc_context` 是什么、为什么几乎所有内存操作最终都走到 `ctx->alloc` 这一组函数指针上。
- 写出 `fz_malloc` / `fz_free` 在「分配失败」时的行为，并理解它如何借助 `fz_store_scavenge` 在 OOM（内存耗尽）时自救。
- 掌握 `fz_keep_*` / `fz_drop_*` 的引用计数配对规则，能解释 `fz_keep_imp` / `fz_drop_imp` 两个内联函数为什么要在加锁的前提下改 `refs`。
- 认识 `fz_storable` 这个「可存储对象」头部，理解它如何把「引用计数 + 自定义析构」统一成一套通用基础设施，并知道 `fz_keep_storable` / `fz_drop_storable` 与具体对象的 `fz_keep_pixmap` / `fz_drop_pixmap` 是什么关系。

本讲承接 [u2-l1](u2-l1-context.md)：上一讲我们知道了 `fz_context` 是「全局状态容器」，本讲就来打开它的 `alloc` 字段，看 MuPDF 是如何在这套容器之上做内存与生命周期的。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：C 没有 RAII，所以「谁来释放」必须靠约定。**
在 C++ / Rust / 带垃圾回收的语言里，对象的生命周期有编译器或运行时兜底。但 MuPDF 是纯 C。一个对象可能被页面引用、被显示列表引用、被缓存（store）引用，到底什么时候能真正 `free`？MuPDF 的答案是用**引用计数（reference counting）**：每多一个持有者就 `+1`，每少一个就 `-1`，归零才真正释放。这就是本讲的 `keep` / `drop` 机制。

**直觉二：分配器要可替换，所以「分配」本身是回调。**
有的嵌入式平台没有标准 `malloc`，有的应用想统计内存、限制内存、把内存放进固定池子。为此 MuPDF 不直接调用 `malloc`，而是通过 `ctx->alloc.malloc` 这种「函数指针 + 私有数据」的回调来分配。你自己提供回调，就接管了所有内存。

**直觉三：单线程的「计数」是普通的，多线程的「计数」要加锁。**
`refs = refs + 1` 在单线程里没问题，但在多线程里两个线程可能同时读到旧值、各自加一、写回，结果只加了一。MuPDF 用一把 `FZ_LOCK_ALLOC` 锁把所有对引用计数的读改写包起来。理解这一点，后面看 `fz_keep_imp_aux` 时就不会奇怪为什么要 `fz_lock`。

> 名词速查：
> - **OOM（Out Of Memory）**：内存申请失败，分配器返回 `NULL`。
> - **RAII**：资源获取即初始化，一种把资源生命周期绑定到对象作用域的语言机制，C 没有。
> - **引用计数（refcount）**：记录「当前有多少个持有者」的整数，归零即释放。
> - **scavenge（清理 / 回收）**：在内存紧张时，从缓存里淘汰可丢弃对象以腾出空间。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [include/mupdf/fitz/context.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h) | 定义 `fz_alloc_context` 分配器回调结构、`fz_keep_imp`/`fz_drop_imp` 引用计数内联函数、`FZ_STORE_DEFAULT` 等常量。 |
| [source/fitz/memory.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c) | 实现 `fz_malloc`/`fz_free`/`fz_realloc` 等分配入口、默认分配器 `fz_alloc_default`，以及 `fz_strdup`/`fz_new_string` 等带引用计数的字符串示例。 |
| [include/mupdf/fitz/store.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/store.h) | 定义 `fz_storable` 可存储对象头部、`FZ_INIT_STORABLE` 宏，以及通用的 `fz_keep_storable`/`fz_drop_storable` 声明。 |
| [source/fitz/store.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c) | 实现 `fz_keep_storable`/`fz_drop_storable`，把通用计数委托给 `fz_keep_imp`/`fz_drop_imp`。 |
| [include/mupdf/fitz/system.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/system.h) | 平台抽象层：基础整数类型、内存块对齐契约 `FZ_MEMORY_BLOCK_ALIGN_MOD`、`fz_is_pow2` 等。 |
| [source/fitz/pixmap.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/pixmap.c) | 一个「把 `fz_storable` 用起来」的真实例子：`fz_pixmap` 的 keep/drop/drop_imp 三件套。 |
| [source/fitz/context.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c) | `fz_new_context_imp` 第一阶段如何用传入的分配器分配 `fz_context` 本身。 |

记忆口诀：**分配看 `memory.c`，计数看 `context.h`，统一头部看 `store.h`。**

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **分配器回调接口** —— MuPDF 怎么把 `malloc` 变成可替换的回调，并在 OOM 时自救。
2. **keep/drop 引用计数** —— `fz_keep_imp` / `fz_drop_imp` 这对内联函数如何安全地增减计数。
3. **可存储对象 storable** —— `fz_storable` 头部如何把「计数 + 析构」统一成所有缓存对象共用的一套机制。

### 4.1 分配器回调接口

#### 4.1.1 概念说明

MuPDF 不直接 `malloc`/`free`，而是把「怎么分配」抽象成一组回调。这组回调的类型就是 `fz_alloc_context`，它持有三件事：

- `user`：传给回调的私有数据指针（opaque），用于承载你的统计状态、内存池句柄等；
- `malloc` / `realloc` / `free`：三个函数指针，签名与 `stdlib` 的同名函数几乎一致，只是多了一个 `void *user` 首参。

`fz_context` 里有一个 `alloc` 字段就是这个结构（见 [u2-l1](u2-l1-context.md)）。于是所有走 MuPDF 的分配最终都汇流到 `ctx->alloc.malloc(ctx->alloc.user, size)`。这样做有两个直接好处：

1. **可替换**：你可以塞进自己的分配器来统计、限流、或者对接平台的特殊内存；
2. **可自救**：因为所有分配都经过 MuPDF 这一层，MuPDF 能在 `malloc` 返回 `NULL` 时先去自己的缓存（store）里「清理」出空间再重试，而不是直接失败。

#### 4.1.2 核心流程

一次 `fz_malloc(ctx, size)` 的执行过程：

```
fz_malloc(ctx, size)
  └─ if size == 0: 直接返回 NULL（约定：0 字节不分配）
  └─ do_scavenging_malloc(ctx, size)
       ├─ 加 FZ_LOCK_ALLOC 锁
       ├─ 循环：
       │    p = ctx->alloc.malloc(ctx->alloc.user, size)
       │    if p != NULL: 解锁并返回 p   ← 成功
       │    否则：调用 fz_store_scavenge(ctx, size, &phase)
       │           （从缓存里淘汰至少 size 字节，成功则继续循环重试）
       └─ 全部失败：解锁，返回 NULL
  └─ 若返回 NULL：fz_throw(...) 抛出 FZ_ERROR_SYSTEM 异常
```

关键点：**分配失败不是立刻抛异常**。MuPDF 会先尝试 `fz_store_scavenge` 从缓存里抢回空间再重试，只有连缓存都榨干了仍不够，`fz_malloc` 才用 `fz_throw` 抛异常。而 `fz_malloc_no_throw` 则是「不抛异常、直接返回 `NULL`」的兄弟版本，留给那些宁可自己处理失败也不想要异常跳转的调用点。

> 源码顶部的注释把这套约定总结得很清楚：分配函数在 OOM 时会自动 scavenge，仅当缓存也无能为力时才失败；普通版本失败会抛异常，`_no_throw` 版本则静默返回 `NULL`。参见 [source/fitz/memory.c:L37-L42](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L37-L42)。

数组的分配还要防整数溢出。`fz_malloc_array_imp(ctx, nmemb, size)` 在相乘前用 `fz_ckd_mul_size` 做溢出检查：

\[ \text{total} = \text{nmemb} \times \text{size}, \quad \text{要求}\ \text{total} \le 2^{64}-1 \]

一旦溢出就直接 `fz_throw`，绝不让一个「被截断的小总数」蒙混过关去分配出一块不够大的内存（这类 bug 是经典的安全漏洞）。

#### 4.1.3 源码精读

先看分配器回调结构的定义——四个字段，一个私有数据加三个函数指针：

[include/mupdf/fitz/context.h:L50-L56](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L50-L56) —— 定义 `fz_alloc_context`，这是整个内存子系统的「契约」。

再看 `fz_malloc` 的实现，注意它对 `size==0` 的处理和失败时的 `fz_throw`：

[source/fitz/memory.c:L96-L108](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L96-L108) —— `fz_malloc`：0 字节返回 NULL，否则走 `do_scavenging_malloc`，失败抛 `FZ_ERROR_SYSTEM`。

自救逻辑在 `do_scavenging_malloc` 的 `do/while` 循环里：

[source/fitz/memory.c:L44-L67](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L44-L67) —— 加锁后反复尝试 `ctx->alloc.malloc`；失败则 `fz_store_scavenge`（[L63](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L63)）回收缓存再重试，直到无可回收才返回 NULL。

注意它始终持有 `FZ_LOCK_ALLOC` 锁再调用 `ctx->alloc.malloc`——这是因为分配器可能是共享的（多个 context 复用），加锁保证线程安全。`fz_free` 同样如此：

[source/fitz/memory.c:L199-L208](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L199-L208) —— `fz_free`：`p` 为 NULL 时什么都不做；否则加锁后调 `ctx->alloc.free`。

默认分配器就是简单包装 `stdlib`：

[source/fitz/memory.c:L294-L300](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L294-L300) —— `fz_alloc_default`：`user=NULL`，三个回调分别转发到 `malloc/realloc/free`。

在 `fz_new_context_imp` 里，如果调用者传 `NULL`，就用这个默认分配器，并且把整个 `fz_alloc_context` 结构**按值拷贝**进 `ctx->alloc`：

[source/fitz/context.c:L266-L282](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L266-L282) —— `alloc` 为 NULL 时回退到 `fz_alloc_default`（[L267](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L267)），随后 `ctx->alloc = *alloc`（[L281](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L281)）整结构拷贝。注意 `fz_context` 自己也是用这个分配器分配的（[L272](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L272)），所以你的自定义分配器连 context 本身都会经过。

最后是数组的溢出检查：

[source/fitz/memory.c:L119-L126](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L119-L126) —— `fz_malloc_array_imp`：先用 `fz_ckd_mul_size`（[L123](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L123)）做 `nmemb × size` 的溢出检查，再交给 `fz_malloc`。

> 旁注：`fz_ckd_mul_size` 的「检查式乘法」声明在 [include/mupdf/fitz/geometry.h:L942](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h#L942)，能用一个 C23 `ckd_mul` 内建（[L904](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/geometry.h#L904)）或手写回退实现，在乘法溢出时返回非零，是防止「分配出过小缓冲区」的关键防线。

补充：对齐分配 `fz_malloc_aligned`（[source/fitz/memory.c:L211-L232](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L211-L232)）在普通分配之上多申请一段空间再做指针对齐，它依赖 system.h 里的对齐契约 `FZ_MEMORY_BLOCK_ALIGN_MOD`（[include/mupdf/fitz/system.h:L326-L336](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/system.h#L326-L336)）和「对齐必须是 2 的幂」的判断（`fz_is_pow2`，[include/mupdf/fitz/system.h:L510-L513](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/system.h#L510-L513)）。

#### 4.1.4 代码实践

**实践目标**：读懂「分配 → 自救 → 抛异常」这条链，并验证 `user` 私有指针真的能从外层流到回调。

**操作步骤**（源码阅读型实践）：

1. 打开 [source/fitz/memory.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c)，从 `fz_malloc`（[L96](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L96)）向下追到 `do_scavenging_malloc`（[L44](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L44)），再追到 `ctx->alloc.malloc(ctx->alloc.user, size)`。
2. 再打开 [source/fitz/context.c:L266-L282](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L266-L282)，确认 `ctx->alloc = *alloc` 把回调结构整块拷进去。
3. 在纸上画出：你传入的 `fz_alloc_context.user` 是如何被存进 `ctx->alloc.user`，又如何在每次 `malloc` 回调里作为首参传回给你的回调函数。

**需要观察的现象**：所有分配入口（`fz_malloc`、`fz_calloc`、`fz_realloc`）都共享同一个 `do_scavenging_*` 内部函数，OOM 时都会触发 scavenge。

**预期结果**：你能用自己的话讲出「为什么 MuPDF 的 `malloc` 永远不会因为缓存里有可淘汰对象就轻易返回 NULL」。

> 待本地验证：若你想看 scavenge 真的发生，可在一个 `FZ_STORE_DEFAULT` 较小的 context 上反复渲染大图，用调试器在 `fz_store_scavenge` 处下断点观察它是否被命中。

#### 4.1.5 小练习与答案

**练习 1**：`fz_malloc(ctx, 0)` 会分配内存吗？返回什么？
**答**：不会。`size == 0` 时直接返回 `NULL`（[source/fitz/memory.c:L100-L101](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L100-L101)），约定「0 字节不分配」。

**练习 2**：`fz_malloc` 和 `fz_malloc_no_throw` 在 OOM 时的区别是什么？
**答**：`fz_malloc` 在 scavenge 后仍失败时调用 `fz_throw(ctx, FZ_ERROR_SYSTEM, ...)` 抛异常（[L106](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L106)）；`fz_malloc_no_throw` 直接返回 `NULL`，不抛异常（[source/fitz/memory.c:L111-L117](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L111-L117)），适合放在 `fz_try` 之外、希望自行处理失败的调用点。

**练习 3**：为什么 `fz_malloc_array_imp` 必须在相乘前做溢出检查？
**答**：若 `nmemb × size` 溢出，会被截断成一个很小的值，`malloc` 只会分配一小块内存，而调用者却以为拿到了 `nmemb` 个元素的数组，后续写入必然越界。`fz_ckd_mul_size`（[L123](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L123)）在溢出时直接 `fz_throw`，从源头堵住这类缓冲区溢出漏洞。

### 4.2 keep/drop 引用计数

#### 4.2.1 概念说明

C 没有 `shared_ptr`，MuPDF 自己用引用计数实现「共享所有权」。约定是：

- **`fz_keep_X(ctx, obj)`**：表示「我也要持有这个对象」，引用计数 `+1`，返回同一个指针；
- **`fz_drop_X(ctx, obj)`**：表示「我不再需要它了」，引用计数 `-1`；当计数归零，真正释放对象。

铁律：**每一个让计数 `+1` 的 `keep`（含创建时的初始 `refs=1`），都必须有一个 `drop` 与之配对。** 计数失衡会导致两类 bug——少了 `drop` 会内存泄漏，多了 `drop` 会提前释放或重复释放。

为了避免每种对象都重写一遍「加锁 + 自增 / 自减 + 判零」的样板代码，MuPDF 把这套逻辑抽成两个内联函数 `fz_keep_imp` / `fz_drop_imp`。几乎所有具体对象的 `fz_keep_X` / `fz_drop_X` 最终都委托给它们。

#### 4.2.2 核心流程

`keep` 与 `drop` 都是「读改写一个整数」的临界区，必须加 `FZ_LOCK_ALLOC` 锁：

```
fz_keep_imp(ctx, p, &p->refs)
  └─ if p == NULL: 直接返回 NULL
  └─ 加 FZ_LOCK_ALLOC
  └─ if *refs > 0: ++*refs      ← 只有正计数才自增（0/-1 是哨兵，见下）
  └─ 解锁，返回 p

fz_drop_imp(ctx, p, &p->refs)
  └─ if p == NULL: 返回 0
  └─ 加 FZ_LOCK_ALLOC
  └─ if *refs > 0: drop = (--*refs == 0)
  └─ 解锁，返回 drop            ← 返回 1 表示「计数归零，该释放了」
```

注意 `drop` 的返回值：它**不负责释放对象**，只告诉你「现在计数是不是 0」。调用方据此决定要不要调真正的析构函数。这种「计数管理」与「资源释放」分离的设计，让同一套 `fz_drop_imp` 能服务于任何对象。

> 一个细节：代码里 `*refs > 0` 才会自增/自减。`refs == 0` 通常表示「静态对象 / 常量」，`refs == -1` 在 `fz_drop_storable` 里被用作「静态分配」哨兵（见 [4.3](#43-可存储对象-storable)）。也就是说，对这些特殊计数 `keep`/`drop` 都是空操作，不会误释放。

#### 4.2.3 源码精读

`fz_keep_imp` / `fz_drop_imp` 是宏，它们做了一个小技巧：当指针 `p` 为 NULL 时，把 `refs` 参数也替换成 NULL，避免对空对象的成员取址：

[include/mupdf/fitz/context.h:L954-L963](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L954-L963) —— 宏 `(P) ? (R) : NULL` 在 `P` 为空时不把 `refs` 传给底层函数。

`fz_keep_imp_aux` 的实现，注意加锁与「正计数才自增」：

[include/mupdf/fitz/context.h:L965-L980](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L965-L980) —— `fz_keep_imp_aux`：加 `FZ_LOCK_ALLOC`，仅当 `*refs > 0` 时 `++*refs`，再解锁返回 `p`。

`fz_drop_imp_aux` 的实现，注意它返回「是否归零」而不直接释放：

[include/mupdf/fitz/context.h:L1046-L1065](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L1046-L1065) —— `fz_drop_imp_aux`：加锁后 `drop = (--*refs == 0)`，解锁并返回 `drop`；`p` 为 NULL 或 `*refs <= 0` 时返回 0。

`fz_string` 是最小、最干净的「带引用计数的对象」示例，值得完整读一遍。它的结构体里有一个 `refs` 成员，构造时初始化为 1：

[source/fitz/memory.c:L255-L262](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L255-L262) —— `fz_new_string`：用 `fz_malloc_flexible` 分配（柔性数组尾部放字符串），`str->refs = 1` 即「创建即持有一份引用」。

> `fz_malloc_flexible` 是个宏，用 `offsetof(T, M) + sizeof(成员) * count` 计算含柔性数组尾部的对象大小（[include/mupdf/fitz/context.h:L740-L741](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L740-L741)），是 C 里「变长结构体」的标准套路。

`keep` / `drop` 直接委托给 `fz_keep_imp` / `fz_drop_imp`，而 `drop` 在归零时才 `fz_free`：

[source/fitz/memory.c:L264-L273](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L264-L273) —— `fz_keep_string` 委托 `fz_keep_imp`；`fz_drop_string` 用 `fz_drop_imp` 判断，归零才 `fz_free(ctx, str)`。这就是「计数管理」与「释放」分离的范本。

#### 4.2.4 代码实践

**实践目标**：通过最小示例验证 keep/drop 的配对语义。

**操作步骤**（源码阅读 + 推理型实践）：

1. 读 [source/fitz/memory.c:L255-L273](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L255-L273)，画出 `fz_string` 的生命周期：`fz_new_string`（refs=1）→ `fz_keep_string`（refs=2）→ `fz_drop_string`（refs=1）→ `fz_drop_string`（refs=0，触发 `fz_free`）。
2. 设想「忘记一次 `drop`」会发生什么（泄漏），「多一次 `drop`」又会怎样（第二次 `--*refs` 得到 0，再次 `fz_free` 同一块内存 → 重复释放 / 堆损坏）。
3. 阅读宏 [include/mupdf/fitz/context.h:L954-L963](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L954-L963)，解释为什么宏里要写 `(P) ? (R) : NULL`。

**需要观察的现象**：`fz_keep_imp_aux` 和 `fz_drop_imp_aux` 对 `*refs <= 0` 的情况都「什么都不做」（不自增、返回 0 不释放）。

**预期结果**：你能口头复述「为什么 `fz_drop_imp` 返回布尔值而不是直接 `free`」——因为释放方式因对象而异，计数函数只管计数。

> 待本地验证：若想眼见为实，可用 `build=memento`（见 [u1-l2](u1-l2-build-system.md)）编译，Memento 会在 `++*refs`/`--*refs` 处记账（代码里的 `Memento_takeRef`/`Memento_dropIntRef`），运行后能输出未配对的 keep/drop 报告。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `fz_keep_imp_aux` 和 `fz_drop_imp_aux` 都要先 `fz_lock(ctx, FZ_LOCK_ALLOC)` 再改 `refs`？
**答**：引用计数是多线程共享的可变状态。若两个线程同时 `++*refs`，可能都读到旧值再各自写回，结果只加了 1。用 `FZ_LOCK_ALLOC` 把「读—改—写」包成原子临界区，才能保证计数正确（[include/mupdf/fitz/context.h:L971-L977](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L971-L977)）。

**练习 2**：`fz_drop_string` 里写成 `if (fz_drop_imp(...)) fz_free(ctx, str);`，能否改成直接 `fz_free`？
**答**：不能。`fz_drop_imp` 只有在计数归零时才返回真。若直接 `fz_free`，只要有别的持有者还在用这个字符串，就会被提前释放，造成 use-after-free。先判归零再释放，正是引用计数的核心安全保证（[source/fitz/memory.c:L269-L273](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L269-L273)）。

### 4.3 可存储对象 storable

#### 4.3.1 概念说明

MuPDF 有一个跨文档、跨页面复用对象的缓存叫 `fz_store`（详见 [u9-l1](u9-l1-store-cache.md)）：解码好的字形、图片、字体都能放进去，下次再用就直接复用，不用重新解码。能放进 store 的对象必须满足一个统一契约——以 `fz_storable` 作为结构体的**第一个成员**。

`fz_storable` 这个头部只有三个字段：

- `refs`：引用计数；
- `drop`：析构函数指针，计数归零时被调用以释放对象；
- `droppable`：可选的「现在能不能被淘汰」查询函数。

把头部统一后，MuPDF 就能提供两个**通用**函数 `fz_keep_storable` / `fz_drop_storable`，对任何派生对象都适用。于是具体对象（如 `fz_pixmap`、`fz_font`、`fz_image`）的 `fz_keep_X` / `fz_drop_X` 通常只有一行——转调通用版本。这就是「一次实现，处处复用」。

> 为什么用「头部嵌入」而不是继承？因为 C 没有继承。把 `fz_storable` 放在结构体最前面，那么「指向 `fz_pixmap` 的指针」和「指向其 `fz_storable` 成员的指针」在内存里地址相同，可以安全地互相转换（C 标准允许「指向首个成员」的转换）。这就是 C 里实现「多态」的标准手法。

#### 4.3.2 核心流程

一个可存储对象从创建到消亡：

```
1. 分配对象（如 fz_pixmap），用 FZ_INIT_STORABLE(&pix->storable, 1, fz_drop_pixmap_imp)
   → refs = 1，drop = fz_drop_pixmap_imp，droppable = NULL

2. 谁要共享就 fz_keep_pixmap(ctx, pix)  →  转调 fz_keep_storable  →  refs++

3. 谁用完就 fz_drop_pixmap(ctx, pix)   →  转调 fz_drop_storable
        └─ 加锁，refs-- 
        └─ 若 refs 归零：调用 s->drop(ctx, s)  →  真正析构（fz_drop_pixmap_imp）
        └─ （若剩余恰好 1 且可能被 store 持有，还会触发一次 scavenge 收缩缓存）
```

构造用宏、keep/drop 转通用、析构走回调——三件套配合，就把「生命周期管理」从每种对象里彻底抽离出来了。

#### 4.3.3 源码精读

`fz_storable` 头部定义和它的初始化宏：

[include/mupdf/fitz/store.h:L76-L80](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/store.h#L76-L80) —— `struct fz_storable`：`refs` + `drop` 析构指针 + `droppable` 可淘汰查询指针。

[include/mupdf/fitz/store.h:L96-L99](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/store.h#L96-L99) —— `FZ_INIT_STORABLE` 宏：把 `refs` 设为给定值、`drop` 设为给定析构函数、`droppable` 置 NULL。

通用 keep/drop 的声明（注释强调它们「永不抛异常」）：

[include/mupdf/fitz/store.h:L114-L129](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/store.h#L114-L129) —— `fz_keep_storable` 自增并返回同指针；`fz_drop_storable` 自减，归零则调 `drop` 释放。

实现里，通用版本只是「去掉 const 后转调 `fz_keep_imp`」：

[source/fitz/store.c:L88-L97](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L88-L97) —— `fz_keep_storable`：显式去 const，委托 `fz_keep_imp(ctx, s, &s->refs)`。

`fz_drop_storable` 则在自减归零后调用 `s->drop(ctx, s)`，并对「剩 1 个引用且可能在 store 里」的情况触发一次 scavenge：

[source/fitz/store.c:L870-L890](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L870-L890) —— `fz_drop_storable`：加锁后 `--s->refs`，归零由 `drop` 回调释放；并把 `refs == -1` 解释为「静态分配对象」，对此不做任何释放。

> 这里的 `refs == -1` 与 `fz_drop_imp` 里「`*refs <= 0` 不释放」是配套的：静态/常量对象用 `-1` 标记，从而永远不被释放，也不会被 keep 误增。

现在看一个真实对象如何把这套机制用起来——`fz_pixmap`。它的 `keep`/`drop` 各只有一行转调：

[source/fitz/pixmap.c:L34-L47](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/pixmap.c#L34-L47) —— `fz_keep_pixmap` / `fz_drop_pixmap` 直接转调 `fz_keep_storable` / `fz_drop_storable`，传入 `&pix->storable`。

真正的「释放 pixmap」逻辑写在析构回调 `fz_drop_pixmap_imp` 里，由 `fz_storable.drop` 在归零时调用：

[source/fitz/pixmap.c:L49-L60](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/pixmap.c#L49-L60) —— `fz_drop_pixmap_imp`：先 drop 掉它引用的颜色空间、分离色、底层 pixmap，再按标志释放 `samples`，最后 `fz_free(ctx, pix)` 释放结构体本身。注意它本身也在用 `fz_drop_*` 释放下级对象——引用计数是层层传递的。

最后是构造处的初始化，把头部三件套接好：

[source/fitz/pixmap.c:L81](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/pixmap.c#L81) —— `FZ_INIT_STORABLE(pix, 1, fz_drop_pixmap_imp)`：新建 pixmap 时 `refs=1`，析构绑定到 `fz_drop_pixmap_imp`。

store.h 顶部有一段很值得读的「设计说明」，把「为什么需要统一头部」「drop_fn 同时充当类型标识」讲得很清楚：

[include/mupdf/fitz/store.h:L32-L52](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/store.h#L32-L52) —— 说明所有可缓存对象都派生自 `fz_storable`，从而获得一致的线程安全引用计数与析构钩子。

#### 4.3.4 代码实践

**实践目标**：以 `fz_pixmap` 为样本，掌握「实现一个可存储对象」需要填的三个接缝点。

**操作步骤**（源码阅读型实践）：

1. 读 [source/fitz/pixmap.c:L34-L60](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/pixmap.c#L34-L60)，把 pixmap 的三件套对应到通用机制：
   - `fz_keep_pixmap` / `fz_drop_pixmap` → 转调通用 `fz_keep_storable` / `fz_drop_storable`；
   - `fz_drop_pixmap_imp` → 作为 `storable.drop` 回调，归零时被通用 `fz_drop_storable` 调用。
2. 读 [source/fitz/pixmap.c:L81](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/pixmap.c#L81)，确认构造时用 `FZ_INIT_STORABLE` 把这三者绑好。
3. 思考：如果 `fz_pixmap` 结构体里把 `storable` 放到**第二个**成员而不是第一个，`fz_keep_pixmap` 里 `&pix->storable` 与对象首地址不再相同，会出什么问题？

**需要观察的现象**：析构回调 `fz_drop_pixmap_imp` 内部先 `fz_drop_colorspace` 等再 `fz_free`，体现「引用计数沿对象图向下传递」。

**预期结果**：你能列出实现一个新的可存储对象必须做的三件事——①把 `fz_storable` 放结构体首位；②构造时 `FZ_INIT_STORABLE`；③写一个析构回调并在其中 drop 掉它引用的其它对象。

> 待本地验证：用 `grep -n "FZ_INIT_STORABLE" source/fitz/*.c` 查看还有哪些对象（image、font、colorspace 等）用了同一套模式，验证「一次实现、处处复用」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `fz_storable` 必须是结构体的**第一个**成员？
**答**：C 里「指向结构体的指针」与「指向其首个成员的指针」地址相同、可安全互转。`fz_keep_storable` 收到的是 `fz_storable *`，若头部在首位，就能无开销地在「具体对象指针」与「头部指针」间转换，从而让一套通用 keep/drop 服务于所有派生对象。若不在首位，这个等价就不成立。

**练习 2**：`fz_drop_pixmap_imp` 里为什么先 `fz_drop_colorspace`、`fz_drop_pixmap(underlying)`，最后才 `fz_free(ctx, pix)`？
**答**：对象可能引用其它引用计数对象（pixmap 持有 colorspace、底层 pixmap）。析构时必须先把它们的引用 drop 掉（让它们有机会归零释放），再释放自身结构体。顺序反过来会导致 `fz_free(pix)` 之后再去访问 `pix->colorspace`，变成 use-after-free（[source/fitz/pixmap.c:L49-L60](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/pixmap.c#L49-L60)）。

**练习 3**：`fz_drop_storable` 里 `refs == -1` 代表什么？
**答**：代表「静态分配的对象」（如编译期常量），不应被释放。代码遇到 `-1` 时 `num = -1`，既不自减也不调用 `drop`，让它永远存活（[source/fitz/store.c:L889-L890](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L889-L890)）。

## 5. 综合实践

把本讲的「自定义分配器」和「keep/drop 生命周期」串起来，写一个统计内存的小程序：用一个自制的 `fz_alloc_context` 包裹 `malloc`，统计「累计分配次数」与「当前未释放块数」，用它创建 context、渲染一页、再按 [u1-l5](u1-l5-first-render.md) 的「标准三步 + 逆序释放」收尾，最后打印计数，验证内存被正确归还。

**实践目标**：亲眼看到「所有 `keep`/`new` 都有对应的 `drop`，渲染一页后 `live` 归零」。

**操作步骤**：

1. 以 `docs/examples/example.c`（见 [u1-l5](u1-l5-first-render.md)）为骨架，在 `main` 顶部加入下面这份自定义分配器（示例代码，非项目原有代码）：

   ```c
   /* 示例代码：自定义分配器，统计分配/释放 */
   #include <mupdf/fitz.h>
   #include <stdio.h>

   static long g_total = 0;   /* 累计成功 malloc 次数 */
   static long g_live  = 0;   /* 当前未释放的块数   */

   static void *my_malloc(void *user, size_t size)
   {
       void *p = malloc(size);          /* 直接转发给系统 */
       if (p) { g_total++; g_live++; }
       return p;                        /* 返回 NULL 时 mupdf 会自动 scavenge/抛异常 */
   }
   static void *my_realloc(void *user, void *old, size_t size)
   {
       void *p = realloc(old, size);
       if (p && !old) { g_total++; g_live++; }   /* 仅当 old==NULL（等价 malloc）时计为新增 */
       return p;
   }
   static void my_free(void *user, void *ptr)
   {
       if (ptr) g_live--;
       free(ptr);
   }

   static const fz_alloc_context my_alloc = {
       NULL,           /* user：本例不需要私有状态 */
       my_malloc,
       my_realloc,
       my_free,
   };
   ```

2. 把创建 context 那一行从 `fz_new_context(NULL, NULL, FZ_STORE_DEFAULT)` 改成传入自定义分配器：

   ```c
   fz_context *ctx = fz_new_context(&my_alloc, NULL, FZ_STORE_DEFAULT);
   ```

3. 保留 example.c 原有的「注册 handler → 打开文档 → 渲染单页为 pixmap → 写出 PPM」逻辑，**注意渲染得到的 pixmap 是 `refs=1` 的新对象，必须 `fz_drop_pixmap`**（这正是 [4.3](#43-可存储对象-storable) 学到的 keep/drop 配对）。

4. 在 `fz_drop_context(ctx)` 之后，打印计数：

   ```c
   printf("total_allocs=%ld  live=%ld\n", g_total, g_live);
   ```

5. 编译运行（参考 [u1-l2](u1-l2-build-system.md) 与 [u1-l5](u1-l5-first-render.md) 的编译方式）。

**需要观察的现象**：

- `total_allocs` 是一个正数（说明确实有大量分配发生，包括 `fz_context` 自身——见 [4.1.3](#413-源码精读) 里 `ctx->alloc = *alloc` 后 context 也是用你的分配器建的）。
- `live` 应当为 `0`（所有分配都被对应释放）。

**预期结果**：若 `live` 为 0，说明你正确地为每一个 `new`/`keep` 配对了 `drop`，渲染整页没有泄漏；若 `live > 0`，通常是漏掉了某个 `fz_drop_*`（常见就是漏 drop pixmap 或 document）。把 `build=memento`（见 [u1-l2](u1-l2-build-system.md)）打开可进一步定位泄漏点。

> 待本地验证：不同文档、不同缩放下 `total_allocs` 数值会变化，但正确收尾后 `live` 必须稳定为 0。若你改坏了释放顺序（例如在 `fz_drop_context` 之前没有 drop pixmap），`live` 通常仍可能凑成 0（因为 context 析构会连带清理），但会触发重复释放或断言——这正是 keep/drop 配对纪律的价值。

## 6. 本讲小结

- MuPDF 不直接 `malloc`/`free`，一切分配都走 `ctx->alloc` 这组回调（`fz_alloc_context`，[context.h:L50-L56](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L50-L56)）；传入 `NULL` 时回退到默认分配器 `fz_alloc_default`。
- 分配在 OOM 时不会立刻失败：`fz_malloc` 先 `fz_store_scavenge` 从缓存抢空间再重试，仍失败才 `fz_throw`；`_no_throw` 版本则静默返回 `NULL`。
- 引用计数是 MuPDF 的「共享所有权」机制：`fz_keep_*` 自增、`fz_drop_*` 自减，每个 `keep`/初始 `refs=1` 都要配一个 `drop`；`fz_keep_imp`/`fz_drop_imp` 在 `FZ_LOCK_ALLOC` 下改计数，是所有具体对象复用的内联基础。
- `fz_drop_imp` 只返回「是否归零」而不直接释放，把「计数」与「释放」解耦，释放方式由各对象自定义。
- 可缓存对象统一以 `fz_storable` 作首个成员，构造时 `FZ_INIT_STORABLE`，keep/drop 转调通用 `fz_keep_storable`/`fz_drop_storable`，归零时由 `drop` 回调析构——`fz_pixmap` 是这套模式的范本。
- 引用计数沿对象图层层传递：析构时先 drop 下级对象、最后才 `fz_free` 自身，顺序不能错。

## 7. 下一步学习建议

- **继续往下读 store**：本讲多次提到 `fz_store_scavenge`，但只用了它的「腾空间」语义。它的完整缓存/淘汰/reap 机制请到 [u9-l1 store：对象缓存与清理](u9-l1-store-cache.md) 深入，届时你会看到 `fz_store_item` / `fz_find_item` / `fz_key_storable` 的「key 引用」如何与 `fz_storable` 的普通引用协作。
- **学异常机制**：本讲里 `fz_malloc` 失败会 `fz_throw`，而 keep/drop「永不抛异常」。要理解这两类函数的边界，请学 [u2-l3 异常处理：fz_try / fz_catch](u2-l3-exceptions.md)，掌握 `fz_try`/`fz_catch`/`fz_var` 的用法——它会解释为什么「在 `fz_try` 里分配、在 `fz_catch` 里清理」需要格外小心。
- **用起来**：把本讲的 keep/drop 规则带到 [u3 文档抽象](u3-l1-document-abstraction.md) 与 [u4 设备模型](u4-l1-device-model.md) 中——`fz_document`、`fz_page`、`fz_pixmap`、`fz_device`、`fz_display_list` 全部是引用计数对象，本讲的纪律将贯穿后续每一讲。
