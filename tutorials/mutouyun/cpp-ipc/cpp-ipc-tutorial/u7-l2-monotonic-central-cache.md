# monotonic_buffer_resource 与中央 1MB 缓存

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「单调缓冲（monotonic buffer）」是什么，以及它为什么只能「整块释放」而不能按块回收。
- 读懂 libipc 的 `monotonic_buffer_resource`：它如何用 bump 指针分配、如何用 `std::align` 处理对齐、缓冲不够时如何按 ×3/2 指数增长、以及增长时如何把真正干活的事委托给上游 `upstream_`。
- 解释 `deallocate` 为何是空操作，而 `release` 又为何只回收「自己长出来的节点」、不动「借来的初始缓冲」。
- 理解 `central_cache_allocator()` 这个单例：它用一个 1MB 静态数组当初始缓冲，外面包一层 `thread_safe_resource`（monotonic + `std::mutex`）来保证多线程安全。
- 说清楚「中央缓存（central cache）」的元数据到底是什么、为什么用一块 1MB 的单调缓冲来装它们。

本讲是 u7-l1（分配器架构、类型擦除、`bytes_allocator`）的直接延续，请确保你已经掌握 `bytes_allocator` 如何用类型擦除持有一个内存资源指针、以及 `new_delete_resource` 是什么。

## 2. 前置知识

### 2.1 什么是 bump 分配（指针前移分配）

想象一条很长的内存带，你手里拿一支笔（指针 `head_`）：

- 要 16 字节，就把笔往前挪 16 字节，把挪之前的那个位置返回给调用方。
- 再要 32 字节，再往前挪 32 字节。

这种「只往前走、不回退」的分配叫 **bump allocation**（也叫线性/指针前移分配）。它极快——没有空闲链表、没有合并、没有碎片整理，每次分配就是一次指针加法。

代价是：你**不能单独释放某一块**。因为块之间没有头部记录大小、也没有链表把它们串起来，你没法把「中间某块」单独还回去。释放只能「整条带子一起扔」。

### 2.2 `std::align` 是什么

`std::align(alignment, bytes, p, space)` 是标准库的工具函数：给定一段从 `p` 开始、长度为 `space` 的内存，它会把 `p` 向后调整到满足 `alignment` 对齐，并相应扣减 `space`。如果剩余空间装不下 `bytes` 字节，它返回 `nullptr`。

libipc 的 bump 分配正是靠它同时完成「对齐」和「判满」两件事。

### 2.3 回顾 u7-l1 的两个角色

- **`new_delete_resource`**：最底层的资源，内部就是跨平台的 `malloc`/`aligned_alloc`。
- **`bytes_allocator`**：多态分配器，用类型擦除持有一个资源指针，对外提供 `allocate`/`deallocate`/`construct<T>`/`destroy<T>`。

本讲的 `monotonic_buffer_resource` 是夹在两者之间的「策略层」资源：它的「上游」`upstream_` 是一个 `bytes_allocator`（默认指向堆），它自己对外又伪装成一个可被 `bytes_allocator` 持有的资源。

### 2.4 一个关键术语：central cache（中央缓存）

到 u7 才出现的「central cache」是 libipc 内存子系统里跨线程共享的那一层固定块缓存（详见 u7-l3）。本讲不展开它的块回收算法，只需要知道一点：**central cache 自己的「控制结构」需要内存**——比如它批量申请的一大组块（`chunk_t`）、它的无锁栈节点（`node_t`）。这些控制结构从哪里来？就是从本讲的 `central_cache_allocator()` 来。所以这 1MB 单调缓冲，装的是 central cache 的「元数据/骨架」，不是用户消息数据。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/libipc/mem/memory_resource.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/memory_resource.h) | 声明 `new_delete_resource` 与 `monotonic_buffer_resource`，定义后者的全部成员变量 |
| [src/libipc/mem/monotonic_buffer_resource.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/monotonic_buffer_resource.cpp) | `monotonic_buffer_resource` 的实现：构造、`allocate`、`deallocate`、`release`、`make_node`、`next_buffer_size` |
| [src/libipc/mem/central_cache_allocator.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/central_cache_allocator.cpp) | 定义 `thread_safe_resource`（monotonic + 互斥量）与单例 `central_cache_allocator()`（1MB 静态缓冲） |
| [include/libipc/mem/central_cache_allocator.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/central_cache_allocator.h) | 声明 `central_cache_allocator()`，注释说明了它的用途 |
| [include/libipc/mem/central_cache_pool.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/central_cache_pool.h) | central cache 本体，展示 `central_cache_allocator().construct<chunk_t>()` 的真实用法（即「元数据」的去处） |
| [include/libipc/def.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h) | 定义常量 `central_cache_default_size = 1024 * 1024`（1MB） |

---

## 4. 核心概念与源码讲解

### 4.1 monotonic 的成员布局与 bump 分配

#### 4.1.1 概念说明

`monotonic_buffer_resource` 的本质是一条「可增长的内存带」。它用一个当前缓冲区 `[head_, tail_)` 来做 bump 分配：`head_` 是下一次分配的起点，`tail_` 是当前缓冲的末尾。

它有两组关键成员：

- **当前缓冲游标**：`head_`（bump 指针）、`tail_`（当前缓冲末尾）。
- **增长记账**：`free_list_`（自己向 upstream 申请、长出来的节点组成的链表）、`next_size_`（下次增长时要申请多大）、`upstream_`（用来增长的上级分配器）。
- **初始状态快照**：`initial_buffer_`、`initial_size_`，用于 `release()` 后复位。

其中 `node` 是每个「长出来的缓冲块」的头部，记录链表指针和该块总大小。

#### 4.1.2 核心流程

`allocate(bytes, alignment)` 的执行过程：

1. 若 `bytes == 0`，直接返回 `nullptr`（并记一条错误日志）。
2. 令 `p = head_`，`s = tail_ - head_`（当前剩余字节数）。
3. 调 `std::align(alignment, bytes, p, s)`：
   - 返回非空 → 当前缓冲装得下，跳到第 6 步。
   - 返回空 → 当前缓冲不够，进入第 4 步「增长」。
4. （增长）保证 `next_size_` 至少能容纳本次请求，向 `upstream_` 申请一个新 `node`，把它插到 `free_list_` 头部，再把 `next_size_` 放大 ×3/2。
5. 在新节点的载荷区里再做一次 `std::align` 定位 `p`，并把 `tail_` 更新为新缓冲的末尾。
6. **bump**：`head_ = p + bytes`，返回 `p`。

注意第 6 步——无论走快路径（当前缓冲够）还是慢路径（增长），最终都是「`head_` 往前挪 `bytes` 字节、返回挪之前的 `p`」，这就是 bump。

#### 4.1.3 源码精读

先看成员声明，建立「这条带子由哪些指针管」的印象：

[include/libipc/mem/memory_resource.h:46-60](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/memory_resource.h#L46-L60) —— `monotonic_buffer_resource` 持有 `upstream_`（增长的上级）、`free_list_`（增长节点链表头）、`head_`/`tail_`（当前 bump 区间）、`next_size_`（下次增长尺寸）、`initial_buffer_`/`initial_size_`（初始快照）。

```cpp
class LIBIPC_EXPORT monotonic_buffer_resource {
  bytes_allocator upstream_;
  struct node { node *next; std::size_t size; } *free_list_;
  ipc::byte * head_;
  ipc::byte * tail_;
  std::size_t next_size_;
  ipc::byte * const initial_buffer_;
  std::size_t const initial_size_;
  // ...
```

再看 bump 分配的核心：

[src/libipc/mem/monotonic_buffer_resource.cpp:103-130](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/monotonic_buffer_resource.cpp#L103-L130) —— `allocate`：用 `std::align` 同时完成对齐与判满，装得下就 bump，装不下就走增长路径。

```cpp
void *p = head_;
auto  s = static_cast<std::size_t>(tail_ - head_);
if (std::align(alignment, bytes, p, s) == nullptr) {
    // ... 当前缓冲不够，增长（见 4.2）...
}
head_ = static_cast<ipc::byte *>(p) + bytes;   // ← 关键：bump 指针前移
return p;
```

`std::align` 返回非空时，`p` 已被调整到对齐地址、`s` 已扣除对齐填充；随后 `head_ = p + bytes` 把游标前移，返回的 `p` 就是这一块。

#### 4.1.4 代码实践

**实践目标**：亲手验证 bump 语义——连续分配时，返回地址是单调递增的、且贴合前一次的末尾。

**操作步骤**（示例代码，需自行写一个 `main.cpp` 并链接 libipc）：

```cpp
// 示例代码：观察 monotonic 的 bump 行为
#include "libipc/mem/memory_resource.h"
#include <cstdio>

int main() {
    ipc::mem::monotonic_buffer_resource res(static_cast<std::size_t>(1024));
    void *a = res.allocate(16);
    void *b = res.allocate(16);
    void *c = res.allocate(32);
    std::printf("a=%p b=%p c=%p\n", a, b, c);
    std::printf("b-a = %td, c-b = %td\n",
                static_cast<char*>(b) - static_cast<char*>(a),
                static_cast<char*>(c) - static_cast<char*>(b));
}
```

**需要观察的现象**：`b - a` 约等于 16（受对齐影响，通常正好 16）；`c - b` 约等于 32。地址单调递增。

**预期结果**：在三段都落在初始 1024 字节缓冲内时，地址依次紧挨着前移。**待本地验证**：具体偏移取决于 `std::max_align_t` 的对齐值与平台，但「单调递增、间距≈请求大小」一定成立。

#### 4.1.5 小练习与答案

**练习 1**：如果第一次 `allocate` 就请求一个比初始缓冲还大的块，会发生什么？
**答案**：`std::align` 在当前缓冲判满返回空，进入增长路径，向 `upstream_` 申请一个足够大的新节点（`next_size_` 会被 `max(next_size_, bytes)` 抬高到至少 `bytes`），在新节点里分配并返回。当前初始缓冲被「跳过」闲置。

**练习 2**：bump 分配为什么不需要任何锁或 CAS？
**答案**：因为它只读写属于本对象的 `head_`/`tail_` 指针，且不维护跨块结构。但前提是**单线程使用**；多线程下 `head_` 的前移会数据竞争，这正是 4.4 要引入 `thread_safe_resource` 的原因。

---

### 4.2 指数增长（×3/2）与 upstream 委托

#### 4.2.1 概念说明

当前缓冲装不下时，`monotonic_buffer_resource` 不会自己去调系统 `malloc`，而是把「申请一大块新内存」这件事**委托**给它的成员 `upstream_`（一个 `bytes_allocator`）。默认情况下 `upstream_` 指向 `new_delete_resource`，也就是堆。

每次增长，新块比上一次大 ×3/2。这种**指数增长**是经典的摊还（amortized）策略：偶尔发生一次较慢的堆分配，换取绝大多数分配都落在已经预分配的缓冲里、走得飞快。

#### 4.2.2 核心流程

增长因子定义在一个匿名命名空间的辅助函数里：

\[ \text{next\_size} = \left\lfloor \text{size} \times \frac{3}{2} \right\rfloor \]

设初始尺寸为 \(s_0\)，经过 \(n\) 次增长后第 \(n\) 个块的尺寸为：

\[ s_n = s_0 \cdot \left(\frac{3}{2}\right)^n \]

累计已分配字节 \(S_n = \sum_{k=0}^{n} s_k = s_0 \cdot \dfrac{(3/2)^{n+1}-1}{(3/2)-1}\)，而最后一次增长本身约占总量的 \(1 - 1/(3/2) \to\) 一个常数比例，因此**平摊到每次分配的扩容成本是 \(O(1)\)**。

实际增长的步骤：

1. `next_size_ = max(next_size_, bytes)`——保证新块至少装得下当前请求。
2. `make_node(upstream_, next_size_, alignment)` 向 upstream 申请 `round_up(sizeof(node), alignment) + next_size_` 字节，填好 `node->next = nullptr`、`node->size = sz`。
3. 把新 `node` 插到 `free_list_` 头部（`node->next = free_list_; free_list_ = node;`）。
4. `next_size_ = next_buffer_size(next_size_)`——把下次的尺寸放大 ×3/2。

`make_node` 里用到的 `round_up` 是经典的「向上取整到 alignment 的倍数」位运算：

\[ \text{round\_up}(v, a) = (v + a - 1)\ \& \sim(a-1) \]

[include/libipc/imp/aligned.h:70-72](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/imp/aligned.h#L70-L72) —— `round_up` 的实现，把节点头 `sizeof(node)` 向上对齐，使紧跟其后的载荷区从对齐地址开始。

#### 4.2.3 源码精读

增长因子：

[src/libipc/mem/monotonic_buffer_resource.cpp:37-39](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/monotonic_buffer_resource.cpp#L37-L39) —— `next_buffer_size` 就是 `size * 3 / 2`。

```cpp
std::size_t next_buffer_size(std::size_t size) noexcept {
  return size * 3 / 2;
}
```

向 upstream 申请节点的工具函数：

[src/libipc/mem/monotonic_buffer_resource.cpp:15-35](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/monotonic_buffer_resource.cpp#L15-L35) —— `make_node` 计算「对齐后的头 + 载荷」总大小，交给 `upstream.allocate`，回填 `next`/`size`。异常安全用 `LIBIPC_TRY/LIBIPC_CATCH` 包裹，失败返回 `nullptr`。

```cpp
auto sz = ipc::round_up(sizeof(Node), alignment) + initial_size;
auto *node = static_cast<Node *>(upstream.allocate(sz));
// ... node->next = nullptr; node->size = sz; ...
```

在 `allocate` 里真正触发增长的片段：

[src/libipc/mem/monotonic_buffer_resource.cpp:111-120](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/monotonic_buffer_resource.cpp#L111-L120) —— 抬高 `next_size_`、申请节点、插链表、放大下次尺寸、在新节点里重新对齐定位。

```cpp
next_size_ = (std::max)(next_size_, bytes);
auto *node = make_node<...>(upstream_, next_size_, alignment);
node->next = free_list_;
free_list_ = node;
next_size_ = next_buffer_size(next_size_);  // 放大 ×3/2
```

#### 4.2.4 代码实践

**实践目标**：把「指数增长」具象化——用纸笔（或一段打印日志）算出连续溢出时每块的大小。

**操作步骤**：阅读上面的源码片段，假设初始缓冲为 0（即用默认构造 `monotonic_buffer_resource{}` 时 `next_size_ = 0`），连续做 5 次 `allocate(100)` 且每次都恰好触发增长（仅作推演，不必真的让每次都增长）。

**需要观察的现象**：第 1 次增长前 `next_size_` 被 `max(0, 100)` 抬到 100；之后依次为 `100 → 150 → 225 → 337 → 505`（整数除法截断）。

**预期结果**：尺寸序列近似 \(100 \times (3/2)^n\)，每次约为上次的 1.5 倍。**待本地验证**：若想实测，可构造一个极小初始缓冲（如 `monotonic_buffer_resource{8}`）并连续分配大块，在 `make_node` 处加日志打印 `initial_size`。

#### 4.2.5 小练习与答案

**练习 1**：为什么是 ×3/2，而不是 ×2？
**答案**：两者都满足摊还 \(O(1)\)。×3/2 比 ×2 增长更平缓，内存浪费上限更低（最坏空闲约为已用量的 1/2，而非 1 倍），在「控制峰值占用」与「减少扩容次数」之间取了折中。这是工程选择，不是硬性要求。

**练习 2**：`monotonic_buffer_resource` 的 `upstream_` 默认指向哪里？central cache 用的那个实例，溢出 1MB 后会去哪里要内存？
**答案**：默认是 `bytes_allocator{}`，即 `new_delete_resource::get()`，也就是堆。central cache 的 `thread_safe_resource` 用 `monotonic_buffer_resource(span)` 构造，该构造最终也用默认 `bytes_allocator{}` 当 upstream，所以**超过 1MB 的部分会回落到堆 `malloc`**。

---

### 4.3 只整体释放：deallocate 空操作 与 release 回收

#### 4.3.1 概念说明

单调缓冲的「单调」就体现在：**只能整体释放，不能按块释放**。因此 `deallocate` 在本资源里是**空操作**——调用它什么都不发生。

真正回收内存的地方是 `release()`（也会在析构时自动调）。但即便 `release` 也有一个精细的设计：

- 它只回收「自己向 upstream 长出来的节点」（即 `free_list_` 里的那些），把它们逐个 `upstream_.deallocate` 还给堆。
- 它**不碰**「借来的初始缓冲」`initial_buffer_`——因为那块内存不属于本资源（central cache 的初始缓冲是一个进程级静态数组，资源不拥有它，无权也不该释放它）。
- 回收完节点后，它把 `head_`/`tail_`/`next_size_` **复位回构造时的初始状态**，使资源可以继续被使用。

这完全对应 `std::pmr::monotonic_buffer_resource` 的语义：「用户提供的初始缓冲不被拥有，只有资源自己后续申请的才会在析构时释放」。

#### 4.3.2 核心流程

`deallocate`：

```
deallocate(p, bytes, alignment):
    什么都不做（参数被 static_cast<void> 显式忽略）
```

`release`：

1. 遍历 `free_list_`，对每个节点调 `upstream_.deallocate(node, node->size)`，沿 `next` 链释放。
2. 若 `initial_buffer_ != nullptr`：把 `head_ = initial_buffer_`、`tail_ = initial_buffer_ + initial_size_`、`next_size_ = next_buffer_size(initial_size_)`（复位到初始缓冲，准备重用）。
3. 否则（构造时没给初始缓冲）：`tail_ = nullptr`、`next_size_ = initial_size_`。

#### 4.3.3 源码精读

空操作的 `deallocate`：

[src/libipc/mem/monotonic_buffer_resource.cpp:132-137](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/monotonic_buffer_resource.cpp#L132-L137) —— 三个参数全部 `static_cast<void>` 忽略，注释 `// Do nothing.`。这正是「单调」二字的代码体现。

```cpp
void monotonic_buffer_resource::deallocate(void *p, std::size_t bytes, std::size_t alignment) noexcept {
  static_cast<void>(p);
  static_cast<void>(bytes);
  static_cast<void>(alignment);
  // Do nothing.
}
```

真正干活的 `release`：

[src/libipc/mem/monotonic_buffer_resource.cpp:81-101](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/monotonic_buffer_resource.cpp#L81-L101) —— 先沿 `free_list_` 把增长节点全还给 upstream，再根据有无初始缓冲复位游标。

```cpp
while (free_list_ != nullptr) {
    auto *next = free_list_->next;
    upstream_.deallocate(free_list_, free_list_->size);
    free_list_ = next;
}
// reset to initial state at contruction
if ((head_ = initial_buffer_) != nullptr) {
    tail_ = head_ + initial_size_;
    next_size_ = next_buffer_size(initial_size_);
} else {
    tail_ = nullptr;
    next_size_ = initial_size_;
}
```

注意：`initial_buffer_` 在整个函数里**只被读取、从不被释放**。这是「借来的内存不归我管」的硬保证。

#### 4.3.4 代码实践

**实践目标**：验证 `deallocate` 确实是空操作，并理解 `release` 后资源可「复活」。

**操作步骤**（示例代码）：

```cpp
// 示例代码
#include "libipc/mem/memory_resource.h"
#include <cstring>

int main() {
    ipc::mem::monotonic_buffer_resource res(static_cast<std::size_t>(256));
    void *a = res.allocate(64);
    res.deallocate(a, 64);          // ← 空操作，a 仍然「占用」着缓冲
    void *b = res.allocate(64);     // ← b 会落在 a 之后，而非复用 a 的位置
    // b != a，证明 deallocate 没有把内存还回来
}
```

**需要观察的现象**：`b != a`，即 `deallocate(a)` 之后 `a` 那段空间并没有被回收复用。

**预期结果**：`b` 紧跟在 `a` 之后（间距约 64），证明单块释放无效。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：既然 `deallocate` 是空操作，那 central cache 把块「还回来」时岂不是泄漏了？
**答案**：不会。central cache（`central_cache_pool`）有自己的块级回收机制——用无锁栈 `cached_`/`aqueired_`（见 u7-l3、u8-l3）把释放的块重新串起来复用，**根本不调** monotonic 的 `deallocate`。monotonic 只负责「批发」一大组块（`chunk_t`），块细粒度的进出由 central cache 自己管。两者职责分离。

**练习 2**：`release()` 为什么不释放 `initial_buffer_`？如果硬要释放会发生什么？
**答案**：因为初始缓冲是构造时**外部传入**的，资源只是借用，不持有所有权（central cache 的初始缓冲是函数内 `static std::array`，由编译器管理生命周期）。硬要释放等于对栈/静态区指针调 `free`，是未定义行为。`release` 只还「自己花钱买的」`free_list_` 节点。

---

### 4.4 thread_safe_resource 与 1MB central cache 单例

#### 4.4.1 概念说明

`monotonic_buffer_resource` 的 `head_`/`tail_`/`free_list_` 都是**普通非原子成员**，多线程并发 `allocate` 会让 bump 指针数据竞争。而 central cache 是**跨线程共享**的（各线程的 thread-local `block_pool` 用尽后会向 central cache 求援），所以必须加锁。

libipc 的做法是写一个 `thread_safe_resource`：**公开继承** `monotonic_buffer_resource`，额外持有一个 `std::mutex`，并把 `allocate`/`deallocate`/析构都重写一遍——在转调基类之前先 `lock_guard`。

然后 `central_cache_allocator()` 是一个返回 `bytes_allocator&` 的单例函数，内部有三个函数级 `static` 局部变量：

1. `buf`：一个 1MB 的 `std::array<byte, central_cache_default_size>`，作为 monotonic 的**初始缓冲**（借给资源、不归资源所有）。
2. `res`：一个 `thread_safe_resource(buf)`，把这块 1MB 包成线程安全的单调资源。
3. `a`：一个 `bytes_allocator(&res)`，用类型擦除持有 `&res`，作为对外返回的多态分配器。

关键：1MB 是一笔「预付款」。central cache 的元数据（`chunk_t` 批量块、`node_t` 栈节点）通常都很小，绝大多数情况下落在 1MB 内、走的是飞快的 bump 快路径；只有累积超过 1MB 才会回落到堆（见 4.2）。而这 1MB 数组是进程级静态变量，程序退出才销毁，所以 `res` 的析构（会调 `release`）实际上只在进程结束时发生一次。

#### 4.4.2 核心流程

单例构造链：

```
central_cache_allocator() 被首次调用
   │
   ├─ static std::array<byte, 1MB> buf;          // 零初始化的静态数组
   ├─ static thread_safe_resource res(buf);      // → monotonic_buffer_resource(buf)
   │       └─ upstream_ = bytes_allocator{}      //   = new_delete_resource（堆）
   │       └─ head_ = buf.begin(), tail_ = buf.end()
   │       └─ next_size_ = next_buffer_size(1MB) = 1.5MB
   └─ static bytes_allocator a(&res);            // 类型擦除持有 &res
   │       └─ holder_mr<thread_safe_resource> 内联于 a，存指针 &res
   └─ return a;
```

当某处调 `central_cache_allocator().allocate(s, al)` 时：

```
bytes_allocator::allocate
   └─ holder_->alloc(s, al)                      // 虚函数，落到 holder_mr<thread_safe_resource>
        └─ res_->allocate(s, al)                 // res_ 类型是 thread_safe_resource*
             └─ thread_safe_resource::allocate   // ← 名字隐藏，命中加锁版本
                  ├─ lock_guard<mutex>           // 加锁
                  └─ monotonic_buffer_resource::allocate(s, al)  // bump
```

注意一个精妙点：`monotonic_buffer_resource::allocate` **不是虚函数**，线程安全靠的是**名字隐藏（name hiding）**加上 holder 里存的是**具体派生类型** `thread_safe_resource*` 的指针，因此 `res_->allocate` 在编译期就绑定到加锁版本，无需虚函数开销。

#### 4.4.3 源码精读

1MB 常量的来源：

[include/libipc/def.h:33-39](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h#L33-L39) —— `central_cache_default_size = 1024 * 1024`，与 `data_length`/`large_msg_*` 等并列的全局尺寸常量。

```cpp
enum : std::size_t {
  central_cache_default_size = 1024 * 1024, ///< 1MB
  ...
};
```

`thread_safe_resource` 的加锁包装：

[src/libipc/mem/central_cache_allocator.cpp:15-37](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/central_cache_allocator.cpp#L15-L37) —— 公开继承 `monotonic_buffer_resource`，私有持有 `std::mutex mutex_`，三个公开方法（析构/`allocate`/`deallocate`）都用 `lock_guard` 包住再转调基类。

```cpp
class thread_safe_resource : public monotonic_buffer_resource {
public:
  thread_safe_resource(span<byte> buffer) noexcept
      : monotonic_buffer_resource(buffer) {}
  void *allocate(std::size_t bytes, std::size_t alignment) noexcept {
    LIBIPC_UNUSED std::lock_guard<std::mutex> lock(mutex_);
    return monotonic_buffer_resource::allocate(bytes, alignment);
  }
  // ... deallocate、析构同理 ...
private:
  std::mutex mutex_;
};
```

单例本身——三个 `static` 局部变量：

[src/libipc/mem/central_cache_allocator.cpp:39-44](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/central_cache_allocator.cpp#L39-L44) —— `buf`（1MB 静态数组）、`res`（包了 buf 的线程安全单调资源）、`a`（持有 `&res` 的多态分配器），按声明顺序构造、逆序析构。

```cpp
bytes_allocator &central_cache_allocator() noexcept {
  static std::array<byte, central_cache_default_size> buf;
  static thread_safe_resource res(buf);
  static bytes_allocator a(&res);
  return a;
}
```

注意 `bytes_allocator a(&res)` 存的是**指针** `&res`，要求 `res` 的生命周期长于 `a`——这里三者同为函数级 static，按声明顺序构造、逆序析构（`a` 先析构、`res` 后析构），`a` 全程指向有效的 `res`，安全。这正对应 `bytes_allocator` 头文件里「指针生命周期须长于分配器」的约定。

这 1MB 究竟装了什么？看 central cache 如何消费它：

[include/libipc/mem/central_cache_pool.h:51-66](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/central_cache_pool.h#L51-L66) —— `central_cache_pool::aqueire()` 在缓存栈为空时，用 `central_cache_allocator().construct<chunk_t>()` 批量申请一整组块。

```cpp
auto *chunk = central_cache_allocator().construct<chunk_t>();  // ← 从 1MB 单调缓冲里 bump 出一个 chunk
// ...把 chunk 里的 block 串成链表...
return chunk->data();
```

[include/libipc/mem/central_cache_pool.h:68-77](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/central_cache_pool.h#L68-L77) —— `release()` 里用 `central_cache_allocator().construct<node_t>()` 申请无锁栈节点（central cache 自己的记账结构）。

所以「central cache 元数据」= `chunk_t`（一批固定块）+ `node_t`（无锁栈节点），它们都从这块 1MB 单调缓冲里 bump 出来。头文件的注释也点明了这一点：

[include/libipc/mem/central_cache_allocator.h:14-17](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/central_cache_allocator.h#L14-L17) —— 注释说明该分配器「用于为 central cache pool 分配内存，底层是一个带定长缓冲的 `monotonic_buffer_resource`」。

#### 4.4.4 代码实践

**实践目标**：跟踪 `central_cache_allocator()` 的构造，说清它如何用「1MB 静态数组 + monotonic」服务 central cache 元数据。这是本讲的主实践，分「读」和「跑」两步。

**操作步骤 1（读：画出构造与调用链）**

1. 打开 [src/libipc/mem/central_cache_allocator.cpp:39-44](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/central_cache_allocator.cpp#L39-L44)，按声明顺序写下三个 static：`buf`(1MB) → `res`(thread_safe_resource) → `a`(bytes_allocator)。
2. 追 `thread_safe_resource(buf)` → `monotonic_buffer_resource(span)`（[monotonic_buffer_resource.cpp:64-71](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/monotonic_buffer_resource.cpp#L64-L71)），确认 `head_=buf.begin()`、`tail_=buf.end()`、`next_size_=1.5MB`、`upstream_=堆`。
3. 追 `bytes_allocator(&res)`（[bytes_allocator.h:134-141](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/bytes_allocator.h#L134-L141)），确认它把 `&res` 存进内联 holder。
4. 追一处真实消费者 [central_cache_pool.h:57](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mem/central_cache_pool.h#L57)：`.construct<chunk_t>()` = `allocate(sizeof(chunk_t))` + placement-new，而 `allocate` 经 holder 转到 `thread_safe_resource::allocate`（加锁）→ `monotonic_buffer_resource::allocate`（在 1MB 里 bump）。

**操作步骤 2（跑：用现成测试验证可分配）**

libipc 已经为此写了测试：

[test/mem/test_mem_central_cache_allocator.cpp:8-15](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/mem/test_mem_central_cache_allocator.cpp#L8-L15) —— 连续分配 1/10/100/1000/10000 字节，断言全部非空。

```cpp
TEST(central_cache_allocator, allocate) {
  auto &a = ipc::mem::central_cache_allocator();
  ASSERT_FALSE(nullptr == a.allocate(1));
  ASSERT_FALSE(nullptr == a.allocate(10));
  ASSERT_FALSE(nullptr == a.allocate(100));
  ASSERT_FALSE(nullptr == a.allocate(1000));
  ASSERT_FALSE(nullptr == a.allocate(10000));
}
```

用 u1-l2 学过的方式构建并运行它（需开启 `LIBIPC_BUILD_TESTS`）：

```bash
cmake -S . -B build -DLIBIPC_BUILD_TESTS=ON
cmake --build build
ctest --test-dir build -R central_cache_allocator -V
```

**需要观察的现象**：测试通过。这些请求累计仅约 11KB，远小于 1MB，因此全部走 bump 快路径、不会触发堆分配。

**预期结果**：`construct`/`allocate` 返回非空指针，测试 PASS。**待本地验证**：具体测试名与 ctest 过滤串以本机构建产物为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么不直接给 `monotonic_buffer_resource::allocate` 加锁（比如把它做成虚函数并在派生类 override），而要新写一个 `thread_safe_resource`？
**答案**：一是 **API 兼容**——`monotonic_buffer_resource` 是公共类，用户可能拿它做单线程用途，强行加锁会引入无谓开销；线程安全是 central cache 这个特定场景的额外需求，用组合/继承另包一层更干净。二是 **零虚函数开销**——libipc 选择「名字隐藏 + holder 存具体派生类型指针」，让 `bytes_allocator` 的调用在编译期就绑定到加锁版本，省去一次虚调用。

**练习 2**：1MB 用满了怎么办？会崩溃吗？
**答案**：不会崩溃。`monotonic_buffer_resource::allocate` 在 1MB 缓冲判满后，会向其 `upstream_`（默认堆）申请一个新节点继续分配（见 4.2）。代价是这部分走向了较慢的 `malloc` 路径，并产生一个在进程结束前不会释放的增长节点。对 central cache 的元数据来说，1MB 通常绰绰有余，溢出是罕见情况。

**练习 3**：`central_cache_allocator()` 返回的是 `bytes_allocator&`，调用方 `.construct<chunk_t>()` 后**从不** `destroy`。这会泄漏吗？
**答案**：不会。`monotonic_buffer_resource::deallocate` 本就是空操作（见 4.3），即使调 `destroy` 也不还内存。central cache 的设计就是「批发一次性、块级自回收」：chunk 一旦分配就常驻在单调缓冲里，由 central cache 的无锁栈在「缓存/在用」之间流转复用，整体随进程结束而回收。这是有意为之，不是泄漏。

---

## 5. 综合实践

把本讲四块知识串起来，完成下面这个「画一张 central cache 内存供给图」的任务：

**任务**：用一张图（文字描述即可）说明「一次 `central_cache_pool::aqueire()` 在缓存为空时的内存供给路径」，要求标注以下每一处并说明理由：

1. `aqueire()` 发现 `cached_` 栈空，决定批量申请 → 调 `central_cache_allocator().construct<chunk_t>()`。
2. `construct` → `bytes_allocator::allocate(sizeof(chunk_t))` → holder 虚调用 → `thread_safe_resource::allocate`（**为何这里会加锁？**）。
3. → `monotonic_buffer_resource::allocate`：在 1MB 静态缓冲里做 `std::align` + bump（**为何是 bump 而不是 free-list 查找？**）。
4. 若 1MB 不足 → `make_node` 向 upstream（堆）申请 ×3/2 增长（**为何不直接报错？为何是 ×3/2？**）。
5. chunk 返回后，`aqueire()` 把它内部的 block 串成链表返回一个 block；这个 block 用完后由 `release()` 压回 `cached_` 栈（**为何不调 monotonic 的 deallocate？**）。

**验收标准**：你能在图上指出——哪段内存来自 1MB 静态数组（bump 快路径）、哪段可能来自堆（增长慢路径）、`std::mutex` 保护的是哪些字段（`head_`/`tail_`/`free_list_`）、以及为什么 central cache 选 monotonic 而不是普通 `new_delete_resource`（答案：批量批发 + 块级自回收，monotonic 的「不按块释放」恰好不是缺点）。

完成后再回答一个延伸问题：如果把 `central_cache_default_size` 改成 64KB，central cache 的功能会坏吗？性能会怎样变化？（提示：功能不坏，只是更快溢出 1MB→堆，bump 快路径命中率下降，`make_node` 频率上升。）

## 6. 本讲小结

- `monotonic_buffer_resource` 是一条「可增长的内存带」，用 `head_`/`tail_` 做 **bump 分配**，靠 `std::align` 一并完成对齐与判满。
- 缓冲不够时**委托 upstream（默认堆）增长**，增长因子为 **×3/2**（`next_buffer_size`），靠 `make_node` 申请「对齐头 + 载荷」的新节点并挂到 `free_list_`。
- 单调缓冲**只能整体释放**：`deallocate` 是空操作；`release` 只回收自己长出来的 `free_list_` 节点，并**复位但不释放**借来的初始缓冲 `initial_buffer_`。
- `central_cache_allocator()` 是个单例：1MB `static std::array` 当初始缓冲 → `thread_safe_resource`（monotonic + `std::mutex`，靠名字隐藏加锁）→ `bytes_allocator(&res)` 对外返回。
- 这 1MB 装的是 **central cache 的元数据**（`chunk_t` 批量块、`node_t` 无锁栈节点），由 `central_cache_pool` 消费；溢出部分回落到堆。
- central cache 选 monotonic 是「天作之合」：它自己用无锁栈做块级回收，正好不需要 monotonic 的按块释放。

## 7. 下一步学习建议

- 下一讲 **u7-l3（block_pool 分层空闲链表 + `$new`/`$delete` + 容器分配器）** 会讲清楚「谁在调用 central cache」——即 thread-local 的 `block_pool` 如何分层、`get_regular_resource` 如何按尺寸（16B…64KB）分级路由，把本讲的 central cache 接到完整的分配链路上。
- 想提前理解 central cache 的块级回收机制，可先读 [include/libipc/concur/intrusive_stack.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/concur/intrusive_stack.h)（无锁 CAS 栈），那是 `central_cache_pool` 的 `cached_`/`aqueired_` 底层，也是 u8-l3 的内容。
- 若对「摊还分析」「内存池」感兴趣，建议对照阅读 C++ 标准库的 `std::pmr::monotonic_buffer_resource` 文档，体会 libipc 版本与标准版的异同（libipc 不要求继承、支持类型擦除持有）。
