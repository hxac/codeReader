# ArrayQueue：有界 MPMC 队列

## 1. 本讲目标

本讲精读 `crossbeam-queue` 的 `ArrayQueue`——一个**有界（bounded）、多生产者多消费者（MPMC）、无锁（lock-free）**的并发队列。它基于 Dmitry Vyukov 的经典有界 MPMC 队列算法。

学完后你应当能够：

- 说清楚「环形缓冲 + 代次（lap）编码」是如何**防止 ABA 问题**的；
- 画出单个槽位 `Slot` 的 `stamp` 状态机（可写 ↔ 可读），并解释它与 `head`/`tail` 的配合；
- 跟踪一次 `push` 与一次 `pop` 的完整 CAS 流程；
- 解释为什么 `head` 和 `tail` 要包在 `CachePadded` 里。

本讲依赖 [u2-l3 AtomicCell](./u2-l3-atomic-cell.md)（原子 load/store/CAS 的基本套路），并复用 [u2-l1 Backoff](./u2-l1-backoff.md) 与 [u2-l2 CachePadded](./u2-l2-cache-padded.md) 两个已学原语。

## 2. 前置知识

### 2.1 CAS 与 ABA

并发算法里最常见的写法是「读旧值 → 算新值 → `compare_exchange(旧值, 新值)`」(简称 CAS)。CAS 只在当前值**仍等于旧值**时才成功。

ABA 问题：线程 T1 读到值 `A`，正准备 CAS 时被挂起；期间别的线程把值改成 `B` 又改回 `A`。T1 醒来后 CAS 成功——因为当前值确实是 `A`——但中间发生过的变化它一无所知，状态机可能已被破坏。在环形队列里，这会导致**覆盖一个尚未被消费的槽位**。

对策是给每个值附一个**单调递增的代次（generation / lap）**：即便数值「绕了一圈回到原点」，代次也对不上，CAS 照样失败。本讲的 `lap` 就是这个代次。

### 2.2 环形缓冲（ring buffer）

用一个定长数组当队列底层数组，配两个游标 `head`（出队端）和 `tail`（入队端）。游标推进到数组末尾时「折回」到 0，周而复始，像在环上走。`ArrayQueue` 的容量在构造时就固定分配好，因此比按需扩容的 `SegQueue`（见 u4-l2）略快。

### 2.3 复用自前序讲义的两件工具

- **`Backoff`**（u2-l1）：CAS 失败时的指数退避，`spin()` 用于「别人已抢走、自己重试」，`snooze()` 用于「等别人把状态推进」。
- **`CachePadded`**（u2-l2）：按缓存行对齐填充，消除多核频繁改写相邻字段时的**伪共享**。本讲里 `head` 与 `tail` 正是被多核高频改写的热点字段，所以各自独占一条缓存行。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [crossbeam-queue/src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs) | 子 crate 入口，声明 `#![no_std]`、特性门控，重导出 `ArrayQueue` 与 `SegQueue`。 |
| [crossbeam-queue/src/array_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs) | 本讲主角：`Slot`、`ArrayQueue` 的全部实现（构造、`push`/`pop`/`force_push`、`len`/`is_empty`/`is_full`、`Drop`）。 |
| [crossbeam-queue/tests/array_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs) | 集成测试，含 `spsc`、`mpmc`、`mpmc_ring_buffer`、`drops`、`linearizable` 等，是本讲代码实践的依据。 |

`lib.rs` 用一行特性门控决定该模块是否编译——只有同时开启 `alloc` 且目标平台支持指针级原子时，`ArrayQueue` 才存在：

```rust
#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]
mod array_queue;
```

见 [crossbeam-queue/src/lib.rs#L28-L34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L28-L34)。这意味着 `ArrayQueue` 在 `no_std + alloc` 环境下也能用。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **lap/index 编码与 Slot 的 stamp 状态机**（防 ABA 的核心）；
2. **push：CAS tail + 写值 + 更新 stamp**；
3. **pop：CAS head + 读值 + 重置 stamp 为下一圈**。

### 4.1 lap/index 编码与 Slot 的 stamp 状态机

#### 4.1.1 概念说明

如果直接用「`position % cap`」当作游标，环绕一圈后游标的**数值**会与一圈前一模一样——这正是 ABA 的温床。`ArrayQueue` 的做法是把 `head` / `tail` **以及每个槽位的 `stamp`** 都编码成一个 `usize`，里面同时打包两段信息：

- **index**：低位，表示在 `buffer` 中的下标（`0..cap-1`）；
- **lap**：高位，表示已经绕了几圈（代次）。

这样同一个 `index` 在「第 0 圈」和「第 1 圈」对应**不同的数值**，CAS 比较的是完整 (lap, index)，旧值绝无可能在环绕后重现，ABA 被根除。

每个槽位 `Slot` 自己也持有一个 `stamp`，记录「我目前属于哪一圈的哪一态」。它的状态机只有两态：

- **可写**：`slot.stamp == tail`（等当前 `tail` 这一圈的生产者来写）；
- **可读**：`slot.stamp == head + 1`（等当前 `head` 这一圈的消费者来读）。

生产者只往「可写」槽写，消费者只从「可读」槽读——这是算法正确性的第二条保险（第一条是 head/tail 自身的 lap）。

#### 4.1.2 核心流程

槽位 `stamp` 的生命周期（伪代码）：

```
初始化:        slot[i].stamp = i                      // lap0, 可写
push 写入后:   slot[i].stamp = tail + 1               // 可读
pop  读出后:   slot[i].stamp = head + one_lap         // 可写（下一圈）
... 第 2 圈再被 push 时，tail 已推进到 head+one_lap ...
```

`one_lap` 是「环绕一圈 head/tail 要加的增量」，定义为一个**严格大于 `cap` 的最小 2 的幂**：

\[ \texttt{one\_lap} = \texttt{next\_power\_of\_two}(cap + 1) \]

由于它是 2 的幂，拆解 index / lap 不必做除法或取模，只需位运算：

\[ \texttt{index} = \texttt{value}\ \&\ (\texttt{one\_lap}-1) \]
\[ \texttt{lap}   = \texttt{value}\ \&\ !(\texttt{one\_lap}-1) \]

要求严格大于 `cap`，是为了把「一圈的游标增量」与「槽位总数」解耦，并保证 index 字段足以容纳 `0..cap-1`。

#### 4.1.3 源码精读

`Slot` 仅两个字段：原子 `stamp` 与存放值的 `UnsafeCell<MaybeUninit<T>>`（`MaybeUninit` 的作用参见 u2-l3，这里不再展开）：

```rust
struct Slot<T> {
    stamp: AtomicUsize,
    value: UnsafeCell<MaybeUninit<T>>,
}
```

见 [crossbeam-queue/src/array_queue.rs#L17-L27](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L17-L27)。

`ArrayQueue` 主结构里，`head` / `tail` 都被 `CachePadded` 包裹（避免伪共享，呼应 u2-l2），并保存预计算的 `one_lap`：

```rust
pub struct ArrayQueue<T> {
    head: CachePadded<AtomicUsize>,
    tail: CachePadded<AtomicUsize>,
    buffer: Box<[Slot<T>]>,
    one_lap: usize,
}
```

见 [crossbeam-queue/src/array_queue.rs#L52-L74](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L52-L74)。

构造函数 `new` 做三件事：断言容量非零、给每个槽位写入**初始 stamp = i**（即 lap 0、index i，表示「第 0 圈可写」）、计算 `one_lap`：

```rust
let buffer: Box<[Slot<T>]> = (0..cap).map(|i| Slot {
    stamp: AtomicUsize::new(i),                       // { lap:0, index:i } → 可写
    value: UnsafeCell::new(MaybeUninit::uninit()),
}).collect();
let one_lap = (cap + 1).next_power_of_two();
```

见 [crossbeam-queue/src/array_queue.rs#L96-L125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L96-L125)。

#### 4.1.4 代码实践（源码阅读型）

**目标**：用一个 `cap = 2` 的小队列，手工推演 4 步操作，亲眼看 `lap` 如何让「绕回原点」的槽位重新可写而不引发 ABA。

**操作步骤**：

1. 假设 `cap = 2`，则 `one_lap = (2+1).next_power_of_two() = 4`，index 字段为低 2 位。
2. 在纸上抄下初值：`head = 0, tail = 0`，`slot[0].stamp = 0`，`slot[1].stamp = 1`。
3. 依次「执行」：`push('a')` → `push('b')` → `pop()` → `push('c')`，每步按 4.1.2 的规则更新 stamp 与 head/tail。
4. 把结果填入下表（参考答案见 4.1.5）。

**应观察的现象**：

- `push('b')` 后，`tail` 从 1 跳到 `4`（=`{lap:1, index:0}`），出现**环绕**——数值上 index 回到了 0，但 lap 从 0 变成了 1。
- `pop()` 读出 `'a'` 后，`slot[0].stamp` 被写成 `head + one_lap = 0 + 4 = 4`。
- 第 4 步 `push('c')` 时，`tail = 4` 恰好等于 `slot[0].stamp = 4`，于是 slot[0] 在**第 1 圈**重新可写。

**预期结果**：第 4 步之所以能安全复用 slot[0]，正是因为它的 stamp 已经被推进到「lap 1」，与当前 `tail = 4` 对得上；若没有 lap，slot[0] 的 stamp 会与第 0 圈混淆，生产者可能把还在等待消费的值覆盖掉（ABA）。

#### 4.1.5 小练习与答案

**练习 1**：把 `cap` 改成 `4`，求 `one_lap`，并写出 index 字段是几位。

> 答案：`one_lap = (4+1).next_power_of_two() = 8`，index 字段为低 3 位（`0..7`），其中实际只用 `0..3`。

**练习 2**：补全 4.1.4 的推演表。

> 答案：
>
> | 步骤 | head | tail | slot[0].stamp | slot[1].stamp | 说明 |
> |------|------|------|---------------|---------------|------|
> | 初值 | 0 | 0 | 0 | 1 | slot0 可写（stamp==tail） |
> | push('a') | 0 | 1 | 1 | 1 | 写 slot0，stamp→tail+1=1 |
> | push('b') | 0 | 4 | 1 | 2 | 写 slot1，stamp→2；tail 环绕到 {lap1, idx0}=4 |
> | pop()→'a' | 1 | 4 | 4 | 2 | 读 slot0，stamp→head+one_lap=0+4=4 |
> | push('c') | 1 | 5 | 5 | 3 | tail=4==slot0.stamp=4，写 slot0，stamp→5 |

**练习 3**：为什么 `head` 和 `tail` 要分别用 `CachePadded` 包裹，而不是放进同一个 `struct` 再共享一条缓存行？

> 答案：生产者高频改 `tail`、消费者高频改 `head`，若两者同处一条缓存行会触发伪共享（u2-l2），导致缓存行在核间反复失效。`CachePadded` 让各占一条缓存行，互不干扰。

---

### 4.2 push：CAS tail + 写值 + 更新 stamp

#### 4.2.1 概念说明

`push` 要把一个元素放进队尾。在有界队列里它可能失败（队列已满），故返回 `Result<(), T>`：成功 `Ok(())`，满了就把原值 `Err(v)` 退回。核心难点是**多个生产者并发抢同一个 `tail`**：必须用 CAS 保证「只有一个生产者真正占住槽位」，其余失败者退避重试。

`push` 与 `force_push`（满时挤掉最老元素）共用一个内部骨架 `push_or_else`，只在「满了该怎么办」上分支不同。

#### 4.2.2 核心流程

```
load tail
loop {
    拆出 index / lap，算 new_tail（同圈 +1，或环绕到下一圈 index=0）
    读 slot[index].stamp (Acquire)
    分三种情况：
      ① stamp == tail              → 槽位可写：CAS(tail → new_tail)
                                      成功：写值，stamp = tail+1 (Release)，返回 Ok
                                      失败：更新 tail，Backoff::spin 重试
      ② stamp + one_lap == tail+1  → 槽位还停在一圈前，疑似「满」：
                                      调用回调 f 判定（push 返回 Err，force_push 挤掉旧值）
      ③ 其它                       → 槽位状态正在迁移中，Backoff::snooze 等待
}
```

#### 4.2.3 源码精读

骨架 `push_or_else` 先拆解 tail、计算 `new_tail`：

```rust
let index = tail & (self.one_lap - 1);
let lap   = tail & !(self.one_lap - 1);
let new_tail = if index + 1 < self.capacity() {
    tail + 1                              // 同圈，index+1
} else {
    lap.wrapping_add(self.one_lap)        // 环绕：lap+1, index 归 0
};
```

见 [crossbeam-queue/src/array_queue.rs#L134-L147](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L134-L147)。注意环绕时用 `wrapping_add`，让 lap 可以一路累加而不会在 `usize` 顶端 panic（实际上要环绕到冲突需要极其漫长的时间）。

接着是「槽位可写」分支：CAS 抢 tail，成功才真正写值并把 stamp 推到 `tail + 1`（变可读）：

```rust
if tail == stamp {
    match self.tail.compare_exchange_weak(tail, new_tail, SeqCst, Relaxed) {
        Ok(_) => {
            unsafe { slot.value.get().write(MaybeUninit::new(value)); }
            slot.stamp.store(tail + 1, Ordering::Release);   // 可读
            return Ok(());
        }
        Err(t) => { tail = t; backoff.spin(); }              // 被人抢了，退避重试
    }
}
```

见 [crossbeam-queue/src/array_queue.rs#L155-L175](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L155-L175)。

- CAS 成功顺序很关键：**先 CAS 占住 tail，再写值，最后才把 stamp 从 `tail` 改成 `tail+1`**。消费者看到 `stamp == head+1` 才会读，而此时值已写入，且 `stamp.store(Release)` 与消费者 `stamp.load(Acquire)` 配对，保证「消费者读到 stamp 更新就一定读到值」。
- CAS 失败说明有别的生产者抢走了 `tail`，用 `backoff.spin()`（u2-l1）退避后重试，注意要把本地的 `tail` 更新为 CAS 返回的最新值。

「疑似满」分支会调用回调 `f`。`push` 的回调再读一次 `head`，若 `head + one_lap == tail` 则确认满，退回原值：

```rust
pub fn push(&self, value: T) -> Result<(), T> {
    self.push_or_else(value, |v, tail, _, _| {
        let head = self.head.load(Ordering::Relaxed);
        if head.wrapping_add(self.one_lap) == tail { Err(v) } else { Ok(v) }
    })
}
```

见 [crossbeam-queue/src/array_queue.rs#L203-L215](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L203-L215)。「满」的判定正是「`tail` 比 `head` 整整领先一圈」。

最后一种情况（`else` 分支）是槽位状态正在被别的线程迁移（比如刚 CAS 了 tail 但 stamp 还没写完），此时用 `backoff.snooze()` 让出时间片等待，见 [crossbeam-queue/src/array_queue.rs#L181-L185](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L181-L185)。

#### 4.2.4 代码实践

**目标**：用单线程验证 `push` 在满时返回原值，并确认 `is_full` 与之一致。

**操作步骤**（运行项目自带的 `smoke` 类断言即可，也可手写）：

```rust
// 示例代码
use crossbeam_queue::ArrayQueue;
let q = ArrayQueue::new(2);
assert_eq!(q.push('a'), Ok(()));
assert_eq!(q.push('b'), Ok(()));
assert_eq!(q.push('c'), Err('c'));   // 满了，原值退回
assert!(q.is_full());
```

**应观察的现象**：第三次 `push` 返回 `Err('c')`，且 `is_full()` 为真。

**预期结果**：`push` 满时**不吞数据**，原值经 `Err` 原样带回（与 channel 的发送错误「内嵌消息」是同一种设计哲学，见 u3-l2）。

#### 4.2.5 小练习与答案

**练习 1**：为什么写值之后用 `Ordering::Release` 存 stamp，而读 stamp 用 `Ordering::Acquire`？

> 答案：Release/Acquire 配对建立 happens-before。生产者「写值→Release 存 stamp」，消费者「Acquire 读 stamp→读值」，只要消费者看到 stamp 变成可读，就一定能看到写入的值，避免读到未初始化内存。

**练习 2**：CAS 失败后，为什么是 `backoff.spin()` 而不是 `snooze()`？

> 答案：CAS 失败意味着「别的生产者刚抢走 tail 并会很快推进状态」，属于「别人已前进、我重试」的低延迟场景，用纯自旋 `spin()`（u2-l1）。`snooze()` 留给「等别人把状态推进完」的情况（即上面的 else 分支）。

**练习 3**：`force_push` 在满时的回调里做了什么 `push` 没做的事？

> 答案：`force_push` 不退回原值，而是 CAS 把 `head` 向前推一格（丢弃最老的元素）、再 `store` 推进 `tail`，最后用 `replace` 把槽里的旧值换出来返回，见 [crossbeam-queue/src/array_queue.rs#L275-L301](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L275-L301)。它把队列当「环形缓冲」用。

---

### 4.3 pop：CAS head + 读值 + 重置 stamp 为下一圈

#### 4.3.1 概念说明

`pop` 从队首取出一个元素，空队列返回 `None`。它是 `push` 的镜像：多个消费者并发抢 `head`，CAS 保证只有一个消费者真正取走值。取走后，槽位不能直接当作「可写」，而要被重置成**下一圈才可写**——这就是标题里「重置 stamp 为下一圈」的含义，也是 lap 机制在消费侧的落点。

#### 4.3.2 核心流程

```
load head
loop {
    拆出 index / lap，算 new（同圈 +1，或环绕到下一圈 index=0）
    读 slot[index].stamp (Acquire)
    分三种情况：
      ① stamp == head + 1          → 槽位可读：CAS(head → new)
                                      成功：读值，stamp = head + one_lap (Release)，返回 Some
                                      失败：更新 head，Backoff::spin 重试
      ② stamp == head              → 槽位还是上一圈的可写态，疑似「空」：
                                      再读 tail，若 tail == head 则真的空，返回 None
      ③ 其它                       → 状态迁移中，Backoff::snooze 等待
}
```

#### 4.3.3 源码精读

「槽位可读」分支：当 `stamp == head + 1`，CAS 抢 head，成功后读值并把 stamp 重置为 `head + one_lap`（让该槽在**下一圈**重新可写）：

```rust
if head + 1 == stamp {
    match self.head.compare_exchange_weak(head, new, SeqCst, Relaxed) {
        Ok(_) => {
            let msg = unsafe { slot.value.get().read().assume_init() };
            slot.stamp.store(head.wrapping_add(self.one_lap), Ordering::Release);
            return Some(msg);
        }
        Err(h) => { head = h; backoff.spin(); }
    }
}
```

见 [crossbeam-queue/src/array_queue.rs#L333-L362](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L333-L362)。

注意 `slot.stamp.store(head.wrapping_add(self.one_lap), ...)` 这一行——它把 stamp 一次性推进一个 `one_lap`，使该槽的「可写」条件 `stamp == tail` 只会在下一圈 `tail` 追上来时再次成立。这就是 4.1 里「读出后 stamp = head + one_lap → 可写（下一圈）」的代码出处。

「疑似空」分支：当 `stamp == head`（槽位仍停在当前圈的可写态，没人写过），再读 `tail`，若 `tail == head` 则确认空：

```rust
} else if stamp == head {
    atomic::fence(Ordering::SeqCst);
    let tail = self.tail.load(Ordering::Relaxed);
    if tail == head { return None; }        // 真的空
    backoff.spin();
    head = self.head.load(Ordering::Relaxed);
}
```

见 [crossbeam-queue/src/array_queue.rs#L363-L373](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L363-L373)。

最后，`Drop` 实现会在队列销毁时遍历仍在队列里的槽位，逐个 `assume_init_drop()` 释放 `T` 的资源——因为 `MaybeUninit` 不会自动析构（与 u2-l3 AtomicCell 必须手写 Drop 同理）：

见 [crossbeam-queue/src/array_queue.rs#L535-L572](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L535-L572)。

#### 4.3.4 代码实践

**目标**：验证空队列 `pop` 返回 `None`，并在消费一个元素后，该槽位能在下一圈被 `push` 复用。

**操作步骤**（示例代码）：

```rust
// 示例代码
use crossbeam_queue::ArrayQueue;
let q = ArrayQueue::new(2);
assert_eq!(q.pop(), None);          // 空
q.push(1).unwrap();
q.push(2).unwrap();
assert_eq!(q.pop(), Some(1));       // 消费 slot0
// 此时 slot0 已被重置为「下一圈可写」，再 push 会重新落到 slot0
q.push(3).unwrap();                 // 复用 slot0
assert_eq!(q.pop(), Some(2));
assert_eq!(q.pop(), Some(3));
```

**应观察的现象**：第 3 次 `push(3)` 成功，说明被 `pop` 腾出的 slot0 已经在「下一圈」重新可写；最终两次 `pop` 依次取回 `2` 与 `3`，顺序正确。

**预期结果**：队列作为一个容量为 2 的环，能正确循环复用槽位而不丢数据。完整多线程无丢失验证见第 5 节。

#### 4.3.5 小练习与答案

**练习 1**：`pop` 读出值后，为什么 stamp 写的是 `head + one_lap` 而不是 `head + 1`？

> 答案：写 `head + one_lap` 是把槽位标记为「下一圈才可写」。若写成 `head + 1`，就会和「刚被 push 写完、stamp = tail+1」的可读态混淆，且无法阻挡当前圈的生产者再次写入同一槽，破坏单生产者单槽的占有关系。

**练习 2**：`pop` 在「疑似空」分支里为什么还要再读一次 `tail`？

> 答案：`stamp == head` 只说明「这个槽当前圈还没被写」，但别的槽可能已被写。读 `tail` 与 `head` 比较：若 `tail == head` 才能确认整个队列空（没有任何槽被写过），否则只是 head 落后，应自旋等待。

**练习 3**：`Drop` 里为什么要先判断 `mem::needs_drop::<T>()`？

> 答案：若 `T` 是 `Copy`/平凡类型（如 `i32`），析构是空操作，跳过可避免无谓遍历。只有 `T` 真正需要析构时，才遍历 `head..tail` 范围的槽位逐个 `assume_init_drop()`。

---

## 5. 综合实践

**任务**：用 `ArrayQueue` 搭一个 4 生产者 4 消费者的 MPMC 传送带，验证**无丢失、无重复**，并据此回答「不用 lap 会出现什么 ABA 问题」。

项目自带的 `mpmc` 浽数正是这个范式，它用一个 `Vec<AtomicUsize>` 当计数板，每个值被消费一次就让对应计数 `+1`，最后断言每个值恰好被消费 `THREADS` 次：

```rust
const COUNT: usize = if cfg!(miri) { 50 } else { 25_000 };
const THREADS: usize = 4;
let q = ArrayQueue::<usize>::new(3);
let v = (0..COUNT).map(|_| AtomicUsize::new(0)).collect::<Vec<_>>();
scope(|scope| {
    for _ in 0..THREADS { /* 消费者：pop 到 v[n] += 1 */ }
    for _ in 0..THREADS { /* 生产者：push(0..COUNT)，满则重试 */ }
}).unwrap();
for c in v { assert_eq!(c.load(Ordering::SeqCst), THREADS); }
```

完整代码见 [crossbeam-queue/tests/array_queue.rs#L216-L249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L216-L249)。

**操作步骤**：

1. 进入仓库根目录，运行：
   ```
   cargo test -p crossbeam-queue --test array_queue mpmc -- --nocapture
   ```
2. 阅读 [array_queue.rs#L216-L249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L216-L249)，理解它为何能「无丢失、无重复」（提示：生产者满则 `while q.push(i).is_err() {}` 重试，消费者空则 `loop { if let Some(x) = q.pop() { break x; } }` 重试，CAS 保证每个槽只被一个生产者写、一个消费者读）。
3. 把 `THREADS` 调到 `8`、`COUNT` 调大，重跑，观察是否依旧通过。
4. **回答 ABA 问题**（文字作答，不要真去改源码）：假设删除 lap 机制、游标退化为 `position % cap`，描述一个具体的出错场景。

**应观察的现象**：测试通过，终端打印 `ok. 1 passed`。调大线程数与消息量后仍通过。

**预期结果 / ABA 场景描述**（参考答案）：

> 若没有 lap，`tail` 只用 `index = position % cap`。设想生产者 P1 读到 `tail = 0`（指向 slot0），正要 CAS 时被挂起。此间其余生产者不断 push、消费者不断 pop，队列绕了完整一圈，`tail` 在数值上又回到了 `0`。P1 醒来，`CAS(tail, 0, 1)` 仍然成功（当前值确实是 0），于是它写入 slot0——但此刻 slot0 要么还持有尚未被消费的值（被覆盖→丢数据），要么其 stamp 状态与 P1 的假设不符（破坏可读/可写不变量→重复消费或读到脏值）。lap 把代次编码进高位，使「绕回原点」后的 `tail` 数值（如 `4`）与旧值（`0`）不同，CAS 失败，P1 被迫重读重试，ABA 被根除。

> 若本地无法运行（缺 Rust 工具链等），本实践的运行结果标注为「待本地验证」；但 ABA 场景的分析不依赖运行，可直接完成。

## 6. 本讲小结

- `ArrayQueue` 是有界、MPMC、无锁的环形队列，底层是定长 `Box<[Slot<T>]>`，容量构造时一次分配，比 `SegQueue` 略快。
- **lap/index 编码**把 `usize` 拆成「高位代次 + 低位下标」，`one_lap = (cap+1).next_power_of_two()`，用位运算而非取模拆解；同一 index 在不同圈数值不同，从而**根除 ABA**。
- 每个 `Slot` 的 `stamp` 是一个两态状态机：可写（`stamp == tail`）↔ 可读（`stamp == head+1`），`push` 写后置 `tail+1`，`pop` 读后置 `head+one_lap`（下一圈才可写）。
- `push`/`pop` 都是「读游标 → 查 stamp → CAS 推进游标 → 改 stamp」的四步，CAS 失败用 `Backoff::spin` 退避，状态迁移中用 `Backoff::snooze` 让出时间片。
- `head`/`tail` 各自 `CachePadded`，消除生产者与消费者之间的伪共享。
- `MaybeUninit` 不自动析构，故 `Drop` 手动遍历存活槽位 `assume_init_drop()`。

## 7. 下一步学习建议

- **u4-l2 SegQueue**：对比无界分段队列如何用链表式 `Block`（每块 31 槽）+ `HAS_NEXT` 位编码解决「无上限」与回收问题，体会它与 `ArrayQueue` 在容量策略与 ABA 防护上的异同。
- **延伸阅读**：Dmitry Vyukov 的原文 [bounded MPMC queue](http://www.1024cores.net/home/lock-free-algorithms/queues/bounded-mpmc-queue)（源码文件头注释引用），对照本讲理解原作者的措辞。
- **测试与正确性**：浏览 [tests/array_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs) 中的 `drops`、`linearizable`、`mpmc_ring_buffer`，它们分别从「析构正确」「可线性化」「环形缓冲语义」三个角度压测本讲算法，为 u7-l3 的 loom/miri 并发正确性验证埋下伏笔。
