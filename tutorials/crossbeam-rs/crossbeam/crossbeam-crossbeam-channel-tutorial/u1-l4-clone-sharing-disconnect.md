# 克隆、共享、断开与迭代

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `Sender::clone()` / `Receiver::clone()` 到底「克隆」了什么，以及为什么克隆出来的是「同一个通道」而不是「一条独立的消息流」。
- 理解 `Drop` 时通道如何「断开（disconnect）」：何时还有缓冲消息可以收、何时收发操作不再阻塞而是直接报错。
- 看懂 `counter.rs` 用引用计数管理多个收发端、并在最后一个引用释放时回收堆内存的逻辑。
- 熟练使用 `iter()`、`try_iter()`、`into_iter()` 三种迭代器来消费通道。

本讲只涉及「外壳层」的克隆、析构与迭代，不深入 array/list/zero 各 flavor 的内部队列实现（那是 u2 的内容）。

## 2. 前置知识

在进入本讲前，你需要已经掌握（见 u1-l2、u1-l3）：

- 通道（channel）由一对 `Sender<T>` 和 `Receiver<T>` 组成；`unbounded()` / `bounded(cap)` 用来创建它们。
- `send` / `recv` 是阻塞式收发，`try_send` / `try_recv` 是非阻塞式收发；底层只有「带截止时间」一种原语。
- crossbeam-channel 是 **mpmc**（多生产者多消费者）通道：`Receiver` 可以被克隆，多个线程能同时接收。

本讲会反复用到下面几个术语，先在这里统一解释：

| 术语 | 含义 |
| --- | --- |
| 引用计数（reference counting） | 用一个原子整数记录「当前有几个收发端指向同一份通道数据」，归零时才真正释放内存。 |
| 共享（sharing） | 多个 `Sender`/`Receiver` 句柄指向**同一个**底层通道，看到的是同一条消息流。 |
| 断开（disconnected） | 某一侧的所有句柄都被 `drop` 后，通道进入「这一侧没人了」的状态，对端会收到相应的错误。 |
| 会合（rendezvous） | 零容量通道里，发送和接收必须同时在场才能完成一次交接（u1-l2 已介绍）。 |

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/channel.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs) | 定义对外壳 `Sender<T>` / `Receiver<T>`，包括它们的 `Clone`、`Drop`、`iter/try_iter` 方法和三种迭代器。 |
| [src/counter.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs) | 引用计数包装器：把任意 flavor 的内部通道包成一个堆上的 `Counter<C>`，记录 senders/receivers 数量，负责断开回调与内存回收。 |
| [tests/iter.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/tests/iter.rs) | 三种迭代器的集成测试，本讲的迭代器实践以它为依据。 |
| [tests/array.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/tests/array.rs) | 含 `recv_after_disconnect` 等测试，用来印证断开语义。 |

> 提示：所有永久链接都指向当前 HEAD `6195355`。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 克隆共享：`Clone` 与引用计数** —— 克隆到底克隆了什么。
2. **4.2 断开语义：`Drop`、disconnect 与内存释放** —— drop 之后会发生什么。
3. **4.3 迭代器消费：`iter` / `try_iter` / `into_iter`** —— 怎么用迭代器风格消费通道。

### 4.1 克隆共享：Clone 与引用计数

#### 4.1.1 概念说明

直觉上，「克隆」一个东西往往会得到一份独立的拷贝。但通道的克隆语义恰好相反：

> `s.clone()` 得到的 `s2` 和 `s` 指向的是**同一个通道**。往 `s` 或 `s2` 发消息，都会进入同一条队列；任何一个克隆端 `drop`，只要还有其它克隆端在，通道就还活着。

这正是 mpmc 得以实现的基础：你可以把一个 `Sender` 克隆很多份分发到多个工作线程，它们并发写入的是同一个通道；也可以把 `Receiver` 克隆多份，多个消费者从同一个通道里抢消息。

关键结论：

- 克隆 ≠ 复制消息流。**没有**「每个 receiver 各收到一份」的广播语义，一条消息只会被**其中一个** receiver 收走。
- 既然多个句柄共享同一份底层通道数据，就必须有人记录「还剩几个句柄」，这个角色就是引用计数。

#### 4.1.2 核心流程

`counter.rs` 用一个堆上的结构体 `Counter<C>` 来承载通道本体和两个计数器：

```text
Counter<C> {
    senders:   AtomicUsize,   // 当前有几个 Sender 句柄
    receivers: AtomicUsize,   // 当前有几个 Receiver 句柄
    destroy:   AtomicBool,    // 「是否已经有人触发了销毁」的协调标志
    chan:      C,             // 真正的 flavor 通道（array/list/zero 之一）
}
```

克隆与释放的对称操作：

```text
clone (Sender):   senders.fetch_add(1)   → +1
drop   (Sender):  senders.fetch_sub(1)   → -1，若归零则触发 disconnect

clone (Receiver): receivers.fetch_add(1) → +1
drop   (Receiver): receivers.fetch_sub(1) → -1，若归零则触发 disconnect
```

注意：`Sender` 和 `Receiver` 的计数是**分开**的。一个通道可以有「3 个 sender、1 个 receiver」，也可以是「1 个 sender、5 个 receiver」。

#### 4.1.3 源码精读

先看通道创建时如何被「计数化」。以 `unbounded` 为例：

[`unbounded()` 把 list flavor 通道交给 `counter::new`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L50-L59) —— 注意它返回的 `s`/`r` 已经被 `counter::new` 包了一层。

[`counter::new` 在堆上分配 `Counter`，并把 senders/receivers 都初始化为 1](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L27-L37)。这里用 `Box::leak` 拿到裸指针 `NonNull`，意味着这块内存**不会**随某个句柄自动释放，必须由引用计数手动回收（见 4.2）。

克隆操作就是调用 `acquire`：

[`Sender::acquire` 把 senders 计数 +1](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L51-L64)。两个要点：

1. 计数自增用 `Ordering::Relaxed` 即可——因为我们只是「报名多一个句柄」，并不依赖这次自增与其他读写建立 happens-before。
2. 末尾有一段**溢出保护**：如果用户反复 `clone` 再 `mem::forget`，理论上能让计数无限增长。库选择在计数超过 `isize::MAX` 时直接 `process::abort()`，因为这种病态场景无法优雅恢复。

外壳的 `Clone for Sender<T>` 只是按 flavor 转发到 `acquire`：

[`Clone for Sender` 对三种 flavor 统一调用 `chan.acquire()`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L686-L696)。`Receiver` 的克隆结构完全对称：[Array/List/Zero 走 `acquire`，而只读的 At/Tick 走 `Arc::clone`，Never 则新建一个零大小实例](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1199-L1212)。

> 小结：克隆 = 「在共享的 `Counter` 上 +1，并复制一份指向同一块堆内存的指针」。所以所有克隆端看到的是同一个 `chan`。

#### 4.1.4 代码实践

**实践目标**：验证克隆出的多个 `Sender` 写入的是同一个通道、单个 `Receiver` 能收到全部消息。

**操作步骤**：

1. 新建一个 binary（例如 `examples/clone_share.rs`，或直接放进一个临时 crate 依赖 `crossbeam-channel`）。
2. 创建一个 `bounded` 通道，克隆 3 个 sender 分给 3 个线程，每个线程发送不同的值。
3. 主线程用唯一一个 `Receiver` 收齐。

```rust
// 示例代码（非项目原有文件，可自行新建）
use std::thread;
use crossbeam_channel::bounded;

let (s, r) = bounded::<i32>(64);
let mut handles = Vec::new();

// 3 个生产者，各自发送互不重叠的一段数字
for tid in 0..3i32 {
    let s = s.clone();           // 克隆：3 个句柄指向同一通道
    handles.push(thread::spawn(move || {
        for v in 0..3 {
            s.send(tid * 10 + v).unwrap(); // 收端被 drop 时才可能 Err
        }
    }));
}

for h in handles {
    h.join().unwrap();
}
drop(s); // 丢掉主线程手里最后一个 sender

// 单个 receiver 收齐 9 条；顺序在线程间不确定，故按排序后比较
let mut got: Vec<i32> = r.iter().collect();
got.sort();
let mut want: Vec<i32> = (0..3).flat_map(|tid| (0..3).map(move |v| tid * 10 + v)).collect();
want.sort();
assert_eq!(got, want);
```

**需要观察的现象**：

- 程序正常退出，`assert_eq!` 通过，说明 3 个克隆 sender 的 9 条消息都被同一个 receiver 收到。
- 若把 `drop(s)` 去掉，`r.iter().collect()` 会**永远阻塞**——因为还剩 sender，迭代器以为「可能还有消息」，一直等下去（这点在 4.3 会解释）。

**预期结果**：`got.len() == 9`，且 `got` 排序后等于 `[0,1,2,10,11,12,20,21,22]`。

> 本地运行：`cargo run`（或在你自己的 crate 里运行）。如果改动后行为不符，请先确认是否 `drop` 了所有 sender。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面的 `s.clone()` 改成不克隆、直接让 3 个线程 move 同一个 `s`，会发生什么编译错误？为什么？

**答案**：`Sender` 没有实现 `Copy`，move 之后 `s` 在主作用域就失效了，且第二个线程无法再拿到 `s`，编译器会报「value moved into closure」的借用错误。这就是为什么多生产者场景必须用 `clone()` 产出多个独立句柄。

**练习 2**：下面两段代码里，`r2` 收到的是 `r1` 收到的「副本」吗？

```rust
let (s, r1) = unbounded::<i32>();
let r2 = r1.clone();
s.send(1).unwrap();
```

**答案**：不是。`r1` 与 `r2` 共享同一个通道，这条消息只会被它们当中的**一个**收走（谁先 `recv` 谁拿到）。克隆 receiver 得到的是「多一个竞争消费者」，而不是「多一份广播」。

---

### 4.2 断开语义：Drop、disconnect 与内存释放

#### 4.2.1 概念说明

「断开」描述的是：当通道某一侧的所有句柄都被 `drop` 之后，整个通道进入的一种状态。它有两种方向，对应两条规则：

1. **所有 sender 都 drop（发送侧断开）**：不会再有新消息了。但**已经缓冲在通道里的消息仍然可以收**；当缓冲排空后，`recv` 立即返回 `Err(RecvError)`（不再阻塞）。
2. **所有 receiver 都 drop（接收侧断开）**：没人收消息了。此时 `send` 立即返回 `Err(SendError(msg))` 并把消息原样退还；通道里**尚未被收走**的消息会被直接丢弃（drop），以便尽早释放内存（这是近版本 `#1121` 的行为改动）。

一句话总结本讲的核心学习目标之一：

> **断开后剩余消息仍可接收（指发送侧断开），而所有收发操作都不再阻塞——要么成功，要么立即报错。**

#### 4.2.2 核心流程

`Drop` 的职责链（以 `Sender` 为例）：

```text
Sender::drop
  └─ chan.release(|c| c.disconnect_senders())   // chan 是 counter::Sender
       └─ counter::Sender::release:
            1) senders.fetch_sub(1, AcqRel)
            2) 若返回值 == 1（即原来是 1，现在是 0）:
                 a) 调用传入的 disconnect 回调（disconnect_senders）
                 b) destroy.swap(true, AcqRel)：若返回 true → 真正释放堆内存
```

关键点：

- `fetch_sub` 返回的是**修改前**的值。返回 `1` 才意味着「我是最后一个 sender」。
- 只有「最后一个」才调用 disconnect 回调。中间的 drop 只是把计数 -1。
- 内存释放的时机由 `destroy` 标志协调（见 4.2.3 的 `swap` 解释），确保**恰好一次**释放，既不重复 free 也不泄漏。

#### 4.2.3 源码精读

先看外壳的 `Drop for Sender<T>`：

[`Drop for Sender` 按 flavor 调用 `release`，并传入「发送侧断开」回调](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L674-L684)。注意 array/list 用 `disconnect_senders`，而 zero（会合通道）用 `disconnect`——因为 zero 不存消息，收发两侧是对称的。

`Receiver` 的析构与之对称：[Array/List 调 `disconnect_receivers`，Zero 调 `disconnect`，而 At/Tick/Never 这三种「特殊只读通道」析构时什么也不做](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1184-L1197)。

真正干活的是 `counter::Sender::release`：

[`release`：计数 -1；归零时调用 disconnect，再用 `destroy.swap` 决定是否回收内存](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L69-L77)。`Receiver::release` 完全对称（[counter.rs:128-136](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L128-L136)）。

**`destroy.swap(true)` 为什么用 `swap` 而不是「先读再写」？**

senders 和 receivers 的计数是独立的，两侧可能各自先归零。我们要求：**整体上最后一个被释放的句柄（无论它是 sender 还是 receiver）负责 free 内存，且只能 free 一次**。

`swap(true)` 是一次原子操作，它把 `destroy` 置为 `true` 并返回**旧值**。于是：

| 场景 | senders 侧 swap 返回 | receivers 侧 swap 返回 | 谁来 free |
| --- | --- | --- | --- |
| senders 先归零、receivers 还活着 | `false`（不动内存） | —— | 暂不 free（receivers 还在用） |
| receivers 随后归零 | —— | `true` | **receivers 侧 free** |
| receivers 先归零、senders 还活着 | —— | `false` | 暂不 free |
| senders 随后归零 | `true` | —— | **senders 侧 free** |

因为 `swap` 是原子的，即使两侧「同时」归零，两次 swap 也被串行化，**恰好有一侧**拿到旧值 `true` 去执行 `Box::from_raw` 释放。这样就避免了双重释放和内存泄漏。

> 断开回调本身做什么？以 array flavor 为例，[`disconnect_senders` 设置一个「标记位」并唤醒所有阻塞的 receiver](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L487-L496)，[`disconnect_receivers` 则唤醒所有阻塞的 sender 并丢弃剩余消息](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L506-L517)。这些细节属于 u2 的内容，这里只需知道「断开会唤醒对端、并可能丢弃消息」即可。

#### 4.2.4 代码实践

**实践目标**：亲手验证「发送侧断开后，缓冲消息仍可收，排空后 `recv` 立即报错不阻塞」。

**操作步骤**：直接照搬项目里的测试 `recv_after_disconnect` 来写一个小程序：

```rust
// 示例代码：对应 tests/array.rs 中的 recv_after_disconnect 思路
use crossbeam_channel::{bounded, RecvError};

let (s, r) = bounded(100);
s.send(1).unwrap();
s.send(2).unwrap();
s.send(3).unwrap();
drop(s);                       // 所有 sender 被 drop → 发送侧断开

assert_eq!(r.recv(), Ok(1));   // 缓冲里的消息照常可收
assert_eq!(r.recv(), Ok(2));
assert_eq!(r.recv(), Ok(3));
assert_eq!(r.recv(), Err(RecvError)); // 排空后立即返回错误，不阻塞
```

**需要观察的现象**：

- 最后一次 `r.recv()` **瞬间**返回 `Err(RecvError)`，没有卡住——说明断开后 recv 不会无限等待。
- 前三次 `recv` 仍能拿到 `1/2/3`——说明「断开」不影响已缓冲的消息。

**预期结果**：与 [tests/array.rs 中 `recv_after_disconnect` 的断言完全一致](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/tests/array.rs#L240-L253)。

**补充观察（接收侧断开）**：参考同文件的 [`send_after_disconnect`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/tests/array.rs#L222-L237)：drop 掉 `r` 之后，`s.send(4)` 会立刻得到 `Err(SendError(4))`，消息被原样退回。

#### 4.2.5 小练习与答案

**练习 1**：一个 `bounded(0)`（会合）通道，只有一个线程。直接 `s.send(1)` 会怎样？

**答案**：因为没有 receiver 同时在场，`send` 会**永久阻塞**（死锁）。零容量通道不存消息，必须有另一个线程在 `recv` 才能「会合」完成交接。这正说明「断开」与「会合」是两回事：会合要求收发同时在场，断开要求某一侧句柄全部消失。

**练习 2**：为什么 `counter::Sender::release` 里要判断 `fetch_sub(...) == 1`，而不是 `== 0`？

**答案**：`fetch_sub` 返回的是**修改前**的值。当最后一个句柄释放时，修改前是 1、修改后是 0，所以返回值是 1。如果写成 `== 0`，就永远不会触发 disconnect 与回收，导致内存泄漏。

**练习 3**：假如 senders 和 receivers **同时**都归零（极端并发），会不会两个线程都去 `Box::from_raw` 导致双重释放？

**答案**：不会。释放与否取决于 `destroy.swap(true)` 的返回值，`swap` 是单次原子读改写，两次调用必被串行化：先执行的那次返回 `false`（不释放），后执行的那次返回 `true`（释放）。所以**恰好一次**释放。

---

### 4.3 迭代器消费：iter / try_iter / into_iter

#### 4.3.1 概念说明

crossbeam-channel 把「不断从通道收消息」这件事包装成了标准库的 `Iterator`，于是你可以用 `for` 循环、`.collect()`、`.take(n)` 等熟悉的方式来消费通道。共有三种迭代器，区别在于「阻塞与否」和「是否拥有 receiver」：

| 构造方式 | 类型 | 是否阻塞 | 是否拥有 `Receiver` |
| --- | --- | --- | --- |
| `r.iter()` / `(&r).into_iter()` | `Iter<'a, T>` | **阻塞**：每次 `next` 等下一条消息 | 否（借用） |
| `r.try_iter()` | `TryIter<'a, T>` | **非阻塞**：没消息就结束 | 否（借用） |
| `r.into_iter()` | `IntoIter<T>` | **阻塞** | 是（消费掉 `r`） |

三条统一的终止规则：**当通道「空且已断开」时，`next()` 返回 `None`。** 这意味着用迭代器完整消费一个通道时，通常需要 drop 掉所有 sender，迭代器才能自然结束。

#### 4.3.2 核心流程

三种迭代器 `next()` 的实现极其精简：

```text
Iter::next     → self.receiver.recv().ok()       // 阻塞收；Err → None
TryIter::next  → self.receiver.try_recv().ok()   // 非阻塞收；Err → None
IntoIter::next → self.receiver.recv().ok()       // 同 Iter，但自己拥有 receiver
```

它们的「终止」完全复用了 `recv` / `try_recv` 的错误语义：

- `Iter`/`IntoIter`：`recv()` 在「空 + 断开」时返回 `Err(RecvError)`，`.ok()` 得到 `None`，迭代结束。在此之前 `recv()` 会**阻塞**等待。
- `TryIter`：`try_recv()` 在「空」时返回 `Err(Empty)`，`.ok()` 得到 `None`，**立即**结束当前一轮（不等待）。

#### 4.3.3 源码精读

[`Receiver::iter()` 返回一个借用 `&self` 的阻塞迭代器](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1103-L1105)。`Iter` 结构体只持有一个 `&Receiver`：

[`Iter::next` 就是 `self.receiver.recv().ok()`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1272-L1278)，并且实现了 `FusedIterator`（一旦返回过 `None`，之后保证一直返回 `None`）。

非阻塞版本 `try_iter`：

[`TryIter::next` 是 `self.receiver.try_recv().ok()`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1324-L1330)。注意它**没有**实现 `FusedIterator`——因为它本就不阻塞，遇到空就停，是否「融合」无所谓。

最后是「消费式」迭代器。有两个 `IntoIterator` 实现：

- [`&Receiver: IntoIterator` 产出 `Iter`（借用）](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1220-L1227)
- [`Receiver: IntoIterator` 产出 `IntoIter`（拥有）](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1229-L1236)

[`IntoIter::next` 同样是 `self.receiver.recv().ok()`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1372-L1378)，区别只是它**拥有** receiver，所以 `into_iter()` 之后原来的 `r` 不能再用了。

> 三者都只是 `recv/try_recv + .ok()` 的薄包装。理解了 u1-l3 的收发语义，迭代器就自然懂了。

#### 4.3.4 代码实践

**实践目标**：体会三种迭代器在「阻塞」与「是否拥有 receiver」上的差异。

**操作步骤 1：`iter()` 阻塞直到断开**

参照 [tests/iter.rs 的 `nested_recv_iter`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/tests/iter.rs#L7-L27)：

```rust
use std::thread;
use crossbeam_channel::unbounded;

let (s, r) = unbounded::<i32>();
thread::spawn(move || {
    s.send(3).unwrap();
    s.send(1).unwrap();
    s.send(2).unwrap();
    drop(s);            // 关键：drop 后 iter() 才会结束
});
let v: Vec<i32> = r.iter().collect();   // 阻塞收齐 3 条，直到 sender drop
assert_eq!(v, vec![3, 1, 2]);           // FIFO，顺序确定
```

**需要观察的现象**：删掉 `drop(s)` 后，`collect()` 会永远卡住——因为没有 sender 被 drop，`iter()` 认为可能还有后续消息。

**操作步骤 2：`try_iter()` 非阻塞快照**

参照 [tests/iter.rs 的 `recv_try_iter`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/tests/iter.rs#L58-L83)：在循环里反复用 `try_iter()` 把「当前已经到达」的消息一次性抽干，然后做别的事（比如发请求），适合做轮询式消费者。

```rust
// 抽干当前所有已就绪消息，不阻塞
let snapshot: Vec<_> = r.try_iter().collect();
```

**需要观察的现象**：`try_iter()` 永远不会阻塞，即使通道还连着、只是暂时没消息，它也立刻返回空。

**操作步骤 3：`into_iter()` 拥有 receiver**

参照 [tests/iter.rs 的 `recv_into_iter_owned`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/tests/iter.rs#L86-L97)：在 sender 已经 drop 之后，用一个 `into_iter()` 把缓冲里的消息收完即止。

```rust
let iter = {
    let (s, r) = unbounded::<i32>();
    s.send(1).unwrap();
    s.send(2).unwrap();
    r.into_iter()        // 消费 r；s 在块结束时 drop
};
assert_eq!(iter.next(), Some(1));
assert_eq!(iter.next(), Some(2));
assert_eq!(iter.next(), None);
```

**需要观察的现象**：`into_iter()` 之后 `r` 已被 move，无法再用；由于 `s` 也已 drop，迭代器收完两条后自然返回 `None`。

**预期结果**：三种迭代器的行为与 `tests/iter.rs` 中对应测试的断言一致。如本地运行结果不确定（例如涉及 `thread::sleep` 的时序），请以「待本地验证」态度核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Iter` 实现了 `FusedIterator`，而 `TryIter` 没有？

**答案**：`FusedIterator` 承诺「一旦返回 `None`，后续都返回 `None`」。`Iter` 基于 `recv()`，一旦拿到 `None` 说明通道已断开，之后不可能再有消息，符合融合语义，声明 `FusedIterator` 能让一些迭代器适配器（如 `flatten`、`scan`）做优化。`TryIter` 基于 `try_recv()`，这次返回 `None` 只代表「这一刻是空的」，下一刻可能又有消息，所以不能声明融合。

**练习 2**：下面代码会输出什么？为什么？

```rust
let (s, r) = unbounded::<i32>();
s.send(1).unwrap();
for x in &r {            // 借用式 into_iter → Iter
    println!("{x}");
}
```

**答案**：会**永久阻塞**。`for x in &r` 等价于 `r.iter()`，它是阻塞迭代器，收完 `1` 之后会继续 `recv()` 等下一条；由于 `s` 还没被 drop，通道没断开，于是卡住。要让循环结束，需在另一处 drop 掉所有 sender，或改用 `r.try_iter()` 只抽干当前消息。

**练习 3**：`r.into_iter()` 和 `(&r).into_iter()` 调用的是同一个 `IntoIterator` 实现吗？

**答案**：不是。前者调用 `impl IntoIterator for Receiver`（产出拥有式的 `IntoIter`，消费 `r`）；后者调用 `impl IntoIterator for &Receiver`（产出借用式的 `Iter`，`r` 仍可用）。两者 `next()` 都基于 `recv().ok()`，但生命周期与所有权不同。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「多生产者 → 单消费者 draining」的小任务。

**需求**：

- 4 个工作线程，每个持有克隆的 `Sender`，各发送 5 条带线程编号的消息（共 20 条）。
- 主线程用唯一的 `Receiver` 接收。
- 等所有工作线程结束后，drop 主线程的 sender，再用 `iter().collect()` 把**剩余**消息一次性收齐。
- 因为 4 个线程并发发送，跨线程的顺序不确定；请断言「总数 = 20」且「按值排序后等于期望集合」。
- 额外：用 `try_iter()` 在 join 之前先抽干一次，体会它与 `iter()` 的区别。

**参考实现（示例代码）**：

```rust
use std::thread;
use crossbeam_channel::unbounded;

const N_THREADS: i32 = 4;
const PER_THREAD: i32 = 5;

let (s, r) = unbounded::<i32>();
let mut handles = Vec::new();

for tid in 0..N_THREADS {
    let s = s.clone();
    handles.push(thread::spawn(move || {
        for v in 0..PER_THREAD {
            s.send(tid * 100 + v).unwrap();
        }
    }));
}

// 非阻塞地先抽一波（可能为空或不为空，都不阻塞）
let _snapshot: Vec<i32> = r.try_iter().collect();

for h in handles {
    h.join().unwrap();
}
drop(s); // 触发发送侧断开

// 阻塞式收齐剩余全部消息
let mut got: Vec<i32> = r.iter().collect();
assert_eq!(got.len(), (N_THREADS * PER_THREAD) as usize);
got.sort();
let mut want: Vec<i32> = (0..N_THREADS).flat_map(|tid| (0..PER_THREAD).map(move |v| tid * 100 + v)).collect();
want.sort();
assert_eq!(got, want);
```

**思考题**（不必写代码）：

1. 为什么必须 `drop(s)` 之后 `iter().collect()` 才会返回？如果不 drop 会怎样？
2. `try_iter()` 抽走的那一波消息，会不会让最终的 `got` 少于 20 条？为什么？（提示：`snapshot` 里收走的消息就不再留在通道里了。）
3. 如果把 `unbounded` 换成 `bounded(8)`，这个程序还能跑通吗？为什么？（提示：通道容量小于消息总数，但 receiver 在主线程持续收/抽干，所以不会死锁。）

## 6. 本讲小结

- **克隆 = 共享同一个通道**：`Sender::clone` / `Receiver::clone` 只是在堆上的 `Counter` 里把对应计数 +1，多个句柄看到的是同一条消息流；消息不会被广播，只会被其中一个收端取走。
- **Drop 链路**：外壳 `Drop` → `counter::Sender/Receiver::release` → 计数 -1 → 归零时调用 flavor 的 `disconnect_senders/disconnect_receivers` 回调。
- **断开语义**：发送侧断开后，**已缓冲的消息仍可收**，排空后 `recv` 立即返回 `Err(RecvError)`；接收侧断开后，`send` 立即返回 `Err(SendError(msg))` 并丢弃剩余消息。两侧都不再阻塞。
- **内存回收靠 `destroy.swap`**：senders 与 receivers 计数独立归零，用一次原子 `swap(true)` 协调，保证恰好由「整体最后一个句柄」释放堆内存，不重不漏。
- **三种迭代器**：`iter()`/`IntoIter` 阻塞、`try_iter()` 非阻塞；`next()` 都是 `recv/try_recv + .ok()`；终止条件是「空且断开」。
- **计数溢出保护**：`acquire` 在计数超过 `isize::MAX` 时 `abort()`，防御反复 `clone + forget` 的病态用法。

## 7. 下一步学习建议

本讲只看了「外壳层」的克隆、析构与迭代，把 `counter.rs` 的引用计数当作黑盒用了。接下来建议：

- **进入 u2（进阶层）**：先读 [u2-l1 通道架构与 flavors 模型总览](./u2-l1-architecture-and-flavors.md)，建立「公共类型壳 + 多 flavor 实现」的整体图景；再读 [u2-l2 引用计数 counter.rs](./u2-l2-reference-counter.md)，把本讲里一带而过的 `Counter`、`destroy` 标志、内存序彻底搞懂。
- **想理解 disconnect 回调到底做了什么**：直接去看 [src/flavors/array.rs 的 `disconnect_senders`/`disconnect_receivers`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L487-L517)，对照本讲 4.2.3 的说明。
- **动手扩展**：尝试写一个「多 receiver 竞争消费」的小例子，观察消息如何在多个克隆 receiver 之间分配（每个消息只被一个 receiver 收到），为后面学习 mpmc 的内部调度做铺垫。
