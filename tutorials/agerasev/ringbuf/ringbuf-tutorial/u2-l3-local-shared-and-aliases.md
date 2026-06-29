# LocalRb vs SharedRb 与 HeapRb/StaticRb 别名

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `LocalRb` 与 `SharedRb` 在「如何存储 read/write 索引」上的根本差异（`Cell` vs 原子），以及由此带来的「单线程 / 多线程」适用边界。
- 解释 `SharedRb` 为何要引入 `CachePadded`、`Acquire`/`Release` 等机制，以及为什么它「略慢于」`LocalRb`。
- 看懂 `LocalRb::split()` 默认产出 `Direct` 包装器、`SharedRb::split()` 默认产出 `Caching` 包装器这一关键差别。
- 读懂 `src/alias.rs` 里 `HeapRb` / `StaticRb` 及其 `HeapProd` / `HeapCons` / `StaticProd` / `StaticCons` 这一整套类型别名，理解「换一个存储后端就得到一个新缓冲区」的设计。

本讲承接 [u2-l2](u2-l2-storage-abstraction.md)（Storage 抽象）：我们已经知道数据存在 `Array` / `Heap` / `Ref` 等存储后端里；本讲回答的是「索引存在哪、怎么同步」，以及「仓库里那些常用名字到底指向什么类型」。

## 2. 前置知识

阅读本讲前，请确保你理解以下概念：

- **内部可变性（interior mutability）**：普通 `&T` 不允许修改数据；`Cell<T>` / `AtomicUsize` 这类类型允许通过 `&self` 改变内部值。这是 `LocalRb` 与 `SharedRb` 各自能让 `Observer::read_index(&self)` 这类「只读借用却要读出可能变化的索引」方法成立的基础。
- **`Cell<T>`**：用于 `Copy` 类型的「整值替换」内部可变性，**没有**任何线程同步语义，只能单线程使用。读写它就是一次普通内存访问，开销极低。
- **原子类型（`AtomicUsize` 等）与内存序（`Ordering`）**：原子操作保证多线程下读写的可见性与原子性；`Acquire` 读取、`Release` 写入用来建立跨线程的「先于（happens-before）」关系。本讲只需直观理解，深入的内存顺序分析留待 [u5-l1](u5-l1-lockfree-atomics-ordering.md)。
- **2\*capacity 模运算**：read/write 索引在 `0..2*capacity` 区间取值，详见 [u2-l1](u2-l1-indices-modular-arithmetic.md)。本讲的两种实现共用这套算术，差别仅在「索引用什么类型存」。
- **SPSC 不变量**：至多一个生产者、至多一个消费者，由 hold 标志运行时强制（见 [u1-l1](u1-l1-project-overview.md)）。
- **Storage trait**：抽象「一段连续存放 `MaybeUninit<T>` 的内存」，详见 [u2-l2](u2-l2-storage-abstraction.md)。

一句话先建立直觉：**`LocalRb` 与 `SharedRb` 是同一套环形缓冲区算法的两种「索引存储」实现**——前者用 `Cell` 存索引、只能单线程；后者用 `CachePadded<AtomicUsize>` 存索引、可跨线程共享。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/rb/local.rs` | `LocalRb` 单线程实现：索引用 `Cell`，hold 标志用 `Cell<bool>` |
| `src/rb/shared.rs` | `SharedRb` 多线程实现：索引用 `CachePadded<AtomicUsize>`，hold 标志用 `AtomicBool` |
| `src/alias.rs` | 一组类型别名：`StaticRb`、`HeapRb` 及对应 `Prod`/`Cons` |
| `src/rb/mod.rs` | `rb` 模块入口，re-export `LocalRb` / `SharedRb` |
| `src/wrap/direct.rs` | `Direct` 包装器（别名 `Prod`/`Cons`/`Obs`），`LocalRb::split` 默认产出它 |
| `src/wrap/caching.rs` | `Caching` 包装器（别名 `CachingProd`/`CachingCons`），`SharedRb::split` 默认产出它 |
| `examples/static.rs` | 一个 `#![no_std]` 示例：用 `StaticRb` + `split_ref` 完成 push/pop |
| `src/lib.rs` | 库文档，含「类型」「性能」「实现细节」三段说明 |

## 4. 核心概念与源码讲解

### 4.1 LocalRb：单线程的 Cell 实现

#### 4.1.1 概念说明

`LocalRb` 是 ringbuf 的**单线程**环形缓冲区。它解决的问题是：当你确定生产与消费都发生在同一线程（或同一不可重入的执行上下文）时，完全不需要原子操作与跨核缓存同步，索引只需要最朴素的 `Cell`。

`Cell<T>` 提供「整值替换」式的内部可变性：通过 `&self` 也能 `get()` 读出、`set()` 写入一个 `Copy` 值。它的代价是**零同步开销**，代价是**绝对不能跨线程共享**（`Cell` 既不是 `Sync`）。这正好契合「单线程」这一前提。

库文档对它的定位很明确：

> `LocalRb`. Only for single-threaded use.
> Slightly faster than multi-threaded version because it doesn't synchronize cache.

#### 4.1.2 核心流程

`LocalRb` 把每个端点（读端 / 写端）打包成一个小结构 `Endpoint`，内含两个 `Cell`：一个存索引、一个存 hold 标志。整条数据通路可以概括为：

1. **读索引**：`read_index()` 调 `self.read.index.get()`——一次普通内存读。
2. **写索引**：`write_index()` 调 `self.write.index.get()`——同样是一次普通内存读。
3. **推进写索引**（push 后）：`set_write_index(v)` 调 `self.write.index.set(v)`——一次普通内存写。
4. **推进读索引**（pop 后）：`set_read_index(v)` 调 `self.read.index.set(v)`。
5. **hold 标志**：`hold_read/hold_write(flag)` 调 `held.replace(flag)`，返回旧值，用于拆分时断言「这一端还没被人占」。

因为没有原子、没有内存序，这些操作的代价与读写一个普通局部变量相当。索引算术（`% (2*capacity)`、`ranges()`）与 `SharedRb` 完全一致，由共享的 `Observer`/`Producer`/`Consumer`/`RingBuffer` trait 驱动。

#### 4.1.3 源码精读

先看结构体定义。`LocalRb` 由两个 `Endpoint` 加一个存储后端 `S` 组成：

[src/rb/local.rs:22-43](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L22-L43) —— `Endpoint` 把索引与 hold 标志各包进一个 `Cell`；`LocalRb<S>` 持有 read / write 两个端点和存储 `storage: S`。注意文档注释明确写了「single-threaded use only」「doesn't synchronize cache」。

`Observer` 的实现里，读索引就是一次 `Cell::get`，**没有任何 `Ordering`**：

[src/rb/local.rs:80-86](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L80-L86) —— `read_index()` / `write_index()` 直接取 `Cell` 值。

写索引推进同样直白：

[src/rb/local.rs:107-119](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L107-L119) —— `Producer::set_write_index` 与 `Consumer::set_read_index` 就是 `Cell::set`。

hold 标志用 `replace` 实现「置位并返回旧值」：

[src/rb/local.rs:121-130](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L121-L130) —— `hold_read/hold_write` 调 `Cell::replace`，这是后续 split 时判断「是否重复拆分」的基础（深入机制见 [u5-l2](u5-l2-hold-flags-spsc-invariant.md)）。

最后看拆分。`LocalRb::split()` 把自身包进 `Rc`，返回 **`Direct` 包装器** `Prod<Rc<Self>>` / `Cons<Rc<Self>>`：

[src/rb/local.rs:138-155](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L138-L155) —— `Split::Prod = Prod<Rc<Self>>`，这里的 `Prod` 来自 `crate::wrap::Prod`，即 `Direct` 包装器（见 `src/wrap/direct.rs`）。因为 `Cell` 本身没有同步成本，所以单线程下「每次操作都立即读写底层索引」的 Direct 策略是最简单也最合适的选择。

> 小结：`LocalRb` = `Cell` 索引 + `Direct` 拆分。它的「快」来自两点——索引读写是普通内存访问、且默认包装器是「立即同步」的 Direct（无需缓存/延迟同步那一套）。

#### 4.1.4 代码实践

**实践目标**：用 `LocalRb` 跑通单线程 push/pop，并与上一讲的 `HeapRb` 体验对比。

**操作步骤**：

1. 在仓库根目录新建一个临时 example 文件（注意：仓库本身没有 `LocalRb` 的示例，下面这段为**示例代码**，便于你本地试跑）：

```rust
// 示例代码：local_demo.rs（放在仓库根目录，用 cargo run --example local_demo 运行需要 alloc feature）
use ringbuf::{traits::*, LocalRb, storage::Heap};

fn main() {
    let mut rb = LocalRb::<Heap<i32>>::new(2);
    let (mut prod, mut cons) = rb.split(); // 拿走所有权，包进 Rc，返回 Direct 的 Prod/Cons

    assert_eq!(prod.try_push(10), Ok(()));
    assert_eq!(prod.try_push(20), Ok(()));
    assert_eq!(prod.try_push(30), Err(30)); // 满了，原样退回

    assert_eq!(cons.try_pop(), Some(10));   // FIFO
    assert_eq!(cons.try_pop(), Some(20));
    assert_eq!(cons.try_pop(), None);       // 空了
}
```

2. 运行：`cargo run --example local_demo`（确保 `alloc` feature 开启，默认即开启）。

**需要观察的现象**：行为与 [u1-l2](u1-l2-quickstart-heaprb.md) 的 `HeapRb` 完全一致——满则 `Err`、空则 `None`、FIFO 顺序。区别只在「真身类型」从 `SharedRb<Heap<T>>` 换成了 `LocalRb<Heap<T>>`。

**预期结果**：程序无断言失败、正常结束。这印证了「两种实现的对外行为一致，差异仅藏在索引存储与线程安全里」。

> 注意：`LocalRb` 不是 `Sync`，因此 `prod` / `cons` 不能 `move` 进另一个线程。若你尝试 `std::thread::spawn(move || { prod.try_push(1); })`，编译器会直接拒绝——这正是 `Cell` 单线程限制的体现。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面示例里 `rb` 在 `split` 之后再调用一次 `rb.split()`，会发生什么？为什么？

> **答案**：编译不通过——`split(self)` 拿走了 `rb` 的所有权，`rb` 在 `split` 之后已不可用。若改成对 `Rc<LocalRb<_>>` 调用 `split()`，第二次拆分会在运行时 panic，因为 `Direct::new` 会断言 hold 标志未被占用（`hold_write(true)` 返回的旧值必须为 `false`）。

**练习 2**：`LocalRb` 的 `read_index()` 为什么能通过 `&self` 返回一个会变化的值？

> **答案**：因为索引存在 `Cell<usize>` 里，`Cell` 提供内部可变性，允许 `&self` 上调用 `get()` 读出当前值。这也是 `Observer` 的只读方法签名都只借用 `&self` 的根本原因。

---

### 4.2 SharedRb：多线程的 CachePadded\<Atomic\> 实现

#### 4.2.1 概念说明

`SharedRb` 是 ringbuf 的**多线程**环形缓冲区，也是绝大多数实际场景（`HeapRb` / `StaticRb` 都是它的别名）所用的实现。它的索引不再是 `Cell`，而是 `CachePadded<AtomicUsize>`——「被缓存行对齐包装的原子整数」。

引入两个新机制：

- **原子操作**：多线程下，读写一个共享的 `usize` 必须是原子的，否则会丢更新。`AtomicUsize` 保证这一点，并通过 `Ordering`（`Acquire` 读 / `Release` 写）建立跨线程可见性顺序。
- **`CachePadded`**：来自 `crossbeam-utils`，把一个值填充（padding）到一整条 CPU 缓存行。目的是避免「伪共享（false sharing）」——若 read/write 索引挤在同一条缓存行上，两个核分别频繁改写它们时会让该缓存行在核间反复失效/同步，造成巨大开销。把它们各占一条缓存行，互不干扰。

库文档对它的描述：

> `SharedRb`. Can be shared between threads.
> `SharedRb` needs to synchronize CPU cache between CPU cores. This synchronization has some overhead.

这就是 `SharedRb`「略慢于 `LocalRb`」的根源：每一次 `load`/`store` 都可能触发跨核缓存同步。为了把这份开销分摊掉，`SharedRb::split()` 默认给出的是 **`Caching` 包装器**——它会把索引缓存在本地、只在必要时才与底层同步（详见 4.3）。

#### 4.2.2 核心流程

`SharedRb` 的字段全部是 `Sync` 类型，所以整个结构体是 `Send + Sync`，可以被 `Arc` 共享。数据通路：

1. **读索引**：`read_index()` = `self.read_index.load(Ordering::Acquire)`——原子读取，保证看到对方最新发布的写索引。
2. **写索引**：`write_index()` = `self.write_index.load(Ordering::Acquire)`。
3. **推进写索引**：`set_write_index(v)` = `self.write_index.store(v, Ordering::Release)`——`Release` 保证此前写入的数据元素先于索引对消费者可见。
4. **推进读索引**：`set_read_index(v)` = `self.read_index.store(v, Ordering::Release)`。
5. **hold 标志**：`hold_read/hold_write(flag)` = `held.swap(flag, Ordering::AcqRel)`——原子地「置新值、返回旧值」。
6. **拆分**：`split()` 把自身包进 `Arc`，返回 **`Caching` 包装器** `CachingProd<Arc<Self>>` / `CachingCons<Arc<Self>>`。

关于线程安全，有一处容易被忽略的设计：`SharedRb` 并不要求 `T: Send`。文档里有一段关键说明：

> Note that there is no explicit requirement of `T: Send`. Instead ring buffer will work just fine even with `T: !Send` until you try to send its producer or consumer to another thread.

也就是说，「元素能否跨线程移动」的约束被推迟到「把 Producer/Consumer 送进另一线程」的那一刻——由包装器自身的 `Send`/`Sync` 推导（依赖 `T: Send`）来把关。这一点更深的分析留待 [u5-l2](u5-l2-hold-flags-spsc-invariant.md)。

#### 4.2.3 源码精读

先看结构体与依赖。注意 `CachePadded` 的引入，以及 `portable-atomic` feature 下原子类型来源的切换：

[src/rb/shared.rs:18-23](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L18-L23) —— 引入 `crossbeam_utils::CachePadded`；原子类型在默认情况下用 `core::sync::atomic`，开启 `portable-atomic` 时改用 `portable_atomic`（后者让 ringbuf 能跑在没有 64 位原子指令的小型目标上，详见 [u8-l1](u8-l1-features-portable-atomic-no-std.md)）。

[src/rb/shared.rs:27-57](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L27-L57) —— 文档注释点明「不要求 `T: Send`」并给出一个两线程使用的内嵌示例；结构体本身有五个字段：两个 `CachePadded<AtomicUsize>`（读/写索引）、两个 `AtomicBool`（hold 标志）、以及存储 `storage: S`。

`Observer` 实现里，读索引带上 `Acquire` 内存序：

[src/rb/shared.rs:96-102](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L96-L102) —— `read_index()` / `write_index()` 用 `load(Ordering::Acquire)`，确保读到对方 `Release` 发布的值时，对方在此之前写入的数据也已对己方可见。

写索引推进用 `Release`，hold 标志用 `AcqRel` 的 `swap`：

[src/rb/shared.rs:123-146](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L123-L146) —— `Producer::set_write_index` / `Consumer::set_read_index` 用 `store(..., Release)`；`RingBuffer::hold_read/hold_write` 用 `swap(flag, AcqRel)`。对比 [src/rb/local.rs:107-130](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L107-L130)，可以看到同样的语义在 `LocalRb` 里只是 `Cell::set` / `Cell::replace`，没有内存序——这正是两种实现的核心对照。

拆分默认产出 `Caching` 包装器：

[src/rb/shared.rs:154-171](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L154-L171) —— `Split::Prod = CachingProd<Arc<Self>>`，`Split::Cons = CachingCons<Arc<Self>>`。包进 `Arc` 是为了两端能各自持有一份引用计数、跨线程移动；选用 `CachingProd/Cons` 而非 `Direct`，是为了把昂贵的原子同步尽量推迟、合并。

#### 4.2.4 代码实践

**实践目标**：用 `SharedRb` 在两个线程之间传递数据，体会它在多线程下可用、而 `LocalRb` 不可用。

**操作步骤**：

1. 直接运行 `src/rb/shared.rs` 文档注释里那段两线程示例（它就是官方示例）。在仓库根目录新建 `examples/shared_demo.rs`，内容照抄文档示例（**示例代码**）：

```rust
// 示例代码：shared_demo.rs（required-features: std）
use std::thread;
use ringbuf::{SharedRb, storage::Heap, traits::*};

fn main() {
    let rb = SharedRb::<Heap<i32>>::new(256);
    let (mut prod, mut cons) = rb.split();
    thread::spawn(move || {
        prod.try_push(123).unwrap();
    })
    .join();
    thread::spawn(move || {
        assert_eq!(cons.try_pop().unwrap(), 123);
    })
    .join();
}
```

2. 运行：`cargo run --release --example shared_demo`（需 `std` feature，默认开启）。

**需要观察的现象**：程序正常结束、无 panic。关键点在于 `prod` 和 `cons` 分别被 `move` 进了**不同的线程**——这在 `LocalRb` 下会编译失败，在 `SharedRb` 下却完全合法。

**预期结果**：断言通过，`123` 被生产者线程写入、消费者线程读出。若把 `SharedRb::<Heap<i32>>` 换成 `LocalRb::<Heap<i32>>` 再编译，编译器会报 `Rc` 不能跨线程的错误（因为 `LocalRb::split` 返回的是 `Prod<Rc<...>>`，`Rc` 不是 `Send`），直观印证两者线程安全边界。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SharedRb` 的 `read_index` / `write_index` 要分别各占一个 `CachePadded`，而不是合并成一个含两个原子字段的结构体？

> **答案**：为了避免伪共享。生产者频繁写 `write_index`、消费者频繁写 `read_index`；若它们同处一条缓存行，一方的写会让该行在另一核失效，触发无谓的跨核同步。`CachePadded` 把它们撑到各自独占一条缓存行，互不干扰。

**练习 2**：读取索引用 `Acquire`、写入索引用 `Release`，这对「消费者能看到生产者刚写入的元素」为什么是必要的？

> **答案**：`Release` store 保证「写元素」发生在「写索引」对其它线程可见之前；`Acquire` load 保证读到该索引后，「写元素」的内容对己方可见。两者配合建立了「先写数据、后公布索引、对方看到索引即可安全读数据」的 happens-before 关系。深入分析见 [u5-l1](u5-l1-lockfree-atomics-ordering.md)。

---

### 4.3 两种实现的对比：差异、性能与拆分策略

#### 4.3.1 概念说明

`LocalRb` 与 `SharedRb` 实现的是**同一套**环形缓冲区算法（相同的 `ranges()`、相同的 2\*capacity 模运算、相同的 `Observer`/`Producer`/`Consumer`/`RingBuffer` trait），对外行为完全一致。它们的差别集中在三个方面：

1. **索引与 hold 标志的存储类型**（`Cell` vs 原子）。
2. **线程安全边界**（单线程 vs 跨线程共享）。
3. **`split()` 默认产出的包装器**（`Direct` vs `Caching`）。

第三个差别尤其值得注意，它把「索引存储的代价」与「拆分策略」联系了起来：单线程下索引读写几乎免费，于是默认用「立即同步」的 `Direct`；多线程下每次原子同步都很贵，于是默认用「按需同步」的 `Caching` 来分摊开销。

#### 4.3.2 核心流程（对照表）

| 维度 | `LocalRb` | `SharedRb` |
|------|-----------|------------|
| 索引存储 | `Cell<usize>` | `CachePadded<AtomicUsize>` |
| hold 标志存储 | `Cell<bool>` | `AtomicBool` |
| 读索引实现 | `Cell::get` | `AtomicUsize::load(Acquire)` |
| 写索引实现 | `Cell::set` | `AtomicUsize::store(Release)` |
| hold 实现 | `Cell::replace` | `AtomicBool::swap(AcqRel)` |
| 跨核缓存同步 | 无 | 有（每次原子 load/store 可能触发） |
| 是否 `Send + Sync` | 否（仅单线程） | 是（可 `Arc` 共享、跨线程） |
| `split()` 智能指针 | `Rc` | `Arc` |
| `split()` 默认包装器 | `Direct`（`Prod`/`Cons`） | `Caching`（`CachingProd`/`CachingCons`） |
| 性能定位 | 略快（无缓存同步） | 通用、可多线程，略慢 |
| 原子后端可替换 | 不涉及 | `portable-atomic` feature 下换 `portable_atomic` |

两条引用印证性能定位：`src/lib.rs` 的「Types」段把 `LocalRb` 标注为「Only for single-threaded use」、把 `SharedRb` 标注为「Can be shared between threads」——见 [src/lib.rs:10-19](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L10-L19)；「Performance」段明确说 `LocalRb` 因无需缓存同步而略快——见 [src/lib.rs:21-28](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L21-L28)。

#### 4.3.3 源码精读（拆分策略的对照）

「默认包装器不同」这一差别，是连接本讲与 [u4](u4-l1-wrap-rbref-abstraction.md) 包装器章节的关键线索。两段拆分实现并排看最清楚：

- `LocalRb` 用 `Rc` + `Direct`：[src/rb/local.rs:138-146](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L138-L146)。
- `SharedRb` 用 `Arc` + `Caching`：[src/rb/shared.rs:154-162](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L154-L162)。

两种包装器的语义由各自的模块文档一语道破：

- `Direct`：「All changes are synchronized with the ring buffer immediately」——[src/wrap/direct.rs:1-3](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L1-L3)。
- `Caching`：「Fetches changes from the ring buffer only when there is no more slots to perform requested operation」——[src/wrap/caching.rs:1-3](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L1-L3)。

换句话说：`LocalRb` 因为索引读写几乎免费，所以选「立即同步」最简单；`SharedRb` 因为每次同步都要付出跨核代价，所以选「按需同步」来把多次小同步合并成少数几次。这是一种「让数据结构的存储代价驱动默认包装器选择」的精巧设计。

#### 4.3.4 代码实践

**实践目标**：通过「编译期拒绝」直观验证两者的线程安全边界。

**操作步骤**：

1. 把 4.1.4 的 `LocalRb` 示例里 `let (mut prod, mut cons) = rb.split();` 之后，加上一句 `std::thread::spawn(move || { let _ = prod.try_push(1); });`。
2. 执行 `cargo build --example local_demo`。

**需要观察的现象**：编译失败，错误信息大致是 `Rc<...>` 不能安全跨线程发送（`Rc` 未实现 `Send`）。

**预期结果**：编译报错。原因是 `LocalRb::split` 返回 `Prod<Rc<LocalRb<_>>>`，`Rc` 不是 `Send`。把同样两行换成 `SharedRb`（4.2.4 的示例）则能编译通过——这就是「单线程 vs 多线程」在类型层面的强制体现，不需要运行时检查。

#### 4.3.5 小练习与答案

**练习 1**：既然 `LocalRb` 更快，为什么不全部用 `LocalRb`？

> **答案**：`LocalRb` 只能单线程使用（`Cell` 非 `Sync`，`split` 返回 `Rc` 非 `Send`）。只要生产与消费可能发生在不同线程（最常见的跨线程通信场景），就只能用 `SharedRb`。「快」是有前提的——前提是「确定单线程」。

**练习 2**：为什么 `SharedRb` 默认用 `Caching` 而非 `Direct` 包装器？

> **答案**：`SharedRb` 的每次索引同步都是带内存序的原子操作，可能触发跨核缓存同步，代价高。`Caching` 包装器把索引缓存在本地、只在「没有空槽/没有数据」时才与底层同步，能大幅减少同步次数。`Direct` 每次操作都立即同步，在多线程下会放大这份开销。深入对比见 [u4-l4](u4-l4-caching-wrapper.md)。

---

### 4.4 类型别名系统：HeapRb 与 StaticRb

#### 4.4.1 概念说明

读到这里你会发现：无论是 `LocalRb` 还是 `SharedRb`，都是泛型 `Rb<S>`——真正决定「数据存哪」的是存储后端 `S`（见 [u2-l2](u2-l2-storage-abstraction.md)）。但每次写 `SharedRb<Heap<i32>>` 或 `SharedRb<Array<i32, 8>>` 太啰嗦，于是 `src/alias.rs` 提供了一组**类型别名（type alias）**，给最常用的组合起短名字：

- `HeapRb<T>` = `SharedRb<Heap<T>>`：堆分配、运行期容量，需 `alloc`。
- `StaticRb<T, N>` = `SharedRb<Array<T, N>>`：静态/栈分配、编译期容量 `N`，无需堆，可用于 `no_std`。

注意：**`HeapRb` 和 `StaticRb` 都是 `SharedRb` 的别名**，因此它们都是多线程安全的；`LocalRb` 没有对应的「常用别名」，需要时直接写 `LocalRb<Heap<T>>` 等。对应的 Producer/Consumer 也有别名（`HeapProd`/`HeapCons`/`StaticProd`/`StaticCons`），它们都是 `CachingProd`/`CachingCons` 的别名——这正是 `SharedRb::split` 的返回类型。

#### 4.4.2 核心流程

别名体系可以这样理解：选一个缓冲区类型 = 选一个存储后端，再由 `split()` 的返回类型自动决定 Producer/Consumer 别名。

```
SharedRb<Array<T, N>>  ──别名──►  StaticRb<T, N>
        │ split()                         （Caching 包装 + & / &'static）
        ▼
  CachingProd<...>  ──别名──►  StaticProd<'a, T, N>
  CachingCons<...>  ──别名──►  StaticCons<'a, T, N>

SharedRb<Heap<T>>       ──别名──►  HeapRb<T>      (cfg alloc)
        │ split()                         （Caching 包装 + Arc）
        ▼
  CachingProd<Arc<HeapRb<T>>>  ──别名──►  HeapProd<T>
  CachingCons<Arc<HeapRb<T>>>  ──别名──►  HeapCons<T>
```

两个细节值得记住：

1. **`StaticProd`/`StaticCons` 带生命周期 `'a` 并基于 `&'a StaticRb`**，因为静态缓冲区通常用 `split_ref()`（借用）拆分，不引入 `Arc`；而 `HeapProd`/`HeapCons` 基于 `Arc<HeapRb<T>>`，因为 `HeapRb` 通常用 `split()`（拿走所有权、包进 `Arc`）拆分。
2. **`Arc` 的来源会随 feature 切换**：默认用 `alloc::sync::Arc`；开启 `portable-atomic` 时改用 `portable_atomic_util::Arc`（用于无原生 64 位原子的目标）。

#### 4.4.3 源码精读

整个别名文件很短，逐段看：

[src/alias.rs:9-12](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L9-L12) —— `Arc` 的条件 re-export：默认走 `alloc::sync::Arc`，`portable-atomic` feature 下走 `portable_atomic_util::Arc`。这决定了下面 `HeapProd`/`HeapCons` 里用到的 `Arc` 到底是哪一个。

[src/alias.rs:14-23](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L14-L23) —— `StaticRb<T, N> = SharedRb<Array<T, N>>`（`const N` 是编译期容量）；`StaticProd<'a, T, N>` / `StaticCons<'a, T, N>` 都基于 `&'a StaticRb<T, N>`，对应 `split_ref()` 的返回。文档特别提醒「Capacity (`N`) must be greater than zero」。

[src/alias.rs:25-35](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L25-L35) —— `HeapRb<T> = SharedRb<Heap<T>>`（整个块在 `#[cfg(feature = "alloc")]` 下，因为 `Heap` 存储依赖堆）；`HeapProd<T>` / `HeapCons<T>` 基于 `Arc<HeapRb<T>>`，对应 `split()` 的返回。

构造器则由宏统一生成（见 [u8-l3](u8-l3-macros-constructors-batch-traits.md)）：`StaticRb::<T, N>::default()` 生成一个空缓冲区，`HeapRb::<T>::new(capacity)` 在堆上分配指定容量——见 [src/rb/macros.rs:1-51](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/macros.rs#L1-L51)。

最后，这些别名都通过 `src/lib.rs` 的 `pub use alias::*;` 暴露在 crate 顶层——见 [src/lib.rs:176](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L176)，所以你可以直接 `use ringbuf::{HeapRb, StaticRb};`。

#### 4.4.4 代码实践

**实践目标**：用 `StaticRb` 跑一个**无需堆分配**的 `#![no_std]` 示例，体会「静态存储后端」的价值。

**操作步骤**：

1. 阅读官方示例 `examples/static.rs`：[examples/static.rs:1-15](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/static.rs#L1-L15)。注意三点：开头 `#![no_std]`、用 `StaticRb::<i32, 1>::default()` 创建、用 `split_ref()` 借用拆分。
2. 运行：`cargo run --example static`。
3. 对照：尝试 `cargo run --no-default-features --example static`，观察它**仍然能编译运行**（因为 `static` 示例没有 `required-features` 门控，不依赖 `alloc`/`std`）。

**需要观察的现象**：`try_push(123)` 成功返回 `Ok(())`；容量为 1 时第二次 `try_push(321)` 返回 `Err(321)`；`try_pop()` 先得 `Some(123)`、再得 `None`。

**预期结果**：断言全部通过。这个示例印证了「`StaticRb` = `SharedRb<Array<T,N>>`」在无堆环境下完全可用——这正是嵌入式 / `no_std` 场景选 `StaticRb` 的原因。

> 对照思考：`HeapRb` 的 `new(capacity)` 需要堆分配（`alloc` feature），所以无法出现在这个 `#![no_std]` 示例里；而 `StaticRb` 的容量由 `const N` 在编译期固定，零堆分配。

#### 4.4.5 小练习与答案

**练习 1**：写出 `HeapRb<u8>`、`StaticRb<u8, 4>` 各自展开后的完整类型。

> **答案**：`HeapRb<u8>` = `SharedRb<Heap<u8>>`；`StaticRb<u8, 4>` = `SharedRb<Array<u8, 4>>`。两者都是 `SharedRb` 的别名，因此都是多线程安全的。

**练习 2**：为什么 `StaticProd`/`StaticCons` 带生命周期参数 `'a`，而 `HeapProd`/`HeapCons` 不带？

> **答案**：静态缓冲区通常用 `split_ref(&mut self)` 借用拆分，返回的 Producer/Consumer 持有 `&'a StaticRb`（引用），生命周期 `'a` 绑定到借用；堆缓冲区通常用 `split(self)` 拿走所有权、包进 `Arc`，返回 `CachingProd<Arc<HeapRb<T>>>` 持有的是 `Arc`（独立的所有权），不再借用外部引用，因此无需生命周期参数。

---

## 5. 综合实践

把本讲的知识串起来：写一个小程序，**同时**演示 `LocalRb`、`SharedRb`、`StaticRb`、`HeapRb` 四者，并对比它们的类型与线程安全边界。

任务步骤：

1. **LocalRb 单线程段**：用 `LocalRb::<Heap<i32>>::new(4)` 创建，`split()` 后单线程 `try_push` 0..3、`try_pop` 验证 FIFO。打印 `prod` 的类型名（`std::any::type_name::<_>()`），确认它是 `Prod<Rc<LocalRb<Heap<i32>>>>`（即 `Direct` + `Rc`）。
2. **SharedRb 多线程段**：用 `SharedRb::<Heap<i32>>::new(4)` 创建，`split()` 后把 `prod` 和 `cons` 分别 `move` 进两个 `std::thread`，跨线程传递一个值。打印类型名，确认是 `CachingProd<Arc<SharedRb<Heap<i32>>>>`（即 `Caching` + `Arc`）。
3. **StaticRb 无堆段**：参考 `examples/static.rs`，用 `StaticRb::<i32, 2>::default()` + `split_ref()` 完成 push/pop（这一段可以独立放进一个 `#![no_std]` 二进制验证无堆）。
4. **观察记录**：在一份注释里写清楚——`HeapRb` 与 `StaticRb` 都是 `SharedRb` 的别名，区别仅在存储后端（`Heap` vs `Array`）；`LocalRb` 是独立的单线程实现，没有别名。

**预期结果**：三段都能正常运行；类型名打印能让你亲眼看到 `Rc` vs `Arc`、`Direct(Prod)` vs `Caching(CachingProd)` 的差别。如果尝试把第 1 段的 `prod` move 进线程，编译会失败——这正好印证 `LocalRb` 的单线程边界。

> 提示：第 2 段跨线程通信的「等待」语义（满/空时的阻塞）需要派生 crate（`async-ringbuf` / `ringbuf-blocking`），本讲的 `try_push`/`try_pop` 是非阻塞的，满则 `Err`、空则 `None`。

## 6. 本讲小结

- `LocalRb` 与 `SharedRb` 实现的是**同一套**环形缓冲区算法，差异只在「索引与 hold 标志用什么存」。
- `LocalRb` 用 `Cell`（`Cell<usize>` 索引、`Cell<bool>` hold），无原子、无跨核同步，**只能单线程**，略快。
- `SharedRb` 用 `CachePadded<AtomicUsize>`（防伪共享）+ `AtomicBool`，配 `Acquire` 读 / `Release` 写 / `AcqRel` swap，**可跨线程** `Arc` 共享，是 `HeapRb` / `StaticRb` 的真身。
- 一个关键而隐蔽的差别：`LocalRb::split()` 默认产出 **`Direct` 包装器**（`Prod`/`Cons` + `Rc`），`SharedRb::split()` 默认产出 **`Caching` 包装器**（`CachingProd`/`CachingCons` + `Arc`）——这是「索引存储代价驱动包装器选择」的结果。
- `src/alias.rs` 用类型别名把常用组合命名：`StaticRb<T,N> = SharedRb<Array<T,N>>`、`HeapRb<T> = SharedRb<Heap<T>>`，及配套的 `StaticProd/Cons`（带生命周期、基于引用）和 `HeapProd/Cons`（基于 `Arc`）。
- 线程安全在类型层面强制：`Rc` 非 `Send` 让 `LocalRb` 无法跨线程；`Arc` 让 `SharedRb` 可以；`SharedRb` 不强求 `T: Send`，约束推迟到「移动 Producer/Consumer 跨线程」那一刻。

## 7. 下一步学习建议

- 想深入「拆分时所有权与生命周期的选择」（`split` vs `split_ref`、何时用 `Arc` 何时用 `&`），请学 [u2-l4](u2-l4-split-mechanism.md)。
- 想系统了解 `Observer` / `Producer` / `Consumer` / `RingBuffer` 这些 trait 的全部方法，进入 [u3](u3-l1-observer-trait.md) trait 体系单元。
- 想彻底搞懂 `Direct` / `Caching` / `Frozen` 三种包装器及其同步策略，进入 [u4](u4-l1-wrap-rbref-abstraction.md) 包装器单元（本讲提到的「`Caching` 按需同步」会在那里展开）。
- 想深入原子内存序、`CachePadded`、Miri 校验等专家级并发话题，进入 [u5-l1](u5-l1-lockfree-atomics-ordering.md)。
- 建议顺带阅读的源码：`src/wrap/direct.rs`、`src/wrap/caching.rs`（两种默认包装器）、`src/rb/macros.rs`（`StaticRb::default` 与 `HeapRb::new` 的生成处）。
