# 引用计数与生命周期 counter.rs

## 1. 本讲目标

- 搞清楚为什么多个 `Sender`/`Receiver` clone 之后「共享同一个通道」，而不是各自复制一份消息流。
- 理解 `src/counter.rs` 怎样用一个堆上的 `Counter` 结构 + 两个原子计数 + 一个 `destroy` 标志，统一管理三种「真实」通道（array/list/zero）的生命周期。
- 掌握 `acquire`（克隆加计数）、`release`（释放减计数、触发断开回调、决定销毁时机）的完整逻辑。
- 理解 `destroy` 标志为什么必须用原子 `swap` 而不是 `load`+`store`，以及计数溢出时为什么直接 `abort`。
- 能够跟踪一次「创建 → 多次 clone → 全部 drop」的计数变化，并指出谁负责释放堆内存。

## 2. 前置知识

承接 u2-l1，我们已经知道 crossbeam-channel 是「一套公共壳（`Sender`/`Receiver`）+ 六种 flavor」的架构。其中只有 array/list/zero 三种是「真实」的、可被双方持有并可 clone/disconnect 的通道；at/tick/never 是只读特殊通道，用 `Arc` 或零大小类型管理，**不走 `counter.rs`**。本讲只讲这三种「真实」通道共享与销毁的底层机制。

需要补充的基础概念：

- **引用计数（reference counting）**：让多个所有者共享同一块数据的技术，Rust 标准库的 `Arc` 就是一种实现。`counter.rs` 实际上是「为通道量身定制的迷你 Arc」——它没有用 `Arc<Channel>`，而是把「发送端计数」和「接收端计数」**分开**记录，这样才能区分「所有发送端都走了」和「所有接收端都走了」两件不同的事。
- **`NonNull<T>`**：一个「保证非空」的裸指针包装类型，不拥有内存、零开销，常用于 unsafe 代码里手动管理堆对象。
- **`Box::leak` / `Box::from_raw`**：`Box::leak` 把一个 `Box` 故意「泄漏」成不再自动释放的引用；`Box::from_raw` 则把裸指针重新装箱成 `Box`，drop 时释放堆内存。`counter.rs` 用这对组合手动控制通道堆对象的生死。
- **原子操作与内存序**：`AtomicUsize::fetch_add`/`fetch_sub`、`AtomicBool::swap`，以及 `Ordering::Relaxed`、`AcqRel`。本讲只讲「够用」的部分，深入的内存序分析留给 u3-l4。
- **mpmc 共享语义**：回顾 u1-l4——clone 一个 `Receiver` 不是复制消息流，而是多一个「取消息的人」共享同一队列，一条消息只会被其中一个 receiver 取走。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/counter.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs) | 引用计数核心：`Counter<C>` 堆对象、`new` 构造、`Sender<C>`/`Receiver<C>` 句柄、`acquire`/`release`/`addr`/`Deref`/`PartialEq` |
| [src/channel.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs) | 对外壳层：`bounded`/`unbounded` 调 `counter::new`；`Sender`/`Receiver` 的 `Clone`/`Drop` 转发到 `acquire`/`release` |
| [src/flavors/array.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs) | 有界数组 flavor，提供 `disconnect_senders`/`disconnect_receivers`（release 时被回调） |
| [src/flavors/list.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs) | 无界链表 flavor，同样提供两个 disconnect 方法 |
| [src/flavors/zero.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs) | 零容量 flavor，两端共用单一 `disconnect` 方法 |
| [tests/list.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/tests/list.rs) | `drops()` 测试，用 `DropCounter` 验证所有消息最终都被释放，可作实践参考 |

## 4. 核心概念与源码讲解

### 4.1 引用计数的整体设计：一个 Counter 管一个通道

#### 4.1.1 概念说明

回顾 u2-l1：构造函数 `bounded`/`unbounded` 会先创建一个 flavor 的 `Channel`，再调 `counter::new(chan)`。这一步就是把「裸的通道实现」包成一个「被引用计数的共享对象」。

为什么不用现成的 `Arc<Channel>`？因为通道有一个 `Arc` 表达不了的语义：**发送端和接收端的「全员阵亡」需要分开判断**。

- `Arc` 只有一个计数，计数归零就释放。但通道需要区分「所有 `Sender` 都 drop 了」（此时 receiver 还能取走缓冲区里的剩余消息，排空后才报 disconnected）和「所有 `Receiver` 都 drop 了」（此时 sender 的 send 立刻失败并丢弃剩余消息）这两种不同事件。
- 所以 `counter.rs` 在堆上放一个 `Counter<C>`，同时记录 `senders` 和 `receivers` 两个计数，外加一个 `destroy` 标志来协调「谁该释放堆内存」。

三个设计要点：

1. `Counter<C>` 在堆上**只分配一次**（创建通道时），之后所有 clone 出来的 `Sender`/`Receiver` 都指向同一个堆对象（通过 `NonNull`）。
2. `Sender<C>`/`Receiver<C>` 是轻量的栈上句柄，只持有一个指向 `Counter` 的 `NonNull` 指针。
3. 真正的通道实现 `C`（如 `flavors::array::Channel<T>`）嵌在 `Counter` 内部。`Sender<C>` 通过 `Deref` 透明地访问它，所以壳层代码写 `chan.send(...)` 时，`chan` 虽然是 `counter::Sender`，但 `.send` 经 Deref 落到内部 flavor 上。

#### 4.1.2 核心流程

创建一个 bounded 通道的生命周期可概括为：

```text
bounded(cap)
   │
   ├─ flavors::array::Channel::with_capacity(cap)   // 造出裸通道 C
   │
   └─ counter::new(C)                              // 包成计数对象
          │
          ├─ Box::new(Counter { senders:1, receivers:1, destroy:false, chan:C })
          ├─ Box::leak  → NonNull<Counter<C>>       // 拿到堆指针，放弃自动 drop
          ├─ 返回 Sender { counter }                // senders 计数 = 1
          └─ 返回 Receiver { counter }              // receivers 计数 = 1

clone(sender) → counter::Sender::acquire()  → senders += 1（复制同一指针）
drop(sender)  → counter::Sender::release()  → senders -= 1
                                            └─ 若归 0：disconnect_senders()，再判断是否释放堆
```

关键不变量：**`Counter<C>` 这个堆对象恰好被释放一次**——由「最后一个被 drop 的句柄（不论是最后一个 sender 还是最后一个 receiver）」负责。

#### 4.1.3 源码精读

先看 `Counter` 结构和它的字段（src/counter.rs 第 12–24 行）：

[src/counter.rs:12-24](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L12-L24) —— `Counter<C>` 包含发送端计数 `senders`、接收端计数 `receivers`、销毁标志 `destroy`，以及真正的通道实现 `chan: C`。三个状态字段都是原子的（`AtomicUsize`/`AtomicBool`），因为多线程会并发 clone 和 drop。

再看 `new` 函数（src/counter.rs 第 27–37 行）：

[src/counter.rs:27-37](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L27-L37) —— 把通道 `chan` 装进 `Box`，用 `Box::leak` 故意「泄漏」所有权，拿到一个 `NonNull<Counter<C>>`；然后用同一个指针分别造出 `Sender` 和 `Receiver`，两个计数都初始化为 1。

细节：`Box::leak` 之后这块堆内存的释放责任就完全交给 `release` 里的手动逻辑——Rust 所有权系统不再管它。

接着看 `Sender`/`Receiver` 句柄本身（src/counter.rs 第 40–42 行、第 99–101 行）：

[src/counter.rs:40-42](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L40-L42) —— `Sender<C>` 只持有一个 `NonNull<Counter<C>>`，没有别的字段；`Receiver<C>` 结构完全对称。两个句柄都极轻量（一个机器字），clone 和 move 都很便宜。

为了让壳层代码能把 `counter::Sender` 当成内部通道用，counter.rs 给它实现了 `Deref`（src/counter.rs 第 84–90 行）：

[src/counter.rs:84-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L84-L90) —— `Deref::Target = C`，解引用得到内部通道 `chan`。这就是为什么 channel.rs 里能写 `chan.send(msg, None)`、`chan.disconnect_senders()`——这些方法经 Deref 落到 flavor 实现上。

`counter()` 是一个私有 unsafe 辅助方法（src/counter.rs 第 46–48 行），把 `NonNull` 转回 `&Counter`：

[src/counter.rs:46-48](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L46-L48) —— `unsafe { self.counter.as_ref() }`。unsafe 是因为编译器无法证明指针仍有效，这由「计数 > 0 时指针必然有效」的不变量保证，而 `release` 正是维护这个不变量的。

对 select 和 `same_channel` 很重要的方法 `addr`（src/counter.rs 第 79–81 行）：

[src/counter.rs:79-81](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L79-L81) —— 把堆指针转成 `usize` 作为通道的唯一地址标识。两个句柄指向同一通道当且仅当它们的 `counter` 指针相等。

对应地，`PartialEq`（src/counter.rs 第 92–96 行）直接比较指针：

[src/counter.rs:92-96](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L92-L96) —— `self.counter == other.counter`。`NonNull` 的 `PartialEq` 比较底层指针地址，这就是 `same_channel` 的底层实现。

最后看壳层 channel.rs 怎么把构造函数接到 counter 上（src/channel.rs 第 113–133 行）：

[src/channel.rs:113-133](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L113-L133) —— `bounded`：cap==0 用 zero flavor，否则用 array flavor；两种都先 `counter::new(chan)` 拿到计数化的 `counter::Sender`/`counter::Receiver`，再包进对外枚举 `SenderFlavor`/`ReceiverFlavor`。

`unbounded` 同理（src/channel.rs 第 50–59 行）走 list flavor：

[src/channel.rs:50-59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L50-L59) —— `counter::new(flavors::list::Channel::new())`。

可以注意到：at/tick/never 三种特殊通道**不经过 `counter.rs`**，它们用 `Arc`（at/tick）或零大小类型（never）。这就是为什么 counter.rs 只需要服务于「真实」的三种 flavor。

#### 4.1.4 代码实践

**实践目标**：亲手验证「clone 后多个 `Sender` 指向同一个 `Counter`」。

操作步骤（源码阅读型）：

1. 对照 `Clone for Sender`（见 4.2.3）：`bounded(1)` 后 `let s2 = s.clone();`。
2. 对照 `same_channel` 实现（src/channel.rs:656-663）确认 `s.same_channel(&s2)` 返回 `true`，而与另一个新通道的 sender 比较返回 `false`。
3. 跟踪 `bounded(1)` 内部：`counter::new` 只被调用一次，整个通道只有一份 `Counter` 堆对象。

需要观察的现象：

- `s` 和 `s2`（间接体现为 `addr()`）相等。
- 创建两个独立通道时，它们的地址不同。

预期结果：clone 共享同一通道、同一计数对象；不同通道各有一份 `Counter`。

（本实践为源码阅读型，无需运行；若想运行，可写一个断言 `s.same_channel(&s2) == true` 的小测试。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Counter` 不直接用标准库的 `Arc<Channel>`？

**答案**：`Arc` 只有一个计数，无法区分「所有发送端 drop」和「所有接收端 drop」这两种不同的断开事件；通道需要分别针对这两种事件唤醒对端、决定保留或丢弃消息，所以必须把发送端计数和接收端计数分开。

**练习 2**：`Sender<C>` 里为什么用 `NonNull<Counter<C>>` 而不是 `Box<Counter<C>>`？

**答案**：`Counter` 的所有权是「多个句柄共享」的，不属于任何一个 `Sender`。若用 `Box`，每个句柄 drop 时都会试图释放堆内存，造成多次释放。`NonNull` 是非拥有的裸指针，把释放责任集中交给 `release` 里基于计数的手动逻辑。

**练习 3**：channel.rs 里写 `chan.send(msg, None)`，`chan` 的类型是 `&counter::Sender<...>`，`send` 方法定义在哪？

**答案**：`counter::Sender` 自己没有 `send` 方法；它通过 `Deref<Target = C>` 解引用到内部 flavor 通道（如 `array::Channel`），`send` 定义在那个 flavor 上。

### 4.2 acquire 与 Clone：克隆如何加计数、溢出如何 abort

#### 4.2.1 概念说明

clone 一个 `Sender`/`Receiver` 在底层就是「复制一个 `NonNull` 指针 + 把对应计数 +1」，这一步在 counter.rs 里叫 `acquire`。

有两点值得专门讲：

1. **内存序用 `Relaxed`**。`fetch_add(1, Relaxed)` 只保证计数本身的原子性，不做额外内存同步。这是安全的，因为 clone 一个句柄本身不读写通道里的消息；真正的消息收发由各 flavor 内部更重的同步机制负责。
2. **溢出直接 `process::abort()`**。理论上反复 clone 再 `mem::forget` 可以让计数无限增长直至溢出回绕（wrap around）。一旦回绕，计数逻辑就会出错（可能提前归零、误释放）。counter.rs 选择的策略是：当计数大到超过 `isize::MAX`（在 64 位平台上为 \(2^{63}-1\)）时，直接 abort 整个进程，而不是尝试优雅恢复。

#### 4.2.2 核心流程

```text
Sender::clone()                              // channel.rs
   └─ counter::Sender::acquire()              // counter.rs
         ├─ senders.fetch_add(1, Relaxed)     // 原子 +1，返回加之前的旧值 count
         ├─ if count > isize::MAX: abort()    // 溢出防护
         └─ 返回 Sender { counter: 同一指针 }  // 复制指针，不新建堆对象
```

注意 `fetch_add` 返回的是**加 1 之前**的值，所以判断用的是「加之前的 count」是否已经超过 `isize::MAX`，给真实计数值留出一个安全裕度。

#### 4.2.3 源码精读

看 `Sender::acquire`（src/counter.rs 第 51–64 行）：

[src/counter.rs:51-64](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L51-L64) —— `fetch_add(1, Relaxed)` 拿到旧值 `count`；若 `count > isize::MAX as usize` 则 `process::abort()`；否则返回一个新 `Sender`，复用同一个 `counter` 指针。注释解释：clone 之后对克隆调用 `mem::forget` 可能导致计数溢出，这种病态场景很难优雅恢复，干脆在计数极大时终止进程。

`Receiver::acquire` 完全对称，操作 `receivers`（src/counter.rs 第 110–123 行）：

[src/counter.rs:110-123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L110-L123) —— 与 `Sender::acquire` 结构一致，只是改操作 `receivers` 计数。

然后看壳层的 `Clone` 实现，它们只是把对 flavor 的枚举分发到 `acquire`。`Clone for Sender`（src/channel.rs 第 686–696 行）：

[src/channel.rs:686-696](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L686-L696) —— 对每个 flavor 变体调用 `chan.acquire()`，把得到的计数化句柄重新包回 `SenderFlavor`。

`Clone for Receiver`（src/channel.rs 第 1199–1212 行）稍复杂，因为 receiver 有六个变体：

[src/channel.rs:1199-1212](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1199-L1212) —— array/list/zero 走 `chan.acquire()`；at/tick 走 `Arc::clone`（不经 counter）；never 直接造一个新的零大小 `Channel::new()`。这里能再次看到 `counter.rs` 的边界：它只服务 array/list/zero 三种「真实」通道的 clone。

#### 4.2.4 代码实践

**实践目标**：跟踪一个 bounded 通道在多次 clone 后 `senders` 计数的变化。

操作步骤：

1. 假设 `let (s, r) = bounded::<i32>(1);`，此时 `senders = 1`、`receivers = 1`。
2. 执行 `let s1 = s.clone(); let s2 = s.clone();`，每次 `Clone for Sender` 都调一次 `counter::Sender::acquire`，于是 `senders` 变为 2、再到 3。
3. 此刻有 3 个 sender 句柄（`s`、`s1`、`s2`）指向同一 `Counter`。

需要观察的现象：

- clone 不创建新的 `Counter` 堆对象，三个句柄两两 `same_channel` 为 `true`。
- `receivers` 始终为 1（没有 clone receiver）。

预期结果：`senders = 3`，`receivers = 1`，堆上只有一份 `Counter`。

（若想实证，可参考 tests/list.rs 的 `drops()` 测试思路，用一个 `AtomicUsize` 统计 `Drop` 次数来侧面印证生命周期，见 [tests/list.rs:383-417](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/tests/list.rs#L383-L417)。）

#### 4.2.5 小练习与答案

**练习 1**：`acquire` 为什么用 `Ordering::Relaxed` 就够了？

**答案**：clone 句柄只增加计数、复制指针，不读写通道消息，也不需要与其他线程的 clone 操作建立 happens-before 关系；计数本身的原子性已由 `fetch_add` 保证。通道数据的可见性由各 flavor 内部更重的同步（如 stamp 的 Acquire/Release）负责。

**练习 2**：如果把溢出判断改成 `count == usize::MAX` 会有什么问题？

**答案**：`fetch_add` 返回旧值，真正的计数值是 `count + 1`；而且多线程并发 acquire 时计数可能跳变。用 `isize::MAX` 作为阈值留出巨大裕度，确保在真正溢出回绕之前就 abort，避免任何线程看到「回绕后变小」的错误计数。注释也说明这是「count becomes very large」就终止，属于保守防御。

**练习 3**：`Receiver::clone` 对 `never` 变体为什么是 `Channel::new()` 而不是 `acquire`？

**答案**：`never` 通道是零大小、永不投递、永不 disconnect 的特殊通道，不经过 `counter.rs`，没有共享的 `Counter` 对象可以计数。每个 never 句柄互相独立（`same_channel` 对任意两个 never 都返回 `true`，但底层不是共享计数）。

### 4.3 release 与 Drop：断开回调与「谁负责释放堆」

#### 4.3.1 概念说明

drop 一个句柄在底层是 `release`，它是 counter.rs 最精妙的部分。`release` 要做三件事，且只在「本侧计数归零」时做：

1. **调用 disconnect 回调**：通知 flavor「本侧全部阵亡了」。比如最后一个 sender drop 时调用 `disconnect_senders()`，最后一个 receiver drop 时调用 `disconnect_receivers()`（zero flavor 两端共用 `disconnect()`）。这个回调会唤醒对端阻塞的操作、设置断开标志、必要时丢弃剩余消息。
2. **决定是否释放 `Counter` 堆内存**：用 `destroy.swap(true, AcqRel)` 协调——只有「整体最后一个句柄」才真正 `Box::from_raw` 释放。
3. **保证释放恰好一次**：这是用 `destroy.swap` 而非 `load`+`store` 的根本原因。

为什么需要 `destroy` 标志？因为发送端和接收端是**两个独立的计数**。假设通道有 3 个 sender 和 2 个 receiver：

- 最后一个 sender drop（senders 1→0）时，它知道「发送端没了」，但 receiver 可能还存在（还要收走缓冲区剩余消息），所以**不能**释放堆内存。
- 最后一个 receiver drop（receivers 1→0）时，发送端可能也已经没了，这时才是真正释放的时机。

问题在于：「最后一个 sender」和「最后一个 receiver」是两次独立的、可能**并发**的 release 调用，它们各自只能看到自己一侧的计数归零，谁都不知道对方是否已经走完。`destroy` 标志就是两者之间的「交接棒」：第一个走到释放判断的人把 `destroy` 设成 `true` 但不释放；第二个走到的人发现 `destroy` 已经是 `true`，于是真正释放。

#### 4.3.2 核心流程

```text
drop(sender)                                       // channel.rs
   └─ counter::Sender::release(|c| c.disconnect_senders())   // counter.rs
         ├─ if senders.fetch_sub(1, AcqRel) == 1:  // 返回减之前的值；==1 表示本侧最后一个
         │     ├─ disconnect_senders(&chan)        // 唤醒 receiver、设断开标志
         │     └─ if destroy.swap(true, AcqRel):   // 返回旧值
         │           └─ Box::from_raw → drop       // 整体最后引用 → 释放堆
         └─ 否则：只是计数 -1，什么都不做
```

`destroy.swap(true)` 的语义关键在于「返回旧值」：

| 执行场景 | `destroy` 旧值 | `swap` 返回 | 是否释放 |
|----------|---------------|-------------|---------|
| 本侧最后一个、对侧还有人 | `false` | `false` | 否（置为 `true` 后离开） |
| 本侧最后一个、对侧已走完 | `true` | `true` | **是**（这次释放） |

所以释放永远只发生一次：发生在「两侧都已走完、并且是第二个走完的那一侧」。

为什么必须用 `swap` 而不是「先 `load` 判断再 `store`」？因为 `load` 和 `store` 是两步，中间有窗口；若「最后一个 sender」和「最后一个 receiver」并发执行，两者可能都 `load` 到 `false`、然后各自决定，导致**漏释放（内存泄漏）或重复释放（double free）**。`swap` 是一个原子的「读改写」，保证两个并发者中恰好有一个拿到旧值 `true`。

#### 4.3.3 源码精读

看 `Sender::release`（src/counter.rs 第 69–77 行）：

[src/counter.rs:69-77](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L69-L77) —— `fetch_sub(1, AcqRel)` 返回减之前的值；若等于 1（即本次把它减到 0，是最后一个 sender）：先调用传入的 `disconnect` 回调，再用 `destroy.swap(true, AcqRel)` 决定是否 `Box::from_raw` 释放堆。

几个细节：

- `release` 是 `unsafe fn`。调用方（channel.rs 的 `Drop`）必须保证本次 drop 的句柄确实有效、且不重复 release。counter.rs 通过「计数归零才释放」维护「指针有效 iff 计数 > 0」的不变量。
- `disconnect` 回调签名是 `FnOnce(&C) -> bool`，但**返回值被丢弃**（代码里直接调用，没有绑定）。这个 `bool` 表示「本次是否首次设置断开标志」，flavor 内部有用，但 counter 层不关心——它只负责「到 0 就回调、再判断销毁」。
- `fetch_sub` 用 `AcqRel`：Acquire 让最后一个 release 看到此前所有线程对通道的写入；Release 让自身的断开/销毁动作对后续可见。深度分析见 u3-l4。
- `destroy.swap` 同样用 `AcqRel`，确保「设 `true`」与「看到 `true` 后释放」之间建立正确的 happens-before，第二个 release 看到第一个 release 的全部断开工作后再释放。

`Receiver::release` 完全对称（src/counter.rs 第 128–136 行）：

[src/counter.rs:128-136](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L128-L136) —— 操作 `receivers`，结构一致。

再看壳层的 `Drop`。`Drop for Sender`（src/channel.rs 第 674–684 行）：

[src/channel.rs:674-684](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L674-L684) —— 按 flavor 分发：array/list 传 `|c| c.disconnect_senders()`，zero 传 `|c| c.disconnect()`。闭包经 Deref 拿到内部 flavor，调对应的断开方法。

`Drop for Receiver`（src/channel.rs 第 1184–1197 行）：

[src/channel.rs:1184-1197](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1184-L1197) —— array/list 传 `disconnect_receivers`，zero 传 `disconnect`；at/tick/never 是空 match 分支（`{}`），因为它们用 `Arc` 或零大小类型，drop 时由 Arc 的引用计数或栈语义自动处理，不走 counter.rs。

最后看 flavor 里这些 disconnect 回调到底做了什么。array 的 `disconnect_senders`（src/flavors/array.rs 第 487–496 行）：

[src/flavors/array.rs:487-496](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L487-L496) —— 用 `tail.fetch_or(mark_bit, SeqCst)` 原子地打上「断开」标记；若此前没打过（首次），就 `receivers.disconnect()` 唤醒所有阻塞的 receiver，返回 `true`；否则返回 `false`。

array 的 `disconnect_receivers`（src/flavors/array.rs 第 506–517 行）类似，额外调用 `discard_all_messages(tail)` 丢弃缓冲区剩余消息（接收端都走了，消息留着也没用）：

[src/flavors/array.rs:506-517](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L506-L517) —— 打 `mark_bit`、首次则唤醒所有阻塞 sender、并丢弃所有未接收消息。

list 的两个 disconnect（src/flavors/list.rs 第 561–570 行、第 575–586 行）结构相同，用 `MARK_BIT` 标记；`disconnect_receivers` 里同样 `discard_all_messages()` 急切释放内存：

[src/flavors/list.rs:561-570](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L561-L570) —— list 的 `disconnect_senders`。

[src/flavors/list.rs:575-586](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L575-L586) —— list 的 `disconnect_receivers`，接收端先走时急切丢弃所有消息。

zero 的 `disconnect`（src/flavors/zero.rs 第 353–364 行）两端共用，加锁后设 `is_disconnected` 并同时唤醒 senders 和 receivers：

[src/flavors/zero.rs:353-364](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L353-L364) —— zero flavor 单一的 disconnect，唤醒两侧。

可以看到清晰的分工：counter.rs 的 `release` 负责「到 0 就回调 + 决定销毁」，而 flavor 的 disconnect 负责「唤醒对端 + 设断开标志 + 丢消息」。

#### 4.3.4 代码实践

**实践目标**：跟踪一个「先 clone 再全部 drop」的过程，回答：`destroy` 标志为何用 swap？谁释放了堆？

操作步骤（源码阅读型，承接 4.2.4 的 clone 结果）：

1. 起点：`bounded::<i32>(1)`，`senders=1, receivers=1, destroy=false`。
2. `s.clone()`×2 后：`senders=3, receivers=1, destroy=false`（见 4.2.4）。
3. 现在 drop 三个 sender 中的前两个：每次 `release` 里 `fetch_sub` 返回 3、2（都不等于 1），仅计数 -1，不进 if。状态：`senders=1, receivers=1`。
4. drop 最后一个 sender：`fetch_sub` 返回 1，进入 if：
   - 调 `disconnect_senders()`——此时还有 1 个 receiver，它会被唤醒，此后 `recv` 在排空缓冲后会得到 disconnected。
   - `destroy.swap(true)` 返回旧值 **false**（receiver 还在），所以**不释放**堆，只把 `destroy` 置 `true`。状态：`senders=0, receivers=1, destroy=true`。
5. drop 这个 receiver：`fetch_sub` 返回 1，进入 if：
   - 调 `disconnect_receivers()`（此时 sender 已无，主要做收尾）。
   - `destroy.swap(true)` 返回旧值 **true**（sender 侧已置位），所以**这次释放**堆：`Box::from_raw(counter.as_ptr())` drop 掉整个 `Counter<C>`。

需要观察的现象与解释：

- **为什么用 swap 而非直接判断**：第 4 步和第 5 步是两次独立的 release，分别只看 senders / receivers 自己的计数。如果改用「`if destroy.load() { 释放 } else { destroy.store(true) }`」，在第 4、5 步并发执行时会出现竞态：两者都 `load` 到 `false`，于是都不进入释放分支，然后都 `store true`——结果是**堆永远不会被释放（泄漏）**。`destroy.swap(true)` 是原子读改写，保证两步中**恰好有一次**返回 `true`，也就是恰好一次释放。
- **谁释放了堆**：本例中是「最后一个 receiver」。如果反过来 receiver 先全走完、sender 后走完，则是「最后一个 sender」释放。结论：**释放由两侧中较晚走完的那一侧负责**。

预期结果：整个过程中堆内存恰好被释放一次，由后走完的一侧在 `destroy.swap` 返回 `true` 时执行。

（若要实证，可参考 tests/list.rs 的 `drops()` 测试：它在多线程并发 send/recv 后，用 `Rc` 的 weak 计数 + 全局 `DROPS` 计数器验证所有消息最终都被释放、没有泄漏——这间接验证了 release 的销毁逻辑正确：[tests/list.rs:383-417](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/tests/list.rs#L383-L417)。）

#### 4.3.5 小练习与答案

**练习 1**：把 `destroy.swap(true, AcqRel)` 改成 `if destroy.load(Acquire) { 释放 }; destroy.store(true, Release);` 会有什么并发 bug？

**答案**：`load` 和 `store` 是两步操作。假设最后一个 sender 和最后一个 receiver 并发 release：两者可能都 `load` 到 `false`，于是都不进入释放分支，然后都 `store true`——结果是**堆内存永远不会被释放（泄漏）**。反之若顺序写成「先 `store true` 再 `load`」，两者可能都 `load` 到 `true`（对方刚 `store` 的）而**都释放（double free）**。`swap` 把「读旧值 + 写 `true`」合成一个原子操作，从根上消除这个竞态，保证恰好一次释放。

**练习 2**：为什么「最后一个 sender」drop 时通常不释放堆，即使它进了 if 分支？

**答案**：因为 `receivers` 计数可能还大于 0（receiver 还在）。释放堆必须等到两侧计数都归零。`destroy` 标志的作用就是让先走完的一侧「打个招呼（置 `true`）但不释放」，后走完的一侧「看到招呼（swap 返回 `true`）才释放」。本侧计数归零只说明本侧完了，不足以决定整体能否释放。

**练习 3**：`release` 里的 `disconnect` 回调返回 `bool`，但 counter 层丢弃了这个返回值。这个 `bool` 在 flavor 里表示什么？

**答案**：表示「本次断开是否是首次」。例如 array 的 `disconnect_senders` 用 `fetch_or(mark_bit)`：若旧值没有 `mark_bit`（首次断开），就唤醒对端并返回 `true`；否则返回 `false`。这能避免重复唤醒。counter 层不关心这个值——它无条件在计数归零时回调，是否首次由 flavor 自己判断。

## 5. 综合实践

把 4.1–4.3 串起来，完成下面这个「计数全生命周期跟踪」任务。

**场景**：阅读（或人工跟踪）下面这段程序，画出每一步 `senders`/`receivers`/`destroy` 的取值，并标出在哪一步发生 disconnect 回调、哪一步释放堆内存。

```rust
// 示例代码：用于跟踪计数变化，不依赖运行
use crossbeam_channel::bounded;

let (s, r) = bounded::<i32>(4);     // 步骤 A
let s2 = s.clone();                 // 步骤 B
let r2 = r.clone();                 // 步骤 C
s.send(1).unwrap();                 // 步骤 D（与计数无关）
drop(s);                            // 步骤 E
drop(s2);                           // 步骤 F
drop(r);                            // 步骤 G
drop(r2);                           // 步骤 H
```

要求：

1. 列出 A–H 每步后三个字段（`senders`、`receivers`、`destroy`）的值。
2. 指出哪一步触发了 `disconnect_senders`、哪一步触发了 `disconnect_receivers`。
3. 指出哪一步执行了 `Box::from_raw` 释放堆，并用 `destroy.swap` 的返回值说明原因。
4. 说明如果交换 F 和 G 的顺序（先 drop `r` 再 drop 最后一个 sender），释放堆的「责任人」是否会改变。

参考答案要点：

- A：`senders=1, receivers=1, destroy=false`。
- B：`senders=2`。C：`receivers=2`。D：计数不变。
- E（drop s）：`fetch_sub` 返回 2 ≠ 1，仅 `senders=1`，不进 if。
- F（drop s2）：`fetch_sub` 返回 1，进 if → 调 `disconnect_senders()`（首次，唤醒 receiver）→ `destroy.swap(true)` 返回 **false**（receiver 还在）→ 不释放。状态 `senders=0, receivers=2, destroy=true`。
- G（drop r）：`fetch_sub` 返回 2 ≠ 1，`receivers=1`，不进 if。
- H（drop r2）：`fetch_sub` 返回 1，进 if → 调 `disconnect_receivers()` → `destroy.swap(true)` 返回 **true**（sender 已置位）→ **释放堆**。
- 交换 F、G：先 drop r（receivers 2→1，不进 if），再 drop s2（senders 1→0，进 if，`disconnect_senders`，`destroy.swap` 返回 false，不释放），再 drop r2（receivers 1→0，进 if，`disconnect_receivers`，`destroy.swap` 返回 true，释放）。责任人仍是「后走完的一侧（receiver `r2`）」。无论顺序如何，释放永远由「两侧中最后 drop 的那个句柄」负责。

## 6. 本讲小结

- crossbeam-channel 不用通用 `Arc`，而是在 `src/counter.rs` 里实现了一个「分发送端/接收端计数」的迷你引用计数：堆上的 `Counter<C>` 同时记录 `senders`、`receivers` 和一个 `destroy` 标志。
- `Sender<C>`/`Receiver<C>` 只持有一个 `NonNull<Counter<C>>` 指针，极轻量；通过 `Deref<Target = C>` 透明访问内部 flavor，所以壳层写 `chan.send(...)` 实际落到 flavor 实现上。
- `acquire`（clone）用 `fetch_add(1, Relaxed)` 加计数，并在计数接近 `isize::MAX`（\(2^{63}-1\)）时 `process::abort()` 防止溢出回绕。
- `release`（drop）只在「本侧计数归零」时执行：先调 disconnect 回调（唤醒对端、设断开标志、必要时丢消息），再用 `destroy.swap(true, AcqRel)` 决定是否释放堆。
- `destroy` 标志是「最后一个 sender」和「最后一个 receiver」之间的交接棒：先走完的一侧置 `true` 但不释放，后走完的一侧看到 `true` 才释放；用原子 `swap`（而非 `load`+`store`）保证堆内存**恰好释放一次**。
- `counter.rs` 只服务 array/list/zero 三种「真实」通道；at/tick/never 用 `Arc` 或零大小类型，不走这套计数。

## 7. 下一步学习建议

- 本讲聚焦「计数与生命周期」本身，但 `release` 里 `disconnect_senders`/`disconnect_receivers` 真正唤醒了谁、怎么唤醒，要结合 u2-l4（context.rs + waker.rs）的阻塞唤醒机制才能完整理解。
- `fetch_sub`/`swap` 为什么用 `AcqRel`、计数归零的 happens-before 是怎么建立的，属于内存序正确性，集中在 u3-l4 深入分析。
- 三种 flavor 各自 disconnect 内部的细节（array 的 `mark_bit`、list 的 `MARK_BIT` + 急切丢消息、zero 的加锁唤醒）会在 u2-l5/u2-l6/u2-l7 分别精读。
- 想看「计数与销毁」在并发下的实证，可读 tests/list.rs 的 `drops()`、tests/array.rs 的 `drop_unreceived()` 等测试。
