# BlockingRb 与 Semaphore 抽象：可插拔的阻塞同步原语

> 所属单元：u7 派生 crate ringbuf-blocking
> 依赖：u2-l3（LocalRb vs SharedRb）、u5-l1（无锁原子与内存顺序）

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 `BlockingRb` 的内部结构——核心无锁的 `SharedRb` 加上两个信号量，并解释为什么只需要「两个二值信号量」就能给环形缓冲区补上「等待」语义。
2. 读懂 `Semaphore` trait 的 `give` / `try_take` / `take` 三个方法，理解它是可插拔抽象、默认 `StdSemaphore` 用 `Condvar + Mutex` 实现。
3. 解释 `StdSemaphore::take` 在「永久等待（`FOREVER`）」「有限超时（`Some(d)`）」「不等待（`NO_WAIT`）」三种模式下的行为，以及 `TimeoutIter` / `TakeIter` 如何把超时算术和「先排空再等待」的模式封装起来。
4. 思考在 `no_std` 嵌入式场景下，如何自己实现一个 `Semaphore`。

## 2. 前置知识

在进入本讲前，请先回忆几个在前面讲义已经建立的认知（本讲会直接承接，不再重复推导）：

- **核心 `SharedRb` 是无锁 SPSC 缓冲区**（u2-l3、u5-l1）：它用 `CachePadded<AtomicUsize>` 存读写索引，用 `AtomicBool` 存 hold 标志，`try_push` / `try_pop` 立即成功或失败、绝不阻塞。它的 trait 方法 `set_write_index` / `set_read_index` 是一次 `Release` 原子 store，`hold_read` / `hold_write` 是一次 `AcqRel` 的 `swap`。
- **「等待语义」被刻意剥离出核心**（u1-l1、u1-l3）：核心 crate 只给非阻塞 `try_*` 接口；要 `push().await` 或阻塞式 `push()`，需要派生 crate。`async-ringbuf` 用 `AtomicWaker`（u6-l1），`ringbuf-blocking` 用信号量——本讲的主角。
- **「唤醒者名字 = 你等待的索引」这条规律**（u6-l1）：生产者满时等的是消费者推进 **read** 索引，消费者空时等的是生产者推进 **write** 索引；而「关闭」复用 hold 通道。本讲的 `BlockingRb` 把同一条规律原样搬到了阻塞世界。

还需要一个新概念：**二值信号量（binary semaphore）**。它是一个只有 0/1 两个状态的同步原语：

- `give`：把状态置为 1（「已通知」），若已经是 1 则什么也不做——因此**多次 `give` 会合并成一次**。
- `take`：若状态是 1 则立即取走（置 0）并返回成功；若状态是 0 则**阻塞等待**，直到别人 `give`，或超时返回失败。

你可以把它想成「一个会自动重置的事件标志」。`ringbuf-blocking` 用它来模拟条件变量：每端的等待循环都是「取一次信号 → 重新检查条件 → 不满足就再等」。

> 本讲不涉及 `BlockingProd` / `BlockingCons` 的对外阻塞 API（`push` / `pop` / `wait_vacant` / 超时返回 `WaitError` 等），那是下一讲 u7-l2 的内容。本讲只讲**底层地基**：缓冲区怎么挂上信号量、信号量这个 trait 长什么样、标准实现怎么工作。

## 3. 本讲源码地图

本讲涉及的关键文件都在派生 crate `ringbuf-blocking`（`blocking/` 目录）内：

| 文件 | 作用 |
| --- | --- |
| `blocking/src/rb.rs` | 定义 `BlockingRb<S, X>`：`SharedRb<S>` + 两个信号量 `X`；为它实现 `Observer` / `Producer` / `Consumer` / `RingBuffer`，在改变对端可见状态时 `give` 信号量。 |
| `blocking/src/sync.rs` | 定义 `Semaphore` trait、`Instant` trait、常量 `NO_WAIT` / `FOREVER`、默认实现 `StdSemaphore`，以及超时机器 `TimeoutIter` / `TakeIter`。 |
| `blocking/src/alias.rs` | 给出 `BlockingHeapRb` / `BlockingStaticRb` 类型别名，并定义 `new` / `Default` 构造器。 |
| `blocking/src/wrap/prod.rs` | （辅助阅读）`BlockingProd` 的 `wait_iter!` 宏与阻塞方法，展示信号量如何被 `take`。 |
| `blocking/src/wrap/cons.rs` | （辅助阅读）`BlockingCons` 的 `wait_iter!` 宏与阻塞方法。 |
| `blocking/src/tests.rs` | （实践参考）`wait` 等测试，给出两个线程间用 `BlockingHeapRb` 传字节的完整用法。 |

## 4. 核心概念与源码讲解

### 4.1 BlockingRb：在 SharedRb 之上加两个信号量

#### 4.1.1 概念说明

核心 `SharedRb` 是无锁的：缓冲区满时 `try_push` 返回 `Err(elem)`、空时 `try_pop` 返回 `None`，调用方只能**自己轮询**。但在「生产者—消费者」协作场景里，我们更希望写满时生产者**自动挂起**、等消费者腾出空间再被唤醒，反之亦然。这正是 `BlockingRb` 要补的能力。

它的设计哲学和 `async-ringbuf`（u6-l1）完全对称：**不改核心 `SharedRb` 一行代码，只在它外面再包一层同步原语**。区别只在于用的原语不同：

- `async-ringbuf`：`SharedRb` + 两个 `AtomicWaker`（异步唤醒）。
- `ringbuf-blocking`：`SharedRb` + 两个**信号量**（阻塞唤醒）。

为什么是「两个」信号量？因为 SPSC 有两个方向：生产者→消费者（新数据到达，唤醒等数据的消费者）、消费者→生产者（腾出空间，唤醒等空间的生产者）。两个方向各用一只独立的信号量，互不干扰。

#### 4.1.2 核心流程

`BlockingRb` 的全部巧妙之处都体现在一个统一的接线规则上：**每当某个操作改变了「对端正在等待的状态」，就 `give` 对应那只信号量**。具体对应关系是：

```
生产者推进 write 索引 (set_write_index)  ──give──►  write 信号量  ──唤醒►  等数据的消费者
消费者推进 read  索引 (set_read_index)  ──give──►  read   信号量  ──唤醒►  等空间的生产者
设置/清除 read  hold 标志 (hold_read)    ──give──►  read   信号量  （含：消费者 drop → 关闭通知）
设置/清除 write hold 标志 (hold_write)   ──give──►  write  信号量  （含：生产者 drop → 关闭通知）
```

这正是 u6-l1 那条规律「**唤醒者名字 = 你等待的索引**」的阻塞版本：

- 消费者空时会去 `take` **write** 信号量（因为新数据要靠生产者推进 write 索引来通知）。
- 生产者满时会去 `take` **read** 信号量（因为腾空间要靠消费者推进 read 索引来通知）。

而「关闭」同样复用 hold 通道：对端 drop 时 hold 标志被复位，这次复位会 `give` 信号量，把还在 `take` 里睡觉的本端唤醒，本端醒来重新检查 `is_closed()`（=`!对端 hold 标志`）就能感知到对端已离开。

阻塞方法的通用等待循环因此长这样（伪代码，真实实现在 `BlockingProd` / `BlockingCons`）：

```text
for _ in take_iter(timeout).reset() {   // 取一次信号量（见 4.3）
    if 条件已满足 { 返回 Ok }
    if is_closed()    { 返回 Err(Closed) }
}
返回 Err(TimedOut)
```

注意它**不是「等到条件满足」**，而是「等到信号量被 `give`」。因为二值信号量会合并多次 `give`，醒来后**必须重新检查条件**，这正是 4.3 节 `TakeIter::reset` 要做「先排空一次」的原因。

#### 4.1.3 源码精读

先看 `BlockingRb` 的结构定义。[blocking/src/rb.rs:16-27](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/rb.rs#L16-L27) 中，它就是三个字段的组合：核心 `base: SharedRb<S>`，加上 `read`、`write` 两只信号量（类型参数 `X: Semaphore`）。

```rust
pub struct BlockingRb<S: Storage, X: Semaphore> {
    base: SharedRb<S>,
    pub(crate) read: X,
    pub(crate) write: X,
}
```

> 小细节：`std` feature 开启时，`X` 有默认值 `StdSemaphore`（`<X: Semaphore = StdSemaphore>`），所以平时写 `BlockingHeapRb<u8>` 不必手写信号量类型；关掉 `std` 时默认值消失，调用方必须显式指定自己的 `X`——这是「可插拔」留给 `no_std` 的入口。

构造器 [blocking/src/rb.rs:29-37](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/rb.rs#L29-L37) 很朴素：接收一个已建好的 `SharedRb`，两只信号量都用 `X::default()` 初始化（初值为「未通知」）。

```rust
pub fn from(base: SharedRb<S>) -> Self {
    Self { base, read: X::default(), write: X::default() }
}
```

`Observer` 的实现 [blocking/src/rb.rs:39-71](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/rb.rs#L39-L71) 全部**原样转发**给 `base`（`capacity` / 两个 `*_index` / 两个 `unsafe_slices*` / 两个 `*_is_held`），不掺入任何信号量逻辑——观测是只读的，不需要唤醒谁。

真正注入信号量的地方是 `Producer` / `Consumer` / `RingBuffer` 三个 trait 的「改写索引」原语。[blocking/src/rb.rs:72-95](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/rb.rs#L72-L95) 把 4.1.2 的接线规则落到了代码里：

```rust
impl<S, X: Semaphore> Producer for BlockingRb<S, X> {
    unsafe fn set_write_index(&self, value: usize) {
        unsafe { self.base.set_write_index(value) };  // 先推进底层原子索引
        self.write.give();                             // 再通知「写」信号量
    }
}
impl<S, X: Semaphore> Consumer for BlockingRb<S, X> {
    unsafe fn set_read_index(&self, value: usize) {
        unsafe { self.base.set_read_index(value) };
        self.read.give();                              // 通知「读」信号量
    }
}
impl<S, X: Semaphore> RingBuffer for BlockingRb<S, X> {
    unsafe fn hold_read(&self, flag: bool) -> bool {
        let old = unsafe { self.base.hold_read(flag) };
        self.read.give();                              // hold 变化也通知「读」信号量
        old
    }
    unsafe fn hold_write(&self, flag: bool) -> bool {
        let old = unsafe { self.base.hold_write(flag) };
        self.write.give();                             // hold 变化也通知「写」信号量
        old
    }
}
```

要点：

1. **先改底层，再 `give`**。顺序很重要：保证对端被唤醒去读索引时，新索引已经写入底层原子变量（对 `SharedRb` 是一次 `Release` store，见 [src/rb/shared.rs:125-127](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L125-L127)）。
2. `hold_read(flag)` 在 `flag` 为 `true`（创建读端）和 `false`（读端 drop）时**都**会 `give`。前者是一次无害的「伪唤醒」——生产者醒来重新检查发现还没空间、就继续等；后者才是真正有意义的「关闭通知」。
3. 其余所有写数据 / 读数据的便捷方法（`try_push` / `push_slice` / `try_pop` / `pop_slice` / …）都来自 `Producer` / `Consumer` 的**默认实现**，它们最终都会调用上面的 `set_write_index` / `set_read_index`，于是「自动」获得 `give` 行为。`BlockingRb` 本身没有重写任何数据搬运逻辑。

`BlockingRb` 同时实现了 `SplitRef`（[blocking/src/rb.rs:97-110](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/rb.rs#L97-L110)，借用 `&mut self`，不分配）和 `Split`（[blocking/src/rb.rs:111-120](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/rb.rs#L111-L120)，把 `self` 包进 `Arc` 再拆）。注意 `split` 出来的两端是 `BlockingProd<Arc<Self>>` / `BlockingCons<Arc<Self>>`——它们内部用 `Caching` 包装层做按需同步（见 u4-l4），阻塞语义则由 `BlockingWrap`（u7-l2）额外提供。

最后是一段重要的 trait 工程化代码 [blocking/src/rb.rs:122-129](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/rb.rs#L122-L129)：`BlockingRbRef` 是一个「萃取器」trait，配合 blanket impl，让任意「指向 `BlockingRb` 的智能指针」（`Arc<BlockingRb<…>>`、`&BlockingRb<…>`）都能在类型层面暴露出它的 `Storage` 与 `Semaphore` 关联类型，供 `BlockingProd` / `BlockingCons` 的泛型约束使用。它直接复用了核心 crate 的 `RbRef` 抽象（u4-l1）。

#### 4.1.4 代码实践

**实践目标**：用真实测试验证「写满→阻塞→消费者取走→被唤醒」的闭环，并对照源码确认 `give` / `take` 的方向。

**操作步骤**（源码阅读 + 可选运行）：

1. 打开 [blocking/src/tests.rs:25-68](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/tests.rs#L25-L68) 的 `wait` 测试。它建了一个容量仅 **7** 字节的 `BlockingHeapRb<u8>`，`split` 后让生产者线程把一段很长的文本分片 `push_slice` 进去，消费者线程用 `wait_occupied` + `pop_slice` 读出。
2. 容量 7 远小于文本长度，所以生产者很快会写满。此时它**不应报错也不应忙等**——`wait_vacant(1)` 会阻塞，直到消费者取走若干字节、推进了 read 索引。
3. 在 [blocking/src/wrap/prod.rs:18-22](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L18-L22) 的 `wait_iter!` 宏里确认：生产者等的是 **`read`** 信号量（`self.rb.rb().read.take_iter(...)`）。
4. 在 [blocking/src/rb.rs:78-83](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/rb.rs#L78-L83) 确认：消费者 `pop_slice` → 推进 read 索引 → `set_read_index` → `self.read.give()`。两端方向正好对上。

**需要观察的现象**：

- 测试通过（`assert_eq!(*smsg, rmsg)`），说明长文本被完整无损地传了过去，证明满/空时确实发生了阻塞与唤醒，而非丢数据。
- 若把容量从 7 改大（如 4096），生产者几乎不会阻塞；若改成 1，阻塞会更频繁——但结果都应一致。

**预期结果**：`cargo test -p ringbuf-blocking --test-threads=1 wait` 通过。（该测试标了 `#[cfg_attr(miri, ignore)]`，Mirri 下跳过。）

**待本地验证**：以上运行命令的具体输出请在本地执行后确认。

#### 4.1.5 小练习与答案

**练习 1**：消费者在缓冲区空时阻塞，等的是哪只信号量？谁会 `give` 它？

> **答案**：等的是 **write** 信号量（见 [blocking/src/wrap/cons.rs:18-22](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/cons.rs#L18-L22)）。由生产者的 `set_write_index` 在推进 write 索引后 `self.write.give()` 触发（[blocking/src/rb.rs:72-77](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/rb.rs#L72-L77)）。

**练习 2**：为什么 `hold_write(false)`（生产者 drop）也要 `self.write.give()`？

> **答案**：这是「关闭通知」。消费者可能正阻塞在 `take(write)` 上，生产者 drop 后不再会有新数据，必须唤醒消费者，让它重新检查 `is_closed()`（=`!write_is_held()`）并据此结束读取循环。

---

### 4.2 Semaphore trait：可插拔的二值信号量抽象

#### 4.2.1 概念说明

`BlockingRb` 的信号量被设计成一个 **trait**（`Semaphore`）而不是具体类型，这是 `ringbuf-blocking` 区别于一般阻塞队列的关键设计：**同步原语可插拔**。

- 在 `std` 环境，默认用 `StdSemaphore`（`Condvar + Mutex`，本讲 4.3 详述）。
- 在 `no_std` 嵌入式环境（没有 `std` 的线程、没有 `Condvar`），你可以提供自己的实现——比如基于 RTOS 信号量、基于 `cortex-m` 的 WFE 指令、或裸的自旋 + 原子。只要实现 `Semaphore` trait，同一套 `BlockingRb` / `BlockingProd` / `BlockingCons` 代码就能跑。

这与 `async-ringbuf`「运行时无关」的精神一致（u6-l1）：把「怎么等」从「等什么」里剥离出去，留给平台。

#### 4.2.2 核心流程

`Semaphore` 抽象的是一个**带超时的二值信号量**，三个核心方法：

| 方法 | 行为 | 返回 |
| --- | --- | --- |
| `give(&self)` | 把信号量置为「已通知」。已是「已通知」则什么都不做（多次 `give` 合并）。 | 无 |
| `try_take(&self)` | 非阻塞地尝试取走。已是「已取走」则什么都不做。 | 旧值（`true` 表示原本已通知） |
| `take(&self, timeout: Option<Duration>)` | 取走信号量；若当前未通知，则**阻塞等待**。 | 成功 `true` / 超时 `false` |

超时参数有三种取值，由 [blocking/src/sync.rs:8-9](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/sync.rs#L8-L9) 的两个常量命名，便于阅读：

- `NO_WAIT = Some(Duration::ZERO)`：不等待，只做一次非阻塞检查。
- `Some(d)`：最多等 `d`。
- `FOREVER = None`：无限等待。

此外 `Semaphore` 还带一个**关联类型** `type Instant: Instant`（[blocking/src/sync.rs:19](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/sync.rs#L19)），指定「用什么时钟计量已等待时间」——`std` 下是 `std::time::Instant`，`no_std` 下你可用自己提供的单调时钟。trait 还提供默认方法 `take_iter`（见 4.2.3），把 `take` 包装成一个可被 `for` 循环消费的迭代器，供阻塞方法统一使用。

#### 4.2.3 源码精读

`Semaphore` trait 定义在 [blocking/src/sync.rs:17-47](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/sync.rs#L17-L47)，逐段看：

```rust
pub trait Semaphore: Default {
    type Instant: Instant;

    fn give(&self);                       // 置位（幂等）
    fn try_take(&self) -> bool;           // 非阻塞取，返回旧值
    fn take(&self, timeout: Option<Duration>) -> bool;  // 阻塞取，true=成功 false=超时

    fn take_iter(&self, timeout: Option<Duration>) -> TakeIter<'_, Self> {
        TakeIter {
            reset: false,
            semaphore: self,
            timeout_iter: TimeoutIter::new(timeout),
        }
    }
}
```

几点说明：

- `Semaphore: Default`：要求能无参构造初值（「未通知」），`BlockingRb::from` 里 `X::default()` 就靠它。
- `take_iter` 是带默认实现的便捷方法：返回一个 `TakeIter`（4.3 节详述其 `reset` 机制）。它是阻塞 API 的核心齿轮——`BlockingProd` / `BlockingCons` 的所有阻塞方法都通过它来等待。

配套的时钟 trait `Instant` 在 [blocking/src/sync.rs:11-15](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/sync.rs#L11-L15)，只有 `now()` 和 `elapsed()` 两个方法。`std` 下直接复用标准库时钟，见 [blocking/src/sync.rs:52-60](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/sync.rs#L52-L60)（`StdInstant = std::time::Instant`）。

别名层 [blocking/src/alias.rs:13-16](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/alias.rs#L13-L16) 把信号量类型作为第二个类型参数 `X` 暴露给用户：

```rust
pub type BlockingHeapRb<T, X = StdSemaphore> = BlockingRb<Heap<T>, X>;
```

也就是说，`BlockingHeapRb<u8>` 等价于 `BlockingRb<Heap<u8>, StdSemaphore>`；想换实现就写 `BlockingHeapRb<u8, MySemaphore>`。`BlockingStaticRb` 同理（[blocking/src/alias.rs:25-28](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/alias.rs#L25-L28)）。构造器 `BlockingHeapRb::new(cap)`（[blocking/src/alias.rs:18-23](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/alias.rs#L18-L23)）内部就是 `BlockingRb::from(HeapRb::new(cap))`，把核心堆缓冲区包一层。

#### 4.2.4 代码实践

**实践目标**：跟踪 `Semaphore` trait 的 `take_iter` 如何被阻塞方法消费，体会「可插拔」带来的复用。

**操作步骤**：

1. 阅读 [blocking/src/wrap/prod.rs:36-47](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L36-L47) 的 `wait_vacant`：它的循环 `for _ in wait_iter!(self)` 里，`wait_iter!`（[blocking/src/wrap/prod.rs:18-22](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L18-L22)）展开为 `self.rb.rb().read.take_iter(self.timeout()).reset()`。
2. 注意整个 `wait_vacant` / `push` 方法**完全不引用 `StdSemaphore`**——它只依赖 `Semaphore` trait 的 `take_iter`、以及 `Observer` 的 `vacant_len` / `read_is_held`。
3. 由此得出结论：若把 `X` 换成自定义信号量，`BlockingProd` / `BlockingCons` 的代码**一行都不用改**。

**需要观察的现象**：阻塞方法的等待逻辑与具体信号量实现解耦，仅通过 `take_iter` 这个 trait 方法交互。

**预期结果**：能在源码中清晰画出 `push` → `wait_iter!` → `Semaphore::take_iter` → `TakeIter::next` → `Semaphore::take` 的调用链。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Semaphore` 要约束 `Default`？

> **答案**：`BlockingRb::from` 用 `X::default()` 初始化两只信号量为「未通知」初态（[blocking/src/rb.rs:30-36](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/rb.rs#L30-L36)）。没有 `Default` 就无法在泛型里统一构造。

**练习 2**：把超时设为 `FOREVER`（`None`）和 `NO_WAIT`（`Some(ZERO)`），分别对应什么行为？

> **答案**：`FOREVER` = 无限阻塞直到被 `give` 或对端关闭；`NO_WAIT` = 只做一次非阻塞检查，相当于「尝试一次就走」（具体由 `TimeoutIter` 在 4.3 实现）。

---

### 4.3 StdSemaphore 与 TimeoutIter / TakeIter：标准库的阻塞实现

#### 4.3.1 概念说明

`StdSemaphore` 是 `Semaphore` 在 `std` 下的默认实现，用的是经典的 **`Condvar + Mutex<bool>`** 模式——这正是 Rust 标准库里实现「条件变量」的标准姿势：用一个 `Mutex<bool>` 保护那个 0/1 标志，用 `Condvar` 来「通知 / 等待」。如果你熟悉 `std::sync::Condvar`，这里的代码会非常眼熟。

`bool` 就是信号量的「已通知 / 未通知」状态；`Condvar::notify_one` 就是 `give` 的「敲钟」；`Condvar::wait` / `wait_timeout` 就是 `take` 的「睡觉等钟声」。`Mutex` 的作用是给这个 `bool` 的读写提供互斥与内存可见性。

#### 4.3.2 核心流程

**`give`**：加锁 → 置 `bool = true` → `notify_one` 敲钟。

**`try_take`**：加锁 → `replace(&mut guard, false)`（取出旧值并复位）。

**`take(timeout)`**：这是最需要看懂的一段。它是一个「在循环里反复 检查标志 → 没有就睡 → 被唤醒再检查」的标准 condvar 等待循环，循环次数由 `TimeoutIter` 控制：

```text
加锁
for 剩余时间 in TimeoutIter::new(timeout):
    if 标志为 true: 取走(置 false) 并 return true
    match 剩余时间:
        Some(t): condvar.wait_timeout(guard, t); 若超时则 break
        None    : condvar.wait(guard)   # FOREVER：无期限睡
# 循环结束（超时）后做最后一次检查
return replace(&mut guard, false)
```

**`TimeoutIter`** 是一个产出「剩余可等待时间」的迭代器，把超时算术集中在一处。记初始时刻 `start`、总时长 `dur`，每次 `next` 计算：

\[
\text{remaining} = \text{dur} - \text{elapsed}
\]

- 若是有限超时 `Some(dur)`：只要 \(\text{dur} > \text{elapsed}\) 就产出 `Some(remaining)`；一旦耗尽就产出 `None`（迭代器结束 = 截止时刻到了）。
- 若是 `FOREVER`（`None`）：永远产出 `Some(None)`，永不结束。
- 若是 `NO_WAIT`（`Some(ZERO)`）：第一次 `next` 时 `dur=0`，`0 > 0` 为假，立刻产出 `None`——循环一次都不进，`take` 直接落到最后的非阻塞检查，等价于 `try_take`。

**`TakeIter`** 在 `Semaphore::take_iter` 上再套一层，提供两个能力：

1. 把「取一次信号量」变成可 `for` 循环消费的迭代器；某次 `take` 超时就结束迭代（让上层据此返回 `TimedOut`）。
2. **`.reset()` 机制**：把第一次 `next` 变成一次**非阻塞的 `try_take`**（排空可能已经 pending 的通知），然后再开始正式的阻塞 `take`。这保证等待循环「先无阻塞地复查一次条件、再决定要不要睡」，避免因为二值信号量合并通知而错过唤醒。4.1.2 伪代码里的 `take_iter(timeout).reset()` 就是它。

#### 4.3.3 源码精读

`StdSemaphore` 的结构极简（[blocking/src/sync.rs:62-67](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/sync.rs#L62-L67)）：

```rust
#[derive(Default)]
pub struct StdSemaphore {
    condvar: Condvar,
    mutex: Mutex<bool>,   // false=未通知, true=已通知
}
```

三个方法的实现见 [blocking/src/sync.rs:69-101](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/sync.rs#L69-L101)。`give` 与 `try_take` 一目了然：

```rust
fn give(&self) {
    let mut guard = self.mutex.lock().unwrap();
    *guard = true;
    self.condvar.notify_one();
}
fn try_take(&self) -> bool {
    replace(&mut self.mutex.lock().unwrap(), false)   // 返回旧值
}
```

`take` 是重头戏：

```rust
fn take(&self, timeout: Option<Duration>) -> bool {
    let mut guard = self.mutex.lock().unwrap();
    for timeout in TimeoutIter::<Self::Instant>::new(timeout) {
        if replace(&mut guard, false) {        // 先看标志，命中就成功
            return true;
        }
        match timeout {
            Some(t) => {
                let r;
                (guard, r) = self.condvar.wait_timeout(guard, t).unwrap();
                if r.timed_out() { break; }    // 真超时 → 跳出做最后检查
            }
            None => guard = self.condvar.wait(guard).unwrap(),  // FOREVER
        };
    }
    replace(&mut guard, false)                  // 最后一次检查（处理伪唤醒 / 超时前一刻到达）
}
```

要点：

- **持锁检查、释放锁睡觉**：`condvar.wait` 会在睡眠时原子地释放 `guard`、醒来时重新获取，这正是 condvar 能阻塞而不死锁的关键。
- **循环 + 最后检查**应对 condvar 的**伪唤醒**（spurious wakeup）——标准库允许 `wait` 在没人 `notify` 的情况下自己醒来，所以必须循环复查；循环结束后还做一次 `replace` 检查，处理「超时瞬间刚好被 `give`」的边界。
- `wait_timeout` 返回的 `r.timed_out()` 用来区分「真超时」和「被 notify 提前唤醒」——只有真超时才 `break`，被唤醒则进入下一轮复查。

`TimeoutIter` 在 [blocking/src/sync.rs:103-130](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/sync.rs#L103-L130)，核心就是前面那条 `remaining = dur - elapsed`：

```rust
fn next(&mut self) -> Option<Self::Item> {
    match self.timeout {
        Some(dur) => {
            let elapsed = self.start.elapsed();
            if dur > elapsed { Some(Some(dur - elapsed)) } else { None }
        }
        None => Some(None),   // FOREVER：永不结束
    }
}
```

注意每次 `next` 都用 `start.elapsed()` **重新**算剩余时间——这样多轮 `wait_timeout` 累计的睡眠时间不会超过原定的 `dur`，而非每轮都睡满 `dur`。

`TakeIter` 在 [blocking/src/sync.rs:132-158](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/sync.rs#L132-L158)：

```rust
fn next(&mut self) -> Option<()> {
    if self.reset {
        self.reset = false;
        self.semaphore.try_take();          // 第一次：非阻塞排空
        Some(())
    } else if self.semaphore.take(self.timeout_iter.next()?) {
        Some(())                             // 取到信号
    } else {
        None                                 // 超时，结束迭代
    }
}
pub fn reset(mut self) -> Self { self.reset = true; self }   // builder：开启首拍排空
```

把 `TakeIter` 与上层循环合起来看（以 `BlockingProd::push` 为例，[blocking/src/wrap/prod.rs:49-60](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L49-L60)）：

- 第 1 轮：`reset` → `try_take` 排空 → `try_push` 试写。若成功直接返回；这正是「数据面早已就绪、根本不用睡」的快路径。
- 第 2 轮起：`take(剩余时间)` 阻塞，直到消费者 `give`（腾出空间）或超时；醒来后再 `try_push`。超时则 `TakeIter` 结束，`push` 返回 `Err((TimedOut, item))`。

`NO_WAIT` 时 `TimeoutIter` 第一次就产出 `None`，`take_iter.next()` 的 `?` 让 `TakeIter` 在第 2 轮立即结束——于是整个循环只跑了第 1 轮（那一次非阻塞 `try_take`），等价于「试一次就走」。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：读懂 `StdSemaphore::take` 在「永久等待」与「超时」两种模式下的差别；并设计一个 `no_std` 信号量的实现思路。

**操作步骤**：

1. 打开 [blocking/src/sync.rs:82-101](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/sync.rs#L82-L101)，对照 4.3.2 的伪代码，逐行解释 `take`。
2. **永久等待模式**：设 `timeout = FOREVER`（`None`）。则 `TimeoutIter::new(None)` 每次产出 `Some(None)`、永不结束；循环里走 `None => condvar.wait(guard)` 分支，无限睡到被 `notify`。只有 `replace` 命中（被 `give`）才 `return true`。结论：除非被 `give`，否则一直阻塞。
3. **超时模式**：设 `timeout = Some(100ms)`。`TimeoutIter` 第一次产出 `Some(100ms)`，`condvar.wait_timeout(guard, 100ms)`；若被唤醒且未 `timed_out()`，重新算 `elapsed` 后产出更小的剩余时间再睡；累计睡满 100ms 仍没命中，`r.timed_out()` 为真 → `break` → 最后一次 `replace` 检查（可能恰好这一刻被 `give`）→ 返回 `false`（超时）。结论：最多阻塞 100ms。
4. **思考 no_std**：在没有 `std::sync` 的嵌入式平台，`Condvar` / `Mutex` 都不可用。请构思一个替代实现，例如：
   - 用一个 `AtomicBool` 存标志；`give` 置位；`take` 在标志为假时进入低功耗等待（ARM Cortex-M 上用 `wfe` 指令），被事件唤醒后复查；超时用一个硬件定时器中断来打破等待。
   - 或者对接所用 RTOS（如 FreeRTOS、embassy）自带的信号量句柄，在 `give`/`take` 里转发。
   - 关键约束：必须实现 `Semaphore`（含 `type Instant`、`Default`、`give`/`try_take`/`take`），并提供一个满足 `Instant` trait 的单调时钟；构造 `BlockingRb<S, MySemaphore>` 即可。

**需要观察的现象**：

- 超时分支里 `wait_timeout` 的返回值 `r` 既能判断是否真超时，循环里重新计算 `elapsed` 保证不超睡。
- `replace(&mut guard, false)` 在循环内、循环外各出现一次，分别处理「命中」与「超时前一刻命中」。

**预期结果**：能用一句话说清两种模式的差别——`FOREVER` 走 `condvar.wait` 无限睡、`Some(d)` 走 `wait_timeout` 且总睡眠时长受 `TimeoutIter` 严格约束；并能列出 `no_std` 自定义 `Semaphore` 必须满足的 trait 契约。

**待本地验证**：步骤 2/3 的精确计时行为可在本地用 `Instant::now()` 包住 `take` 调用来实测。

#### 4.3.5 小练习与答案

**练习 1**：`take` 的 `for` 循环结束后，为什么还要再执行一次 `replace(&mut guard, false)`？

> **答案**：处理两类情况——(a) condvar 的伪唤醒导致循环提前结束；(b) 「超时分支 `break` 的瞬间刚好有人 `give`」。这一次最后的非阻塞检查能捕获这些边界，避免漏掉已经到达的通知。

**练习 2**：`NO_WAIT` 时 `take` 实际会阻塞吗？为什么？

> **答案**：不会。`TimeoutIter::new(Some(ZERO))` 第一次 `next` 就产出 `None`（`0 > 0` 为假），`for` 循环体一次都不执行，直接落到最后的 `replace`——等价于一次 `try_take`。

**练习 3**：为 `no_std` 实现 `Semaphore` 时，`type Instant` 该怎么办？

> **答案**：必须提供一个实现 `Instant` trait（`now()` + `elapsed()`）的单调时钟类型，比如读取某个 SysTick 或 DWB 计数寄存器，把它作为 `type Instant`。若你的平台完全不需要超时，也要提供一个占位实现，因为 `take_iter` 默认方法会构造 `TimeoutIter<X::Instant>`。

## 5. 综合实践

把本讲三块知识串起来，完成一个「**画出 BlockingRb 的唤醒数据流，并预测一次满缓冲下的阻塞—唤醒时序**」的小任务：

1. **建图**：在纸上画出 `SharedRb`（中央）、`read`/`write` 两只信号量（两侧）、`BlockingProd`/`BlockingCons`（上下）四者的关系。标注：
   - `set_write_index` / `hold_write` → `write.give` → 消费者 `take(write)`；
   - `set_read_index` / `hold_read` → `read.give` → 生产者 `take(read)`。
2. **预测时序**：设容量 `cap = 2`。生产者连续 `push` A、B、C（C 时已满）。请按时间顺序写出：
   - 生产者 `push(C)` 进入 `wait_iter` → `try_take(read)` 排空 → `try_push` 失败 → `take(read)` 阻塞；
   - 消费者 `pop()` 取走 A → `set_read_index` → `read.give`；
   - 生产者被唤醒 → `try_push(C)` 成功 → `set_write_index` → `write.give`；
   - （若消费者紧接着 `pop` 空了）消费者 `take(write)` 被上一条 `write.give` 唤醒。
3. **代码佐证**：在 [blocking/src/rb.rs:72-95](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/rb.rs#L72-L95)、[blocking/src/wrap/prod.rs:49-60](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L49-L60)、[blocking/src/wrap/cons.rs:49-59](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/cons.rs#L49-L59) 中为每一步找到对应代码行。
4. **可选取运行**：仿照 [blocking/src/tests.rs:25-68](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/tests.rs#L25-L68) 写一个 `cap = 2` 的版本，在生产者/消费者的关键点加 `println!`（带线程名），观察实际唤醒顺序与你画的是否一致。**待本地验证**。

## 6. 本讲小结

- `BlockingRb<S, X>` = 核心 `SharedRb<S>` + 两只信号量 `X`（`read`、`write`）；数据面逻辑全部复用核心，阻塞层只在「改变对端可见状态」处 `give`。
- 接线规则与 `async-ringbuf` 对称：「**信号量名 = 你等待的索引**」：推进 write 索引 / hold_write → `write.give`（唤醒消费者）；推进 read 索引 / hold_read → `read.give`（唤醒生产者）。「关闭」复用 hold 通道，对端 drop 时 `give` 唤醒等待方。
- `Semaphore` 是一个**可插拔 trait**：`give` / `try_take` / `take(timeout)`，带关联时钟类型 `Instant`；`BlockingProd` / `BlockingCons` 只依赖 trait，换实现无需改业务代码。
- 默认实现 `StdSemaphore` 用 `Condvar + Mutex<bool>`，`take` 是带循环复查与最后检查的标准 condvar 等待，能正确处理伪唤醒与超时边界。
- `TimeoutIter` 用 `remaining = dur - elapsed` 统一管理三种超时模式：`FOREVER`（`None`，永不结束）、`Some(d)`（累计不超过 `d`）、`NO_WAIT`（`Some(ZERO)`，等价一次非阻塞检查）。
- `TakeIter.reset()` 让等待循环「先非阻塞排空一次、再决定阻塞」，避免二值信号量合并通知导致的漏唤醒。
- `no_std` 下没有 `Condvar`，但只要实现 `Semaphore` trait（含时钟）即可让整套 `BlockingRb` 在嵌入式平台运行——这正是把同步原语做成 trait 的回报。

## 7. 下一步学习建议

- **下一讲 u7-l2** 将建在本讲地基之上，系统讲解 `BlockingProd` / `BlockingCons` 的对外阻塞 API：`push` / `pop` / `wait_vacant` / `wait_occupied` / `push_exact` / `pop_exact` / `push_all_iter` / `pop_until_end`、`WaitError`（`TimedOut` / `Closed`）、`set_timeout`，以及 `std` 下的 `io::Read` / `io::Write` 集成。读完本讲后再看那一讲，你会发现所有等待逻辑都只是 `for _ in wait_iter!(self)` 这一模式的特化。
- 若想对照学习「异步版」如何用 `AtomicWaker` 解决同样的问题，可复习 u6-l1 与 u6-l2，体会「阻塞信号量」与「异步 waker」在唤醒注入点上的同构性。
- 想深入 `Condvar + Mutex` 这一经典模式，可阅读 `std::sync::Condvar` 的官方文档与本讲 [blocking/src/sync.rs:69-101](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/sync.rs#L69-L101) 对照。
- 对 `no_std` 自定义 `Semaphore` 感兴趣的读者，建议接着阅读所用嵌入式运行时（如 `embassy`、`cortex-m` RTIC）的同步原语文档，作为实现 `Semaphore` trait 的素材。
