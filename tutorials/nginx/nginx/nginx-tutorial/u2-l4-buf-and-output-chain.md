# 缓冲区 buf 与输出链 output_chain

## 1. 本讲目标

nginx 是一个数据搬运工：从磁盘读文件、从后端读响应、再写向客户端 socket。这条搬运线上流动的「货物」统一用一对结构表达——`ngx_buf_t`（一块缓冲）与 `ngx_chain_t`（把多块缓冲串成链）。本讲的目标是让你：

1. 读懂 `ngx_buf_t` 的字段，理解一块缓冲为何能同时表示「内存数据」「文件数据」「特殊控制信号」三种身份。
2. 掌握 `ngx_chain_t` 链式缓冲的组织方式，以及 `ngx_alloc_chain_link` / `ngx_chain_add_copy` 如何借助内存池的 `chain` 自由链复用链节点。
3. 理解 `ngx_output_chain` 这个通用「过滤式输出框架」的主循环：它如何用 `in` / `free` / `busy` 三条链驱动数据流转、如何在必要时把文件 buf 拷成内存 buf、以及如何用返回值表达背压。
4. 理解 `ngx_chain_writer` 作为写出口如何把链交给 connection 的 `send_chain`，以及 `NGX_AGAIN` 这个返回值如何一路向上传递「还没写完、请稍后再来」的信号。

学完后，你应能在后续 HTTP 过滤器链（u6-l6）、upstream 缓冲（u7-l5）里立刻认出这套结构，并解释「数据为什么这样流」。

## 2. 前置知识

本讲默认你已学过：

- **内存池 `ngx_pool_t`**（u2-l1）：`ngx_buf_t` 自身、链节点 `ngx_chain_t`、临时缓冲的数据区都来自 `ngx_palloc` / `ngx_pcalloc`；尤其重要的是内存池里有一个专属的 `chain` 自由链字段（[src/core/ngx_palloc.h:61](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.h#L61) 的 `ngx_chain_t *chain`），本讲会反复用到它来复用链节点。
- **容器与侵入式思想**（u2-l3）：`ngx_chain_t` 是一个非侵入式的简单单链表（节点持有 `buf` 指针），理解链表遍历即可。

三个概念提前点出，避免初学者卡壳：

- **零拷贝与不得不拷**：nginx 默认希望「文件数据直接用 `sendfile` 从内核发出去」、内存数据直接写 socket，**不做拷贝**。但某些下游过滤器要求「数据必须在内存里」（如 gzip 要压缩它），这时才把文件内容读进一块临时内存 buf——这就是 `ngx_output_chain` 里 `copy_buf` 存在的理由。
- **背压（backpressure）**：当客户端读得慢、socket 发送缓冲区写满时，nginx 不能无限地往后端/磁盘要数据，必须「停下来等」。这个「停下来」在代码里就是函数返回 `NGX_AGAIN`，并把没写完的数据留在 `ctx->in` / `ctx->busy` 链上，等下一次事件触发再来。
- **返回值语义**：nginx 通用返回值定义在 [src/core/ngx_core.h:39-44](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_core.h#L39-L44)：`NGX_OK=0`（成功完成）、`NGX_ERROR=-1`（出错）、`NGX_AGAIN=-2`（未完成，稍后再来）、`NGX_DONE=-4`（完成但语义特殊，如关闭连接）、`NGX_DECLINED=-5`（本模块不处理，交给下一个）。本讲最关心 `NGX_AGAIN`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/core/ngx_buf.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.h) | `ngx_buf_t` / `ngx_chain_t` / `ngx_output_chain_ctx_t` / `ngx_chain_writer_ctx_t` 结构定义，以及 `ngx_buf_in_memory`、`ngx_buf_size` 等关键宏 |
| [src/core/ngx_buf.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.c) | 临时 buf 创建、链节点分配/释放、链复制、链回收、已发送量推进等实现 |
| [src/core/ngx_output_chain.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_output_chain.c) | `ngx_output_chain` 主循环、`as_is` 判定、`copy_buf` 拷贝、`ngx_chain_writer` 写出口 |
| [src/http/ngx_http_copy_filter_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_copy_filter_module.c) | HTTP copy 过滤器，是 `ngx_output_chain` 在 HTTP 响应链路里最典型的真实调用方，用于理解上下文如何初始化 |

## 4. 核心概念与源码讲解

### 4.1 ngx_buf_t：一块缓冲的多种身份

#### 4.1.1 概念说明

`ngx_buf_t` 是 nginx 数据流的原子单位。它要同时服务三类截然不同的「货物」：

- **内存里的数据**：一段已读进进程地址空间的字节，由 `pos`/`last` 圈出有效区间。
- **文件里的数据**：尚未读进内存、仍躺在磁盘文件里的一段，由 `file_pos`/`file_last` 圈出区间，`file` 指向文件对象。
- **控制信号（special buf）**：没有真实数据，只携带一个语义标志，如「刷新缓冲 `flush`」「同步 `sync`」「这是最后一块 `last_buf`」。

用一个结构表达三种身份的好处是：下游过滤器只需写一套遍历 `ngx_chain_t` 的代码，遇到内存 buf 就写内存、遇到文件 buf 就 `sendfile`、遇到控制 buf 就只处理标志位，不必为每种货物单独设计数据通路。

#### 4.1.2 核心流程

理解 `ngx_buf_t` 的关键是区分「地址区间」与「性质标志」两组字段：

1. **区间字段**：
   - 内存有效数据 = `[pos, last)`；`start`/`end` 是这块缓冲的整个容量边界（`pos` 可回退到 `start` 以复用）。
   - 文件有效数据 = `[file_pos, file_last)`。
2. **性质标志（位域）**：
   - `temporary` / `memory` / `mmap`：三种「内存来源」，区别在于内容**是否可改**——`temporary` 可改（进程临时区），`memory`/`mmap` 不可改（只读缓存或 mmap 映射）。
   - `in_file`：这块 buf 的数据在文件里。
   - `recycled`：这块 buf 是从自由链复用的，用完应归还而非丢弃。
   - `flush` / `sync` / `last_buf` / `last_in_chain`：控制语义。
3. **辅助字段**：`tag` 标记「这块 buf 归哪个模块管」（回收时按 tag 区分归属）；`shadow` 指向「另一块语义上等价的 buf」（一个 buf 被拆/映射成多块时用来追踪原始来源）。

有效数据大小由宏 `ngx_buf_size` 给出：

\[
\text{size}(b) = \begin{cases} \text{last} - \text{pos}, & \text{内存 buf} \\ \text{file\_last} - \text{file\_pos}, & \text{文件 buf} \end{cases}
\]

#### 4.1.3 源码精读

结构体本身——注意区间字段在前、标志位域在后：

[src/core/ngx_buf.h:20-56](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.h#L20-L56) — `struct ngx_buf_s`：`pos/last` 圈内存有效区间，`file_pos/file_last` 圈文件有效区间；`start/end` 是整块容量边界；`tag` 标记归属模块；`file` 指向文件对象；`shadow` 指向等价 buf；位域 `temporary/memory/mmap/recycled/in_file/flush/sync/last_buf/last_in_chain/last_shadow/temp_file` 描述性质与控制语义。注释明确：`temporary` 表示「内容可改」，`memory` 表示「内容在只读缓存里、不可改」，`mmap` 表示「内容是 mmap 映射、不可改」。

判断「是否内存 buf」「是否纯控制信号」「有效大小」的几个宏，是后续所有过滤器的共用词汇：

[src/core/ngx_buf.h:125-138](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.h#L125-L138) — `ngx_buf_in_memory(b)` = `temporary || memory || mmap`（任一为真即有内存数据）；`ngx_buf_special(b)` = `(flush||last_buf||sync) && 无内存 && 无文件`（纯控制信号，无真实字节）；`ngx_buf_size(b)` 按「有内存取 `last-pos`，否则取 `file_last-file_pos`」计算有效大小。注意 `ngx_buf_size` 对纯控制 buf 返回 0——这正是主循环里「零大小且非 special 要告警」判断的依据。

创建一块临时内存 buf 的标准入口：

[src/core/ngx_buf.c:12-44](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.c#L12-L44) — `ngx_create_temp_buf(pool, size)`：先 `ngx_calloc_buf` 把结构体清零（所有标志、`file`/`shadow`/`tag` 都为 0），再 `ngx_palloc(pool, size)` 申请数据区，令 `pos = last = start`、`end = start + size`，并置 `temporary = 1`（内容可改）。注释列举了被 `calloc` 清零的字段，说明初始时它既不是文件 buf 也没有任何控制标志，是一块「空白可写」的内存缓冲。

#### 4.1.4 代码实践

**实践目标**：用 `ngx_create_temp_buf` 建一块内存 buf，写入若干字节，手工验证 `ngx_buf_size` 与 `ngx_buf_in_memory` 的取值。

**操作步骤**：

1. 阅读下面「示例代码」（非 nginx 原有代码），理解 buf 字段被写入后的状态：

```c
/* 示例代码：构造并填充一块内存 buf（非项目原有代码） */
ngx_buf_t *b = ngx_create_temp_buf(pool, 128);   /* end = start + 128 */
if (b == NULL) { return NGX_ERROR; }

/* 写入 5 字节 "hello"：只动 last，不动 pos */
u_char *p = ngx_cpymem(b->last, "hello", 5);
b->last = p;                                      /* now [pos, last) = "hello" */

ngx_log_error(NGX_LOG_INFO, log, 0,
              "in_memory=%d size=%O",
              ngx_buf_in_memory(b), ngx_buf_size(b));
/* 期望：in_memory=1 size=5 */
```

2. 对照真实创建：HTTP 静态文件模块在拼装响应头时也用 `ngx_create_temp_buf`，可在 [src/http/modules/ngx_http_static_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c) 中搜索 `ngx_create_temp_buf` 或 `ngx_alloc_buf` 定位现场，看真实代码如何设置 `last_buf` 等标志。

**需要观察的现象**：`pos` 指向已写区间起点、`last` 指向终点，二者之差即有效大小；`ngx_buf_in_memory` 因 `temporary=1` 而为真；未设 `in_file`/`flush`/`last_buf`，故 `ngx_buf_special` 为假。

**预期结果**：日志打印 `in_memory=1 size=5`。本例依赖 nginx 的池与日志环境，**运行结果待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`temporary`、`memory`、`mmap` 三个标志都表示「数据在内存里」，为什么还要分三个？

**答案**：区别在「内容是否可改」。`temporary` 是进程临时缓冲，可改写（如 copy_filter 拷出来的临时 buf）；`memory` 指向只读内存缓存（如打开文件缓存的映射区），不可改；`mmap` 是 `mmap()` 映射区，同样不可改。下游过滤器据此决定能否原地修改——例如 `need_in_temp` 要求必须是可改的临时区，`ngx_output_chain_as_is` 里就会拒绝 `memory`/`mmap` 的 buf（见 4.3.3）。

**练习 2**：一个 `ngx_buf_t` 能否同时既有内存数据又有文件数据？

**答案**：可以。结构里 `pos/last` 与 `file_pos/file_last` 是**并存**的两组区间，`in_file` 与 `temporary/memory/mmap` 也可同时置位。`ngx_output_chain_copy_buf` 在拷贝「内存 + 文件」混合 buf 时就显式处理 `if (src->in_file)` 分支：把内存部分 `memcpy` 后，再决定文件部分是走 `sendfile`（`dst->in_file=1`）还是丢弃文件属性（`dst->in_file=0`）。`ngx_buf_size` 在这种混合情况下只取内存大小（见宏定义优先 `ngx_buf_in_memory`）。

---

### 4.2 ngx_chain_t 链式组织：alloc_chain_link 与 chain_add_copy

#### 4.2.1 概念说明

单个 `ngx_buf_t` 表达一块缓冲，但一次响应往往由「头部 + 多段 body + 结尾标记」组成，于是需要把多块 buf 串起来——这就是 `ngx_chain_t`。它是一个极简的单链表节点：一个 `buf` 指针加一个 `next` 指针，仅此而已。

```
ngx_chain_t   ngx_chain_t   ngx_chain_t
+--------+    +--------+    +--------+
| buf    |    | buf    |    | buf    |
| next --|--->| next --|--->| next   |--> NULL
+--------+    +--------+    +--------+
```

链节点本身也是频繁分配/释放的小对象（一次响应要建很多个）。nginx 为它专门设了一条**池内自由链**：释放时不归还操作系统，而是挂到 `pool->chain` 上；下次分配先从这条自由链取，命中就零 `malloc`。这是 nginx 在高并发下控制分配次数的典型手法。

#### 4.2.2 核心流程

1. **分配一个链节点** `ngx_alloc_chain_link(pool)`：先看 `pool->chain` 是否有空闲节点，有就摘下来复用；否则 `ngx_palloc` 新建一个。
2. **释放一个链节点** `ngx_free_chain(pool, cl)`（宏）：把节点头插到 `pool->chain`，等下次复用。注意它**只回收链节点结构本身**，不碰 `cl->buf` 指向的缓冲数据。
3. **复制一条链** `ngx_chain_add_copy(pool, &chain, in)`：遍历 `in`，为每个 buf 新分配一个链节点挂到 `chain` 尾部——`buf` 指针是**共享**的（浅拷贝），不复制缓冲数据本身。
4. **批量回收** `ngx_chain_update_chains`：把已发完的 busy 节点按 `tag` 决定是归还自由链还是直接丢弃，并重置 buf 的 `pos/last` 以备复用（详见 4.3）。

#### 4.2.3 源码精读

链表节点结构，极简：

[src/core/ngx_buf.h:59-62](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.h#L59-L62) — `struct ngx_chain_s` 只有 `buf` 与 `next` 两个指针。`ngx_chain_t` 本身早在 [src/core/ngx_core.h:19](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_core.h#L19) 就做了前向声明，所以全核心代码都能用这个类型。

分配链节点，优先命中池内自由链：

[src/core/ngx_buf.c:47-65](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.c#L47-L65) — `ngx_alloc_chain_link`：若 `pool->chain` 非空，摘下首节点返回（零 malloc）；否则才 `ngx_palloc` 新建。这是「池顶 bump 分配」之外的第二层复用——bump 用于数据，自由链用于链节点这类高频小对象。

释放即头插自由链（宏，无函数调用开销）：

[src/core/ngx_buf.h:147-150](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.h#L147-L150) — `ngx_free_chain(pool, cl)`：`cl->next = pool->chain; pool->chain = cl`。两条赋值把节点头插回自由链。注意它不清 `cl->buf`，调用方需自行保证不再误用。

复制链——遍历 `in` 逐个挂尾，buf 指针共享：

[src/core/ngx_buf.c:126-153](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.c#L126-L153) — `ngx_chain_add_copy`：先沿 `*chain` 走到尾（用二级指针 `ll` 记录「下一个该写哪里」），再遍历 `in`，为每个 buf 用 `ngx_alloc_chain_link` 新建节点、`cl->buf = in->buf`（浅拷贝共享 buf）、挂到尾。`ngx_output_chain` 主循环用它把外来 `in` 链并入 `ctx->in`。

`ngx_output_chain.c` 内部还有一个等价的 `ngx_output_chain_add_copy`（注意是 static，名字相同但带 `NGX_SENDFILE_LIMIT` 分支，见 [src/core/ngx_output_chain.c:309-375](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_output_chain.c#L309-L375)），它在复制时会把跨 `NGX_SENDFILE_LIMIT` 边界的文件 buf 拆成两块——这是少数平台才编入的细节，理解主流程时可忽略。

#### 4.2.4 代码实践

**实践目标**：构造一条含两个内存 buf 的 chain，体会「链节点共享 buf、逐块遍历」的模型。

**操作步骤**：

1. 阅读下面「示例代码」（非 nginx 原有代码），这是本讲综合实践的基础骨架：

```c
/* 示例代码：构造两个内存 buf 的 chain（非项目原有代码） */
ngx_chain_t  *cl1, *cl2;
ngx_buf_t    *b1, *b2;

b1 = ngx_create_temp_buf(pool, 32);
b2 = ngx_create_temp_buf(pool, 32);
if (b1 == NULL || b2 == NULL) { return NGX_ERROR; }

b1->last = ngx_cpymem(b1->pos, "hello ", 6);   /* [pos,last) = "hello " */
b2->last = ngx_cpymem(b2->pos, "world", 5);     /* [pos,last) = "world" */
b2->last_buf = 1;                               /* 标记最后一块 */

cl1 = ngx_alloc_chain_link(pool);
cl2 = ngx_alloc_chain_link(pool);
cl1->buf = b1;  cl1->next = cl2;
cl2->buf = b2;  cl2->next = NULL;

/* 遍历：累加待写大小 */
off_t total = 0;
ngx_chain_t *p;
for (p = cl1; p; p = p->next) {
    total += ngx_buf_size(p->buf);
    ngx_log_error(NGX_LOG_INFO, log, 0,
                  "buf size=%O last_buf=%d",
                  ngx_buf_size(p->buf), p->buf->last_buf);
}
/* 期望：两行 size=6 / size=5，total=11 */
```

2. 阅读真实使用：`ngx_create_chain_of_bufs` 一次性建「num 块定长 buf」的链，是上面手写过程的批量化版本。

[src/core/ngx_buf.c:68-123](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.c#L68-L123) — `ngx_create_chain_of_bufs`：一次 `ngx_palloc` 申请 `num * size` 的连续数据区，循环里给每块切出 `[start, end)`、建 `ngx_buf_t`、用 `ngx_alloc_chain_link` 串成链。HTTP copy 过滤器的 `conf->bufs`（num+size）最终就经它变成实际缓冲。

**需要观察的现象**：遍历时 `p = p->next` 推进链表，每块 `ngx_buf_size` 只取该块 `[pos,last)` 的长度；`last_buf` 只在最后一块为真，是通知下游「响应到此结束」的控制信号。

**预期结果**：日志依次打印 `size=6 last_buf=0`、`size=5 last_buf=1`，`total=11`。本例依赖 nginx 编译环境，**运行结果待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`ngx_free_chain` 只回收链节点，不回收 `cl->buf`。那么 buf 的数据区什么时候被回收？

**答案**：buf 的结构体与数据区都分配在内存池上，随请求池整体销毁而回收（u2-l1 已讲「小块不支持单独 free」）。`ngx_free_chain` 复用的只是「链节点」这个 16 字节的小壳子，目的是避免为每个 chain 节点反复 `malloc`。若某个 buf 是 `recycled` 的，则它本身也会被 `ngx_chain_update_chains` 重置 `pos/last` 后放回 `free` 链供同模块复用（见 4.3）。

**练习 2**：`ngx_chain_add_copy` 为什么是浅拷贝（共享 buf 指针）而不是深拷贝？

**答案**：nginx 追求零拷贝。同一块响应数据可能要同时流经多个过滤器（如 copy_filter → gzip_filter → write_filter），若每次都深拷贝就丧失了 sendfile 与就地处理的性能优势。共享 buf 后，各过滤器只读 `pos/last` 推进游标或设置标志，不复制数据；需要修改时才由 copy_filter 显式建一个 `temporary` 临时 buf 拷一份（即 4.3 的 `copy_buf`）。

---

### 4.3 ngx_output_chain 主循环：in/free/busy 三链表与 copy

#### 4.3.1 概念说明

`ngx_output_chain` 是一个**通用过滤式输出框架**。它不直接写 socket，而是把上游传来的 `in` 链「规整」成下游能接受的形式（必要时把文件 buf 读进内存），再交给一个可插拔的 `output_filter`（默认是 `ngx_chain_writer`，最终写 socket）。

它维护三条链表来表达数据流的不同状态：

| 链表 | 含义 |
| --- | --- |
| `ctx->in` | **待处理**的输入 buf：已收到但还没被规整/拷贝完 |
| `ctx->busy` | **已交给下游但尚未发完**的 buf：下游 `send_chain` 没消费掉、留在那儿等下次 |
| `ctx->free` | **可复用的空 buf**：下游已发完、`pos/last` 已重置，下次 copy 时优先取它 |

核心矛盾它要解决的是：**上游给的 buf 形态可能不符合下游要求**。例如下游开了 `need_in_memory`（要压缩），但上游给的是文件 buf——这时 `ngx_output_chain` 就得用一块临时内存 buf 把文件内容读进来，再把这块临时 buf 交给下游。这个「读文件进内存」就是 `copy_buf`。

#### 4.3.2 核心流程

`ngx_output_chain` 主循环（伪代码）：

```
ngx_output_chain(ctx, in):
    if ctx->in==NULL and ctx->busy==NULL:        # 快路径
        if in==NULL or (单块且 as_is):           # as_is = 无需拷贝
            return output_filter(in)             # 直接交下游，零拷贝

    把 in 追加到 ctx->in 尾部                     # ngx_output_chain_add_copy

    for(;;):
        while ctx->in:                            # 逐块规整输入
            b = ctx->in->buf
            if size(b)==0 and not special(b):     # 零大小且非控制 → 告警并丢弃
                告警; 从 in 摘除; continue
            if as_is(ctx, b):                     # 无需拷贝 → 直接搬到 out
                把 ctx->in 首节点移到 out 尾; continue
            # 需要拷贝：准备一块目标 buf
            if ctx->buf==NULL:
                优先从 ctx->free 取空 buf；
                否则若已分配到上限(ctx->bufs.num) 或 out 非空 → break（先发出去）
                否则 ngx_output_chain_get_buf 新建一块
            rc = copy_buf(ctx)                    # 内存 memcpy / 文件 read
            if rc==AGAIN: 若 out 非空 break 否则 return AGAIN
            把拷好的 ctx->buf 包成链节点挂到 out 尾

        if out==NULL:                             # 本轮没产出可发的
            if ctx->in: return AGAIN              # 还有输入没处理 → 稍后再来
            return last                           # 否则返回上轮 filter 的结果

        last = output_filter(out)                 # 交给下游写
        if last==ERROR or DONE: return last
        update_chains(free, busy, out, tag)       # 回收：发完的进 free，没发完的留 busy
```

关键返回值流转：

- `NGX_AGAIN`（-2）：表示「还有数据没发完，请稍后再来」——这是背压信号。要么是 `ctx->in` 还有待拷输入（拷贝被限流），要么是下游 filter 返回 `NGX_AGAIN`（socket 写满）。
- `NGX_OK`（0）：全部发完。
- 内部常量 `NGX_NONE`（1）：表示「上一轮 filter 没有给出明确的 OK/AGAIN」，本轮若无新产出就把它当作返回值透传。
- `NGX_ERROR`（-1）：出错。

> 说明：任务描述里提到「NEED_IN_MORE 等返回值」，nginx 源码里**没有**字面名为 `NEED_IN_MORE` 的常量；与之等价的「还需要更多输入/稍后再来」信号就是 `NGX_AGAIN`。本讲后续一律使用真实常量名。

#### 4.3.3 源码精读

上下文结构 `ngx_output_chain_ctx_t` 把三链表、拷贝参数、下游 filter 都打包在一起：

[src/core/ngx_buf.h:78-110](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.h#L78-L110) — `ctx->in/free/busy` 三链表；`sendfile/directio/need_in_memory/need_in_temp` 控制拷贝策略；`bufs`（num+size）限制临时 buf 数量与单块大小；`tag` 标记本 ctx 的 buf 归属；`output_filter` + `filter_ctx` 是下游可插拔出口。`allocated` 记录本轮已新建的临时 buf 数，与 `bufs.num` 比较来限流。

「无需拷贝」的快路径——大多数请求走这里，零拷贝直通：

[src/core/ngx_output_chain.c:48-72](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_output_chain.c#L48-L72) — 当 `ctx->in` 与 `ctx->busy` 都空、且新来的是单块「as_is」buf 时，直接 `return ctx->output_filter(ctx->filter_ctx, in)`，不进主循环、不分配。注释称之为 "the short path"。静态文件用 sendfile 直发就命中这条路径。

「是否需要拷贝」的判定 `as_is`——核心策略点：

[src/core/ngx_output_chain.c:249-306](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_output_chain.c#L249-L306) — `ngx_output_chain_as_is`：special buf 直接 as_is；若不允许 sendfile 且 buf 不在内存 → 必须拷（返回 0）；若 `need_in_memory` 但 buf 不在内存 → 必须拷；若 `need_in_temp` 但 buf 是 `memory`/`mmap`（只读不可改）→ 必须拷到临时区。其余情况 as_is。这正是「下游要内存数据时强制把文件读进来」的判定。

主循环里「需要拷贝时准备目标 buf」的限流逻辑：

[src/core/ngx_output_chain.c:162-190](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_output_chain.c#L162-L190) — 当 `ctx->buf==NULL` 时：先尝试 `align_file_buf`（directio 对齐），不成功则优先从 `ctx->free` 复用空 buf；若 `free` 也空且「已分配数 == `bufs.num`」或已有 `out` 产出 → `break`（先把 out 发出去，腾出空间再来）。这保证一次循环不会无限新建临时 buf，把内存用量约束在 `bufs.num * bufs.size` 内。

真正干拷贝活的 `copy_buf`——分内存与文件两路：

[src/core/ngx_output_chain.c:504-535](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_output_chain.c#L504-L535) — `ngx_output_chain_copy_buf` 内存分支：`ngx_memcpy(dst->pos, src->pos, size)` 把源 buf 内存拷进临时 buf，推进 `src->pos` 与 `dst->last`；若源同时 `in_file` 且允许 sendfile，则把文件区间「转嫁」给 `dst`（`dst->in_file=1; dst->file=src->file; ...`），让下游用 sendfile 发这段文件。若 `src->pos==src->last`（这块内存发完）才把 `flush/last_buf/last_in_chain` 等控制标志继承给 `dst`——避免在分块拷贝时过早传播「最后一块」语义。

[src/core/ngx_output_chain.c:562-662](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_output_chain.c#L562-L662) — `copy_buf` 文件分支：源不在内存时，用 `ngx_read_file`（或 file AIO / 线程读）把文件内容读进 `dst->pos`，读到 `n` 字节后推进 `dst->last`，再按 sendfile 与否决定是否保留 `in_file`。读到 `src->file_pos==src->file_last` 时才继承控制标志。这一路就是把磁盘文件搬进内存 buf 的实质代码。

交下游 + 回收三链表：

[src/core/ngx_output_chain.c:227-244](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_output_chain.c#L227-L244) — 内层 `while(ctx->in)` 结束后：若 `out==NULL` 且 `last==NGX_NONE`，则 `ctx->in` 非空时返回 `NGX_AGAIN`（背压），否则返回 `last`。否则调用 `ctx->output_filter(filter_ctx, out)` 拿到下游返回值存入 `last`，再 `ngx_chain_update_chains` 把发完的节点回收到 `free`、没发完的留在 `busy`，循环继续。

回收逻辑——按 `tag` 区分归属，按 `ngx_buf_size` 判是否发完：

[src/core/ngx_buf.c:184-223](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.c#L184-L223) — `ngx_chain_update_chains`：先把 `*out` 挂到 `busy` 尾；再从 `busy` 头扫描——若节点 `tag != 本 ctx 的 tag`（是别人家的 buf）直接 `free_chain` 丢弃；若 `ngx_buf_size != 0`（还没发完）就 `break` 留在 busy；否则（发完了）重置 `pos=last=start`、把节点移到 `free` 供复用。`tag` 在这里起到「不同模块的 buf 不互相污染自由链」的作用。

#### 4.3.4 代码实践

**实践目标**：用 `ngx_output_chain` 驱动 4.2 节那条两块 buf 的 chain，接一个「只打印待写总长度」的自定义 filter，观察 `NGX_AGAIN` / `NGX_OK` 与 `ctx->busy` 的流转。这就是任务描述里「在 filter 中只打印待写长度，观察返回值流转」的真实落地。

**操作步骤**：

1. 阅读下面「示例代码」（非 nginx 原有代码）。它实现一个最小 filter：累加 `in` 链上所有 `ngx_buf_size`，打印后返回 `NGX_OK`（模拟「下游全收下」）：

```c
/* 示例代码：自定义 output_filter（非项目原有代码） */
static ngx_int_t
my_print_filter(void *ctx, ngx_chain_t *in)
{
    off_t total = 0;
    for (; in; in = in->next) {
        total += ngx_buf_size(in->buf);
    }
    ngx_log_error(NGX_LOG_INFO, ((ngx_pool_t*)ctx)->log, 0,
                  "filter: about to write %O bytes", total);
    return NGX_OK;   /* 模拟下游 socket 一次性收完 */
}
```

2. 用它装配 `ngx_output_chain_ctx_t` 并调用主函数（示例代码，非项目原有代码）：

```c
/* 示例代码：装配 ctx 并驱动（非项目原有代码） */
ngx_output_chain_ctx_t  octx;
ngx_memzero(&octx, sizeof(octx));

octx.pool = pool;
octx.bufs.num = 2;                 /* 至多 2 块临时 buf */
octx.bufs.size = 32;
octx.tag = (ngx_buf_tag_t) &my_module;
octx.sendfile = 1;
octx.need_in_memory = 0;           /* 不强制读进内存 → 命中 as_is 快路径 */
octx.output_filter = my_print_filter;
octx.filter_ctx = pool;            /* 这里借 pool 当 ctx 仅为拿 log */

ngx_int_t rc = ngx_output_chain(&octx, cl1);   /* cl1 是 4.2 节的两块链 */
/* 期望：filter 打印 "about to write 11 bytes"，rc == NGX_OK，octx.in==NULL */
```

3. **观察背压**：把 `my_print_filter` 的返回值改成 `NGX_AGAIN`（模拟 socket 写满），再次调用。此时 `ngx_output_chain` 会在 [src/core/ngx_output_chain.c:236](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_output_chain.c#L236) 拿到 `NGX_AGAIN` 存入 `last`，经 `ngx_chain_update_chains` 把 out 移入 `ctx->busy`，循环回到 `while(ctx->in)` 时 `ctx->in` 已空、`out` 在下一轮重新生成，最终从 [src/core/ngx_output_chain.c:227-234](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_output_chain.c#L227-L234) 返回 `NGX_AGAIN`。再次以 `in=NULL` 调用 `ngx_output_chain(&octx, NULL)`，`ctx->busy` 里残留的 buf 会被重新交给 filter。

**需要观察的现象**：
- `need_in_memory=0` 且都是内存 buf → 命中 `as_is`，不发生 `memcpy`，filter 直接收到原链。
- filter 返回 `NGX_OK` → `rc==NGX_OK`，`octx.in==NULL`、`octx.busy==NULL`（全发完回收）。
- filter 返回 `NGX_AGAIN` → `rc==NGX_AGAIN`，数据滞留在 `octx.busy`，需再次调用才能排空。

**预期结果**：日志打印 `filter: about to write 11 bytes`；`NGX_OK` 分支下 `octx.in` 与 `octx.busy` 均为 NULL，`NGX_AGAIN` 分支下 `octx.busy` 非空。本例依赖 nginx 编译与链接环境，**运行结果待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么主循环里 `ngx_buf_size==0 && !ngx_buf_special` 要告警并跳过？

**答案**：零大小的 buf 如果不是控制信号（flush/last_buf/sync），就是「无意义空 buf」——它既没有数据也没有语义，留在链里只会让下游空转、干扰 `last_buf` 判定。这通常是上游 bug（如重复设置 pos=last）。主循环在 [src/core/ngx_output_chain.c:103-126](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_output_chain.c#L103-L126) 用 `NGX_LOG_ALERT` 告警并 `ngx_debug_point()` 暂停（debug 构建下），然后摘除该节点继续，避免它卡死整条链。注意纯控制 buf（`ngx_buf_special` 为真）虽然也是零大小，但携带语义，必须放行。

**练习 2**：`ctx->allocated == ctx->bufs.num` 时 `break` 出内层循环，有什么效果？

**答案**：这是临时 buf 的**配额限流**。`bufs.num` 限制一次循环内最多新建多少块临时 buf（HTTP 里由 `client_body_buffer_size`/`proxy_buffer_size` 类配置间接决定）。达到上限就 `break`，先把已拷好的 `out` 发给下游、等下游发完回收进 `free`，下一轮循环再从 `free` 复用，从而把「正在拷贝的临时内存」总量封顶在 `bufs.num * bufs.size`。这防止慢客户端场景下 nginx 把整份大响应一次性读进内存。

---

### 4.4 ngx_chain_writer：真正的写出口与背压信号

#### 4.4.1 概念说明

`ngx_output_chain` 把数据规整成 `out` 链后，要交给 `ctx->output_filter` 真正发出去。nginx 自带的默认实现就是 `ngx_chain_writer`：它把 `out` 追加到自己维护的 `ctx->out` 链上，再调用连接对象的 `c->send_chain(c, ctx->out, limit)`——后者最终落到 `writev` / `sendfile`（u4-l4 会讲 OS 抽象层）。

`ngx_chain_writer` 的核心职责不是「写」，而是**记账与背压**：

- 它维护 `ctx->out`（待发）与 `ctx->last`（链尾指针，O(1) 追加）。
- `c->send_chain` 返回**没发完的剩余链**（`chain`）：若 `chain != NULL`，说明 socket 缓冲区满了，剩下这些没发出去。
- 此时 `ngx_chain_writer` 返回 `NGX_AGAIN`，把剩余链留在 `ctx->out`，等下次写事件就绪再来。

#### 4.4.2 核心流程

```
ngx_chain_writer(ctx, in):
    for each buf in in:
        校验非零大小；累加 size
        新建链节点挂到 ctx->out 尾（buf 共享）
    for each buf in ctx->out:
        累加剩余待发 size
    if size==0 and not c->buffered: return NGX_OK    # 没东西要发

    chain = c->send_chain(c, ctx->out, limit)        # 真正写 socket
    if chain == NGX_CHAIN_ERROR: return NGX_ERROR

    if chain && c->write->ready:
        ngx_post_event(c->write, posted_next_events) # 把写事件挂到下一轮优先处理

    释放已发完的链节点；ctx->out = chain（剩余）
    if ctx->out == NULL and not c->buffered:
        return NGX_OK                                # 全发完
    return NGX_AGAIN                                 # 还有剩余 → 背压
```

#### 4.4.3 源码精读

写出口结构，维护待发链与尾指针：

[src/core/ngx_buf.h:113-119](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.h#L113-L119) — `ngx_chain_writer_ctx_t`：`out` 是待发链头、`last` 是二级指针指向链尾（用 `*last = cl; last = &cl->next` 实现 O(1) 追加）、`connection` 是目标连接、`limit` 限制单次发送量。

把外来 `in` 追加到 `ctx->out`，并做零/负大小校验：

[src/core/ngx_output_chain.c:679-736](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_output_chain.c#L679-L736) — 遍历 `in`，对每个 buf 先用与主循环相同的逻辑校验「零大小且非 special」告警、「负大小」报错；合法的累加 `size` 并 `ngx_alloc_chain_link` 挂到 `*ctx->last`。注意它**共享 buf 指针**（`cl->buf = in->buf`），不复制数据。

真正写 socket + 处理剩余链：

[src/core/ngx_output_chain.c:786-819](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_output_chain.c#L786-L819) — 若 `size==0 && !c->buffered` 直接 `return NGX_OK`（没东西要发，如只剩控制 buf 已被前面处理）。否则 `chain = c->send_chain(c, ctx->out, limit)`：`chain` 为 NULL 表示全发完，非 NULL 表示剩余未发。把已发完的节点 `ngx_free_chain` 释放、`ctx->out = chain` 留下剩余；若 `ctx->out==NULL && !c->buffered` 返回 `NGX_OK`，否则返回 `NGX_AGAIN`。`chain && c->write->ready` 时还 `ngx_post_event` 把写事件提前到下一轮，加快排空。

真实装配现场——HTTP copy 过滤器初始化 `ngx_output_chain_ctx_t`：

[src/http/ngx_http_copy_filter_module.c:99-122](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_copy_filter_module.c#L99-L122) — copy 过滤器在请求池上 `ngx_pcalloc` 一个 `ngx_output_chain_ctx_t`，设置 `sendfile = c->sendfile`、`need_in_memory = r->main_filter_need_in_memory || r->filter_need_in_memory`、`need_in_temp`、`alignment`、`pool = r->pool`、`bufs = conf->bufs`、`tag = &ngx_http_copy_filter_module`，并把 `output_filter` 指向 `ngx_http_next_body_filter`（即下一个 body 过滤器，链尾最终是 write_filter，后者内部用的就是 `ngx_chain_writer`）。`tag` 取模块地址，正是给 `ngx_chain_update_chains` 识别「这块 buf 是 copy_filter 拷出来的」用的。

调用点与背压标志：

[src/http/ngx_http_copy_filter_module.c:145-152](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_copy_filter_module.c#L145-L152) — `rc = ngx_output_chain(ctx, in)` 之后，用 `ctx->in == NULL` 切换 `r->buffered` 的 `NGX_HTTP_COPY_BUFFERED` 位：`ctx->in` 非空说明还有数据没规整完，置位 `buffered`；为空则清位。这个 `buffered` 标志一路汇总到请求对象，决定 worker 是否要把这个连接挂到定时器等下次写事件——这就是背压如何「向上游传播、最终让 nginx 停止从后端/磁盘要数据」的完整闭环。

#### 4.4.4 代码实践

**实践目标**：跟踪一次真实 HTTP 响应，确认 `ngx_output_chain` → `ngx_chain_writer` → `c->send_chain` 的调用关系与背压返回值。

**操作步骤**：

1. 在 [src/http/ngx_http_copy_filter_module.c:145](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_copy_filter_module.c#L145) 的 `ngx_output_chain(ctx, in)` 处设断点或加 `ngx_log_error`，确认它把 body 过滤器链的 `in` 交给 `ngx_output_chain`。
2. 沿 `ctx->output_filter`（[src/http/ngx_http_copy_filter_module.c:120-121](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_copy_filter_module.c#L120-L121) 设为 `ngx_http_next_body_filter`）追到链尾的 write 过滤器，确认它内部调用 `ngx_chain_writer`。
3. 在 [src/core/ngx_output_chain.c:790](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_output_chain.c#L790) 的 `c->send_chain(c, ctx->out, ctx->limit)` 处观察返回值 `chain`：客户端读得快时 `chain==NULL`（全发完），慢时 `chain` 指向剩余链。

**需要观察的现象**：客户端慢速读取（如 `curl --limit-rate 1k`）一个大文件响应时，`c->send_chain` 返回非 NULL 的剩余链，`ngx_chain_writer` 返回 `NGX_AGAIN`，`ngx_output_chain` 把它透传给 copy_filter，copy_filter 据 `ctx->in` 置 `NGX_HTTP_COPY_BUFFERED`，请求被标记为「有缓冲数据未发」，worker 停止从文件继续读、挂写事件等待。

**预期结果**：能画出 `ngx_output_chain` 返回 `NGX_AGAIN` → copy_filter 置 `buffered` 位 → 请求挂起等写事件 → socket 可写后再次驱动的完整背压链路。**具体日志细节待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`ngx_chain_writer` 里 `if (size == 0 && !c->buffered) return NGX_OK` 这条捷径在什么情况下命中？

**答案**：当本轮 `in` 全是「零大小且非 special」的非法 buf（已被前面告警跳过）或 `ctx->out` 本来就空、且连接此前没有未发完的缓冲数据（`!c->buffered`）时，`size` 累加为 0。此时没必要调用 `c->send_chain`（写空链没意义），直接返回 `NGX_OK`。若 `c->buffered` 为真（此前还有数据没发完），即使本轮 `size==0` 也不能返回 OK，必须再调一次 `send_chain` 尝试排空。

**练习 2**：为什么 `ngx_chain_writer` 在 `chain && c->write->ready` 时要 `ngx_post_event(c->write, &ngx_posted_next_events)`？

**答案**：`send_chain` 返回非 NULL 说明 socket 缓冲区刚好写满、还有剩余没发；但 `c->write->ready` 仍为真说明写事件此刻还「就绪」（可能缓冲区刚满）。把写事件投递到 `ngx_posted_next_events`（u5-l4 会讲 posted 事件），让本轮事件循环处理完当前事件后**立刻紧接着再处理一次写事件**，而不必等下一轮 epoll——这加快排空剩余数据，减少延迟。这是 nginx 在「写就绪」时的一个微优化。

---

## 5. 综合实践

把本讲的 buf、chain、output_chain、chain_writer 串起来，完成一个「源码阅读 + 行为预测」任务：

**背景**：一个静态文件请求 `/index.html` 走 sendfile 直发；同一个响应若被 `gzip` on 包裹，则 gzip 过滤器要求 body 在内存里。两种场景下 `ngx_output_chain` 的行为截然不同。

**任务**：

1. 打开 [src/http/ngx_http_copy_filter_module.c:99-122](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_copy_filter_module.c#L99-L122)，回答：
   - `ctx->sendfile` 来自哪里？静态文件直发时它的值？
   - `ctx->need_in_memory` 何时为真？gzip 场景下谁设了 `r->filter_need_in_memory`？（提示：在 `src/http/modules/ngx_http_gzip_filter_module.c` 中搜索 `filter_need_in_memory`。）
2. 对照 [src/core/ngx_output_chain.c:249-306](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_output_chain.c#L249-L306) 的 `as_is` 判定，预测：
   - sendfile 直发场景：文件 buf 是否 `as_is`？走哪条快路径？是否发生 `memcpy`？
   - gzip 场景：文件 buf 是否 `as_is`？会进入哪段 `copy_buf`？数据从哪里读到哪里？
3. 用 `curl --limit-rate 1k` 模拟慢客户端拉取一个大文件，结合 [src/http/ngx_http_copy_filter_module.c:147-152](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_copy_filter_module.c#L147-L152) 解释 `NGX_HTTP_COPY_BUFFERED` 标志如何让 nginx 暂停从文件读数据。

**预期产出**：一张「场景 × as_is × 是否拷贝 × 走哪条 I/O 路径 × 背压标志」的五列表，覆盖 sendfile 直发、need_in_memory 拷贝、慢客户端背压三种情形。这张表是你后续阅读 HTTP 过滤器链（u6-l6）与 upstream 缓冲（u7-l5）时的「数据流速查卡」。

## 6. 本讲小结

- `ngx_buf_t` 用一组区间字段（`pos/last` 内存、`file_pos/file_last` 文件）加一组位域标志（`temporary/memory/mmap/in_file/flush/last_buf/...`）同时表达内存数据、文件数据、控制信号三种身份；`ngx_buf_size` / `ngx_buf_in_memory` / `ngx_buf_special` 是贯穿全代码的判定宏。
- `ngx_chain_t` 是极简单链表（`buf` + `next`）；`ngx_alloc_chain_link` 优先从内存池的 `chain` 自由链复用节点，`ngx_free_chain` 头插回收，`ngx_chain_add_copy` 浅拷贝共享 buf——零拷贝是默认取向。
- `ngx_output_chain` 是通用过滤式输出框架，靠 `in`/`free`/`busy` 三链表驱动：`as_is` 判定无需拷贝时直通，否则用 `copy_buf` 把文件读进临时内存 buf；`bufs.num` 给临时 buf 配额限流，`ngx_chain_update_chains` 按 `tag` 回收已发完的节点。
- 返回值 `NGX_AGAIN` 是背压信号：下游 socket 写满或拷贝被限流时，数据滞留在 `ctx->busy`/`ctx->in`，函数返回 `NGX_AGAIN`，等下次事件再来；nginx 没有 `NEED_IN_MORE` 这个常量，等价信号就是 `NGX_AGAIN`。
- `ngx_chain_writer` 是默认写出口：把 `out` 追加到 `ctx->out`，调 `c->send_chain` 真正写 socket，按剩余链决定返回 `NGX_OK`（全发完）或 `NGX_AGAIN`（还有剩余）；HTTP copy 过滤器在请求池上装配 `ngx_output_chain_ctx_t` 并用 `ctx->in==NULL` 切换 `NGX_HTTP_COPY_BUFFERED` 标志，把背压向上游传播。

## 7. 下一步学习建议

- 本讲的 `ngx_chain_t` 与 `ngx_output_chain` 是 **u6-l6 过滤器链 header/body filter** 的直接前置：header filter 产出响应头、body filter 经 copy_filter → `ngx_output_chain` → write_filter → `ngx_chain_writer` 写出，届时会看到本讲结构的完整闭环。
- 本讲的 `ngx_buf_t` 文件分支与 `c->send_chain` 将在 **u4-l4 操作系统抽象层** 深入：`ngx_linux_sendfile_chain`、`ngx_writev_chain` 正是 `send_chain` 的 Linux 实现，可对照理解「文件 buf 走 sendfile、内存 buf 走 writev」的分流。
- 本讲的背压与三链表模型是 **u7-l5 事件化缓冲 ngx_event_pipe** 的基础：upstream 响应在客户端慢时的临时文件溢写，本质上是把 `in/free/busy` 模型放大到「内存缓冲满则溢写磁盘」的更复杂版本。
- 若想看 buf 标志位的真实传播，可在学完 u6-l2 请求生命周期后，在 `ngx_http_finalize_request` 与 copy_filter 之间追踪 `last_buf` 如何从 content handler 一路传到 `ngx_chain_writer` 触发连接收尾。
