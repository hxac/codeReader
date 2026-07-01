# counter 引用计数与 err 错误类型

## 1. 本讲目标

本讲承接 u3-l1（channel 总览与基本使用），从「公共接口层」往下钻一层，只读两个文件：

- `crossbeam-channel/src/counter.rs`：管理多份 `Sender`/`Receiver` 句柄生命周期的引用计数内核。
- `crossbeam-channel/src/err.rs`：定义 `send`/`recv`/`select` 全家桶的错误类型。

学完后你应当能够：

1. 画出「多个 `Sender` 与多个 `Receiver` 共享同一份堆上 `Counter`」的内存模型，说出 `senders`/`receivers`/`destroy`/`chan` 四个字段各管什么。
2. 复述 `acquire`（克隆）/`release`（丢弃）的引用计数协议，并解释「为什么通道不会被重复释放、也不会泄漏」——关键是 `destroy` 标志的「你先走、我收尾」握手。
3. 看懂 `err.rs` 里 10 个错误类型各自的触发条件，并知道阻塞版 `send`/`recv` 的错误如何通过 `From` 转换融入 `try_*`/`*_timeout` 的更丰富错误类型。

本讲**不**涉及具体 flavor（array/list/zero）的缓冲算法，也不涉及阻塞唤醒机制——前者是 u3-l3 起的内容，后者是 u3-l7 的主题。本讲的视角是：当你在多个线程里 `clone()`、再 `drop()` 这些句柄时，通道的「计数」与「错误」是如何被精确记账的。

## 2. 前置知识

在进入源码前，先建立三个直觉。本讲假设你已读过 u3-l1，知道 `unbounded()`/`bounded(cap)` 会产出一对 `(Sender, Receiver)`，且二者都能 `clone()`（crossbeam-channel 是 MPMC）。

### 2.1 为什么需要引用计数

`Sender` 和 `Receiver` 都是**可克隆的句柄（handle）**：你克隆出来的一堆 `Sender`，背后指向的是**同一个**通道内部状态（缓冲区、阻塞线程队列等）。这份共享状态只能分配**一次**、也必须释放**一次**。于是需要一个独立的「账本」记录：当前有几个发送端、几个接收端还活着。这个账本就是 `Counter`。

这与 `Arc` 的思想一致，但这里有一个 `Arc` 没有的特殊点：**发送端和接收端是两本独立的账**，它们要协作决定「谁负责拔管子（disconnect）」「谁负责埋（deallocate）」。

### 2.2 两个易混词：disconnect 与 deallocate

- **disconnect（断开）**：通知通道的 flavor「这一侧已经没人了」。例如最后一个 `Sender` 被丢弃时，要唤醒所有正阻塞在 `recv` 上的线程——因为它们再等也等不到消息了。这是一个**逻辑事件**。
- **deallocate（释放内存）**：把堆上的 `Counter`（连同它持有的 flavor）真正 `drop` 掉、归还内存。这是一个**物理事件**。

关键洞察：**disconnect 发生两次（发送端一次、接收端各一次），但 deallocate 只能发生一次。** 本讲的核心就是看清楚这「两次断开、一次释放」是如何被 `destroy` 标志安全协调的。

### 2.3 错误类型为什么要分这么多

同样是「发送失败」，`send`（阻塞）只会因为「通道断开」失败；而 `try_send`（非阻塞）还可能因为「通道满」失败；`send_timeout` 还多一种「超时」。每种调用方式的失败原因集合不同，所以 Rust 用**不同的错误类型**精确表达「这次调用可能以哪些方式失败」，让调用者被迫在编译期就把每种情况都处理掉（典型的 `Result` + 枚举模式）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [crossbeam-channel/src/counter.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs) | 引用计数内核（`pub(crate)`，内部类型） | `Counter` 结构、`new`、`Sender`/`Receiver` 的 `acquire`/`release`、`destroy` 握手 |
| [crossbeam-channel/src/err.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs) | 错误类型家族 | 10 个错误类型、触发条件、`From` 转换、`into_inner`/`is_*` 辅助方法 |
| [crossbeam-channel/src/channel.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs)（部分） | 公共 `Sender<T>`/`Receiver<T>` 把 counter 接入 | `Clone::clone` 调 `acquire`、`Drop::drop` 调 `release`、`send` 的错误收窄、`unsafe impl Send/Sync` |

> 层次提示：`counter.rs` 里的 `Sender<C>`/`Receiver<C>` 是**内部类型**（`pub(crate)`），泛型 `C` 是具体 flavor 的内部通道（如 `flavors::list::Channel<T>`）；你在应用代码里用的 `crossbeam_channel::Sender<T>` 是 `channel.rs` 里的公共类型，它用一个 `SenderFlavor<T>` 枚举把 `counter::Sender<各 flavor>` 包起来再 `match` 派发（详见 u3-l3）。本讲聚焦最底层的 `Counter` 这一层，除非特别说明，文中 `Sender`/`Receiver` 指 `counter.rs` 的内部句柄。

---

## 4. 核心概念与源码讲解

### 4.1 Counter：引用计数的共享骨架

#### 4.1.1 概念说明

`Counter<C>` 是一块**堆上的共享账本**，被同一通道的所有 `Sender` 克隆和所有 `Receiver` 克隆共同指向。它持有四个字段：

- `senders: AtomicUsize` —— 当前存活的发送端句柄数量。
- `receivers: AtomicUsize` —— 当前存活的接收端句柄数量。
- `destroy: AtomicBool` —— 「谁负责收尾」的握手标志（详见 4.2）。
- `chan: C` —— 真正的通道实现（某个 flavor），内联在 `Counter` 里。

为什么用堆 + 指针，而不是把状态直接放进 `Sender`？因为 `Sender` 要被克隆成多份、分散到不同线程，每份都只是「一个瘦指针」。共享状态必须有一个稳定地址——所以 `counter::new` 用 `Box::leak` 把 `Counter` 钉在堆上，永不移动，再把它的地址以 `NonNull` 的形式塞进每个句柄。

这与标准库 `Arc` 形似但**不**用 `Arc`：通道需要两本独立的账与一个跨两侧的销毁握手，`Arc` 的单一计数表达不了；而且把两个计数与 flavor 状态放进**同一块**分配，也避免了 `Arc` 套 `Arc` 的多层间接。

#### 4.1.2 核心流程

通道从创建到销毁的生命周期可以画成：

```text
counter::new(chan)
  ├── Box::leak: 在堆上分配 Counter{senders:1, receivers:1, destroy:false, chan}
  └── 返回 (Sender{counter}, Receiver{counter})   // 两份句柄共享同一指针

clone() ──→ acquire():  senders 或 receivers  fetch_add(1, Relaxed)
drop()  ──→ release():  senders 或 receivers  fetch_sub(1, AcqRel)
                       ├── 若减到 0：调用 flavor 的 disconnect（唤醒对端）
                       └── 再用 destroy 标志决定是否真正 free 这块堆内存
```

要点：

1. **计数器初始都是 1**：`counter::new` 返回的「原始那一对」各占一个名额。
2. **clone = 计数 +1，drop = 计数 -1**，对应字段自增/自减。
3. **谁先把自己的计数减到 0，谁就去 disconnect**；**真正的内存释放**则由 `destroy` 标志在两侧之间协调，保证恰好一次。

| 事件 | 计数变化 | 是否最后一个 | 动作 |
|------|---------|------------|------|
| `new` | 设为 1 | — | 堆分配 `Counter` |
| `clone`（acquire） | `+1` | 否 | 复制句柄（同指针） |
| `drop`（release） | `-1`（旧值 > 1） | 否 | 仅减计数 |
| `drop`（release） | `-1`（旧值 == 1） | 是 | `disconnect` + 参与 `destroy` 裁决 |

#### 4.1.3 源码精读

先看 `Counter` 结构与 `new` 构造：

[crossbeam-channel/src/counter.rs:12-24](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L12-L24) —— 定义 `Counter<C>` 的四个字段（senders/receivers/destroy/chan）。

[crossbeam-channel/src/counter.rs:27-37](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L27-L37) —— `new` 用 `Box::leak(Box::new(Counter{...}))` 把账本钉在堆上，得到 `NonNull<Counter<C>>`，再装进 `Sender`/`Receiver` 返回。这里有三个设计要点：① `Box::leak` 故意「泄漏」`Box`，使其**不会**自动释放，把回收责任交给 `release` 里的 `Box::from_raw`；② `NonNull::from` 让发送端与接收端拿到**同一个**指针——这就是「共享」的物理体现；③ `senders`、`receivers` 都初始化为 `1`，对应这一对原始句柄。

再看 `Sender` 的形状与它如何「透明」访问底层 flavor：

[crossbeam-channel/src/counter.rs:40-48](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L40-L48) —— `Sender<C>` 只持有一个 `NonNull<Counter<C>>`（瘦指针，指针大小）。`counter()` 用 `unsafe { self.counter.as_ref() }` 把它变回 `&Counter<C>`。

[crossbeam-channel/src/counter.rs:84-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L84-L90) —— `impl Deref for Sender<C>`，`Target = C`。这意味着 `*sender` 直接就是底层 flavor 通道，公共层调用 `chan.send(...)` 时，`chan` 其实经由 `Deref` 透传到 `counter.chan`。`Receiver` 的 `Deref` 完全对称（[counter.rs:143-149](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L143-L149)）。

句柄相等性由「指针相等」定义：

[crossbeam-channel/src/counter.rs:92-96](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L92-L96) —— `PartialEq` 直接比较 `NonNull`（即两个句柄是否指向同一个 `Counter`）。这是公开 API `same_channel` 判断「两个端是否属于同一通道」的底层依据。`addr()`（[counter.rs:79-81](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L79-L81)）把指针转成 `usize`，供 `select` 判断「两个操作是否作用在同一通道」。

跨线程安全性由公共层强制：

[crossbeam-channel/src/channel.rs:382-383](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L382-L383) —— `unsafe impl<T: Send> Send for Sender<T> {}` 与 `Sync`。`NonNull` 本身既非 `Send` 也非 `Sync`，但这两条 `unsafe impl`（接收端在 [channel.rs:749-750](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L749-L750)）以「显式 unsafe impl 优先于自动推导」的规则，在 `T: Send` 时把句柄强制设为可跨线程共享——这正是 MPMC 多线程 `clone`/`drop` 的前提。

最后看一眼内部计数句柄如何被公共 API 造出来（以 `unbounded` 为例）：

[crossbeam-channel/src/channel.rs:50-59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L50-L59) —— `counter::new(flavors::list::Channel::new())` 拿到 `counter::Sender<flavors::list::Channel<T>>`，随后被包进 `SenderFlavor::List(..)` 成为对外的 `Sender<T>`。`bounded` 的两条分支同理（[channel.rs:113-133](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L113-L133)），只是 `chan` 换成 `array` 或 `zero` flavor。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（计数器是 `pub(crate)`，无法从外部直接打印其值）。

1. **实践目标**：建立「多个句柄共享一份堆 `Counter`」的清晰心智模型，并理解 `Box::leak` 为何不会真泄漏。
2. **操作步骤**：
   - 打开 [counter.rs:12-37](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L12-L37)，在纸上画出 `counter::new` 执行后的内存图：左侧是堆上的 `Counter`（四个字段），右侧是栈上的 `(Sender, Receiver)`，两者各画一根箭头指向堆块。
   - 模拟以下序列，逐行更新 `senders`/`receivers` 的值：
     ```text
     初始:                        senders=1, receivers=1
     let s2 = s.clone();          senders=2
     let r2 = r.clone();          receivers=2
     let s3 = s.clone();          senders=3
     drop(s2);                    senders=2
     drop(s3);                    senders=1   ← 还没到 0，不 disconnect
     drop(s);                     senders=0   ← 到 0，触发 disconnect_senders
     ```
3. **需要观察的现象**：每一步只有「计数归零」的那一步才会进入 `release` 的 disconnect 分支；其余 drop 只是默默减一。
4. **预期结果**：你应当得出「最后一个发送端 drop 时才断开」的结论，并能解释为什么在它之前的任意 drop 都不会唤醒阻塞的接收者。
5. **关于 `Box::leak` 是否泄漏**：只要你在程序结束前把所有 `Sender`/`Receiver` 都正常 `drop`，最后一个 drop 就会在 `release` 里 `Box::from_raw` 回收内存；只有用 `mem::forget` 故意丢弃句柄才会真泄漏，而 4.2 的溢出保护正是为此兜底。
6. 待本地验证：计数器具体数值无法用公共 API 打印，以上为依据源码的推理结论。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Counter` 用 `Box::leak` + `NonNull` 而不是直接用 `Arc`？

**参考答案**：通道需要**两本独立的账**（senders / receivers）和一个**跨两侧的销毁握手**（destroy），标准 `Arc` 只有单一引用计数，无法表达「发送端归零」与「接收端归零」这两个独立事件及其协调。此外 `Counter` 内联了 `chan: C`，把两个计数与 flavor 状态放进同一块分配，自己掌控释放时机更直接，也避免 `Arc` 套 `Arc` 的多层间接。

**练习 2**：`Sender<C>` 实现了 `Deref<Target = C>`，这意味着什么？

**参考答案**：意味着可以对 `Sender` 直接调用底层 flavor 的方法（如 `chan.send(...)`），编译器自动插入 `*` 解引用。公共层的 `send`/`try_send` 正是经由这个 `Deref` 透传到 `counter.chan` 上的 flavor 方法，计数逻辑与 flavor 算法因此解耦。

---

### 4.2 acquire/release 与唯一销毁协议

#### 4.2.1 概念说明

引用计数有两个经典难题，本节看 crossbeam 如何一并解决：

1. **竞态下的精确归零判定**：多线程同时 `drop`，如何可靠地知道「我是把计数减到 0 的那一个」？答案是用 `fetch_sub` 的**返回值**（返回的是旧值）。若旧值为 1，则这次减法后变成 0，我就是「最后一个」。
2. **恰好一次释放**：发送端归零、接收端归零是**两次独立事件**，但堆内存只能释放一次。crossbeam 用一个 `destroy: AtomicBool` 做「你先走、我收尾」的握手。

还有一个工程细节：**计数溢出防护**。若有人恶意 `clone` 后 `mem::forget` 丢弃的克隆，计数会无限上涨。crossbeam 在计数逼近 `isize::MAX` 时直接 `process::abort()`——比起在退化场景下冒险（回绕→误判归零→double free），宁可整个进程中止。这与标准库 `Arc` 的防御策略一致。

#### 4.2.2 核心流程

**acquire（克隆，对应 `Clone::clone`）**：

```text
count = senders.fetch_add(1, Relaxed)     // 或 receivers
if count > isize::MAX as usize { abort() } // 防溢出
return 新的 Sender{counter}                // 复制同一个指针
```

**release（丢弃，对应 `Drop::drop`）**：

```text
old = senders.fetch_sub(1, AcqRel)        // 或 receivers
if old == 1 {                             // 我是把它减到 0 的那个
    disconnect(&chan);                     // 通知 flavor：这一侧没人了（唤醒对端）
    already = destroy.swap(true, AcqRel); // 抢「收尾权」
    if already {                          // 对方已经先来过（destroy 已是 true）
        Box::from_raw + drop              // 由我执行真正的内存释放
    }
    // 否则（already == false）：我把 destroy 置 true，但我不释放，等对方来释放
}
```

「恰好一次释放」的关键就在 `destroy.swap(true, ...)`：

- 它返回**旧值**并把 `destroy` 置为 `true`。
- **第一个**到达 release 归零的侧（假设是发送端）：`destroy` 还是 `false`，swap 返回 `false` → 不释放，只把标志置真。
- **第二个**到达 release 归零的侧（接收端）：`destroy` 已是 `true`，swap 返回 `true` → 执行 `Box::from_raw` 真正释放。

于是释放权被「让」给了后到的一方，保证全局恰好一次。两次 disconnect 都会执行（各自唤醒对端），但 deallocate 只发生一次。

> 用一点离散数学表示：设两次 release 归零事件为 \(E_s\)（发送端）与 \(E_r\)（接收端），释放操作 \(D\) 定义为
> \[ D \iff \texttt{destroy.swap(true)} \text{ 返回 } \textit{true} \]
> 由于 `destroy` 初值为 `false` 且只会被这两次 swap 写入，两次 swap 中**恰有一次**看到旧值 `false`、另一次看到 `true`，故 \(D\) 恰发生一次。

#### 4.2.3 源码精读

**acquire**——注意用 `Relaxed`，以及溢出保护：

[crossbeam-channel/src/counter.rs:51-64](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L51-L64) —— `Sender::acquire`：`fetch_add(1, Ordering::Relaxed)`。`Relaxed` 足够，因为克隆时你**已经合法持有一份句柄**（计数 ≥ 1），自增不需要和别的线程的读写建立 happens-before；它只是把「我多了一份」这个事实记进账本——这正是 `Arc::clone` 也用 `Relaxed` 的同款取舍。随后若发现 `count > isize::MAX` 就 `process::abort()`。`Receiver::acquire` 完全对称（[counter.rs:110-123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L110-L123)）。

**release**——本讲最核心的一段：

[crossbeam-channel/src/counter.rs:69-77](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L69-L77) —— `Sender::release`：

1. `senders.fetch_sub(1, Ordering::AcqRel)` 返回旧值；若旧值为 `1`，说明这次把它减到了 `0`，进入收尾分支。
2. 调用传入的 `disconnect` 闭包（`disconnect(&self.counter().chan)`），让 flavor 执行断开逻辑（置断开标志、唤醒对端阻塞线程）。
3. `destroy.swap(true, Ordering::AcqRel)`：抢「收尾权」。返回 `true`（说明对侧已经先来过）才真正 `Box::from_raw` 释放堆内存。

`Receiver::release` 结构完全一致，只是操作 `receivers` 字段（[counter.rs:128-136](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L128-L136)）。

**为什么 `fetch_sub` 和 `destroy.swap` 都用 `AcqRel`？**

- `fetch_sub` 的 **Release**：保证本线程此前对通道数据的所有写入，在计数归零前已完成并对外可见；**Acquire**：保证「减到 0 的那个线程」能看到此前其他线程丢弃克隆时执行的所有 Release，从而看到一致状态。
- `destroy.swap` 的 **AcqRel** 是**跨侧握手的关键**：先到的一侧用 Release 把 `destroy` 写为 `true` 并发布自己对通道的全部写入；后到的一侧用 Acquire 读到 `true`，于是它能安全地看到先到侧的所有写入，再执行 `Box::from_raw` 释放——不会读到半释放的状态。

**`release` 是 `unsafe fn`，且 `disconnect` 闭包的 `bool` 返回值被丢弃**：

[crossbeam-channel/src/counter.rs:69](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L69) —— 签名 `unsafe fn release<F: FnOnce(&C) -> bool>(&self, disconnect: F)`。`unsafe` 是因为调用者必须保证「此刻确实持有一份合法引用」（即调用一次 `release` 对应一次之前的 `acquire`/`new`），否则会减成负数、错误释放。注意闭包返回 `bool`（flavor 的 `disconnect_*` 返回「是不是本线程首次把通道标记为断开」），但 `release` 内部 `disconnect(&self.counter().chan);` 这条**语句形式丢弃了返回值**——真正的释放决策完全由 `destroy` 标志决定，与闭包返回值无关。这是一个阅读时容易绊倒的细节。

**disconnect 闭包里到底做了什么？** 以 list flavor（unbounded 的默认实现）为例：

[crossbeam-channel/src/flavors/list.rs:561-570](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L561-L570) —— `disconnect_senders` 用 `fetch_or(MARK_BIT)` 在索引上打「发送端已断开」的标记位（且只让第一个这么做的人返回 `true`），并调用 `self.receivers.disconnect()` 唤醒所有阻塞的接收者。返回的 `bool` 如前所述在 `counter::release` 中并未被使用。

[crossbeam-channel/src/flavors/list.rs:575-586](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L575-L586) —— `disconnect_receivers` 同样打标记位，并额外 `discard_all_messages()` **急切释放**尚未消费的消息（因为接收端都走了，留着也没人读）。array flavor 的同名方法逻辑类似（[flavors/array.rs:487-516](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L487-L516)）。

**公共层如何把 `Clone`/`Drop` 接到 `acquire`/`release`**：

[crossbeam-channel/src/channel.rs:686-696](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L686-L696) —— `Clone for Sender<T>`：按 flavor 调 `chan.acquire()`。`Drop for Sender<T>`（[channel.rs:674-684](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L674-L684)）：按 flavor 调 `chan.release(|c| c.disconnect_senders())`（zero flavor 调 `c.disconnect()`）。接收端对称（[channel.rs:1199-1209](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1199-L1209) 与 [channel.rs:1184-1197](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1184-L1197)）。注意 `at`/`tick`/`never` 这三种特殊 flavor 没有 `release` 路径——它们不参与引用计数销毁。

把 4.1 与 4.2 串起来，整条销毁链路是：**公开 `Sender::drop` → `counter::Sender::release` → `fetch_sub` 命中 1 → `disconnect_senders`（唤醒对端）→ `destroy` 裁决 → 必要时 `Box::from_raw` 回收**。

#### 4.2.4 代码实践

这是本讲的主实践任务（结合行为观察与源码追踪）。

1. **实践目标**：跨线程克隆与丢弃多个句柄，间接验证「只有最后一个引用离开才触发断开」，并追踪 `senders` 何时归零。

2. **操作步骤**：下面这段**示例代码**（非仓库原有，使用公共 API）演示多线程克隆与丢弃：

   ```rust
   // 示例代码：追踪 senders 计数归零 → 触发断开
   use std::thread;
   use crossbeam_channel::{unbounded, TryRecvError};

   fn main() {
       let (s, r) = unbounded::<i32>();

       // clone 出第二份发送端交给子线程
       let s2 = s.clone();                 // senders: 1 -> 2
       let h = thread::spawn(move || {
           // 子线程结束 drop(s2)：主线程的 s 还在，senders 还有 1，通道未断开
           drop(s2);
       });

       // 此时只 drop 了子线程那份，通道还没断 → try_recv 是 Empty
       h.join().unwrap();
       assert_eq!(r.try_recv(), Err(TryRecvError::Empty));

       drop(s);                            // 主线程的原始发送端：senders 减到 0 → disconnect_senders
       assert_eq!(r.try_recv(), Err(TryRecvError::Disconnected));
   }
   ```

   运行（待本地验证）：`cargo run --example <名字>` 或在依赖 `crossbeam-channel` 的小 crate 里运行。

3. **需要观察的现象**：
   - 只 drop 子线程那份发送端（另一份仍存活）时，`r.try_recv()` 返回 `Empty`——通道还没断。
   - 当**两个**发送端都被 drop（`senders` 归零），`r.try_recv()` 才返回 `Disconnected`。
4. **预期结果**：断言通过。这正是 `release` 中 `fetch_sub(..) == 1` 守门的结果——只有把计数从 1 减到 0 的那一次 drop 才会调用 `disconnect_senders`，把断开信号传给接收端。
5. **进阶观察**：想确认销毁确实只发生一次，可把消息类型换成带 `Drop` 实现的类型（在 `Drop` 里打印日志），观察通道销毁时缓冲中的消息是否被释放、是否每条只释放一次。注意 `disconnect_receivers` 会主动 `discard_all_messages`，所以「接收端先全部离开」时消息会更早被释放。
6. 待本地验证：计数器是内部类型，无法直接打印其值；上述「何时归零」的结论是基于源码协议的推理。

#### 4.2.5 小练习与答案

**练习 1**：假设线程 A drop 最后一个发送端、线程 B drop 最后一个接收端，两者几乎同时到达 `destroy.swap(true, ..)`。谁负责 `Box::from_raw`？会不会 double free？

**参考答案**：`swap` 是原子的，两线程中必有一个先拿到 `prev == false`（不回收）、另一个拿到 `prev == true`（执行回收）。因此**后到达者**负责回收，且因 `swap` 互斥，不会 double free，也不会漏销毁。

**练习 2**：`acquire` 用 `Relaxed`、`release` 用 `AcqRel`，为什么不对称？

**参考答案**：克隆时调用方已经持有一份合法引用，自增只是记账，不需要建立跨线程的 happens-before，`Relaxed` 足够且最快；丢弃时，减到 0 的那个线程要负责 disconnect 与可能的释放，必须用 Acquire 看到此前所有丢弃线程的 Release、并用 Release 发布自己的写入，因此用 `AcqRel`。这与 `Arc::clone` / `Arc::drop` 的取舍一致。

**练习 3**：`disconnect` 闭包返回 `bool`，但 `release` 没用它。这会是个 bug 吗？

**参考答案**：不是 bug，而是有意的设计分层。flavor 的 `disconnect_*` 返回「是否本线程首次打上断开标记」（用 `MARK_BIT` 的 `fetch_or` 保证只有一次返回 `true`），可供别处复用；但 `counter::release` 的释放决策完全由「计数归零 + `destroy` 标志」决定，与该返回值无关，因此丢弃它是正确的。阅读源码时要注意别被签名里的 `-> bool` 误导。

---

### 4.3 err.rs：错误类型家族

#### 4.3.1 概念说明

`err.rs` 把「发送/接收/选择」的各种失败方式建模成一族类型。设计原则是**精确性**：每种公共方法返回一个**只包含它可能发生的失败原因**的错误类型，逼调用者用 `match` 把每种情况都处理掉。

可以把这 10 个类型分成三族：

| 族 | 类型 | 来源方法 | 携带数据 |
|----|------|---------|---------|
| **发送族** | `SendError<T>` | `send` | 消息 `T`（可 `into_inner` 取回） |
| | `TrySendError<T>` | `try_send` | `Full(T)` / `Disconnected(T)` |
| | `SendTimeoutError<T>` | `send_timeout` | `Timeout(T)` / `Disconnected(T)` |
| **接收族** | `RecvError` | `recv` | 无（单元结构） |
| | `TryRecvError` | `try_recv` | `Empty` / `Disconnected` |
| | `RecvTimeoutError` | `recv_timeout` | `Timeout` / `Disconnected` |
| **选择族** | `TrySelectError` | `try_select` | 无 |
| | `SelectTimeoutError` | `select_timeout` | 无 |
| | `TryReadyError` | `try_ready` | 无 |
| | `ReadyTimeoutError` | `ready_timeout` | 无 |

两个值得记住的规律：

1. **发送族错误都把消息 `T` 带回来**（`into_inner()`），因为发送失败时消息没被消费，必须还给调用者；接收族与选择族不携带数据。
2. **阻塞版只可能因「断开」失败**：`send` 会一直等到发出或断开，所以 `SendError` 只有断开一种；`try_*`/`*_timeout` 多出「满/空/超时」。

#### 4.3.2 核心流程

每种方法的失败判定可以这样总结（结合 u3-l1 的 flavor 与断开概念）：

```text
send(msg)          阻塞直到发出 或 通道断开
                   → 成功 Ok(())     失败 Err(SendError(msg))           # 仅断开

try_send(msg)      不阻塞，立即尝试
                   → 满/零容量无接收端: Err(TrySendError::Full(msg))
                   → 断开:            Err(TrySendError::Disconnected(msg))

send_timeout(msg)  阻塞，但带截止时间
                   → 超时:           Err(SendTimeoutError::Timeout(msg))
                   → 断开:           Err(SendTimeoutError::Disconnected(msg))

recv()             阻塞直到收到 或 空+断开
                   → 成功 Ok(msg)    失败 Err(RecvError)                  # 空+断开

try_recv()         不阻塞，立即尝试
                   → 空/零容量无发送端: Err(TryRecvError::Empty)
                   → 空+断开:          Err(TryRecvError::Disconnected)

recv_timeout()     阻塞，但带截止时间
                   → 超时:   Err(RecvTimeoutError::Timeout)
                   → 空+断开: Err(RecvTimeoutError::Disconnected)
```

为方便错误在调用链中流转，`err.rs` 还提供了一组 `From` 转换，把「更窄」的阻塞错误**提升**成「更宽」的 `try_*`/`*_timeout` 错误：

```text
SendError<T>     →  TrySendError::Disconnected(T)
SendError<T>     →  SendTimeoutError::Disconnected(T)
RecvError        →  TryRecvError::Disconnected
RecvError        →  RecvTimeoutError::Disconnected
```

这样，一个内部用 `send`（返回 `SendError`）、对外暴露 `try_send` 语义（返回 `TrySendError`）的函数，可以用 `?` 直接把内部错误转成外部错误，无需手写 `match`。

记一条总规律：**「断开（Disconnected）」是所有数据通道操作的终极失败信号**，它由 4.2 的 `disconnect` 机制点亮——只要某一侧最后一个引用被 drop，对端的所有阻塞/非阻塞操作就能看到断开。

#### 4.3.3 源码精读

**发送族——以 `TrySendError` 为例（最具代表性，含两个变体）**：

[crossbeam-channel/src/err.rs:19-29](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L19-L29) —— `TrySendError<T>`：`Full(T)`（通道满；对零容量通道则表示「此刻没有接收端」）与 `Disconnected(T)`。注意每个变体都**携带消息 `T`**。

[crossbeam-channel/src/err.rs:11-12](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L11-L12) —— `SendError<T>(pub T)`：元组结构体，只有「断开」一种情况，直接把消息 `T` 装在里面（`pub` 字段，可 `err.0` 取出）。`SendTimeoutError<T>` 同构（[err.rs:36-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L36-L46)）。

**辅助方法与 `From` 转换**：

[crossbeam-channel/src/err.rs:180-210](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L180-L210) —— `TrySendError<T>` 的 `into_inner()`（取回消息）、`is_full()`、`is_disconnected()` 三个判断方法，用 `matches!` 宏实现。

[crossbeam-channel/src/err.rs:172-178](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L172-L178) —— `From<SendError<T>> for TrySendError<T>`：把 `SendError(t)` 映射为 `Disconnected(t)`。`SendTimeoutError` 也有等价转换（[err.rs:229-235](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L229-L235)）。

**接收族**：

[crossbeam-channel/src/err.rs:53-54](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L53-L54) —— `RecvError`：单元结构体（`struct RecvError;`），不携带任何数据，因为接收失败时本就一无所有。它甚至直接 `#[derive(Debug)]`。

[crossbeam-channel/src/err.rs:59-69](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L59-L69) —— `TryRecvError`：`Empty`（通道空；零容量则表示「此刻无发送端」）与 `Disconnected`（空且断开）。`RecvTimeoutError` 同构（[err.rs:74-84](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L74-L84)）。

[crossbeam-channel/src/err.rs:289-307](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L289-L307) —— `From<RecvError> for TryRecvError`（映射为 `Disconnected`）与 `TryRecvError` 的 `is_empty()`/`is_disconnected()`。`RecvTimeoutError` 的对应物在 [err.rs:320-338](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L320-L338)。

**选择族**：

[crossbeam-channel/src/err.rs:86-116](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L86-L116) —— `TrySelectError`、`SelectTimeoutError`、`TryReadyError`、`ReadyTimeoutError` 四个单元结构体，分别对应 `try_select`（无操作就绪）、`select_timeout`（超时）、`try_ready`、`ready_timeout`。它们都不携带数据，产生点在 `select.rs`（详见 u3-l9）。

**trait 实现：`Debug`/`Display`/`Error` 的不对称**：

[crossbeam-channel/src/err.rs:118-130](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L118-L130) —— `SendError<T>` **手写** `Debug`（打印 `"SendError(..)"`）而非 derive，因为 `T` 不一定实现 `Debug`；`Display` 给出可读文案；`impl<T: Send> error::Error` 在 `T: Send` 时成立。这是发送族三个类型的统一模式（`TrySendError` 的手写 Debug 见 [err.rs:152-159](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L152-L159)）。接收族与选择族因不携带 `T`，直接 `#[derive(Debug)]`。

**公共 `send` 如何把 flavor 的「最宽错误」收窄成 `SendError`**：

[crossbeam-channel/src/channel.rs:446-456](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L446-L456) —— `pub fn send(&self, msg: T) -> Result<(), SendError<T>>`：内部调用 `chan.send(msg, None)`（注意第二个参数是 `None`，即**无截止时间**），flavor 返回 `Result<(), SendTimeoutError<T>>`，再用 `map_err` 把 `Disconnected(msg)` 收窄为 `SendError(msg)`，把 `Timeout(_)` 标为 `unreachable!()`。因为传了 `None`，flavor 永远不可能因超时而中止，所以 `Timeout` 分支不可达、`unreachable!()` 是安全的。接收端的 `recv` 同样把 `RecvTimeoutError` 收窄成 `RecvError`（参见 [channel.rs:831-857](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L831-L857) 一带的 `recv` 实现）。

**对外重导出**：

[crossbeam-channel/src/lib.rs:382-385](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L382-L385) —— `lib.rs` 把 `err.rs` 的全部错误类型统一 `pub use` 出来，所以用户可以直接 `use crossbeam_channel::{SendError, TryRecvError, …}`。

#### 4.3.4 代码实践

这是本讲实践任务的第二部分：用一个可运行示例触发主要错误类型，并整理成表。

1. **实践目标**：亲手触发并区分发送/接收族的错误，建立「错误类型 ↔ 触发条件」的对应表。

2. **操作步骤**：在依赖 `crossbeam-channel` 的 crate 里运行下面这段**示例代码**：

   ```rust
   // 示例代码：触发并观察 channel 的各类错误
   use std::time::Duration;
   use crossbeam_channel::{
       bounded, unbounded, RecvError, RecvTimeoutError,
       SendTimeoutError, TryRecvError, TrySendError,
   };

   fn main() {
       // 1) SendError：丢弃所有接收端后 send（带回原消息）
       let (s, r) = unbounded::<i32>();
       drop(r);
       assert_eq!(s.send(1), Err(crossbeam_channel::SendError(1)));

       // 2) TrySendError::Full：有界(1) 装 1 条后再 try_send
       let (s, r) = bounded(1);
       s.try_send(10).unwrap();
       assert_eq!(s.try_send(11), Err(TrySendError::Full(11)));

       // 3) TrySendError::Disconnected
       let (s, r) = bounded::<i32>(1);
       drop(r);
       assert_eq!(s.try_send(1), Err(TrySendError::Disconnected(1)));
       // 发送错误带回原消息，可取回：
       assert_eq!(s.try_send(2).unwrap_err().into_inner(), 2);

       // 4) SendTimeoutError::Timeout：满通道 + 极短超时
       let (s, _r) = bounded::<i32>(1);
       let _ = s.try_send(0);
       assert!(s.send_timeout(1, Duration::from_millis(50))
                   .unwrap_err().is_timeout());

       // 5) RecvError：丢弃所有发送端后 recv（通道为空）
       let (s, r) = unbounded::<i32>();
       drop(s);
       assert_eq!(r.recv(), Err(RecvError));

       // 6) TryRecvError::Empty / Disconnected
       let (_s, r) = unbounded::<i32>();
       assert_eq!(r.try_recv(), Err(TryRecvError::Empty));
       let (s, r) = unbounded::<i32>();
       drop(s);
       assert_eq!(r.try_recv(), Err(TryRecvError::Disconnected));

       // 7) RecvTimeoutError::Timeout
       let (_s, r) = unbounded::<i32>();
       assert!(r.recv_timeout(Duration::from_millis(50))
                   .unwrap_err().is_timeout());
   }
   ```

   运行（待本地验证）：`cargo run --example <名字>`。

3. **需要观察的现象**：每个 `assert_eq!`/`assert!` 对应一种错误变体；注意发送错误（`Full(11)`、`Disconnected(1)`）都内嵌了原消息，而接收错误（`Empty`、`Disconnected`、`Timeout`）不带数据。

4. **预期结果**：得到如下「错误触发条件」表（选择族单列）：

   | # | 错误类型 | 触发条件 |
   |---|---------|---------|
   | 1 | `SendError<T>` | `send` 时通道已断开（无接收端） |
   | 2 | `TrySendError::Full` | `try_send` 时通道已满（零容量则表示此刻无接收端） |
   | 3 | `TrySendError::Disconnected` | `try_send` 时通道已断开 |
   | 4 | `SendTimeoutError::Timeout` | `send_timeout` 超时未发出（满或零容量无接收端） |
   | 5 | `RecvError` | `recv` 时通道为空且已断开（无发送端） |
   | 6 | `TryRecvError::Empty` | `try_recv` 时通道为空（零容量则表示此刻无发送端） |
   | — | `TryRecvError::Disconnected` | `try_recv` 时通道为空且已断开 |
   | — | `RecvTimeoutError::Timeout` | `recv_timeout` 超时未收到 |
   | — | `SelectTimeoutError` / `TrySelectError` 等 | `select_timeout` 超时 / `try_select` 无操作就绪 |

5. 待本地验证：`send_timeout`/`recv_timeout` 是否触发取决于线程调度与时长；`Duration::from_millis(50)` 在繁忙机器上一般足够稳定，必要时可调大时长。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SendError`、`TrySendError`、`SendTimeoutError` 都携带 `T`，而 `RecvError` 不携带任何数据？

**参考答案**：发送失败时，消息 `T` 没有被通道消费，必须原样还给调用者（否则就丢了），所以发送族错误都把 `T` 装回来并提供 `into_inner()`。接收失败时本就没有产出任何值，无物可还，故 `RecvError` 是单元结构体。

**练习 2**：公开 `Sender::send` 内部出现 `SendTimeoutError::Timeout(_) => unreachable!()`。为什么这里可以安全地断言「不可达」？

**参考答案**：`send` 调用 flavor 时传的是 `None`（无截止时间），flavor 的 `send` 永远不会因超时而中止，只会成功或返回 `Disconnected`。因此 `Timeout` 分支在 `send` 这条路径上不可能出现，`unreachable!()` 是对这一事实的断言。

**练习 3**：写一个函数 `fn try_or_block(s: &Sender<i32>) -> Result<(), TrySendError<i32>>`，内部用阻塞 `send`，对外返回 `TrySendError`，用 `?` 简化。

**参考答案**：

```rust
// 示例代码
use crossbeam_channel::{Sender, TrySendError};
fn try_or_block(s: &Sender<i32>) -> Result<(), TrySendError<i32>> {
    // send 返回 Err(SendError)，经 From 提升为 TrySendError::Disconnected
    s.send(1).map_err(TrySendError::from)?;
    Ok(())
}
```

`SendError` 经 `From` 提升为 `TrySendError::Disconnected`（[err.rs:172-178](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L172-L178)），所以 `?` 直接生效。

**练习 4**：`SendError<T>` 为什么手写 `Debug` 而不是 `#[derive(Debug)]`？

**参考答案**：derive `Debug` 会要求 `T: Debug`，从而把无关约束强加给所有使用者；而通道传递的 `T` 完全可能不实现 `Debug`。手写 `Debug` 打印固定的 `"SendError(..)"`，使 `SendError<T>: Debug` 对任意 `T` 成立，降低使用门槛。

---

## 5. 综合实践

把本讲的「计数协议」和「错误类型」串起来，完成下面这个「通道生命周期观察器」小任务。

**任务**：用 `bounded(2)` 通道演示从「正常收发」到「断开回收」的完整过程，印证本讲的三个核心结论：① 多端共享同一 `Counter`；② 只有最后一个引用 drop 才触发断开；③ 断开后各类操作返回对应错误。

```rust
// 示例代码：通道生命周期观察器
use std::thread;
use std::time::Duration;
use crossbeam_channel::{
    bounded, Receiver, RecvError, Sender, TryRecvError,
};

struct DropLog(&'static str);
impl Drop for DropLog {
    fn drop(&mut self) { println!("drop: {}", self.0); }
}

fn main() {
    let (s, r): (Sender<DropLog>, Receiver<DropLog>) = bounded(2);

    // ① 多端共享同一通道：clone 出第二发送端交给子线程
    let s2 = s.clone();                          // senders: 1 -> 2
    let worker = thread::spawn(move || {
        let _ = s2.send(DropLog("from-worker"));
        // 子线程返回时 drop(s2)：主线程的 s 还在，通道未断
    });

    thread::sleep(Duration::from_millis(100));

    // ② 主线程仍持有 s，接收端能看到 worker 发来的消息
    assert!(matches!(r.try_recv(), Ok(_)));

    // ③ 主线程 drop 最后一个发送端 → senders 归零 → disconnect_senders
    drop(s);
    worker.join().unwrap();

    // ④ 断开后：接收返回 Disconnected；阻塞 recv 不会卡死，立即返回 RecvError
    assert_eq!(r.try_recv(), Err(TryRecvError::Disconnected));
    assert_eq!(r.recv(), Err(RecvError));

    println!("通道已被最后一个 drop 的引用回收（参见 4.2 的 destroy 裁决）");
}
```

**操作步骤**：

1. 把上述代码放入 `crossbeam-channel/examples/lifecycle.rs`（或独立小 crate）。
2. 运行（待本地验证）：`cargo run -p crossbeam-channel --example lifecycle`。
3. 对照 4.2.3 的销毁链路，在纸面上标注每一步对应 `counter.rs` 的哪一行：`acquire`（clone）、`fetch_sub==1`（最后一个 drop）、`disconnect_senders`（唤醒）、`destroy.swap`（裁决回收）。

**需要观察的现象**：

- `DropLog` 的 drop 日志会按消息被消费/丢弃的时机打印，帮你定位「消息何时释放」。
- worker 线程 drop `s2` 后通道**没有**断开（因为主线程 `s` 还在）；只有主线程也 drop `s` 后，`r.try_recv()` 才返回 `Disconnected`。
- 最后 `r.recv()` 立即返回 `Err(RecvError)`，证明断开后阻塞接收不会卡死——这正是 4.2 里 `disconnect` 唤醒机制的价值。

**预期结果**：程序正常退出，所有断言通过，无内存泄漏（若把 `DropLog` 的 drop 日志累加，应与发送条数一致）。

## 6. 本讲小结

- `Counter<C>` 是通道的**共享内核**：`senders`/`receivers` 两个 `AtomicUsize` 计数、一个 `destroy` 仲裁位、内联的 flavor 通道 `chan`。所有 clone 出来的端都只持有一份 `NonNull<Counter<C>>` 裸指针。
- crossbeam-channel **手写**引用计数而非用 `Arc`：用 `Box::leak` 构造（不自动释放），用 `release` 里的 `Box::from_raw` 回收，从而把「两端各自计数 + 恰好一次销毁」的复杂语义握在自己手里。
- `acquire`（`clone`）用 `Relaxed` 增计数并带溢出 `abort` 保护；`release`（`drop`）用 `AcqRel` 减计数，当旧值为 1 时触发该侧 `disconnect` 回调唤醒对端，再通过 `destroy.swap` 裁决由「后到达者」回收整条通道——保证恰好销毁一次。
- `disconnect_senders`/`disconnect_receivers` 用 `MARK_BIT` 的 `fetch_or` 把「断开」只点亮一次并唤醒对端阻塞操作——这是所有 `Disconnected` 错误的物理来源；接收端断开还会 `discard_all_messages` 急切释放消息。
- `release` 是 `unsafe fn`（调用者必须保证对应一次合法 acquire）；`disconnect` 闭包虽返回 `bool`，但 `release` 丢弃该返回值，释放决策只看 `destroy` 标志。
- `err.rs` 是一张规整的错误矩阵：发送族（`SendError`/`TrySendError`/`SendTimeoutError`）**内嵌并带回原消息**、故手写 `Debug`；接收族（`RecvError`/`TryRecvError`/`RecvTimeoutError`）无数据；选择族四件套（`TrySelectError`/`SelectTimeoutError`/`TryReadyError`/`ReadyTimeoutError`）。阻塞版只有 `Disconnected`；`try_*` 多 `Full`/`Empty`；`*_timeout` 多 `Timeout`。`From` 转换把窄错误嵌入宽错误，公共 `send`/`recv` 据此把 flavor 返回的超时错误收窄，并用 `unreachable!()` 标记不可能出现的 `Timeout` 分支。

## 7. 下一步学习建议

本讲搞清楚了「通道的引用计数与销毁」以及「操作失败时返回什么错误」，但**故意没碰**两个问题：

1. **flavor 内部到底怎么存放消息、怎么做并发收发？** 例如 `disconnect` 用到的 `MARK_BIT`、`tail` 究竟编码了什么。这是下一讲 **u3-l3「flavors 架构与 SelectHandle trait」**的入口——你会看到 `SenderFlavor`/`ReceiverFlavor` 枚举如何把操作路由到 array/list/zero 等具体实现，以及统一的 `SelectHandle` trait。
2. **阻塞的 `send`/`recv` 是怎么「睡着」和「被叫醒」的？** 本讲只提到 `disconnect` 会「唤醒对端」，但唤醒的细节（线程局部阻塞上下文、被阻塞线程的队列）要等到 **u3-l7「Context 与 Waker：阻塞与唤醒机制」**才展开。

建议的阅读顺序：先 u3-l3（flavor 架构总览）→ u3-l4/u3-l5/u3-l6（array/list/zero 三种 flavor 的存储与算法）→ u3-l7（阻塞唤醒）。读 flavor 时，可以回头对照本讲的 `disconnect_senders`/`disconnect_receivers`，体会「计数销毁」与「flavor 内部状态机」是如何在 `release` 这一处汇合的。
