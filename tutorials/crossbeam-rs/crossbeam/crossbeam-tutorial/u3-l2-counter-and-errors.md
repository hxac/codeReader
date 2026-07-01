# counter 引用计数与 err 错误类型

## 1. 本讲目标

本讲紧接 u3-l1（channel 总览与基本使用）。在上一讲里，我们只看了 `channel.rs` 的公共方法外壳——`unbounded()` / `bounded()` 造出通道，`send` / `recv` 走 `match flavor` 派发。但我们刻意回避了两个底层问题：

1. **多个 `Sender` / `Receiver` 共享同一个通道，谁来释放它？** 你可以 `s.clone()` 出任意多个发送端，分别 `move` 到不同线程；当它们一个个 `drop` 时，通道这块堆内存到底在哪一刻、由谁来回收？
2. **当通道“断了”，`send` / `recv` / `try_send` / `recv_timeout` 各自返回什么错误？** 为什么有的错误带回原消息、有的不带？

学完本讲，你应当能够：

- 理解 `Sender` / `Receiver` 共享的 `Counter` 结构，看清 crossbeam-channel 是如何**手动**实现引用计数（而非用 `Arc`）的；
- 掌握 `acquire` / `release` 的增减计数逻辑，以及 `destroy` 标志如何保证「整条通道恰好被销毁一次」；
- 认识 `err.rs` 中发送侧、接收侧、select 侧三大类错误枚举，能说出每种错误分别在什么条件下产生、为什么发送错误要带回原消息。

---

## 2. 前置知识

在进入源码前，先厘清几个本讲会用到的概念。

- **引用计数（reference counting, RC）**：一块内存被多个所有者共享时，用一个整数记录“当前有几个所有者”；每多一个所有者计数 `+1`（clone），每离开一个计数 `-1`（drop），计数归零时释放内存。Rust 标准库的 `Arc` 就是线程安全的引用计数。
- **RAII 与 `Drop`**：Rust 通过 `Drop` trait 在值离开作用域时自动执行清理。crossbeam-channel 正是靠给 `Sender` / `Receiver` 实现 `Clone`（计数 `+1`）和 `Drop`（计数 `-1`）来管理通道生命周期的。
- **`AtomicUsize` 与 `Ordering`**：在多线程下增减计数必须用原子操作。本讲会遇到三种内存序：`Relaxed`（只保证自身原子，不与其它读写排序）、`AcqRel`（既 Acquire 又 Release）、`SeqCst`（全局顺序一致）。我们会在用到处解释为什么这样选。
- **`Box::leak` / `Box::from_raw` / `NonNull`**：标准 `Box` 在 drop 时会自动释放堆内存；`Box::leak` 故意“泄漏”它、交出一个 `&mut T`，使这块内存**不再自动释放**，必须由人手动 `Box::from_raw` 回收。`NonNull<T>` 是「保证非空」的裸指针包装。这三者组合起来，就是 crossbeam-channel 手写引用计数的物理基础。
- **MPMC 通道（承接 u3-l1）**：crossbeam-channel 是多生产者多消费者模型，`Sender` 和 `Receiver` 都可以 `clone()`，因此一条通道的两端各自都可能被多线程共享。

---

## 3. 本讲源码地图

本讲主要精读两个文件，并借助第三个文件看清它们如何被接线：

| 文件 | 作用 |
| --- | --- |
| [`crossbeam-channel/src/counter.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs) | 通道的**引用计数内核**：`Counter<C>` 结构、`new` 构造、`Sender<C>` / `Receiver<C>` 句柄及其 `acquire` / `release`。 |
| [`crossbeam-channel/src/err.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs) | 全部**错误类型**的定义：发送侧 / 接收侧 / select 侧共 10 个错误类型，及其 `Debug` / `Display` / `Error` / `From` 实现。 |
| [`crossbeam-channel/src/channel.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs) | 公共 API 外壳，本讲只看其中 `counter::new` 的调用点，以及 `Sender` / `Receiver` 的 `Clone` / `Drop` 如何把 `acquire` / `release` 接到 `counter.rs` 上。 |

> 提示：`counter.rs` 里的 `Counter` / `Sender` / `Receiver` 都是 `pub(crate)` 的内部类型，**不要**把它们和 `channel.rs` 里对外公开的 `Sender<T>` / `Receiver<T>` 搞混。后者（公开的）把前者（内部的计数句柄）包了一层 `flavor` 枚举。本讲的 `Sender` / `Receiver` 若无特别说明，指 `counter.rs` 的内部句柄。

---

## 4. 核心概念与源码讲解

### 4.1 Counter：通道的共享核心与手动引用计数

#### 4.1.1 概念说明

一条通道在堆上只有**一份**真实状态（缓冲区、阻塞队列、断开标志等），但它的发送端和接收端都可能被 `clone` 出很多份，散落在不同线程。于是天然存在「共享所有权」问题：这块内存归谁管？什么时候释放？

一个自然的想法是用 `Arc`。但 crossbeam-channel **没有**用 `Arc`，而是手写了一套引用计数，原因有二：

1. **通道的销毁语义比普通 `Arc` 复杂**：它必须做到「整条通道恰好被销毁一次」，而且回收动作由「最后一个发送端」或「最后一个接收端」中**后离开的那一方**完成（见 4.2）。手写计数可以把这个两阶段销毁协议和断开通知（disconnect）紧耦合在一起。
2. **性能与布局**：手写计数把 `senders` / `receivers` 两个计数和通道状态 `chan` 放进**同一个**分配里，避免 `Arc` 套 `Arc` 的多层间接。

这套机制的“主角”就是 `Counter<C>`：`C` 是具体 flavor 的通道类型（如 `flavors::list::Channel<T>`）。所有 clone 出来的发送端、接收端，手里拿的都只是**指向同一份 `Counter` 的裸指针**。

#### 4.1.2 核心流程

一条通道从创建到销毁的引用计数流程大致是：

1. **构造**：`counter::new(chan)` 在堆上分配一个 `Counter`，`senders = 1`、`receivers = 1`，返回一对 `Sender` / `Receiver`，两者持有**同一个** `Counter` 指针。
2. **克隆**（`acquire`）：`Sender::clone()` → 计数 `senders += 1`，产出一个新句柄指向同一份 `Counter`；接收端同理。
3. **释放**（`release`）：某个句柄 `drop` → 对应计数 `-= 1`；若它恰好是**最后一个**（计数从 1 变 0），则触发该侧的 `disconnect` 通知（唤醒对端），并参与“销毁通道”的裁决。
4. **销毁**：发送侧、接收侧各有一次“最后一次 drop”。`destroy` 标志确保两次中**恰好一次**真正 `Box::from_raw` 回收堆内存。

其状态演进可用下面这张简表概括（以发送端为例，接收端对称）：

| 事件 | `senders` 变化 | 是否最后一个 | 动作 |
| --- | --- | --- | --- |
| `new` | 设为 1 | — | 堆分配 `Counter` |
| `clone` (acquire) | `+1` | 否 | 复制句柄 |
| `drop` (release) | `-1` | 旧值 > 1：否 | 仅减计数 |
| `drop` (release) | `-1` | 旧值 == 1：是 | `disconnect` + 参与 `destroy` 裁决 |

#### 4.1.3 源码精读

先看 `Counter` 的四个字段：

```rust
struct Counter<C> {
    senders: AtomicUsize,   // 当前发送端引用数
    receivers: AtomicUsize, // 当前接收端引用数
    destroy: AtomicBool,    // 销毁裁决标志
    chan: C,                // 具体 flavor 的通道状态
}
```

参见 [counter.rs:12-24](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L12-L24)：这段定义了引用计数的全部状态——`senders` / `receivers` 分别是两端的计数，`destroy` 是销毁仲裁位（4.2 详解），`chan` 内联存放真正的通道（按 flavor 不同而不同）。

再看构造函数 `new`：

```rust
pub(crate) fn new<C>(chan: C) -> (Sender<C>, Receiver<C>) {
    let counter = NonNull::from(Box::leak(Box::new(Counter {
        senders: AtomicUsize::new(1),
        receivers: AtomicUsize::new(1),
        destroy: AtomicBool::new(false),
        chan,
    })));
    let s = Sender { counter };
    let r = Receiver { counter };
    (s, r)
}
```

参见 [counter.rs:27-37](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L27-L37)：这里有三个关键设计——

- **`Box::leak`**：故意“泄漏” `Box`，使其**不会**在离开作用域时自动释放。这是手写引用计数的核心套路：内存的回收责任从编译器手里转移到了 `release` 里的 `Box::from_raw`。
- **`NonNull::from`**：把泄漏得到的引用转成一个保证非空的裸指针；发送端和接收端拿到的是**同一个** `counter`，这就是“共享”的物理体现。
- 两个计数都初始化为 `1`，对应这一对原始的 `Sender` / `Receiver`。

`Sender` / `Receiver` 本身非常薄，只持有一个 `NonNull<Counter<C>>`：

```rust
pub(crate) struct Sender<C> {
    counter: NonNull<Counter<C>>,
}
```

参见 [counter.rs:39-42](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L39-L42)（`Receiver` 结构对称，见 [counter.rs:99-101](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L99-L101)）。句柄不持有任何额外数据，clone / drop 只是增减共享 `Counter` 里的整数。

为了让句柄用起来像「直接是通道本身」，二者都实现了 `Deref`，把方法调用转交给内层的 `chan`：

```rust
impl<C> ops::Deref for Sender<C> {
    type Target = C;
    fn deref(&self) -> &C {
        &self.counter().chan
    }
}
```

参见 [counter.rs:84-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L84-L90)：因此 `channel.rs` 里的 flavor 代码可以拿到 `&C`，直接调用具体通道（如 `list::Channel`）的方法，而无需关心计数。此外 `PartialEq`（[counter.rs:92-96](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L92-L96)）按指针比较，这就是公开 API `same_channel` 判断“两个端是否属于同一通道”的依据。

最后看一眼这层内部类型是如何被公开 API 造出来的——以 `unbounded` 为例：

```rust
pub fn unbounded<T>() -> (Sender<T>, Receiver<T>) {
    let (s, r) = counter::new(flavors::list::Channel::new());
    let s = Sender { flavor: SenderFlavor::List(s) };
    let r = Receiver { flavor: ReceiverFlavor::List(r) };
    (s, r)
}
```

参见 [channel.rs:50-59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L50-L59)：`counter::new` 拿到的是 `counter::Sender<flavors::list::Channel<T>>`，随后被包进 `SenderFlavor::List(..)` 成为对外的 `Sender<T>`。`bounded` 的两条分支同理（[channel.rs:113-133](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L113-L133)），只是 `chan` 换成了 `array` 或 `zero` flavor。

#### 4.1.4 代码实践

**实践目标**：亲手验证“clone 出来的多个端共享同一份通道”，并理解为什么 `Box::leak` 不会造成真正的泄漏。

**操作步骤**：

1. 阅读上面的 `new` 实现，确认 `Sender` 与 `Receiver` 持有同一个 `NonNull<Counter<C>>`。
2. 在仓库的 `crossbeam-channel/` 下新建一个临时示例（**示例代码，非项目原有文件**，可放在 `examples/` 或独立的小 crate）：

```rust
// 示例代码：验证多个端共享同一通道
use crossbeam_channel::unbounded;

fn main() {
    let (s, r) = unbounded::<i32>();
    let s2 = s.clone();
    let r2 = r.clone();

    // clone 出来的端与原始端属于同一条通道
    assert!(s.same_channel(&s2));
    assert!(r.same_channel(&r2));
    // 发送端与接收端虽然职责不同，但底层是同一通道
    assert!(s.same_channel(/* 跨端比较语义上同通道；same_channel 只在同侧比较指针 */ &s2));

    // 通过其中任一端发送，所有接收端都能收到
    s2.send(42).unwrap();
    assert_eq!(r2.recv(), Ok(42));
}
```

3. 运行（待本地验证）：`cargo run --example <名字>`。

**需要观察的现象**：`same_channel` 在 clone 之间返回 `true`；通过 `s2` 发送的消息能被 `r2` 收到——这说明所有端背后是同一份 `Counter` / `chan`。

**预期结果**：断言全部通过。`same_channel` 之所以能这样判断，正是因为它最终比的是 `counter` 裸指针（见 4.1.3 的 `PartialEq`）。

> 关于 `Box::leak` 是否泄漏：只要你在程序结束时把所有 `Sender` / `Receiver` 都 `drop` 掉，最后一个 drop 会在 `release` 里调用 `Box::from_raw` 回收内存。只有当你用 `mem::forget` 故意丢弃句柄时才会真正泄漏——而 `acquire` 里的溢出保护（见 4.2）正是为这种病态场景兜底。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Sender` / `Receiver` 用 `NonNull<Counter<C>>` 而不是直接存 `Box<Counter<C>>` 或 `Arc<Counter<C>>`？

> **参考答案**：用 `Box` 会在每个句柄 drop 时尝试释放，导致多次释放；用 `Arc` 可以自动管理，但通道需要更精细的“两端各自计数 + 恰好一次销毁”语义，且希望把两个计数和通道状态放进同一块内存。`NonNull` 裸指针让所有句柄共享同一份堆内存，由手写的 `acquire` / `release` 统一管理计数与回收。

**练习 2**：`new` 里把 `senders` 和 `receivers` 都初始化为 `1`，这个 `1` 对应谁？

> **参考答案**：对应 `new` 返回的那一对**原始** `Sender` 和 `Receiver`。之后每次 `clone` 才会 `+1`。

---

### 4.2 acquire / release：增减计数与 destroy 唯一销毁协议

#### 4.2.1 概念说明

`Counter` 只是状态，真正的计数管理发生在两个方法上：`acquire`（克隆时增计数）和 `release`（销毁时减计数）。它们分别被公开 `Sender` / `Receiver` 的 `Clone` 和 `Drop` 实现调用。

这里有两个精妙之处需要看懂：

1. **`acquire` 用 `Relaxed`，`release` 用 `AcqRel`，为何不对称？**
   - `acquire` 只是给计数 `+1`，不“守门”任何清理动作，`Relaxed` 足矣；
   - `release` 负责检测“我是不是最后一个”并触发清理，必须用 `AcqRel`，确保“最后一个”的判定与 `disconnect`、销毁之间不会乱序。这正是标准库 `Arc::clone`（`Relaxed`）与 `Arc::drop`（`Acquire`）的同款取舍。

2. **`destroy` 标志如何保证「恰好销毁一次」？**
   - 一条通道有「最后一个发送端 drop」和「最后一个接收端 drop」两个时刻。这两个时刻可能由两个不同线程、以任意先后发生。
   - 谁都不许漏销毁（内存泄漏），也都不许重复销毁（double free）。
   - 解法：第一个到达销毁裁决的线程把 `destroy` 从 `false` 置 `true`，它**不**回收；第二个到达的线程读到 `true`，由它执行 `Box::from_raw`。于是「后到达者负责回收」，恰好一次。

此外，`release` 还承担一项副作用：当某侧最后一个引用消失，调用 flavor 的 `disconnect` 回调，把通道标记为断开并唤醒对端阻塞的操作——这正是 4.3 里各种 `Disconnected` 错误的物理来源。

#### 4.2.2 核心流程

以「发送端最后一个 drop」为例（接收端对称），`release` 的执行流程：

```
fetch_sub(1, AcqRel) 拿到旧值 old
├─ old != 1（还有别的发送端）→ 仅减计数，结束
└─ old == 1（我是最后一个发送端）
   ├─ 调用 disconnect 回调（如 disconnect_senders）
   │    └─ flavor 把通道标记为“发送侧已断开”，唤醒阻塞的接收端
   └─ destroy.swap(true, AcqRel) 得到 prev
       ├─ prev == false（我是两侧中第一个到裁决点的）→ 不回收，结束
       └─ prev == true（对端已经先到过裁决点）→ Box::from_raw 回收整条通道
```

需要特别留意：`disconnect` 回调虽然在签名上返回 `bool`（表示“是否本次新发生断开”），但 `release` **并不使用**这个返回值——它只是触发断开副作用。`bool` 返回值是 flavor 内部判断“要不要唤醒对端”用的，与计数销毁无关。

#### 4.2.3 源码精读

先看 `acquire`：

```rust
pub(crate) fn acquire(&self) -> Self {
    let count = self.counter().senders.fetch_add(1, Ordering::Relaxed);

    // 克隆后 mem::forget 可能导致计数溢出，难以优雅恢复，故超界直接 abort。
    if count > isize::MAX as usize {
        process::abort();
    }

    Self { counter: self.counter }
}
```

参见 [counter.rs:51-64](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L51-L64)（接收端 `acquire` 对称，见 [counter.rs:110-123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L110-L123)）。要点：

- `fetch_add(1, Relaxed)` 返回**旧值** `count`；新计数是 `count + 1`。
- **溢出保护**：若旧值已超过 `isize::MAX`，说明有人恶意 `clone` + `mem::forget` 灌水，直接 `process::abort()`，宁可崩溃也不冒“计数回绕 → 误判归零 → double free”的风险。这与标准库 `Arc` 的防御策略一致。

再看 `release`：

```rust
pub(crate) unsafe fn release<F: FnOnce(&C) -> bool>(&self, disconnect: F) {
    if self.counter().senders.fetch_sub(1, Ordering::AcqRel) == 1 {
        disconnect(&self.counter().chan);   // 触发断开；返回的 bool 在此被丢弃

        if self.counter().destroy.swap(true, Ordering::AcqRel) {
            drop(unsafe { Box::from_raw(self.counter.as_ptr()) }); // 后到达者回收
        }
    }
}
```

参见 [counter.rs:69-77](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L69-L77)（接收端 `release` 对称，见 [counter.rs:128-136](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L128-L136)）。逐行拆解：

- `fetch_sub(1, AcqRel) == 1`：旧值为 1，说明本次减完就归零——我是该侧最后一个引用。`AcqRel` 保证“最后一个”看到此前所有通过该通道发生的操作。
- `disconnect(&self.counter().chan)`：调用传入的断开回调；注意它返回 `bool` 但被直接丢弃（`;`）。
- `destroy.swap(true, AcqRel)`：原子地把 `destroy` 置 `true` 并返回**旧值**。旧值为 `true` 表示对端一侧已经先到过这里，于是由**本线程**执行 `Box::from_raw` 回收整条通道。

> 关于 `release` 为何标 `unsafe`：它涉及 `Box::from_raw` 这种手动内存回收，且依赖“每个句柄在其生命周期内恰好 release 一次、计数始终正确”这一不变量。这些保证由调用方（公开 `Sender` / `Receiver` 的 `Drop`）在类型系统层面承担，故契约以 `unsafe` 标注、限定在 `pub(crate)` 范围内。

那么 `disconnect` 回调具体做了什么？看公开 `Sender` 的 `Drop` 如何接线：

```rust
impl<T> Drop for Sender<T> {
    fn drop(&mut self) {
        unsafe {
            match &self.flavor {
                SenderFlavor::Array(chan) => chan.release(|c| c.disconnect_senders()),
                SenderFlavor::List(chan)  => chan.release(|c| c.disconnect_senders()),
                SenderFlavor::Zero(chan)  => chan.release(|c| c.disconnect()),
            }
        }
    }
}
```

参见 [channel.rs:674-684](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L674-L684)。这里把闭包 `|c| c.disconnect_senders()` 作为 `disconnect` 回调传给 `release`；当 `release` 判定“我是最后一个发送端”时，才会调用它。`Receiver` 的 `Drop` 对称（[channel.rs:1184-1197](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1184-L1197)），传的是 `disconnect_receivers`。而 `Clone` 则调用 `acquire`（[channel.rs:686-696](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L686-L696)）。

`disconnect_senders` / `disconnect_receivers` 在 flavor 里做的事，以 `list` 为例：

```rust
pub(crate) fn disconnect_senders(&self) -> bool {
    let tail = self.tail.index.fetch_or(MARK_BIT, Ordering::SeqCst);
    if tail & MARK_BIT == 0 {
        self.receivers.disconnect();   // 唤醒所有阻塞的接收端
        true
    } else {
        false
    }
}
```

参见 [flavors/list.rs:561-570](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L561-L570)：它用 `fetch_or(MARK_BIT)` 原子地把“断开位”打上标记（且只让第一个这么做的人返回 `true`），并调用 `receivers.disconnect()` 唤醒所有正阻塞在 `recv` 上的线程——这些线程被唤醒后会看到通道已断开，从而返回 `RecvError` / `Disconnected`。`disconnect_receivers`（[flavors/list.rs:575-586](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L575-L586)）还会额外 `discard_all_messages()` 急切释放尚未消费的消息。array/zero flavor 的同名方法逻辑类似（array 见 [flavors/array.rs:487-516](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L487-L516)，zero 见 [flavors/zero.rs:353-364](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L353-L364)）。

把 4.1 和 4.2 串起来，整条销毁链路是：**公开 `Sender::drop` → `counter::Sender::release` → `fetch_sub` 命中 1 → `disconnect_senders`（唤醒对端）→ `destroy` 裁决 → 必要时 `Box::from_raw` 回收**。

#### 4.2.4 代码实践

**实践目标**：跨线程 clone 与 drop 多个发送端，观察“只有**最后一个**引用离开，通道才会断开”，从而间接验证 `senders` 计数何时归零。

**操作步骤**：编写如下示例（**示例代码，非项目原有文件**）：

```rust
// 示例代码：追踪 senders 计数归零 → 触发断开
use std::thread;
use crossbeam_channel::{unbounded, TryRecvError};

let (s, r) = unbounded::<i32>();

// clone 出第二份发送端，分别交给主线程和子线程
let s2 = s.clone();
let h = thread::spawn(move || {
    // 子线程 drop 掉 s2：此时主线程的 s 还在，senders 还有 1，通道未断开
    drop(s2);
});

drop(s);          // 主线程也 drop：现在 senders 归零，触发 disconnect_senders
h.join().unwrap();

// 观察：接收端现在能看到“断开”
assert_eq!(r.try_recv(), Err(TryRecvError::Disconnected));
```

运行（待本地验证）：`cargo run --example <名字>`。

**需要观察的现象**：

1. 若只 drop 其中一个发送端（另一个仍存活），`r.try_recv()` 返回 `Empty` 而非 `Disconnected`——通道还没断。
2. 当**两个**发送端都被 drop（`senders` 归零），`r.try_recv()` 才返回 `Disconnected`。

**预期结果**：断言通过。这正是 `release` 中 `fetch_sub(..) == 1` 守门的结果——只有把计数从 1 减到 0 的那一次 drop 才会调用 `disconnect_senders`，把断开信号传给接收端。

> 进阶观察：若想确认销毁确实发生且只发生一次，可以给消息类型换成带 `Drop` 实现的类型（在 `Drop` 里打印日志），观察通道销毁时缓冲中的消息是否被释放、是否每条只释放一次。注意 `disconnect_receivers` 在 list/array 上会主动 `discard_all_messages`，所以“接收端先全部离开”时消息会更早被释放。

#### 4.2.5 小练习与答案

**练习 1**：假设线程 A drop 最后一个发送端、线程 B drop 最后一个接收端，两者几乎同时到达 `destroy.swap(true, ..)`。谁负责 `Box::from_raw`？会不会 double free？

> **参考答案**：`swap` 是原子的，两线程中必有一个先拿到 `prev == false`（不回收）、另一个拿到 `prev == true`（执行回收）。因此**后到达者**负责回收，且因 `swap` 互斥，不会 double free，也不会漏销毁。

**练习 2**：为什么 `acquire` 用 `Relaxed`、`release` 用 `AcqRel`？

> **参考答案**：`acquire` 只是计数 `+1`，不充当任何清理的“闸门”，`Relaxed` 足够且最便宜；`release` 要判断“计数是否归零”并据此触发 `disconnect` 与销毁，必须用 `AcqRel` 保证归零判定、断开副作用与回收之间的可见性与顺序。这与 `Arc::clone` / `Arc::drop` 的取舍一致。

**练习 3**：`disconnect` 回调明明返回 `bool`，`release` 却没有用它。这个 `bool` 是给谁用的？

> **参考答案**：是 flavor 内部用的——它表示“本次是否**新发生**断开”（用 `MARK_BIT` 的 `fetch_or` 保证只有一次返回 `true`），flavor 据此决定是否要唤醒对端的阻塞操作。`release` 只关心“触发断开”这个副作用，不关心返回值，故直接丢弃。

---

### 4.3 err.rs：错误类型家族

#### 4.3.1 概念说明

通道操作会失败，失败的方式和**调用风格**强相关。crossbeam-channel 把失败模式拆成了三个维度：

1. **调用风格**：阻塞版（`send` / `recv`）、非阻塞版（`try_send` / `try_recv`）、限时版（`send_timeout` / `recv_timeout`）。
2. **失败原因**：满了（`Full`）、空了（`Empty`）、超时（`Timeout`）、断开（`Disconnected`）。
3. **是否携带数据**：发送失败必须**带回原消息**（否则消息凭空丢失）；接收失败没有“原消息”可带回。

由此推导出一个非常规整的“错误矩阵”：

- 阻塞的 `send` 永远不会因为“满”而失败（它会等），所以它的唯一失败原因是 `Disconnected`；同理阻塞的 `recv` 唯一失败也是 `Disconnected`。
- `try_*` 版本额外多出 `Full` / `Empty`（“此刻不能立即完成”）。
- `*_timeout` 版本额外多出 `Timeout`。
- 发送侧的每个错误都内嵌消息 `T`，并提供 `into_inner()` 取回；接收侧错误是无数据的单元 / 枚举。
- select 侧（u3-l9 详讲）有自己的四件套：`TrySelectError` / `SelectTimeoutError` / `TryReadyError` / `ReadyTimeoutError`。

#### 4.3.2 核心流程：错误一览表

| 公开方法（所在） | 返回的错误类型 | 失败变体 / 条件 |
| --- | --- | --- |
| `Sender::send` | `SendError<T>` | 仅 `Disconnected(T)`：所有接收端已断开 |
| `Sender::try_send` | `TrySendError<T>` | `Full(T)`：有界通道满（或零容量当下无接收端）；`Disconnected(T)` |
| `Sender::send_timeout` / `send_deadline` | `SendTimeoutError<T>` | `Timeout(T)`：超时未发出；`Disconnected(T)` |
| `Receiver::recv` | `RecvError` | 仅 `Disconnected`：通道为空且所有发送端已断开 |
| `Receiver::try_recv` | `TryRecvError` | `Empty`：通道为空（零容量则当下无发送端）；`Disconnected` |
| `Receiver::recv_timeout` / `recv_deadline` | `RecvTimeoutError` | `Timeout`：超时未收到；`Disconnected` |
| `Select::try_select` | `TrySelectError` | 所有操作此刻都会阻塞（无一就绪） |
| `Select::select_timeout` / `select_deadline` | `SelectTimeoutError` | 截止前无一操作就绪 |
| `Select::try_ready` | `TryReadyError` | 无一操作就绪 |
| `Select::ready_timeout` / `ready_deadline` | `ReadyTimeoutError` | 截止前无一操作就绪 |

记一条总规律：**“断开（Disconnected）”是所有数据通道操作的终极失败信号**，它由 4.2 的 `disconnect` 机制点亮——只要某一侧最后一个引用被 drop，对端的所有阻塞/非阻塞操作就能看到断开。

#### 4.3.3 源码精读

**发送侧三类错误**都内嵌消息 `T`。最基础的 `SendError` 是个元组结构体：

```rust
#[derive(PartialEq, Eq, Clone, Copy)]
pub struct SendError<T>(pub T);
```

参见 [err.rs:11-12](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L11-L12)。它的 `into_inner()`（[err.rs:147-149](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L147-L149)）取回这条没法发出的消息。注意它**没有** `derive(Debug)`，而是手写 `Debug` 打印 `"SendError(..)"`——因为 `T` 未必是 `Debug`：

```rust
impl<T> fmt::Debug for SendError<T> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        "SendError(..)".fmt(f)
    }
}
```

参见 [err.rs:118-122](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L118-L122)。这是所有“携带 `T`”错误的共同手法（`TrySendError` / `SendTimeoutError` 同理，见 [err.rs:152-159](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L152-L159) 与 [err.rs:212-216](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L212-L216)）。

`TrySendError` 是双变体枚举，覆盖“满”与“断开”：

```rust
pub enum TrySendError<T> {
    Full(T),
    Disconnected(T),
}
```

参见 [err.rs:19-29](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L19-L29)（注意 `Full` 的文档特别说明：对零容量通道，`Full` 表示“此刻没有接收端在场”）。它提供 `into_inner()`、`is_full()`、`is_disconnected()` 三个查询（[err.rs:180-209](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L180-L209)）。`SendTimeoutError` 结构对称，只是把 `Full` 换成 `Timeout`（[err.rs:36-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L36-L46)）。

**接收侧三类错误**不带数据，因此可以直接 `derive(Debug)`。`RecvError` 是单元结构体：

```rust
#[derive(PartialEq, Eq, Clone, Copy, Debug)]
pub struct RecvError;
```

参见 [err.rs:53-54](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L53-L54)。`TryRecvError` / `RecvTimeoutError` 分别是 `Empty | Disconnected` 与 `Timeout | Disconnected`（[err.rs:59-69](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L59-L69)、[err.rs:74-84](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L74-L84)）。

**`From` 转换：把“阻塞错误”嵌入“更宽的错误”**。这是错误体系里很关键的设计——底层 flavor 的 `send` 返回的是最宽的 `SendTimeoutError`，公开 `Sender::send` 把它收窄成 `SendError`：

```rust
impl<T> From<SendError<T>> for TrySendError<T> {
    fn from(err: SendError<T>) -> Self {
        match err {
            SendError(t) => Self::Disconnected(t),
        }
    }
}
```

参见 [err.rs:172-178](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L172-L178)（`SendError → SendTimeoutError` 见 [err.rs:229-235](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L229-L235)，`RecvError → TryRecvError` / `→ RecvTimeoutError` 见 [err.rs:289-295](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L289-L295) 与 [err.rs:320-326](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L320-L326)）。公开 `Sender::send` 正是用这条收窄链把 `SendTimeoutError::Disconnected` 映射为 `SendError`、并把不可能出现的 `Timeout` 标记为 `unreachable!`：

```rust
pub fn send(&self, msg: T) -> Result<(), SendError<T>> {
    match &self.flavor { /* …调用 flavor.send(msg, None)… */ }
        .map_err(|err| match err {
            SendTimeoutError::Disconnected(msg) => SendError(msg),
            SendTimeoutError::Timeout(_) => unreachable!(),
        })
}
```

参见 [channel.rs:446-456](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L446-L456)：因为 `send` 传了 `None`（无截止时间），flavor 永远不可能返回 `Timeout`，所以 `unreachable!()` 是安全的。接收端的 `recv` 同样把 `RecvTimeoutError` 收窄成 `RecvError`（[channel.rs:831-857](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L831-L857)）。

**select 侧四件套**都是无数据的单元结构体，定义在 [err.rs:86-116](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L86-L116)。它们的产生点在 `select.rs`，例如 `try_select` 在无一操作就绪时返回 `TrySelectError`：

```rust
pub fn try_select<'a>(
    handles: &mut [(&'a dyn SelectHandle, usize, usize)],
    is_biased: bool,
) -> Result<SelectedOperation<'a>, TrySelectError> {
    match run_select(handles, Timeout::Now, is_biased) {
        None => Err(TrySelectError),
        Some((token, index, addr)) => Ok(SelectedOperation { /* … */ }),
    }
}
```

参见 [select.rs:456-469](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L456-L469)（`try_ready → TryReadyError` 见 [select.rs:1009-1011](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1009-L1011)，`select_timeout/select_deadline → SelectTimeoutError`、`ready_deadline → ReadyTimeoutError` 见 [select.rs:498-513](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L498-L513) 与 [select.rs:1167-1169](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1167-L1169)）。select 算法本身留到 u3-l9 详讲，本讲只需知道这四个错误是「多路选择没选到」的几种姿态。

最后，所有这些错误类型都实现了 `Display` 与 `std::error::Error`（发送侧要求 `T: Send`，例如 [err.rs:124-130](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L124-L130)），并在 `lib.rs` 里统一重导出（[lib.rs:382-385](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L382-L385)），所以用户可以直接 `use crossbeam_channel::{SendError, TryRecvError, …}`。

#### 4.3.4 代码实践

**实践目标**：用一个最小程序亲手触发并区分发送侧 / 接收侧的主要错误，并把它们整理成表。

**操作步骤**：编写如下示例（**示例代码，非项目原有文件**）：

```rust
// 示例代码：触发并观察 channel 的各类错误
use std::thread;
use std::time::Duration;
use crossbeam_channel::{
    bounded, unbounded, RecvError, RecvTimeoutError,
    SendTimeoutError, TryRecvError, TrySendError,
};

fn main() {
    // 1) TrySendError::Full —— 有界通道已满
    let (s, r) = bounded(1);
    s.try_send(1).unwrap();
    assert_eq!(s.try_send(2), Err(TrySendError::Full(2)));

    // 2) TrySendError::Disconnected —— 所有接收端被 drop
    drop(r);
    assert_eq!(s.try_send(3), Err(TrySendError::Disconnected(3)));
    // 发送错误带回原消息，可取回：
    assert_eq!(s.try_send(4).unwrap_err().into_inner(), 4);

    // 3) TryRecvError::Empty / Disconnected
    let (s, r) = unbounded::<i32>();
    assert_eq!(r.try_recv(), Err(TryRecvError::Empty));
    drop(s);
    assert_eq!(r.try_recv(), Err(TryRecvError::Disconnected));

    // 4) 超时：send_timeout / recv_timeout
    let (s, r) = bounded::<i32>(0); // 零容量：无人接收即“满”
    assert_eq!(
        s.send_timeout(1, Duration::from_millis(50)),
        Err(SendTimeoutError::Timeout(1)),
    );
    let (_s, r) = unbounded::<i32>();
    assert_eq!(
        r.recv_timeout(Duration::from_millis(50)),
        Err(RecvTimeoutError::Timeout),
    );

    // 5) 阻塞版只有一种失败：Disconnected
    let (s, r) = unbounded::<i32>();
    drop(s);
    assert_eq!(r.recv(), Err(RecvError));
}
```

运行（待本地验证）：`cargo run --example <名字>`。

**需要观察的现象**：每个 `assert_eq!` 都对应一种错误变体；注意发送错误（`Full(2)`、`Disconnected(3)`）都内嵌了原消息，而接收错误（`Empty`、`Disconnected`、`Timeout`）不带数据。

**预期结果**：全部断言通过。其中 `send_timeout` 在零容量通道上、`recv_timeout` 在空通道上各等待约 50ms 后返回 `Timeout`——这也间接说明限时版是在「阻塞等待」与「立即返回」之间的中间态。

**整理任务**：把 4.3.2 的“错误一览表”复制到你的笔记里，对照上面的程序，在每种错误旁边补一句“用什么通道配置 + 什么操作触发”。例如：`TrySendError::Full` ← `bounded(1)` 已满时 `try_send`；`Disconnected` ← drop 掉对应侧所有引用后任一操作。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SendError` / `TrySendError` / `SendTimeoutError` 不直接 `derive(Debug)`，而 `RecvError` / `TryRecvError` 却可以？

> **参考答案**：前三个内嵌消息 `T`，而 `T` 不一定实现 `Debug`；`derive(Debug)` 会给 `T` 加 `Debug` 约束，从而缩小可用范围。所以它们手写 `Debug` 只打印 `"SendError(..)"` 这类占位串。接收侧错误不带数据，没有这个顾虑，可以直接 `derive(Debug)`。

**练习 2**：公开 `Sender::send` 内部出现 `SendTimeoutError::Timeout(_) => unreachable!()`。为什么这里可以安全地断言“不可达”？

> **参考答案**：`send` 调用 flavor 时传的是 `None`（无截止时间），flavor 的 `send` 永远不会因超时而中止，只会成功或返回 `Disconnected`。因此 `Timeout` 分支在 `send` 这条路径上不可能出现，`unreachable!()` 是对这一事实的断言。

**练习 3**：`From<SendError<T>> for TrySendError<T>` 这个转换的存在意义是什么？

> **参考答案**：它把“最窄”的阻塞错误 `SendError`（只有 `Disconnected`）无损嵌入“更宽”的 `TrySendError`（多了 `Full`），方便在不同调用风格之间用 `?` 传播错误：一个返回 `Result<_, TrySendError<T>>` 的函数可以同时容纳 `try_send` 的直接错误和 `send` 经转换后的错误。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个“通道生命周期观察器”小任务。

**任务描述**：编写一个程序（**示例代码，非项目原有文件**），用 `bounded(2)` 通道演示从“正常收发”到“断开回收”的完整过程，并印证本讲的三个核心结论：① 多端共享同一 `Counter`；② 只有最后一个引用 drop 才触发断开；③ 断开后各类操作返回对应错误。

```rust
// 示例代码：通道生命周期观察器
use std::thread;
use std::time::Duration;
use crossbeam_channel::{
    bounded, Receiver, RecvError, Sender, TryRecvError, TrySendError,
};

struct DropLog(&'static str);
impl Drop for DropLog {
    fn drop(&mut self) { println!("drop: {}", self.0); }
}

fn main() {
    let (s, r): (Sender<DropLog>, Receiver<DropLog>) = bounded(2);

    // ① 多端共享同一通道：clone 出第二发送端交给子线程
    let s2 = s.clone();
    let worker = thread::spawn(move || {
        s2.send(DropLog("from-worker")).ok();
        // 子线程在此返回，s2 被 drop：但主线程的 s 仍在，通道未断
    });

    thread::sleep(Duration::from_millis(100));

    // ② 此时主线程仍持有 s，接收端能看到 worker 发来的消息
    assert!(matches!(r.try_recv(), Ok(_)));

    // ③ 主线程 drop 最后一个发送端 → senders 归零 → disconnect_senders
    drop(s);
    worker.join().unwrap();

    // ④ 断开后：发送返回 Disconnected（带回消息），接收返回 Disconnected
    //    （注意这里 s 已 drop，故用一个新引用演示发送错误需要重新构造；
    //     为保持示例自洽，下面仅演示接收侧断开。）
    assert_eq!(r.try_recv(), Err(TryRecvError::Disconnected));

    // ⑤ 阻塞 recv 在“空且断开”时立刻返回 RecvError（不会永远阻塞）
    assert_eq!(r.recv(), Err(RecvError));

    println!("通道已被最后一个 drop 的引用回收（参见 4.2 的 destroy 裁决）");
}
```

**操作步骤**：

1. 把上述代码放入 `crossbeam-channel/examples/lifecycle.rs`（或独立小 crate）。
2. 运行（待本地验证）：`cargo run -p crossbeam-channel --example lifecycle`。
3. 对照 4.2.3 的销毁链路，在脑中（或纸面上）标注每一步对应 `counter.rs` 的哪一行：`acquire`（clone）、`fetch_sub==1`（最后一个 drop）、`disconnect_senders`（唤醒）、`destroy.swap`（裁决回收）。

**需要观察的现象**：

- `DropLog` 的 `drop` 日志会按消息被消费 / 被丢弃的时机打印，帮你定位“消息何时释放”。
- worker 线程 drop `s2` 后通道**没有**断开（因为主线程 `s` 还在）；只有主线程也 drop `s` 后，`r.try_recv()` 才返回 `Disconnected`。
- 最后 `r.recv()` 立即返回 `Err(RecvError)`，证明断开后阻塞接收不会卡死——这正是 4.2 里 `disconnect` 唤醒机制的价值。

**预期结果**：程序正常退出，所有断言通过，无内存泄漏（若把 `DropLog` 的 drop 日志累加，应与发送条数一致）。

---

## 6. 本讲小结

- `Counter<C>` 是通道的**共享内核**：`senders` / `receivers` 两个 `AtomicUsize` 计数、一个 `destroy` 仲裁位、内联的 flavor 通道 `chan`。所有 clone 出来的端都只持有一份 `NonNull<Counter<C>>` 裸指针。
- crossbeam-channel **手写**引用计数而非用 `Arc`：用 `Box::leak` 构造（不自动释放），用 `release` 里的 `Box::from_raw` 回收，从而把“两端各自计数 + 恰好一次销毁”的复杂语义握在自己手里。
- `acquire`（`clone`）用 `Relaxed` 增计数并带溢出 `abort` 保护；`release`（`drop`）用 `AcqRel` 减计数，当旧值为 1 时触发该侧 `disconnect` 回调唤醒对端，再通过 `destroy.swap` 裁决由“后到达者”回收整条通道——保证恰好销毁一次。
- `disconnect_senders` / `disconnect_receivers` 用 `MARK_BIT` 的 `fetch_or` 把“断开”只点亮一次，并唤醒对端阻塞操作——这是所有 `Disconnected` 错误的物理来源。
- `err.rs` 是一张规整的错误矩阵：发送侧（`SendError` / `TrySendError` / `SendTimeoutError`）**内嵌并带回原消息**，故手写 `Debug`；接收侧（`RecvError` / `TryRecvError` / `RecvTimeoutError`）无数据；select 侧四件套（`TrySelectError` / `SelectTimeoutError` / `TryReadyError` / `ReadyTimeoutError`）。
- 阻塞版错误只有 `Disconnected` 一种；`try_*` 多 `Full` / `Empty`；`*_timeout` 多 `Timeout`。`From` 转换把窄错误嵌入宽错误，公开 `send` / `recv` 据此把 flavor 返回的 `SendTimeoutError` / `RecvTimeoutError` 收窄，并用 `unreachable!()` 标记不可能出现的 `Timeout` 分支。

---

## 7. 下一步学习建议

本讲搞清楚了“通道的引用计数与销毁”以及“操作失败时返回什么错误”，但**故意没碰**两个问题：

1. **flavor 内部到底怎么存放消息、怎么做并发收发？** 例如 `disconnect` 用到的 `MARK_BIT`、`tail` 究竟编码了什么。这是下一讲 **u3-l3「flavors 架构与 SelectHandle trait」**的入口——你会看到 `SenderFlavor` / `ReceiverFlavor` 枚举如何把操作路由到 array / list / zero 等具体实现，以及统一的 `SelectHandle` trait。
2. **阻塞的 `send` / `recv` 是怎么“睡着”和“被叫醒”的？** 本讲只提到 `disconnect` 会“唤醒对端”，但唤醒的细节（线程局部阻塞上下文、被阻塞线程的队列）要等到 **u3-l7「Context 与 Waker：阻塞与唤醒机制」**才展开。

建议的阅读顺序：先 u3-l3（flavor 架构总览）→ u3-l4/u3-l5/u3-l6（array/list/zero 三种 flavor 的存储与算法）→ u3-l7（阻塞唤醒）。读 flavor 时，可以回头对照本讲的 `disconnect_senders` / `disconnect_receivers`，体会“计数销毁”与“flavor 内部状态机”是如何在 `release` 这一处汇合的。
