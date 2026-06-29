# AsyncProducer/AsyncConsumer：Future、等待与取消安全

## 1. 本讲目标

本讲承接 [u6-l1](u6-l1-asyncrb-atomicwaker.md)（在无锁核心 `SharedRb` 上叠加 `AtomicWaker` 唤醒）。上一讲我们只讲了「唤醒被安插在哪几个底层点」，这一讲要回答读者真正会问的问题：

- 我怎么写出 `prod.push(x).await`、`cons.pop().await` 这样的异步代码？
- 这些 `.await` 背后到底返回了什么？为什么能「等待」？
- 如果一个 Future 被提前丢弃（取消），缓冲区里的数据会不会坏掉、会不会重复写入或丢失？

学完后你应当能够：

1. 说出 `AsyncProducer` / `AsyncConsumer` 两个 trait 各提供了哪些 async 方法（`push` / `pop` / `wait_vacant` / `wait_occupied` / `push_exact` / `pop_exact` / `push_iter_all` / `pop_until_end`），以及它们返回值的含义。
2. 看懂每个 async 方法返回的具名 Future（`PushFuture` / `PopFuture` 等），理解它们内部统一的「轮询循环 + 注册 waker」结构。
3. 解释 `close` / `is_closed` 如何通过对端 drop 触发，让正在 `await` 的 Future 优雅收尾（`pop` 返回 `None`、`push` 返回 `Err(item)`）。
4. 看懂每种 Future 的**取消安全（cancel safety）**约定：取消时缓冲区不会损坏，并知道哪些方法的数据「完全不丢」、哪些「可能部分提交但可查询」、哪些「元素随 Future 一起丢弃」。

## 2. 前置知识

本讲假设你已掌握以下内容（来自前置讲义）：

- **SPSC 不变量与 hold 标志**（[u5-l2](u5-l2-hold-flags-spsc-invariant.md)）：ringbuf 是「至多一个生产者、一个消费者」的队列，`read_held` / `write_held` 两个原子 bool 编码两端占用。**这是本讲理解 `is_closed` 的钥匙**：对端被 drop 后会复位它那一侧的 hold 标志，于是 `is_closed()` 变成真。
- **核心 Producer / Consumer trait**（[u3-l2](u3-l2-producer-trait.md)、[u3-l3](u3-l3-consumer-trait.md)）：`try_push` 满则返回 `Err(item)`、`try_pop` 空则返回 `None`，且都是非阻塞的。本讲所有 Future 内部最终都落到这两个方法上。
- **委托机制**（[u3-l5](u3-l5-delegation-based.md)）：包装器用空标记 trait + blanket impl 把方法零成本转发给内部 base。
- **AsyncRb + AtomicWaker**（[u6-l1](u6-l1-asyncrb-atomicwaker.md)）：`AsyncRb` = `SharedRb` + 两个 `AtomicWaker`（`read`、`write`），唤醒注入点在 `set_write_index` / `set_read_index` / `hold_*`。本讲的 `register_waker` 就是往这两个 waker 上注册。

下面补充两个本讲要用到的 Rust 异步基础概念：

- **Future 与 poll**：Rust 的 `Future` 不主动运行，而是由运行时（executor）反复调用 `poll(Pin<&mut Self>, &mut Context)`。`poll` 返回 `Poll::Ready(x)`（完成）或 `Poll::Pending`（还没好，请稍后再来）。返回 `Pending` 前，实现者应当把调用方的 `Waker`（从 `Context` 取得）记下来，等条件满足时调用 `waker.wake()` 通知运行时重新 poll。这正是 ringbuf 注册 `AtomicWaker` 的目的。
- **FusedFuture**：标准 `Future` 完成后再被 poll 是「未定义行为」。`FusedFuture` 在此之上加了 `is_terminated(&self) -> bool`，让运行时知道「这个 Future 已经结束，不要再 poll 它」。本讲里每个 Future 都实现了 `FusedFuture`。
- **取消（cancel）= drop Future**：Rust 里「取消一个异步操作」的官方机制就是把它的 Future **丢弃（drop）**。运行时不会打断一个正在执行的 `poll`，取消只会发生在两次 `poll` 之间。因此「取消安全」的核心问题是：**Future 被丢弃那一刻，它持有的状态（要写入的元素、已拷贝的计数等）会不会让缓冲区处于不一致状态？**

## 3. 本讲源码地图

本讲涉及的文件都在派生 crate `async-ringbuf`（仓库 `async/` 目录）内：

| 文件 | 作用 |
| --- | --- |
| [async/src/traits/producer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs) | 定义 `AsyncProducer` trait 与所有「写端 Future」（`PushFuture` / `PushSliceFuture` / `PushIterFuture` / `WaitVacantFuture`）。 |
| [async/src/traits/consumer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs) | 定义 `AsyncConsumer` trait 与所有「读端 Future」（`PopFuture` / `PopSliceFuture` / `PopVecFuture` / `WaitOccupiedFuture`）。 |
| [async/src/wrap/mod.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/mod.rs) | `AsyncWrap` 包装器与别名 `AsyncProd` / `AsyncCons`。 |
| [async/src/wrap/prod.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/prod.rs) | 为 `AsyncProd` 实现 `AsyncProducer`（关键：`register_waker` / `close`）以及 `Sink`。 |
| [async/src/wrap/cons.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/cons.rs) | 为 `AsyncCons` 实现 `AsyncConsumer`（关键：`register_waker` / `close`）以及 `Stream`。 |
| [async/src/rb.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/rb.rs) | `AsyncRb`：唤醒真正发生的地方（`set_*_index` / `hold_*` 里调用 `AtomicWaker::wake`）。 |
| [async/examples/simple.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/examples/simple.rs) | 最小可运行示例，本讲多处实践以它为蓝本。 |

一句话总览层次关系：`AsyncProd/AsyncCons`（包装器）→ 委托得到核心的 `try_push/try_pop`（数据面）+ 自己实现 `register_waker/close`（控制面）→ `AsyncProducer/AsyncConsumer` trait 在其上提供 async 方法 → 每个方法返回一个具名 Future → Future 的 `poll` 用「循环 + 注册 waker」把核心的非阻塞 `try_*` 变成「可等待」的异步操作，唤醒由 `AsyncRb` 里的 `AtomicWaker` 驱动。

## 4. 核心概念与源码讲解

### 4.1 包装器骨架与 trait 契约：AsyncProd / AsyncCons、register_waker、close / is_closed

#### 4.1.1 概念说明

上一讲我们看到 `AsyncRb` 只是把唤醒能力加进了底层缓冲区，它本身并没有 `push().await` 这种方法。要让用户写出异步代码，需要一层「异步包装器」：

- `AsyncWrap<R, P, C>`：与核心 crate 的 `Direct` 同构——一把「钥匙」`R`（指向 `AsyncRb` 的智能指针，如 `Arc<AsyncRb<..>>`）加两个 const generic 布尔 `P`（写权）、`C`（读权）。它内部其实就包了一个核心 `Direct`（[async/src/wrap/mod.rs:11-13](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/mod.rs#L11-L13)）。
- 两个别名：`AsyncProd<R> = AsyncWrap<R, true, false>`（只写）、`AsyncCons<R> = AsyncWrap<R, false, true>`（只读）（[async/src/wrap/mod.rs:15-16](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/mod.rs#L15-L16)）。结合 `async/src/alias.rs`，常用的就是 `AsyncHeapProd<T>`（基于 `Arc`）和 `AsyncStaticProd<'a, T, N>`（基于 `&'a`）。

`AsyncProducer` 与 `AsyncConsumer` 两个 trait 在核心 `Producer` / `Consumer` 之上**只新增三个控制面方法**：

- `register_waker(&self, waker)`：把调用方的 waker 登记到底层 `AsyncRb` 的某个 `AtomicWaker` 上。
- `close(&mut self)`：主动关闭本端。
- `is_closed(&self) -> bool`：对端是否已关闭（已被 drop 或主动 close）。

数据面（`try_push` / `try_pop` / `push_slice` / `pop_slice` / 各种 `*_len` / `is_full` / `is_empty` 等）完全由核心 trait 提供，包装器通过委托（[u3-l5](u3-l5-delegation-based.md)）零成本继承，本讲不再重复。

#### 4.1.2 核心流程

关键问题是「`register_waker` 登记到哪个 waker」和「`is_closed` 凭什么判断」。两条规则：

1. **waker 名 = 你等待的索引**（沿用 [u6-l1](u6-l1-asyncrb-atomicwaker.md)）：
   - 生产者等「有空位」，空位来自消费者推进 `read` 索引 → 生产者登记到 **`read`** waker。
   - 消费者等「有元素」，元素来自生产者推进 `write` 索引 → 消费者登记到 **`write`** waker。
2. **`is_closed` = 对端那一侧的 hold 标志已经复位**：
   - 生产者的 `is_closed()` 返回 `!self.read_is_held()`——读端（消费者）不再被持有，即消费者已关闭（[async/src/traits/producer.rs:17-19](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L17-L19)）。
   - 消费者的 `is_closed()` 返回 `!self.write_is_held()`——写端（生产者）不再被持有，即生产者已关闭（[async/src/traits/consumer.rs:16-18](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs#L16-L18)）。

「关闭」与「唤醒」如何串起来？以**生产者 drop，消费者 `pop().await` 返回 `None`** 为例：

```text
prod 被 drop
  └─ AsyncWrap 内部的 Direct 被 drop（核心 crate 的 Drop）
       └─ Direct::close 复位 write_held（见 u5-l2）
            └─ AsyncRb::hold_write(false)  ← 这里额外调用 self.write.wake()
                 └─ 消费者登记的 write waker 被唤醒
                      └─ PopFuture 重新 poll
                           └─ try_pop 仍空，但 is_closed() == true
                                └─ 返回 Poll::Ready(None)
```

也就是说，「关闭」复用了 [u6-l1](u6-l1-asyncrb-atomicwaker.md) 讲过的 `hold_*` 唤醒通道：drop/close 时翻一下 hold 标志，既复位了占用状态，又顺带唤醒了对端，对端复查 `is_closed()` 就能收尾。

#### 4.1.3 源码精读

**包装器与别名**——`AsyncWrap` 内部就是一个 `Option<Direct<R,P,C>>`，并实现 `Based` / `Wrap` / `DelegateObserver` 以接入委托体系（[async/src/wrap/mod.rs:11-48](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/mod.rs#L11-L48)）：

```rust
pub struct AsyncWrap<R: AsyncRbRef, const P: bool, const C: bool> {
    base: Option<Direct<R, P, C>>,   // 包了一个核心 Direct
}
pub type AsyncProd<R> = AsyncWrap<R, true, false>;
pub type AsyncCons<R> = AsyncWrap<R, false, true>;
```

注意 `base` 是 `Option<...>` 而非直接 `Direct`。这是为了让 `close()` 能用 `self.base.take()` 把内部 Direct **取走并 drop**，从而触发上面那条「复位 hold → 唤醒」的链路（[async/src/wrap/prod.rs:28-31](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/prod.rs#L28-L31)、[async/src/wrap/cons.rs:26-29](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/cons.rs#L26-L29)）：

```rust
impl<R: AsyncRbRef> AsyncProducer for AsyncProd<R> {
    fn register_waker(&self, waker: &core::task::Waker) {
        self.rb().read.register(waker)   // 生产者登记到 read waker
    }
    #[inline]
    fn close(&mut self) {
        drop(self.base.take());          // 取走 Direct 并丢弃 → 复位 write_held → write.wake()
    }
}
```

`AsyncCons` 完全对称，只是 `register_waker` 登记到 **`write`** waker（[async/src/wrap/cons.rs:21-30](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/cons.rs#L21-L30)）。

**唤醒真正发生的地方**在 `AsyncRb` 的 `Producer` / `Consumer` / `RingBuffer` impl 里。每次推进索引或翻 hold 标志，都额外调一次 `wake()`（[async/src/rb.rs:74-99](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/rb.rs#L74-L99)）：

```rust
impl<S: Storage> Producer for AsyncRb<S> {
    unsafe fn set_write_index(&self, value: usize) {
        unsafe { self.base.set_write_index(value) };
        self.write.wake();   // 推进 write → 唤醒等元素的消费者
    }
}
// Consumer::set_read_index 对称地 self.read.wake()
// RingBuffer::hold_write / hold_read 也各自 wake()
```

这就是为什么 `prod.push(x).await` 能被「消费者取走一个元素」唤醒：消费者 `pop` 成功后核心会 `set_read_index` → `read.wake()` → 生产者注册在 `read` waker 上的 waker 被调用 → 运行时重新 poll `PushFuture`。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证「对端 drop 让 `is_closed()` 翻转」并理解唤醒链路。
2. **操作步骤**：
   - 阅读 [async/examples/simple.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/examples/simple.rs)。注意消费者末尾那行 `assert_eq!(cons.pop().await, None);`——它之所以能返回 `None`，正是因为生产者 task 把 `prod` 走出了作用域被 drop。
   - 在消费者 task 里，于第一次 `pop` 之前手动打印 `cons.is_closed()`，在生产者循环结束后、消费者收尾的 `pop` 之前再打印一次（可用 `futures::join!` 的执行交错性质，或改成 `drop(prod)` 后打印）。
3. **需要观察的现象**：第一次打印应为 `false`（生产者还活着，`write_is_held()` 为真）；生产者 drop 后打印应为 `true`。
4. **预期结果**：`is_closed()` 从 `false` 变 `true`，随后 `pop().await` 立即返回 `None`。
5. 若想看清唤醒，可临时在 `register_waker` / `close` 处加 `println!`（需 `std` 环境），观察消费者 `pop` 先返回 `Pending`、生产者 `close` 后被唤醒。改动只用于本地观察，不要提交。

#### 4.1.5 小练习与答案

**练习 1**：为什么生产者的 `register_waker` 登记到 `read` waker，而不是 `write` waker？
**答案**：生产者等待的是「有空位」，而空位只有在消费者推进 `read` 索引时才会出现（`set_read_index` 会触发 `read.wake()`）。登记到 `write` waker 没有意义，因为 `write` waker 是被写操作唤醒的，而写操作正是生产者自己。

**练习 2**：`AsyncProd::close()` 里只有一行 `drop(self.base.take())`，它如何同时做到「复位 hold 标志」和「唤醒对端」？
**答案**：`take()` 取走内部的 `Direct`，`drop` 它会触发核心 `Direct` 的 `Drop`（见 [u4-l2](u4-l2-direct-wrapper.md)、[u5-l2](u5-l2-hold-flags-spsc-invariant.md)），后者调用 `hold_write(false)` 复位写占用标志。又因为这里 `Self::Rb` 是 `AsyncRb`，其 `RingBuffer::hold_write` 实现里在 `base.hold_write(false)` 之后额外调用了 `self.write.wake()`（[async/src/rb.rs:93-98](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/rb.rs#L93-L98)），于是对端消费者被唤醒。

---

### 4.2 写端 AsyncProducer 与 PushFuture 家族

#### 4.2.1 概念说明

`AsyncProducer` trait（[async/src/traits/producer.rs:12-134](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L12-L134)）把核心的、非阻塞的 `try_push` / `push_slice` / `push_iter` 包成「满了就等」的异步方法。它提供以下 async 方法，每个都返回一个**具名 Future 类型**（不是 `Box<dyn Future>`，零分配、按值返回）：

| 方法 | 返回类型 | 完成时返回 |
| --- | --- | --- |
| `push(item)` | `PushFuture` | `Ok(())` 写入成功；`Err(item)` 消费者已关闭 |
| `push_exact(&[T])`（需 `Copy`） | `PushSliceFuture` | `Ok(())` 全部拷入；`Err(count)` 消费者关闭，`count` 为已拷贝数 |
| `push_iter_all(iter)` | `PushIterFuture` | `true` 迭代器耗尽；`false` 消费者关闭 |
| `wait_vacant(count)` | `WaitVacantFuture` | `()`：空位达 `count` 个或已关闭 |

此外还有两个底层 `poll_*` 方法（`poll_ready` 返回 `Poll<bool>`、`poll_write` 在 `std` 下用于字节流），它们是 `Sink` / `AsyncWrite` 适配的基石（`Sink` / `AsyncWrite` 的完整集成留到 [u6-l3](u6-l3-stream-sink-async-io.md)）。

#### 4.2.2 核心流程

所有写端 Future 共享同一个「**轮询循环 + 注册 waker 防丢唤醒**」骨架（伪代码）：

```text
fn poll():
    waker_registered = false
    loop:
        if 对端已关闭:          return Ready(Err/None)
        尝试用核心 try_push / push_slice / push_iter 写入
        if 写入成功 / 写完:      return Ready(Ok)
        # 还没写满需求，但缓冲区暂时满了
        if waker_registered:    return Pending   # 已注册且仍不行 → 真正去等
        register_waker(cx.waker)                 # 第一次：登记 waker
        waker_registered = true
        # 回到 loop 顶部再查一次（防丢唤醒：登记与检查之间的竞态由这一轮兜底）
```

这个「先尝试 → 失败则登记 waker → 再查一次 → 仍失败才 Pending」的两轮结构，正是上一讲提到的「先 register，再复查」防丢唤醒策略的具体落地。`PushFuture` 还多了一步：把待写元素在 `Option` 里「取出来写、写失败再放回去」，这是它的取消安全关键（详见 4.4）。

#### 4.2.3 源码精读

**`push` 与 `PushFuture`**——`push` 不做任何实际工作，只是把元素装进 Future（[async/src/traits/producer.rs:30-35](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L30-L35)）：

```rust
fn push(&mut self, item: Self::Item) -> PushFuture<'_, Self> {
    PushFuture { owner: self, item: Some(item) }
}
```

`PushFuture` 的字段是 `item: Option<A::Item>`（[async/src/traits/producer.rs:139-142](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L139-L142)）。每次 poll 先 `take()` 把元素取到局部变量，`try_push` 成功就消耗掉（返回 `Ok`），失败则用 `unwrap_err()` 拿回元素放回 `Option`（[async/src/traits/producer.rs:149-171](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L149-L171)）：

```rust
fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
    let mut waker_registered = false;
    loop {
        let item = self.item.take().unwrap();        // 取出待写元素
        if self.owner.is_closed() {
            break Poll::Ready(Err(item));            // 对端关闭：把元素还回去
        }
        let push_result = self.owner.try_push(item);
        if push_result.is_ok() {
            break Poll::Ready(Ok(()));               // 写入成功
        }
        self.item.replace(push_result.unwrap_err()); // 没写进去：放回 Option
        if waker_registered {
            break Poll::Pending;                      // 已登记仍满 → 等待
        }
        self.owner.register_waker(cx.waker());
        waker_registered = true;
    }
}
```

**`push_exact`（`PushSliceFuture`）**——从不可变切片 `&[T]` 批量拷入，记录已拷贝数 `count`（[async/src/traits/producer.rs:199-220](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L199-L220)）。每轮 `push_slice` 拷一段、把切片前移 `&slice[len..]`、累加 `count`；拷空返回 `Ok`，关闭返回 `Err(count)`。它要求 `Item: Copy`，因为底层用的是按字节复制的 `push_slice`。

**`push_iter_all`（`PushIterFuture`）**——把任意迭代器的元素全部推入。这里用了一个巧思：把迭代器包成 `Peekable`，每轮 `push_iter` 尽量推、再用 `peek()` 判断是否已耗尽，而不必实际 `next()` 一个来试探（[async/src/traits/producer.rs:246-265](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L246-L265)）：

```rust
self.owner.push_iter(&mut iter);
if iter.peek().is_none() {
    break Poll::Ready(true);   // 迭代器耗尽
}
```

它还提供 `inner()` / `inner_mut()` / `into_inner()`，让你在取消后把剩余迭代器取回来（4.4 详述）。

**`wait_vacant`（`WaitVacantFuture`）**——纯等待，不动数据，用 `count` 与 `vacant_len()` 比较，用 `done` 标志防重复完成（[async/src/traits/producer.rs:281-310](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L281-L310)）。注意签名是 `&mut self`：因为 `AtomicWaker` 是单 waker 原语，同一端同时只能有一个等待 Future。

#### 4.2.4 代码实践

1. **实践目标**：观察 `push().await` 在缓冲区满时「挂起」，被消费者取走后才继续。
2. **操作步骤**：复制 [async/examples/simple.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/examples/simple.rs)，把容量从 `10` 改成 `2`，把迭代次数 `N_ITERS` 改成 `5`，并在生产者每次 `push` 前后各加一条 `println!("push {}", i)`。
3. **需要观察的现象**：因为容量只有 2，生产者会在 push 完前两个后挂起，等消费者取走元素后才继续；消费者与生产者的打印会交错出现（而非生产者一次性打完）。
4. **预期结果**：输出呈现「生产者推 2 个 → 消费者取若干 → 生产者继续推」的交错节奏，最终消费者收到 0..5 且最后一次 `pop().await` 为 `None`。
5. 若用 `futures::executor::block_on` + `join!` 跑出交错，即验证了「满则 await、被消费唤醒」的语义。如果运行环境无法执行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`push_exact` 为什么要求 `Item: Copy`，而 `push_iter_all` 不要求？
**答案**：`push_exact` 内部用核心的 `push_slice`（按字节复制，要求 `Copy`）从不可变切片拷入；`push_iter_all` 用 `push_iter`，逐个从迭代器 `next()` 取出元素并移入缓冲区（move 语义），所以不要求 `Copy`。

**练习 2**：`PushFuture::poll` 里为什么先 `self.item.take()`，失败后又 `self.item.replace(...)`，而不是直接持有元素不放？
**答案**：`take()` 把元素移到局部变量，便于 `try_push(item)` 消费它；`try_push` 失败时返回 `Err(item)` 把元素原样退回，于是用 `replace` 放回 `Option`。这样保证「无论怎么取消，元素要么在 `Option` 里、要么已成功进缓冲区、要么随 `Err` 返回给调用者」，不会出现「元素既进了缓冲区又留在 Future 里」的双重拥有。这是取消安全的实现基础（见 4.4）。

---

### 4.3 读端 AsyncConsumer 与 PopFuture 家族

#### 4.3.1 概念说明

`AsyncConsumer` trait（[async/src/traits/consumer.rs:11-127](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs#L11-L127)）是写端的镜像，把核心非阻塞的 `try_pop` / `pop_slice` 包成「空了就等」的异步方法：

| 方法 | 返回类型 | 完成时返回 |
| --- | --- | --- |
| `pop()` | `PopFuture` | `Some(item)` 取到一个；`None` 空且生产者已关闭 |
| `pop_exact(&mut [T])`（需 `Copy`） | `PopSliceFuture` | `Ok(())` 切片填满；`Err(count)` 生产者关闭，`count` 为已拷贝数 |
| `pop_until_end(&mut Vec<T>)`（需 `alloc`） | `PopVecFuture` | `()`：生产者已关闭，vec 含所有取出元素 |
| `wait_occupied(count)` | `WaitOccupiedFuture` | `()`：元素达 `count` 个或已关闭 |

底层 `poll_next`（返回 `Poll<Option<T>>`）是 `Stream` 适配的基石。注意 `pop` 的关闭语义：**只有当缓冲区已空且生产者已关闭**时才返回 `None`——如果还有积压元素，即便生产者已 drop，也会先把元素全部 `pop` 出来再返回 `None`（见下面的 poll 顺序）。

#### 4.3.2 核心流程

读端 Future 的轮询循环与写端同构，但有两个值得注意的细节：

- `PopFuture` 用 `done: bool` 标志（而非 `Option`）来防重复完成，因为取出的元素直接作为 `Poll::Ready(Some(item))` 返回，不需要存在结构体里（[async/src/traits/consumer.rs:145-163](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs#L145-L163)）。
- 关闭判断的顺序很关键：先读 `is_closed()` 快照，再 `try_pop()`。这样能保证「先取完积压、空了再因关闭返回 `None`」。

```text
fn poll():
    waker_registered = false
    loop:
        closed = is_closed()           # 先快照关闭状态
        if let Some(item) = try_pop(): # 先尝试取
            done = true; return Ready(Some(item))
        if closed:                      # 取不到且对端关闭
            return Ready(None)
        if waker_registered: return Pending
        register_waker(write_waker)
        waker_registered = true
```

#### 4.3.3 源码精读

**`pop` 与 `PopFuture`**——结构体只持有 `owner` 和 `done`（[async/src/traits/consumer.rs:132-164](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs#L132-L164)）。注意 `is_terminated` 把「已完成」和「对端已关闭」都算作终止（`self.done || self.owner.is_closed()`），因为关闭后再 poll 也只会返回 `None`：

```rust
fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
    let mut waker_registered = false;
    loop {
        assert!(!self.done);
        let closed = self.owner.is_closed();
        if let Some(item) = self.owner.try_pop() {
            self.done = true;
            break Poll::Ready(Some(item));   // 取到 → 直接返回
        }
        if closed {
            break Poll::Ready(None);          // 空且关闭 → None
        }
        if waker_registered {
            break Poll::Pending;
        }
        self.owner.register_waker(cx.waker());  // 登记到 write waker
        waker_registered = true;
    }
}
```

**`poll_next`（trait 默认方法）**——与 `PopFuture::poll` 等价的「取一个」逻辑，返回 `Poll<Option<T>>`，是 `Stream` 的基础（[async/src/traits/consumer.rs:86-105](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs#L86-L105)）。`AsyncCons` 的 `Stream` impl（[async/src/wrap/cons.rs:32-52](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/cons.rs#L32-L52)）内联了同样一段循环。

**`pop_until_end`（`PopVecFuture`，需 `alloc`）**——持续把元素收进一个 `Vec`，直到生产者关闭。它用 `Vec::spare_capacity_mut()` 拿到未初始化空间，再用核心的 `pop_slice_uninit` 直接写入并 `unsafe { vec.set_len(...) }` 推进长度，避免逐个 `push`（[async/src/traits/consumer.rs:242-273](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs#L242-L273)）：

```rust
let n = self.owner.pop_slice_uninit(vec.spare_capacity_mut());
if n == 0 { break; }
unsafe { vec.set_len(vec.len() + n) };
```

**`pop_exact`（`PopSliceFuture`）** 与写端 `push_exact` 对称，把元素填入可变切片 `&mut [T]`，记录 `count`，提供 `count()` 查询已填数（[async/src/traits/consumer.rs:186-214](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs#L186-L214)）。**`wait_occupied`（`WaitOccupiedFuture`）** 与 `wait_vacant` 对称，纯等待（[async/src/traits/consumer.rs:289-308](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs#L289-L308)）。

#### 4.3.4 代码实践

1. **实践目标**：验证「生产者 drop 后，消费者先把积压元素取完，最后才返回 `None`」。
2. **操作步骤**：基于 [async/examples/simple.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/examples/simple.rs)，改写消费者 task：先用一个 `Vec` 收集所有 `pop().await` 的结果，直到拿到 `None`，最后断言 `vec == (0..N_ITERS).collect()`。
3. **需要观察的现象**：消费者循环会在某一次 `pop().await` 返回 `None` 时停止；在此之前拿到的元素恰好是生产者发出的全部元素。
4. **预期结果**：`vec` 长度等于 `N_ITERS` 且内容为 `0..N_ITERS`，证明积压被取尽后才因关闭收尾。
5. 若无法运行，标注「待本地验证」；亦可纯源码验证：阅读 [PopFuture::poll](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs#L145-L163) 中「先 `try_pop`、空才看 `closed`」的顺序，推出同样结论。

#### 4.3.5 小练习与答案

**练习 1**：`PopFuture` 为什么用 `done: bool` 而不是像 `PushFuture` 那样用 `Option<Item>`？
**答案**：`pop` 取出的元素直接作为 `Poll::Ready(Some(item))` 的返回值交出去了，不需要在结构体里保存。因此只需一个 `done` 标志防止「完成后被重复 poll」。而 `push` 的待写元素必须留在 Future 里（否则没法在多次 poll 之间携带它），所以用 `Option<Item>`。

**练习 2**：若生产者在发了 3 个元素后立即 drop，消费者用 `pop_until_end(vec).await`，`vec` 最终含几个元素？
**答案**：3 个。`PopVecFuture` 会先把缓冲区里积压的 3 个全部取出，然后发现 `is_closed()` 为真且取不到更多，才返回。关闭不会丢弃已发出的元素。

---

### 4.4 取消安全（cancel safety）与 FusedFuture

#### 4.4.1 概念说明

「取消安全」是异步编程里最容易被忽视、却最关键的正确性问题。它的含义是：**当一个 Future 在 `await` 点被丢弃时，它所操作的资源（这里是环形缓冲区）不会进入不一致或损坏的状态**。对 ringbuf 而言，具体要保证：不会把同一个元素写两次、不会把已取出的元素弄丢到「无人拥有」、不会让缓冲区的读写索引与实际数据错位。

ringbuf 的每个 async 方法都在文档里写明了取消安全约定（源码里以 `# Cancel safety` 段落标注）。本模块把它们汇总成一张对照表，并解释「为什么能做到」。

#### 4.4.2 核心流程

ringbuf 实现取消安全的核心机制有两条：

1. **「先 take、失败放回 / 完成置 done」的状态机**：每次 `poll` 都把「本操作的临时状态」放在 Future 字段里，要么完整提交（并标记完成）、要么原样回退。取消（drop）只发生在两次 `poll` 之间，那时 Future 字段反映的是「尚未成功」的状态，丢弃它不会留下半截提交。
2. **核心 `try_*` 的原子性**：单次 `try_push` / `try_pop` 本身就是「全部成功或全部不变」，不存在「半个元素」的中间态；Future 只是把多个 `try_*` 串起来，并用计数 `count` 记录已成功多少。

据此，各 Future 的取消安全分三档：

| 方法（Future） | 取消（drop Future）后的状态 | 等级 |
| --- | --- | --- |
| `push`（`PushFuture`） | 元素**未进缓冲区**；元素随 Future 一起 drop（无法取回） | 安全（不损坏，但元素丢失） |
| `pop`（`PopFuture`） | **无元素被移除** | 完全安全 |
| `wait_vacant`（`WaitVacantFuture`） | 无数据 | 完全安全 |
| `wait_occupied`（`WaitOccupiedFuture`） | 无数据 | 完全安全 |
| `push_exact`（`PushSliceFuture`） | 可能**部分拷入**，已拷贝数用 `.count()` 查询；原切片仍完整（不可变借用） | 安全（可查询进度） |
| `pop_exact`（`PopSliceFuture`） | 切片可能**部分填充**，已填数用 `.count()` 查询 | 安全（可查询进度） |
| `push_iter_all`（`PushIterFuture`） | 已推入的留在缓冲区；剩余留在迭代器，用 `into_inner()` **可取回** | 安全（无丢失） |
| `pop_until_end`（`PopVecFuture`） | `vec` 含**取消前已取出**的元素 | 安全（部分收集） |

#### 4.4.3 源码精读

**`PushFuture` 为何取消时不会多 push 一个元素**——这是规格里特别要求理解的一点。秘密在 `poll` 开头的 `take()` 与失败后的 `replace()`（[async/src/traits/producer.rs:152-169](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L152-L169)）：

- `poll` 一开始 `let item = self.item.take().unwrap();` 把元素从 `Option` 移到**局部变量**。
- 若 `try_push` 成功：元素被消费，`self.item` 为 `None`，返回 `Ok`。
- 若 `try_push` 失败（满）：用 `self.item.replace(err)` 把元素**放回 `Option`**，然后 `Pending`。

关键推论：**在两次 `poll` 之间（即取消可能发生的唯一时刻），元素要么已经成功进缓冲区（`self.item == None`，Future 已 `Ready`），要么还在 `self.item` 里**。取消时若是后者，元素随 `PushFuture` 一起被 drop——它**从未进过缓冲区**，所以绝不会出现「缓冲区里有一份、调用方又以为没写进去」的双重问题。代价是：取消后这个元素**丢失了**（无法从已 drop 的 Future 取回，字段是私有的）。这就是文档「If future is cancelled no item pushed to the RB」的精确含义：保证的是「缓冲区一致」，而非「元素不丢」。

**`PushIterFuture` 为何能取回剩余元素**——它的字段是 `iter: Option<Peekable<I>>`，并提供 `into_inner(self) -> Peekable<I>`（[async/src/traits/producer.rs:266-276](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L266-L276)）。已推入缓冲区的元素已经被 `push_iter` 消费（它们是「已生产」的合法数据，不算丢失），剩余尚未消费的仍在 `Peekable` 里，取消后可用 `into_inner()` 拿回继续用。

**`PushSliceFuture` / `PopSliceFuture` 的 `.count()`**——两者都暴露 `pub fn count(&self) -> usize`（[producer.rs:221-228](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L221-L228)、[consumer.rs:215-223](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs#L215-L223)）。取消后，调用方据此知道「已经拷贝/填充了多少」，从而决定如何续传。

**`FusedFuture` 的作用**——每个 Future 都实现了 `FusedFuture`，其 `is_terminated()` 反映「是否已最终完成、不该再 poll」。例如 `PushFuture` 在 `self.item.is_none()` 时终止（[async/src/traits/producer.rs:144-148](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L144-L148)），`PopFuture` 在 `self.done || is_closed()` 时终止（[async/src/traits/consumer.rs:137-141](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs#L137-L141)）。这让 `futures::select!` / `tokio::select!` 这类「分支之一完成后跳过其它分支」的组合子能正确识别已死的 Future，避免误 poll。配合 `poll` 里的 `assert!(!self.done)` / `assert!(!done)`（[producer.rs:298](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L298)、[consumer.rs:148](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs#L148)），构成「完成后禁止再 poll」的双重保险。

#### 4.4.4 代码实践

1. **实践目标**：用源码推理验证 `PushFuture` 的取消安全，并理解 `PushSliceFuture::count()`。
2. **操作步骤**：
   - 逐行阅读 [PushFuture::poll](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L149-L171)，列出 `self.item` 在「成功」「满待重试」「对端关闭」三条路径上的取值。
   - 设想：调用方 `let f = prod.push(x);` 后，在没有 `await` 完之前就 `drop(f)`。回答：此刻 `x` 在哪里？缓冲区里有没有 `x`？
3. **需要观察的现象（推理）**：由于取消发生在两次 `poll` 之间，`self.item` 此时为 `Some(x)`（若上一次 poll 没成功）或 `None`（若上一次已成功并返回 `Ready`，但那样 Future 已完成、通常不会再次被持有）。绝大多数取消场景下 `x` 仍在 `self.item`，随 drop 一起销毁。
4. **预期结论**：缓冲区里**没有** `x`（除非那次 poll 已成功）。所以「取消不会多 push 一个元素」成立；代价是 `x` 丢失。
5. 进阶：阅读 [PushSliceFuture](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L199-L220)，解释为何取消后用 `.count()` 能精确知道已拷贝数（答案：每轮 `self.count += len`，count 始终反映已成功拷贝量）。

#### 4.4.5 小练习与答案

**练习 1**：在 `tokio::select!` 里同时 `select!` 一个 `push(x)` 和一个超时分支，超时分支胜出导致 `push` 的 Future 被 drop。之后这个 `x` 去哪了？缓冲区状态如何？
**答案**：`x` 在 `PushFuture` 的 `self.item` 里，随 Future drop 而被销毁（丢失）。缓冲区里没有 `x`，读写索引与实际数据一致，状态完好——这就是取消安全的保证。若想避免丢失，应改用 `push_exact`（切片可保留原数据并查询 count）或 `push_iter_all`（剩余可 `into_inner` 取回）。

**练习 2**：`PopSliceFuture` 和 `PushSliceFuture` 都实现了 `FusedFuture`，`is_terminated` 都看 `self.slice.is_none()`。为什么 `slice` 在完成后会变成 `None`？
**答案**：它们的 `poll` 里用 `self.slice.take().unwrap()` 取出切片，处理后要么 `break Ready`（此时不再 `replace`，`slice` 保持 `None`），要么 `self.slice.replace(...)` 放回剩余切片继续。因此完成（`Ready`）后 `slice` 必为 `None`，`is_terminated` 据此返回真。

---

## 5. 综合实践

把本讲四条主线串成一个完整程序。目标：写一个**生产者中途 drop、消费者优雅收尾**的异步管道，并亲手解释取消安全。

参考 [async/examples/simple.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/examples/simple.rs) 的结构（用 `futures::executor::block_on` + `join!`，无需 tokio），完成下面这个示例程序（**示例代码**，非项目原有文件）：

```rust
// 示例代码：演示 push().await / pop().await 与对端关闭收尾
use async_ringbuf::{traits::*, AsyncHeapRb};
use futures::{executor::block_on, join};

async fn async_main() {
    let (mut prod, mut cons) = AsyncHeapRb::<i32>::new(4).split();

    // 生产者：只发 0..5 就提前结束（prod 走出作用域被 drop）
    let producer = async {
        for i in 0..5 {
            prod.push(i).await.unwrap(); // 满 4 个后会 await，等消费者取走
        }
        // prod 在此 drop → 复位 write_held → write.wake()
    };

    // 消费者：把所有元素收进 vec，直到 pop 返回 None
    let consumer = async {
        let mut got = Vec::new();
        while let Some(x) = cons.pop().await { // 生产者 drop 后最终返回 None
            got.push(x);
        }
        assert_eq!(got, (0..5).collect::<Vec<_>>());
    };

    join!(producer, consumer);
}

fn main() { block_on(async_main()); }
```

完成后请做三件事：

1. **跑通它**：`cargo run --example <你的示例名>`（需在 `async/examples/` 下并配置 `required-features = ["alloc"]`，参考 [async/Cargo.toml](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/Cargo.toml)）。验证 `got == [0,1,2,3,4]`，证明「生产者 drop → 消费者取完积压 → `pop` 返回 `None`」的链路（对应 4.1.2 的流程图）。
2. **解释收尾**：用一句话说明为何消费者 `pop().await` 最终返回 `None` 而不是永远挂起。（提示：`prod` drop → `hold_write(false)` → `AsyncRb::hold_write` 里的 `self.write.wake()` → `PopFuture` 重新 poll → `is_closed()` 真 → `None`。）
3. **解释取消安全**：假设把消费者那行改成 `cons.pop().await` 后立刻被外部 `select!` 取消（drop 掉这个 `PopFuture`）。阅读 [PushFuture::poll](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L149-L171) 与 [PopFuture::poll](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs#L145-L163)，回答：取消 `PopFuture` 会不会让某个元素「不翼而飞」？取消 `PushFuture` 会不会让某个元素被写两次？（答案：`pop` 完全安全、无元素丢失；`push` 不会写两次，但被取消的元素会随 Future 丢失。）

若本地无法运行 tokio/futures 执行器，请标注「待本地验证」，但第 2、3 步的源码推理必须给出结论。

## 6. 本讲小结

- `AsyncProducer` / `AsyncConsumer` 在核心 `Producer` / `Consumer` 之上**只新增 `register_waker` / `close` / `is_closed` 三个控制面方法**，数据面的 `try_*` 全靠委托（[u3-l5](u3-l5-delegation-based.md)）继承。
- 每个 async 方法返回一个**具名 Future**（`PushFuture` / `PopFuture` 等，按值、零分配），其 `poll` 统一采用「尝试 → 失败则登记 waker → 再查一次 → 仍不行才 `Pending`」的两轮循环，靠 [u6-l1](u6-l1-asyncrb-atomicwaker.md) 的 `AtomicWaker` 驱动唤醒、防丢唤醒。
- `register_waker` 登记到「你等待的索引」对应的 waker：生产者登记 `read`、消费者登记 `write`。
- **关闭收尾**：对端 drop / close 会复位其 hold 标志并 `wake()`；本端复查 `is_closed()`（`!read_is_held` / `!write_is_held`）后，`pop` 返回 `None`、`push` 返回 `Err(item)`。注意 `pop` 会先把积压元素取尽。
- **取消安全**靠「先 take 失败放回 / 完成置 done」的状态机 + 核心 `try_*` 的原子性：`pop` / `wait_*` 完全安全；`push_exact` / `pop_exact` 可能部分提交但可用 `.count()` 查询；`push_iter_all` 剩余可 `into_inner()` 取回；唯独 `push` 取消时会丢失元素（但绝不重复写入）。所有 Future 实现 `FusedFuture` 配合 `select!`。

## 7. 下一步学习建议

- 下一讲 [u6-l3](u6-l3-stream-sink-async-io.md) 把本讲的底层 `poll_next` / `poll_ready` / `poll_write` / `poll_read` 接入生态：`AsyncCons` 实现 `futures::Stream`、`AsyncProd` 实现 `futures::Sink`，并在 `std` feature 下实现 `AsyncRead` / `AsyncWrite`。学完后你就能把 ringbuf 当作异步字节流管道。
- 想对照「阻塞版」的同步策略，可读 [u7-l1](u7-l1-blockingrb-semaphore.md)、[u7-l2](u7-l2-blocking-api-timeout-io.md)，体会 `Semaphore`（信号量阻塞）与本讲 `AtomicWaker`（异步唤醒）解决同一类「等待」问题的不同范式。
- 若想亲手实现一个带 `select!` 取消的复杂场景，建议复习本讲 4.4 的对照表，并阅读 [async/src/traits/producer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs) 与 [async/src/traits/consumer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs) 中每个 `# Cancel safety` 文档段，作为编码时的查阅手册。
