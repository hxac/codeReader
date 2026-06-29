# AsyncRb 与 AtomicWaker：在无锁核心上加 async 唤醒

## 1. 本讲目标

本讲进入第一个派生 crate —— `async-ringbuf`。核心 crate `ringbuf` 只提供「立即成功或失败」的无锁 `try_*` 接口，没有任何「等待」语义（详见 u5-l1）。而 `async-ringbuf` 在不改动无锁核心的前提下，给 `SharedRb` 外面「包」了一层异步唤醒机制，让我们可以写出 `prod.push(item).await`、`cons.pop().await` 这样的协程式代码：满时让出执行权、有空位再被唤醒继续。

学完本讲你应该能够：

- 说出 `AsyncRb = SharedRb + 两个 AtomicWaker` 的结构，并解释为什么只加这两个字段就够了。
- 画出「谁注册 waker、谁唤醒 waker」的方向图，说出 `write` / `read` 两个 waker 各自的命名含义与触发点。
- 理解 `async-ringbuf` 为什么「不绑定任何运行时」（既不需要 tokio，也不需要 async-std）。

本讲聚焦 `AsyncRb` 这个**底层数据结构**本身（唤醒机制的安插点），至于 `AsyncProducer`/`AsyncConsumer` 那些 `Future` 的轮询与取消安全细节，留到下一讲（u6-l2）展开。

## 2. 前置知识

本讲建立在前几讲已建立的认知之上，这里只做最简回顾：

- **SPSC 与 hold 标志**（u5-l2）：环形缓冲区是「单生产者单消费者」结构，「至多一个写端、一个读端」由两个 hold 标志（`read_held` / `write_held`）在运行时强制。
- **无锁 = 非阻塞**（u5-l1）：`try_push` 满时返回 `Err(item)`、`try_pop` 空时返回 `None`，绝不会睡眠或自旋。等待语义被刻意剥离。
- **SharedRb 的索引推进**（u2-l1、u5-l1）：写数据后用 `set_write_index`（`Release` 原子 store）发布；读数据后用 `set_read_index` 发布。索引前进就是「提交点」。

本讲需要补充两个 async Rust 的基础概念：

- **`Future` 与 `poll`**：Rust 的异步是「协作式」的。一个 `Future` 不会自己跑，而是被一个执行器（executor）反复调用 `poll`。`poll` 要么返回 `Poll::Ready(结果)` 表示完成，要么返回 `Poll::Pending` 表示「还没好」。返回 `Pending` 时，`Future` 必须把当前任务的 `Waker` 存起来，等条件满足时调用 `waker.wake()` 把任务重新放回队列再次 `poll`。
- **「丢失唤醒」(lost wakeup) 问题**：如果先检查条件、再注册 `Waker`，那么在这两步之间若条件恰好被满足（对方的唤醒就发生在这个空隙里），唤醒就丢失了，任务会永远挂起。标准对策是「**先注册 Waker，再复查一次条件**」。本讲的代码正是按这个模式写的。

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `async/` 子 crate 内，除 `shared.rs` 属核心 crate）：

| 文件 | 作用 |
|------|------|
| `async/src/rb.rs` | 定义 `AsyncRb<S>` 结构，手动实现四个核心 trait，在索引变更点安插 `wake()`。**本讲主角。** |
| `async/src/alias.rs` | 定义 `AsyncHeapRb` / `AsyncStaticRb` 等类型别名与构造器，并按 feature 切换 `Arc` 来源。 |
| `async/src/wrap/mod.rs` | 定义 `AsyncWrap` 与别名 `AsyncProd`/`AsyncCons`，是「钥匙」`R`（`Arc<AsyncRb>` 或 `&AsyncRb`）的包装器。 |
| `async/src/wrap/prod.rs` | `AsyncProd` 实现 `register_waker`（注册到 `read` waker）。 |
| `async/src/wrap/cons.rs` | `AsyncCons` 实现 `register_waker`（注册到 `write` waker）。 |
| `async/src/lib.rs` | crate 入口，`#![no_std]`，仅依赖 `futures-util`。 |
| `async/Cargo.toml` | 依赖只有 `ringbuf`（workspace）与 `futures-util`，没有任何运行时 crate。 |
| `async/examples/simple.rs` | 最小可运行示例：`join!` 一个 push 循环与一个 pop 循环。 |
| `async/README.md` | 声明「不依赖任何 async runtime」的特性。 |
| `src/rb/shared.rs` | 核心 crate 的 `SharedRb`，`set_*_index` / `hold_*` 的原始实现，是 `AsyncRb` 的 `base`。 |

## 4. 核心概念与源码讲解

### 4.1 模块一：AsyncRb 的结构 —— SharedRb + 两个 AtomicWaker

#### 4.1.1 概念说明

`async-ringbuf` 的设计哲学是「**不重造轮子**」：无锁数据结构、原子顺序、内存安全这些难题，核心 crate `ringbuf` 已经解决得很完善了。异步层要做的，只是在「**对端会关心的状态发生变化**」时，去「戳一下」那个正在睡眠的对端任务，让它醒来重新尝试。

为此，`AsyncRb` 把一个现成的 `SharedRb` 原封不动地装进来当 `base`，再额外加 **两个** `futures_util::task::AtomicWaker` 字段。不多不少：一个用来唤醒读端、一个用来唤醒写端。

> **什么是 `AtomicWaker`？** 它是 `futures` 生态提供的一种同步原语，内部只存「**一个**」`Waker`。它提供两个核心方法：`register(&waker)`（存入一个等待者的 waker）和 `wake()`（唤醒当前存着的 waker）。关键在于它是「原子」的——`register` 和 `wake` 可以在两个线程/任务上并发安全地调用，无需加锁，且能正确处理「先 wake 后 register」「同时 wake 和 register」等竞态，不会丢唤醒。
>
> 为什么用「单 waker」原语就够了？因为这是 SPSC：**至多一个**消费者在等数据、**至多一个**生产者在等空位。不需要支持多个等待者，所以不需要更复杂的「多消费者 waker 集合」结构。

#### 4.1.2 核心流程

`AsyncRb` 的构造非常简单：

1. 拿到一个已经构造好的 `SharedRb<S>`（容量、存储后端都已就绪）。
2. 附加两个用 `AtomicWaker::default()` 创建的空 waker。
3. 包装成 `AsyncRb { base, read, write }`。

数据面（读写元素、查容量、查索引）的所有逻辑，`AsyncRb` 都**原样委托**给 `base`（`SharedRb`），自己一个字节都不改。`AsyncRb` 真正「做手脚」的，只有 `Producer::set_write_index`、`Consumer::set_read_index`、`RingBuffer::hold_read`、`RingBuffer::hold_write` 这四个「会改变对端可见状态」的方法——它在委托之后多打一个 `wake()`。这正是下一节要讲的唤醒方向。

整个层次关系可以画成：

```
AsyncProd<R>  ──(try_push 委托)──►  Direct<R>  ──(set_write_index)──►  AsyncRb<S>
                                     (即时同步)                           │ base 委托
                                                                         ▼
                                                                   SharedRb<S>  ──► 原子 store(Release)
                                                                   + write.wake() ← AsyncRb 多加的这一步
```

#### 4.1.3 源码精读

先看结构定义与构造器：

```rust
// async/src/rb.rs:22-36
pub struct AsyncRb<S: Storage> {
    base: SharedRb<S>,
    pub(crate) read: AtomicWaker,
    pub(crate) write: AtomicWaker,
}

impl<S: Storage> AsyncRb<S> {
    pub fn from(base: SharedRb<S>) -> Self {
        Self {
            base,
            read: AtomicWaker::default(),
            write: AtomicWaker::default(),
        }
    }
}
```

> [async/src/rb.rs:22-26](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/rb.rs#L22-L26)：`AsyncRb` 的全部字段。注意 `base` 就是核心 crate 的 `SharedRb`，两个 `AtomicWaker` 分别叫 `read` 和 `write`。

> [async/src/rb.rs:28-36](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/rb.rs#L28-L36)：`AsyncRb::from` 构造器。它不分配容量、不建存储，而是接收一个**已经就绪**的 `SharedRb`——所以创建工作仍由核心 crate 完成（如 `HeapRb::new`），异步层只负责「挂上唤醒」。

再看数据面如何「原样委托」给 base。以 `Observer` 的几个查询方法为例：

```rust
// async/src/rb.rs:40-72（节选）
impl<S: Storage> Observer for AsyncRb<S> {
    type Item = S::Item;
    #[inline] fn capacity(&self) -> NonZeroUsize { self.base.capacity() }
    #[inline] fn read_index(&self) -> usize { self.base.read_index() }
    #[inline] fn write_index(&self) -> usize { self.base.write_index() }
    // ...
}
```

> [async/src/rb.rs:40-72](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/rb.rs#L40-L72)：`AsyncRb` 对 `Observer` 的完整实现。注意这里**没有用** u3-l5 讲过的 `DelegateObserver` 委托机制，而是**逐个手写**。原因正是：`AsyncRb` 需要在某些方法里夹带 `wake()`，统一委托做不到「只改其中几个」。所有方法都标了 `#[inline]`，转发是零开销的。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `AsyncRb` 只是在 `SharedRb` 上「加两个 waker」，数据面行为与核心 crate 一致。

**操作步骤**：

1. 在本仓库根目录运行（注意要指定 `--package`，因为这是多 crate 工作区）：

   ```bash
   cargo run --package async-ringbuf --example simple
   ```

2. 阅读示例 [async/examples/simple.rs:1-29](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/examples/simple.rs#L1-L29)，注意它的关键四步：`AsyncHeapRb::<i32>::new(10)` 创建 → `.split()` 拆成 `(prod, cons)` → `prod.push(i).await` → `cons.pop().await`。

3. 把缓冲区容量从 `10` 改成 `3`，把循环次数从 `100` 改成 `5`，再运行一次。

**需要观察的现象**：程序正常输出（无 panic），消费端按 `0,1,2,3,4` 顺序收到每个元素，最后 `cons.pop().await` 返回 `None`（因为生产端已 drop）。

**预期结果**：`push(i).await.unwrap()` 永远成功、`pop().await` 永远返回预期的 `Some(i)`。容量改成 3 也不会改变正确性——这说明即使缓冲区很小、push 频繁撞满，await/唤醒机制也能让生产者正确地「等一等」消费者。本步骤运行结果：待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`AsyncRb` 为什么不在 `try_push` / `try_pop` 这种高层方法里直接放 `wake()`，而要在 `set_*_index` 这种底层 unsafe 方法里放？

**参考答案**：因为「唤醒对端」的准确触发点应该是「**对端可见的状态真的变了**」的那一刻，也就是索引被推进的瞬间。高层方法（`try_push` 等）在「满/空」时根本不会推进索引，也就不应该唤醒；只有真正成功写入并 `set_write_index` 之后，数据才对消费端可见，这时唤醒才有意义。放在最底层原语里，能保证**无论哪条调用路径**（单条 `try_push`、批量 `push_slice`、`push_iter`，甚至直接 `vacant_slices_mut` + `advance_write_index`），只要索引前进了，唤醒就一定发生，不会遗漏。

**练习 2**：如果 `AsyncRb` 完全不实现 `Observer`/`Producer` 等接口、只是把 `SharedRb` 当一个普通字段，会发生什么？

**参考答案**：那样包装器 `AsyncProd`/`AsyncCons` 就无法通过统一的 trait 接口访问它（u4 讲的 `Direct<R>` 要求 `R::Rb` 实现这些 trait），整个泛型链会断掉。`AsyncRb` 实现这些 trait，本质上是「**冒充**一个 `SharedRb`」——同样的接口、几乎同样的行为——好让上层所有「针对 `SharedRb` 写的泛型代码」无需改动就能用上它，同时悄悄在索引变更处注入唤醒。

---

### 4.2 模块二：唤醒的方向 —— write / read 两个 waker 与四个触发点

#### 4.2.1 概念说明

这是本讲最关键、也最容易绕晕的部分。`AsyncRb` 有两个 waker，分别叫 `write` 和 `read`。理解它们的关键在于搞清**命名规则**：每个 waker 以「**它信号化的那个索引**」命名——

- **`write` waker**：当 **write 索引前进**（有新数据被写入）时被唤醒。谁会等「有新数据」？**消费者**（读端空了在等）。所以消费者注册到 `write` waker。
- **`read` waker**：当 **read 索引前进**（有元素被取走、腾出空位）时被唤醒。谁会等「有空位」？**生产者**（写端满了在等）。所以生产者注册到 `read` waker。

一句话记忆：「**名字 = 你等的那个索引**，注册方 = 等它的那个对端」。

#### 4.2.2 核心流程

把方向关系整理成一张表（这是本讲的「地图」，建议记牢）：

| 等待方（注册 waker） | 阻塞原因 | 它在等的索引变化 | 注册到哪个 waker | 触发该 waker 的操作 |
|---|---|---|---|---|
| **生产者** `AsyncProd` | 缓冲区满，等空位 | read 索引前进（消费者取走） | `read` | `set_read_index`、`hold_read` |
| **消费者** `AsyncCons` | 缓冲区空，等数据 | write 索引前进（生产者写入） | `write` | `set_write_index`、`hold_write` |

对应的唤醒触发点，在 `AsyncRb` 的源码里一共只有 **4 个**，正好覆盖「改变对端可见状态」的全部操作：

1. `Producer::set_write_index` → 委托 base + `self.write.wake()`（写入了新数据 → 唤醒消费者）
2. `Consumer::set_read_index` → 委托 base + `self.read.wake()`（腾出了空位 → 唤醒生产者）
3. `RingBuffer::hold_read` → 委托 base + `self.read.wake()`（消费端创建/销毁 → 唤醒生产者，让它复查 `is_closed`）
4. `RingBuffer::hold_write` → 委托 base + `self.write.wake()`（生产端创建/销毁 → 唤醒消费者，让它复查 `is_closed`）

第 3、4 点尤其精妙：`is_closed()` 是靠 hold 标志判断的（生产者的 `is_closed = !read_is_held`，即「消费者还在不在」）。当一个端点 drop 时，它的 `close` 会把对应 hold 标志翻成 `false`，而这一步走的是 `hold_*`，因此**会顺便唤醒对端的 waker**——让正在 `await` 的对端醒来、发现 `is_closed()` 已变真、于是返回 `Err`/`None` 正常收尾。**关闭与数据抵达共用同一套唤醒通道**，无需为「关闭」另设机制。

#### 4.2.3 源码精读

先看 `AsyncRb` 在四个触发点如何注入 `wake()`：

```rust
// async/src/rb.rs:74-99
impl<S: Storage> Producer for AsyncRb<S> {
    unsafe fn set_write_index(&self, value: usize) {
        unsafe { self.base.set_write_index(value) };   // 先真正推进索引（Release store）
        self.write.wake();                              // 再唤醒等在 write 上的消费者
    }
}
impl<S: Storage> Consumer for AsyncRb<S> {
    unsafe fn set_read_index(&self, value: usize) {
        unsafe { self.base.set_read_index(value) };
        self.read.wake();                               // 唤醒等在 read 上的生产者
    }
}
impl<S: Storage> RingBuffer for AsyncRb<S> {
    #[inline]
    unsafe fn hold_read(&self, flag: bool) -> bool {
        let old = unsafe { self.base.hold_read(flag) };
        self.read.wake();
        old
    }
    #[inline]
    unsafe fn hold_write(&self, flag: bool) -> bool {
        let old = unsafe { self.base.hold_write(flag) };
        self.write.wake();
        old
    }
}
```

> [async/src/rb.rs:74-79](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/rb.rs#L74-L79)：写端触发点。先 `self.base.set_write_index` 完成核心的 `Release` 原子 store（见 [src/rb/shared.rs:125-127](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L125-L127)），再 `self.write.wake()`。

> [async/src/rb.rs:80-85](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/rb.rs#L80-L85)：读端触发点。对称地 `self.read.wake()`。

> [async/src/rb.rs:86-99](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/rb.rs#L86-L99)：两个 hold 触发点。hold 标志改变（端点创建/销毁）时也唤醒，让对端能复查 `is_closed()`。底层 base 的实现见 [src/rb/shared.rs:139-145](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L139-L145)（`swap(flag, AcqRel)`）。

再看「谁注册到哪个 waker」。生产者和消费者各自只实现一行 `register_waker`：

```rust
// async/src/wrap/prod.rs:23-32
impl<R: AsyncRbRef> AsyncProducer for AsyncProd<R> {
    fn register_waker(&self, waker: &core::task::Waker) {
        self.rb().read.register(waker)   // 生产者等空位 → 注册到 read waker
    }
    fn close(&mut self) { drop(self.base.take()); }
}
```

```rust
// async/src/wrap/cons.rs:21-30
impl<R: AsyncRbRef> AsyncConsumer for AsyncCons<R> {
    fn register_waker(&self, waker: &core::task::Waker) {
        self.rb().write.register(waker)  // 消费者等数据 → 注册到 write waker
    }
    fn close(&mut self) { drop(self.base.take()); }
}
```

> [async/src/wrap/prod.rs:23-32](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/prod.rs#L23-L32)：生产者 `register_waker` 注册到 `self.rb().read`。

> [async/src/wrap/cons.rs:21-30](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/cons.rs#L21-L30)：消费者 `register_waker` 注册到 `self.rb().write`。

注册之后的「先注册、再复查」循环，以 `PushFuture::poll`（生产者 push 的 Future）为例：

```rust
// async/src/traits/producer.rs:152-171（节选）
fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
    let mut waker_registered = false;
    loop {
        let item = self.item.take().unwrap();
        if self.owner.is_closed() { break Poll::Ready(Err(item)); }      // 消费者没了？
        let push_result = self.owner.try_push(item);                     // 再试一次
        if push_result.is_ok() { break Poll::Ready(Ok(())); }
        self.item.replace(push_result.unwrap_err());
        if waker_registered { break Poll::Pending; }                     // 第二轮仍满 → 挂起
        self.owner.register_waker(cx.waker());                           // 先注册
        waker_registered = true;                                         // 再循环回去复查
    }
}
```

> [async/src/traits/producer.rs:152-171](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L152-L171)：`PushFuture::poll`。注意循环结构：第一次满时 `register_waker` 后**继续 loop**，于是 `try_push` 会被再执行一次——这一步就是「复查」，它消除了「注册与唤醒之间的空隙」里的丢唤醒风险。如果复查仍满，才返回 `Pending`，安心等被 `read.wake()` 唤醒。

#### 4.2.4 代码实践

**实践目标**：用一段「制造饱和」的程序，亲眼看到「push 在满时会 await、被消费者取走后唤醒继续」。

**操作步骤**：

1. 在 `async/examples/` 下新建一个临时示例（仅供本讲观察，**不要提交**），例如 `examples/saturate.rs`（示例代码）：

   ```rust
   // 示例代码：观察 push 在满时 await
   use async_ringbuf::{traits::*, AsyncHeapRb};
   use futures::{executor::block_on, join};

   async fn async_main() {
       let rb = AsyncHeapRb::<i32>::new(2);          // 故意用极小容量
       let (mut prod, mut cons) = rb.split();

       join!(
           async move {
               for i in 0..5 {
                   println!("push {i}");
                   prod.push(i).await.unwrap();      // 容量 2，第 3 个起会 await
               }
               drop(prod);                            // 显式关闭写端
           },
           async move {
               for _ in 0..5 {
                   if let Some(v) = cons.pop().await { println!("  pop {v}"); }
               }
           },
       );
   }

   fn main() { block_on(async_main()); }
   ```

2. 在 `[package]` 的 `[[example]]` 表里加一项（或直接用 `cargo run --example saturate`，若它因缺 manifest 报错，则参考 [async/Cargo.toml:37-39](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/Cargo.toml#L37-L39) 的 `required-features = ["alloc"]` 加一个 example 条目）。

3. 运行并观察 `push` / `pop` 的交错顺序。

**需要观察的现象**：你会看到 push 与 pop **交替**出现，而不是「5 个 push 全打完再 5 个 pop」。因为容量只有 2，生产者 push 到第 3 个时缓冲区已满，`push(2).await` 会挂起，直到消费者 `pop` 出一个、`set_read_index` 触发 `read.wake()` 把生产者唤醒。

**预期结果**：所有 5 个元素都被正确收发，顺序为 `0..5`，无丢失无重复。这证明「满时 await → 被唤醒续传」链路工作正常。本步骤运行结果：待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：假设消费者 `cons.pop().await` 正在等待（已注册到 `write` waker 并返回 `Pending`）。此时生产者 drop 了。请追踪从「生产者 drop」到「消费者 `pop` 返回 `None`」的完整唤醒链路。

**参考答案**：生产者 drop → `AsyncProd` 的 `Drop`（经由 `into_rb_ref` / `close`）→ `drop(self.base.take())` 销毁内部的 `Direct` → `Direct` 的 `close` 调用 `hold_write(false)` 复位写端 hold 标志 → 因为这是 `AsyncRb`，`hold_write` 走的是 [async/src/rb.rs:94-98](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/rb.rs#L94-L98) 的覆写版本 → `self.write.wake()` 唤醒消费者 → 消费者 `PopFuture::poll` 被再次调度 → 复查时 `is_closed()`（即 `!write_is_held()`）已为真 → 返回 `None`。整条链路**复用了 hold 触发点的唤醒通道**，没有为「关闭」单独发明信号。

**练习 2**：为什么唤醒只用「单 waker」的 `AtomicWaker`，而不是能容纳多个等待者的结构（如 `task::WakerSet`）？

**参考答案**：因为 SPSC 不变量（u5-l2）保证「至多一个生产者、至多一个消费者」。等待空位的至多只有那一个生产者、等待数据的至多只有那一个消费者，所以每个方向上至多只有一个等待者，单 waker 完全够用且开销最小。如果允许多等待者，就必须用集合类结构——但那会破坏 SPSC 模型，超出本库定位。

---

### 4.3 模块三：运行时无关的设计与 AsyncHeapRb / AsyncStaticRb 别名

#### 4.3.1 概念说明

一个常见的疑问：异步不就得有 tokio / async-std 这种运行时吗？`async-ringbuf` 是怎么做到「运行时无关」的？

答案是：它**只依赖**抽象的「`Future` + `poll` + `Waker`」协议（由 `core::future`、`core::task` 提供，属于 `no_std` 可用的语言核心），外加 `futures-util` 这个「运行时中立」的工具库。它从不 `spawn` 任务、不创建线程、不调用任何运行时专属 API。因此：

- 你可以用 `tokio` 跑它（在 `#[tokio::main]` 里 `prod.push(x).await`）。
- 也可以用 `async-std`、`smol`、`futures::executor::block_on` 跑它。
- 甚至可以完全 `no_std`（在嵌入式里手写一个最简执行器驱动这些 Future）。

README 把这一点列为特性之一。

#### 4.3.2 核心流程

「运行时无关」的实现要点有三：

1. **依赖极简**：`async/Cargo.toml` 的 `[dependencies]` 只有 `ringbuf`（workspace）和 `futures-util`，**没有任何** `tokio`/`async-std`/`smol`。`tokio` 只出现在 `[dev-dependencies]`（仅供测试，[async/Cargo.toml:33-35](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/Cargo.toml#L33-L35)）。
2. **协议中立**：等待语义完全由 `AsyncRb` 提供的「register/wake」+ 标准 `Future::poll` 表达，没有运行时专属的「通知器」「定时器」类型。
3. **`#![no_std]`**：crate 入口声明 `#![no_std]`，`std` 是可选 feature（仅用于开启 `AsyncRead`/`AsyncWrite` 字节流集成）。

在此基础上，`alias.rs` 给出与核心 crate 对齐的常用别名，让我们像用 `HeapRb` 一样用 `AsyncHeapRb`：

- `AsyncHeapRb<T> = AsyncRb<Heap<T>>`（堆分配，需 `alloc`）
- `AsyncStaticRb<T, N> = AsyncRb<Array<T, N>>`（静态数组，`no_std` 可用）
- 拆分后的两端：`AsyncHeapProd/Cons`（基于 `Arc`，可跨线程/跨任务）、`AsyncStaticProd/Cons`（基于 `&'a`，带生命周期、不跨线程）——与核心 crate 的 `HeapProd`/`StaticProd` 完全对应。

#### 4.3.3 源码精读

先看 crate 入口与依赖清单，确认「运行时无关」：

```rust
// async/src/lib.rs:1-19（节选）
#![no_std]
// ...
mod alias;
pub mod rb;
pub mod traits;
pub mod wrap;

pub use alias::*;
pub use rb::AsyncRb;
```

> [async/src/lib.rs:1-19](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/lib.rs#L1-L19)：`#![no_std]` 入口，导出 `AsyncRb` 与别名，无运行时依赖。

```toml
# async/Cargo.toml:25-31（节选）
[dependencies]
ringbuf = { workspace = true }
futures-util = { version = "0.3.31", default-features = false, features = ["sink"] }
# 没有 tokio / async-std / smol
```

> [async/Cargo.toml:25-31](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/Cargo.toml#L25-L31)：仅依赖 `ringbuf` 与 `futures-util`，二者都是运行时中立的。`tokio` 仅在 `[dev-dependencies]` 出现。

再看别名与构造器：

```rust
// async/src/alias.rs:14-36（节选）
#[cfg(feature = "alloc")]
pub type AsyncHeapRb<T> = AsyncRb<Heap<T>>;
#[cfg(feature = "alloc")]
pub type AsyncHeapProd<T> = AsyncProd<Arc<AsyncHeapRb<T>>>;
#[cfg(feature = "alloc")]
pub type AsyncHeapCons<T> = AsyncCons<Arc<AsyncHeapRb<T>>>;

#[cfg(feature = "alloc")]
impl<T> AsyncHeapRb<T> {
    pub fn new(cap: usize) -> Self {
        Self::from(HeapRb::new(cap))   // 复用核心 crate 的构造器
    }
}

pub type AsyncStaticRb<T, const N: usize> = AsyncRb<Array<T, N>>;

impl<T, const N: usize> Default for AsyncRb<Array<T, N>> {
    fn default() -> Self { AsyncRb::from(SharedRb::default()) }
}
```

> [async/src/alias.rs:14-19](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/alias.rs#L14-L19)：`AsyncHeapRb` 及其拆分端别名。注意拆分端用的是 `Arc<AsyncHeapRb<T>>`（拥有、可跨任务），与核心 crate 的 `HeapProd` 基于 `Arc` 一致（u2-l3）。

> [async/src/alias.rs:21-26](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/alias.rs#L21-L26)：`AsyncHeapRb::new` 直接转调核心 crate 的 `HeapRb::new(cap)`，再用 `AsyncRb::from` 包一层。容量、堆分配逻辑完全不重复。

> [async/src/alias.rs:28-36](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/alias.rs#L28-L36)：`AsyncStaticRb`（`no_std` 友好的静态版本）与其 `Default` 实现。静态版无需 `alloc`，因此没有 `new`，改用 `Default`。

还有一处值得注意——`Arc` 的来源会随 feature 切换：

```rust
// async/src/alias.rs:9-12
#[cfg(all(feature = "alloc", not(feature = "portable-atomic")))]
pub use alloc::sync::Arc;
#[cfg(all(feature = "alloc", feature = "portable-atomic"))]
pub use portable_atomic_util::Arc;
```

> [async/src/alias.rs:9-12](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/alias.rs#L9-L12)：开启 `portable-atomic` 时，`Arc` 改用 `portable_atomic_util::Arc`，让 async 版本也能跑在没有 64 位原子的小芯片上（与核心 crate 的 u8-l1 处理一致）。

最后看 README 的特性声明与示例：

> [async/README.md:23-31](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/README.md#L23-L31)：Features 列表，明确写有 `Does not depends on any async runtime (like tokio and async-std)`。

> [async/examples/simple.rs:1-29](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/examples/simple.rs#L1-L29)：用 `futures::executor::block_on` + `join!` 跑，而非任何运行时宏，印证运行时无关。

#### 4.3.4 代码实践

**实践目标**：亲手确认「同一份 async 代码可在不同执行器下运行」。

**操作步骤**：

1. 默认情况下跑（用 `futures` 的执行器）：

   ```bash
   cargo run --package async-ringbuf --example simple
   ```

2. （进阶，待本地验证）把示例里的执行换成 tokio：在 `async/examples/` 下仿写一个 `simple_tokio.rs`，把 `fn main() { block_on(async_main()) }` 换成：

   ```rust
   #[tokio::main]
   async fn main() { async_main().await; }
   ```

   （需在 `async/Cargo.toml` 的 `[dev-dependencies]` 里已有 `tokio`，见 [async/Cargo.toml:33-35](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/Cargo.toml#L33-L35)。）

3. 运行 `cargo run --package async-ringbuf --example simple_tokio`。

**需要观察的现象**：两份示例逻辑完全相同（都是 `join!` push 循环与 pop 循环），只是「谁来调度 Future」不同（一个是 `futures::executor`，一个是 tokio 运行时）。

**预期结果**：两者输出一致、行为一致。这证明 `AsyncRb` 与 `AsyncProd`/`AsyncCons` 不绑定任何运行时——只要执行器遵守 `Future` + `Waker` 协议，就能驱动它们。本步骤运行结果：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`AsyncHeapRb::new` 和 `AsyncStaticRb` 的构造方式为何不同？

**参考答案**：`AsyncHeapRb` 有 `new(cap)`，因为堆分配需要运行期指定容量；`AsyncStaticRb` 的容量是编译期常量 `N`（写在类型 `Array<T, N>` 里），无需传参，所以用 `Default::default()` 构造（[async/src/alias.rs:32-36](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/alias.rs#L32-L36)）。这与核心 crate 的 `StaticRb` 用法一致（u2-l2、u2-l3）。

**练习 2**：为什么 `AsyncHeapProd` 用 `Arc`，而 `AsyncStaticProd` 用 `&'a`？

**参考答案**：因为两者的「拆分方式」不同（u2-l4）。`split()`（按值消费所有权，需堆）会把缓冲区搬进 `Arc`，得到可跨任务移动的拥有型句柄；`split_ref()`（借用 `&mut self`，零分配）得到带生命周期的引用型句柄。堆版用 `Arc` 对应核心 crate 的 `HeapProd`，静态版用 `&'a` 对应 `StaticProd`。

---

## 5. 综合实践

把本讲三个模块串起来的小任务：**画一张「async push/pop 在满/空时的时序图」，并验证它**。

**任务**：

1. 取一个容量为 2 的 `AsyncHeapRb<i32>`，split 出 `prod` 与 `cons`。
2. 在 `join!` 中，生产者连续 `push(0).await`、`push(1).await`、`push(2).await`、`push(3).await`；消费者先「睡一下」（例如在一个计时/`yield` 后）再开始 `pop`。
3. 在 push 与 pop 前后各打一行日志，标注当前 `prod.vacant_len()` / `cons.occupied_len()`（这两个是 `Observer` 的方法，`AsyncProd`/`AsyncCons` 通过委托继承得到）。

**需要观察并解释的现象**：

- `push(0)`、`push(1)` 立即成功（vacant 从 2→0）。
- `push(2).await` 在满时**挂起**——此时消费者还没开始取，所以这条会 await。
- 消费者开始 `pop()`，`set_read_index` 触发 `read.wake()` → 唤醒生产者 → `push(2)` 完成。后续交替进行。
- 全程没有任何「忙等」或「轮询」，全是「阻塞→唤醒」。

**画图要求**：用本讲 4.2 的方向表，标注每一次 `await` 挂起时「注册到哪个 waker」、每一次唤醒「由哪个触发点（`set_*_index` / `hold_*`）发出」。这张图能帮你把「`AsyncRb` 结构 → 唤醒方向 → 运行时无关的 poll 循环」三者彻底打通。

> 说明：本任务需要自行编写示例并配执行器，运行结果待本地验证；但「时序与唤醒方向」可通过阅读源码（4.2.3 引用的几段）严格推理得出，无需运行即可确认逻辑正确。

## 6. 本讲小结

- `AsyncRb<S>` 的结构是 `SharedRb<S>`（`base`）+ 两个 `AtomicWaker`（`read`、`write`），数据面逻辑全部委托给 `base`，异步层只负责「状态变更时唤醒对端」。
- 唤醒方向遵循「**waker 名 = 你等的那个索引**」：生产者等空位 → 注册 `read` waker（`set_read_index`/`hold_read` 触发）；消费者等数据 → 注册 `write` waker（`set_write_index`/`hold_write` 触发）。
- 一共只有 **4 个**唤醒注入点：`set_write_index`、`set_read_index`、`hold_read`、`hold_write`，覆盖「改变对端可见状态」的全部操作。
- 「关闭」复用同一套唤醒通道：端点 drop 时翻 hold 标志会唤醒对端，让 `is_closed()` 生效，无需为关闭另设机制。
- 防丢唤醒靠 `PushFuture`/`PopFuture` 等的「**先 register_waker，再 loop 复查**」循环结构。
- `async-ringbuf` 运行时无关：只依赖 `Future`+`poll`+`Waker` 协议与 `futures-util`，`tokio`/`async-std` 仅作可选 dev-dependency；`AsyncHeapRb`（`alloc`）与 `AsyncStaticRb`（`no_std`）提供与核心 crate 对齐的别名。

## 7. 下一步学习建议

本讲只讲了 `AsyncRb` 这个**底层结构**（唤醒机制装在哪、谁唤醒谁）。下一讲 **u6-l2（AsyncProducer/AsyncConsumer：Future、等待与取消安全）** 将系统展开上层 `Future` 家族（`PushFuture`/`PopFuture`/`WaitVacantFuture`/`push_exact`/`pop_until_end` 等），重点是它们的返回语义、`is_closed` 收尾，以及每种 Future 的**取消安全（cancel safety）**保证——即 `await` 被 drop 时会不会丢数据。之后再进入 **u6-l3**，把 `AsyncCons` 当 `Stream`、`AsyncProd` 当 `Sink`，并在 `std` feature 下接入 `AsyncRead`/`AsyncWrite`。

建议同步阅读的源码：

- [async/src/traits/producer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs) 与 [async/src/traits/consumer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs)：通读所有 `Future` 的 `poll` 循环，体会「先注册再复查」模式。
- [async/src/wrap/mod.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/mod.rs)：`AsyncWrap` 如何用 `DelegateObserver` + `Based` 复用核心包装器 `Direct` 的能力（承接 u3-l5、u4）。
