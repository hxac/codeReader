# 内部 OnceLock 惰性初始化

## 1. 本讲目标

本讲专讲 `crossbeam_utils::sync` 内部一个不对外公开的工具：`pub(crate) OnceLock<T>`。它是一份「基于 `std::sync::Once` 的精简版 `OnceLock`」。读完本讲，你应当能够：

- 说清楚「惰性初始化（lazy initialization）」要解决的问题，以及为什么 `static` 变量需要它；
- 解释 `OnceLock` 为什么用 `Once` + `UnsafeCell<MaybeUninit<T>>` 三个部件拼出来，以及每个部件各负什么责；
- 跟踪一次 `get_or_init(f)` 的完整执行路径：快路径（`is_completed`）直接返回，慢路径（`call_once`）只初始化一次；
- 为 `get_unchecked`、`initialize`、`Drop` 里的每一处 `unsafe` 写出安全性前提；
- 解释 `ShardedLock` 为什么用 `OnceLock` 来建立「全局线程索引注册表」，而不是直接写一个 `static Mutex<...>`。

本讲是 `sync` 单元的第四篇，硬前置是 [u3-l3 ShardedLock](./u3-l3-shardedlock.md)——本讲的「主顾」正是 ShardedLock 的注册表。你也可以把它看作 `AtomicCell` 里 `MaybeUninit` + `needs_drop` 直觉（见 [u2-l1](./u2-l1-atomiccell-api.md)）在「一次性初始化」场景下的再演绎。

## 2. 前置知识

### 2.1 为什么需要 Once / 惰性初始化

假设你想要一个**全局唯一的、可变的、被所有线程共享**的值，比如一个全局的 `Mutex<HashMap<K, V>>`。在 C/Rust 里最自然的写法是放进 `static`：

```rust
// 伪代码，无法编译
static REGISTRY: Mutex<HashMap<ThreadId, usize>> = Mutex::new(HashMap::new());
```

问题在于：`static` 变量在程序启动时就被构造，必须能在 **编译期常量求值（const eval）** 里生成。而 `Mutex::new(...)` 虽然在较新的 Rust 里是 `const fn`，但 `HashMap::new()` 配上运行期才有的分配器，往往**无法在常量上下文里构造**。

退一步，即便能塞进 `static`，还有第二个顾虑：这个全局值只在**被用到时**才该付出构造代价。如果一个库提供了 50 个 `static` 资源，但一次程序运行只用到 1 个，启动时全部构造就是浪费。

「惰性初始化」解决的就是这两个问题：**把值先空着，等第一次有人来取的时候，再现场构造；并且即使 100 个线程同时来取，构造函数也只执行一次。**

### 2.2 std::sync::Once

[`std::sync::Once`](https://doc.rust-lang.org/std/sync/struct.Once.html) 是标准库提供的「一次性同步原语」。它的核心方法是：

```rust
pub fn call_once<F: FnOnce()>(&self, f: F)
```

语义是：

- 不管有多少线程并发调用 `call_once(f1)`、`call_once(f2)`……**只有一个闭包会真正执行**，其余线程阻塞等它完成；
- 执行完成后，`Once` 记住「已完成」，此后任何 `call_once` 都立即返回，**且这是一个原子读**（无锁快路径）；
- 它还提供一个查询方法 [`is_completed()`](https://doc.rust-lang.org/std/sync/struct.Once.html#method.is_completed)（Rust 1.43 起稳定），返回是否已经完成初始化。

`Once` 本身**只管「这件事做了没有」**，不管「做了之后产生什么值」。`OnceLock<T>` 要做的事就是：在 `Once` 之上叠一个「存放值 `T` 的槽位」，把这两者缝合起来。

> 关键性质：`Once::new()` 自 Rust 1.2 起就是 `const fn`，可以在 `static` 上下文里调用。这正是 `OnceLock` 能用于全局惰性初始化的前提。

### 2.3 UnsafeCell 与 MaybeUninit

- [`UnsafeCell<T>`](https://doc.rust-lang.org/core/cell/struct.UnsafeCell.html) 是 Rust 里**内部可变性**（interior mutability）的最底层原语：它告诉你「这个 `&T` 背后其实可能被改写」，从而绕过「共享引用不可变」的默认假设。`OnceLock` 的 `get_or_init` 接收 `&self` 却要写入值，必须借助它。
- [`MaybeUninit<T>`](https://doc.rust-lang.org/core/mem/union.MaybeUninit.html) 表示「一块可能还没初始化的 `T` 大小的内存」。`OnceLock` 在创建时还没有 `T`，必须先用 `MaybeUninit::uninit()` 占位，等 `call_once` 真正跑完再写入。这一点和 `AtomicCell` 用 `MaybeUninit` 的动机一致（见 [u2-l1](./u2-l1-atomiccell-api.md)）。

## 3. 本讲源码地图

本讲涉及的关键源码文件：

| 文件 | 作用 |
|------|------|
| [src/sync/once_lock.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs) | `pub(crate) OnceLock<T>` 的全部实现，本讲主角 |
| [src/sync/sharded_lock.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs) | `ShardedLock` 中 `thread_indices()` 用 `OnceLock` 惰性建立全局注册表 |
| [src/sync/mod.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/mod.rs) | 声明 `mod once_lock;`，并带 `not(crossbeam_loom)` 门控 |

注意 `OnceLock` 是 **`pub(crate)`** 的，不会出现在 crate 的公开 API 里——它纯粹是 `sync` 模块内部的复用工具。它在 `src/sync/mod.rs` 中的声明如下：

[src/sync/mod.rs:7-8](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/mod.rs#L7-L8) —— 私有声明 `mod once_lock;`，且被 `#[cfg(not(crossbeam_loom))]` 门控（loom 模型测试下不编译，因为 `std::sync::Once` 不在 loom 的可建模世界内）。

文件顶部还交代了它的来历：

[src/sync/once_lock.rs:1-3](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs#L1-L3) —— 注释说明本文件基于**当时还不稳定**的 `std::sync::OnceLock` 抄写改写。这解释了为什么 `crossbeam-utils` 要自带一份而非直接用标准库。

---

## 4. 核心概念与源码讲解

### 4.1 OnceLock 的结构与构造

#### 4.1.1 概念说明

`OnceLock<T>` 的目标可以用一句话概括：**一个线程安全的、只能被初始化一次的、可持有任意类型 `T` 的「槽位」**。

要同时满足这几个约束，单靠 `Once` 不够（它不带值），单靠 `UnsafeCell<T>` 也不够（它没有同步、且要求一开始就有 `T`）。`crossbeam-utils` 的做法是把三者拼起来：

| 部件 | 类型 | 负责 |
|------|------|------|
| 同步与「一次性」保证 | `Once` | 决定值有没有被初始化、保证只初始化一次 |
| 内部可变性 | `UnsafeCell<...>` | 让 `&self` 也能写入槽位 |
| 推迟构造 | `MaybeUninit<T>` | 让槽位在创建时可以先空着 |

这种「三件套」是 Rust 里实现 `OnceLock`/`lazy` 的经典配方，理解了它，你就能读懂标准库、`once_cell`、`lazy_static` 等同类实现。

#### 4.1.2 核心流程

状态视角下，一个 `OnceLock<T>` 在生命周期里依次处于三种状态：

```
   ┌─────────────────────┐     首次 get_or_init(f)
   │  未初始化 (empty)    │ ─────────────────────────┐
   │  once 未完成          │                           │
   │  MaybeUninit 未填充   │                           ▼
   └─────────────────────┘                  ┌─────────────────────┐
                                            │  初始化中 (running)   │
                                            │  call_once 正在执行 f │
                                            └────────┬────────────┘
                                       f 返回 / 写入 │
                                                     ▼
                                          ┌─────────────────────┐
                                          │  已初始化 (completed) │
                                          │  once.is_completed() │
                                          │  MaybeUninit 已填充   │
                                          └─────────────────────┘
                              （此后所有 get_or_init 走无锁快路径）
```

- **未初始化**：`new()` 之后的状态，`once` 还没跑过，槽位是 `MaybeUninit::uninit()`。
- **初始化中**：某个线程正在 `call_once` 里执行 `f`，其他并发调用 `get_or_init` 的线程在此阻塞。
- **已初始化**：`f` 已返回并写入槽位，`once.is_completed()` 为真，此后所有访问都走快路径。

#### 4.1.3 源码精读

结构定义本身极其简短：

[src/sync/once_lock.rs:8-13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs#L8-L13) —— `OnceLock<T>` 由 `once: Once` 与 `value: UnsafeCell<MaybeUninit<T>>` 两个字段组成。

注释里特意提了一句「不像 `std::sync::OnceLock`，这里不需要 `PhantomData`，因为我们没用 `#[may_dangle]`」——这是个高级 Drop 检查细节，初学者只需知道：标准库版本为了在自定义 `Drop` 里放宽生命周期检查用了 `#[may_dangle]`，从而必须补一个 `PhantomData<T>` 来告诉类型系统「我其实持有 `T`」；这份精简版没这么做，所以省掉了 `PhantomData`。

紧接着是 `Send`/`Sync` 的不安全实现：

[src/sync/once_lock.rs:15-16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs#L15-L16) —— `T: Sync + Send` 时声明 `Sync`，`T: Send` 时声明 `Send`。

注意它对 `Sync` 的要求比 `Send` 多一个 `T: Sync`：因为 `Sync` 意味着「多个线程可以**同时**通过共享引用读到这个值」，所以值本身必须能被并发共享读取（`T: Sync`）；而 `Send` 只表示「可以把 `OnceLock` 转移到另一个线程」，值随之搬走，不需要可共享读，只要 `T: Send` 即可。这与 `AtomicCell` 要求 `T: Send` 才 `Sync` 是同一个家族的推理（见 [u2-l1](./u2-l1-atomiccell-api.md)）。

构造函数：

[src/sync/once_lock.rs:21-26](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs#L21-L26) —— `new()` 把 `once` 置为 `Once::new()`、`value` 置为 `MaybeUninit::uninit()`，且整体是 `const fn`。

`const fn` 是关键：它让 `OnceLock` 能直接初始化一个 `static`，而无需任何运行期构造代码。两个部件本身都是常量可构造的——`Once::new()` 自 1.2 起 `const`、`MaybeUninit::uninit()` 自 1.36 起 `const`——所以拼起来的 `new()` 也能是 `const`。这正是上一节「`static` 惰性初始化」能成立的地基。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`Once::new()` 是 `const fn`，所以 `OnceLock` 风格的静态变量不需要任何 `lazy_static!` 宏就能存在」。

**操作步骤**：

1. 在一个依赖了 `crossbeam-utils`（开启 `std`）的 crate 里写一个最小组件，模仿 `OnceLock` 的结构定义一个自己的 `static`：

```rust
// 示例代码：模仿 once_lock.rs 的结构，仅用于演示 const 构造
use std::sync::Once;
use core::{cell::UnsafeCell, mem::MaybeUninit};

struct MyLock<T> {
    once: Once,
    value: UnsafeCell<MaybeUninit<T>>,
}

impl<T> MyLock<T> {
    const fn new() -> Self {
        Self {
            once: Once::new(),
            value: UnsafeCell::new(MaybeUninit::uninit()),
        }
    }
}

// 关键：能放进 static
static G: MyLock<u64> = MyLock::new();

fn main() {
    println!("static 构造成功");
}
```

2. 用 `cargo build` 编译。

**需要观察的现象**：能否不借助任何宏、不写任何 `unsafe impl Sync`（暂时忽略这里 `MyLock` 未实现 `Sync`，仅验证「`const fn new` 可用于 `static`」）就把含有 `Once` 与 `UnsafeCell` 的结构放进 `static`。

**预期结果**：编译通过。这说明 `OnceLock::new()` 的 `const fn` 性质，是它能在 `static` 上下文里使用的全部原因。

#### 4.1.5 小练习与答案

**练习 1**：`OnceLock<T>` 把 `value` 字段类型写成 `UnsafeCell<MaybeUninit<T>>`。如果把外层 `UnsafeCell` 去掉、直接用 `MaybeUninit<T>`，会发生什么？

**参考答案**：`MaybeUninit<T>` 本身对内部可变性没有任何承诺，且 `&MaybeUninit<T>` 不能用来安全地写入。`get_or_init(&self, ...)` 只拿到共享引用 `&self`，要在共享引用背后写入槽位，必须借助 `UnsafeCell` 这层「告诉编译器这里可能被改写」的标记；否则编译器会基于「共享引用指向不可变内存」做优化，导致未定义行为。

**练习 2**：为什么 `unsafe impl Sync for OnceLock<T>` 要加 `T: Sync` 这个约束，而 `Send` 只要 `T: Send`？

**参考答案**：`Sync` 表示「`&OnceLock<T>` 可以被多线程共享」，而持有 `&OnceLock<T>` 就能调 `get_or_init` 拿到 `&T`——也就是多线程会**同时读到** `T`，所以必须 `T: Sync`。`Send` 只表示「`OnceLock<T>` 这个整体可以搬到另一个线程」，伴随的是所有权转移，不涉及多线程并发读 `T`，故只要 `T: Send`。

---

### 4.2 get_or_init 的两阶段初始化

#### 4.2.1 概念说明

`get_or_init(f)` 是 `OnceLock` 唯一的「取值 + 按需初始化」入口。它要同时做到两件事：

1. **正确性**：哪怕 1000 个线程同时调用，`f` 也只执行一次，且所有线程最终都看到同一个被妥善初始化的 `T`；
2. **性能**：初始化完成后，**热路径上不能有任何锁**——因为 `static` 全局值会被极高频地访问。

这两点合起来，就是经典的「双检锁（double-checked locking）」诉求。Rust 里实现它的最省心方式，是让 `Once` 承担所有同步责任，`OnceLock` 只在它之上做一层薄薄的「快路径查询」。

#### 4.2.2 核心流程

`get_or_init` 的逻辑只有两步：

```
get_or_init(f):
    1. 若 once.is_completed() 为真      ← 快路径（无锁原子读）
         → 直接 get_unchecked() 返回 &T
    2. 否则                              ← 慢路径
         → initialize(f)（内部 call_once 只让一个线程真正执行 f）
         → 再 get_unchecked() 返回 &T
```

为什么快路径只需要一次 `is_completed()` 原子读就足够安全？因为 `Once::call_once` 在「把状态翻成已完成」时会发出恰当的 release/acquire 屏障——只要 `is_completed()` 返回了真，调用者就一定 happens-after 那次写入，从而一定能看到被初始化好的 `T`。换句话说，`Once` 已经替我们保证了「完成标记」与「值写入」之间的可见性顺序，`OnceLock` 不需要再加自己的同步原语。

慢路径 `initialize` 把闭包 `f` 包进 `call_once`：

```
initialize(f):
    slot = self.value.get()              ← 拿到 *mut MaybeUninit<T>
    once.call_once(|| {
        value = f()                      ← 真正的初始化（只跑一次）
        slot.write(MaybeUninit::new(value))  ← 写入槽位
    })
```

`call_once` 的语义保证了「写入槽位」这一步全局只发生一次，且在「标记完成」之前完成。

#### 4.2.3 源码精读

`get_or_init` 全文：

[src/sync/once_lock.rs:43-56](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs#L43-L56) —— 先用 `is_completed()` 走快路径返回，失败才进入 `initialize(f)` 慢路径，最后无论如何都 `get_unchecked()` 返回 `&T`。

注意它对文档契约的承诺（注释 30-42 行）：「多个线程可以并发用**不同的**初始化函数调用 `get_or_init`，但保证只有一个会被执行」。也就是 `f` 是「按需提供」的，谁先到谁先跑，后到者的 `f` 直接被丢弃——这是 `Once` 的固有语义。还有一条「若 `f` panic，panic 会传播给调用者，且槽位保持未初始化」，这正是 `call_once` 在 panic 时的「中毒（poison）」行为。

慢路径 `initialize`：

[src/sync/once_lock.rs:58-69](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs#L58-L69) —— `#[cold]` 提示编译器这是冷路径，把 `f` 的执行与 `slot.write` 都塞进 `call_once` 闭包。

两个细节值得指出：

1. **`#[cold]` 标注**。这是一个对优化器的提示：告诉它「这个函数几乎不会被调用」。优化器据此会把 `initialize` 移出热路径的指令缓存区，让 `get_or_init` 的快路径更紧凑。这与上一讲 ShardedLock 里 `current_index()` 用 `#[inline]` 是同一类「帮编译器排好热/冷代码」的工程技巧。
2. **先算 `slot` 再进 `call_once`**。`slot` 是裸指针 `*mut MaybeUninit<T>`，在闭包外取得。闭包内只做「`f()` 求值 → `slot.write` 写入」。之所以能这样，是因为 `slot` 指向的内存属于 `self`，而 `self` 在整个 `call_once` 期间都存活且被 `Once` 内部锁保护，不会逃逸或被释放。

#### 4.2.4 代码实践

**实践目标**：用「源码阅读型实践」验证 `call_once` 「只执行一次、且对调用者透明」的语义，并观察 `is_completed` 在初始化前后的变化。

**操作步骤**：

1. 准备一个能看到 `Once` 内部状态的观察程序：

```rust
// 示例代码：观察 Once 的状态翻转
use std::sync::Once;
use std::thread;

fn main() {
    let once = Once::new();
    println!("before: is_completed = {}", once.is_completed());

    let handles: Vec<_> = (0..8)
        .map(|i| {
            thread::spawn(move || {
                once.call_once(|| {
                    println!("-> 初始化函数在 thread {i} 里跑了");
                    std::thread::sleep(std::time::Duration::from_millis(10));
                });
            })
        })
        .collect();
    for h in handles { h.join().unwrap(); }

    println!("after:  is_completed = {}", once.is_completed());
}
```

2. 多次运行该程序。

**需要观察的现象**：

- 「初始化函数在 thread X 里跑了」这一行**只打印一次**，且 `X` 是某个不确定的线程号（说明由抢到首跑权的线程执行）；
- `before` 时 `is_completed = false`，`after` 时为 `true`。

**预期结果**：8 个线程并发调用，初始化函数恰好执行 1 次。这正是 `OnceLock::get_or_init` 「多线程只初始化一次」的物理来源——它完全是 `Once::call_once` 的语义。

> 如果你的环境无法编译运行（例如只读环境），明确标注「待本地验证」并仅做源码推理：依据 [`Once::call_once` 文档](https://doc.rust-lang.org/std/sync/struct.Once.html#method.call_once) 的保证，上述现象是其规范承诺的行为。

#### 4.2.5 小练习与答案

**练习 1**：`get_or_init` 在 `initialize(f)` 返回后，又调用了一次 `get_unchecked()`。为什么不在 `initialize` 内部直接返回 `&T`，而要回到外层统一返回？

**参考答案**：这是为了让「快路径」和「慢路径」共享同一个 `get_unchecked` 出口，简化代码并保证两条路径返回的引用语义完全一致。更关键的是：调用 `initialize` 的线程**不一定**就是执行 `f` 的那个线程——如果调用者到达时 `call_once` 已在进行，它会阻塞等完成后返回，此时 `f` 的实际执行者是别的线程。因此必须在 `call_once` 返回后、统一通过 `get_unchecked` 取值，而不是在闭包内部返回。

**练习 2**：注释里警告「从 `f` 内部重入地初始化 cell 是错误行为，当前实现会死锁」。请推测为什么会死锁。

**参考答案**：`call_once` 内部有同步机制——正在执行闭包的线程会持有它内部的锁（或等价的「运行中」状态）。如果闭包 `f` 又去调用同一个 `OnceLock` 的 `get_or_init`，新调用发现 `is_completed()` 仍为假（还没跑完），会再次进入 `call_once`，而后者会发现「这个 `Once` 正被当前线程持有且未完成」，于是陷入自等自的死锁。这是所有「一次性初始化」原语的共同陷阱。

---

### 4.3 get_unchecked、Drop 与 ShardedLock 的注册表

#### 4.3.1 概念说明

本节收两个尾巴：

1. **`get_unchecked` 与 `Drop`**：把槽位「当成已经初始化的 `T`」读出来、以及在销毁时正确回收 `T` 的资源。这两处是 `OnceLock` 里全部 `unsafe` 的集中地。
2. **ShardedLock 的实际用法**：`OnceLock` 在 `crossbeam-utils` 里唯一的消费者，就是 ShardedLock 的「全局线程索引注册表」。理解它，才能把前两节抽象的「惰性初始化」落到一个真实需求上。

为什么 ShardedLock 需要一个惰性初始化的注册表？回顾 [u3-l3](./u3-l3-shardedlock.md)：`read()` 要按「当前线程的 index」选 shard，这个 index 由 TLS 里的 `REGISTRATION` 提供。而 `REGISTRATION` 在线程首次进入时，需要去一个**全局表**里登记自己、领一个号码牌。这个全局表是一份 `Mutex<ThreadIndices>`——里面装着 `HashMap` 和 `Vec`，**无法在常量上下文里构造**，所以必须惰性创建。`OnceLock` 正好是承载它的容器。

#### 4.3.2 核心流程

**读取值（`get_unchecked`）**：

```
get_unchecked():  // 调用前提：值确已初始化
    debug_assert!(once.is_completed())
    return (*value.get()).assume_init_ref()   // &MaybeUninit<T> → &T
```

它把 `MaybeUninit<T>` 当作「已经初始化」来取引用。这是 `OnceLock` 唯一的取值出口，`get_or_init` 两条路径最终都汇聚到它。

**销毁（`Drop`）**：

```
drop():
    if once.is_completed():
        (*value.get()).cast::<T>().drop_in_place()   // 回收 T 的资源
```

只有「曾经初始化过」的槽位才需要 `drop`，否则会去 drop 一段未初始化内存——未定义行为。

**ShardedLock 注册表的取用（`thread_indices`）**：

```
thread_indices() -> &'static Mutex<ThreadIndices>:
    static THREAD_INDICES: OnceLock<Mutex<ThreadIndices>> = OnceLock::new();
    THREAD_INDICES.get_or_init(init)   // 首次访问时构造，之后零开销
```

`OnceLock::new()` 是 `const fn`，所以这行 `static` 能成立；`get_or_init(init)` 保证 `init`（构造空 `HashMap` + 空 `Vec`）在整个程序生命周期里只跑一次。

#### 4.3.3 源码精读

`get_unchecked`：

[src/sync/once_lock.rs:71-77](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs#L71-L77) —— 带一个 `debug_assert!(is_completed())` 兜底，然后 `assume_init_ref()` 把 `&MaybeUninit<T>` 转成 `&T`。

它被标记为 `unsafe fn`，安全性前提写在注释里：「The value must be initialized」。调用方（也就是 `get_or_init`）必须确保只在 `is_completed()` 为真、或刚从 `initialize`/`call_once` 返回后调用。`debug_assert` 不是安全保证，只在调试构建里帮你抓 bug；release 里它会被去掉，正确性完全依赖调用约定。

`Drop` 实现：

[src/sync/once_lock.rs:80-89](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs#L80-L89) —— 用 `is_completed()` 守卫，只对已初始化的槽位 `drop_in_place`。

这里有一处体现 MSRV 取舍的注释：

[src/sync/once_lock.rs:84-86](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs#L84-L86) —— 作者特意说明：`MaybeUninit::assume_init_drop` 需要 Rust 1.60，为了不抬高 crate 的 MSRV（见 [u1-l1](./u1-l1-project-overview.md) 提到的 1.56），改用 `value.get().cast::<T>().drop_in_place()` 这个等价写法。

两者语义等价：都是「假定这段内存是一个合法的 `T`，调用它的 `Drop`」。区别仅在 API 入口——`assume_init_drop` 是 `MaybeUninit` 上的便捷方法（1.60 才有），而 `ptr::cast::<T>().drop_in_place()` 是更底层的裸指针操作，老版本 Rust 就支持。

> 顺带一提：因为 `OnceLock` 自身记录了「是否初始化过」，这里**不需要**像 `AtomicCell`（[u2-l1](./u2-l1-atomiccell-api.md)）那样用 `needs_drop()` 在 `Copy` 与非 `Copy` 间分流——`is_completed()` 这个布尔值就是唯一的判断依据，对任何 `T` 都成立。

最后看 ShardedLock 怎么消费它。注册表的取用函数：

[src/sync/sharded_lock.rs:594-604](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L594-L604) —— `thread_indices()` 内部声明 `static THREAD_INDICES: OnceLock<Mutex<ThreadIndices>> = OnceLock::new();`，并用 `get_or_init(init)` 首次访问时构造。

`ThreadIndices` 里装的是 `HashMap<ThreadId, usize>` 与 `Vec<usize>`（free list）——两者都依赖堆分配，**不可能**写成常量初始化器，所以非用惰性初始化不可。而 `OnceLock` 同时满足了三个诉求：

1. **可在 `static` 里声明**（`new()` 是 `const fn`）；
2. **首次访问才构造**，未被使用则零开销；
3. **多线程首次访问安全**（`Once` 保证只构造一次）。

读热路径上的 `current_index()` 并不直接碰这个 `OnceLock`：

[src/sync/sharded_lock.rs:577-580](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L577-L580) —— `current_index()` 只读 TLS 里的 `REGISTRATION.index`，注册表锁不在读热路径上。

注册表只在两个时机被触碰：**线程首次进入**（`thread_local!` 初始化 `REGISTRATION` 时，去注册表领号）和**线程退出**（`Registration::drop` 归还号）。这与上一讲 [u3-l3](./u3-l3-shardedlock.md) 中「注册表锁不在读热路径」的结论互为印证。

最后顺带看一眼 `once_lock` 模块的 loom 门控：

[src/sync/mod.rs:7-8](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/mod.rs#L7-L8) —— `#[cfg(not(crossbeam_loom))] mod once_lock;`，loom 下整个 `once_lock` 不编译。

这也是上一讲提到「ShardedLock 在 loom 下不可用」的根因之一：`ShardedLock` 依赖 `once_lock`，而 `once_lock` 依赖 `std::sync::Once`；loom 无法对 `std::sync::Once` 建模，于是连同 `ShardedLock` 一起被门控掉（详见 [u1-l2 模块地图](./u1-l2-module-map.md)）。

#### 4.3.4 代码实践

**实践目标**：仿照 `once_lock.rs` 实现一个**简化版 `OnceLock`**（基于 `std::sync::Once`），并用它在多线程下惰性初始化一个共享 `HashMap`，验证初始化函数**只执行一次**。

**操作步骤**：

1. 新建一个 binary crate，把下面这份「最小可用版 OnceLock」贴进去（这是**示例代码**，仿照 `src/sync/once_lock.rs` 精简而来，省略了 `Drop` 与 `Sync` 实现以聚焦主干）：

```rust
// 示例代码：简化版 OnceLock，仿照 crossbeam-utils/src/sync/once_lock.rs
use std::sync::Once;
use core::{cell::UnsafeCell, mem::MaybeUninit};

struct MiniOnceLock<T> {
    once: Once,
    value: UnsafeCell<MaybeUninit<T>>,
}

// 仅用于单线程演示时声明 Sync；生产代码应像 once_lock.rs 那样按 T 约束声明
unsafe impl<T: Send + Sync> Sync for MiniOnceLock<T> {}
unsafe impl<T: Send> Send for MiniOnceLock<T> {}

impl<T> MiniOnceLock<T> {
    const fn new() -> Self {
        Self {
            once: Once::new(),
            value: UnsafeCell::new(MaybeUninit::uninit()),
        }
    }

    fn get_or_init<F: FnOnce() -> T>(&self, f: F) -> &T {
        // 快路径
        if self.once.is_completed() {
            return unsafe { (*self.value.get()).assume_init_ref() };
        }
        // 慢路径
        let slot = self.value.get();
        self.once.call_once(|| unsafe {
            slot.write(MaybeUninit::new(f()));
        });
        unsafe { (*self.value.get()).assume_init_ref() }
    }
}

fn main() {
    use std::collections::HashMap;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::thread;

    static REGISTRY: MiniOnceLock<HashMap<u64, &'static str>> = MiniOnceLock::new();
    static INIT_COUNT: AtomicUsize = AtomicUsize::new(0);

    let handles: Vec<_> = (0..8)
        .map(|i| {
            thread::spawn(move || {
                let map = REGISTRY.get_or_init(|| {
                    INIT_COUNT.fetch_add(1, Ordering::SeqCst); // 应只 +1 一次
                    let mut m = HashMap::new();
                    m.insert(1u64, "hello");
                    m.insert(2u64, "world");
                    m
                });
                // 所有线程都应看到同一份 map
                assert_eq!(map.get(&1), Some(&"hello"));
                println!("thread {i}: got map with {} entries", map.len());
            })
        })
        .collect();
    for h in handles { h.join().unwrap(); }

    println!("init ran {} time(s)", INIT_COUNT.load(Ordering::SeqCst));
}
```

2. 用 `cargo run` 运行（需开启 `std`，默认即开启）。

**需要观察的现象**：

- `init ran 1 time(s)`——尽管有 8 个线程并发 `get_or_init`，初始化闭包**只跑了 1 次**；
- 8 行 `thread i: got map with 2 entries` 全部打印——所有线程都看到了同一份完整的 `HashMap`；
- `static REGISTRY` 在 `static` 上下文里直接用 `MiniOnceLock::new()` 初始化，无需任何宏。

**预期结果**：上述三点全部成立。如果第 3 点编译失败，多半是你给 `MiniOnceLock` 加了某个非 `const fn` 的初始化——回看 4.1 节，`Once::new()` 与 `MaybeUninit::uninit()` 都必须是 `const`。

> 如果你的环境无法运行（例如只读沙箱），标注「待本地验证」，并对照源码确认：`INIT_COUNT` 只增一次的性质，完全来自 [`Once::call_once` 的规范](https://doc.rust-lang.org/std/sync/struct.Once.html#method.call_once)。

#### 4.3.5 小练习与答案

**练习 1**：在 `Drop` 实现里，如果把 `if self.once.is_completed()` 这个守卫去掉，直接 `drop_in_place`，会出什么问题？

**参考答案**：当一个 `OnceLock<T>` 在从未被 `get_or_init` 过的情况下就被 drop（比如程序里定义了 `static` 但没人访问，或本地变量提前离开作用域），槽位里是 `MaybeUninit::uninit()`——一段**未初始化**的内存。对它调用 `drop_in_place` 等于去 drop 一个非法的 `T`，是未定义行为。`is_completed()` 守卫正是为了只在「确曾写入过」时才回收。注释里的 `// SAFETY: The inner value has been initialized` 说的就是这个前提。

**练习 2**：ShardedLock 的 `thread_indices()` 用 `static THREAD_INDICES: OnceLock<Mutex<ThreadIndices>> = OnceLock::new();`。为什么不直接写成 `static THREAD_INDICES: Mutex<ThreadIndices> = Mutex::new(ThreadIndices { ... });`？

**参考答案**：因为 `ThreadIndices` 含 `HashMap` 和 `Vec`，它们的构造需要堆分配，**无法在常量上下文里求值**，所以 `Mutex::new(ThreadIndices { ... })` 编不过。`OnceLock` 把构造推迟到首次 `get_or_init` 调用时（运行期），同时用 `Once` 保证多线程下只构造一次——这正是惰性初始化解决的经典场景。同时 `OnceLock::new()` 是 `const fn`，能放进 `static`，从而拿到 `&'static Mutex<...>`。

**练习 3**：`get_unchecked` 里的 `debug_assert!(self.once.is_completed())` 在 release 构建里会被去掉。那么 release 下「确保只在已初始化时调用」这个不变量由谁保证？

**参考答案**：由**调用约定**保证，而非运行期检查。`get_unchecked` 是 `unsafe fn`，它的 `# Safety` 文档要求「The value must be initialized」。crate 内部唯一的调用点是 `get_or_init`，而后者只在两种情况下调用 `get_unchecked`：快路径里 `is_completed()` 刚返回真，或慢路径里 `initialize`（内含 `call_once`）刚返回。两种情况都保证值已写入。`debug_assert` 只是开发期的一份额外保险，release 下靠 `unsafe` 的契约来维护正确性。

---

## 5. 综合实践

把本讲的三件事——**结构拼装、两阶段初始化、Drop 安全性**——串成一个完整任务。

**任务**：基于 `std::sync::Once`，从零实现一个**功能完整的** `MyOnceLock<T>`，要求：

1. 结构为 `Once` + `UnsafeCell<MaybeUninit<T>>`，`new()` 为 `const fn`；
2. 实现 `get_or_init<F: FnOnce() -> T>(&self, f: F) -> &T`，含 `is_completed` 快路径与 `call_once` 慢路径，慢路径标 `#[cold]`；
3. 实现 `unsafe fn get_unchecked(&self) -> &T`，带 `debug_assert!(is_completed())`；
4. 实现 `Drop`：用 `is_completed()` 守卫，按 `once_lock.rs` 的写法用 `cast::<T>().drop_in_place()` 回收（不要用 `assume_init_drop`，体会 MSRV 取舍）；
5. 按 `T: Send + Sync` / `T: Send` 声明 `Sync`/`Send`；
6. 用它做一个全局注册表演示：`static REG: MyOnceLock<Mutex<Vec<String>>>`，起 16 个线程并发 `get_or_init` 写入各自线程名，最后主线程读取并打印总数。

**验证清单**：

- 初始化闭包的执行次数为 1（用一个 `AtomicUsize` 计数）；
- 16 个线程写入的名字都出现在最终列表里（说明大家共享同一份被妥善初始化的值）；
- 把 `T` 换成需要 `Drop` 的类型（如 `Vec<String>`），程序结束后用 valgrind 或 Miri 检查无泄漏、无非法 drop；
- 把 `MyOnceLock` 换成你为 `Copy` 类型实例化的版本，确认 `Drop` 守卫在「从未初始化」时不会误触发。

完成后，把你这份实现与 [src/sync/once_lock.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs) 逐行对照，找出你遗漏的注释或 SAFETY 说明——这正是源码阅读里最有价值的部分。

## 6. 本讲小结

- `OnceLock<T>` = `Once`（一次性同步）+ `UnsafeCell`（内部可变性）+ `MaybeUninit<T>`（推迟构造）三件套，是 Rust 里实现「一次性惰性初始化」的经典配方。
- `new()` 是 `const fn`——靠的是 `Once::new()` 与 `MaybeUninit::uninit()` 都是 const——这是它能放进 `static`、支撑全局惰性值的全部原因。
- `get_or_init` 是双检锁：`is_completed()` 快路径无锁返回，`initialize`（`#[cold]`）慢路径用 `call_once` 保证 `f` 全局只跑一次，两条路径汇于同一个 `get_unchecked`。
- `get_unchecked` 是 `unsafe fn`，安全性前提「值已初始化」由调用约定维护，`debug_assert` 仅是开发期兜底。
- `Drop` 用 `is_completed()` 守卫只回收已初始化的槽位；用 `cast::<T>().drop_in_place()` 而非 `assume_init_drop` 是为了不抬高 MSRV（1.56）。
- `OnceLock` 在 crate 内唯一消费者是 ShardedLock 的 `thread_indices()`——因为 `HashMap`/`Vec` 无法常量构造，必须惰性建表；模块整体被 `#[cfg(not(crossbeam_loom))]` 门控。

## 7. 下一步学习建议

本讲之后，`sync` 单元（u3）的四篇——Parker、WaitGroup、ShardedLock、OnceLock——已全部讲完，你已掌握 `crossbeam-utils` 在「标准库同步」层面的全部公开类型与一处典型内部工具。建议：

1. **横向对照标准库**：阅读 [`std::sync::OnceLock`](https://doc.rust-lang.org/std/sync/struct.OnceLock.html) 与 [`std::sync::LazyLock`](https://doc.rust-lang.org/std/sync/struct.LazyLock.html) 的源码，对照本讲的 `once_lock.rs`，体会稳定版多了哪些能力（如 `get`、`set`、`into_inner`）以及 `#[may_dangle]` + `PhantomData` 的取舍。
2. **进入作用域线程**：本讲建立的「`Once` 驱动的一次性初始化」与「`static` 全局值」直觉，是理解 [u4-l1 thread::scope](./u4-l1-thread-scope.md) 中「scope 结束前保证 join」这一更强承诺的基础——可顺势进入第四单元。
3. **回看 AtomicCell**：把本讲的 `MaybeUninit` 用法与 [u2-l1](./u2-l1-atomiccell-api.md) 的 `MaybeUninit` + `needs_drop` 对照，体会同一原语在「一次性写入」与「反复原子读写」两种场景下的不同用法。
4. **如果想再深一层**：阅读 [src/atomic/seq_lock.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs) 里的乐观读路径，那是另一套「无锁读 + 受控写」的设计，与本讲的 `Once` 是不同的同步思路，可作为专家层 [u5-l3](./u5-l3-cfg-loom-wideseqlock.md) 的预热。
