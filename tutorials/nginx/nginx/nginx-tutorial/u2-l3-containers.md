# 容器数据结构：array/list/queue/rbtree/hash

## 1. 本讲目标

nginx 用 C 写成，没有 C++ 的 STL，于是它在 `src/core` 里自备了一套精炼的容器。本讲的目标是让你：

1. 掌握 `ngx_array_t`（动态数组）与 `ngx_list_t`（分块链表）两种线性容器的扩容机制与适用场景。
2. 理解 `ngx_queue_t` 作为「侵入式链表」的设计哲学，能读懂它的插入、遍历与归并排序。
3. 掌握 `ngx_rbtree_t`（红黑树）的初始化、插入与中序遍历，并理解它为何被选作定时器底座。
4. 掌握 `ngx_hash_t`（哈希表）的「建表一次、只读查询」模型，理解桶大小搜索与通配符查找。
5. 了解 `ngx_radix_tree_t`（基数树）在 IP/CIDR 匹配中的用途。

学完后，你应能打开任意一个 nginx 模块，认出它用了哪种容器、为什么用这种容器。

## 2. 前置知识

本讲默认你已学过：

- **内存池 `ngx_pool_t`**（u2-l1）：本讲所有容器的节点内存都来自 `ngx_palloc` / `ngx_pcalloc`，容器本身不负责「逐个 free」，而是随池整体回收。这是理解 nginx 容器为何普遍「没有 free 函数」的关键。
- **`ngx_str_t` 与 `ngx_uint_t`**（u2-l2）：哈希表的键是字节串，`ngx_uint_t` 是 nginx 的无符号整型别名（在 64 位平台通常是 `uint64_t`）。

两个 C 概念提前点出：

- **侵入式容器（intrusive container）**：节点结构里「内嵌」链表/树的链接字段，而不是把用户数据包进容器节点。好处是零额外分配、一个对象可同时挂在多个链表上；代价是用户结构必须显式包含链接成员。`ngx_queue_t`、`ngx_rbtree_t` 都是侵入式的。
- **offsetof**：`offsetof(type, member)` 返回成员在结构体内的字节偏移。侵入式容器靠它从「链接字段地址」反推出「宿主结构地址」，是 C 里实现泛型容器的常用技巧。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/core/ngx_array.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_array.c) | 动态数组的创建、push、destroy |
| [src/core/ngx_array.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_array.h) | `ngx_array_t` 结构与 `ngx_array_init` 内联函数 |
| [src/core/ngx_list.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_list.c) | 分块链表的创建与 push |
| [src/core/ngx_list.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_list.h) | `ngx_list_t` / `ngx_list_part_t` 结构、`ngx_list_init`、遍历范式注释 |
| [src/core/ngx_queue.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_queue.h) | 侵入式双向队列：全部以宏实现 |
| [src/core/ngx_queue.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_queue.c) | 队列的求中点与稳定归并排序 |
| [src/core/ngx_rbtree.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rbtree.h) | 红黑树节点/树结构、颜色宏、`ngx_rbtree_min` |
| [src/core/ngx_rbtree.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rbtree.c) | 红黑树插入/删除/后继，基于 CLRS 算法 |
| [src/core/ngx_hash.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_hash.h) | 哈希表相关结构与函数声明 |
| [src/core/ngx_hash.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_hash.c) | 哈希查找、建表、通配符建表、键收集 |
| [src/core/ngx_radix_tree.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_radix_tree.h) | 基数树结构与 API |
| [src/core/ngx_radix_tree.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_radix_tree.c) | 32/128 位基数树的插入、删除、查找 |

## 4. 核心概念与源码讲解

### 4.1 动态数组 ngx_array_t

#### 4.1.1 概念说明

`ngx_array_t` 是一块连续内存里存放的、元素定长的动态数组。它解决两个问题：

- **按下标 O(1) 随机访问**（因为内存连续）。
- **容量可增长**：初始预分配 `n` 个元素槽，push 满了再扩。

和 C++ `std::vector` 的核心区别：内存来自内存池，且没有「单独 free 某个元素」的能力——要么整池回收，要么调用 `ngx_array_destroy` 把恰好位于池顶部的内存退还给池。

#### 4.1.2 核心流程

1. `ngx_array_create(pool, n, size)`：在池上分配控制结构，并预分配 `n * size` 的元素区。
2. `ngx_array_push(a)`：返回下一个空闲槽指针。若已满，先尝试「原地生长」，否则开两倍新数组并拷贝。
3. 通过 `a->elts` 取首元素地址，按下标遍历 `a->nelts` 个元素。
4. `ngx_array_destroy(a)`：若数组恰好在池顶，回退池指针。

push 的均摊代价是 \(O(1)\)：每次翻倍后拷贝的总量是几何级数

\[
\sum_{i=0}^{k} 2^{i} \cdot size = (2^{k+1}-1)\cdot size
\]

分摊到 \(2^{k+1}\) 次 push 上，每次约为常数。

#### 4.1.3 源码精读

结构体只有 5 个字段，控制结构与数据区分离：

[src/core/ngx_array.h:16-22](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_array.h#L16-L22) — 定义 `ngx_array_t`：`elts` 指向元素首地址，`nelts` 当前元素数，`size` 单元素字节数，`nalloc` 容量，`pool` 所属内存池。

`create` 只是把「分配控制结构」与「初始化」拆开：

[src/core/ngx_array.c:12-27](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_array.c#L12-L27) — `ngx_array_create` 先 `ngx_palloc` 出控制结构，再委托给 `ngx_array_init` 预分配元素区。

push 的精髓在「两种扩容路径」：

[src/core/ngx_array.c:54-84](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_array.c#L54-L84) — 数组已满时的处理。若 `elts` 末端正好是池当前水位 `p->d.last` 且本块还有空余，直接 `p->d.last += a->size`、`nalloc++` 原地生长，零拷贝；否则 `ngx_palloc(2*size)` 开新数组、`ngx_memcpy` 搬运、`nalloc *= 2`。前者是 nginx 反复利用「池顶 bump 分配」特性的典型优化。

destroy 同样利用「池顶」特性做精确回收：

[src/core/ngx_array.c:30-44](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_array.c#L30-L44) — `ngx_array_destroy` 只有当元素区、控制结构恰好处在池顶部时才回退 `p->d.last`，否则什么都不做（交给池整体销毁）。这正是「小块不支持单独 free」的体现。

#### 4.1.4 代码实践

**实践目标**：用 `ngx_array` 在内存池上存一组整数并遍历；同时阅读一段真实使用。

**操作步骤**：

1. 阅读下面这段「示例代码」（非 nginx 原有代码），理解 API 节奏：

```c
/* 示例代码：动态数组存整数（非项目原有代码） */
ngx_array_t *arr = ngx_array_create(pool, 4, sizeof(ngx_int_t));
for (ngx_int_t i = 0; i < 10; i++) {     /* 初始容量 4，会触发一次扩容 */
    ngx_int_t *slot = ngx_array_push(arr);
    if (slot == NULL) { return NGX_ERROR; }
    *slot = i * i;
}

ngx_int_t *data = arr->elts;             /* 连续内存，按下标访问 */
for (ngx_uint_t i = 0; i < arr->nelts; i++) {
    ngx_log_error(NGX_LOG_INFO, log, 0, "arr[%ui]=%i", i, data[i]);
}
```

2. 阅读真实使用：`ngx_hash_wildcard_init` 在建通配符哈希时用两个临时数组收集「当前层」与「下一层」的键。

[src/core/ngx_hash.c:500-512](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_hash.c#L500-L512) — 在临时池上 `ngx_array_init` 两个数组 `curr_names`、`next_names`，随后用 `ngx_array_push` 逐个填键。这是「数组 + 内存池」配合递归的典型写法。

**需要观察的现象**：示例代码中初始容量为 4，循环到第 5 次 `ngx_array_push` 时会进入 `a->nelts == a->nalloc` 分支；若此时数组恰在池顶则原地生长，否则触发两倍扩容与一次 `ngx_memcpy`。

**预期结果**：日志按序打印 `arr[0]=0` 到 `arr[9]=81`。由于本例依赖 nginx 的池、日志与编译环境，独立编译需要链接 nginx 核心对象文件，**运行结果待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`ngx_array_push` 在「原地生长」分支里为什么只 `nalloc++` 而不是 `nalloc *= 2`？

**答案**：原地生长只在池顶有「恰好一个元素」的空余时才走，它没有获得两倍空间，只是把池水位往后挪了一个 `size`，所以只能 `nalloc++`。两倍扩容是另一条「另开新数组」分支才做的事。

**练习 2**：若连续 `ngx_array_push` 1000 次但初始 `n=1`，最坏情况下会发生多少次 `ngx_memcpy`？

**答案**：每次翻倍，拷贝规模为 1, 2, 4, ..., 512，共约 10 次拷贝。总拷贝元素数约为 \(2^{10}-1 \approx 1023\)，均摊到 1000 次 push 上每次约 1 次元素拷贝，即均摊 \(O(1)\)。

---

### 4.2 分块链表 ngx_list_t

#### 4.2.1 概念说明

`ngx_list_t` 是「分块链表（unrolled list）」：不是每个元素一个节点，而是每个「块（part）」装 `nalloc` 个连续元素，块之间用 `next` 串成链表。

它解决数组的两个痛点：

- **最终大小未知时不必反复两倍扩容**：满了就再挂一个定长块。
- **保留连续内存的局部性**：块内仍是连续数组，cache 友好。

代价：**失去 O(1) 随机访问**，只能顺序遍历。nginx 用它存放「数量可变、只需顺序处理」的数据，最经典的就是 HTTP 请求头列表。

#### 4.2.2 核心流程

1. `ngx_list_create(pool, n, size)`：分配控制结构 + 第一个块的元素区。
2. `ngx_list_push(l)`：返回最后一个块的下一个空闲槽；若最后一块满了，新分配一个 `ngx_list_part_t` 并挂到链尾。
3. 遍历：从 `&list->part` 开始，按 `part->nelts` 计数，到末尾跳 `part->next`。

#### 4.2.3 源码精读

结构体把「第一个块」直接内嵌（`part`），并用 `last` 指针跟踪链尾，避免每次 push 都遍历链表：

[src/core/ngx_list.h:18-31](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_list.h#L18-L31) — `ngx_list_part_s` 含 `elts`/`nelts`/`next`；`ngx_list_t` 内嵌第一个 `part`，外加 `last` 指向当前链尾块。

push 的「满了就挂新块」逻辑：

[src/core/ngx_list.c:30-63](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_list.c#L30-L63) — 当 `last->nelts == l->nalloc` 时，新分配一个 `ngx_list_part_t` 及其元素区，接到 `l->last->next`，再前移 `l->last`。注意它**不拷贝旧数据**（与数组扩容不同），因为块之间本就是链式关系。

头文件里直接给出了遍历范式，照抄即可：

[src/core/ngx_list.h:55-77](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_list.h#L55-L77) — 官方遍历模板：双重循环，内层按下标走完 `part->nelts`，到末尾跳 `part->next` 并重置 `i=0`，`part->next == NULL` 时结束。

#### 4.2.4 代码实践

**实践目标**：确认 HTTP 请求头用 `ngx_list_t` 存储，并理解其遍历方式。

**操作步骤**：

1. 打开 HTTP 请求结构定义，确认请求头容器类型。

[src/http/ngx_http_request.h:186](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.h#L186) — `ngx_http_request_t` 的请求头入站集合 `headers_in.headers` 字段类型为 `ngx_list_t`；出站响应头 [src/http/ngx_http_request.h:263](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.h#L263) 同样是 `ngx_list_t headers`。每个元素是一个 `ngx_table_elt_t`（键值对）。

2. 在 `ngx_http_request.c` 中搜索 `headers_in.headers.part` 与 `part.next`，对照 4.2.3 的遍历模板，确认 nginx 正是用该范式逐个处理请求头。

**需要观察的现象**：处理请求头的循环里，外层用 `part = part->next` 推进块，内层用 `i < part->nelts` 限定本块有效元素数。

**预期结果**：能看到与头文件注释几乎一致的遍历结构，证明「分块链表 + 顺序遍历」是 nginx 处理头部列表的标准套路。**具体行号待本地在 `ngx_http_request.c` 中确认**（不同版本可能微调）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ngx_list_push` 不像 `ngx_array_push` 那样拷贝旧数据？

**答案**：链表的块之间靠 `next` 指针连接，新块挂到链尾即可，旧块仍然有效、无需搬迁；而数组要保持「单一连续内存」的随机访问能力，扩容时必须整体搬迁。

**练习 2**：若要实现「按下标取第 i 个元素」，`ngx_list_t` 的复杂度是多少？

**答案**：\(O(i / nalloc)\)——必须从第一个块起逐块跳 `next`，直到落在包含第 i 个元素的块。因此 nginx 只在「只需顺序遍历」的场景用链表。

---

### 4.3 侵入式双向队列 ngx_queue_t

#### 4.3.1 概念说明

`ngx_queue_t` 是一个**侵入式双向循环链表**：节点结构只有 `prev`/`next` 两个指针，它被「嵌入」到用户自己的结构体里当成员。一个用户对象可以同时挂在多个 queue 上（只要它有多个 queue 成员），且挂接操作零内存分配。

它和 `ngx_list_t` 的本质差异：

| 维度 | `ngx_list_t` | `ngx_queue_t` |
| --- | --- | --- |
| 数据组织 | 块内连续数组 | 一个节点一个元素 |
| 内存 | 池分配块 | 节点内嵌，零分配 |
| 随机访问 | 块内可下标 | 无 |
| 典型用途 | 头部等定长集合 | 任意结构排队、排序 |

#### 4.3.2 核心流程

1. 定义一个「哨兵头」`ngx_queue_t q; ngx_queue_init(&q);`——初始化时 `prev = next = &q`，空表自指。
2. `ngx_queue_insert_head(&q, &node)` / `insert_tail`：O(1) 挂接。
3. `ngx_queue_remove(&node)`：O(1) 摘除，只改前后节点的指针。
4. 遍历：`for (p = ngx_queue_head(&q); p != ngx_queue_sentinel(&q); p = ngx_queue_next(p))`。
5. 从节点指针反取宿主结构：`ngx_queue_data(p, my_type, link_member)`。

#### 4.3.3 源码精读

几乎全部逻辑都是头文件宏，没有函数调用开销：

[src/core/ngx_queue.h:16-21](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_queue.h#L16-L21) — `ngx_queue_s` 只有 `prev`/`next`，是纯链接字段，不含任何用户数据。

[src/core/ngx_queue.h:24-26](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_queue.h#L24-L26) — `ngx_queue_init` 让头节点自指，构成「空表 = 头的 prev/next 都指向自己」。

[src/core/ngx_queue.h:43-47](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_queue.h#L43-L47) — `ngx_queue_insert_tail` 四指针赋值完成双向挂接，O(1)。

[src/core/ngx_queue.h:75-87](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_queue.h#L75-L87) — `ngx_queue_remove`：DEBUG 模式额外把摘除节点的 `prev/next` 置 NULL 便于检测野指针，正式编译只做两指针修复。

反取宿主结构的关键宏，正是 offsetof 的应用：

[src/core/ngx_queue.h:106-107](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_queue.h#L106-L107) — `ngx_queue_data(q, type, link)` = `(type*)((u_char*)q - offsetof(type, link))`。已知内嵌成员地址，减去偏移得到宿主结构首地址。

`ngx_queue.c` 只实现了两个「非平凡」操作：求中点（快慢指针）与稳定归并排序。

[src/core/ngx_queue.c:21-49](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_queue.c#L21-L49) — `ngx_queue_middle` 用快慢指针：`middle` 每次走 1 步，`next` 每次走 2 步，`next` 到尾时 `middle` 恰在中点。用于归并排序的分割。

[src/core/ngx_queue.c:54-74](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_queue.c#L54-L74) — `ngx_queue_sort` 是递归归并：`middle` 取中点 → `ngx_queue_split` 切两半 → 各自递归排序 → `ngx_queue_merge` 合并。稳定排序，复杂度 \(O(n \log n)\)。

#### 4.3.4 代码实践

**实践目标**：写一个把自定义结构挂到 queue、排序、遍历的最小骨架，体会侵入式用法。

**操作步骤**：

1. 阅读下面「示例代码」（非 nginx 原有代码），注意 `ngx_queue_t` 是作为成员嵌入的：

```c
/* 示例代码：侵入式队列 + offsetof 反取宿主（非项目原有代码） */
typedef struct {
    ngx_queue_t  link;     /* 内嵌链接字段 */
    ngx_int_t    score;
} my_item_t;

ngx_queue_t  head;
ngx_queue_init(&head);

my_item_t *it;
for (ngx_int_t s = 5; s >= 1; s--) {
    it = ngx_palloc(pool, sizeof(my_item_t));
    it->score = s;
    ngx_queue_insert_tail(&head, &it->link);   /* 挂链接字段，零额外分配 */
}

/* 按 score 升序稳定排序 */
ngx_queue_sort(&head, cmp_score);

/* 遍历：从 link 反取宿主 */
ngx_queue_t *p;
for (p = ngx_queue_head(&head); p != ngx_queue_sentinel(&head);
     p = ngx_queue_next(p)) {
    my_item_t *item = ngx_queue_data(p, my_item_t, link);
    ngx_log_error(NGX_LOG_INFO, log, 0, "score=%i", item->score);
}
/* cmp_score 的签名见练习 1 */
```

**需要观察的现象**：挂接时传的是 `&it->link`（成员地址），遍历时用 `ngx_queue_data(p, my_item_t, link)` 还原回 `my_item_t*`，整个过程没有为链表节点单独分配内存。

**预期结果**：日志按 `score=1,2,3,4,5` 顺序输出。本例依赖 nginx 编译环境，**运行结果待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：写出上面示例中 `cmp_score` 的函数签名与实现。

**答案**：

```c
static ngx_int_t cmp_score(const ngx_queue_t *a, const ngx_queue_t *b) {
    my_item_t *ia = ngx_queue_data(a, my_item_t, link);
    my_item_t *ib = ngx_queue_data(b, my_item_t, link);
    return ia->score - ib->score;   /* 负=排前，正=排后 */
}
```

**练习 2**：为什么 `ngx_queue_remove` 在 DEBUG 模式下要把 `prev/next` 置 NULL？

**答案**：防止摘除后的「悬空节点」被误用——一旦再被解引用，NULL 会立刻触发段错误从而暴露 bug；正式编译为省两条赋值而省略，靠正确逻辑保证不被访问。

---

### 4.4 红黑树 ngx_rbtree_t

#### 4.4.1 概念说明

`ngx_rbtree_t` 是 nginx 自带的平衡二叉搜索树实现，算法直接来自《Introduction to Algorithms》（CLRS）。它解决「需要有序、动态增删、频繁取最值」的场景：

- 查找/插入/删除均为 \(O(\log n)\)。
- 树高上界为 \(2\log_2(n+1)\)，不会退化为链表。
- 取最小值 \(O(\log n)\)（一路向左），若维护「最左节点」指针可做到 \(O(1)\)。

nginx 最重要的用途是**定时器**：每个事件挂一个按到期时刻排序的节点，`ngx_event_find_timer` 取最左节点即得最早超时。其次是共享内存里的各种有序索引。

#### 4.4.2 核心流程

1. 准备一个 `sentinel`（哨兵）节点并 `ngx_rbtree_init(&tree, &sentinel, insert_fn)`。哨兵统一代表「空子树」，省去大量 NULL 判断。
2. 设定节点 `node.key`，调用 `ngx_rbtree_insert(&tree, &node)`：先按 `insert_fn` 做普通 BST 插入并染红，再自底向上重新平衡（旋转 + 变色）。
3. `insert_fn` 有两种：`ngx_rbtree_insert_value`（普通数值比较）与 `ngx_rbtree_insert_timer_value`（带溢出处理的毫秒比较）。
4. 取最小：`ngx_rbtree_min(root, sentinel)`；中序遍历后继：`ngx_rbtree_next(&tree, node)`。
5. 删除：`ngx_rbtree_delete(&tree, &node)`，删除后做「删除修正」。

红黑树五条性质简要（用于理解平衡逻辑）：

1. 每个节点非红即黑。
2. 根是黑。
3. 哨兵（叶子）是黑。
4. 红节点的孩子必黑（不能有连续红）。
5. 任一节点到其所有后代哨兵的路径上黑节点数相同（黑高相同）。

#### 4.4.3 源码精读

节点结构把 `key`、三个指针、颜色、1 字节 `data` 打包：

[src/core/ngx_rbtree.h:22-29](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rbtree.h#L22-L29) — `ngx_rbtree_node_s`：`key` 为排序键，`left/right/parent` 三指针，`color`（1=红,0=黑），`data` 仅 1 字节（多数场景不够用，所以通常把节点嵌入更大结构）。

树结构 + 初始化宏：

[src/core/ngx_rbtree.h:37-48](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rbtree.h#L37-L48) — `ngx_rbtree_t` 含 `root`、`sentinel`、`insert` 函数指针；`ngx_rbtree_init` 把根指向哨兵、哨兵染黑、绑定插入函数。用函数指针让同一套平衡逻辑适配不同比较方式。

插入主流程：先 BST 插入，再自底向上重平衡：

[src/core/ngx_rbtree.c:24-93](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rbtree.c#L24-L93) — `ngx_rbtree_insert`：空树时直接作为黑根；否则调用 `tree->insert` 找位插入并染红，随后 `while` 循环在「父为红」时做「叔节点变色 / 旋转」三类修正，最后强制根为黑。

两种插入比较函数，注意定时器版本的特殊处理：

[src/core/ngx_rbtree.c:96-118](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rbtree.c#L96-L118) — `ngx_rbtree_insert_value`：普通比较 `node->key < temp->key` 决定向左向右。

[src/core/ngx_rbtree.c:121-153](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rbtree.c#L121-L153) — `ngx_rbtree_insert_timer_value`：用 `(ngx_rbtree_key_int_t)(node->key - temp->key) < 0` 判断。注释说明定时器毫秒值存在 32 位下约 49 天溢出，这种带符号差值比较天然处理了回绕。

取最小与后继，是中序遍历的基础：

[src/core/ngx_rbtree.h:76-84](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rbtree.h#L76-L84) — `ngx_rbtree_min` 一路向左到哨兵为止。

[src/core/ngx_rbtree.c:378-404](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_rbtree.c#L378-L404) — `ngx_rbtree_next`：若有右子树取右子树最小；否则沿父链上溯，直到从左子方向回来，那个祖先就是后继。

真实用途：事件定时器。

[src/event/ngx_event_timer.c:13-26](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L13-L26) — 全局 `ngx_event_timer_rbtree` 与哨兵 `ngx_event_timer_sentinel`，`ngx_event_timer_init` 用 `ngx_rbtree_insert_timer_value` 初始化。注释说明该树允许重复 key，因为只用它取最小值。

#### 4.4.4 代码实践

**实践目标**：用红黑树实现一个按 key 排序的小集合，并升序遍历；再对照定时器真实用法。

**操作步骤**：

1. 阅读下面「示例代码」（非 nginx 原有代码），注意把 `ngx_rbtree_node_t` 作为成员嵌入：

```c
/* 示例代码：红黑树排序集合（非项目原有代码） */
typedef struct {
    ngx_rbtree_node_t  node;     /* 内嵌：key 存在 node.key */
    ngx_uint_t         extra;
} my_entry_t;

ngx_rbtree_t       tree;
ngx_rbtree_node_t  sentinel;
ngx_rbtree_init(&tree, &sentinel, ngx_rbtree_insert_value);

for (ngx_uint_t k = 0; k < 5; k++) {
    my_entry_t *e = ngx_palloc(pool, sizeof(my_entry_t));
    e->node.key = (k * 13) % 7;  /* 乱序 key */
    e->extra = k;
    ngx_rbtree_insert(&tree, &e->node);
}

/* 从最小 key 升序遍历 */
ngx_rbtree_node_t *n;
for (n = ngx_rbtree_min(tree.root, tree.sentinel);
     n != tree.sentinel;
     n = ngx_rbtree_next(&tree, n)) {
    my_entry_t *e = ngx_rbtree_data(n, my_entry_t, node);
    ngx_log_error(NGX_LOG_INFO, log, 0, "key=%ui extra=%ui",
                  e->node.key, e->extra);
}
```

2. 对照 [src/event/ngx_event_timer.c:32-45](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L32-L45) 的 `ngx_event_find_timer`：它先判断树是否为空（`root == sentinel`），再取 `ngx_rbtree_min`，与本示例遍历起点一致。

**需要观察的现象**：尽管插入 key 是乱序的 `(k*13)%7 = 0,6,5,1,0`，遍历输出应按键升序排列，且重复 key 0 出现两次（红黑树允许重复 key，靠「相等时向右」的隐式约定，见练习 2）。

**预期结果**：日志按 key 升序打印。本例依赖 nginx 编译环境，**运行结果待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么定时器红黑树用 `ngx_rbtree_insert_timer_value` 而不是普通 `ngx_rbtree_insert_value`？

**答案**：定时器 key 是 32 位毫秒值，约 49 天会溢出回绕。直接用 `node->key < temp->key` 的无符号比较在回绕后会得到错误的大小关系；而 `(ngx_rbtree_key_int_t)(node->key - temp->key)` 把差值转成有符号数，利用「有符号差值的正负」天然反映时间先后，正确处理回绕。

**练习 2**：红黑树如何处理「重复 key」？

**答案**：`ngx_rbtree_insert_value` 在 `node->key < temp->key` 为假（即 `key >= temp->key`）时向右走，因此相等 key 会被插到已有相等节点的右子树。中序遍历时它们会相邻出现，所以能稳定保留重复项。事件定时器正利用这一点容纳多个同时刻到期的事件。

---

### 4.5 哈希表 ngx_hash_t

#### 4.5.1 概念说明

`ngx_hash_t` 是 nginx 的**静态哈希表**：在配置加载阶段一次性建表，运行时只做只读查找，不支持运行时增删。这种「建一次、查无数次」的模型非常适合 nginx 的配置数据（如 HTTP 头名映射、变量名映射、server_name 匹配），这些数据在 reload 前固定不变。

设计要点：

- **开链法**：每个桶是一条 `ngx_hash_elt_t` 链，但链节点是连续紧凑排布的（不是每节点一次 malloc）。
- **桶数自选**：建表时在 `[start, max_size]` 区间搜索一个最小的桶数 `size`，使得每个桶都装得下（不超过 `bucket_size`），从而让查找时桶内链很短。
- **键小写化**：查找前键统一转小写，建表时也存小写，大小写不敏感。

查找的平均复杂度约为 \(O(1 + \alpha)\)，其中负载因子 \(\alpha = n / size\)；因建表时控制了桶大小，\(\alpha\) 被压得很低。

#### 4.5.2 核心流程

**建表**（`ngx_hash_init`）：

1. 校验 `max_size`、`bucket_size` 合法性。
2. 在 `[start, max_size]` 试探 `size`：对每个候选 `size`，模拟把所有键按 `key_hash % size` 落桶，累加每桶占用，若任一桶超 `bucket_size` 则换下一个 `size`。
3. 找到合适 `size` 后，在池上分配 `buckets` 数组与紧凑的 `elts` 区，逐键写入桶链，末尾用 `value=NULL` 标记链尾。

**查找**（`ngx_hash_find`）：

1. `elt = buckets[key % size]` 定位桶头。
2. 沿桶链走，逐个比较 `len` 与字节内容，命中则返回 `value`。
3. 遇到 `value == NULL` 的结尾标记则未命中。

#### 4.5.3 源码精读

核心哈希函数是一个简单乘加：

[src/core/ngx_hash.h:114](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_hash.h#L114) — `#define ngx_hash(key, c) ((ngx_uint_t) key * 31 + c)`。经典乘 31 字符串哈希，分布足够均匀且实现极简。

桶元素结构是变长的（`name[1]` 柔性数组）：

[src/core/ngx_hash.h:16-26](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_hash.h#L16-L26) — `ngx_hash_elt_t` 含 `value` 指针、`len`、变长 `name`；`ngx_hash_t` 仅含 `buckets` 与 `size`。

查找实现，注意桶内步进是按指针对齐计算的：

[src/core/ngx_hash.c:12-49](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_hash.c#L12-L49) — `ngx_hash_find`：`key % size` 取桶，`while (elt->value)` 遍历链；先比 `len` 再逐字节比 `name`；命中返回 `value`，否则用 `ngx_align_ptr(&elt->name[0] + elt->len, sizeof(void*))` 跳到下一个元素。这里「对齐」是为了让下一个 `elt` 的 `value` 指针落在机器字边界，保证访问性能。

建表时搜索桶数的双重循环：

[src/core/ngx_hash.c:305-335](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_hash.c#L305-L335) — 外层枚举 `size`，内层模拟落桶并累计每桶字节数 `test[key]`；若某桶超 `bucket_size` 则 `goto next` 试更大 `size`；全部通过则 `goto found`。这是「用空间换低冲突」的贪心选择。

实际写入桶链：

[src/core/ngx_hash.c:424-448](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_hash.c#L424-L448) — 逐键计算 `key_hash % size`，在对应桶的当前偏移处写 `value`、`len`，并用 `ngx_strlow` 把键小写拷进 `name`；最后给每个非空桶末尾补一个 `value=NULL` 的结束标记。

通配符查找支持 `*.example.com` 这类前缀/后缀通配，靠「值指针低 2 位编码语义」：

[src/core/ngx_hash.c:52-143](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_hash.c#L52-L143) — `ngx_hash_find_wc_head` 注释列出低 2 位的含义：`00` 普通值、`01` 仅通配、`10`/`11` 指向下一级通配哈希。因为指针至少 4 字节对齐，低 2 位本就空闲，nginx 用它来编码状态，省去额外字段。

#### 4.5.4 代码实践

**实践目标**：理解哈希查找路径，并在 HTTP 代码中定位一次真实的哈希建表。

**操作步骤**：

1. 用 `ngx_hash_find` 的逻辑手工推演一次查找：假设 `size=8`、键 `"Host"`（小写 `host`，`key_hash` 设为某值），`key % 8` 落到第 3 桶，沿桶链比较 `len` 与字节直到命中。
2. 在 HTTP 模块里搜索 `ngx_hash_init` 调用，确认请求头名表是静态建一次的。

[src/http/ngx_http.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c) 中存在对 `ngx_hash_init` 的调用（用于把请求头名映射到处理函数），可用编辑器在该文件搜索 `ngx_hash_init(` 定位具体行。这是「配置阶段建表、请求阶段只读查」的典型现场。

**需要观察的现象**：建表发生在配置解析/reload 阶段（`ngx_http_block` 流程内），运行时每个请求只调用 `ngx_hash_find` 只读查找，无锁无分配。

**预期结果**：能解释「为什么 nginx 处理海量请求时头部查找几乎不产生内存分配」——因为哈希表在启动时已建好，请求路径上只剩纯读。**具体调用行号待本地确认**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `ngx_hash_init` 要从 `start` 开始向上搜索 `size`，而不是直接用 `max_size`？

**答案**：在保证每桶不溢出的前提下，选最小的 `size` 可以让桶数组更紧凑、提高 cache 命中率，同时每个桶的链也不至于过长。直接用 `max_size` 会浪费空间且可能让大量桶为空。

**练习 2**：`ngx_hash_find_wc_head` 为什么能把「值指针的低 2 位」拿来存状态？

**答案**：哈希表里存的 `value` 是指针，而 C 里 `malloc` 返回的指针至少满足 `sizeof(void*)`（通常 4 或 8 字节）对齐，低 2 位恒为 0，处于空闲状态。nginx 借用这 2 位编码「普通值/仅通配/指向下级哈希」四种语义，取出真值时用 `& ~3` 屏蔽掉即可，省下一个状态字段。

---

### 4.6 基数树 ngx_radix_tree_t

#### 4.6.1 概念说明

`ngx_radix_tree_t` 是**基数树（radix / Patricia trie）**：按键的二进制位逐位分叉（0 走左、1 走右），用键的前缀做路径压缩。nginx 用 32 位键时它最多 32 层，128 位键（IPv6）最多 128 层。

它最适合 **IP/CIDR 最长前缀匹配**：给定一个 IP，从根按位下行，沿途记住最近一个「有值」的节点，最终返回的就是匹配到的最长前缀对应的值。nginx 的 `geo` 模块正是用它实现「按客户端 IP 段映射变量值」。

查找复杂度 \(O(W)\)，\(W\) 为键位宽（32 或 128），与树中节点数无关。

#### 4.6.2 核心流程

1. `ngx_radix_tree_create(pool, preallocate)`：建根节点，可选预分配前若干层（提升 TLB 命中）。
2. `ngx_radix32tree_insert(tree, key, mask, value)`：按 `mask` 限定的前缀位数下行，必要时新建节点，末端节点存 `value`。
3. `ngx_radix32tree_find(tree, key)`：从根按 `key` 的位下行，**沿途每次遇到有值节点就更新 `value`**，走到空节点为止，返回最后记下的值——这就是最长前缀匹配。
4. `ngx_radix32tree_delete`：把节点值清成 `NGX_RADIX_NO_VALUE`，若它无子节点则回收到 `free` 链。

#### 4.6.3 源码精读

节点结构与「无值」哨兵：

[src/core/ngx_radix_tree.h:16-34](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_radix_tree.h#L16-L34) — `NGX_RADIX_NO_VALUE = (uintptr_t)-1` 表示「该节点无值」；节点含 `right/left/parent` 与 `value`，无颜色、无 key（key 隐含在路径里）。

查找的最长前缀匹配逻辑：

[src/core/ngx_radix_tree.c:236-263](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_radix_tree.c#L236-L263) — `ngx_radix32tree_find`：`bit = 0x80000000` 从最高位起，`key & bit` 决定向右向左；循环里「只要当前节点有值就更新 `value`」，直到 `node == NULL`。返回的 `value` 即沿途最深的有值节点——最长前缀。

插入按 mask 限定前缀长度：

[src/core/ngx_radix_tree.c:108-170](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_radix_tree.c#L108-L170) — `ngx_radix32tree_insert`：第一段沿已有节点下行到 `bit & mask` 为止；若停在已有节点且已有值则返回 `NGX_BUSY`；第二段按需 `ngx_radix_alloc` 新建缺失的中间节点，末端写 `value`。

节点分配走「页内 bump + free 链」自管理：

[src/core/ngx_radix_tree.c:463-488](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_radix_tree.c#L463-L488) — `ngx_radix_alloc`：优先复用 `tree->free` 链上的回收节点；否则从一页（`ngx_pagesize`）内 bump 分配，页用完再 `ngx_pmemalign` 申请新页。这样基数树节点密集排布，cache/TLB 友好。

真实用途：geo 模块按 CIDR 映射。

[src/http/modules/ngx_http_geo_module.c:537](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_geo_module.c#L537) — `ngx_http_geo_module` 用 `ngx_radix_tree_create(cf->pool, -1)` 建树（`-1` 表示按页大小自动选预分配层数），随后按 CIDR 插入，请求时 [src/http/modules/ngx_http_geo_module.c:209](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_geo_module.c#L209) 用 `ngx_radix32tree_find` 按客户端 IP 查值。

#### 4.6.4 代码实践

**实践目标**：理解最长前缀匹配，阅读 geo 模块的真实使用。

**操作步骤**：

1. 手工推演：向树中插入 `10.0.0.0/8 -> value A`（mask = `0xFF000000`，前 8 位）和 `10.1.0.0/16 -> value B`（前 16 位）。查找 `10.1.2.3` 时，沿位下行会在第 8 层遇到 A（记下）、第 16 层遇到 B（覆盖记下），最终返回 B——这就是「最长前缀匹配」优先更细的网段。
2. 打开 [src/http/modules/ngx_http_geo_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_geo_module.c)，在 537 行附近看建树，在 191/209/222/231 行附近看按 `INADDR` 或客户端 IP 查找的分支。

**需要观察的现象**：`find` 返回 `NGX_RADIX_NO_VALUE` 时表示没有任何网段匹配，geo 模块据此回退到默认值。

**预期结果**：能解释「为什么 geo 模块用基数树而不是哈希表」——CIDR 是前缀匹配，哈希表只能精确匹配整键，无法表达「网段包含」关系。**运行结果待本地验证**。

#### 4.6.5 小练习与答案

**练习 1**：`ngx_radix32tree_find` 为什么要在循环里「沿途每次遇到有值节点就更新 value」，而不是走到目标位再取值？

**答案**：因为键可能只匹配到某个**前缀**就再无更细节点（下行遇到 NULL）。最长前缀匹配要求返回「最深的、有值的前缀节点」对应的值，所以必须沿途记录，遇到 NULL 结束时返回最近一次记录的有值节点。

**练习 2**：`ngx_radix_tree_create` 的 `preallocate` 参数设为 `-1` 是什么含义？

**答案**：表示「按页大小自动选择预分配层数」。基数树在 [src/core/ngx_radix_tree.c:62-79](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_radix_tree.c#L62-L79) 依据 `ngx_pagesize / sizeof(ngx_radix_node_t)` 选择 6/7/8 层，预先生成 `0,1,00,01,...` 这些浅层节点，让常见查找的前几跳落在连续内存里，提升 TLB 命中率，而预分配总量不超过一页以避免浪费。

---

## 5. 综合实践

把本讲的容器串起来，完成一个「配置查表」的源码阅读任务：

**背景**：nginx 在 HTTP 阶段需要把「请求头名」映射到「处理函数」，且要支持 `server_name` 通配匹配。这两件事分别用了本讲的两种容器。

**任务**：

1. 在 `src/http/ngx_http.c` 中找到一次 `ngx_hash_init` 或 `ngx_hash_keys_array_init` 调用，回答：
   - 它把哪些键（请求头名 / server_name）放进了哈希表？
   - 用的是 `ngx_hash_init`（精确）还是 `ngx_hash_wildcard_init`（含通配）？为什么？
   - 建表发生在请求处理之前还是之中？依据是调用点所在的初始化函数。
2. 在 `src/http/ngx_http_variables.c` 中找到变量名到 `ngx_http_variable_t` 的映射容器，确认它也是 `ngx_hash_t`，并解释「为什么变量系统适合用静态哈希表而非红黑树」。
3. 对照 4.4 节，说明 `ngx_event_timer_rbtree` 与上述哈希表在「何时建、何时查、能否改」上的差异，用一张表总结。

**预期产出**：一张三列表格（容器 / 数据生命周期 / 选用理由），覆盖 array、list、queue、rbtree、hash、radix 六种容器在 nginx 中的典型用法。这张表是你后续阅读 HTTP、事件、upstream 各模块时的「容器速查卡」。

## 6. 本讲小结

- `ngx_array_t` 是池上动态数组，push 满时优先「池顶原地生长」，否则两倍扩容拷贝；均摊 \(O(1)\)，按下标 \(O(1)\) 访问。
- `ngx_list_t` 是分块链表，每块定长、满了挂新块、不拷贝旧数据；适合「数量可变、只需顺序遍历」的集合（如 HTTP 头）。
- `ngx_queue_t` 是侵入式双向循环链表，全宏实现、零分配、O(1) 增删；靠 `ngx_queue_data` + offsetof 反取宿主结构；自带稳定归并排序。
- `ngx_rbtree_t` 是 CLRS 红黑树，\(O(\log n)\) 增删查；用哨兵节点省 NULL 判断；定时器版本用有符号差值比较处理 49 天毫秒溢出；nginx 定时器底座。
- `ngx_hash_t` 是静态哈希表，配置阶段一次建表、运行时只读查；建表时搜索最小合适桶数并紧凑排布桶链；通配符查找靠值指针低 2 位编码语义。
- `ngx_radix_tree_t` 是基数树，按键二进制位分叉，支持最长前缀匹配，\(O(W)\) 查找；geo 模块用它做 CIDR/IP 映射。
- 六种容器几乎都建在内存池之上，「无单独 free」是共同特征——生命周期由池统一管理。

## 7. 下一步学习建议

- 本讲的红黑树是下一讲 **u5-l4 定时器与 posted 事件** 的直接前置：定时器就挂在 `ngx_event_timer_rbtree` 上，建议结合 `ngx_event_find_timer` / `ngx_event_expire_timers` 精读。
- 本讲的哈希表是 **u6-l7 变量系统** 与 **u6-l1 HTTP 框架** 的基础：HTTP 头名表、变量名表都是 `ngx_hash_t`，届时会看到「建表」与「请求路径只读查」的完整闭环。
- 本讲的 `ngx_array_t` / `ngx_list_t` 将在 **u3-l1 配置解析** 中频繁出现：指令参数数组、server/location 列表都基于它们。
- 若想进一步理解共享内存里的并发容器，可在学完 **u4-l3 共享内存与 slab** 后回看红黑树在共享区中的使用（slab 分配器替代普通池）。
