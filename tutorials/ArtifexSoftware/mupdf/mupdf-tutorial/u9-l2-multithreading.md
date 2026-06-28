# 多线程渲染

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「为什么 MuPDF 在多线程下需要 `fz_clone_context`，且创建 context 时必须传入锁函数」。
- 看懂官方示例 `multi-threaded.c` 中「主线程读页 + 工作线程渲染」的协作分工，并能动手修改线程数。
- 理解 context 家族「共享 store/font/glyph_cache，但各自维护独立异常栈」的设计，以及它带来的约束（哪些对象能跨线程传递、哪些不能）。
- 把 `multi-threaded.c` 从「每页一线程」改造成「固定 4 线程的工作池」，并用计时对比验证多线程加速。

## 2. 前置知识

本讲是专家层内容，需要你已经掌握下面两讲建立的认知（本讲在其之上继续，不重复）：

- **u2-l1 fz_context**：`fz_context` 是几乎所有 fitz 函数的第一个参数，内部装着分配器 `alloc`、锁 `locks`、异常栈 `error`，以及 `store`/`font`/`colorspace`/`glyph_cache` 等子上下文。创建入口是 `fz_new_context(alloc, locks, max_store)`。
- **u9-l1 store**：`fz_store` 是挂在 context 家族上的跨文档/跨页/跨线程对象缓存，结构是「LRU 双向链表 + 4096 桶哈希表 + 大小计数」，全部由 `FZ_LOCK_ALLOC` 这一把锁保护，因此天然线程安全。

此外需要一点 POSIX 线程（pthread）常识：`pthread_create` 起线程、`pthread_join` 等线程结束、`pthread_mutex_lock/unlock` 加解锁。MuPDF 本身对线程库一无所知，这些 pthread 调用只出现在**应用层**（示例和 `mudraw`），库内部只通过回调使用锁。

一个最容易踩的坑先点明：**MuPDF 默认的锁是「空函数」**，单线程下够用，一旦你想 `fz_clone_context` 就会被拒绝。这正是本讲要解释的第一件事。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `docs/examples/multi-threaded.c` | 官方多线程渲染示例，本讲主线。「主线程逐页 load→录制 display list→派发；工作线程 clone context→draw device 渲染→回传 pixmap」。 |
| `include/mupdf/fitz/context.h` | context 的全部公开契约：锁结构 `fz_locks_context`、锁编号枚举 `FZ_LOCK_*`、`fz_clone_context` 文档、`struct fz_context` 的共享/非共享字段划分。 |
| `source/fitz/context.c` | `fz_new_context_imp` 与 `fz_clone_context` 的真实实现，是理解「克隆了什么、共享了什么」的唯一权威。 |
| `source/fitz/memory.c` | `fz_locks_default` 的定义——一对什么都不做的空函数，解释了「为什么默认锁下不能克隆」。 |
| `source/fitz/store.c` | `fz_keep_store_context`，证明克隆时 store 只是被「加引用计数」而非复制。 |
| `source/tools/mudraw.c` | 生产级多线程：固定数量 worker 线程池 + 信号量协调 + 分带（band）并行，是 `multi-threaded.c` 的工业版，用于对比参照。 |

## 4. 核心概念与源码讲解

本讲对应三个最小模块：

1. **clone_context 与锁**：为什么多线程要克隆 context、为什么要传锁、克隆具体做了什么。
2. **主/工作线程分工**：`multi-threaded.c` 的协作模式，以及「display list 是跨线程传递页面内容的唯一安全载体」。
3. **共享 store 设计**：context 家族共享缓存但各自维护异常栈，由此带来的「能跨线程传什么」的硬约束。

### 4.1 clone_context 与锁

#### 4.1.1 概念说明

MuPDF 的 `fz_context` 是「全局状态容器」。但 C 没有语言级的线程概念，MuPDF 也刻意不绑定任何线程库（保持可移植）。于是它把「加锁/解锁」这件事设计成两个回调，由**应用**在创建 context 时注入：你想用 pthread、Windows API、C++ 的 `std::mutex` 都行，MuPDF 只管在需要时调用你给的函数。

这里有三个关键约束互相支撑：

- **约束一：异常是「每线程一份」的**。MuPDF 的异常机制（u2-l3 讲过）基于 `setjmp/longjmp`，靠 context 内嵌的异常栈帧驱动。`longjmp` 只能跳回**同一线程**里先前 `setjmp` 的位置，跨线程跳转是未定义行为。因此两个线程绝不能共用同一个异常栈——否则 A 线程抛出的异常会冲进 B 线程的 `fz_catch`。
- **约束二：缓存是「一族 context 共享」的**。`store`、`font_context`、`glyph_cache` 等子上下文解析字体、解码图像开销很大，每个线程各存一份是浪费。设计上让一族 clone 出来的 context 指向**同一个** store。
- **约束三：共享就必须加锁**。多个线程同时读写同一个 store、同一个 `FT_Library`、同一个分配器，必须有锁保护。所以「要 clone 就必须有真锁」。

这三条合起来就推出了本模块的核心结论：**多线程 = 给每个工作线程 `fz_clone_context` 一份独立的 context（独立异常栈），同时让所有 clone 共享同一组缓存（共享 store），共享的部分靠应用注入的锁来串行化。**

#### 4.1.2 核心流程

context 家族的「家谱」用两个字段维护（见 `struct fz_context`）：

- `master`：指向家族的「根 context」。自己指向自己就是根；克隆出来的指向根。
- `context_count`：整个家族当前的 context 个数，**只在 master 上有意义**。

`fz_clone_context(ctx)` 的流程：

1. **守卫**：若 `ctx` 用的是默认空锁，直接返回 NULL——没有真锁就不许克隆。
2. **分配**：用家族的分配器 `malloc` 一个新的 `fz_context`。
3. **记账**：在 `FZ_LOCK_ALLOC` 保护下把 `master->context_count++`，并让新 context 的 `master` 指向同一个根。
4. **整块拷贝**：`memcpy(new_ctx, ctx, sizeof(fz_context))`——把所有字段（包括指向共享缓存的指针）原样复制过去。
5. **重置异常栈**：`fz_init_error_context(new_ctx)`——给新 context 一个干净的、独立的异常栈。
6. **keep 共享子上下文**：对 `store`/`font`/`glyph_cache`/`colorspace` 等逐个调用 `fz_keep_*`，本质是给共享对象的引用计数 `+1`。

`fz_drop_context` 则是逆操作：把 `master->context_count--`，只有当家族计数归零（最后一个 context 退出）才真正释放共享的 store 等资源。

锁的使用遵循一条严格的**偏序规则**来避免死锁（见下方源码注释）。用数学语言说，定义锁上的关系「持有 i 时可申请 j 当且仅当 i < j」：

\[ \text{持有锁 } i \text{ 时，只允许申请锁 } j,\ \text{其中 } i < j \]

这条规则保证了「锁申请图」永远是一条有向无环链，不可能出现 A 等 B、B 等 A 的环，因此**不可能死锁**。MuPDF 内部只有三把锁，编号固定：

| 编号 | 锁名 | 保护对象 |
| --- | --- | --- |
| 0 | `FZ_LOCK_ALLOC` | 内存分配 + store 缓存 + 引用计数 |
| 1 | `FZ_LOCK_FREETYPE` | FreeType 字体光栅化（非线程安全） |
| 2 | `FZ_LOCK_GLYPHCACHE` | 字形位图缓存 |

#### 4.1.3 源码精读

先看公开契约——锁结构与锁编号。MuPDF 要求应用提供 `FZ_LOCK_MAX`（即 3）把互斥锁：

[include/mupdf/fitz/context.h:L269-L281](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L269-L281)

`fz_locks_context` 三个字段：`user`（透传给回调的私有指针）、`lock`、`unlock` 两个函数指针。下方的 `enum` 把三把锁编号定死。注释里明确写了那条死锁防御规则（「不能在已持有任意 i（0 ≤ i ≤ n）时再去取 n」）：

[include/mupdf/fitz/context.h:L262-L267](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L262-L267)

`fz_clone_context` 的文档说清了「共享什么、不共享什么」——共享分配器/store/锁，但各有自己的异常栈：

[include/mupdf/fitz/context.h:L347-L362](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L347-L362)

再看实现。`fz_clone_context` 第一行就是守卫——对比 `ctx->locks.lock` 是否等于默认空锁 `fz_locks_default.lock`，是就直接返回 NULL：

[source/fitz/context.c:L318-L335](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L318-L335)

注意第 332–334 行：`context_count++` 这一步是**在 `FZ_LOCK_ALLOC` 锁保护下**做的，因为家族计数是所有 clone 共享的可变状态。接着第 338 行 `memcpy` 整块拷贝，第 341 行重置异常栈（这一步造就了「独立异常栈」）。

克隆的后半段——对每个共享子上下文调用 `fz_keep_*`（本质是引用计数 +1，证明它们被「共享」而非「复制」）：

[source/fitz/context.c:L343-L354](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L343-L354)

以 store 为例，`fz_keep_store_context` 只是 `fz_keep_imp(ctx, ctx->store, &ctx->store->refs)`——给同一个 store 对象的 `refs` 加 1，指针本身不变：

[source/fitz/store.c:L710-L715](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L710-L715)

那么「默认空锁」到底是什么？看 `memory.c`——`fz_lock_default` 和 `fz_unlock_default` 是两个**空函数体**，什么都不做：

[source/fitz/memory.c:L302-L317](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L302-L317)

这就是为什么单线程程序传 `NULL` 给 `fz_new_context` 能正常工作（默认空锁 = 不加锁 = 单线程下没问题），而一旦 `fz_clone_context` 发现你还在用这把空锁就拒绝执行——因为没有任何机制能保护即将被多线程共享的 store。

最后看应用层怎么注入真锁。`multi-threaded.c` 准备了一个 pthread 互斥锁数组，并定义两个薄封装：

[docs/examples/multi-threaded.c:L139-L153](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/multi-threaded.c#L139-L153)

`main` 里初始化这 `FZ_LOCK_MAX` 把锁，组装成 `fz_locks_context` 后传给 `fz_new_context`：

[docs/examples/multi-threaded.c:L166-L185](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/multi-threaded.c#L166-L185)

注意 `locks.user = mutex`——把锁数组的首地址作为私有指针透传，`lock_mutex` 里再用 `mutex[lock]` 取出对应编号的锁。这样避免了全局变量。第 185 行 `fz_new_context(NULL, &locks, FZ_STORE_UNLIMITED)` 的第二个参数就是这组真锁，有了它后续 `fz_clone_context` 才不会返回 NULL。

#### 4.1.4 代码实践

**实践目标**：亲手验证「没有真锁就无法 clone」这条硬约束。

**操作步骤**：

1. 复制 `docs/examples/multi-threaded.c` 为 `mt-nolock.c`（放示例目录外，避免污染源码）。
2. 把 `main` 中第 185 行改为 `ctx = fz_new_context(NULL, NULL, FZ_STORE_UNLIMITED);`（第二个参数从 `&locks` 改成 `NULL`，即用默认空锁）。
3. 在 `renderer` 函数里 `ctx = fz_clone_context(ctx);` 之后加一行：`if (ctx == NULL) { fprintf(stderr, "clone failed!\n"); return NULL; }`。
4. 编译运行（编译方式见本文件顶部注释：`gcc -I include mt-nolock.c build/release/libmupdf.a build/release/libmupdf-third.a -lpthread -lm -o mt-nolock`，再用一个小 PDF 运行）。

**需要观察的现象**：每个工作线程都会打印 `clone failed!`，因为 `fz_clone_context` 检测到默认空锁直接返回了 NULL。

**预期结果**：所有渲染线程拿不到自己的 context，渲染失败。这反向证明了约束三——要共享就必须有真锁。**待本地验证**（取决于你的编译环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fz_clone_context` 用「比较函数指针是否等于 `fz_locks_default.lock`」来判断有没有真锁，而不是在 `fz_context` 里加一个 `has_real_locks` 布尔字段？

**答案**：因为 `fz_new_context_imp` 在 `locks == NULL` 时会把 `fz_locks_default` 拷进 `ctx->locks`（见 [context.c:L269-L270](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L269-L270)）。所以「没传锁」和「传了默认锁」在 context 里留下的函数指针完全相同，直接比指针既省字段又可靠。

**练习 2**：家族计数 `context_count` 的自增为什么要放在 `FZ_LOCK_ALLOC` 里？如果不在锁里会有什么问题？

**答案**：`context_count` 存在 master context 上，是所有 clone 共享的可变状态。两个线程同时 clone 会导致「读—改—写」竞争，可能丢失一次自增，最终 `fz_drop_context` 时计数提前归零、提前释放仍在被其他线程使用的共享 store。用 `FZ_LOCK_ALLOC` 串行化这次自增即可避免。

### 4.2 主/工作线程分工

#### 4.2.1 概念说明

多线程要正确，最根本的是**划清「谁能在哪条线程上碰哪个对象」**。`multi-threaded.c` 采用了一种清晰到可以当作模板的分工：

- **主线程独占文档对象**：`fz_document`、`fz_page` 只能在主线程上访问。注释里写得很直白——「only one thread at a time can ever be accessing the document」。原因是文档/页面对象内部有可变状态（xref 缓存、懒加载标记等），且没有为并发访问加锁。
- **工作线程只碰 display list 和 pixmap**：display list 一旦录制完成就是「自包含、不可变」的指令流（u4-l2 讲过，节点自行持有下级资源），可以被任意线程安全地多次回放；pixmap 是一块纯像素内存，互不重叠的像素区域可以并行写入。
- **display list 是跨线程的唯一桥梁**：主线程把页面「翻译」成 display list，再把 list 连同一个新的空 pixmap 交给工作线程；工作线程用自己的 clone context 跑 `fz_run_display_list` 把像素填进 pixmap，再把 pixmap 交回主线程写 PNG。

所以这个模式本质是 **生产者/消费者 + 不可变消息**：主线程是生产者（生产 display list 任务），工作线程是消费者（消费 list 产出 pixmap），消息（list/pixmap）都是不可变或互斥写入的，从而避免了细粒度加锁。

> 一个常见误解：以为「渲染」本身慢、要并行，所以让多线程一起去解析同一个 `fz_page`。错。页面解析（解释内容流）必须在主线程串行做；只有「把已解析的 list 光栅化成像素」这一步可以并行。这也正是 mudraw 强制「多线程必须开 display list」的根因。

#### 4.2.2 核心流程

`multi-threaded.c` 的整体时序：

```
主线程                                      工作线程 i
──────                                      ─────────
fz_new_context(locks)                       （尚未创建）
fz_register_document_handlers
fz_open_document
threads = fz_count_pages          ┐
for i in 0..threads:              │ 每页一线程
  page = fz_load_page(i)          │
  bbox = fz_bound_page            │
  list = fz_new_display_list      │  ← 录制
  dev  = fz_new_list_device(list) │
  fz_run_page(page, dev)          │
  fz_close_device(dev)            │
  fz_drop_page(page)              │  ← 页面对象用完即弃
  data = {ctx, list, bbox, ...}   │
  pthread_create(renderer, data) ─┼────────────►  ctx = fz_clone_context(ctx)
                                  │                pix = fz_new_pixmap_with_bbox
                                  │                dev = fz_new_draw_device(pix)
                                  │                fz_run_display_list(list, dev)
                                  │                fz_close_device(dev); fz_drop_device(dev)
                                  │                fz_drop_context(ctx)   ← 释放克隆
                                  │                return data
for i: pthread_join ────────────────────────────── 接回 data
  fz_save_pixmap_as_png(data->pix)
  fz_drop_pixmap / fz_drop_display_list / free(data)
fz_drop_document
fz_drop_context
```

关键设计点：

1. **克隆发生在工作线程内部**（`renderer` 里第 102 行），而不是主线程里预先克隆。每个工作线程进入时克隆、退出时 drop，生命周期干净。
2. **`fz_var(dev)`**：`renderer` 在 `fz_try` 之外声明了 `dev`，又在块内赋值、`fz_always` 里读取，按 u2-l3 的规则必须 `fz_var(dev)` 防止跳转后丢值（见第 107 行）。
3. **错误用标志位回传**：工作线程的 `fz_catch` 不抛、不打印，只把 `data->failed = 1`，让主线程在 `pthread_join` 后统一处理。因为异常栈是每线程的，跨线程「抛」毫无意义。

#### 4.2.3 源码精读

线程间传递的消息结构 `thread_data`——注意它同时承载了输入（`ctx`、`list`、`bbox`）和输出（`pix`、`failed`）：

[docs/examples/multi-threaded.c:L53-L81](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/multi-threaded.c#L53-L81)

工作线程函数 `renderer` 的核心：先克隆 context（第 102 行），再创建 pixmap、draw device 并回放 display list，全程包在 `fz_try/fz_always/fz_catch` 里：

[docs/examples/multi-threaded.c:L87-L132](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/multi-threaded.c#L87-L132)

第 127 行 `fz_drop_context(ctx)` 释放的是**本线程克隆出来的** context（局部变量 `ctx` 已在第 102 行被覆盖为克隆），不影响主线程的 context。

主线程的录制循环——`fz_load_page` → `fz_bound_page` → `fz_new_display_list` → `fz_new_list_device` → `fz_run_page` → `fz_close_device`，然后在 `fz_always` 里丢弃 device 和 page（list 已经自包含，不再需要 page）：

[docs/examples/multi-threaded.c:L205-L266](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/multi-threaded.c#L205-L266)

特别注意第 218–220 行的注释：「this cannot be done on the worker threads, as only one thread at a time can ever be accessing the document」——这是分工的铁律。

主线程的汇合循环——`pthread_join` 收回 `data`，写 PNG，然后由**主线程**统一释放 pixmap、display list 和 data 结构：

[docs/examples/multi-threaded.c:L271-L299](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/multi-threaded.c#L271-L299)

作为工业版对比，看 `mudraw.c` 怎么做。它的 worker 结构 `worker_t` 同样持有一个 `fz_context *ctx`（克隆来的）和待渲染的 `list`/`pix`，但用信号量 `start`/`stop` 协调，且 worker 是**常驻**的（不像示例那样每页新建销毁）：

[source/tools/mudraw.c:L284-L301](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L284-L301)

`mudraw` 在需要时给每个 worker 调一次 `fz_clone_context`：

[source/tools/mudraw.c:L1129-L1136](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1129-L1136)

worker 线程主循环——`mu_wait_semaphore(start)` 等任务、跑 `drawband`、`mu_trigger_semaphore(stop)` 报完成，`band == -1` 时退出：

[source/tools/mudraw.c:L1782-L1810](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1782-L1810)

而 `mudraw` 在解析参数时把「多线程必须开 display list」写成了硬校验：

[source/tools/mudraw.c:L2294-L2313](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2294-L2313)

「cannot use multiple threads without using display list」——这正是 4.2.1 结论的工程化体现：没有 display list 这座不可变桥梁，多线程无从并行。

#### 4.2.4 代码实践

**实践目标**：把 `multi-threaded.c` 从「每页一线程」改造成「固定 4 线程的工作池」，渲染一个多页 PDF，并用 `clock_gettime` 计时对比单线程。

**操作步骤**：

1. 复制示例为 `mt-pool.c`。
2. 引入一个共享的「下一个待渲染页号」计数器，并用一把 pthread 互斥锁保护它（这把锁是你自己的应用锁，与 MuPDF 的 `FZ_LOCK_*` 无关）。

   ```c
   /* 示例代码：应用层的工作池协调状态 */
   static int next_page = 0;        /* 下一个待渲染页（从 0 开始） */
   static int total_pages = 0;
   static pthread_mutex_t queue_lock = PTHREAD_MUTEX_INITIALIZER;
   ```
3. 改写 `renderer`：进入后 `ctx = fz_clone_context(ctx);`，然后循环「从队列取页 → 录制 display list → 回放 → 写 PNG」，直到取不到页为止。注意：**录制 display list 这一步也要放进工作线程的串行区**（因为页面对象不能并发访问），所以用 `queue_lock` 把「load_page + 录制 list」整段保护起来；只有「回放 list → pixmap」这一步在锁外并行：

   ```c
   /* 示例代码：工作池版 renderer 骨架（简化，省略错误处理与 fz_var） */
   void *renderer(void *data_) {
       fz_context *master_ctx = ((struct thread_data *)data_)->ctx;
       fz_context *ctx = fz_clone_context(master_ctx);   /* 各自克隆 */
       int pageno;
       while (1) {
           /* —— 临界区：独占文档对象，串行解析 —— */
           pthread_mutex_lock(&queue_lock);
           pageno = next_page++;
           fz_page *page = NULL; fz_display_list *list = NULL;
           if (pageno < total_pages) {
               page = fz_load_page(ctx, doc, pageno);
               fz_rect bbox = fz_bound_page(ctx, page);
               list = fz_new_display_list(ctx, bbox);
               fz_device *d = fz_new_list_device(ctx, list);
               fz_run_page(ctx, page, d, fz_identity, NULL);
               fz_close_device(ctx, d); fz_drop_device(ctx, d);
               fz_drop_page(ctx, page);
               /* 注意：bbox 需通过结构体或全局传出锁外 */
           }
           pthread_mutex_unlock(&queue_lock);
           if (pageno >= total_pages) break;
           /* —— 锁外：并行光栅化（这是真正并行的部分）—— */
           fz_pixmap *pix = fz_new_pixmap_with_bbox_and_data(ctx, ...);
           fz_clear_pixmap_with_value(ctx, pix, 0xff);
           fz_device *dev = fz_new_draw_device(ctx, fz_identity, pix);
           fz_run_display_list(ctx, list, dev, fz_identity, bbox, NULL);
           fz_close_device(ctx, dev); fz_drop_device(ctx, dev);
           /* 写 PNG、drop pix、drop list ... */
       }
       fz_drop_context(ctx);
       return NULL;
   }
   ```
   （上面骨架标注了「示例代码」，省略了 `bbox` 的传出、错误处理与 `fz_var`，补全时请参照原示例的风格。）
4. `main` 中把「每页 pthread_create」改成「固定 `NWORKERS = 4` 次 pthread_create」，`threads = fz_count_pages` 仍用于填 `total_pages`。
5. 用 `clock_gettime(CLOCK_MONOTONIC, ...)` 在 `main` 的渲染段前后取时间差，算总耗时（毫秒）。
6. 分别用 `NWORKERS = 1` 和 `NWORKERS = 4` 编译运行同一个多页 PDF，对比耗时。

**需要观察的现象**：

- `NWORKERS = 4` 时总耗时显著低于 `NWORKERS = 1`（页面越多、CPU 核越多，加速越明显）。
- 4 个 worker 输出的 PNG 总数等于总页数，且每页内容正确（和单线程逐一比对）。

**预期结果**：在多核机器上，4 线程接近 2~4 倍加速（不会到 4 倍，因为有 `queue_lock` 串行的解析段、PNG 编码段、以及共享 store 的锁竞争）。若几乎无加速，多半是机器只有 1~2 个可用核，或文档页数太少。**待本地验证**。

> 提示：如果你只想最小改动验证「多线程更快」，也可以不改成工作池，而是直接用原示例（每页一线程）对比「把 renderer 体内的 `fz_run_display_list` 那段替换成空跑」的版本——但工作池版才是贴近真实工程（`mudraw`）的做法。

#### 4.2.5 小练习与答案

**练习 1**：原示例里，主线程在 `fz_run_page` 录制完 display list 后立刻 `fz_drop_page`（第 247 行），却把 list 交给工作线程继续用。为什么 list 在 page 被丢弃后仍然有效？

**答案**：因为 display list 在录制时（`fz_append_display_node`）会自行 keep 住它引用的所有下级资源（path/text/image 等），形成自包含的对象图。一旦录制完成，list 对这些资源持有引用，不再依赖原始 `fz_page`。所以丢弃 page 不影响 list 的回放——这正是 list 能安全跨线程传递的前提。

**练习 2**：`mudraw` 为什么拒绝「多线程 + 关闭 display list（`-D`）」的组合？

**答案**：没有 display list，就没有「已解析好的不可变指令流」可供多线程并行回放；此时若多线程渲染，只能并发地去解释同一个 `fz_page` 内容流，而页面对象不是线程安全的，必然出错。所以 mudraw 在参数校验阶段就用 `exit(1)` 拒绝了这种非法组合。

### 4.3 共享 store 设计

#### 4.3.1 概念说明

最后一个模块回答：克隆出来的 context 到底「共享了什么、没共享什么」，以及这带来的实战约束。

回顾 u9-l1：`fz_store` 缓存已解码的可复用对象（字体实例、解码后的图像 tile 等），跨文档、跨页面复用以避免重复解码。多线程下，如果每个工作线程各存一份 store，同一张图片会被解码 N 次，缓存的意义就没了。所以设计上让一族 clone **共享同一个 store 指针**。

但要共享，就必须解决两个问题：

1. **并发安全**：多个线程同时往同一个 store 插入/查找/驱逐。MuPDF 的解法是「一把大锁 `FZ_LOCK_ALLOC` 包住整个 store」——简单粗暴但有效，因为热点路径（缓存命中）可以做到锁内极短。u9-l1 已确认 store 的哈希表本身就是用 `FZ_LOCK_ALLOC` 创建的。
2. **生命周期**：store 不能在某一个工作线程退出时就被释放，必须等整个家族最后一个 context 退出。这就是 `master`/`context_count` 机制的作用——`fz_drop_context` 只把计数减一，归零才真释放。

与 store 同理被共享的还有 `font_context`（唯一的 `FT_Library`）、`glyph_cache`（字形位图缓存）、`colorspace_context` 等——它们都靠引用计数被一族 context 共用。

**关键反差**：异常栈 `error` 是「不共享」的。`fz_clone_context` 里专门调用 `fz_init_error_context(new_ctx)` 把新 context 的异常栈重置为初始状态。这造就了一个重要性质：**一个线程抛出的异常，只在该线程自己的 context 异常栈上流转，绝不会影响兄弟线程。** 这不仅是性能优化，更是正确性要求（`longjmp` 不能跨线程）。

由此推出一条实战铁律：**凡是「格式相关、带可变状态」的对象（`fz_document`、`fz_page`）绝不能跨线程共享；只有「自包含、不可变」的对象（`fz_display_list`、`fz_font` 经 keep 后、纯像素 `fz_pixmap`）才能安全跨线程传递。** 缓存（store）是个例外——它被设计成线程安全的，可以跨线程共享访问。

#### 4.3.2 核心流程

context 字段的三类划分（对照 [include/mupdf/fitz/context.h:L885-L928](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L885-L928)）：

```
struct fz_context
├─ 家族簿记      master, context_count, next_document_id   （master 上有效）
├─ 基础设施      alloc, locks            （按值拷贝 → 全家共用同一组分配器/锁）
├─ 非共享(独立)  error, warn, activity,  ← clone 时 error 被重置 → 独立异常栈
│                aa, seed48, icc_enabled  （每 context 各自的值）
└─ 共享(指针)    handler, archive, style, tuning,
                stddbg, font, hyph, colorspace,
                store, glyph_cache        ← clone 时只 keep(引用计数+1) → 全家共用同一对象
```

「克隆」对这三类的处理：

| 类别 | clone 时的处理 | 结果 |
| --- | --- | --- |
| 基础设施（alloc/locks） | memcpy 整块拷贝 | 指向同一组分配器/锁（共享） |
| 异常栈（error） | memcpy 后再 `fz_init_error_context` 重置 | **独立**（每线程一份） |
| 共享子上下文（store 等） | memcpy 后逐个 `fz_keep_*`（refs+1） | **共享**同一对象 |

锁保护共享 store 的证据——store 的哈希表在创建时就绑定了 `FZ_LOCK_ALLOC`：

\[ \text{store 内部不变量由 } FZ\_LOCK\_ALLOC \text{ 这一把锁统一守护} \]

这也意味着：所有对 store 的访问（无论来自哪个线程）都串行化在这把锁上。这是 MuPDF 用「单锁 + 偏序」换「无死锁 + 简单」的设计取舍。

#### 4.3.3 源码精读

`struct fz_context` 的字段布局，注释明确区分了 `/* unshared contexts */` 与 `/* shared contexts */`：

[include/mupdf/fitz/context.h:L885-L928](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L885-L928)

注意第 908 行的 `/* unshared contexts */` 之下是 `aa`、`seed48`、`icc_enabled`；第 921 行的 `/* shared contexts */` 之下是 `store`、`font`、`glyph_cache` 等。`error`（第 904 行）虽然在结构上和 `warn`、`activity` 排在一起按值存放，但 `fz_clone_context` 会专门重置它。

引用计数的「锁安全」实现——所有 `fz_keep_imp`/`fz_drop_imp` 都在 `FZ_LOCK_ALLOC` 下原子地改计数。以 keep 为例：

[include/mupdf/fitz/context.h:L965-L980](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L965-L980)

`fz_lock(ctx, FZ_LOCK_ALLOC)` → 改 `refs` → `fz_unlock`。这就是「一族 context 共享 store 时，store 的 `refs` 被多线程增减仍然正确」的底层保证。`fz_drop_imp_aux` 同理（见 [context.h:L1046-L1065](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L1046-L1065)）。

store 哈希表创建时绑定 `FZ_LOCK_ALLOC`——这是 store 内部能被多线程安全访问的根源：

[source/fitz/store.c:L65-L72](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L65-L72)

`fz_drop_context` 的家族计数与「最后退出者负责释放共享资源」逻辑：

[source/fitz/context.c:L194-L213](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L194-L213)

第 196–197 行把 `master->context_count--`，只有归零（第 197 行）才设置 `call_log`/`free_master`，之后才真正 drop store 等共享子上下文（第 219 行 `fz_drop_store_context`）。这就是「工作线程各自 `fz_drop_context` 不会提前释放共享 store」的机制。

`master` 在「自己还是根」与「已被销毁但留壳计数」两种状态间切换，见第 231–235 行的注释与逻辑：

[source/fitz/context.c:L228-L240](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L228-L240)

还有个细节：`next_document_id` 这个全局递增 ID 生成器（用于给文档对象分配唯一标识）也要锁保护，且它「只在 master 上有意义」——clone 出的 context 通过 `while (ctx->master && ctx->master != ctx) ctx = ctx->master;` 跳回根再读改写：

[source/fitz/context.c:L388-L399](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L388-L399)

这是「共享状态 + 单锁」模式的又一个实例。

#### 4.3.4 代码实践

**实践目标**：直观验证「一族 clone 共享同一个 store」，并观察共享缓存对多线程渲染的加速作用。

**操作步骤**：

1. 基于 4.2.4 的工作池版本，准备一个**同一张高清图片重复出现**的多页 PDF（比如同一张图贴在每一页）。
2. 编写两个对照版本：
   - 版本 A：正常多线程（4 worker，共享 store）。
   - 版本 B：在每个工作线程里，clone 之后**立即**调用 `fz_purge_stored_items(ctx)` 不太好（那会清掉全局缓存影响其他线程），所以改为对比「第 1 次渲染 vs 第 2 次渲染同一文档」的耗时：第 2 次因为 store 已缓存了图片解码结果，应明显更快。
3. 用 `clock_gettime` 分别测「第 1 次全程渲染」与「紧随其后的第 2 次全程渲染」（同一个 context 家族、不清 store）的耗时。
4. 可选：把 `fz_new_context` 的第三参数从 `FZ_STORE_UNLIMITED` 改成一个很小的值（如 `4 << 20`，4 MiB），再测第 2 次渲染——store 太小会频繁驱逐，第 2 次的加速会消失。

**需要观察的现象**：

- 第 2 次渲染显著快于第 1 次（图片解码被 store 缓存命中）。
- 把 store 上限调到很小时，第 2 次的加速优势消失（缓存被驱逐，重新解码）。

**预期结果**：证实 store 在一族 context 间共享且跨次复用；多线程下多个 worker 命中同一份缓存，避免重复解码。这与 u9-l1 的「store 跨文档复用」结论在多线程下依然成立。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：假设你在工作线程里捕获到一个 `fz_throw` 抛出的异常，能否用 `fz_rethrow` 把它「抛」给主线程处理？

**答案**：不能。异常靠 `longjmp` 跳转，只能回到**同一线程**内更早的 `setjmp`（即同线程 context 异常栈上的某一帧）。跨线程没有合法的 `setjmp` 落点，`longjmp` 跨线程是未定义行为。正确做法正如示例：工作线程在 `fz_catch` 里把失败信息写进共享的 `data->failed` 标志，由主线程在 `pthread_join` 后读取处理。

**练习 2**：为什么 MuPDF 用「一把 `FZ_LOCK_ALLOC` 大锁」保护整个 store，而不是给哈希表的每个桶各配一把锁来提高并发度？

**答案**：这是「简单 vs 性能」的取舍。单锁方案的好处是：① 配合「锁偏序」规则天然无死锁；② store 的不变量（如大小计数、LRU 链表、 scavenging 时的摘链延迟释放）跨越多个数据结构，用一把锁很容易保证整体一致；③ 热点路径（缓存命中查找）在锁内停留极短，实测竞争不严重。分桶锁虽能提高并发度，但会让 scavenging（可能从任意桶驱逐）的加锁顺序极难管理，容易死锁或破坏不变量。MuPDF 选择了简单可靠。

## 5. 综合实践

把三个模块串起来，完成一个「带性能对比报告的多线程渲染器」：

1. **基础**：以 `multi-threaded.c` 为蓝本，编译出可运行版本（`make examples` 后用 `./build/debug/multi-threaded doc.pdf`，参考 [docs/examples/multi-threaded.c:L16-L27](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/multi-threaded.c#L16-L27) 的构建说明）。
2. **改造为固定 4 线程工作池**（4.2.4 已给骨架）：主线程负责打开文档、数页；4 个常驻 worker 通过共享队列领页；页面对象的 load/录制在应用锁内串行，list 回放在锁外并行。
3. **加计时与对比**：分别测 `NWORKERS = 1/2/4` 下渲染同一多页 PDF 的总耗时，填入下表：

   | worker 数 | 总耗时(ms) | 加速比(vs 1 worker) | 生成的 PNG 数 | 是否与单线程逐页一致 |
   | --- | --- | --- | --- | --- |
   | 1 |  | 1.0× |  |  |
   | 2 |  |  |  |  |
   | 4 |  |  |  |  |

4. **验证正确性**：用 `cmp` 或肉眼对比 1-worker 与 4-worker 生成的同名 PNG，确认像素一致（display list 回放是确定性的，理应完全一致）。
5. **验证共享 store**：在同一个程序里连续渲染同一文档两遍，对比两次耗时，体会 store 缓存复用。
6. **写一份小结**：回答三个问题——① 你的机器几个核、加速比到顶了吗？② 加速的瓶颈在哪（队列锁串行段？PNG 编码？store 锁竞争？）③ 若把 `fz_new_context` 的 store 上限调小，多线程加速比如何变化？

这个任务覆盖了本讲全部三个最小模块：clone 与锁（步骤 2 必须正确克隆）、主/工作分工（步骤 2 的队列与并行划分）、共享 store（步骤 5）。

## 6. 本讲小结

- 多线程下必须为**每个工作线程** `fz_clone_context` 出独立 context；而要能 clone，创建 context 时就必须传入**真锁**（默认是空函数，clone 会被拒）。
- 应用通过 `fz_locks_context`（`user` + `lock`/`unlock` 回调）注入 `FZ_LOCK_MAX`=3 把互斥锁；MuPDF 内部遵循「持有锁 i 时只能申请锁 j（i<j）」的偏序规则，从结构上杜绝死锁。
- `fz_clone_context` = `memcpy` 整块拷贝 + 重置异常栈 + 对共享子上下文逐个 `fz_keep_*`。结果是：一族 clone **共享** store/font/glyph_cache（靠 `FZ_LOCK_ALLOC` 串行化），但**各自独立**维护异常栈。
- `multi-threaded.c` 的分工铁律：`fz_document`/`fz_page` 只能在主线程串行访问；只有自包含、不可变的 `fz_display_list`（和纯像素 `fz_pixmap`）能安全跨线程传递。display list 是跨线程的唯一桥梁，这也是 mudraw 强制「多线程必须开 display list」的根因。
- 异常是「每线程一份」的状态，绝不能跨线程 `fz_rethrow`；工作线程应把失败写进共享标志，由主线程在 `pthread_join` 后处理。
- 生产级多线程（mudraw）用「常驻 worker 池 + 信号量 + 分带」把示例的「每页一线程」升级为可控制并发度、可分带并行的工业版本，但核心的「clone context + 共享 store + display list 桥梁」三件套完全一致。

## 7. 下一步学习建议

- **继续本单元**：下一讲 **u9-l3 分带渲染与渐进输出** 会接着 mudraw 的 worker 池，讲清楚「为什么把一页切成水平 band 分别渲染能降低内存峰值」，以及 `band-writer` 如何把条带流式写进 PS/PWG/PCL 等输出格式。它与本讲的 worker 池是配套关系。
- **回溯原理**：若对「为什么 display list 不可变、能跨线程」还存疑，重读 u4-l2 的「录—存—放」三步与节点自持资源机制；对异常栈的独立性，回看 u2-l3 的 `fz_try/fz_catch` 基于 `setjmp/longjmp` 的实现。
- **延伸阅读**：
  - 官方多线程说明（示例文件头部注释里给出的链接）：`https://mupdf.readthedocs.io/en/latest/reference/c/overview.html#multi-threading`。
  - `source/tools/mudraw.c` 中 `bgprint_worker`（[L1812](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L1812) 起）展示了「页级流水线」——一边渲染当前页、一边后台打印上一页，是比 worker 池更进一步的并发模式。
- **动手方向**：尝试把本讲的工作池版本改成「双缓冲流水线」（主线程预录下一页的 display list，同时 worker 渲染当前页），体会 `bgprint` 式的重叠 I/O 与计算。
