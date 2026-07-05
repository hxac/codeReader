# 项目定位：什么是并发跳表 SkipMap/SkipSet

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标只有一个：**让你在没有读过任何源码之前，先建立起对 `crossbeam-skiplist` 这个 crate 的全局认识**。读完本讲你应该能够：

1. 用一句话说清楚 `SkipMap` / `SkipSet` 是什么、解决什么问题。
2. 理解跳表（skip list）作为**概率性有序数据结构**的定位，以及它为什么能支持并发。
3. 区分 `SkipMap` 与 `BTreeMap`、`HashMap` 的异同，知道什么场景该选谁。
4. 准确理解本 crate 最容易让人踩坑的并发模型：**方法接收 `&self` 而非 `&mut self`**、**单操作原子、多操作非原子**。

本讲几乎不涉及算法实现（那是第三单元的事），而是把 `src/lib.rs` 顶层模块文档里浓缩的项目哲学讲透。后面所有讲义都建立在这个全局观之上。

## 2. 前置知识

在进入正文前，先建立几个直观概念。

**什么是有序 map / set？**

- `map`（映射）存放「键值对」`key → value`，能按 key 查找、插入、删除。
- `set`（集合）只存放 key（可以理解为 value 为空类型的 map）。
- 「有序」是指元素按 key 的大小（更准确地说是按某个比较规则）排列，遍历时会按这个顺序逐个产出。Rust 标准库的 `BTreeMap` / `BTreeSet` 就是有序容器。

**什么是并发数据结构？**

普通的数据结构（如 `BTreeMap`）在被多个线程同时读写时是不安全的。最常见的解决办法是把它包进一把锁：

```rust
// 示例代码：用读写锁保护 BTreeMap
use std::sync::RwLock;
use std::collections::BTreeMap;
let shared: RwLock<BTreeMap<K, V>> = RwLock::new(BTreeMap::new());
```

这样做能保证安全，但所有线程在修改时必须**排队**（互斥），写多时会成为瓶颈。**并发数据结构**的目标就是：让多个线程能真正同时地推进操作，尽量不互相阻塞。`crossbeam-skiplist` 提供的 `SkipMap` / `SkipSet` 就是这样的并发有序容器。

**术语速查**

- **lock-free（无锁）**：线程不会被其它线程「卡死」等待，至少有一个线程总能取得进展。
- **原子操作（atomic）**：不可被线程切换打断的操作，要么完整发生，要么根本没发生。
- **use-after-free / double-free**：使用已释放的内存 / 重复释放同一块内存，都是未定义行为（UB）。

## 3. 本讲源码地图

本讲只涉及两个文件，且重点是它们的「文档」而非「实现」：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/lib.rs` | crate 的根模块，包含顶层文档与模块声明 | 第 1–229 行的顶层模块文档（项目哲学）与第 244–269 行的模块/feature 声明 |
| `README.md` | 面向用户的简介 | 项目一句话定位、依赖添加方式、MSRV |

实现细节集中在 `src/base.rs`（无锁算法）、`src/map.rs`（`SkipMap` 封装）、`src/set.rs`（`SkipSet` 封装），这些留给后续讲义。本讲你只需要记住：`base` 是底层的并发跳表原语，`map` 和 `set` 是对它的高层封装。

## 4. 核心概念与源码讲解

### 4.1 跳表的定位：什么是概率性有序数据结构

#### 4.1.1 概念说明

`crossbeam-skiplist` 提供两个主要类型 `SkipMap` 和 `SkipSet`，它们在 README 的开头被一句话定义：接口类似 `BTreeMap` / `BTreeSet`，但**支持跨多线程的安全并发访问**。

> This crate provides the types `SkipMap` and `SkipSet`. These data structures provide an interface similar to `BTreeMap` and `BTreeSet`, respectively, except they support safe concurrent access across multiple threads.
> —— [README.md:15-19](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/README.md#L15-L19)

这两句话出自 [src/lib.rs:1-7](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L1-L7)，是整个 crate 的开篇定义。注意几个关键词：

- **接口类似 BTreeMap/BTreeSet**：你会用 `BTreeMap` 就会用用 `SkipMap`，API 学习成本极低。
- **有序**：遍历产出元素时是按 key 排好序的（默认字典序）。
- **安全并发**：多线程同时读写不会引发内存错误。

底层的数据结构是**跳表（skip list）**。跳表是一种**概率性**有序数据结构：它不靠严格平衡（像红黑树、B 树那样），而是靠**随机数**来决定每个节点在多少层「快速通道」上出现，从而在期望意义下达到 \(O(\log n)\) 的查找/插入/删除复杂度。

为什么「概率性」对并发友好？因为平衡树在并发插入/删除时需要做**再平衡（rebalance）**——这往往要改很大一片节点，加锁范围大、冲突多。跳表没有再平衡，每个节点的高度在插入时独立随机决定，节点之间只需局部链接，这让「无锁」实现成为可能。这也是本 crate 选择跳表而不是红黑树的根本原因。

#### 4.1.2 核心流程

跳表的结构可以这样想象：底层是一条完整的有序链表，上面叠了若干层「稀疏索引」。每个节点随机决定自己「长多高」（出现在几层）：

```
Level 3:  HEAD ------------------------------> 50 -------------------------> NIL
Level 2:  HEAD ---------------> 25 ----------> 50 ----------> 75 ----------> NIL
Level 1:  HEAD ------> 12 ----> 25 ----> 37 -> 50 ----> 62 -> 75 ------> 88 -> NIL
Level 0:  HEAD -> 5 -> 12 -> 20 -> 25 -> 33 -> 37 -> 50 -> 55 -> 62 -> 75 -> 80 -> 88 -> NIL
```

查找时从**最高层、最左侧**出发，一路「向右、向下」：

1. 在当前层向右走，直到下一个节点 key ≥ 目标。
2. 若相等则找到；否则下降一层。
3. 重复，直到最底层。

因为高层是「快速通道」，每一步都能跳过大段节点，所以期望查找路径长度很短。

#### 4.1.3 源码精读

本讲不深入跳表节点实现（那是 `base.rs` 的事），但要理解为什么跳表能保证 \(O(\log n)\) 的期望复杂度。关键在于**节点高度的随机分布**。

设一个节点被提升到第 \(k\) 层的概率为 \(p^k\)（即每升一层概率为 \(p\)，本 crate 取 \(p = 1/2\)）。若共有 \(n\) 个节点，则第 \(k\) 层的期望节点数为：

\[
E[\text{nodes at level } k] = n \cdot p^k
\]

最高的「有用层」\(L\) 满足该层期望节点数约为 1：

\[
n \cdot p^L \approx 1 \quad\Rightarrow\quad L = \log_{1/p} n
\]

对 \(p = 1/2\)，有 \(L = \log_2 n\)。在每一层，查找平均只需比较常数次就下降一层，因此总比较次数为 \(O(\log n)\)。

> 注意：具体的 `random_height` 实现细节（xorshift、`trailing_zeros`）在 [src/base.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs) 中，本讲不展开，将在第三单元「插入路径」讲义（u3-l10）精读。

#### 4.1.4 代码实践

**实践目标**：亲手跑通 `SkipMap` 的最小示例，确认「有序 + 并发安全」这两点。

**操作步骤**：

1. 在你的项目 `Cargo.toml` 中按 [README.md:24-31](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/README.md#L24-L31) 加入依赖：

   ```toml
   [dependencies]
   crossbeam-skiplist = "0.1"
   crossbeam-utils = "0.8"   # 用到它的 scope
   ```

2. 复制 lib.rs 顶层文档里的 `SkipMap` 示例（[src/lib.rs:166-199](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L166-L199)）到 `src/main.rs` 运行。

**需要观察的现象**：插入顺序是 `Office Space / Pulp Fiction / The Godfather / The Blues Brothers`，但 `for entry in &movie_reviews` 遍历时**按字典序**输出。

**预期结果**：`get("Pulp Fiction")` 返回的 `Entry` 上 `key()` 为 `"Pulp Fiction"`、`value()` 为 `"Masterpiece."`；删除 `The Blues Brothers` 后再 `get` 返回 `None`。

**待本地验证**：实际打印顺序需你本地运行确认。

#### 4.1.5 小练习与答案

**练习 1**：跳表为什么用「随机高度」而不是像红黑树那样做严格平衡？

> **参考答案**：严格平衡在并发场景下需要大范围「再平衡」操作，会牵动很多节点、加锁冲突大；随机高度让每个节点的结构在插入时独立决定，只需局部改链，天然适合无锁并发，且在期望意义下复杂度仍是 \(O(\log n)\)。

**练习 2**：取 \(p = 1/2\)、\(n = 1024\)，估算跳表最高层的期望层数。

> **参考答案**：\(L = \log_2 n = \log_2 1024 = 10\)，即期望约有 10 层索引。

---

### 4.2 并发访问模型：`&self`、单操作原子与竞态

#### 4.2.1 概念说明

这是本讲**最重要、也最容易踩坑**的概念。lib.rs 的「Concurrent access」章节（[src/lib.rs:8-84](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L8-L84)）讲清了三件事：

1. `SkipMap` / `SkipSet` 实现了 `Send` + `Sync`，可以跨线程共享。
2. 修改类方法（如 `insert`）接收 **`&self`** 而非 `&mut self`，因此可以被**并发调用**。
3. 对 map 的**单个**操作是原子的，但**多个**操作之间不保证原子。

「接收 `&self`」是反直觉的：通常 `insert` 这种修改操作需要 `&mut self` 来独占。这里之所以能用 `&self`，是因为内部用原子操作（atomic）和 epoch 回收实现了「内部可变性」——多个线程可以同时通过同一个共享引用安全地修改数据结构。

#### 4.2.2 核心流程

并发访问的整体模型：

```
        线程 A: map.insert(k1, v1)          ┐
                                          ├── 同时进行，互不阻塞（lock-free）
        线程 B: map.insert(k2, v2)          ┘

        线程 A: map.get(k)   ──┐
                            ├── 单个操作原子完成
        线程 B: map.remove(k) ─┘  但跨操作的组合【不】原子
```

lib.rs 给出的关键论断（[src/lib.rs:43-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L43-L46)）：

> Concurrent access to skip lists is lock-free and sound. Threads won't get blocked waiting for other threads to finish operating on the map.

#### 4.2.3 源码精读

**（1）`&self` 让并发调用成为可能** —— 见 [src/lib.rs:12-15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L12-L15)：

```
//! Methods which mutate the map, such as [`insert`],
//! take `&self` rather than `&mut self`. This allows
//! them to be invoked concurrently.
```

正因为如此，lib.rs 的并发示例里变量甚至不需要 `mut`（[src/lib.rs:20-35](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L20-L35)）：两个线程通过 `scope` 同时往同一个 `person_ages` 里 `insert`。

**（2）单操作原子、多操作非原子** —— 见 [src/lib.rs:70-73](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L70-L73)：

> a _single_ operation on the map, such as `insert`, operates atomically: race conditions are impossible. However, concurrent calls to functions can become interleaved across threads, introducing non-determinism.

文档紧接着给了一个经典反例（[src/lib.rs:49-68](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L49-L68)）：一个线程 `remove(&5)`，主线程 `insert(5)` 后再 `contains(&5)`——这个 `contains` **可能返回 false**，因为另一线程可能在两次调用之间把 5 删掉了。这就是「竞态（race condition）」。

**（3）竞态是逻辑错误，不是内存错误** —— 见 [src/lib.rs:82-84](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L82-L84)：

```
//! Note that race conditions do not violate Rust's memory safety rules.
//! A race between multiple threads can never cause memory errors or
//! segfaults. A race condition is a _logic error_ in its entirety.
```

这一点至关重要：`crossbeam-skiplist` 保证了**内存安全**（不会段错误、use-after-free），但不保证**逻辑正确**——后者需要你自己避免在多行代码间假设状态不变。

#### 4.2.4 代码实践

**实践目标**：亲手复现 lib.rs 文档里的竞态示例，区分「内存安全」与「逻辑竞态」。

**操作步骤**：把 [src/lib.rs:49-68](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L49-L68) 的 `SkipSet` 示例改成循环多次（放大竞态窗口）：

```rust
// 示例代码：放大竞态以观察 contains 偶发失败
use crossbeam_skiplist::SkipSet;
use crossbeam_utils::thread::scope;

let numbers = SkipSet::new();
let mut failed = 0;
for _ in 0..1000 {
    numbers.insert(5);
    scope(|s| {
        s.spawn(|_| { numbers.remove(&5); });
        if !numbers.contains(&5) { failed += 1; }
    }).unwrap();
}
println!("contains 在竞态下返回 false 的次数: {}", failed);
```

**需要观察的现象**：`failed` 可能 > 0，但程序**永远不会崩溃或段错误**。

**预期结果**：`failed` 为某个非负整数（取决于调度），证明这是逻辑竞态而非内存错误。

**待本地验证**：`failed` 的具体数值取决于线程调度，无法预先确定。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SkipMap::insert` 用 `&self` 而不是 `&mut self`？

> **参考答案**：因为 `&mut self` 要求独占借用，无法被多个线程同时调用。本 crate 用原子操作 + epoch 回收实现内部可变性，所以只需共享引用 `&self` 即可安全并发修改。

**练习 2**：「单操作原子、多操作非原子」中的「原子」指的是什么？

> **参考答案**：指单个方法调用（如一次 `insert` 或一次 `remove`）作为一个整体完成，不会被其它线程的操作穿插打断，因此单个操作不会产生竞态。但「先 insert 再 contains」是两个操作，中间可能被其它线程插入任意操作，故不原子。

**练习 3**：`contains` 在竞态中返回 false，违反了 Rust 的内存安全吗？

> **参考答案**：没有。这只是逻辑上的竞态（race condition），不会引发段错误或内存错误；文档明确说明竞态是纯粹的逻辑错误。

---

### 4.3 内存管理：可变访问取舍与 epoch 垃圾回收

#### 4.3.1 概念说明

并发数据结构有两个绕不开的难题，lib.rs 各用一节回答：

1. **可变访问（Mutable access，[src/lib.rs:86-100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L86-L100)）**：为什么不提供 `get_mut`？
2. **垃圾回收（Garbage collection，[src/lib.rs:102-125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L102-L125)）**：删除一个节点后，如何保证还在读它的线程不会 use-after-free？

这两个问题其实是一体两面：都源于「同一份数据被多个线程同时访问」。

#### 4.3.2 核心流程

**为什么没有 `get_mut`**：如果允许通过 `get_mut` 拿到 `&mut V`，而读操作又能并发进行，就会产生**数据竞争（data race）**——这是真正的内存不安全。文档列出两种方案及其代价：

```
方案 A: 库内部给每个 value 包一把锁
        → 代价: 不再 lock-free、可能死锁、不需要可变访问的用户也被迫付锁的开销

方案 B（本 crate 选用）: 不提供可变访问，把控制权交给用户
        → 用户需要时可变时，自己用内部可变性: SkipMap<K, RwLock<V>>
```

**epoch 回收如何防 use-after-free**：考虑文档给的危险序列（[src/lib.rs:108-115](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L108-L115)）：

```
1. 线程 A 调用 get(k)，持有了指向 value 的引用
2. 线程 B 把 key=k 从 map 中 remove 掉
3. 线程 A 现在去访问那个 value
```

如果 remove 时立刻释放内存，第 3 步就 use-after-free。本 crate 用 [`crossbeam-epoch`](https://docs.rs/crossbeam-epoch) 实现的 **epoch-based memory reclamation**（基于纪元的内存回收）来规避：被删除的节点**不会立即释放**，而是等到「所有可能还在引用它的线程都已经离开当前纪元」之后才真正回收。

#### 4.3.3 源码精读

**（1）可变访问的取舍** —— [src/lib.rs:98-100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L98-L100) 给出官方推荐写法：

```
//! If mutable access is needed, then you can use interior mutability,
//! such as [`RwLock`]: `SkipMap<Key, RwLock<Value>>`.
```

即：把 value 类型本身设为带锁的（如 `RwLock<V>`），让「需要可变的人付锁的代价，不需要的人不付」。

**（2）epoch 回收的工作方式** —— [src/lib.rs:117-121](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L117-L121)：

```
//! To solve the above, this crate uses the _epoch-based memory reclamation_ mechanism
//! implemented in [`crossbeam-epoch`]. Simplified, a value removed from the map
//! is not freed until after all references to it have been dropped.
```

**（3）回收是自动的，但 `Entry` 句柄会推迟回收** —— [src/lib.rs:123-125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L123-L125)：

```
//! However, keep in mind that holding [`Entry`] handles to entries in the map will prevent
//! that memory from being freed until at least after the handles are dropped.
```

`get()` 返回的 `Entry` 是一个**句柄**，它持有对节点的引用。只要你还握着 `Entry`，对应的内存就不会被回收。这正是「线程 A 持有句柄时，线程 B 删除也不影响 A」的实现保障。具体的引用计数与 epoch 协作机制在 `base.rs`，将在第二单元「epoch 内存回收与引用计数」讲义（u2-l6）精读。

#### 4.3.4 代码实践

**实践目标**：验证「持有 `Entry` 句柄时，被删除的节点不会被立即回收」。

**操作步骤**：这是一个**源码阅读 + 思考型实践**，因为没有公开 API 能直接观察 epoch 回收时机。请按以下步骤思考并撰写说明：

1. 阅读 [src/lib.rs:102-125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L102-L125) 的「Garbage collection」章节。
2. 用文字回答：在「线程 A `get` → 线程 B `remove` → 线程 A 访问」这个序列里，`Entry` 句柄的引用计数分别在哪一步阻止了 use-after-free？

**预期结果**：你能用 2–3 句话说明——`get` 返回的 `Entry` 增加了节点的引用计数；`remove` 只是逻辑摘除（打标记），真正释放内存由 epoch 推进决定；只要 `Entry` 未 drop（引用计数 > 0），epoch 就不会回收它。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SkipMap` 不内置 `get_mut`，而要用户自己写 `SkipMap<K, RwLock<V>>`？

> **参考答案**：内置可变访问要么给每个 value 加锁（破坏 lock-free、可能死锁、让不需要可变访问的用户也付锁代价），要么允许数据竞争（内存不安全）。本 crate 选择把控制权交给用户，让真正需要可变访问的人自己承担锁的代价。

**练习 2**：epoch 回收与「自动 GC」（如 Java 的 GC）有何相同与不同？

> **参考答案**：相同点是都「延迟回收不再被引用的对象」。不同点是 epoch 回收只针对跳表内部的节点，通过「纪元」机制在确定安全时批量回收，无需 tracing、停顿更可控；它不是语言级的通用 GC。

---

### 4.4 选型指南：SkipMap vs BTreeMap vs HashMap

#### 4.4.1 概念说明

读完前几节，你可能会想：「既然有 `RwLock<BTreeMap>`，为什么还要 `SkipMap`？」lib.rs 的「Performance versus B-trees」（[src/lib.rs:127-142](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L127-L142)）和「Alternatives」（[src/lib.rs:144-154](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L144-L154)）两节专门回答了这个问题，并给出了**诚实**的选型建议——它甚至会告诉你跳表在哪些场景**更慢**。

#### 4.4.2 核心流程

选型决策树（综合 lib.rs 两节内容）：

```
你需要并发访问一个集合吗？
├─ 否 → 直接用 BTreeMap / HashMap / DashMap 等标准方案
└─ 是 → 你需要「有序」吗？
        ├─ 否 → 优先考虑无序并发 map（DashMap / flurry），通常更快
        └─ 是 → 写入频率高吗？
                ├─ 高 → SkipMap / SkipSet（无锁并发写，互斥代价小）
                └─ 低 → RwLock<BTreeMap> 可能更快（跳表常数因子大）
```

#### 4.4.3 源码精读

**（1）跳表相对 `RwLock<BTreeMap>` 的核心优势** —— [src/lib.rs:133-137](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L133-L137)：

```
//! The main benefit of a skip list over a `RwLock<BTreeMap>`
//! is that it allows concurrent writes to progress without
//! mutual exclusion. However, when the frequency
//! of writes is low, this benefit isn't as useful.
//! In these cases, a shared [`BTreeMap`] may be a faster option.
```

**（2）诚实警告：跳表可能更慢** —— [src/lib.rs:128-131](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L128-L131)：

> when you need concurrent writes to an ordered collection, skip lists are a reasonable choice. However, they can be substantially slower than B-trees in some scenarios.

**（3）最终建议：用基准测试决定** —— [src/lib.rs:139-142](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L139-L142)：在实践中性能因场景而异，最好的办法是在自己的应用里 benchmark。

**（4）Alternatives：如果不需要有序** —— [src/lib.rs:144-154](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L144-L154) 直接推荐了 `DashMap` 和 `flurry` 两个**无序**并发 map，并指出无序 map「往往比有序的更快」。这一点很关键：很多人选跳表只是因为没意识到自己其实不需要有序。

#### 4.4.4 代码实践

**实践目标**：把选型决策内化为可应用的判断。

**操作步骤**：针对以下三个真实场景，分别判断该用 `SkipMap`、`RwLock<BTreeMap>` 还是 `DashMap`：

| 场景 | 你的选择 | 理由 |
| --- | --- | --- |
| 高频交易撮合引擎的「价格档位 → 订单链表」，写极多 | （待你填写） | |
| 配置中心，启动时加载后几乎只读，偶尔更新 | （待你填写） | |
| 缓存「用户 ID → session」，不需要按 ID 排序 | （待你填写） | |

**预期结果**：

- 场景一 → `SkipMap`：有序（按价格）+ 高频并发写，正是跳表的主场。
- 场景二 → `RwLock<BTreeMap>`：写极少，跳表无锁优势用不上，B 树常数因子更小、更快。
- 场景三 → `DashMap`：不需要有序，无序并发 map 更快。

#### 4.4.5 小练习与答案

**练习 1**：什么情况下 `RwLock<BTreeMap>` 会比 `SkipMap` 更快？

> **参考答案**：当写入频率很低（读多写少）时，跳表「无锁并发写」的优势用不上，而 B 树的实现常数因子更小、缓存更友好，所以 `RwLock<BTreeMap>` 反而更快。

**练习 2**：一个场景是「缓存 `String → Vec<u8>`，不需要排序」，本 crate 合适吗？

> **参考答案**：不合适。不需要有序时应优先用无序并发 map（文档推荐的 `DashMap` 或 `flurry`），它们通常比跳表更快。本 crate 的价值正在「有序 + 并发」这个交集。

**练习 3**：文档为什么强调「最终要用 benchmark 决定」？

> **参考答案**：因为性能高度依赖具体的工作负载（数据规模、读写比、key 分布、硬件缓存层次等），理论上的优劣在不同场景可能反转，只有实测才能给出可靠结论。

## 5. 综合实践

把本讲的知识串起来，完成下面这个任务（本讲规格指定的核心实践）：

1. **阅读** [README.md](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/README.md) 与 [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs) 第 1–229 行的顶层模块文档。
2. **写一段约 200 字的中文总结**，回答：**在什么场景下应选择 `SkipMap` 而不是 `RwLock<BTreeMap>`**？要点应至少涵盖「有序」「并发写频率」「无锁 vs 互斥」「常数因子」这几个维度。
3. **举一个你自己工作/学习中的并发有序集合使用场景**（例如：实时排行榜、按时间戳排序的事件队列、带版本号的任务调度表等），分析为什么 `SkipMap` 比加锁的 `BTreeMap` 或无序的 `DashMap` 更适合。

**进阶（可选）**：把你在 4.2.4 中放大的竞态示例，和你总结里的场景结合起来——思考在你的场景中，会不会出现「单操作原子、多操作非原子」导致的竞态？如果会，你会如何设计（比如用 `Entry` 句柄、用 `lower_bound`/`upper_bound` 缩小窗口）来规避？这些手段的具体原理将在第四单元「Entry 与 RefEntry」讲义（u4-l12）和第五单元「并发语义」讲义（u5-l18）展开。

## 6. 本讲小结

- `crossbeam-skiplist` 提供 `SkipMap` / `SkipSet`，是**接口类似 `BTreeMap`/`BTreeSet`、但支持安全并发访问**的有序容器。
- 底层是**跳表**——一种靠随机高度决定节点层级的**概率性有序数据结构**，期望查找/插入/删除复杂度为 \(O(\log n)\)；它没有再平衡，天然适合无锁并发。
- 并发模型的核心：修改方法接收 **`&self`**（内部用原子操作 + epoch 实现内部可变性），**单个操作原子，多个操作之间不原子**。
- 竞态（race condition）是**逻辑错误**，不是内存错误——`crossbeam-skiplist` 保证内存安全，但不替你保证逻辑正确。
- 不提供 `get_mut`，需要可变访问时让用户自行用 `SkipMap<K, RwLock<V>>`；被删节点的回收由 **epoch-based reclamation** 延迟完成，`Entry` 句柄会推迟回收。
- 选型：**有序 + 高频并发写** 用 `SkipMap`；读多写少用 `RwLock<BTreeMap>` 可能更快；不需要有序时优先 `DashMap`/`flurry`；最终用 benchmark 决定。

## 7. 下一步学习建议

本讲建立了全局观，接下来建议：

1. **下一篇讲义（u1-l2）**：快速上手 `SkipMap` / `SkipSet` 的基本用法，把 lib.rs 的 doctest 示例跑起来，理解返回 `Entry` 句柄的设计。
2. **u1-l3**：理清 `base → map → set` 的模块分层与 `std/alloc/no_std` 三档 feature 门控。
3. 等第一单元的「会用」打好基础后，第二单元再进入 `base.rs` 的数据结构（`Node`/`Tower` 内存布局、epoch 回收细节），那时我们会回到源码层面，看本讲提到的「引用计数」「epoch 回收」究竟如何实现。

如果你想先有个直观体感，推荐现在就跳到 [src/lib.rs:166-229](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L166-L229) 的两个 doctest 示例，对照本讲的概念跑一遍。
