# 无锁并发：原子操作、CachePadded 与内存顺序

## 1. 本讲目标

本讲是进入专家层的第一讲，目标是把 ringbuf「无锁（lock-free）」这个宣传语**在源码层面讲透**。学完本讲你应该能够：

1. 说清 `SharedRb` 为什么是真正的无锁数据结构（操作要么立即成功、要么立即失败，绝不阻塞或自旋等锁）。
2. 理解 `CachePadded<AtomicUsize>` 两个字段为什么各自独占一条 CPU 缓存行，以及这如何避免「伪共享（false sharing）」。
3. 掌握 `Acquire` 读 + `Release` 写这对内存顺序如何在生产者与消费者之间建立 **happens-before（先于）关系**，从而保证消费者读到的元素数据一定是生产者刚写入的，而不是旧值或垃圾。
4. 看懂 `examples/test_ordering.rs` 这个压力测试为什么专门针对弱内存序架构（如 aarch64），并能自己运行它来验证正确性。

本讲只关心**线程间如何安全、高效地传递数据**，不深入 SPSC 不变量（那是下一讲 u5-l2 的 hold flags）和 `MaybeUninit` 的 unsafe 内存安全（u5-l3）。

## 2. 前置知识

本讲默认你已经掌握以下内容（均在前置讲义中建立）：

- **双索引与 2·capacity 模运算**（u2-l1）：ringbuf 用 `read`/`write` 两个落在 `0..2*capacity` 区间的索引描述状态，`read` 指向最旧元素、`write` 指向下一个空槽，物理槽位 = 索引 % capacity。
- **`SharedRb` vs `LocalRb`**（u2-l3）：`LocalRb` 单线程、用 `Cell<usize>` 存索引；`SharedRb` 多线程、用 `CachePadded<AtomicUsize>` 存索引。二者实现同一套环形缓冲区算法，本讲主角是后者。
- **Direct 包装器与即时同步**（u4-l2）：`Direct` 是「每次操作都立即读写底层原子索引」的策略；本讲会用到「`SharedRb::split` 默认产出 `CachingProd`/`CachingCons`，它们基于 `Frozen` 缓存索引、按需同步」这一结论（u4-l3、u4-l4）。

下面补充几个本讲专属、但又必须先建立直觉的基础概念。

### 2.1 原子操作（atomic operation）

普通变量的读写（如 `x = 1`）在多线程下是不安全的：编译器和 CPU 可能把它拆成多条指令，中间穿插其他线程的访问，导致「撕裂读/写」。**原子操作**保证一次读或写在硬件层面不可分割。Rust 中用 `core::sync::atomic::AtomicUsize`、`AtomicBool` 等类型表达。

### 2.2 内存顺序（memory ordering）

光有原子性还不够。现代 CPU（尤其 ARM、aarch64）和编译器会**重排**普通内存读写以提高性能。如果生产者「先写数据、再置标志位」，这两步可能被重排成「先置标志位、再写数据」，于是消费者看到标志位变了、却读到旧数据。

**内存顺序**就是用来约束这种重排的。本讲用到的三种：

- `Ordering::Release`：用在 store（写）上。保证**这条 store 之前的所有读写都不会被重排到这条 store 之后**。相当于「打包发布」——之前的一切先做完，才发布。
- `Ordering::Acquire`：用在 load（读）上。保证**这条 load 之后的所有读写都不会被重排到这条 load 之前**。相当于「取快递」——拿到后才拆里面东西。
- `Ordering::AcqRel`：用在 read-modify-write（如 `swap`）上，同时具备 Acquire 和 Release 语义。

### 2.3 happens-before（先于关系）

两个操作 A、B 之间存在 **happens-before** 关系（记作 \(A \xrightarrow{\text{hb}} B\)），意思是 A 的效果对 B 一定可见。本讲最关键的一条传递链是：

\[ \text{写数据} \;\xrightarrow{\text{sb}}\; \text{Release store index} \;\xrightarrow{\text{sw}}\; \text{Acquire load index} \;\xrightarrow{\text{sb}}\; \text{读数据} \]

其中 sb = sequenced-before（同一线程内的先后），sw = synchronizes-with（一个 Release store 与一个「读到它写入的值」的 Acquire load 之间建立的同步）。把这三段连起来，就得到「写数据 happens-before 读数据」，于是消费者读到的必定是生产者刚写的。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/rb/shared.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs) | `SharedRb` 的全部实现：4 个原子字段、`Observer`/`Producer`/`Consumer`/`RingBuffer` 的 impl。本讲主角。 |
| [src/wrap/frozen.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs) | `Frozen` 包装器：用 `Cell` 缓存本地索引，`commit`/`fetch` 才真正触发底层原子 store/load。 |
| [src/wrap/caching.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs) | `Caching` 包装器：`SharedRb::split` 的默认产物，在 `Frozen` 之上加「按需同步」。是真正调到原子的入口。 |
| [examples/test_ordering.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/test_ordering.rs) | 压力测试示例：两个线程传 1000 万个字节，检验弱内存序架构下顺序正确性。 |

## 4. 核心概念与源码讲解

### 4.1 无锁的含义与 SharedRb 的原子结构

#### 4.1.1 概念说明

「无锁（lock-free）」这个词经常被误用。在本讲里，它的精确含义是：

> **每个线程的每次操作都能在有限步内完成——要么成功，要么失败返回——绝不会因为另一个线程迟迟不行动而被无限期挂起或阻塞。**

对比两种「会卡住」的方案：

- **互斥锁（Mutex）**：线程 A 拿不到锁就会被操作系统挂起（sleep），直到持锁的线程 B 释放。如果 B 崩了或长时间不放手，A 就永远等下去。整个系统的进度取决于 B。
- **自旋锁（spinlock）**：线程 A 拿不到锁就死循环重试。如果 B 被换出去了，A 白白烧 CPU。

ringbuf 的 `try_push`/`try_pop` 既不挂起也不自旋：满就立刻返回 `Err`，空就立刻返回 `None`/`None`。**它把「等待」这件事彻底剥离给了派生 crate**（`async-ringbuf` 用 `await`、`ringbuf-blocking` 用信号量阻塞）。核心 `SharedRb` 是「非阻塞（non-blocking）」的，这才是「无锁」的真正承诺。

要实现这一点，`SharedRb` 不用 `Mutex`，而是用**原子变量**存索引和 hold 标志。原子变量的读写不需要加锁，硬件直接保证。

#### 4.1.2 核心流程

`SharedRb` 的多线程读写遵循「读-改-索引原子」的非阻塞流程：

```
生产者 try_push:                消费者 try_pop:
  1. (按需) Acquire 读 read_index     1. (按需) Acquire 读 write_index
  2. 若满 → 返回 Err(elem)            2. 若空 → 返回 None
  3. 把元素写入 storage 空槽          3. 从 storage 槽读出元素
  4. Release 写 write_index           4. Release 写 read_index
  5. 返回 Ok(())                      5. 返回 Some(elem)
```

关键点：步骤 2、5 的「满返回 Err」「空返回 None」是**立即**的，没有任何等待。步骤 1、4 的原子读写也立即返回（原子操作不会阻塞）。

#### 4.1.3 源码精读

先看 `SharedRb` 的结构体定义，它只有 5 个字段，其中 4 个是原子：

[src/rb/shared.rs:51-57](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L51-L57) —— `SharedRb` 用 `CachePadded<AtomicUsize>` 存两个索引、`AtomicBool` 存两个 hold 标志、`S` 存数据。

```rust
pub struct SharedRb<S: Storage + ?Sized> {
    read_index: CachePadded<AtomicUsize>,
    write_index: CachePadded<AtomicUsize>,
    read_held: AtomicBool,
    write_held: AtomicBool,
    storage: S,
}
```

注意结构体上方那段文档注释（[src/rb/shared.rs:27-30](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L27-L30)）有一句很关键的设计声明：**不要求 `T: Send`**。即便元素类型 `T` 不能跨线程传递（`T: !Send`），缓冲区本身也能正常工作——直到你把 producer/consumer 发到另一个线程。这说明线程安全不是靠「禁止非 Send 类型」来保证，而是靠「至多一个生产者、至多一个消费者」的 SPSC 约束 + 原子顺序来保证（详见 4.3 和 u5-l2）。

原子字段在构造时初始化：

[src/rb/shared.rs:66-75](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L66-L75) —— `from_raw_parts` 用传入的 `read`/`write` 初始化两个 `AtomicUsize`，两个 hold 标志初始化为 `false`。

```rust
Self {
    storage,
    read_index: CachePadded::new(AtomicUsize::new(read)),
    write_index: CachePadded::new(AtomicUsize::new(write)),
    read_held: AtomicBool::new(false),
    write_held: AtomicBool::new(false),
}
```

这就是无锁的全部「存储介质」：没有任何 `Mutex`、`RwLock`、`Condvar`，只有原子变量。

#### 4.1.4 代码实践

**实践目标**：亲手体会 `SharedRb` 操作的「立即成功或失败、不阻塞」特性。

**操作步骤**：

1. 新建一个二进制 crate 或在 examples 下写一个示例，用 `SharedRb::<Heap<i32>>::new(2)` 创建容量为 2 的缓冲区。
2. `split()` 得到 `(prod, cons)`。
3. 在**同一线程**里连续 `prod.try_push(1)`、`try_push(2)`、`try_push(3)`，打印每个返回值。
4. 不开任何消费线程，直接打印结果。

**预期结果**：前两次返回 `Ok(())`，第三次返回 `Err(3)`——满则立即退回元素，程序没有卡住、没有睡眠。这正是无锁的体现。

**需要观察的现象**：整个程序瞬间结束，`try_push` 在满时**立即返回**而非阻塞等待消费者取走空位。如果你想要「等到有空位」的语义，核心 `SharedRb` 给不了，必须换成 `async-ringbuf` 或 `ringbuf-blocking`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `SharedRb` 的索引字段从 `AtomicUsize` 换成普通 `usize`，在多线程下会出什么问题？

**参考答案**：读写普通 `usize` 在多线程下可能撕裂（读到「半个旧值半个新值」），且没有 happens-before 保证，消费者可能看到新索引却读到未初始化的元素数据。Rust 的借用检查器也会直接禁止 `&SharedRb` 跨线程共享非 `Sync` 的字段——`AtomicUsize` 是 `Sync` 的，普通 `usize` 在 `UnsafeCell` 外不是。

**练习 2**：有人说「无锁就是不用 Mutex」。这个说法精确吗？

**参考答案**：不精确。「不用 Mutex」是无锁的**手段之一**，但无锁的**本质承诺**是「每个线程的操作都能在有限步内完成、不因他线程停滞而无限阻塞」。仅去掉 Mutex 而换成自旋锁，仍然不算真正的 lock-free（自旋会因持锁线程被换出而空转）。ringbuf 用原子 + 「满/空立即返回」实现了真正的非阻塞。

---

### 4.2 CachePadded：用缓存行填充杜绝伪共享

#### 4.2.1 概念说明

要理解为什么需要 `CachePadded`，必须先理解 CPU 缓存的工作方式。

CPU 不直接读写内存，而是以**缓存行（cache line）**为单位搬运数据，一条缓存行通常是 **64 字节**（部分架构 128 字节）。当 CPU 核心要写某个字节时，会把包含它的整条 64 字节缓存行加载进自己的 L1 缓存；写入后这条缓存行被标记为「脏」，其他核里对应的缓存行随即**失效（invalidate）**，下次要用必须重新加载。

**伪共享（false sharing）** 是这样一类性能陷阱：两个线程各自只修改**不同**的变量，但这两个变量**恰好落在同一条 64 字节缓存行里**。于是 A 写它的变量 → B 核的整条缓存行失效 → B 不得不重新加载；B 写它的变量 → A 核的整条缓存行失效……两个核像抢球一样把这条缓存行踢来踢去，性能急剧下降——尽管它们碰的根本不是同一个字节。

`SharedRb` 正是伪共享的高危场景：

- **生产者线程**频繁写 `write_index`（步骤 4 的 Release store）。
- **消费者线程**频繁写 `read_index`（步骤 4 的 Release store）。

如果 `read_index` 和 `write_index` 挤在同一条缓存行，生产者和消费者就会疯狂互相使对方的缓存行失效。

`CachePadded`（来自 `crossbeam-utils`）的解法很直接：把内部值用无意义的填充字节撑到**一整条缓存行**，强制两个原子各占一行，物理上隔离。

#### 4.2.2 核心流程

`CachePadded<T>` 的内存布局示意（假设缓存行 64 字节，`AtomicUsize` 占 8 字节）：

```
read_index:  [ 8B 有效 | 56B 填充 ]  ← 占满第 1 条缓存行
write_index: [ 8B 有效 | 56B 填充 ]  ← 占满第 2 条缓存行
```

这样生产者写 `write_index` 时，只弄脏第 2 条缓存行；消费者写 `read_index` 时，只弄脏第 1 条缓存行。两条互不干扰，各自的核心 L1 缓存保持有效，不再来回搬运。

注意 `read_held`/`write_held` 没有用 `CachePadded` 包装——因为 hold 标志的访问频率远低于索引（只在拆分/销毁时触碰），不值得为它们单独占缓存行。

#### 4.2.3 源码精读

字段定义已经在前一节见过：

[src/rb/shared.rs:52-53](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L52-L53) —— 两个索引字段都用 `CachePadded<AtomicUsize>` 包装，确保各自独占缓存行。

```rust
read_index: CachePadded<AtomicUsize>,
write_index: CachePadded<AtomicUsize>,
```

`CachePadded` 来自 `crossbeam_utils`，导入见：

[src/rb/shared.rs:18](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L18) —— `use crossbeam_utils::CachePadded;`。

从使用者的角度看，`CachePadded<AtomicUsize>` 的 `.load()`/`.store()` 等方法与裸 `AtomicUsize` 完全一样（`CachePadded` 实现了 `Deref<Target = T>`），所以 `read_index.load(...)` 这种写法和没包装时一模一样，**零 API 成本**，只多了内存填充的开销。这就是为什么后续 `read_index()`、`set_write_index()` 的代码读起来和普通原子无异。

#### 4.2.4 代码实践

**实践目标**：通过「阅读 + 推理」理解 CachePadded 的作用，并知道如何实证它。

**操作步骤**：

1. 阅读上一节 4.1 的 `SharedRb` 字段定义，确认只有两个索引被 `CachePadded` 包装，而两个 `read_held`/`write_held` 没有。
2. 思考：为什么作者不给 hold 标志也加 `CachePadded`？（提示：访问频率）
3. （可选，实证）如果想亲眼看到伪共享的性能差异，可以 fork 仓库、把 `CachePadded` 去掉，写一个高频 push/pop 的双线程基准，对比有无填充的吞吐量。

**需要观察的现象 / 预期结果**：

- 去掉 `CachePadded` 后程序**仍然正确**——`CachePadded` 只影响性能，不影响正确性。
- 但在多核高频场景下，吞吐量会明显下降（因为缓存行乒乓）。在生产者/消费者都满负荷时差异最显著。

> **待本地验证**：具体性能差异数值取决于 CPU 架构、核数、负载模式，需在你自己的机器上用 `cargo bench`（见 `scripts/bench.sh`）实测。

#### 4.2.5 小练习与答案

**练习 1**：把 `read_index` 和 `write_index` 放同一条缓存行，会破坏正确性吗？

**参考答案**：不会。伪共享纯属性能问题——原子操作的可见性和顺序性由内存顺序保证，与缓存行布局无关。同一条缓存行里的两个原子，其 load/store 仍然是各自原子的、仍然遵守 Acquire/Release。伪共享只是让缓存失效更频繁，导致慢，但不会让数据错。

**练习 2**：缓存行大小是多少？为什么 `CachePadded` 不是固定按 64 字节填充？

**参考答案**：主流 x86、ARM 是 64 字节，部分架构（如某些 ARMv8、较早的 PowerPC）可达 128 字节。`CachePadded` 在编译期按目标架构的 `max_align` 自动选择填充长度（通常取较大的对齐值），以兼容不同平台，所以不是硬编码 64。

---

### 4.3 Acquire / Release：生产者→消费者的 happens-before

这是本讲最核心的一节。前面两节回答了「用什么存（原子）」和「怎么存得快（缓存行填充）」，本节回答**最关键的问题：凭什么消费者读到的元素一定是生产者刚写的？**

#### 4.3.1 概念说明

回顾 2.2 的内存顺序概念。问题的根源是**重排**。考虑生产者线程：

```
storage[slot].write(elem);          // (A) 把元素写进内存槽
set_write_index(new_write);         // (B) Release store 推进 write 索引
```

消费者线程：

```
w = write_index();                  // (C) Acquire load 读 write 索引
if 看到 w 前进了:
    elem = storage[slot].assume_init_read();  // (D) 读出元素
```

如果没有内存顺序约束，在弱内存序架构（aarch64）上，硬件可能把 (A) 和 (B) 重排成「先 (B) 后 (A)」。于是消费者先看到索引前进（C 成功），再去读槽位 (D)，却读到**还没写完**的旧数据/垃圾——数据竞争，未定义行为。

ringbuf 的解法是经典的**单变量消息传递模式（message passing idiom）**：

- (B) 用 `Release`：保证 (A) 这一步（以及之前一切）全部完成后，才执行 (B) 的 store。即「数据打包好了再发布索引」。
- (C) 用 `Acquire`：保证看到新索引后，(D) 这一步（以及之后一切）才执行。即「确认索引更新后再读数据」。

两者配对形成 synchronizes-with：(B) Release store 与读到它新值的 (C) Acquire load 之间建立同步。整条传递链：

\[ (A) \;\xrightarrow{\text{sb}}\; (B)_{\text{Release}} \;\xrightarrow{\text{sw}}\; (C)_{\text{Acquire}} \;\xrightarrow{\text{sb}}\; (D) \]

合起来就是 \( (A) \xrightarrow{\text{hb}} (D) \)——**生产者写元素 happens-before 消费者读元素**，所以消费者读到的必定是生产者写入的值。

反过来，消费者→生产者的「腾出空位」通知用 `read_index` 同样配对（消费者 `set_read_index` Release、生产者 `read_index` Acquire），于是生产者能正确看到消费者腾出的空闲槽位。

#### 4.3.2 核心流程

完整的生产者 push 调用链（经 `SharedRb::split` 默认产出的 `CachingProd`）：

```
CachingProd::try_push(elem)
  ├── (若本地判满) Frozen::fetch  → SharedRb::read_index()  // Acquire load read_index
  ├── 写元素到 storage 空槽
  ├── 推进本地 Cell<write>
  └── Frozen::commit → SharedRb::set_write_index()          // Release store write_index
```

消费者的 pop 调用链对称：

```
CachingCons::try_pop()
  ├── (若本地判空) Frozen::fetch  → SharedRb::write_index()  // Acquire load write_index
  ├── 从 storage 槽 assume_init_read 取元素
  ├── 推进本地 Cell<read>
  └── Frozen::commit → SharedRb::set_read_index()            // Release store read_index
```

注意：**只有索引的 store/load 用了 Acquire/Release；元素数据本身（storage 里的 `MaybeUninit`）是普通内存读写**。它之所以安全，正是因为索引的 Release/Acquire 把数据读写「夹」在了 happens-before 链里。

另外，hold 标志的 `swap` 用了 `AcqRel`（见 4.3.3 末尾），因为它是读改写操作，需要同时具备 Acquire（读旧值看是否被人占用）和 Release（发布新占用状态）。

#### 4.3.3 源码精读

**读索引（消费者侧读 write、生产者侧读 read）用 Acquire：**

[src/rb/shared.rs:95-102](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L95-L102) —— `read_index()` 和 `write_index()` 都用 `Ordering::Acquire` 加载，确保读到新索引后、后续对 storage 的访问能看到对端发布前的所有数据写入。

```rust
#[inline]
fn read_index(&self) -> usize {
    self.read_index.load(Ordering::Acquire)
}
#[inline]
fn write_index(&self) -> usize {
    self.write_index.load(Ordering::Acquire)
}
```

**写索引（生产者写 write、消费者写 read）用 Release：**

[src/rb/shared.rs:124-135](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L124-L135) —— `set_write_index`/`set_read_index` 都用 `Ordering::Release` 存储，确保此前的数据写入先于索引发布可见。

```rust
unsafe fn set_write_index(&self, value: usize) {
    self.write_index.store(value, Ordering::Release);
}
// ...
unsafe fn set_read_index(&self, value: usize) {
    self.read_index.store(value, Ordering::Release);
}
```

**这两组方法是怎么被调到的？** 答案在 `Frozen::commit`/`fetch`：

[src/wrap/frozen.rs:111-120](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L111-L120) —— `commit` 把本地缓存的索引通过 `set_*_index` 写回底层，触发的就是上面的 **Release store**。

```rust
pub fn commit(&self) {
    unsafe {
        if P { self.rb().set_write_index(self.write.get()); }
        if C { self.rb().set_read_index(self.read.get()); }
    }
}
```

[src/wrap/frozen.rs:123-130](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L123-L130) —— `fetch` 通过 `read_index()`/`write_index()` 拉取对端进度，触发的就是上面的 **Acquire load**。

```rust
pub fn fetch(&self) {
    if P { self.read.set(self.rb().read_index()); }
    if C { self.write.set(self.rb().write_index()); }
}
```

**而 `commit`/`fetch` 又是 `CachingProd::try_push` 按需触发的：**

[src/wrap/caching.rs:114-123](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L114-L123) —— `CachingProd::try_push`：本地判满才 `fetch`（Acquire 读 read_index），成功写入后立即 `commit`（Release 写 write_index）。

```rust
fn try_push(&mut self, elem: Self::Item) -> Result<(), Self::Item> {
    if self.frozen.is_full() { self.frozen.fetch(); }
    let r = self.frozen.try_push(elem);
    if r.is_ok() { self.frozen.commit(); }
    r
}
```

把这条链画完整就是：

```
CachingProd::try_push
  → frozen.try_push 把元素物理写入 storage
  → frozen.commit → SharedRb::set_write_index (Release)
```

消费者侧 `CachingCons::try_pop` 完全对称：

[src/wrap/caching.rs:133-143](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L133-L143) —— 本地判空才 `fetch`（Acquire 读 write_index），取到元素后立即 `commit`（Release 写 read_index）。

**hold 标志用 AcqRel：**

[src/rb/shared.rs:138-145](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L138-L145) —— `hold_read`/`hold_write` 用 `swap(flag, Ordering::AcqRel)`，既 Acquire 读旧值、又 Release 发布新值。

```rust
unsafe fn hold_read(&self, flag: bool) -> bool {
    self.read_held.swap(flag, Ordering::AcqRel)
}
```

这里之所以用 `AcqRel` 而非纯 `Relaxed`，是因为 `swap` 是「读旧值并写新值」的复合操作，需要两侧都建立顺序。hold 标志的具体语义（保证 SPSC 唯一性）在下一讲 u5-l2 详讲。

#### 4.3.4 代码实践

**实践目标**：把 Acquire/Release 的 happens-before 链亲手画出来，理解「为什么消费者能正确看到生产者写入」。

**操作步骤**：

1. 打开 [src/rb/shared.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs)，定位 `write_index()`（Acquire load，L100）与 `set_write_index`（Release store，L126）。
2. 假设生产者执行：`storage[3].write(42); set_write_index(w+1);`，消费者执行：`if write_index() > w { storage[3].assume_init_read(); }`。
3. 在纸上按本节 4.3.1 的格式画出四步 (A)(B)(C)(D) 与三条箭头（sb / sw / sb）。

**需要观察的现象 / 预期结果**：

- (A) 写元素 ─sb→ (B) Release store write_index ─sw→ (C) Acquire load write_index ─sb→ (D) 读元素。
- 结论：(A) happens-before (D)，消费者读到 `42`，绝不会读到旧值。
- **关键认知**：如果有人把 `set_write_index` 改成 `Ordering::Relaxed`，这条 sw 边就断了，(D) 可能读到垃圾——这就是为什么 Release 不能省。

> 这是**源码阅读型实践**，不运行代码，靠推理完成。下一节（4.4）会提供一个可运行的实证。

#### 4.3.5 小练习与答案

**练习 1**：为什么元素数据本身（storage 里的 `MaybeUninit`）不用原子，却仍然线程安全？

**参考答案**：因为元素数据的读写被「夹」在索引的 Release/Acquire 之间。生产者写数据 ─sb→ Release store write_index ─sw→ Acquire load write_index ─sb→ 消费者读数据，整条 happens-before 链成立，所以数据访问虽然是非原子普通内存读写，却不会发生数据竞争。这正是 SPSC 单生产者单消费者模式的优势——数据面可以完全免锁免原子，同步只发生在索引上。

**练习 2**：如果只把读改成 Acquire、写还是 Relaxed（或反过来），会发生什么？

**参考答案**：synchronizes-with 边断裂。Acquire load 必须配对一个 Release store（或更强的 SeqCst）才能建立同步；任一侧降级到 Relaxed，重排约束就消失，消费者可能在弱内存序架构上读到新索引却读到未完成的元素写入——数据竞争、未定义行为。

**练习 3**：生产者要感知「消费者腾出了空位」，走的是哪条索引？内存顺序是什么？

**参考答案**：走 `read_index`。消费者 `set_read_index` 用 Release store 推进，生产者 `read_index()` 用 Acquire load 读取（见 `Frozen::fetch` 中 `if P { self.read.set(self.rb().read_index()) }`）。配对方式与 write_index 完全对称。

---

### 4.4 test_ordering 压力测试：弱内存序架构下的正确性检验

#### 4.4.1 概念说明

内存顺序 bug 有个讨厌的特性：**在强内存序架构（x86/x86-64）上几乎测不出来**。x86 本身对普通读写有较强顺序保证（TSO 模型），即便你把 `Release` 误写成 `Relaxed`，大多数情况下程序仍然「看起来对」——因为硬件帮你兜了底。

但在**弱内存序架构**（ARM、aarch64、POWER、RISC-V 等）上，硬件会积极重排，`Relaxed` 的 bug 就会暴露：消费者读到乱序的数据。

这意味着光在 x86 上跑测试不够。ringbuf 提供了 `examples/test_ordering.rs`，它用「海量数据 + 高频跨线程传递」的方式**放大**重排发生的概率，让你在弱内存序机器（如树莓派 4 / Apple Silicon / 云上的 aarch64 实例）上能捕到顺序 bug。

#### 4.4.2 核心流程

示例的设计要点：

- **数据量极大**：`COUNT = 10_000_000`，一千万次传递，把出错概率放大。
- **缓冲区极小**：`SharedRb::<Heap<u8>>::new(17)`，容量只有 17，迫使生产者/消费者**频繁同步**（几乎每次都要 push/pop），最大化索引的 Release/Acquire 触发频率。
- **数据可校验**：发送的字节是 `1,2,3,...,255,1,2,3,...` 循环，结尾发一个 `0` 作为终止符。消费者逐字节断言 `assert_eq!(x, y)`，顺序哪怕错一个字节都会 panic。
- **双线程**：生产者、消费者各跑在一个线程里。

如果内存顺序有 bug，消费者在弱内存序机器上会以可观的概率读到错位/错值的字节，`assert_eq!` 触发 panic，测试失败。

#### 4.4.3 源码精读

[examples/test_ordering.rs:7-51](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/test_ordering.rs#L7-L51) —— 整个示例：用容量 17 的缓冲区、双线程传递一千万个可校验字节。

生产者线程：

[examples/test_ordering.rs:15-24](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/test_ordering.rs#L15-L24) —— 循环 `try_push`，满则重试（自旋在 `try_push` 返回 `Err` 上，但这是**用户层**的自旋，不是锁——核心 `SharedRb` 仍是立即返回）。

```rust
let pjh = thread::spawn(move || {
    let mut y = msg.next();
    while let Some(x) = y {
        if prod.try_push(x).is_ok() {
            y = msg.next();
        }
    }
});
```

消费者线程：

[examples/test_ordering.rs:26-45](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/test_ordering.rs#L26-L45) —— 循环 `try_pop`，逐字节 `assert_eq!(x, y)` 校验顺序，遇到终止符 `0` 则退出并返回已校验个数。

```rust
let cjh = thread::spawn(move || {
    let mut y = 1;
    loop {
        if let Some(x) = cons.try_pop() {
            if x == 0 { break i; }
            assert_eq!(x, y);   // 顺序错一个字节就 panic
            // ...
        }
    }
});
```

最后断言消费者正好校验了一千万个：

[examples/test_ordering.rs:47-48](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/test_ordering.rs#L47-L48) —— `pjh.join().unwrap()` 确保生产者正常结束，`assert_eq!(cjh.join().unwrap(), COUNT)` 确保消费者收全。

这个示例需要 `std` feature（用了 `std::thread`）：

[Cargo.toml:55-57](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L55-L57) —— `test_ordering` 示例声明 `required-features = ["std"]`。

#### 4.4.4 代码实践

**实践目标**：亲手运行这个压力测试，并结合源码解释为什么生产者写入的元素能被消费者正确可见（这是本讲规格指定的实践任务）。

**操作步骤**：

1. 在仓库根目录执行（注意要加 `--release`，一千万次迭代 debug 模式太慢）：

   ```bash
   cargo run --release --example test_ordering
   ```

2. 观察输出：消费者每处理一成打印一次进度（`... 10%`、`... 20%`……），最后打印 `Success!`。

3. 等待结束后，结合本节 4.3 的源码精读，回答：为什么消费者每次 `assert_eq!(x, y)` 都不会失败？

**需要观察的现象 / 预期结果**：

- 程序最终打印 `Success!`，退出码 0。
- 消费者收到的字节序列严格是 `1,2,...,255,1,2,...`，无一错位。

**原理解释**（把本讲串起来）：

1. 生产者把字节 `x` 写入 storage 槽（普通内存写）。
2. 生产者 `CachingProd::try_push` 成功后 `commit` → `SharedRb::set_write_index` 以 **Release** 推进 write 索引——这一步保证「写字节」先于「发布索引」。
3. 消费者 `CachingCons::try_pop` 判空时 `fetch` → `SharedRb::write_index` 以 **Acquire** 读索引——看到新索引后，对 storage 的读必定能看到生产者发布的字节。
4. 因此消费者 `assert_eq!(x, y)` 拿到的 `x` 一定是生产者刚写的那个字节，顺序正确。
5. 若没有 Release/Acquire（比如误用 Relaxed），在 aarch64 上消费者的 Acquire 读可能先于数据写入完成，读到错值，`assert_eq!` panic。本测试正是用来捕这种 bug。

> **待本地验证**：在 x86 上此测试几乎总是通过（硬件 TSO 兜底），**要真正检验弱内存序正确性，应在 aarch64 机器（如 `cargo run --target aarch64-unknown-linux-gnu` 交叉运行于 ARM 设备/容器，或 QEMU 模拟）上运行**。此外，本讲稍后提到的 `scripts/miri.sh`（u8-l5）能在 x86 上用 Miri 模拟弱内存序来发现这类问题。

#### 4.4.5 小练习与答案

**练习 1**：为什么这个测试用容量 17 这么小的缓冲区，而不是 1024？

**参考答案**：小容量迫使生产者和消费者**频繁同步**——容量 17 时几乎每几个元素就要等对方，write/read 索引的 Release/Acquire 触发密度极高，最大化暴露重排 bug 的概率。大容量会让两端各自攒一大批再同步，减少了「索引发布与数据写入紧挨」的场景，反而测不出问题。

**练习 2**：这个测试能「证明」内存顺序正确吗？

**参考答案**：不能完全证明——它是**经验性压力测试**，只能「在跑了 N 次没出错」的意义上增加信心，无法做数学证明。形式化证明内存顺序正确性需要借助 Loom/Miri 这类工具（`scripts/miri.sh` 会用 Miri 模拟弱内存序）。但作为回归测试，它能在弱内存序架构上有效捕捉真实的顺序回归。

**练习 3**：为什么消费者线程末尾要 `if x == 0 { break i; }`？

**参考答案**：`0` 是人为约定的**终止符**。生产者发送的数据是 `1..=255` 循环（见 `(1..=u8::MAX).cycle()`），不含 0，所以末尾单独 `chain([0])` 发一个 0 作为「结束」信号，让消费者知道数据传完可以退出。这比另开一个关闭通道更轻量。

## 5. 综合实践

**任务**：综合运用本讲三个知识点（无锁原子、CachePadded、Acquire/Release），写一份「ringbuf 跨线程数据传递正确性证明笔记」，并跑一次实证。

**步骤**：

1. **画调用链**：以 `prod.try_push(x)` 到 `cons.try_pop()` 为例，画出从 `CachingProd::try_push` → `Frozen::commit` → `SharedRb::set_write_index`（Release）再到对端 `CachingCons::try_pop` → `Frozen::fetch` → `SharedRb::write_index`（Acquire）的完整跨线程路径，标注每一段的内存顺序。
2. **写 happens-before 链**：用本讲 4.3.1 的 sb/sw/sb 三段式，写出「生产者写元素 happens-before 消费者读元素」的完整推导。
3. **实证**：运行 `cargo run --release --example test_ordering`，记录是否输出 `Success!`；若有条件，在 aarch64 设备或通过 `scripts/miri.sh` 再跑一次，对比。
4. **反思 CachePadded**：在你的笔记里说明：若去掉 `CachePadded`，正确性是否受影响？性能会怎样？为什么 hold 标志不需要 `CachePadded`？

**预期成果**：一份一页纸的笔记，能自洽地解释「ringbuf 凭什么在不加锁的前提下，让消费者正确读到生产者写的每一个元素」。

## 6. 本讲小结

- **无锁的真正含义**是「非阻塞」：`try_push`/`try_pop` 立即成功或失败，绝不睡眠或自旋等锁；等待语义被剥离给 async/blocking 派生 crate。
- **`SharedRb` 用 4 个原子字段**存状态：两个 `CachePadded<AtomicUsize>` 存索引、两个 `AtomicBool` 存 hold 标志，没有任何 `Mutex`。
- **`CachePadded` 解决伪共享**：把 `read_index`/`write_index` 各撑到一条缓存行，避免生产者写 write、消费者写 read 时互相使对方缓存行失效——纯属性能优化，不影响正确性。
- **Acquire 读 + Release 写**建立跨线程 happens-before：生产者写元素→Release store write 索引；消费者 Acquire load write 索引→读元素，保证读到的是刚写的，而非旧值/垃圾。元素数据本身不用原子，靠索引的顺序「夹」住。
- **hold 标志用 `AcqRel` 的 swap**，因为它是读改写复合操作；其 SPSC 语义留待下一讲。
- **`test_ordering` 是经验性压力测试**：小容量 + 海量数据放大重排概率，专门在弱内存序架构（aarch64）上检验 Acquire/Release 正确性；x86 因 TSO 兜底常测不出，需配合 Miri 或真机 ARM 验证。

## 7. 下一步学习建议

- **u5-l2 Hold flags：保证单生产者单消费者的不变量**：本讲反复提到「SPSC 约束」，下一讲深入 `read_held`/`write_held` 这两个 `AtomicBool` 如何在运行时强制「至多一个生产者、一个消费者」，以及为什么 `push_overwrite` 不能在 split 后并发使用。
- **u5-l3 MaybeUninit 与 unsafe 内存管理**：本讲说「元素数据是普通内存读写、靠索引顺序保护」，下一讲讲清 `MaybeUninit`、`unsafe_slices`、`assume_init_read` 这些 unsafe 原语的安全约定。
- **可选补充阅读**：`crossbeam-utils` 的 `CachePadded`/`CachePadded` 文档；Rust Reference 的 [Memory Model](https://doc.rust-lang.org/nomicon/atomics.html)（nomicon 的 atomics 章节）讲清了 Acquire/Release/SeqCst 的形式语义；想形式化验证可了解 `loom` 与 Miri 的弱内存序模拟。
