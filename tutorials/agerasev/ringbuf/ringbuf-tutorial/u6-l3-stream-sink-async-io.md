# Stream / Sink / AsyncRead / AsyncWrite 集成

## 1. 本讲目标

本讲承接 [u6-l2](u6-l2-async-producer-consumer-futures.md)，回答一个问题：**async-ringbuf 的两端能不能像「流」一样被生态里现成的工具消费？**

答案是能。`AsyncCons` 实现了 `futures::Stream`，`AsyncProd` 实现了 `futures::Sink`；当开启 `std` feature 时，`AsyncCons<Item=u8>` 还实现 `AsyncRead`、`AsyncProd<Item=u8>` 实现 `AsyncWrite`。于是环形缓冲区的两端可以直接插进 futures 生态的管道（`while let Some(x) = cons.next().await`、`prod.send(x).await`、`cons.read(&mut buf)`），无需任何适配代码。

学完本讲你应当能够：

- 说清 `Stream` / `Sink` / `AsyncRead` / `AsyncWrite` 四个 trait 分别由哪一端实现、各自把哪个底层方法对接出去。
- 理解为什么 `Stream` / `Sink` 是**无条件**实现的（no_std 可用），而 `AsyncRead` / `AsyncWrite` 必须开启 `std`。
- 能手动解读 `poll_next` / `poll_ready` 等「手动 poll」接口，看出它们复用了 [u6-l2](u6-l2-async-producer-consumer-futures.md) 里的「尝试 → 失败登记 waker → 复查」循环。
- 会用 `StreamExt`、`SinkExt`、`AsyncReadExt`、`AsyncWriteExt` 把缓冲区接到真实程序里。

## 2. 前置知识

在进入源码前，先建立四个 trait 的直觉。它们都来自 `futures` 生态，本质是把「异步的生产 / 消费」抽象成统一接口：

| Trait | 角色 | 关键方法 | 在 async-ringbuf 中 |
| --- | --- | --- | --- |
| `Stream` | 异步迭代器，产出一系列值 | `poll_next → Poll<Option<Item>>` | `AsyncCons` 实现 |
| `Sink<Item>` | 异步接收器，吞入一系列值 | `poll_ready` + `start_send` + `poll_close` | `AsyncProd` 实现 |
| `AsyncRead` | 异步字节读（`Item = u8`） | `poll_read(buf) → Poll<io::Result<usize>>` | `AsyncCons<Item=u8>` 实现（需 `std`） |
| `AsyncWrite` | 异步字节写（`Item = u8`） | `poll_write(buf)` + `poll_close` | `AsyncProd<Item=u8>` 实现（需 `std`） |

几条需要先记住的事实（后续源码会逐一印证）：

- `Stream` 和 `Sink` 是「元素级」抽象，可以承载任意 `Item` 类型（不只是字节）；`AsyncRead` / `AsyncWrite` 是「字节级」抽象，只有当 `Item = u8` 时才有意义。
- 这四种实现**没有发明新机制**，全部复用 [u6-l2](u6-l2-async-producer-consumer-futures.md) 已经建好的 `try_pop` / `try_push` / `register_waker` / `is_closed`。它们只是把这些能力「翻译」成对应 trait 的方法签名。
- 「关闭」语义直接复用 [u6-l1](u6-l1-asyncrb-atomicwaker.md) 讲过的 hold 标志通道：一端 drop 会复位 hold 标志并 `wake()` 对端，对端复查 `is_closed()` 即可收尾。

如果你对 `Future` / `Poll` / `Pin` / `Context` / `Waker` 这套异步原语还不熟，可以把本讲里每一个 `poll_*` 方法都理解成「运行时会反复调用它，直到它返回 `Ready`」。

## 3. 本讲源码地图

本讲只动 `async-ringbuf` 这个派生 crate，涉及三个核心文件：

| 文件 | 作用 |
| --- | --- |
| [async/src/wrap/cons.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/cons.rs) | 为 `AsyncCons` 实现 `Stream`、`AsyncRead`，并实现 `AsyncConsumer`（`register_waker` / `close`）。 |
| [async/src/wrap/prod.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/prod.rs) | 为 `AsyncProd` 实现 `Sink`、`AsyncWrite`，并实现 `AsyncProducer`（`register_waker` / `close`）。 |
| [async/src/traits/consumer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs) | 定义 `AsyncConsumer` trait，提供 `poll_next` / `poll_read` 的**默认实现**，是 `Stream` / `AsyncRead` 实现的算法来源。 |

辅助文件：

| 文件 | 作用 |
| --- | --- |
| [async/src/traits/producer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs) | 定义 `AsyncProducer` trait，提供 `poll_ready` / `poll_write` 的默认实现，是 `Sink` / `AsyncWrite` 实现的算法来源。 |
| [async/src/traits/mod.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/mod.rs) | 把 `AsyncConsumer` / `AsyncProducer` 与核心 `ringbuf::traits::*` 一起 re-export，使 `use async_ringbuf::traits::*` 一行拿全。 |
| [async/Cargo.toml](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/Cargo.toml) | feature 配置：`futures-util` 的 `sink` 恒开，`io` 仅在 `std` 下引入——这是四种实现可用性差异的根因。 |

## 4. 核心概念与源码讲解

### 4.1 AsyncCons 作为 Stream：把缓冲区读端变成异步迭代器

#### 4.1.1 概念说明

`Stream` 是 futures 生态里的「异步迭代器」：每次 `poll_next` 要么返回 `Ready(Some(item))` 给出一个元素，要么返回 `Ready(None)` 表示流结束，要么返回 `Pending` 表示「现在没数据，请稍后再来」。

把 `AsyncCons` 实现成 `Stream` 后，消费端就可以用最自然的写法遍历整个缓冲区：

```rust
while let Some(item) = cons.next().await {
    println!("got {item}");
}
```

这里有两个语义要点：

- **有元素就给元素**：底层调用 `try_pop`（来自 [u3-l3](u3-l3-consumer-trait.md) 的 `Consumer` trait）。
- **流何时结束**：当缓冲区为空**且**对端生产者已经关闭（被 drop）时，`poll_next` 返回 `Ready(None)`。这正对应 [u6-l2](u6-l2-async-producer-consumer-futures.md) 讲过的 `is_closed()`。

#### 4.1.2 核心流程

`poll_next` 的算法与 [u6-l2](u6-l2-async-producer-consumer-futures.md) 里 `PopFuture::poll` 几乎一模一样，是一套**带防丢唤醒的两轮循环**：

```
loop {
    1. 读 closed = is_closed()            // 取一次关闭快照
    2. 若 try_pop() 成功 → Ready(Some(item))   // 快路径：有数据立即返回
    3. 若 closed      → Ready(None)            // 空且对端关闭 → 流结束
    4. 若已登记过 waker → Pending              // 真的暂时没数据，挂起
    5. 否则 register_waker(waker); 标记已登记; 继续 loop   // 先登记再复查
}
```

第 5 步的「先登记 waker，再回到 loop 复查」是防止唤醒丢失的关键：如果在 `try_pop` 与 `register_waker` 之间生产者恰好 `wake()`，因为还没登记所以那次唤醒会丢；但登记后立刻再查一轮，就能补救。这套模式在 [u6-l2](u6-l2-async-producer-consumer-futures.md) 已详细分析，本讲不再重复。

#### 4.1.3 源码精读

`Stream` 的实现就在 `AsyncCons` 上，[async/src/wrap/cons.rs:32-52](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/cons.rs#L32-L52) 把上面流程原样写出：

```rust
impl<R: AsyncRbRef> Stream for AsyncCons<R> {
    type Item = <R::Rb as Observer>::Item;

    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let mut waker_registered = false;
        loop {
            let closed = self.is_closed();
            if let Some(item) = self.try_pop() {
                break Poll::Ready(Some(item));
            }
            if closed {
                break Poll::Ready(None);
            }
            if waker_registered {
                break Poll::Pending;
            }
            self.register_waker(cx.waker());
            waker_registered = true;
        }
    }
}
```

读这段要抓三个点：

1. `type Item = <R::Rb as Observer>::Item` —— 流产出的元素类型直接取自底层缓冲区（见 [u3-l1](u3-l1-observer-trait.md) 的 `Observer::Item`），所以 `AsyncHeapRb<i32>` 拆出的 `AsyncCons` 是一个 `Stream<Item = i32>`。
2. `try_pop` / `is_closed` / `register_waker` 全部来自 `AsyncConsumer` trait——`AsyncCons` 通过委托（见 [u3-l5](u3-l5-delegation-based.md)）拿到了 `try_pop`，又在本文件实现了 `AsyncConsumer` 的 `register_waker` / `close`，见 [async/src/wrap/cons.rs:21-30](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/cons.rs#L21-L30)。
3. 消费端登记的是**写 waker**：`register_waker` 里调 `self.rb().write.register(waker)`（[async/src/wrap/cons.rs:23](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/cons.rs#L23)）——因为消费者等的是「有新数据」，而新数据由生产者推进 `write_index` 时唤醒，这正符合 [u6-l1](u6-l1-asyncrb-atomicwaker.md) 的「waker 名 = 你等的索引」约定。

> 小细节：`AsyncConsumer` trait 本身也提供了 `poll_next` 的**默认实现**（[async/src/traits/consumer.rs:86-105](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs#L86-L105)），逻辑与上面完全相同。为什么 `Stream` 还要再写一遍？因为 trait 默认方法要求 `Self: Unpin`，而 `Stream` 的 impl 想在不强加 `Unpin` 约束（虽然 `AsyncWrap` 实际是 `Unpin`）的前提下直接对接 `Stream::poll_next` 的签名。两份代码是「同一算法、两个入口」。

#### 4.1.4 代码实践

**实践目标**：把 `AsyncCons` 当作 `Stream`，用 `StreamExt::next` 消费整个缓冲区，观察流在生产者 drop 后自然结束。

> 这是**示例代码**（不在仓库 examples 中），读者需自行创建。它复用了 `async/examples/simple.rs` 的 `block_on` + `join!` 套路。

```rust
// 示例代码
use async_ringbuf::{traits::*, AsyncHeapRb};
use futures::{executor::block_on, join, stream::StreamExt};

async fn async_main() {
    let rb = AsyncHeapRb::<i32>::new(8);
    let (prod, cons) = rb.split();

    join!(
        async move {
            let mut prod = prod;
            for i in 0..5 {
                prod.push(i).await.unwrap(); // 生产者写入 5 个
            }
            // prod 在块结束时被 drop → 触发 close → 唤醒消费者
        },
        async move {
            let mut cons = cons;
            // 把 AsyncCons 当作 Stream 逐个消费
            while let Some(item) = cons.next().await {
                println!("got {item}");
            }
            println!("stream ended");
        },
    );
}

fn main() {
    block_on(async_main());
}
```

**操作步骤**：

1. 在 `async/` 目录下新建一个 example（或在自己的 crate 里），依赖 `async-ringbuf` 与 `futures`（默认特性即可，`block_on` 需 `executor` 特性：`futures = { version = "0.3", features = ["executor"] }`）。
2. 用 `cargo run` 运行。

**需要观察的现象**：消费者依次打印 `got 0` … `got 4`，最后打印 `stream ended`。

**预期结果**：`while` 循环在缓冲区被抽空、且生产者关闭后正常退出（`cons.next()` 返回 `None`）。这说明 `Stream::poll_next` 在 `is_closed() && try_pop()==None` 时正确返回了 `Ready(None)`。

**待本地验证**：上述输出顺序在单线程 `block_on + join!` 下应稳定成立；若改用多线程运行时，元素到达顺序仍为 FIFO，但打印交错可能不同。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面程序里生产者的 `push` 循环改成写 100 个元素、缓冲区容量仍是 8，`cons.next().await` 会不会一直把数据耗尽？为什么？

**参考答案**：会。`Stream::poll_next` 在缓冲区满时只是返回 `Pending` 让出执行权，生产者随后被调度、消费者腾出空间后又被唤醒，循环往复。只要生产者最终关闭，消费者一定能把全部 100 个元素都 `next()` 出来，最后收到 `None`。

**练习 2**：`Stream::poll_next` 里为什么要在 `register_waker` **之后**再回到 `loop` 顶部复查一次，而不是直接返回 `Pending`？

**参考答案**：为了防止「丢失唤醒」。若在 `try_pop` 失败到 `register_waker` 之间，生产者恰好推进了索引并 `wake()`，由于此时还没登记 waker，那次唤醒会丢；登记后立刻再查一轮 `try_pop`，就能在挂起前补上这次本该看到的数据。

---

### 4.2 AsyncProd 作为 Sink：把缓冲区写端变成异步接收器

#### 4.2.1 概念说明

`Sink<Item>` 是 `Stream` 的对偶：它「吞」元素而不是「吐」元素。一个 `Sink` 有四个核心方法，对应「投递一个元素」的三个阶段加一个收尾：

| 方法 | 职责 | 在 async-ringbuf 中的对接 |
| --- | --- | --- |
| `poll_ready` | 「现在能接受一个元素吗？」 | 复用 `AsyncProducer::poll_ready`（[async/src/traits/producer.rs:95-110](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L95-L110)） |
| `start_send(item)` | 真正投递一个元素 | 调 `try_push`（[u3-l2](u3-l2-producer-trait.md)） |
| `poll_flush` | 冲刷缓冲 | **恒为 `Ready(Ok)`，无需冲刷**（见下） |
| `poll_close` | 关闭接收器 | 调 `AsyncProducer::close`，drop 写端 |

一个关键设计点：**`poll_flush` 是个空操作**。因为 ringbuf 的写入一旦推进 `write_index` 就立即对消费端可见（见 [u4-l2](u4-l2-direct-wrapper.md) 与 [u4-l4](u4-l4-caching-wrapper.md)，`Direct` 即时同步、`Caching` 成功即 commit），不存在「攒在内部、需要显式冲刷才出去」的延迟。所以源码注释直白写着 `// Don't need to be flushed.`。

#### 4.2.2 核心流程

`Sink::poll_ready` 复用 `AsyncProducer::poll_ready`，后者同样是两轮循环：

```
loop {
    1. 若 is_closed()  → Ready(false)    // 对端消费者已关闭
    2. 若 !is_full()   → Ready(true)     // 有空位，可以投递
    3. 若已登记 waker  → Pending
    4. 否则 register_waker; 继续复查
}
```

投递阶段 `start_send` 之所以敢直接 `assert!(try_push(item).is_ok())`，是因为协议保证 `start_send` 只在 `poll_ready` 返回 `Ready(Ok)` 之后才被调用，此刻必然有空位，`try_push` 必然成功。

`poll_close` 则调 `self.close()`，把内部 `base`（即 `Direct` 句柄）`take` 掉，从而触发 `Direct` 的 `Drop` → `hold_write(false)` → 唤醒对端消费者（见 [u6-l1](u6-l1-asyncrb-atomicwaker.md) 与 [u4-l1](u4-l1-wrap-rbref-abstraction.md) 的 `into_rb_ref`/`close` 机制）。

#### 4.2.3 源码精读

`Sink` 的实现见 [async/src/wrap/prod.rs:34-56](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/prod.rs#L34-L56)，核心部分：

```rust
impl<R: AsyncRbRef> Sink<<R::Rb as Observer>::Item> for AsyncProd<R> {
    type Error = ();

    fn poll_ready(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        Poll::Ready(if ready!(<Self as AsyncProducer>::poll_ready(self, cx)) {
            Ok(())
        } else {
            Err(())
        })
    }
    fn start_send(mut self: Pin<&mut Self>, item: <R::Rb as Observer>::Item) -> Result<(), Self::Error> {
        assert!(self.try_push(item).is_ok());
        Ok(())
    }
    fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        Poll::Ready(Ok(())) // 写入即时可见，无需冲刷
    }
    fn poll_close(mut self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.close();
        Poll::Ready(Ok(()))
    }
}
```

要点：

- `type Error = ()`：sink 永远不会因为「投递失败」而报错——满了只是 `poll_ready` 返回 `Pending`，不会 `Err`；只有对端关闭时 `poll_ready` 返回 `Err(())`，由调用方（如 `SinkExt::send`）决定如何处理。
- `poll_ready` 把 `AsyncProducer::poll_ready` 返回的 `Poll<bool>`（`true`=就绪、`false`=对端已关闭）翻译成 `Poll<Result<(), ()>>`（`Ok`=就绪、`Err`=已关闭）。其中的 `ready!` 宏用于「遇到 `Pending` 就提前 return `Pending`」。
- `start_send` 直接 `try_push`——数据面方法 `try_push` 是通过 `DelegateProducer` 委托拿到的，见 [async/src/wrap/prod.rs:21](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/prod.rs#L21)。
- `poll_close` 调用的 `self.close()` 来自 [async/src/wrap/prod.rs:29-31](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/prod.rs#L29-L31) 的 `AsyncProducer::close`，它 `drop(self.base.take())`。

注意一个**易错点**：`AsyncProducer` 有同步方法 `close(&mut self)`，而 `SinkExt` 也有异步方法 `close(&mut self) -> Close<...>`，两者同名。直接写 `prod.close().await` 会触发方法解析歧义。运行时若想经 `Sink` 显式关闭，应写全路径 `futures::sink::SinkExt::close(&mut prod).await`；更简单且等价的做法是直接 `drop(prod)`——因为 `AsyncProd`（内部 `Direct`）的 `Drop` 会调 `close`。

#### 4.2.4 代码实践

**实践目标**：把 `AsyncProd` 当作 `Sink`，用 `SinkExt::send` 投递元素，再用 `drop` 关闭写端，验证消费端在写端关闭后收到 `None`。

> 这是**示例代码**。注意：`SinkExt` 需要 `futures` 开启 `sink` 特性（`futures = { version = "0.3", features = ["executor", "sink"] }`）——这一点下一节会解释为什么。

```rust
// 示例代码
use async_ringbuf::{traits::*, AsyncHeapRb};
use futures::{executor::block_on, join, sink::SinkExt};

async fn async_main() {
    let rb = AsyncHeapRb::<i32>::new(8);
    let (prod, cons) = rb.split();

    join!(
        async move {
            let mut prod = prod;
            for i in 0..5 {
                prod.send(i).await.unwrap(); // AsyncProd 作为 Sink 接收元素
            }
            drop(prod); // 关闭写端：等价于 Sink::poll_close → AsyncProducer::close
        },
        async move {
            let mut cons = cons;
            for _ in 0..5 {
                println!("got {}", cons.pop().await.unwrap());
            }
            assert_eq!(cons.pop().await, None); // 写端已 close
        },
    );
}

fn main() {
    block_on(async_main());
}
```

**操作步骤**：

1. 确认 `futures` 依赖开启了 `sink` 特性（否则 `SinkExt` 不可用）。
2. `cargo run` 运行。

**需要观察的现象**：消费者打印 `got 0` … `got 4`；随后 `cons.pop().await` 返回 `None`（不 panic）。

**预期结果**：`send` 内部依次走 `poll_ready` → `start_send`（`try_push`）→ `poll_flush`（空操作）；`drop(prod)` 触发 `poll_close` 等价的关闭路径，复位 `write_held` 并唤醒消费者，使其在抽空后 `pop` 得到 `None`。

**待本地验证**：`send` 在缓冲区瞬时满时的等待行为需多线程或更大写入量才容易观察到；本例容量 8、写 5 个不会触发等待。

#### 4.2.5 小练习与答案

**练习 1**：`Sink` 的 `poll_flush` 为什么直接返回 `Ready(Ok)` 而不是真的去等消费者把数据读走？

**参考答案**：因为 ringbuf 写端的语义是「推进 `write_index` 即发布」——`Direct` 即时同步、`Caching` 在成功 `try_push` 后立即 `commit`（见 [u4-l2](u4-l2-direct-wrapper.md)、[u4-l4](u4-l4-caching-wrapper.md)）。数据只要进了缓冲区就对消费端可见，没有需要显式冲刷的内部暂存层，所以 `poll_flush` 无事可做。

**练习 2**：`start_send` 里为什么敢用 `assert!(try_push(item).is_ok())` 而不处理 `Err`？如果有人不遵守 `Sink` 协议、在没调用 `poll_ready` 的情况下直接 `start_send` 会怎样？

**参考答案**：`Sink` 协议规定 `start_send` 只能在 `poll_ready` 返回就绪之后调用，此刻必然有空位，`try_push` 必成功，故用 `assert` 表达这一不变量。若违反协议、在缓冲区满时直接 `start_send`，`try_push` 返回 `Err(item)`，`assert` 会 panic——这是对协议违背的明确报错，而非静默丢数据。

---

### 4.3 std feature 下的 AsyncRead / AsyncWrite：把两端变成字节管道

#### 4.3.1 概念说明

`AsyncRead` / `AsyncWrite` 是 `futures::io`（以及 `tokio::io`）里的**字节流**抽象，相当于同步世界 `std::io::Read` / `Write` 的异步版本。它们只对「字节」有意义，因此这两个 impl 都带有约束 `Item = u8`，并且整个 impl 被 `#[cfg(feature = "std")]` 包裹。

为什么必须 `std`？因为 `AsyncRead` / `AsyncWrite` 返回 `io::Result`，而 `io::Error` 定义在 `std`（或 `futures-util` 的 `io` 模块，需要单独开 `io` 特性）。`async-ringbuf` 的 `Cargo.toml` 把 `io` 的引入绑在 `std` 上：`std = ["alloc", "ringbuf/std", "futures-util/io"]`（[async/Cargo.toml:16](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/Cargo.toml#L16)）。

对照之下，`Stream` / `Sink` 不依赖 `io::Error`，所以**无条件**实现、no_std 也可用。这就解释了 cargo 配置里的一个不对称：`futures-util` 的 `sink` 特性是恒开的（[async/Cargo.toml:27-29](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/Cargo.toml#L27-L29)），而 `io` 只随 `std` 引入。

| 实现 | 约束 | feature | 底层数据面方法 |
| --- | --- | --- | --- |
| `Stream for AsyncCons` | 任意 `Item` | 无（恒可用） | `try_pop` |
| `Sink for AsyncProd` | 任意 `Item` | 无（恒可用，需 `futures-util/sink`） | `try_push` |
| `AsyncRead for AsyncCons` | `Item = u8` | `std` | `pop_slice`（批量、`Copy`） |
| `AsyncWrite for AsyncProd` | `Item = u8` | `std` | `push_slice`（批量、`Copy`） |

注意字节版本用的是 **`pop_slice` / `push_slice`**（见 [u3-l2](u3-l2-producer-trait.md)、[u3-l3](u3-l3-consumer-trait.md)），而非 `try_pop` / `try_push`——因为字节场景天然 `Copy`，用批量切片一次搬多个字节，开销更低。

#### 4.3.2 核心流程

`AsyncRead::poll_read` 要把缓冲区里的字节尽量填进调用方给的 `buf: &mut [u8]`，算法（默认实现在 [async/src/traits/consumer.rs:108-126](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/consumer.rs#L108-L126)）：

```
loop {
    1. closed = is_closed()
    2. len = pop_slice(buf)            // 尽量填，返回实际填入字节数
    3. 若 len != 0 || closed → Ready(Ok(len))   // 有数据 / EOF 都返回
    4. 若已登记 waker → Pending
    5. 否则 register_waker; 复查
}
```

这里的 EOF 语义符合 POSIX 习惯：**空且对端关闭时返回 `Ok(0)`** 表示读到结尾。注意它**不会**在「空但未关闭」时返回 `Ok(0)`（那会被误判成 EOF），而是返回 `Pending` 等待新数据——这正是第 3 步用 `len != 0 || closed` 而非单纯 `len != 0` 的原因。

`AsyncWrite::poll_write`（默认实现在 [async/src/traits/producer.rs:113-133](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/traits/producer.rs#L113-L133)）对偶：用 `push_slice(buf)` 尽量写，返回实际写入字节数；对端关闭时返回 `Ok(0)`。它的 `poll_close` 同样调 `self.close()` 关闭写端。

#### 4.3.3 源码精读

读端的两个 impl 一前一后，差别只在「单元素 `try_pop`」与「批量 `pop_slice`」：

[async/src/wrap/cons.rs:54-74](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/cons.rs#L54-L74) —— `AsyncRead`，`Item = u8`，`#[cfg(feature = "std")]`：

```rust
#[cfg(feature = "std")]
impl<R: AsyncRbRef> AsyncRead for AsyncCons<R>
where
    Self: AsyncConsumer<Item = u8>,
{
    fn poll_read(mut self: Pin<&mut Self>, cx: &mut Context<'_>, buf: &mut [u8]) -> Poll<io::Result<usize>> {
        let mut waker_registered = false;
        loop {
            let closed = self.is_closed();
            let len = self.pop_slice(buf);
            if len != 0 || closed {
                break Poll::Ready(Ok(len));
            }
            if waker_registered {
                break Poll::Pending;
            }
            self.register_waker(cx.waker());
            waker_registered = true;
        }
    }
}
```

写端 [async/src/wrap/prod.rs:58-74](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/wrap/prod.rs#L58-L74)：

```rust
#[cfg(feature = "std")]
impl<R: AsyncRbRef> AsyncWrite for AsyncProd<R>
where
    R::Rb: RingBuffer<Item = u8>,
{
    fn poll_write(self: Pin<&mut Self>, cx: &mut Context<'_>, buf: &[u8]) -> Poll<io::Result<usize>> {
        <Self as AsyncProducer>::poll_write(self, cx, buf) // 委托给默认实现
    }
    fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }
    fn poll_close(mut self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        self.close();
        Poll::Ready(Ok(()))
    }
}
```

注意 `poll_write` 直接委托给 `AsyncProducer::poll_write`（trait 默认方法），而 `poll_flush` 仍是空操作——理由与 `Sink` 一致：写入即时可见。`poll_close` 调 `self.close()` 关闭写端。

#### 4.3.4 代码实践

**实践目标**：用 `AsyncProd<u8>` 作 `AsyncWrite` 写字节、`AsyncCons<u8>` 作 `AsyncRead` 读字节，观察「写端关闭后读端读到 `Ok(0)`（EOF）」的字节流行为。

> 这是**示例代码**。需要 `futures` 开启 `io` 特性（`futures = { version = "0.3", features = ["executor", "io"] }`）；同时 `async-ringbuf` 的 `std`（默认）特性必须开启。

```rust
// 示例代码
use async_ringbuf::{traits::*, AsyncHeapRb};
use futures::executor::block_on;
use futures::io::{AsyncReadExt, AsyncWriteExt};
use futures::join;

async fn async_main() {
    let rb = AsyncHeapRb::<u8>::new(16);
    let (prod, cons) = rb.split();

    join!(
        async move {
            let mut prod = prod;
            prod.write_all(b"hi").await.unwrap(); // AsyncWrite 写字节
            // drop(prod) 关闭写端 → 读端读到 EOF
        },
        async move {
            let mut cons = cons;
            let mut buf = [0u8; 16];
            let n = cons.read(&mut buf).await.unwrap(); // AsyncRead 读字节
            println!("read {n} bytes: {:?}", std::str::from_utf8(&buf[..n]).unwrap());
            let n2 = cons.read(&mut buf).await.unwrap(); // 写端已关闭、缓冲已空 → Ok(0)
            assert_eq!(n2, 0);
            println!("EOF");
        },
    );
}

fn main() {
    block_on(async_main());
}
```

**操作步骤**：

1. 确认依赖：`async-ringbuf`（默认 `std`）+ `futures`（`executor`、`io` 特性）。
2. `cargo run` 运行。

**需要观察的现象**：打印 `read 2 bytes: Ok("hi")` 与 `EOF`。

**预期结果**：第一次 `read` 经 `pop_slice` 取回 2 字节；写端 drop 后第二次 `read` 因 `is_closed() && 空` 返回 `Ok(0)`。

**待本地验证**：若缓冲区初始为空、生产者尚未写入，`cons.read` 会 `Pending` 而非返回 `Ok(0)`——可在生产者里加 `sleep` 后验证「空但未关闭」时不会误判 EOF。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `AsyncRead`/`AsyncWrite` 的实现用 `pop_slice`/`push_slice`，而 `Stream`/`Sink` 用 `try_pop`/`try_push`？

**参考答案**：字节元素必然 `Copy`，批量切片一次搬多个字节，比逐个 `try_pop` 循环开销低（见 [u3-l2](u3-l2-producer-trait.md) 关于 `push_slice` 摊薄同步开销的讨论）。而 `Stream`/`Sink` 要支持任意 `Item`（可能非 `Copy`），只能用单元素的 `try_pop`/`try_push`。

**练习 2**：`poll_read` 的结束条件为什么写成 `len != 0 || closed`？如果只写 `len != 0` 会有什么后果？

**参考答案**：`pop_slice` 在缓冲区空时返回 `0`。若只判 `len != 0`，则「空但未关闭」时函数不会进入 `Ready`，会继续登记 waker 并 `Pending`——这其实是想要的等待行为；但「空且已关闭」（EOF）时也需要返回，所以必须额外用 `closed` 兜底，否则读到末尾会永远 `Pending`、读不到 `Ok(0)`。二者合起来才能既正确等待、又正确收尾。

---

## 5. 综合实践

把本讲四个接口串起来，实现一个「字节中转管道」：用 `AsyncWrite` 把字符串写进 `AsyncProd<u8>`，再用 `AsyncRead` 从 `AsyncCons<u8>` 读出来，最后用 `Stream`（元素视图）验证缓冲区确实被抽空。这个任务同时用到四种 trait 中的三种，并体会「同一块缓冲区可以从元素视图（Stream/Sink）和字节视图（AsyncRead/AsyncWrite）两条路访问」。

> 这是**示例代码**。需要 `async-ringbuf`（默认 `std`）与 `futures = { version = "0.3", features = ["executor", "io", "async-await"] }`。

```rust
// 示例代码
use async_ringbuf::{traits::*, AsyncHeapRb};
use futures::executor::block_on;
use futures::io::{AsyncReadExt, AsyncWriteExt};
use futures::join;

async fn async_main() {
    let rb = AsyncHeapRb::<u8>::new(32);
    let (prod, cons) = rb.split();

    join!(
        async move {
            let mut prod = prod;
            prod.write_all(b"hello ringbuf").await.unwrap(); // 字节视图：AsyncWrite
            drop(prod); // 关闭写端
        },
        async move {
            let mut cons = cons;
            let mut out = Vec::new();
            // 字节视图：AsyncRead，循环读到 EOF
            let mut buf = [0u8; 8];
            loop {
                let n = cons.read(&mut buf).await.unwrap();
                if n == 0 {
                    break;
                }
                out.extend_from_slice(&buf[..n]);
            }
            println!("received: {:?}", String::from_utf8(out).unwrap());
            // 元素视图：此时缓冲区应已空、且流已结束
            assert_eq!(cons.next().await.as_mut(), None.as_mut().map(|_: &u8| &0u8).cloned().as_ref().map(|_| 0u8));
        },
    );
}

fn main() {
    block_on(async_main());
}
```

> 说明：上面 `assert_eq!(cons.next().await, None)` 这一行写得过于绕口，可简化为：
> ```rust
> // 示例代码
> assert_eq!(cons.next().await, None); // Stream 也已结束
> ```
> （这里 `cons.next()` 需 `use futures::stream::StreamExt;`。）

**操作步骤**：

1. 配好依赖，`cargo run`。
2. 观察打印的 `received` 是否与原文一致。
3. 把 `AsyncWrite` 的写入换成 `Sink` 的 `send`（逐字节），把 `AsyncRead` 的读取换成 `Stream` 的 `next` 循环，验证两种视图给出相同结果。

**预期结果**：`received: "hello ringbuf"`，且最后的 `cons.next()` 返回 `None`，证明字节写满后又被字节读空，元素视图同步结束。

**待本地验证**：不同读取块大小（`buf` 容量）下的循环次数。

## 6. 本讲小结

- `AsyncCons` 实现 `Stream`、`AsyncProd` 实现 `Sink`，两者**无条件**实现、no_std 可用；`Stream`/`Sink` 的算法就是把 `try_pop`/`try_push` + `register_waker` + `is_closed` 套进 `poll_next`/`poll_ready` 的两轮唤醒循环。
- 开启 `std` 后，`AsyncCons<Item=u8>` 实现 `AsyncRead`、`AsyncProd<Item=u8>` 实现 `AsyncWrite`；它们改用批量的 `pop_slice`/`push_slice`，开销更低。
- 四种实现**没有新机制**，全部复用 [u6-l2](u6-l2-async-producer-consumer-futures.md) 的 `AsyncProducer`/`AsyncConsumer` 与 [u6-l1](u6-l1-asyncrb-atomicwaker.md) 的唤醒通道；关闭语义复用 hold 标志：一端 drop → 复位 hold → `wake()` → 对端 `is_closed()` 收尾。
- `Sink::poll_flush` / `AsyncWrite::poll_flush` 是空操作，因为 ringbuf 写入「推进索引即发布」、无需冲刷；真正的收尾在 `poll_close` → `AsyncProducer::close`。
- feature 不对称是刻意设计：`futures-util/sink` 恒开（支撑 no_std 的 `Sink`/`Stream`），`futures-util/io` 仅随 `std` 引入（支撑 `AsyncRead`/`AsyncWrite`）。
- 实战要点：用 `StreamExt::next`、`SinkExt::send`、`AsyncReadExt::read`、`AsyncWriteExt::write_all` 即可把缓冲区插进任意 futures 管道；注意 `AsyncProducer::close`（同步）与 `SinkExt::close`（异步）同名，显式关闭可用全路径或直接 `drop`。

## 7. 下一步学习建议

- 若想看「两个缓冲区之间搬数据」的场景，阅读 [async/src/transfer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/transfer.rs) 中的 `async_transfer`，它正是用 `AsyncProducer`/`AsyncConsumer` 的 async 方法实现的，可作为本讲接口的综合应用。
- 下一单元进入 [u7](../) 的 `ringbuf-blocking`：它会用阻塞式 `Semaphore` 而非 `AtomicWaker` 提供同步语义，并同样实现 `io::Read`/`io::Write`，建议对照本讲的 `AsyncRead`/`AsyncWrite` 体会「同一接口、不同同步策略」的设计。
- 想深入取消安全与多 Future 并发的读者，可回看 [u6-l2](u6-l2-async-producer-consumer-futures.md) 中各 Future 的 `FusedFuture` 实现，并思考：本讲的 `Stream::poll_next` 是否取消安全？为什么？
