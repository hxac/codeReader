# block_pool 分层空闲链表 + $new/$delete + 容器分配器

## 1. 本讲目标

本讲是内存子系统（U7）的收口篇。在 u7-l1 我们建立了「资源接口 → `new_delete_resource` → 类型擦除 `bytes_allocator`」的三层骨架，在 u7-l2 我们看到中央缓存 `central_cache_allocator()` 用一块 1MB 静态缓冲 + `monotonic_buffer_resource` 服务「central cache 的元数据」。但那 1MB 到底**装了什么**、**谁在调用它**、最终用户 `mem::$new` 又是怎么一路路由到那块 1MB 的——这些问题 u7-l2 留给了本讲。

学完本讲你应当能够：

1. 说出 `block_pool` 的固定块空闲链表（free list）如何用 `union` 复用「空闲指针 / 占用存储」。
2. 解释 `central_cache_pool` 的「两级缓存」：线程局部 `block_pool`（L1）+ 进程级锁无关栈（L2），以及它如何向 1MB monotonic 批量申请 chunk。
3. 读懂 `mem::$new` / `mem::$delete` 的「类型擦除析构器」存储：在对象前预留 16 字节头部，存一个回收函数指针，让 `$delete` 不必知道对象的真实类型。
4. 画出一次 `mem::$new<…>` 的完整内存路由路径，并按 `get_regular_resource` 的分级表（16B…64KB）判断它落到哪个池。
5. 理解 `container_allocator` 与 `ipc::map` / `ipc::unordered_map` 如何让标准容器复用这套分配器。

---

## 2. 前置知识

本讲默认你已经读过 u7-l1（分配器架构与类型擦除）和 u7-l2（monotonic_buffer_resource 与 1MB 中央缓存）。下面三个直觉先建立起来：

### 直觉一：固定块池 = 把「任意大小的 malloc」变成「只发同一种尺寸的票」

通用 `malloc` 慢，是因为它要处理任意大小、还要合并碎片。如果我们**只分配一种固定大小**（比如恰好 64 字节）的块，就可以用一个**单向链表**（free list）管理所有空闲块：

- 分配：从链表头摘一个 → O(1)。
- 回收：把块挂回链表头 → O(1)。
- 没有空闲块时：一次性向底层要一大批（一个 chunk），再慢慢发。

这就是 **fixed-size block pool（固定块池）**。libipc 把它做成分级的：为 16B、32B、…、64KB 各准备一个池，按请求大小四舍五入到最近的池。

### 直觉二：空闲块用什么存「下一个」指针？——union 复用

一个块要么「空闲」（需要存指向下一个空闲块的指针），要么「占用」（存用户数据）。两者**不会同时发生**，所以可以用 `union` 把它们重叠在同一块内存上：

```c++
union block {
  block *next;                       // 空闲时：指向下一个空闲块
  alignas(std::max_align_t)
    std::array<byte, BlockSize> storage;  // 占用时：用户数据
};
```

空闲时读 `next`，交给用户时整块 `storage` 都给他。零额外开销。这是本讲反复出现的核心数据结构。

### 直觉三：类型擦除的析构 = 「回收说明书」贴在包裹外面

`delete p` 需要知道 `p` 的静态类型才能调用正确的析构函数。但 libipc 想做一个**不依赖类型**的释放接口 `mem::$delete(void* p)`。办法是：在 `$new` 的时候，**在对象前面贴一张「回收说明书」**——一个函数指针，这个函数在 `$new` 时由编译器按真实类型生成好，里面写死了「怎么析构、怎么释放」。`$delete` 只要把这张说明书取出来调用即可，自己完全不需要知道类型。这就是「类型擦除析构器」。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的部分 |
|---|---|---|
| `include/libipc/mem/new.h` | 对外暴露 `mem::$new` / `mem::$delete` / `mem::alloc` / `mem::free`，以及类型擦除析构器的头部布局 | `$new`/`$delete`/`deleter`/`detail_new` |
| `src/libipc/mem/new.cpp` | `get_regular_resource` 的分级路由表、`block_pool_resource` 的 thread_local 实例 | 分级 switch、`alloc`/`free` |
| `include/libipc/mem/block_pool.h` | 固定块空闲链表 `block_pool`（线程局部 L1） | `allocate`/`deallocate`/`expand` |
| `include/libipc/mem/central_cache_pool.h` | 进程级锁无关栈 `central_cache_pool`（L2），向 1MB monotonic 批量申请 chunk | `aqueire`/`release`/`instance` |
| `include/libipc/concur/intrusive_stack.h` | L2 用的无锁 CAS 栈（u8-l3 详讲，本讲只用其接口） | `push`/`pop` |
| `include/libipc/mem/central_cache_allocator.h` / `.cpp` | 取得那个 1MB monotonic 包装的 `bytes_allocator`（u7-l2 详讲） | `central_cache_allocator()` |
| `include/libipc/mem/container_allocator.h` | 让标准容器用的分配器适配器 | `allocate`/`deallocate`/`construct` |
| `src/libipc/mem/resource.h` | `ipc::map` / `ipc::unordered_map` 别名 | 两个 `using` |

**调用层次全景**（从上到下，越往下越接近原始内存）：

```
mem::$new<T>(args)  /  mem::alloc(bytes)          ← 用户接口 (new.h)
        │
        ▼
get_regular_resource(bytes)  →  选一个 block_collector (new.cpp)
        │   （按 16B…64KB 分级，>64K 走 new/delete）
        ▼
block_pool_resource<BlockSize,Expansion>::get()   ← thread_local 实例 (new.cpp)
        │
        ▼
block_pool<BlockSize,Expansion>                    ← L1：线程局部空闲链表 (block_pool.h)
        │   空了就 expand()
        ▼
central_cache_pool<block<BlockSize>,Expansion>::instance()  ← L2：进程级锁无关栈 (central_cache_pool.h)
        │   两栈都空就向 1MB 要 chunk
        ▼
central_cache_allocator().construct<chunk_t>()     ← 1MB monotonic + mutex (u7-l2)
        │
        ▼
（溢出时）new_delete_resource → 系统 malloc        ← u7-l1
```

本讲要逐层打开这条链路。

---

## 4. 核心概念与源码讲解

### 4.1 block_pool：线程局部的固定块空闲链表（L1）

#### 4.1.1 概念说明

`block_pool` 是**每个线程独享**的一份固定块空闲链表。它的核心价值有两点：

1. **固定块，O(1) 分配/回收**：只发同一种尺寸的块，分配就是摘链表头，回收就是挂链表头，没有任何大小计算或碎片合并。
2. **线程局部（thread-local），无锁**：每个线程一份，互不干扰，连原子操作都不需要——这就是它比 L2（进程级、要用 CAS）更快的原因。

`block_pool` 是一个模板 `block_pool<BlockSize, BlockPoolExpansion>`：

- `BlockSize`：每块的固定字节数（如 64、4096）。
- `BlockPoolExpansion`：本池耗尽时，一次性向 L2 批量申请多少块（一个 chunk 的大小）。

#### 4.1.2 核心流程

`block_pool` 内部只维护一个游标 `cursor_`，它指向一条**单向空闲链表的头**（链表节点就是 `block` 本身，靠 `block::next` 串起来）。

```text
allocate()：                       deallocate(p)：
  if cursor_ == nullptr:             b = (block*)p
    cursor_ = expand()  ← 向 L2要    b->next = cursor_
  p = cursor_                        cursor_ = b          // 挂回头
  cursor_ = cursor_->next  // 摘头
  return p->storage.data()
```

构造时立刻 `expand()` 一次，往 L2 预热一批块；析构时把整条链表 `release()` 还给 L2。链表的链接复用了 `union block`：块空闲时存 `next`，占用时存 `storage`，二者共享同一片内存。

#### 4.1.3 源码精读

**`block` 联合体**（空闲指针与用户存储复用同一片内存）：

[block_pool.h 之前我们先看 central_cache_pool.h 里的 block 定义](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/central_cache_pool.h#L24-L28)（`block_pool` 通过 `block<BlockSize>` 复用它）：

```c++
template <std::size_t BlockSize>
union block {
  block *next;                                              // 空闲时：下一个空闲块
  alignas(std::max_align_t) std::array<byte, BlockSize> storage; // 占用时：用户数据
};
```

`alignas(std::max_align_t)` 保证每块都按最大对齐对齐，用户塞任何类型都不会踩对齐坑。

**主模板 `block_pool<BlockSize, BlockPoolExpansion>`** 的成员与扩张：

[include/libipc/mem/block_pool.h:66-77](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/block_pool.h#L66-L77) 定义了块类型 `block<BlockSize>`、中央池类型，以及耗尽时调用的 `expand()`（把任务委托给 L2 的 `central_cache_pool::instance().aqueire()`，注意源码里方法名拼写是 `aqueire`）。

构造即预热、析构即归还：

[include/libipc/mem/block_pool.h:80-85](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/block_pool.h#L80-L85) —— 构造函数 `cursor_(expand())` 一上来就向 L2 拿一批；析构函数把剩余链表 `release(cursor_)` 还给 L2。注意它是 move-only（删了拷贝，[L87-L92](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/block_pool.h#L87-L92)）。

**`allocate` / `deallocate`** 的链表摘头/挂头：

[include/libipc/mem/block_pool.h:99-114](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/block_pool.h#L99-L114) —— `allocate` 先判 `cursor_==nullptr`，空了就 `expand()` 补货，再摘头返回 `storage.data()`；`deallocate` 把回收的块挂到链表头。全程只动一个指针，无锁、无原子。

> **旁注：`block_pool<0, 0>`（「只收不发」的通用池）**
> 源码还有一个特化 [block_pool.h:26-63](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/block_pool.h#L26-L63)，块类型退化为只含一个 `next` 指针的 `struct block_t { block_t *next; }`。它的注释明确说：「只能用于回收**大小未知但一致**的一组块，**不能用于分配**」。对应地，它用的 `central_cache_pool<block_t, 0>` 在 `aqueire` 时**从不分配 chunk**（见 4.2.3）。它是一个对称的「只出不进」回收池，主分配链路（4.3）并不直接使用它，但它的存在让 `block_pool` 家族语义完整。

#### 4.1.4 代码实践

**实践目标**：验证 `block_pool` 的链表是 LIFO（后进先出），且 `union` 复用确实零开销。

**操作步骤**（示例代码，非项目原有代码，仅作理解用；项目内部并不直接暴露 `block_pool` 给用户，而是经由 `mem::alloc`）：

```c++
#include "libipc/mem/block_pool.h"
using namespace ipc::mem;

// 示例代码：直接用一个 64 字节块池
block_pool<64, 8> pool;          // 构造时已向 central cache 预热了一批
void *a = pool.allocate();       // 摘链表头
void *b = pool.allocate();       // 再摘一个
pool.deallocate(a);              // a 挂回链表头
void *c = pool.allocate();       // LIFO：c 应等于 a
```

**需要观察的现象**：因为 `deallocate` 把块挂到链表头、`allocate` 从链表头摘，所以 `c` 与 `a` 指向同一地址（LIFO）。另外块大小恒为 64，`sizeof(block<64>)` 也约等于 64（受 `max_align_t` 对齐影响），证明 `next` 没有额外占用存储。

**预期结果**：`c == a` 成立；池子从 `central_cache_pool` 批量拿块，连续多次 `allocate` 大概率落在同一 chunk 的连续地址上。**待本地验证**（`block_pool` 是内部头，需把本文件加入编译）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `block_pool` 不需要任何锁或原子操作就能线程安全？
**答案**：因为 `block_pool` 的实例是 **thread-local**（见 4.3 的 `block_pool_resource::get()` 返回 `thread_local` 实例）。每个线程独占自己的 `cursor_` 链表，天然无竞争，连原子开销都省了。

**练习 2**：`allocate` 里为什么要先判断 `cursor_ == nullptr` 再 `expand()`，而不是构造时一次性拿够？
**答案**：惰性 + 批量的折中。构造时拿第一批（`expand()` 一次），用完了才再拿。这样既不会一开始就占太多内存，又因为每次 `expand()` 拿一整批（`BlockPoolExpansion` 块）而摊还了向 L2 申请的成本。

---

### 4.2 central_cache_pool：进程级锁无关栈与 chunk 批量分配（L2）

#### 4.2.1 概念说明

`central_cache_pool` 是**全进程共享**的单例（`instance()`），是 `block_pool`（L1）的「后勤仓库」。它解决一个矛盾：L1 是线程局部的，但块最终要能跨线程流转回收（A 线程释放的块，B 线程也能用）。于是引入 L2 作为所有线程共同的「中央仓库」：

- **L1（block_pool，线程局部）**：本线程的私房钱，零开销，但只在本线程流转。
- **L2（central_cache_pool，进程级）**：公共仓库，跨线程流转，但进出要用**无锁 CAS 栈**保证安全。

L2 自己不生产原始内存，它向更底层（u7-l2 的 1MB monotonic）**批量申请 chunk**（一整组块），再分发给各线程的 L1。

#### 4.2.2 核心流程

`central_cache_pool` 内部有**两条无锁栈**（`concur::intrusive_stack`，u8-l3 详讲，本质是 Treiber 栈）：

- `cached_`：**可用块**栈，存放「已经回收、等待再分发」的块（实为块链表头）。
- `aqueired_`：**在用节点**栈，记录「分发出去但节点尚未归还」的记账节点，供 `release` 复用。

```text
aqueire()：                                   release(p)：
  n = cached_.pop()                             a = aqueired_.pop()        // 复用一个记账节点
  if n != nullptr:                              if a == nullptr:
    aqueired_.push(n)    // 记账                  a = construct<node_t>()   // 或新建一个节点
    return n->value      // 返回一个块            a->value = p
  // cached 空：向 1MB 批量要一个 chunk          cached_.push(a)            // 块归还可用栈
  chunk = central_cache_allocator().construct<chunk_t>()
  把 chunk 里 Expansion 个块用 next 串成链表
  return chunk->data()   // 返回整条链表的头
```

**关键巧思**：栈里存的「值」是 `block_t*`（一个块指针），而这个块本身又是**一条链表的头**（块的 `next` 串着后续块）。也就是说，一次 `aqueire` 可能返回**一整串块**（新鲜 chunk 路径），也可能返回**一个块**（回收再分发路径），由调用方 `block_pool::expand` 统一当作「链表头」塞进 `cursor_`。块在「空闲」与「占用」之间切换时，`union block` 的 `next` / `storage` 自动复用同一片内存。

#### 4.2.3 源码精读

**主模板 `central_cache_pool<BlockT, BlockPoolExpansion>`**：

[include/libipc/mem/central_cache_pool.h:34-46](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/central_cache_pool.h#L34-L46) 定义了三种类型：`block_t`（块）、`chunk_t = std::array<block_t, BlockPoolExpansion>`（一批块）、`node_t`（无锁栈的记账节点），以及两条栈 `cached_` / `aqueired_`。

**`aqueire`**：先从 `cached_` 回收栈里捞，捞不到就向 1MB monotonic 批量要一个 chunk 并串成链表：

[include/libipc/mem/central_cache_pool.h:51-66](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/central_cache_pool.h#L51-L66) —— `cached_.pop()` 命中时把记账节点转移到 `aqueired_` 并返回一个块；未命中时 `central_cache_allocator().construct<chunk_t>()`（即向 u7-l2 那块 1MB 缓冲申请一个 `array<block_t, Expansion>`），随后用一个 `for` 循环把 `Expansion` 个块用 `next` 串成单向链表，返回链表头 `chunk->data()`。

**`release`**：复用记账节点，把块挂回可用栈：

[include/libipc/mem/central_cache_pool.h:68-77](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/central_cache_pool.h#L68-L77) —— 先从 `aqueired_` 弹一个记账节点（没有就向 1MB 新建一个 `node_t`），把它包住要归还的块指针 `p`，再压回 `cached_`。这样记账节点总数被「峰值并发 `aqueire` 数」封顶，不会无限增长。

**单例**：`static central_cache_pool pool;` 是进程级唯一实例：

[include/libipc/mem/central_cache_pool.h:80-83](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/central_cache_pool.h#L80-L83) —— Meyers 单例，C++11 起静态局部变量初始化是线程安全的。

**`<BlockT, 0>` 特化（只缓冲、不分配）**：

[include/libipc/mem/central_cache_pool.h:102-111](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/central_cache_pool.h#L102-L111) —— 当 `BlockPoolExpansion == 0`（即 `block_pool<0,0>` 通用池所用的配置）时，`aqueire` 在 `cached_` 空时直接返回 `nullptr`，**永不申请 chunk**。注释点明：「对于没有默认扩张尺寸的池，中央缓存只做缓冲，不做分配。」这对应 4.1.3 旁注里那个「只收不发」的池。

**底层无锁栈**（`intrusive_stack`，u8-l3 详讲）：

[include/libipc/concur/intrusive_stack.h:44-62](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/concur/intrusive_stack.h#L44-L62) —— `push`/`pop` 都用 `compare_exchange_weak`（`release`/`acquire` 序）做 Treiber 栈。`pop` 遇到 `top_ == nullptr` 直接返回 `nullptr`，这正是 `central_cache_pool` 判「栈空」的依据。本讲只需把它当作「线程安全的 LIFO 栈」使用。

**底层 1MB monotonic**（u7-l2 已详讲，这里只点一句）：

[src/libipc/mem/central_cache_allocator.cpp:39-44](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/central_cache_allocator.cpp#L39-L44) —— `central_cache_allocator()` 返回一个包着 `thread_safe_resource`（1MB 静态数组 + `monotonic_buffer_resource` + `std::mutex`）的 `bytes_allocator`。`central_cache_pool` 的 `construct<chunk_t>()` 最终就从这块 1MB 里 bump 分配元数据。

#### 4.2.4 代码实践

**实践目标**：理解 `aqueire` 的两条返回路径（回收再分发 vs 新鲜 chunk），以及「栈里存的是链表头」这一巧思。

**操作步骤**（源码阅读型实践）：

1. 打开 [central_cache_pool.h:51-66](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/central_cache_pool.h#L51-L66)。
2. 假设进程刚启动、`cached_` 为空：第一次 `aqueire()` 走哪条路径？返回的是「一个块」还是「一串块」？
3. 假设某线程 `release` 了一个块后再 `aqueire`：这次走哪条路径？返回的块与刚释放的有什么关系？

**需要观察的现象 / 预期结果**：

- 首次：走 `construct<chunk_t>()` 路径，返回**一串**（`BlockPoolExpansion` 个）块链表的头。
- 回收后再取：走 `cached_.pop()` 路径，返回**一个**块（正是刚释放那个，因为 LIFO）。
- 两条路径返回值都被 `block_pool::expand` 当作「链表头」塞进 `cursor_`，`allocate` 照常摘头——调用方完全不需要区分两种来源。

**待本地验证**：可在 `aqueire` 入口加一行日志，区分「命中 cached_」与「新申请 chunk」两种情况，观察程序启动初期的 chunk 申请次数。

#### 4.2.5 小练习与答案

**练习 1**：`central_cache_pool` 为什么用**两条**栈（`cached_` 和 `aqueired_`），而不是一条？
**答案**：职责分离 + 节点复用。`cached_` 是「可用块」栈（供 `aqueire` 分发）；`aqueired_` 是「记账节点」栈（`aqueire` 时把节点从 `cached_` 转移过来暂存，`release` 时复用它包新块）。这样 `release` 不必每次都新建 `node_t`，记账节点总数被并发峰值封顶。

**练习 2**：为什么说「栈里存的一个值，其实是一整条块链表」不会丢块？
**答案**：块链表靠 `block::next` 串联，这个 `next` 存在块自己的内存里（`union` 复用），不在栈节点里。栈节点的 `value` 只指向链表头。`aqueire` 把链表头交给 `block_pool::expand`，`block_pool::allocate` 摘头后 `cursor_ = cursor_->next` 自然能走到链表后续块——链表结构自始至终完整。

---

### 4.3 $new / $delete：类型擦除的析构器存储

#### 4.3.1 概念说明

`mem::$new<T>(args)` 和 `mem::$delete(p)` 是 libipc 内部最常用的对象分配接口（`conn_info_t`、`buffer_`、`id_info_t`、`recycle_t` 等都用它，见 u2/u3/u5 各讲）。它比 `new`/`delete` 强在两点：

1. **底层走 block_pool 分级池**（而不是直接 `malloc`），小对象快、碎片少。
2. **类型擦除释放**：`$delete(void* p)` 不需要知道 `p` 的静态类型就能正确析构并归还内存。

第 2 点是本模块的重点。其手法是「头部贴回收说明书」：`$new` 时在对象**前面**预留 16 字节头部，第 0 字节起放一个函数指针（回收器），这个回收器在 `$new` 时按真实类型 `T` 由编译器生成、把「怎么析构 T、释放多大」写死在函数体里。`$delete` 只负责取出这个函数指针并调用，自己完全类型无关。

#### 4.3.2 核心流程

**内存布局**（64 位平台，`regular_head_size = 16`）：

```text
偏移:   0          8         16                16+sizeof(T)
        ┌──────────┬─────────┬──────────────────────────────┐
        │ recycle_t│ size_t  │       T 的对象体              │
        │ (回收器) │ (仅void)│                              │
        └──────────┴─────────┴──────────────────────────────┘
        ▲                     ▲
        b = mem::alloc(...)   返回给用户的指针 p = b + 16
```

头部大小推导（[new.h:48-50](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/new.h#L48-L50)）：

- `recycle_t` 是函数指针 → 8 字节；`recycler_size = round_up(8, 8) = 8`。
- `allocated_size = sizeof(std::size_t) = 8`。
- `regular_head_size = round_up(8 + 8, alignof(std::max_align_t)) = round_up(16, 16) = 16`。

```text
$new<T>(args):
  b = mem::alloc(16 + sizeof(T))     ← 路由见 4.4
  p = b + 16
  在 p 处构造 T(args)
  *(recycle_t*)b = 「析构 T 并 free(b, 16+sizeof(T))」的函数
  return p                            ← 用户拿到的是 b+16

$delete(p):
  r = (recycle_t*)(p - 16)            ← 回到头部
  (*r)(p)                             ← 调用回收器，它自己知道怎么析构、释放
```

两条分支的区别在于「回收器需不需要在头部存大小」：

- **有类型 `T`**（`do_allocate<T>`）：`sizeof(T)` 在编译期已知，回收器函数体里直接写死 `16 + sizeof(T)`，**不需要**读头部存的 size，`size_t` 槽位闲置。
- **无类型 `void`**（`do_allocate<void>`，即 `$new<void>(bytes)`）：运行期才知道 `bytes`，回收器无法写死大小，于是把总大小 `rbz` 存进头部的 `size_t` 槽位，回收时读出来。

#### 4.3.3 源码精读

**对外接口**：

[include/libipc/mem/new.h:91-105](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/new.h#L91-L105) —— `$new<T>(args)` 转发到 `do_allocate<T>::apply(args...)`；`$delete(p)` 从 `p - regular_head_size` 处取出回收器函数指针并调用。注意 `$delete` 的形参是 `void*`，**完全类型无关**——这就是类型擦除。

**有类型路径 `do_allocate<T>`**：

[include/libipc/mem/new.h:52-70](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/new.h#L52-L70) —— 分配 `regular_head_size + sizeof(T)`，在 `b + regular_head_size` 处 `construct<T>`，然后把一个**lambda**（捕获了类型 `T`）赋给头部的回收器槽位。这个 lambda 的内容是：`destroy((T*)p)`（调析构）后 `mem::free(p - 16, 16 + sizeof(T))`（按编译期已知大小归还）。构造抛异常时 `LIBIPC_CATCH` 返回 `nullptr`，避免泄漏。

**无类型路径 `do_allocate<void>`**：

[include/libipc/mem/new.h:72-87](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/new.h#L72-L87) —— `apply(bytes)` 把运行期大小 `rbz = 16 + bytes` 存进头部 `size_t` 槽位（`*(size_t*)(b + recycler_size) = rbz`），回收器 lambda 读取这个槽位来 `mem::free`。`bytes == 0` 直接返回 `nullptr`。

> 头部 `size_t` 槽位的角色由此清晰：有类型路径不用它（大小编译期写死），无类型路径用它存运行期总大小。

**`mem::alloc` / `mem::free`**（`$new`/$`delete` 的底层）：

[include/libipc/mem/new.h:33-38](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/new.h#L33-L38) 声明；实现见 [src/libipc/mem/new.cpp:112-118](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new.cpp#L112-L118) —— `alloc(bytes)` 即 `get_regular_resource(bytes).allocate(bytes)`，`free(p, bytes)` 即 `get_regular_resource(bytes).deallocate(p, bytes)`。**路由就发生在这里**，下一节（4.4）展开。

**配合 `std::unique_ptr` 的析构器**：

[include/libipc/mem/new.h:107-114](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/new.h#L107-L114) —— `struct deleter { template<T> void operator()(T*p) const { $delete(p); } }`。于是可以写 `std::unique_ptr<T, ipc::mem::deleter>`，让 RAII 自动走 `$delete`。

#### 4.3.4 代码实践

**实践目标**：用真实测试验证 `$new`/`$delete` 的类型擦除多态回收（基类指针释放派生类对象）。

**操作步骤**：阅读并运行测试 [test/mem/test_mem_new.cpp:93-106](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/mem/test_mem_new.cpp#L93-L106)（`TEST(new, delete_poly)`）：

```c++
Base *p = ipc::mem::$new<Derived>(-1);   // 基类指针指向派生类对象
// ... use p ...
ipc::mem::$delete(p);                    // 用基类指针释放 —— 仍正确调用 ~Derived
ASSERT_EQ(construct_count__, 0);         // 派生类析构被调用
```

其中 `Derived`/`Derived64K` 定义在 [test_mem_new.cpp:61-89](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/mem/test_mem_new.cpp#L61-L89)，`~Derived` 会把全局计数 `construct_count__` 归零。

**需要观察的现象**：`$delete(p)` 传入的是 `Base*`，但测试断言 `construct_count__ == 0`，说明**派生类析构函数确实被调用了**。

**预期结果**：测试通过。这正是类型擦除的威力——`$delete` 不需要 `Derived` 的类型信息，因为它调用的回收器是 `$new<Derived>` 当时生成、内含 `destroy((Derived*)p)` 的那个函数。注意 [test_mem_new.cpp:108-121](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/mem/test_mem_new.cpp#L108-L121) 还有一个 `Derived64K`（带 65536 字节 padding）的版本，它会落到 4.4 的 level 3 / level 4 池，验证大对象也走同一套机制。

运行方式：构建时开 `LIBIPC_BUILD_TESTS=ON`，执行 `test_mem` 目标（见 u8-l5 的测试体系）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`$delete` 的注释说「传入的指针类型若与 `$new` 不同，可能产生额外开销」（[new.h:98-100](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/new.h#L98-L100)）。既然回收器是按真实类型生成的，为什么还会有「类型不同」的问题？
**答案**：回收器内部对 `p` 做的是「按 `$new` 时的真实类型析构」，所以**正确性**不受传入指针类型影响（如上一实践所示，基类指针也能正确释放派生类）。注释指的「开销」主要在**路由**层面：`$delete`→回收器→`mem::free(b, 16+sizeof(T))` 时，`get_regular_resource` 是按 `16+sizeof(T)` 这个尺寸去查池的；只要 `$new` 和 `$delete` 涉及的尺寸一致，就能命中同一个 thread-local 池快速回收，否则可能落到不同桶。类型匹配能保证尺寸天然一致。

**练习 2**：为什么有类型路径不往头部写 `size_t`，而无类型路径必须写？
**答案**：有类型路径的回收器是按 `T` 实例化的 lambda，`sizeof(T)` 在编译期已知，被直接编译进函数体；头部那个 `size_t` 槽位闲置。无类型路径 `$new<void>(bytes)` 的 `bytes` 是运行期值，回收器无法写死，只能把总大小 `rbz` 存进头部，回收时读出来——这正是 [new.h:79-84](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/new.h#L79-L84) 干的事。

---

### 4.4 get_regular_resource 分级路由 + container_allocator 与 ipc 容器

#### 4.4.1 概念说明

前面三节分别是 L1（block_pool）、L2（central_cache_pool）、用户接口（$new/$delete）。把它们粘起来的是 `get_regular_resource(bytes)`：它根据请求字节数 `bytes`，在一张**分级表**里选出对应的 `block_collector`（即某个固定块池），`>64KB` 则退回 `new`/`delete`。本节先讲路由表，再讲如何把这套分配器接到标准容器（`ipc::map` / `ipc::unordered_map`）上。

#### 4.4.2 核心流程

**分级路由表**（`regular_level` 先按区间选「层」，再按 `round_up` 选「桶」）：

| 层 level | 字节范围 | `round_up` 粒度 | 桶（BlockSize） | 扩张数 Expansion | 后端 |
|---|---|---|---|---|---|
| 0 | ≤ 128 | 16 | 16, 32, 48, 64, 80, 96, 112, 128 | 512 | `block_pool` |
| 1 | ≤ 1024 | 128 | 256, 384, 512, 640, 768, 896, 1024 | 256 | `block_pool` |
| 2 | ≤ 8192 | 1024 | 2048, 3072, 4096, 5120, 6144, 7168, 8192 | 128 | `block_pool` |
| 3 | ≤ 65536 | 8192 | 16384, 24576, 32768, 40960, 49152, 57344, 65536 | 64 | `block_pool` |
| 4 | > 65536 | — | — | — | `new_delete_resource`（直接 `new`/`delete`） |

`round_up` 的定义见 [imp/aligned.h:69-72](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/imp/aligned.h#L69-L72)：

\[
\text{round\_up}(v, a) = (v + a - 1)\ \&\ \sim(a - 1)
\]

它把 `v` 向上取整到 `a` 的整数倍。每个 `block_pool_resource<BlockSize, Expansion>::get()` 返回一个 **thread_local** 实例，所以每个桶、每个线程都有一份独立的 L1 池。

**容器分配器**：`container_allocator<T>` 是标准库 Allocator 概念的适配器，`allocate`/`deallocate` 直接转发到 `mem::alloc`/`mem::free`。`ipc::map` / `ipc::unordered_map` 只是把 `std::map` / `std::unordered_map` 的最后一个模板参数（分配器）换成 `container_allocator`，让容器内部的所有节点分配都走分级池。

#### 4.4.3 源码精读

**`regular_level` 选层**：

[src/libipc/mem/new.cpp:7-13](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new.cpp#L7-L13) —— 按四个阈值（128/1024/8192/65536）把请求分到 0~4 五层。

**`get_regular_resource` 选桶**：

[src/libipc/mem/new.cpp:53-110](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new.cpp#L53-L110) —— 外层 `switch(l)` 选层，内层 `switch(round_up(s, 粒度))` 选桶，返回 `block_pool_resource<BlockSize, Expansion>::get()`。每个内层 `switch` 都有 `default: break;` 兜底：若 `round_up` 的结果没命中任何桶（理论上不会，因为粒度已保证取整后必落在桶上），会落到函数末尾 [L109](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new.cpp#L109) 的 `block_pool_resource<0, 0>::get()`。

**`block_resource_base`：块池后端 vs new/delete 后端**：

[src/libipc/mem/new.cpp:15-41](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new.cpp#L15-L41) —— 主模板继承 `block_pool<BlockSize, Expansion>`（走固定块池）；特化 `block_resource_base<0, 0>` 继承 `new_delete_resource`（`>64KB` 或兜底时直接 `new`/`delete`）。二者都实现 `block_collector` 的 `allocate`/`deallocate`，对外接口统一。

**thread_local 实例**（L1 的线程隔离就在这里落地）：

[src/libipc/mem/new.cpp:43-51](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new.cpp#L43-L51) —— `static block_collector &get() { thread_local block_pool_resource instance; return instance; }`。**每个线程、每个桶**都有独立的 `block_pool`，这就是 4.1 说的「无锁线程局部」的根源。

**`container_allocator`**：

[include/libipc/mem/container_allocator.h:63-78](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/container_allocator.h#L63-L78) —— `allocate(count)` 调 `mem::alloc(sizeof(T)*count)` 返回**未构造**的原始内存；`deallocate(p, count)` 调 `mem::free`。构造/析构交给单独的 `construct`/`destroy`（[L80-L87](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/container_allocator.h#L80-L87)），转发到 `ipc::construct`/`ipc::destroy`（即 placement-new / 析构，见 u7-l1）。`operator==` 恒为 `true`（[L90-L93](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/container_allocator.h#L90-L93)），表示任意两个 `container_allocator` 可互换释放——因为它们最终都走同一套 `mem::alloc/free` 全局路由。

**`ipc::map` / `ipc::unordered_map`**：

[src/libipc/mem/resource.h:13-21](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/resource.h#L13-L21) —— 两个 `using`，把 `std::map` / `std::unordered_map` 的分配器参数固定为 `container_allocator<std::pair<const Key, T>>`。从此 `ipc::unordered_map<K,V>` 的所有内部节点分配都经 `mem::alloc` → `get_regular_resource` → 分级池，而不是裸 `new`。

#### 4.4.4 代码实践

**实践目标**：画出一次 `mem::$new<void>(4096)` 的完整内存路由路径。

> 说明：练习原文写作 `mem::$new<obj>(4096)`。需要先澄清一个关键点——**路由尺寸取决于「头部 + 载荷」，而非构造实参**。对有类型 `$new<T>(args)`，路由尺寸是 `regular_head_size(16) + sizeof(T)`，构造实参 `args`（包括这里的 `4096`）**不影响路由**；只有无类型 `$new<void>(bytes)` 才把 `bytes` 当作载荷尺寸。为了让「4096」直接对应一个路由决策，下面用 `$new<void>(4096)` 做精确推演；有类型情形只需把「载荷 4096」换成 `sizeof(T)` 即可。

**操作步骤（手动推演）**：

1. 进入 `do_allocate<void>::apply(4096)`（[new.h:72-87](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/new.h#L72-L87)）：`bytes=4096`，`rbz = regular_head_size + 4096 = 16 + 4096 = 4112`，调 `mem::alloc(4112)`。
2. `mem::alloc(4112)` → `get_regular_resource(4112).allocate(4112)`（[new.cpp:112-114](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new.cpp#L112-L114)）。
3. `get_regular_resource(4112)`：`regular_level(4112)` → `4112 <= 8192` 命中 **level 2**（[new.cpp:9-12](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new.cpp#L9-L12)）。
4. 内层 `round_up(4112, 1024)`：

\[
\text{round\_up}(4112, 1024) = (4112 + 1023)\ \&\ \sim 1023 = 5135\ \&\ \sim 1023 = 5120
\]

   命中 **case 5120** → `block_pool_resource<5120, 128>::get()`（[new.cpp:75](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/new.cpp#L75)）。
5. 返回 thread_local 的 `block_pool<5120, 128>` 实例，`allocate()` 摘链表头；首次为空则 `expand()` → `central_cache_pool<block<5120>, 128>::instance().aqueire()`。
6. `aqueire()` 的 `cached_` 为空 → `central_cache_allocator().construct<chunk_t>()`，向 1MB monotonic 申请一个 `array<block<5120>, 128>`（约 128 × 5120 ≈ 640KB， fits 在 1MB 内），串成链表返回头。
7. 回到 `do_allocate<void>`：在块内写回收器（偏移 0）、写总大小 `rbz=4112`（偏移 8），返回 `b + 16`（偏移 16，载荷区，4096 字节可用）。

**路由路径图**：

```text
$new<void>(4096)
  └─ do_allocate<void>::apply(4096)
       └─ mem::alloc(4112)                       [new.h:74-77]
            └─ get_regular_resource(4112)         [new.cpp:54]
                 ├─ regular_level → 2             [new.cpp:11]
                 ├─ round_up(4112,1024) → 5120
                 └─ block_pool_resource<5120,128>::get()  [new.cpp:75]  ← thread_local
                      └─ block_pool<5120,128>::allocate() [block_pool.h:99]
                           └─ (空) expand()
                                └─ central_cache_pool<block<5120>,128>::instance().aqueire()
                                     └─ (cached空) central_cache_allocator().construct<chunk_t>()  ← 1MB monotonic
```

**需要观察的现象 / 预期结果**：一次 4096 字节的「裸」分配，实际占用一个 5120 字节的块（向上取整到 level 2 的桶），并触导向 1MB central cache 申请一个含 128 个 5120 字节块的 chunk。如果接着 `$delete` 它，回收器读出头部存的 `rbz=4112`，`mem::free(b, 4112)` 再次走 `get_regular_resource(4112)` 命中**同一个** `block_pool<5120,128>` 线程局部池，把块挂回链表头——分配与回收落在同一个桶，零跨池开销。

**延伸（有类型情形）**：若真是 `mem::$new<obj>(4096)` 且 `sizeof(obj) == 4080`，则路由尺寸 = `16 + 4080 = 4096`，`round_up(4096, 1024) = 4096` → `block_pool<4096, 128>`。**待本地验证**：可在 `get_regular_resource` 入口打印 `s` 与选中的桶，对照上表核对。

#### 4.4.5 小练习与答案

**练习 1**：一个 `sizeof == 100` 的对象，经 `$new` 会落到哪个桶？扩张数是多少？
**答案**：路由尺寸 = `16 + 100 = 116`。`regular_level(116)` → `116 <= 128` → level 0。`round_up(116, 16) = 128` → `block_pool_resource<128, 512>`。扩张数 512（level 0 最密集，因为小对象最频繁）。

**练习 2**：为什么 `container_allocator` 的 `operator==` 恒返回 `true`？这有什么好处？
**答案**：因为所有 `container_allocator` 最终都转发到同一个全局 `mem::alloc/free` 路由，没有「属于哪个池」的状态——任意实例都能释放另一个实例分配的内存。`operator==` 返回 `true` 告诉标准容器「这两个分配器可互换」，于是容器在赋值/移动时不必重新分配节点，大幅减少无谓拷贝。这是「无状态分配器」的标准做法。

**练习 3**：为什么 `>64KB` 的分配不走 block_pool，而退回 `new`/`delete`？
**答案**：固定块池的优势在于「同尺寸、高频、小对象」。大对象（>64KB）频率低、尺寸离散，若也为每个尺寸建桶，桶的数量会爆炸且每个桶利用率低；直接用 `new`/`delete`（底层 `new_delete_resource`，见 u7-l1 的对齐分配）反而更合适。所以分级表在 64KB 处「收口」，把大对象交给系统分配器。

---

## 5. 综合实践

**任务**：把本讲四个最小模块串起来，写一段「内存路由探针」并解释每一跳。

**操作步骤**（源码阅读 + 推演型，**待本地验证**）：

1. 阅读测试 [test/mem/test_mem_new.cpp:145-172](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/mem/test_mem_new.cpp#L145-L172)（`TEST(new, multi_thread)`）：16 个线程各做 1 万次 `$new<int>()` + `$delete`，再各分配 1 万个 `array<char,10>` 并校验内容。
2. 对每一类分配，回答三个问题：
   - 路由尺寸是多少？（`int` → `16+4=20`；`array<char,10>` → `16+10=26`）
   - 落到哪个 `block_pool<BlockSize, Expansion>`？（`20 → round_up(20,16)=32 → <32,512>`；`26 → round_up(26,16)=32 → <32,512>`）
   - 为什么 16 线程 × 高频分配不需要加锁？（每个线程有自己的 thread_local `block_pool`，L1 无锁；只有首次扩张时才到 L2 的锁无关栈）
3. 把 `ipc::mem::$new<int>()` 换成 `ipc::unordered_map<int,int>` 插入若干元素，跟踪一个内部节点的分配路径：`unordered_map` → `container_allocator` → `mem::alloc` → `get_regular_resource` → 某个 block_pool。
4. 思考：如果某线程突发分配大量 `int` 然后全部释放，这些块会留在哪里？（留在**本线程**的 thread_local `block_pool<32,512>` 链表里，不会立刻归还 L2；只有该 `block_pool` 析构——线程退出时——才 `release` 回 `central_cache_pool` 供别的线程复用。）

**预期结果**：你能对着源码说出从 `$new` 到 1MB monotonic 的**每一跳**，并能解释 L1（线程局部无锁）、L2（锁无关栈 + 批量 chunk）、头部类型擦除这三层设计各自解决什么问题。

---

## 6. 本讲小结

- **`block_pool`（L1）** 是线程局部的固定块空闲链表，用 `union block` 复用「空闲 `next` 指针 / 占用 `storage`」，分配回收都是 O(1) 摘头/挂头，**无锁无原子**（因为 thread-local）。
- **`central_cache_pool`（L2）** 是进程级单例，用两条锁无关 CAS 栈（`cached_` 可用块 / `aqueired_` 记账节点）实现跨线程块流转；栈空时向 u7-l2 的 1MB monotonic **批量**申请一个 chunk（`array<block, Expansion>`），串成链表分发。
- **`$new`/`$delete`** 用「头部贴回收说明书」实现类型擦除：在对象前预留 16 字节，存一个按真实类型生成的回收函数指针，`$delete(void*)` 只需取出并调用，不必知道对象类型；有类型路径把 `sizeof(T)` 编进函数体，无类型路径把总大小存进头部 `size_t` 槽。
- **`get_regular_resource`** 是粘合剂：按 16B…64KB 分 4 层、每层若干桶，把请求路由到对应 thread_local `block_pool`，`>64KB` 退回 `new`/`delete`。
- **`container_allocator`** 与 **`ipc::map`/`ipc::unordered_map`** 把标准容器的节点分配接到这套分级池上，`operator==` 恒真使其成为无状态可互换分配器。
- 整条链路是「用户接口 `$new` → 分级路由 → thread-local L1 → 进程级 L2 锁无关栈 → 1MB monotonic →（溢出）系统 malloc」的分层缓存，越上层越快越局部、越下层越共享越通用。

---

## 7. 下一步学习建议

- **u8-l3（intrusive_stack 与 id_pool 无锁结构）**：本讲把 `central_cache_pool` 的两条栈当「黑盒无锁栈」用了，u8-l3 会拆开 `concur::intrusive_stack` 的 Treiber 栈 CAS 实现，并对照讲 `id_pool` 的数组当链表技巧——两者分别是 central cache 与 u3-l3 chunk 存储的地基。
- **u8-l1（内存序、伪共享与缓存行）**：本讲提到的 `intrusive_stack` 的 `acquire`/`release` 内存序、以及 `block` 的 `alignas(std::max_align_t)`，在 u8-l1 会有系统的内存可见性与伪共享分析。
- **回看 u3-l3（大消息外部存储）**：带着本讲对 `id_pool`/空闲链表/类型擦除析构的理解，重读 `recycle_storage` 挂载为 `buff_t` 析构器的零拷贝回收，会看到本讲 `$new`/`$delete` 的类型擦除思路在大消息引用计数回收里的同构应用。
- 若想动手：在 `get_regular_resource` 各分支加计数日志，跑一次 `test_mem`，观察不同尺寸请求命中各桶的分布，直观验证分级表的覆盖率。
