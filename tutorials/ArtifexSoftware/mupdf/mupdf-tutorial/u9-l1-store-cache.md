# store：对象缓存与清理

## 1. 本讲目标

本讲深入 MuPDF 的资源缓存 `fz_store`。读完后你应当能够：

- 说清 `fz_store` 在整条渲染链路里扮演的角色，以及它为什么是「跨文档、跨页面、跨线程」的。
- 描述 store 的内部结构：LRU 双向链表 + 哈希表 + 大小计数，并解释为什么这样设计。
- 掌握 `fz_store_item` / `fz_find_item` 的存取约定，以及 `fz_store_type` 这张「键操作虚表」如何让 store 容纳任意类型的对象。
- 理解 store 达到上限时的两条回收路径：插入时触发的 `ensure_work` 驱逐，与内存分配失败时触发的 `scavenge` 多阶段回收。
- 通过修改 store 上限、重复渲染同一页，亲手观察到「缓存命中」对渲染耗时的影响。

## 2. 前置知识

本讲建立在你已经完成 u2-l2（内存管理与引用计数）与 u4-l3（draw device 与 pixmap 位图渲染）的基础之上。在阅读本讲前，请确认你已理解以下概念：

- **引用计数（keep / drop）**：MuPDF 用 `fz_keep_imp` / `fz_drop_imp` 在 `FZ_LOCK_ALLOC` 锁保护下原子地增减计数；计数归零时调用对象的析构回调。这是本讲反复出现的「语言」。
- **fz_storable 可存储对象**：凡能进 store 的对象都以 `fz_storable` 作为结构体首成员（C 手写多态），它只有 `refs`、`drop`、`droppable` 三个字段。详见 u2-l2。
- **pixmap**：连续像素内存，是图像/字形被光栅化后的最终产物，也是 store 中最典型的「值」。
- **fz_context**：几乎所有 fitz 函数的第一个参数。`ctx->store` 指向本讲要讲的缓存。

一个直觉性的比喻：**store 是 MuPDF 的「解码成果备忘录」**。把 PDF 里一张 5MB 的 JPEG 图片解码成一个 50MB 的 pixmap 很贵；如果同一张图在同一页出现两次，或者同一页要被渲染多次（放大、打印），我们没有理由重复解码。store 就是把这些「已经花过代价解码出来的、可复用的对象」按「键」存起来，下次要时直接取，内存吃紧时再按 LRU 丢弃。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/mupdf/fitz/store.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/store.h) | store 的公共 API 与数据结构声明：`fz_storable`、`fz_key_storable`、`fz_store_hash`、`fz_store_type` 虚表，以及 `fz_store_item`/`fz_find_item`/`fz_store_scavenge` 等函数原型。 |
| [source/fitz/store.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c) | store 的全部实现：链表/哈希表维护、存取、LRU、驱逐、多阶段 scavenge、reap。本讲的主角。 |
| [include/mupdf/fitz/context.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h) | 定义 `FZ_STORE_DEFAULT`（256MiB）与 `FZ_STORE_UNLIMITED`（0）两个上限常量。 |
| [source/fitz/context.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c) | `fz_new_context` 的第二阶段在创建 context 时调用 `fz_new_store_context` 建仓。 |
| [source/fitz/memory.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c) | `do_scavenging_malloc`：分配失败时先向 store「讨要」内存再重试，是 scavenge 的另一个触发点。 |
| [source/fitz/image.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/image.c) | image/pixmap tile 缓存——store 最典型的真实使用者，也是本讲选用的案例代码。 |

> 提示：`source/fitz/glyph-cache.h` 描述的「字形缓存」**不**在本讲范围内。字形缓存是一个独立的、上限写死为 1 MiB 的小缓存（见 u5-l1），与本章的 `fz_store` 是两套机制，不要混淆。

## 4. 核心概念与源码讲解

### 4.1 store 缓存结构

#### 4.1.1 概念说明

`fz_store` 是挂在一个 context 家族上的「对象缓存」。它的存在只为一件事：**把花过代价解码出来的对象暂存起来，供后续复用**。它有三个区别于普通容器的关键特征：

1. **它跨文档、跨页面**：同一个 context（或 `fz_clone_context` 出来的兄弟 context）共享同一份 store，因此渲染第 1 页时解码的字体/图片，渲染第 2 页时也能命中。
2. **它有容量上限**：内存是有限的，store 达到 `max` 字节数后必须丢弃旧对象。
3. **它服务于 keep/drop 引用计数体系**：放进 store 的对象会被 store 持有一个引用，外部也持有引用；谁先 drop、对象何时真正释放，由引用计数统一裁决。

理解 store，先理解它内部装的是什么。store 不直接存「对象」，而是存一条条 **`fz_item`**——每条 item 由「键 + 值 + 大小 + 类型虚表」组成：

```c
typedef struct fz_item
{
    void *key;
    fz_storable *val;
    size_t size;
    struct fz_item *next;
    struct fz_item *prev;
    fz_store *store;
    const fz_store_type *type;
} fz_item;
```
（[source/fitz/store.c:30-39](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L30-L39)）

其中 `val` 才是真正的解码产物（如一个 pixmap），`key` 是描述「这是什么对象的什么产物」的键（如「图 X 在 2 倍降采样下的左上角分块」），`size` 是该值占用 store 容量的字节数，`type` 指向一张描述「如何操作这种键」的虚表（见 4.2）。

而 `fz_store` 本身由四个核心部分组成：

```c
struct fz_store
{
    int refs;
    /* 按使用顺序排列的双向链表，LRU 项在尾部 */
    fz_item *head;
    fz_item *tail;
    /* 哈希表：快速定位「键可哈希」的那部分项 */
    fz_hash_table *hash;
    /* 容量跟踪：保持 size <= max */
    size_t max;
    size_t size;
    int defer_reap_count;
    int needs_reaping;
    int scavenging;
};
```
（[source/fitz/store.c:42-62](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L42-L62)）

这是一个**「双向链表 + 哈希表」的组合**，等价于教科书里的 LRU cache：

- **双向链表**（`head`/`tail`）按「最近使用」排序，刚用过的移到表头，最久没用的停在表尾。需要驱逐时从表尾（最老）开始，天然实现 LRU。
- **哈希表**（`hash`，4096 桶）给「键可哈希」的项提供 O(1) 查找，避免每次查找都线性扫描整条链表。注意：并非所有键都能哈希，哈希表只是链表的加速索引。
- **`max` / `size`**：上限与当前已用字节数，是驱逐判定的依据。
- **三个标志位**（`defer_reap_count` / `needs_reaping` / `scavenging`）服务于回收流程，我们在 4.3 讲。

> 文件顶部的注释一语道破并发模型：**「Every entry in fz_store is protected by the alloc lock」**——store 的每一项都由 `FZ_LOCK_ALLOC` 这把锁保护。这意味着 store 本身就是线程安全的，多线程渲染共享同一份 store 不需要调用方加锁（u9-l2 会用到这一点）。

#### 4.1.2 核心流程

store 的创建发生在 `fz_new_context` 的第二阶段（u2-l1 讲过的「两阶段构造」），它只是众多子上下文中的一个：

```c
fz_try(ctx)
{
    fz_new_store_context(ctx, max_store);
    fz_new_glyph_cache_context(ctx);
    ...
```
（[source/fitz/context.c:296-299](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L296-L299)）

其中 `max_store` 就是 `fz_new_context` 的第三个参数，取值由两个常量决定：

```c
enum {
    FZ_STORE_UNLIMITED = 0,
    FZ_STORE_DEFAULT = 256 << 20,
};
```
（[include/mupdf/fitz/context.h:312-315](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L312-L315)）

`FZ_STORE_DEFAULT` 即 \(256 \times 2^{20} = 256\,\text{MiB}\)；`FZ_STORE_UNLIMITED = 0` 是一个魔数值，store 内部凡是见到 `max == FZ_STORE_UNLIMITED` 就跳过一切容量检查（永不驱逐）。

`fz_new_store_context` 负责把上面的结构体造出来，重点是建好那张 4096 桶的哈希表：

```c
store->hash = fz_new_hash_table(ctx, 4096, sizeof(fz_store_hash),
                                FZ_LOCK_ALLOC, NULL);
...
store->refs = 1;
store->max = max;
```
（[source/fitz/store.c:64-86](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L64-L86)）

store 的整体生命周期是：

1. `fz_new_context` → `fz_new_store_context` 建仓，`max` 由参数决定。
2. 渲染过程中，格式专用层反复 `fz_store_item` 存入、`fz_find_item` 取出。
3. `store->size` 超过 `max` 时，插入路径触发 `ensure_work` 驱逐旧项；分配失败时触发 `scavenge`。
4. context 销毁时 `fz_drop_store_context` → `fz_empty_store` 逐项清空。

### 4.2 store_item 存取

#### 4.2.1 概念说明

存取是 store 的高频操作。这里有两个反直觉但至关重要的设计，理解它们本节就通了：

**第一，store 对「键」一无所知。** 键可以是「图像 + 缩放 + 区域」，也可以是「字体 + 字形 + 变换」。store 不可能为每种键写专门的存取代码，于是它把「如何 keep/drop/比较/哈希一个键」抽象成一张虚表 `fz_store_type`，和每种对象类型一一对应：

```c
typedef struct
{
    const char *name;
    int (*make_hash_key)(fz_context *ctx, fz_store_hash *hash, void *key);
    void *(*keep_key)(fz_context *ctx, void *key);
    void (*drop_key)(fz_context *ctx, void *key);
    int (*cmp_key)(fz_context *ctx, void *a, void *b);
    void (*format_key)(fz_context *ctx, char *buf, size_t size, void *key);
    int (*needs_reap)(fz_context *ctx, void *key);
} fz_store_type;
```
（[include/mupdf/fitz/store.h:265-274](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/store.h#L265-L274)）

- `keep_key` / `drop_key`：对键做引用计数（键本身可能引用着 image 等对象，不能裸指针）。
- `cmp_key`：判断两个键是否相等，是「慢路径」线性查找的比较器。
- `make_hash_key`：把键压缩成一个 40 字节的 `fz_store_hash`，返回 0 表示「此键不可哈希」只能走慢路径。
- `needs_reap`：reap 流程用，4.3 讲。

**第二，「值」用 drop 函数指针当类型标签。** store 的查找接口 `fz_find_item` 第三参数是一个 `fz_store_drop_fn *drop`：

```c
void *fz_find_item(fz_context *ctx, fz_store_drop_fn *drop,
                   void *key, const fz_store_type *type);
```
（[include/mupdf/fitz/store.h:334](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/store.h#L334)）

因为每个可存储对象的 `fz_storable.drop` 指向**该类型专属的析构函数**（如 `fz_drop_pixmap_imp`），所以「值的 drop 指针」就成了「值的类型身份证」。查找时用 `item->val->drop == drop` 来确认「我找到的不仅是同键，还是同类型的值」，防止不同类型恰好共用一个键。store.h 里也明说了这一点：「Objects within the store are identified by type by comparing their drop_fn pointers」。

#### 4.2.2 核心流程

**存入 `fz_store_item`** 的流程（节选关键步骤）：

```c
/* 1. 分配 item 容器；构造哈希键 */
item = fz_malloc_no_throw(ctx, sizeof(fz_item));
if (type->make_hash_key)
    use_hash = type->make_hash_key(ctx, &hash, key);
type->keep_key(ctx, key);            /* store 接管一个对键的引用 */
fz_lock(ctx, FZ_LOCK_ALLOC);

/* 2. 若可哈希，插入哈希表（同时检测重复） */
if (use_hash) {
    existing = fz_hash_insert(ctx, store->hash, &hash, item);
    if (existing) { ... return existing->val; }  /* 已存在：不重复存 */
}

/* 3. 对值自增引用：现在 store 也持有这个值了 */
if (val->refs > 0) val->refs++;

/* 4. 非无限仓时，若超容则驱逐腾位（见 4.3） */
if (store->max != FZ_STORE_UNLIMITED) {
    size = store->size + itemsize;
    while (size > store->max) {
        ...
        saved = ensure_work(ctx, size - store->max);
    }
}
store->size += itemsize;
touch(store, item);                  /* 5. 插到链表头部（最新使用） */
```
（[source/fitz/store.c:439-577](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L439-L577)）

注意返回值的语义（store.h 第 300-308 行的注释）：**返回 NULL 表示成功插入；返回非 NULL 表示该键已有旧值，于是不插入新值，而是返回那个旧值**。这是一个「去重 + 让调用方统一使用缓存里的对象」的设计——多线程同时解码同一张图时，后到者发现自己的产物已被别人缓存，就丢弃自己的、改用缓存里的。image.c 里正是这么做的（4.2.3）。

**取出 `fz_find_item`** 的流程则展示「快慢两条路径」：

```c
if (type->make_hash_key) {
    hash.drop = drop;
    use_hash = type->make_hash_key(ctx, &hash, key);
}
fz_lock(ctx, FZ_LOCK_ALLOC);
if (use_hash)
    item = fz_hash_find(ctx, store->hash, &hash);     /* 快：O(1) */
else
    for (item = store->head; item; item = item->next) /* 慢：O(n) */
        if (item->val->drop == drop && !type->cmp_key(ctx, item->key, key))
            break;
if (item) {
    touch(store, item);               /* LRU：刚用过，移到表头 */
    if (item->val->refs > 0) item->val->refs++;   /* 调用方要持有一个引用 */
    return (void *)item->val;
}
```
（[source/fitz/store.c:579-633](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L579-L633)）

两个要点：
- 命中时**返回的是「已增引用」的指针**——store 注释明说「a reference has been taken」。因此调用方用完必须配对一个 `fz_drop_*`。这与 u2-l2 的引用计数铁律一致。
- `touch` 把命中的项移到链表头，维持 LRU 顺序。`touch` 的实现是标准的双向链表「摘下再头插」：

```c
static void touch(fz_store *store, fz_item *item)
{
    /* 已在表中则先摘下 */
    if (item->next != item) { /* 摘链 ... */ }
    /* 重新插到表头 */
    item->next = store->head;
    ...
    store->head = item;
}
```
（[source/fitz/store.c:414-437](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L414-L437)）

#### 4.2.3 源码精读：image tile 缓存（真实案例）

最能说明 store 用法的真实代码是图像分块缓存。`fz_image` 把自己解码出的 pixmap 分块存进 store，键是「图像 + 降采样级别 + 区域矩形」：

```c
typedef struct {
    int refs;
    fz_image *image;
    int l2factor;
    fz_irect rect;
} fz_image_key;
```
（[source/fitz/image.c:57-63](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/image.c#L57-L63)）

为这种键配套的虚表 `fz_image_store_type` 把六个回调一一填好：

```c
static const fz_store_type fz_image_store_type =
{
    "fz_image",
    fz_make_hash_image_key,
    fz_keep_image_key,
    fz_drop_image_key,
    fz_cmp_image_key,
    fz_format_image_key,
    fz_needs_reap_image_key
};
```
（[source/fitz/image.c:147-156](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/image.c#L147-L156)）

取 tile 时走标准的 `fz_find_item`，第三参数正是 pixmap 的析构函数 `fz_drop_pixmap_imp`（用 drop 指针当类型身份证）：

```c
tile = fz_find_item(ctx, fz_drop_pixmap_imp, key, &fz_image_store_type);
```
（[source/fitz/image.c:1004](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/image.c#L1004)）

存 tile 时则体现「返回非 NULL 表示已被别人缓存」的去重逻辑——若发现自己刚解码的 tile 已被另一个线程抢先缓存，就丢弃自己的、改用缓存里那个：

```c
existing_tile = fz_store_item(ctx, keyp, tile,
                              fz_pixmap_size(ctx, tile), &fz_image_store_type);
if (existing_tile) {
    /* 已有线程抢先缓存：丢弃自己的，用缓存里的 */
    fz_drop_pixmap(ctx, tile);
    tile = existing_tile;
}
```
（[source/fitz/image.c:1135-1143](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/image.c#L1135-L1143)）

> 小结这一节：store 的存取完全建立在「`fz_store_type` 虚表描述键」＋「drop 指针标识值类型」＋「返回值即引用计数语义」三件套上。任何新对象类型想进 store，只要实现这张虚表即可，store 本身一行都不用改。

#### 4.2.4 代码实践：阅读型——跟踪一次缓存命中

1. **实践目标**：亲手在源码里走一遍「图像解码产物如何被缓存命中」，理解 find/store 的配对与引用计数语义。
2. **操作步骤**：
   - 打开 [source/fitz/image.c:998-1016](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/image.c#L998-L1016) 的 `fz_find_image_tile`，确认它调用 `fz_find_item` 时第 3 个参数是 `fz_drop_pixmap_imp`。
   - 打开 [source/fitz/image.c:1125-1143](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/image.c#L1125-L1143)，看 `fz_store_item` 的返回值如何被判断。
   - 回到 [source/fitz/store.c:614-629](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L614-L629)，确认命中分支里 `item->val->refs++` 的存在。
3. **需要观察的现象**：`fz_find_item` 命中时对值做了 `refs++`，因此 image.c 拿到的 `tile` 必须在用完后被 `fz_drop_pixmap`。
4. **预期结果**：你应当能在 image.c 中找到 `tile` 的 `fz_drop_pixmap` 调用点（注意 `fz_always`/`fz_catch` 分支），证明「find 给出的引用」有对应的 drop 配对，没有泄漏。
5. 待本地验证：用 `mutool trace` 渲染一个含重复图像的 PDF，观察日志中同一 image 是否只解码一次（第二次走缓存命中）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `fz_find_item` 既要传 `key`，又要传 `drop` 函数指针？只传 key 不行吗？

**参考答案**：key 只描述「找什么」（哪张图的哪个分块），但 store 里同一键理论上可能被不同类型的值占用（实际上由类型保证不会，但 store 不信任这一点）。`drop` 指针是值的「类型身份证」，`item->val->drop == drop` 这一行额外校验了类型一致性，防止取回错误类型的对象。同时它也用于哈希键的构造（`hash.drop = drop`）。

**练习 2**：`fz_store_item` 返回 NULL 与返回非 NULL 分别意味着什么？为什么有这种设计？

**参考答案**：返回 NULL 表示成功插入；返回非 NULL 表示该键已存在同名旧值，于是放弃插入新值并返回那个旧值。这是「去重 + 多线程友好」的设计：多个线程同时解码同一对象时，后完成者发现自己的产物已被人缓存，就直接复用缓存值，丢弃自己的，避免重复占内存且保证所有调用方拿到的是同一个对象。

### 4.3 scavenging 回收

#### 4.3.1 概念说明

缓存若只进不出，内存迟早爆掉。store 的「出」有三条路径，按触发场景区分：

| 路径 | 触发时机 | 函数 | 目标 |
| --- | --- | --- | --- |
| 插入驱逐 | `fz_store_item` 发现 `size > max` | `ensure_work` | 为新项腾出刚好够的空间 |
| 分配回收 | `fz_malloc` 失败 | `fz_store_scavenge` → `scavenge` | 多阶段、尽可能多地腾内存 |
| 显式收缩 | 调用方主动调用 | `fz_shrink_store` | 把 store 缩到当前大小的某个百分比 |

还有一个特殊机制 **reap**，专门处理「键可存储对象」（`fz_key_storable`）的孤儿引用，放在最后讲。

**核心约束：只能驱逐 `refs == 1` 的项。** store 里每个项的值至少有「store 自己持有的那一个引用」（refs ≥ 1）。如果 `refs == 1`，说明除了 store 没有别人在用，可以安全丢弃；如果 `refs > 1`，说明外部（某段正在进行的渲染）还引用着，强删会导致野指针。所有驱逐代码都以 `item->val->refs == 1` 作为「可驱逐」判据。

#### 4.3.2 核心流程：插入驱逐 ensure_work

当 `fz_store_item` 发现加进新项后会超容，它调用 `ensure_work(ctx, size - store->max)`——「确保能腾出这么多字节」。算法是两遍扫描（store.c 顶部有详细注释说明为何如此）：

```c
static size_t ensure_work(fz_context *ctx, size_t tofree)
{
    /* 第一遍：从表尾(LRU端)向前，统计「可驱逐项(refs==1)」的累计字节，
       确认总量够不够腾出 tofree；不够直接返回 0，不存这项。 */
    count = 0;
    for (item = store->tail; item; item = item->prev)
        if (item->val->refs == 1) {
            count += item->size;
            if (count >= tofree) break;
        }
    if (item == NULL) return 0;   /* 永远腾不够：放弃缓存此项 */

    /* 第二遍：真正摘下并累积到待释放链，达到 tofree 即停 */
    for (item = store->tail; item; item = prev) {
        ...
        store->size -= item->size;
        /* 摘链 + 从哈希表移除 + 累积到 to_be_freed */
        count += item->size;
        if (count >= tofree) break;
    }
    /* 解锁后再真正释放（drop 可能很重，不能持锁） */
    while (to_be_freed) { ... fz_unlock ... item->val->drop(...) ... fz_lock ... }
    return count;
}
```
（[source/fitz/store.c:318-412](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L318-L412)）

第一遍是**可行性预检**：如果即便把所有可驱逐项都丢了也凑不够，那就干脆**不缓存**这个新项（返回 0，外层 `fz_store_item` 据此 `break` 不改 `store->size`）。注释明确解释了这一取舍——既然内存已经花掉去 malloc 了，宁可不进 store 也不要让一个被多次使用的资源反复重新 malloc。

一个贯穿全模块的实现技巧：**先在锁内摘链、累积到一个待释放链，解锁后再真正调用 `drop` 析构**。因为 `drop` 可能触发连锁释放甚至异常，绝不能在持有 `FZ_LOCK_ALLOC` 时执行，否则会死锁或破坏异常机制。`ensure_work`、`do_reap`、`fz_filter_store` 都遵循这个「摘链 + 延迟释放」模式。

#### 4.3.3 核心流程：分配回收 scavenge

当 `fz_malloc` 失败，MuPDF 不会立刻放弃，而是先向 store「讨要」内存。这是 [source/fitz/memory.c:44-68](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L44-L68) 的 `do_scavenging_malloc`：

```c
do {
    p = ctx->alloc.malloc(ctx->alloc.user, size);
    if (p != NULL) { fz_unlock(...); return p; }
}
while (fz_store_scavenge(ctx, size, &phase));   /* 失败就让 store 吐一点再重试 */
return NULL;   /* store 也吐干净了，才真正返回 NULL（fz_malloc 随后抛异常） */
```

注意 `scavenging` 标志位防止递归：scavenge 过程中如果触发的 `drop` 又去 malloc、又失败、又调 scavenge，会被 `if (store->scavenging) return 0;` 直接挡掉（[source/fitz/store.c:824-827](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L824-L827)）。

`fz_store_scavenge` 是**分 16 个阶段**逐步加压的回收器。每阶段它把「允许的 store 上限」调低一档，再让内部的 `scavenge` 实际去驱逐：

```c
do {
    /* 第 *phase 阶段允许的 store 上限 */
    if (*phase >= 16)
        max = 0;
    else if (store->max != FZ_STORE_UNLIMITED)
        max = store->max / 16 * (16 - *phase);   /* 逐阶段线性降到 0 */
    else
        max = store->size / (16 - *phase) * (15 - *phase);
    (*phase)++;
    tofree = size + store->size - max;           /* 需要腾出的字节数 */
    if (scavenge(ctx, tofree))
        return 1;                                /* 腾出来了，malloc 可重试 */
} while (max > 0);
```
（[source/fitz/store.c:937-968](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L937-L968)）

设 `M = store->max`，则第 \(k\) 阶段允许的上限为：

\[
\text{max}_k \;=\; \frac{M}{16}\,(16 - k), \qquad k = 0,1,\dots,16
\]

即从满额 \(M\) 一路线性降到 0。早期阶段压力小（只丢一点点），后期阶段压力大（几乎清空）。这种「逐步加压」的设计避免了在内存紧张时一次性清空整个 store 造成渲染性能骤降——先丢最不痛的，腾够了就停。

真正干活的 `scavenge(ctx, tofree)` 内部策略很精巧（[source/fitz/store.c:817-868](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L817-L868)，文件 794-816 行有详尽注释）：它从表尾向前扫描可驱逐项，**累计够 tofree 字节后，只挑其中最大的那一个驱逐**，然后重新开始扫描（因为 `evict` 会临时解锁，链表可能变了）。这种 \(O(n^2)\) 的「贪心挑最大」策略，目的是用**最少的驱逐次数**凑够空间，避免像朴素 LRU 那样连续丢掉一堆小块。

```c
for (item = store->tail; item; item = item->prev) {
    if (item->val->refs == 1 &&
        (item->val->droppable == NULL || item->val->droppable(ctx, item->val))) {
        suffix_size += item->size;
        if (largest == NULL || item->size > largest->size)
            largest = item;                 /* 记下当前最大可驱逐块 */
        if (suffix_size >= tofree - freed) break;
    }
}
if (largest == NULL) break;                 /* 没有可驱逐块了 */
freed += largest->size;
evict(ctx, largest);                        /* 只驱逐最大的一个，临时解锁 */
```

注意这里比 `ensure_work` 多了一个条件：`droppable` 回调。某些对象即使在 `refs == 1` 时也不能立刻删（例如还映射着外部资源），对象可在初始化时通过 `FZ_INIT_AWKWARD_STORABLE` 提供 `droppable` 回调返回 0 来拒绝被驱逐（[include/mupdf/fitz/store.h:101-104](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/store.h#L101-L104)）。

#### 4.3.4 核心流程：reap 与 key storable

最后一个回收机制 `reap` 处理一种微妙的引用环。先看为什么需要它——回到 store.h 的说明（第 182-216 行）：

PDF 里每个 `fz_image` 本身被存进 store（作为值），但渲染它产生的 pixmap 也存进 store，而这个 pixmap 的**键**里就包含这个 `fz_image`。于是 image 同时扮演「某项的值」和「另一项的键的一部分」两种角色。这种「既能当值、又能进键」的对象叫 **key storable**，结构上比普通 storable 多一个 `store_key_refs` 计数：

```c
typedef struct {
    fz_storable storable;
    short store_key_refs;
} fz_key_storable;
```
（[include/mupdf/fitz/store.h:87-91](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/store.h#L87-L91)）

问题来了：当我们关闭文档、drop 掉所有 image 时，image 的 `refs` 可能仍不为零——因为它的引用**全部来自 store 里那些 pixmap 的键**（`store_key_refs == refs`）。这些 pixmap 已成「孤儿」（没有人再需要它们的源 image），却因为互相引用赖在 store 里。`reap` 就是来清理这种孤儿的。

判定逻辑极简：当一个 key storable 的 `store_key_refs == refs`，说明「剩下的引用全是 store 内部键的引用，没有任何外部用户需要它了」，于是它以及所有以它为键的项都该被收割：

```c
static int fz_key_storable_needs_reaping(fz_context *ctx, const fz_key_storable *ks)
{
    return ks == NULL ? 0 : (ks->store_key_refs == ks->storable.refs);
}
```
（[source/fitz/image.c:34-38](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/image.c#L34-L38)）

收割在 `fz_drop_key_storable` 里被触发：当一次 drop 让 `refs` 降到恰好等于 `store_key_refs` 时，立刻（或延迟）发起一次 `do_reap`：

```c
drop = --s->storable.refs == 0;
if (!drop && s->storable.refs == s->store_key_refs) {
    if (ctx->store->defer_reap_count > 0)
        ctx->store->needs_reaping = 1;     /* 延迟：只置标志 */
    else
        do_reap(ctx);                       /* 立即扫一遍 store 收割孤儿 */
}
```
（[source/fitz/store.c:199-214](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L199-L214)）

`do_reap` 遍历整条 store，对每一项调用其 `type->needs_reap`，凡返回真的项就摘链释放（[source/fitz/store.c:110-184](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L110-L184)）。因为 reap 要「触碰 store 里每一项」，开销大，所以提供了 `fz_defer_reap_start` / `fz_defer_reap_end` 来把一段区域内可能触发的多次 reap **批处理**成一次（[source/fitz/store.c:1084-1108](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L1084-L1108)）：start 把 `defer_reap_count` 加 1，期间所有 reap 只置 `needs_reaping` 标志；end 把计数减 1，归零时若 `needs_reaping` 为真才真正执行一次 reap。

#### 4.3.5 代码实践：FZ_STORE_UNLIMITED vs 16 MiB

这是本讲的主实践，直接对应学习目标中的「缓存命中影响」。

1. **实践目标**：通过对比「无限仓」与「小仓」下重复渲染同一页的耗时，亲手验证 store 命中对性能的作用，并理解 scavenge 的触发。
2. **操作步骤**（说明：mudraw 命令行**没有**「设任意 store 字节数」的开关——它的 store 上限要么是默认的 `FZ_STORE_DEFAULT`，要么由 `-L` 一键压到极小。因此本实践分「命令行快速对比」与「C 程序精确对比」两条路径）：
   - 先确认已按 u1-l2 编译出 `mutool`（如 `make tools`，产物在 `build/debug/mutool`）。
   - 准备一个含较多图像或复杂矢量的多页 PDF（如 `docs/examples` 自带样本，或任意带图片的 PDF）。
   - **路径一·命令行快速对比**：用 mudraw 的 `-L`（lowmemory）开关对比。`-L` 做三件事：把 `max_store` 设为 1、给 device 打上 `FZ_NO_CACHE` 提示、并在页间 `fz_empty_store`（见 [source/tools/mudraw.c:2247](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2247) 与 [source/tools/mudraw.c:2346-2347](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2346-L2347)）。分别跑：
     ```
     ./build/debug/mutool draw -o /tmp/normal_%d.pam input.pdf      # 默认 256MiB 仓
     ./build/debug/mutool draw -L -o /tmp/lowmem_%d.pam input.pdf   # 仓被压到 1 + 关缓存
     ```
     对比两者渲染多页的总耗时与内存峰值。
   - **路径二·C 程序精确对比（推荐，能精确设 16 MiB）**：参照 [docs/examples/example.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c)，分别用：
     - `fz_new_context(NULL, NULL, FZ_STORE_UNLIMITED)`（无限仓）
     - `fz_new_context(NULL, NULL, 16<<20)`（16 MiB 仓）

     在 `fz_try` 内对一个含图像的页面，循环调用 `fz_new_pixmap_from_page_number` 渲染 20 次，用 `clock()` 或 `times()` 记录每次耗时，最后 `fz_drop_pixmap` 释放。两次程序分别编译运行。
3. **需要观察的现象**：
   - 无限仓下，第 2 次及之后的渲染应明显快于第 1 次——字体、图像解码产物都命中了 store。
   - 16 MiB 仓下，若该页解码产物总量超过 16 MiB，store 会在插入时频繁触发 `ensure_work` 驱逐（[source/fitz/store.c:535-566](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L535-L566)），后续渲染的命中率下降、耗时回升。
   - `-L` 路径下，由于 `FZ_NO_CACHE`，draw device 不会把图像分块写进 store，第 2 次渲染与第 1 次几乎一样慢，可与路径二的 16 MiB 曲线对照。
   - 若在路径二里故意用一个超小上限（如 `1<<20` 即 1 MiB）渲染超大页面，渲染中途某次 `fz_malloc` 失败会触发 `fz_store_scavenge`（见 4.3.3），可观察到额外的回收开销。
4. **预期结果**：重复渲染耗时随 store 上限增大而下降，存在一个拐点（约等于「单页解码产物总量」），超过拐点后耗时基本不再改善。
5. 待本地验证：具体耗时数字依赖文档内容与机器，本讲无法给出确切毫秒数，请实测记录你的「首渲染 / 二次渲染」耗时比。

#### 4.3.6 小练习与答案

**练习 1**：`ensure_work` 为什么要做「两遍扫描」，而不是一遍扫到够就直接驱逐？

**参考答案**：第一遍是可行性预检——如果所有可驱逐项（`refs == 1`）加起来都不够 `tofree`，就干脆不缓存这个新项（返回 0），而不是驱逐了一堆却仍放不下。第二遍才真正摘链释放。此外，真正释放要在解锁后进行（`drop` 不能持锁），所以必须先把要删的项摘出链表、累积好，再统一释放，两遍扫描配合「摘链 + 延迟释放」模式实现。

**练习 2**：`fz_store_scavenge` 为什么分 16 个阶段，而不是一次把 store 清空？

**参考答案**：分阶段是「逐步加压」——早期阶段只把允许的上限调低一点，丢少量对象就够 malloc 成功；只有早期不够时才进入更激进阶段。这样在内存只是略微紧张时，能尽量保留缓存、维持渲染性能，避免「一次 OOM 就清空整个 store」导致后续渲染性能雪崩。

**练习 3**：`scavenge` 内部为什么用 \(O(n^2)\) 的「反复扫描挑最大块」，而不是朴素 LRU 的「从尾向前丢到够为止」？

**参考答案**：因为 `evict` 必须临时解锁去执行 `drop`（析构可能很重或抛异常），解锁期间链表可能被其他线程改动，无法安全地「连续删多个」。于是采取每次只删一个最大块、删完重新扫描的策略。文件 794-816 行的注释举例：要释放 97 字节、尾部依次是 32/64/128/256 的块，朴素 LRU 会丢 32+64（=96 不够）再丢 128，共丢 3 块；而挑最大块只需丢 1 个 128 块就够。用扫描次数换驱逐次数最少，对缓存命中率更友好。

## 5. 综合实践

把本讲三个模块串起来，完成一次「store 行为观察」小任务：

1. 编写一个最小 C 程序（基于 docs/examples/example.c 改造），用 `fz_new_context(NULL, NULL, FZ_STORE_DEFAULT)` 创建 context，打开一个含图片的 PDF。
2. 在渲染前后，调用 `fz_debug_store(ctx, fz_stdout(ctx))`（[include/mupdf/fitz/store.h:420](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/store.h#L420)）打印 store 当前内容，它会在末尾输出 `max=…, size=…, actual size=…` 一行（[source/fitz/store.c:783](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L783)）。
3. 渲染第 1 页一次，打印 store；再渲染第 1 页一次，打印 store。观察：
   - 第二次渲染后 store 的 `size` 几乎不变（命中，没有新增解码产物），印证 4.2 的存取语义。
   - 把上限改成 `4<<20`（4 MiB）重跑，观察 `size` 是否逼近 `max`、以及是否有项被驱逐（链表项数减少）。
4. 写一段简短结论：用本讲术语解释你看到的「size 变化」与「重复渲染耗时变化」之间的因果关系。

如果无法编译运行，请把第 3 步改为「源码阅读型」：在 [source/fitz/store.c:570-573](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c#L570-L573) 处确认 `store->size += itemsize` 与 `touch` 的顺序，解释为何重复渲染同页时 `size` 不会持续增长。

## 6. 本讲小结

- `fz_store` 是挂在一个 context 家族上的对象缓存，内部是 **LRU 双向链表 + 4096 桶哈希表 + 大小计数**，全部由 `FZ_LOCK_ALLOC` 一把锁保护，因此天然支持多线程共享。
- 上限由 `fz_new_context` 第三参数决定：`FZ_STORE_DEFAULT`（256 MiB）或 `FZ_STORE_UNLIMITED`（0，永不驱逐）。
- 存取完全建立在「`fz_store_type` 虚表描述键操作」＋「drop 函数指针当值类型身份证」＋「返回值即已增引用」三件套上；`fz_find_item` 命中返回的指针必须配对一个 drop。
- 驱逐只能针对 `refs == 1` 的项（只被 store 自己引用）；`ensure_work`（插入驱逐，两遍扫描 + 摘链延迟释放）和 `scavenge`（分配回收，16 阶段逐步加压、挑最大块驱逐）是两条主要回收路径。
- `fz_malloc` 失败会先 `fz_store_scavenge` 向 store 讨内存再重试，由 `scavenging` 标志防递归。
- `reap` 机制专治 key storable 的「孤儿引用」：当某对象 `store_key_refs == refs`（只剩 store 内部键引用它）时，连带清除所有以它为键的项；`fz_defer_reap_start/end` 可把多次 reap 批处理成一次。

## 7. 下一步学习建议

- 本讲建立的「共享 store + 单锁」模型是多线程渲染的基础。下一讲 **u9-l2 多线程渲染** 会讲 `fz_clone_context` 如何让多个工作线程共享同一份 store、各自维护异常栈，并用 `FZ_LOCK_*` 锁函数串行化对 store 的竞争访问。
- 若想看更多 store 的真实使用者，可阅读 [source/fitz/colorspace.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/colorspace.c)（颜色空间转换缓存）与 [source/pdf/pdf-store.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-store.c)（PDF 对象缓存），它们都遵循「定义 `fz_store_type` 虚表 → find/store 配对」的同一范式。
- 想理解 store 的调试输出，可在编译时定义 `ENABLE_STORE_LOGGING` 或 `DEBUG_SCAVENGING` 宏，观察 `[source/fitz/store.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/store.c)` 中被这些宏包裹的日志点。
