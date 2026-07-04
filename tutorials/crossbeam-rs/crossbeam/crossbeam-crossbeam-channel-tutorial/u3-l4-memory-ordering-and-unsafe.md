# 内存序与 unsafe 的正确性

## 1. 本讲目标

前面 u2-l5 / u2-l6 / u2-l7 已经分别讲清了 array、list、zero 三种 flavor「做什么、怎么做」。本讲换一个视角，只盯一个问题：**它们为什么是对的？** 读完本讲，你应当能够：

- 说清 Rust 原子操作四种内存序（`Relaxed` / `Acquire` / `Release` / `SeqCst`）在 crossbeam-channel 里各自被用在哪、为什么是它而不是另一种；
- 为 array 的 `write` 选 `Release`、`read`/`start_*` 选 `Acquire`、CAS 选 `SeqCst` 写出完整的**可见性推理**（happens-before 链）；
- 把 `src/flavors/list.rs` 里所有 `unsafe` 块按「指针解引用 / `MaybeUninit` 读写 / `Box::from_raw` 堆回收」三类分类，并为每一类说清楚它依赖的不变量；
- 解释 zero flavor 的 `ready: AtomicBool` 是如何充当「消息已就绪」闸门、让 `UnsafeCell<Option<T>>` 安全的；
- 理解 `CachePadded` 防伪共享、`Backoff` 自旋退避这两个「硬件现实」工程细节如何把无锁算法转成实际性能。

本讲是 u2-l5（array）与 u2-l6（list）的进阶补充，不再重复 stamp 编码、state 位、`Block::destroy` 接力等数据结构细节，只聚焦「内存序选择」与「unsafe 边界」这两条正确性主线。

## 2. 前置知识

在分析具体源码前，先用四段话建立「为什么无锁通道特别需要关心内存序」的直觉。

**硬件现实：可见性不是免费的。** 在多核机器上，一个线程对内存的写并不会瞬间被其它线程看见。CPU 有_store buffer_、有缓存层级、有乱序执行。如果你写了一段「先写数据 `msg`，再写标志 `ready=true`」的代码，别的线程完全可能先看到 `ready=true` 再看到 `msg`——因为这两条写指令可能被重排、或以不同速度传播到其它核。要让「看到标志 ⟹ 一定看到数据」成立，必须用**内存序**显式禁止这种重排。这就是 crossbeam-channel 全部并发正确性的出发点。

**Rust 的工具：原子操作 + 内存序。** 标准库的 `AtomicUsize::store(v, Ordering::Release)` 与 `load(Ordering::Acquire)` 是一对「暗号」：当一个线程 `Release` 写、另一个线程 `Acquire` 读到那个值，就在两者之间建立一条 **happens-before（happens-before，简称 hb）** 边——前者在 `Release` 之前的**所有**写（包括对非原子数据的写）都对后者的 `Acquire` 之后的读可见。用公式表达这条「发布—获取」配对：

\[
\text{写}(msg) \;\xrightarrow{\text{hb}}\; \texttt{store}(\textit{flag},\text{Release})
\;\xrightarrow{\text{同步}}\; \texttt{load}(\textit{flag},\text{Acquire})
\;\xrightarrow{\text{hb}}\; \text{读}(msg)
\]

中间的「同步」边把两段 hb 链首尾相连。crossbeam-channel 反复使用的就是这套范式：把「消息本体」写进 `UnsafeCell<MaybeUninit<T>>`（普通写、非原子），再用一个**原子版本号**（array 的 `stamp` / list 的 `state` / zero 的 `ready`）以 `Release` 发布、对端以 `Acquire` 获取。

**为什么还需要 `SeqCst`。** `Acquire`/`Release` 只管「配对的两端」，不保证全局顺序。但有些判断需要多个变量之间「看一幅一致的快照」——比如判断通道是否已满，要同时看 `head` 和 `tail`；判断是否已断开，要让 `mark_bit` 与正在推进的游标有一个全序。这类场景 crossbeam-channel 保守地选了 `SeqCst`（顺序一致），它额外参与一个全局总序，代价是在弱内存架构（ARM/POWER）上更贵，但正确性最稳。本讲会逐一指出这些点。

**`UnsafeCell` + `MaybeUninit` 是「内部可变」的合法外衣。** Rust 默认「共享不可变、可变不共享」。通道要在「多个线程共享 `&Channel`」的前提下修改槽里的消息，必须用 `UnsafeCell` 告诉编译器「这里允许内部可变」；而消息在「写入前 / 读出后」是未初始化的，必须用 `MaybeUninit` 免除自动初始化。两者本身只是「打开逃生通道」，并不提供线程安全——真正的安全要靠外面的原子版本号 CAS 来保证「同一时刻只有一个线程在碰这个槽」。理解了「原子版本号是锁、`UnsafeCell`+`MaybeUninit` 是被锁保护的数据」，本讲的全部 `unsafe` 都能对上号。

> 关键术语回顾：`happens-before`、`Acquire`/`Release`/`SeqCst`/`Relaxed`、`UnsafeCell`、`MaybeUninit`、`stamp`（array 版本号）、`state` 位（list 的 `WRITE`/`READ`/`DESTROY`）、`ready`（zero 闸门）、`CachePadded`、`Backoff`。其中 stamp 编码、state 位、`Block::destroy` 接力已在 u2-l5 / u2-l6 建立，本讲直接使用其结论。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [src/flavors/array.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs) | `stamp` 的 `Release` 写 / `Acquire` 读配对、CAS 的 `SeqCst`、`fence(SeqCst)`、`discard_all_messages` 的 `unsafe`。 |
| [src/flavors/list.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs) | `state` 位的 `Release`/`Acquire`/`AcqRel`、全部 `unsafe` 块的三类分类、`Block::new` 的零分配。 |
| [src/flavors/zero.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs) | `ready: AtomicBool` 闸门、栈/堆 `Packet` 两种路径的 `unsafe`、`Mutex` 与 `ready` 双重同步。 |
| [src/counter.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs) | `release` 的 `AcqRel`——它是 `discard_all_messages` / `Box::from_raw` 安全性的基石（最后一个句柄 acquire 观察到此前所有 drop）。 |
| [src/waker.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs) | `SyncWaker::is_empty` 的 `SeqCst` 双检——`notify` 快速路径的内存序。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1** happens-before 与可见性契约（含 `CachePadded` / `Backoff` 工程铺垫）
- **4.2** array：stamp 作为可见性闸门（`Relaxed`/`Acquire`/`Release`/`SeqCst`/`fence` 全谱）
- **4.3** list：state 位同步与 `unsafe` 三类边界
- **4.4** zero：`ready` 标志同步与 `UnsafeCell` 安全范式

---

### 4.1 happens-before 与可见性契约

#### 4.1.1 概念说明

crossbeam-channel 的三种真实 flavor 共享同一套正确性骨架，可以概括成一句话：

> **消息本体走普通（非原子）写，可见性靠一个原子「版本号」以 Release 发布、对端以 Acquire 获取来保证。**

这个「版本号」在不同 flavor 里叫法不同，但角色完全一致：

| flavor | 版本号字段 | 类型 | 「就绪」语义 |
| --- | --- | --- | --- |
| array | `Slot::stamp` | `AtomicUsize` | stamp 推进到「`head+1` 或 `tail`」表示槽已写/已读 |
| list | `Slot::state` | `AtomicUsize` | `WRITE` 位置位表示已写、`READ` 位置位表示已读 |
| zero | `Packet::ready` | `AtomicBool` | `true` 表示消息已就位/已被取走 |

只要这条「Release 写版本号 ⟷ Acquire 读版本号」的配对成立，被它「夹在前面」的消息写就一定对「配对之后」的消息读可见——`UnsafeCell<MaybeUninit<T>>` 的安全性由此而来。

#### 4.1.2 核心流程

一次无锁收发的可见性流程（以发送方向为例，接收方向对称）：

```text
生产者线程                         消费者线程
──────────                         ──────────
1. CAS 抢到槽位（订座）
2. 写 msg 进 UnsafeCell<MaybeUninit>   （普通写，对其它核尚不可见）
3. 版本号.store(新值, Release)   ──┐
                                   │  happens-before（发布—获取）
4. notify() 唤醒消费者           ──┘   5. 版本号.load(Acquire) 读到「就绪」值
                                        6. 读 msg（此时必可见）
```

关键在第 3 步的 `Release` 与第 5 步的 `Acquire` 配对：它们之间形成的同步边，把第 2 步的普通写「打包」带过线程边界。若把第 3 步错写成 `Relaxed`，这条同步边就断了，消费者可能在第 6 步读到未初始化的内存——这就是本讲要防的 bug。

#### 4.1.3 源码精读：counter.rs 的 release——整条安全性链的基石

在进入三个 flavor 之前，先看一处「看似与可见性无关、实则是地基」的内存序：引用计数的释放。它在 [src/counter.rs:69-77](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L69-L77)：

```rust
pub(crate) unsafe fn release<F: FnOnce(&C) -> bool>(&self, disconnect: F) {
    if self.counter().senders.fetch_sub(1, Ordering::AcqRel) == 1 {
        disconnect(&self.counter().chan);
        if self.counter().destroy.swap(true, Ordering::AcqRel) {
            drop(unsafe { Box::from_raw(self.counter.as_ptr()) });
        }
    }
}
```

注意 `fetch_sub` 用的是 **`AcqRel`**，而不是像 `acquire`（克隆，[src/counter.rs:52](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L52) 的 `fetch_add(1, Relaxed)`）那样用 `Relaxed`。原因正是可见性：

- `fetch_sub` 返回 `1` 意味着「我是本侧最后一个句柄」。作为最后一个，我接下来要调用 `disconnect`（可能去 `discard_all_messages` 逐个 `assume_init_drop` 消息、甚至 `Box::from_raw` 释放整个通道堆内存）。
- `AcqRel` 的 **Acquire** 半边，保证「最后一个」线程观察到了**此前所有其它句柄被 drop 前完成的全部写**——包括别的消费者从槽里读出消息、别的生产者写完消息。没有这条 acquire，最后一个线程可能在别人还在读槽时就去释放堆内存，造成 use-after-free。

这就是 array.rs 里 `disconnect_receivers` 的 `# Safety` 注释所要求的不变量（[src/flavors/array.rs:502-505](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L502-L505)）：

> May only be called once upon dropping the last receiver. The destruction of all other receivers must have been observed with acquire ordering or stronger.

而「observed with acquire ordering」正是由 `counter::release` 的 `fetch_sub(_, AcqRel)` 提供。**counter.rs 的这一行 `AcqRel`，是 array/list 两种 flavor 里所有 `Box::from_raw`、`assume_init_drop` 得以安全的总开关。**

#### 4.1.4 工程正确性：CachePadded 与 Backoff

「内存序」保证**逻辑**正确，「CachePadded / Backoff」保证**性能**不崩，两者都是无锁算法落地的必需品。

**`CachePadded` 防伪共享（false sharing）。** 现代 CPU 以「缓存行」（通常 64 字节）为粒度同步。如果两个线程分别频繁写**不同**字段、但这两个字段恰好落在**同一**缓存行，两核就会反复互相作废对方的缓存行，性能骤降。array 的 `head`（消费者写）与 `tail`（生产者写）正是这种危险关系，所以两者都被包进 `CachePadded`：

```rust
head: CachePadded<AtomicUsize>,   // src/flavors/array.rs:68
tail: CachePadded<AtomicUsize>,   // src/flavors/array.rs:77
```

`CachePadded` 把字段填充到独立缓存行，让生产/消费两核各写各的行、互不干扰。list 同理，`head`/`tail` 是 `CachePadded<Position<T>>`（[src/flavors/list.rs:179](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L179)）。

**`Backoff` 自旋退避。** CAS 失败时若死命重试，会狂打缓存行与内存总线。`Backoff` 在失败后先 `spin()`（忙等若干周期，适合很快会成功的情形），多次失败后升级为 `snooze()`（提示 CPU「我在自旋等待」，可让出超线程资源、降低功耗）。例如 array 的 `start_send` 在 CAS 失败后 `backoff.spin()`（[src/flavors/array.rs:191](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L191)），在「等别人更新 stamp」的分支用 `backoff.snooze()`（[src/flavors/array.rs:208](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L208)）。`Backoff` 只影响「等多久」，不影响正确性。

#### 4.1.5 代码实践

1. **实践目标**：亲手验证「不配对就出 bug」的直觉，理解 Release/Acquire 不可省。
2. **操作步骤**：这是一个**源码阅读型实践**，不必运行。打开 [src/counter.rs:69-77](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L69-L77)，把 `fetch_sub` 的 `Ordering::AcqRel` 改成 `Ordering::Relaxed`（**仅在你的本地副本上改，勿提交**），然后回答：此时若线程 A（最后一个 receiver）正在 `discard_all_messages` 里 `assume_init_drop`，线程 B（另一个还没完全 drop 完的 receiver）可能正在 `read` 同一个槽——`Relaxed` 能否阻止 A 看到的「B 已读完」是过期判断？
3. **需要观察的现象**：推理出「`Relaxed` 不建立 happens-before，A 无法可靠观察到 B 的 `read` 已完成」。
4. **预期结果**：理论上会出现「A 把还在被 B 读的槽当成已读空、提前 `Box::from_raw` 释放整个通道」的数据竞争。这正是该处必须 `AcqRel` 的理由。**待本地验证**（这种数据竞争在普通测试里几乎不会复现，需要 `loom`/`ThreadSanitizer` 级别的工具，u3-l8 会提及并发测试）。
5. 把你的推理写成一两句 happens-before 链。

#### 4.1.6 小练习与答案

**练习 1**：`counter::acquire`（克隆）用 `Relaxed`，而 `release`（析构）用 `AcqRel`。为什么克隆可以「松」、析构必须「紧」？

> **答**：克隆只是 `+1` 计数，不发布任何需要被别人看见的数据，计数本身的最终一致性足够（即使短暂读到旧值，再多自旋/CAS 一次即可），故 `Relaxed`。析构的最后一个句柄要负责 `disconnect` 与释放堆内存，必须看到「此前所有句柄在 drop 前完成的操作」，需要 Acquire 来同步；同时又要把「本侧计数归零」这个事实发布给对侧（对侧 release 时据此决定谁来 `Box::from_raw`），需要 Release。故 `AcqRel`。

**练习 2**：`CachePadded` 改变的是正确性还是性能？去掉它会出内存安全问题吗？

> **答**：只改变性能（避免 `head`/`tail` 的伪共享），不改变正确性——去掉它程序依然符合内存模型、依然正确，只是多核下因缓存行反复失效而变慢。它是「把无锁算法变成高性能算法」的工程细节，性能量化见 u3-l7。

---

### 4.2 array：stamp 作为可见性闸门

#### 4.2.1 概念说明

array flavor（`bounded(cap>0)`）的每个槽 `Slot<T>` 有两个字段（[src/flavors/array.rs:30-37](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L30-L37)）：

```rust
struct Slot<T> {
    stamp: AtomicUsize,                      // 版本号：可见性闸门
    msg: UnsafeCell<MaybeUninit<T>>,         // 消息本体：被闸门保护的数据
}
```

整条正确性链条就是围绕 `stamp` 展开的：**写消息（普通写）后用 `Release` 写 stamp；读 stamp 用 `Acquire`，读到「就绪」值才能动消息**。stamp 还兼任「这个槽现在归谁」的占有权标志——只有 CAS 把游标推进、且 stamp 与游标「对上暗号」的线程才有权碰这个槽（u2-l5 已详述编码，此处不重复）。

#### 4.2.2 核心流程：两条对称的可见性链

array 的发送与接收是**对称**的两条 happens-before 链：

**生产者 → 消费者（消息可见性）：**

\[
\text{write}(msg) \;\xrightarrow{\text{hb}}\;
\texttt{stamp.store}(\textit{tail+1},\text{Release})
\;\xrightarrow{\text{同步}}\;
\texttt{stamp.load}(\text{Acquire})_{\text{消费端}}
\;\xrightarrow{\text{hb}}\;
\text{read}(msg)
\]

**消费者 → 生产者（槽位可回收性）：**

\[
\text{read}(msg) \;\xrightarrow{\text{hb}}\;
\texttt{stamp.store}(\textit{head+lap},\text{Release})
\;\xrightarrow{\text{同步}}\;
\texttt{stamp.load}(\text{Acquire})_{\text{生产端}}
\;\xrightarrow{\text{hb}}\;
\text{overwrite}(msg)
\]

第二条链保证：下一圈的生产者要覆盖这个槽前，一定看得到上一圈消费者已经把消息读走了，从而不会踩踏正在被读的数据。

#### 4.2.3 源码精读：每一处内存序的理由

**`write`（写消息）**——[src/flavors/array.rs:215-230](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L215-L230)：

```rust
pub(crate) unsafe fn write(&self, token: &mut Token, msg: T) -> Result<(), T> {
    if token.array.slot.is_null() { return Err(msg); }       // 断开：无槽
    let slot: &Slot<T> = unsafe { &*token.array.slot.cast::<Slot<T>>() };
    unsafe { slot.msg.get().write(MaybeUninit::new(msg)) }   // ① 普通写消息
    slot.stamp.store(token.array.stamp, Ordering::Release);  // ② Release 发布
    self.receivers.notify();                                 // ③ 唤醒消费者
    Ok(())
}
```

- ① 是对 `MaybeUninit` 的**普通写**，此刻对其它核**不可见**。
- ② `stamp.store(Release)` 是**发布**：任何以 `Acquire` 读到这个新 stamp 的线程，因 happens-before 必能看到 ① 写的消息。**这是整条链的关键，绝不能换成 `Relaxed`。**
- ③ 唤醒一个睡眠的消费者（`notify` 的内存序见 4.2.4）。

**`start_recv`（抢槽 + 读 stamp）**——关键两行在 [src/flavors/array.rs:245-248](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L245-L248)：

```rust
let stamp = slot.stamp.load(Ordering::Acquire);   // 配对 write 的 Release
if head + 1 == stamp { /* 槽已就绪，可 CAS 推进 head */ }
```

`Acquire` 读到满足 `head+1 == stamp` 的值，意味着 `write` 的 `Release` 已生效，消息可见。**`read`** 随后在 [src/flavors/array.rs:315-316](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L315-L316) 取走消息，并以 `Release` 写回新 stamp（第二条链的发布）：

```rust
let msg = unsafe { slot.msg.get().read().assume_init() };
slot.stamp.store(token.array.stamp, Ordering::Release);   // 发布「槽已读，可回收」
```

**CAS 为什么用 `SeqCst`。** `start_send` 推进 tail 的 CAS 在 [src/flavors/array.rs:177-182](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L177-L182)：

```rust
match self.tail.compare_exchange_weak(
    tail, new_tail, Ordering::SeqCst, Ordering::Relaxed,
) { ... }
```

成功侧用 `SeqCst` 而非常见的 `Acq/Rel`，原因是 `tail` 同时承载了**断开标志 `mark_bit`**。断开由 `disconnect_*` 的 `fetch_or(mark_bit, SeqCst)`（[src/flavors/array.rs:488](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L488) 与 [507](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L507)）置位。「一个正在 push 的线程」与「一个正在置 mark 的断开线程」必须争用一个全局顺序：要么 push 先（这次发送有效）、要么断开先（这次发送应失败）。`SeqCst` 提供这个唯一总序，保证 mark 不会被「丢」在两次 CAS 之间。失败侧用 `Relaxed`，因为失败只是重试，所需的同步在循环顶部的 `stamp.load(Acquire)` 已经拿到。

**`fence(SeqCst)` 的「快照」作用。** 在判满/判空的分支里有一处易被忽略的栅栏，[src/flavors/array.rs:194-200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L194-L200)：

```rust
} else if stamp.wrapping_add(self.one_lap) == tail + 1 {
    atomic::fence(Ordering::SeqCst);                // ← 栅栏
    let head = self.head.load(Ordering::Relaxed);   // ← 普通读
    if head.wrapping_add(self.one_lap) == tail { return false; }  // 满
    ...
}
```

这里先 `Acquire` 读了 `stamp`（循环顶部），再用 `fence(SeqCst)` 之后再 `Relaxed` 读 `head`。栅栏的作用是**阻止 `head` 的读被重排到 `stamp` 读之前**，让「判满」看到的是一幅「stamp 之后」的 head 快照，从而既不误判满（丢一次合法 push）、也不误判非满（多塞一条导致溢出踩踏）。`start_recv` 判空处有完全对称的栅栏（[src/flavors/array.rs:277-282](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L277-L282)）。

#### 4.2.4 源码精读：len / is_empty / is_full 与 notify 的 SeqCst

状态查询方法 `len` / `is_empty` / `is_full` 全部用 `SeqCst` 读 `head` 与 `tail`（如 [src/flavors/array.rs:583-592](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L583-L592)、[595-604](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L595-L604)）。它们返回的是「某一瞬间的快照」，用 `SeqCst` 是为了在「同时读两个游标」时尽量拿到一致视图。注意：这些方法文档已声明「返回值是瞬间快照、不可用于关键同步决策」（u1-l3 已述），`SeqCst` 只是把「不一致」的概率压到最低，并非保证严格一致。

`SyncWaker::notify`（[src/waker.rs:225-237](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L225-L237)）用 `is_empty` 的 `SeqCst` 做**双重检查**（double-checked）：先无锁 `load(SeqCst)`，若显示「有阻塞者」才上锁再查一次。`SeqCst` 保证「生产者写消息 + stamp(Release)」与「`notify` 读 `is_empty`」之间有足够同步，避免「消息已写、但 notify 误判无阻塞者而跳过唤醒」的丢失唤醒。

#### 4.2.5 代码实践（对应实践任务第 1 部分）

1. **实践目标**：为 array 的 `write` 选 `Release`、`read`/`start_recv` 选 `Acquire` 写出可见性理由。
2. **操作步骤**：打开 [src/flavors/array.rs:215-230](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L215-L230)（`write`）与 [src/flavors/array.rs:245-248](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L245-L248)、[315-316](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L315-L316)（`start_recv`/`read`），按下面格式补全推理。
3. **需要观察的现象**：能画出两条对称的 happens-before 链，并指出若改序会断在哪条边上。
4. **预期结果**（参考答案）：

   > **`write` 用 `Release` 的理由**：`write` 先把 `msg` 普通写进 `MaybeUninit`，再 `stamp.store(Release)`。`Release` 的语义是「本次 store 之前的所有写（即 msg 的写）对任何以 `Acquire` 读到该 stamp 的线程可见」。若改成 `Relaxed`，则不建立同步边，消费者可能读到 stamp「就绪」却看到未初始化的 `msg`——未定义行为。
   >
   > **`start_recv`/`read` 用 `Acquire` 的理由**：消费者以 `stamp.load(Acquire)` 读取，与 `write` 的 `Release` 配对；读到 `head+1 == stamp` 即获得 happens-before，随后 `read` 读 `msg` 必然看到生产者写入的值。`read` 末尾再 `stamp.store(Release)` 发布「槽已读」，让下一圈生产者的 `start_send` 中 `stamp.load(Acquire)`（[src/flavors/array.rs:162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L162)）确信槽可安全覆盖。

5. 若想亲眼验证，可在本地副本把 `write` 的 `Release` 改 `Relaxed` 跑 `cargo test`——**待本地验证**：单线程下几乎必过，多线程高并发下偶发崩溃或读到错值（这类 bug 难稳定复现）。

#### 4.2.6 小练习与答案

**练习 1**：`start_send` 里 `self.tail.load(Ordering::Relaxed)`（[src/flavors/array.rs:145](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L145)）为什么可以「松」？可见性从哪里补回来？

> **答**：`tail` 的 `Relaxed` 读只是「找一个候选值去试探」，真正的正确性不依赖它——后面有 `stamp.load(Acquire)` 校验槽状态、有 `SeqCst` CAS 保证只有胜者推进。即便读到陈旧的 `tail`，要么 stamp 对不上（重试），要么 CAS 失败（重试），不会产生错误行为。可见性由 stamp 的 `Acquire` 与 CAS 的 `SeqCst` 补回来，所以 `tail` 的初读可以放 `Relaxed` 省开销。

**练习 2**：判满处的 `fence(SeqCst)` 之后用 `head.load(Relaxed)`，而不是直接 `head.load(SeqCst)`。这两种写法在语义上有何差别？

> **答**：`fence(SeqCst)` + `Relaxed` load 是一种已知模式：栅栏参与全局 SeqCst 总序、阻止该 load 被重排到栅栏之前（即排在前面的 stamp `Acquire` 读之前），从而拿到「stamp 之后」的一致 head 快照；但该 load 本身不额外计入 SeqCst 总序，在某些弱内存架构上比「直接 `SeqCst` load」略轻。两者都能保证这里的判满正确性，crossbeam 选择了前者。

---

### 4.3 list：state 位同步与 unsafe 三类边界

#### 4.3.1 概念说明

list flavor（`unbounded()`）的槽结构与 array 同构，只是「版本号」换成了「状态位」`state`（[src/flavors/list.rs:50-56](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L50-L56)）：

```rust
struct Slot<T> {
    msg: UnsafeCell<MaybeUninit<T>>,
    state: AtomicUsize,     // WRITE=1 已写 | READ=2 已读 | DESTROY=4 待销毁
}
```

三个常量在 [src/flavors/list.rs:34-36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L34-L36) 定义。可见性机制与 array 完全同构：`write` 写消息后用 `Release` 置 `WRITE` 位，接收方用 `Acquire` 等 `WRITE` 位。本节的重点是**把 list.rs 全部 `unsafe` 块分类**，看清每类依赖什么不变量。

#### 4.3.2 核心流程：state 位的发布—获取

发送可见性链（[src/flavors/list.rs:312-313](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L312-L313)）：

```rust
unsafe { slot.msg.get().write(MaybeUninit::new(msg)) }   // 普通写消息
slot.state.fetch_or(WRITE, Ordering::Release);           // Release 发布「已写」
```

接收方在 `wait_write` 里 `Acquire` 轮询 `WRITE` 位（[src/flavors/list.rs:60-65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L60-L65)）：

```rust
fn wait_write(&self) {
    let backoff = Backoff::new();
    while self.state.load(Ordering::Acquire) & WRITE == 0 { backoff.snooze(); }
}
```

`Acquire` 看到 `WRITE` ⟹ 消息可见，随后 `read`（[src/flavors/list.rs:417](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L417)）才安全 `assume_init()` 取走。

**销毁接力的 `AcqRel`。** `read` 末尾有一处关键内存序（[src/flavors/list.rs:421-427](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L421-L427)）：

```rust
unsafe {
    if offset + 1 == BLOCK_CAP {
        Block::destroy(block, 0);
    } else if slot.state.fetch_or(READ, Ordering::AcqRel) & DESTROY != 0 {
        Block::destroy(block, offset + 1);   // 接力销毁
    }
}
```

`fetch_or(READ, AcqRel)` 一箭双雕：**Release** 半边把自己的 `READ` 发布出去（让并发的 `Block::destroy` 看到本槽已读完、可以释放）；**Acquire** 半边读取是否已有别人设置了 `DESTROY`（若有，自己接手销毁整个 block）。这正是 u2-l6 讲过的「DESTROY 接力」——这里补充它的内存序依据：必须 `AcqRel` 才能同时完成「发布我已读 + 获取待销毁信号」。

#### 4.3.3 源码精读：CAS 内存序与 array 的细微差别

list 的游标 CAS 在成功侧同样用 `SeqCst`，但**失败侧用 `Acquire`**（[src/flavors/list.rs:273-278](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L273-L278)），与 array 的「失败 `Relaxed`」不同：

```rust
match self.tail.index.compare_exchange_weak(
    tail, new_tail, Ordering::SeqCst, Ordering::Acquire,
) { ... }
```

差别原因：list 的 CAS 失败后会直接使用返回的新 `tail` 值继续操作，需要立即看到「推进到该 tail 的那个线程」此前完成的写（比如它新装的 `block`、新写的消息），所以失败侧也要 `Acquire`。array 失败后只是回到循环顶部重读 stamp（那里有 `Acquire`），故失败侧可 `Relaxed`。这是一处值得回味的微调。

另外，安装首个 block 的 CAS 用的是 **`Release`/`Relaxed`** 而非 `SeqCst`（[src/flavors/list.rs:254-258](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L254-L258)）：因为 block 指针的发布只需要「安装后读者能看到完整初始化的 block」，`Release` 配合随后的 `head.block.store(.., Release)`（[src/flavors/list.rs:260](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L260)）与读者的 `block.load(Acquire)` 即可，不涉及 `mark_bit` 争用，无需 `SeqCst`。

#### 4.3.4 代码实践：list.rs 全部 unsafe 三类分类（对应实践任务第 2 部分）

1. **实践目标**：把 `src/flavors/list.rs` 中所有 `unsafe` 块按「指针解引用 / `MaybeUninit` 读写 / `Box::from_raw`（堆回收）」三类分类，说明各自必要性。
2. **操作步骤**：通读 list.rs，按下表逐项核对（行号已给出）。重点理解每类 unsafe 依赖的不变量。
3. **需要观察的现象**：能指出每处 unsafe「为什么这里编译器无法自动证明安全、必须由人担保」。
4. **预期结果**（参考分类）：

   **第一类：指针解引用 / 原子指针转具体类型（编译器无法证明指针非空、对齐、存活）**
   - [src/flavors/list.rs:100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L100) `Box::from_raw(ptr.as_ptr().cast())`——把分配器返回的裸指针「认领」为 `Box<Block>`，前提是 `Global::allocate_zeroed` 确实返回了正确布局的内存。
   - [src/flavors/list.rs:124](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L124) `(*this).slots.get_unchecked(i)`（`Block::destroy` 内）——裸指针解引用 + 越界免检，靠 `i < BLOCK_CAP-1` 保证。
   - [src/flavors/list.rs:311](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L311)、[415](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L415) `(*block).slots.get_unchecked(offset)`——同上，靠 `start_send`/`start_recv` 保证 `offset < BLOCK_CAP`。
   - [src/flavors/list.rs:632](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L632)、[690](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L690) 丢弃消息路径里的 `(*block).slots.get_unchecked(offset)`。

   **第二类：`MaybeUninit` 初始化 / 读出 / 原地析构（编译器不知道槽当前是否持有有效 `T`）**
   - [src/flavors/list.rs:312](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L312) `slot.msg.get().write(MaybeUninit::new(msg))`——**初始化**槽，前提是此时槽确为「空」（由 CAS 抢到的占有权 + state 无 WRITE 保证）。
   - [src/flavors/list.rs:417](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L417) `slot.msg.get().read().assume_init()`——**读出**消息，前提是 `wait_write` 已 `Acquire` 看到 `WRITE`（消息已发布）。
   - [src/flavors/list.rs:634](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L634)、[691](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L691) `(*slot.msg.get()).assume_init_drop()`——**原地析构**未取走的消息，前提是断开且本槽确实有未读消息（由 `wait_write` 等到 `WRITE` 保证）。
   - 不变量核心：**`state` 的 `WRITE`/`READ` 位 + CAS 占有权共同决定了「槽此刻是否持有有效 `T`」**。只要遵守「写前确保空、读/drop 前确保已写」，`MaybeUninit` 就不会产生未定义行为。

   **第三类：`Box::from_raw` / `Box::into_raw`（堆内存所有权回收——编译器无法证明这是一次性、不重复释放）**
   - [src/flavors/list.rs:252](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L252) `Box::into_raw(Block::new())`——把 block 所有权交给共享结构。
   - [src/flavors/list.rs:263](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L263) `Box::from_raw(new)`——首块 CAS 失败时**收回**自己刚分配却没装上的 block（避免泄漏）。
   - [src/flavors/list.rs:136](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L136) `Block::destroy` 末尾 `drop(Box::from_raw(this))`——无人占用时释放整块。
   - [src/flavors/list.rs:639](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L639)、[648](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L648)（`discard_all_messages`）、[695](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L695)、[704](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L704)（`Drop`）——逐块释放链表。不变量：**DESTROY 接力保证每个 block 恰好被释放一次**（u2-l6 已述），且这些释放只发生在「已无读者/写者占用」时（由 `READ`/`WRITE` 位与 counter 的 acquire 顺序保证）。

5. **若无法确认某处**：`Block::new` 的零初始化（[src/flavors/list.rs:90-105](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L90-L105)）依赖「AtomicPtr/AtomicUsize/MaybeUninit 均可零初始化」这一前提，注释 [src/flavors/list.rs:94-99](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L94-L99) 已逐条论证，属于第二类与第三类的交叉点，可标注「待确认细节」后细读注释。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `read` 末尾用 `fetch_or(READ, AcqRel)` 而不是先 `load(Acquire)` 再 `store(Release)` 两步？

> **答**：两步之间存在窗口——`load` 与 `store` 之间别的线程可能改变 `state`（比如设置 `DESTROY`），导致「我看到的」与「我写回的」不一致，接力销毁的判断会出错。`fetch_or` 是**单条原子读—改—写**，把「读旧值判断 DESTROY」与「写回 READ」合成一个不可分割的操作，`AcqRel` 同时满足「发布 READ」与「获取 DESTROY」，是唯一正确的写法。

**练习 2**：`Block::new` 用 `Global::allocate_zeroed` 零分配后再 `Box::from_raw`，为什么不直接 `Box::new(Block{...})`？

> **答**：`Box::new` 会先在栈上构造整个 `Block`（含 `BLOCK_CAP=31` 个 `Slot`，每个含 `MaybeUninit<T>`），再逐字节拷贝到堆，对大 `T` 既慢又产生短暂的双份内存。零分配直接在堆上划零、原地认领，省去拷贝；代价是必须 `unsafe` 担保「零值对每个字段都是合法初值」——这正是注释 [src/flavors/list.rs:94-99](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L94-L99) 逐条列出的理由（AtomicPtr 可零、MaybeUninit 可零、AtomicUsize 可零）。

---

### 4.4 zero：ready 标志同步与 UnsafeCell 安全范式

#### 4.4.1 概念说明

zero flavor（`bounded(0)`）不缓冲任何消息，靠一次性 `Packet` 完成会合（rendezvous）。它的 `Packet` 用一个**布尔**闸门代替 array/list 的数值版本号（[src/flavors/zero.rs:41-50](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L41-L50)）：

```rust
struct Packet<T> {
    on_stack: bool,                  // 区分栈/堆分配
    ready: AtomicBool,               // 可见性闸门
    msg: UnsafeCell<Option<T>>,      // 消息本体
}
```

`ready` 是**双向复用**的信号灯：「谁把消息带进 packet，谁就在写完后置 `ready=true` 发布；另一方 `Acquire` 等 `ready` 后再读」。可见性同样靠 Release/Acquire 配对，与 array/list 同构，只是信号退化为单 bit。

#### 4.4.2 核心流程：两条路径、两个同步源

zero 有两条 packet 路径，取决于「谁把消息带进 packet」：

```text
路径 A（send 阻塞）：发送方带消息 (message_on_stack)
  发送方: 建 packet(含 msg) → 注册 → park
  接收方: 配对取走 packet → read(msg) → ready.store(Release)
  发送方: 醒来 → wait_ready(Acquire) → 确认对方取走 → drop packet

路径 B（recv 阻塞）：接收方带空 packet (empty_on_stack)
  接收方: 建 packet(空) → 注册 → park
  发送方: 配对取走 packet → write(msg) → ready.store(Release)
  接收方: 醒来 → wait_ready(Acquire) → read(msg)
```

关键认知：zero 的同步来自**两个源**——

1. **`Mutex<Inner>`**：配对（`try_select` + `store_packet`）在持锁期间完成，锁本身的 acquire/release 保证了「packet 指针的交接」与「注册时已写入的内容」可见。这就是为什么路径 A 里接收方一拿到 packet 就能直接读 msg（消息在注册时已存在、锁已同步）。
2. **`ready: AtomicBool`**：用于「配对之后、对方尚未写完」的情形（路径 B 的写消息、路径 A 的「确认取走」）。此时锁已释放，必须靠 `ready` 的 Release/Acquire 建立跨线程可见性。

#### 4.4.3 源码精读：write / read / wait_ready

**`write`（发送方写消息）**——[src/flavors/zero.rs:150-160](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L150-L160)：

```rust
pub(crate) unsafe fn write(&self, token: &mut Token, msg: T) -> Result<(), T> {
    if token.zero.0.is_null() { return Err(msg); }                // 断开
    let packet = unsafe { &*(token.zero.0 as *const Packet<T>) };
    unsafe { packet.msg.get().write(Some(msg)) }                  // ① 普通写
    packet.ready.store(true, Ordering::Release);                  // ② 发布
    Ok(())
}
```

① 写 `Some(msg)` 进 `UnsafeCell<Option<T>>`；② `Release` 发布。配对端 `wait_ready` 以 `Acquire` 等到 `true` 后即可见 msg。

**`wait_ready`**——[src/flavors/zero.rs:81-86](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L81-L86)：

```rust
fn wait_ready(&self) {
    let backoff = Backoff::new();
    while !self.ready.load(Ordering::Acquire) { backoff.snooze(); }
}
```

**`read`（接收方读消息）**——[src/flavors/zero.rs:179-202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L179-L202)，分两支：

```rust
if packet.on_stack {
    // 路径 A：消息在注册时已存在（锁已同步），直接取走，再置 ready 通知发送方「我拿了」
    let msg = unsafe { packet.msg.get().replace(None).unwrap() };
    packet.ready.store(true, Ordering::Release);
    Ok(msg)
} else {
    // 堆 packet（select 路径）：等对方写完，再取走并回收堆内存
    packet.wait_ready();
    let msg = unsafe { packet.msg.get().replace(None).unwrap() };
    drop(unsafe { Box::from_raw(token.zero.0.cast::<Packet<T>>()) });
    Ok(msg)
}
```

注意路径 A（栈 packet）里接收方置 `ready` 是为了**告诉发送方「消息已被取走，你可以安全销毁栈 packet 了」**——发送方在 `Selected::Operation` 分支会 `packet.wait_ready()`（[src/flavors/zero.rs:274](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L274)）。若没有这条 Release/Acquire，发送方可能在接收方还没读完时就返回并销毁栈 packet，造成栈上 use-after-free。

#### 4.4.4 源码精读：为什么 `UnsafeCell<Option<T>>` 安全

把 zero 的安全性归纳为三条不变量：

1. **配对互斥**：`Mutex<Inner>` 保证同一 packet 在同一时刻只被「一对」收发线程经由 `try_select` 配对，packet 指针的交接在锁内完成。
2. **写后发布**：写消息方先写 `msg`、再 `ready.store(Release)`；读消息方先 `ready.load(Acquire)` 看到 `true`、再读 `msg`。Release/Acquire 配对保证「读 msg 时消息已可见」。
3. **取走后再释放**：销毁 packet（栈：由所有者线程 `wait_ready` 后随栈帧回收；堆：由 `Box::from_raw` 在确认 `ready` 后回收）一律发生在「对方已通过 `ready` 声明完成」之后。

三条共同保证：**任一时刻对 `msg` 的访问（写、读、replace、drop）只有一方在进行**——这正是 `UnsafeCell` 得以安全的前提。`Option<T>` 的 `None` 还提供了「已取走」的显式状态，`replace(None).unwrap()` 在逻辑出错时会 panic，相当于一道运行时断言。

`unsafe` 块集中在：裸指针转类型（`&*(... as *const Packet<T>)`，[src/flavors/zero.rs:156](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L156)、[185](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L185)）、`UnsafeCell` 内部写读（[157](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L157)、[191](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L191)、[198](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L198)）、以及堆 packet 的 `Box::into_raw`/`Box::from_raw`（select 的 register/unregister，[src/flavors/zero.rs:403](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L403)、[416](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L416)、[199](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L199)）。分类与 list 完全同构，只是数据从 `MaybeUninit<T>` 变成了 `Option<T>`。

#### 4.4.5 代码实践

1. **实践目标**：验证「`ready` 闸门不可省」——理解去掉它会破坏哪条不变量。
2. **操作步骤**：阅读 [src/flavors/zero.rs:225-279](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L225-L279)（阻塞 `send`）。在本地副本上把 `Selected::Operation` 分支里的 `packet.wait_ready()`（[src/flavors/zero.rs:274](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L274)）注释掉（**勿提交**），推理会发生什么。
3. **需要观察的现象**：发送方不再等待接收方「取走」的信号，可能在接收方执行 `packet.msg.get().replace(None)`（[src/flavors/zero.rs:191](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L191)）之前就让 `send` 返回、栈帧 `packet` 析构。
4. **预期结果**：接收方随后访问的 packet 已是「已析构的栈内存」——栈上 use-after-free，未定义行为。**待本地验证**（同样难稳定复现，可用 `MIRI` 跑相关测试观察报错）。
5. 进阶：对比 select 路径的堆 packet——为何 `unregister` 里能安全 `Box::from_raw`（[src/flavors/zero.rs:416](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L416)）？答：select 的 register/unregister 严格成对、且 packet 在未被配对时只被注册线程持有，回收权清晰（u2-l7、u3-l2 已述）。

#### 4.4.6 小练习与答案

**练习 1**：zero 用 `Option<T>`，array/list 用 `MaybeUninit<T>`。两者在安全性思路上有何异同？

> **答**：都是「在 `UnsafeCell` 里放一个可能未初始化/已取走的值，靠外部原子保证独占访问」。差别在表达手段：`MaybeUninit<T>` 把「是否有效」完全交给外部的 stamp/state 位，类型系统不参与；`Option<T>` 的 `None`/`Some` 让「已取走」成为显式值，`replace(None).unwrap()` 多一道运行时断言。zero 因为会合语义下 packet 生命周期短、状态简单，`Option` 更直观；array/list 要在环形/链表里反复复用槽位、关心初始化开销，`MaybeUninit` 更合适。

**练习 2**：路径 A 里接收方明明可以直接读 msg（锁已同步可见），为什么还要在读完置 `ready.store(Release)`？

> **答**：那不是给接收方自己用的，是**给发送方用的「取走确认」**。发送方带消息进 packet 后 park，醒来后必须等接收方读完才能销毁（栈）packet 或返回。`ready` 的 Release/Acquire 在「锁已释放」之后建立可见性，告诉发送方「消息已被安全取走，你可以回收 packet 了」。少了这一步，发送方可能在接收方读取期间回收栈 packet。

---

## 5. 综合实践

把四个模块串起来，做一个「内存序与 unsafe 边界审计」的源码阅读任务。这不是写新程序，而是用一张表把三种 flavor 的关键点对齐，检验你是否真的读懂了。

**任务**：对照下表，逐格填出「可见性发布点 / 获取点 / 安全性所依赖的不变量」，行号必须来自真实源码（参考答案在表后）。

| flavor | 发布点（Release 写版本号） | 获取点（Acquire 读版本号） | 让 `unsafe` 安全的不变量 |
| --- | --- | --- | --- |
| array | `write` 内 `stamp.store(Release)` | `start_recv`/`start_send` 内 `stamp.load(Acquire)` | stamp 与游标「对暗号」者独占槽；最后一个 receiver 经 counter `AcqRel` 观察到此前所有 drop |
| list | `write` 内 `state.fetch_or(WRITE, Release)` | `wait_write` 内 `state.load(Acquire)` | CAS 占有权 + WRITE/READ 位决定槽是否持有有效 T；DESTROY 接力保证 block 恰好释放一次 |
| zero | `write`/`read` 内 `ready.store(Release)` | `wait_ready` 内 `ready.load(Acquire)` | `Mutex` 串行配对 + ready 闸门保证任一时刻只有一方碰 msg |

**示例代码**（可选，验证 array 可见性链的最小复现，需在自建项目运行；下面只演示「正常工作」的正确用法，验证「改序会坏」需如 4.2.5 所述改源码副本）：

```rust
// 示例代码：多生产多消费下验证 bounded 通道的正确性（依赖正确的内存序）
use crossbeam_channel::{bounded, unbounded};
use std::thread;

fn main() {
    // array flavor：bounded(cap>0)，验证 Release/Acquire 配对下的消息可见性
    let (s, r) = bounded::<u64>(4);
    let mut handles = Vec::new();
    for p in 0..4 {
        let s = s.clone();
        handles.push(thread::spawn(move || {
            for i in 0..1000 { let _ = s.send((p as u64) << 32 | i); }
        }));
    }
    drop(s);
    // 单 receiver 收齐 4000 条，任何消息错乱/未初始化都会让求和错
    let mut sum: u64 = 0;
    while let Ok(v) = r.recv() { sum = sum.wrapping_add(v); }
    for h in handles { h.join().unwrap(); }
    println!("array sum = {sum}");

    // list flavor：unbounded，对照无界路径
    let (s2, r2) = unbounded::<u64>();
    let h = thread::spawn(move || {
        let mut s = 0u64;
        while let Ok(v) = r2.recv() { s = s.wrapping_add(v); }
        s
    });
    for i in 0..1000 { let _ = s2.send(i); }
    drop(s2);
    println!("list sum = {}", h.join().unwrap());
}
```

**结合源码理解**：

1. **4.1**：每一个成功的收发都隐含「counter `release` 的 `AcqRel` 已为最终的清理兜底」。
2. **4.2**：array 的 `bounded(4)` 下，4 个生产者并发 `send`，每条都走 `start_send`(CAS 推进 tail) → `write`(Release 写 stamp) → `receivers.notify()`；receiver 的 `recv` 走 `start_recv`(Acquire 读 stamp) → `read`(取消息 + Release 写 stamp)。求和正确 = 可见性链成立。
3. **4.3**：list 的 `unbounded` 下，发送方永不阻塞（恒就绪），每条走 `start_send` → `write`(Release 置 WRITE)；接收方 `wait_write`(Acquire 等 WRITE) → `read`，并在块尾走 DESTROY 接力（`AcqRel`）回收 block。
4. **4.4**：若把 `bounded(4)` 换成 `bounded(0)`（zero），则每次收发都走会合 packet，靠 `ready` 闸门 + `Mutex` 配对完成。

阅读时建议同时打开三个 flavor 文件与本讲表格对照，沿「写消息 → Release 发布版本号 → 对端 Acquire 获取 → 读消息」这条主线走一遍，把每处 `unsafe` 都对上「它依赖哪个不变量」。

## 6. 本讲小结

- crossbeam-channel 的并发正确性骨架是：**消息走普通写、可见性靠原子「版本号」以 Release 发布 / Acquire 获取来保证**——array 是 `stamp`、list 是 `state` 位、zero 是 `ready`。
- array 的 `write` 用 `Release` 写 stamp、`start_recv`/`read` 用 `Acquire` 读 stamp，形成两条对称的 happens-before 链，既保证消息可见，又保证下一圈生产者不踩踏正在被读的槽——这是 `UnsafeCell<MaybeUninit<T>>` 安全的依据。
- CAS 的成功侧普遍用 **`SeqCst`**，是为了让 `mark_bit`（断开）与游标推进争用一个全局总序；判满/判空处的 `fence(SeqCst)` 配合随后的 `Relaxed` 读，拿到「stamp 之后」的 consistent 快照；list 的 CAS 失败侧用 `AcqRel` 的 `Acquire` 半边直接同步（与 array 的 `Relaxed` 失败形成对比）。
- list 的全部 `unsafe` 可归三类：**指针解引用**（`get_unchecked`、`(*block)`）、**`MaybeUninit` 读写**（`write` / `read().assume_init()` / `assume_init_drop`）、**`Box::from_raw`/`into_raw` 堆回收**；每类都依赖「state 位 + CAS 占有权」或「DESTROY 接力」之类的外部不变量。
- zero 用 `Option<T>` + `ready: AtomicBool`，安全性来自 `Mutex` 串行配对 + `ready` 的 Release/Acquire「取走确认」，保证任一时刻只有一方碰 `msg`；栈 packet 靠 `wait_ready` 防止发送方过早回收。
- **counter.rs `release` 的 `fetch_sub(_, AcqRel)` 是地基**：它让「最后一个句柄」acquire 观察到此前所有 drop，是 array/list 里 `discard_all_messages`、`Box::from_raw`、`assume_init_drop` 得以安全的总开关。
- `CachePadded` 防伪共享、`Backoff` 自旋退避是「硬件现实」工程细节，只影响性能不影响正确性，把无锁算法转化为实际性能优势（量化见 u3-l7）。

## 7. 下一步学习建议

- **u3-l5（utils.rs 与 alloc_helper.rs）**：本讲多处提到 `Mutex`（非毒）、`Backoff`、`Global::allocate_zeroed`，下一讲会讲清这些工具的实现，尤其是 `Global` 在零大小分配、`without_provenance_mut` 上的处理——它直接关系 list `Block::new` 的安全性。
- **u3-l7（性能与基准测试）**：本讲反复出现的 `CachePadded`/`Backoff`/`SeqCst` 都有性能代价，那一讲用基准数据说明 crossbeam-channel 在 SPSC/MPMC 下为何能胜过 std mpsc，并量化弱内存序选择的取舍。
- **u3-l8（测试体系）**：本讲多次标注「数据竞争难在普通测试复现、需 `loom`/MIRI」。那一讲会介绍并发测试如何用随机化与多线程压力验证这些不变量，包括从 Go / std::sync::mpsc 移植的语义兼容性测试。
- 若想系统补强「内存序推理」本身，建议结合 Rust Reference 的 *Memory Model* 与 `crossbeam-memory-model`（loom）文档阅读，把本讲的 happens-before 链在更小的事例上手推几遍。
