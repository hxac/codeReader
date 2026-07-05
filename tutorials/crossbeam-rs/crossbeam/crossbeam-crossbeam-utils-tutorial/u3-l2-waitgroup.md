# WaitGroup 引用计数同步

## 1. 本讲目标

本讲专讲 `crossbeam-utils::sync::WaitGroup`——一个**引用计数式**的同步原语。学完后你应当能够：

- 说清 `WaitGroup` 解决什么问题、与「fork-join / 等待一组任务完成」的关系；
- 画出 `WaitGroup` 的内部状态 `Inner{count, cvar, lock}`，并解释为何要**单独**维护一个 `count` 而不直接用 `Arc` 的引用计数；
- 解释 `clone` 增计数、`drop`/`wait` 减计数、计数归零后 `notify_all` 的完整流程；
- 论证「先 `fetch_sub` 减计数，再 `lock`，最后 `notify`」这一关键顺序为何能**避免丢失唤醒（lost wakeup）**——这与上一讲 `Parker::unpark` 中「先加锁再 `notify_one`」是同一类直觉的复现；
- 区分 `WaitGroup` 与 `std::sync::Barrier` 在用法与语义上的差异。

---

## 2. 前置知识

本讲是 `sync` 单元的第二讲，承接 [u3-l1 Parker](u3-l1-parker.md)。我们继续使用上一讲建立的两条直觉：

1. **原子计数 + Mutex + Condvar** 的经典组合：用一个 `AtomicUsize` 记录状态，用 `Mutex` + `Condvar` 完成阻塞等待与唤醒。
2. **锁内通知防漏唤醒**：唤醒方在调用 `Condvar::notify_*` 之前先获取一次 `Mutex`，让「决定要睡眠」与「真正睡眠」成为不可分割的临界区，从而关闭丢失唤醒的时间窗。

下面补充两个本讲会用到的概念：

- **fork-join 模式**：主线程「fork」出一组工作线程并行干活，随后「join」等待它们全部完成，再汇总结果。这是并发计算里最常见的结构之一。
- **happens-before（先于）关系**：并发编程中刻画「线程 A 的内存写操作，对线程 B 后续的读可见」的先后保证。它的建立需要一对配对的原子操作：一端 `Release`（释放写），另一端 `Acquire`（获取读）。本讲里，工作线程把结果写入共享内存后做 `fetch_sub(Release)`，等待线程用 `load(Acquire)` 观察到计数归零——这一对操作就建立了 happens-before，使主线程能安全读到工作线程的成果。

> 术语速查：**lost wakeup（丢失唤醒）** 指唤醒信号在等待者真正入睡之前发出、却没人记录，导致等待者永远沉睡；**spurious wakeup（虚假唤醒）** 指 `Condvar::wait` 可能在没有显式通知的情况下返回，因此醒来后必须重新检查条件。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`src/sync/wait_group.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs) | `WaitGroup` 的全部实现：结构定义、`new`/`wait`/`clone`/`drop`、`Debug`。本讲的主角。 |
| [`src/sync/mod.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/mod.rs) | `sync` 模块门面，声明子模块并把 `WaitGroup` 重导出到 `crossbeam_utils::sync::WaitGroup`。 |
| [`tests/wait_group.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/wait_group.rs) | 集成测试，演示「所有线程都 `wait`」的会合用法与「子线程 `drop`、主线程 `wait`」的 fork-join 用法。 |

> 与 u1-l3 一致：`sync` 模块需要 `feature = "std"`，`WaitGroup` 在 loom 模型测试下仍可用（它没有 `not(crossbeam_loom)` 门控）。

---

## 4. 核心概念与源码讲解

### 4.1 WaitGroup / Inner 结构

#### 4.1.1 概念说明

`WaitGroup` 的语义可以一句话概括：**每克隆一次就「登记」一个参与者；每丢弃（或调用 `wait` 消费）一次就「注销」一个；当最后一个参与者注销时，所有正在 `wait` 的线程被唤醒。**

它的典型用法是 fork-join：

```rust
// 示例代码（非项目原文件，据 wait_group.rs 顶部文档示例改写）
let wg = WaitGroup::new();          // count = 1（主线程这一份）
for _ in 0..4 {
    let wg = wg.clone();            // count += 1（每登记一个子线程）
    thread::spawn(move || {
        // 干活……
        drop(wg);                   // 子线程结束前注销：count -= 1
    });
}
wg.wait();                          // 主线程消费自己这份并阻塞，直到 count 归零
```

#### 4.1.2 核心流程

`WaitGroup` 本身只是一个共享所有权的句柄，真正的状态在堆上的 `Inner`：

```
┌─────────────────────────┐        Arc（共享所有权）
│ WaitGroup  (栈/线程私有) │ ───────────────────────────────┐
└─────────────────────────┘                                  │ clone() 复制句柄、共享 Inner
                                                             ▼
                                          ┌──────────────────────────────────┐
                                          │ Inner (堆，所有句柄共享同一份)      │
                                          │  cvar: Condvar      唤醒/等待      │
                                          │  lock: Mutex<()>    配对互斥锁      │
                                          │  count: AtomicUsize 活跃参与者计数  │
                                          └──────────────────────────────────┘
```

`count` 的不变量：

\[
\text{count} = (\text{尚未注销的参与者句柄数})
\]

初始 `new()` 时 `count = 1`（句柄本身就是一个参与者）。`clone()` 使 `count += 1`；`drop`/`wait` 使 `count -= 1`；当 `count` 从 1 变 0（即「我」是最后一个注销者），就触发 `notify_all`。

#### 4.1.3 源码精读

结构定义极其朴素：`WaitGroup` 只包一个 `Arc<Inner>`，而 `Inner` 三件套正是上一讲 Parker 用过的「Condvar + Mutex + 原子计数」——[src/sync/wait_group.rs:50-59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L50-L59)。

```rust
pub struct WaitGroup {
    inner: Arc<Inner>,
}

/// Inner state of a `WaitGroup`.
struct Inner {
    cvar: Condvar,
    lock: Mutex<()>,
    count: AtomicUsize,
}
```

构造函数把 `count` 初始化为 **1**（不是 0），因为 `new()` 返回的那一份句柄自身就计为一个参与者——[src/sync/wait_group.rs:61-71](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L61-L71)：

```rust
impl Default for WaitGroup {
    fn default() -> Self {
        Self {
            inner: Arc::new(Inner {
                cvar: Condvar::new(),
                lock: Mutex::new(()),
                count: AtomicUsize::new(1),   // ← 注意：初始为 1
            }),
        }
    }
}
```

> **关键设计问题：既然 `Arc` 自带引用计数，为何还要单独维护 `count`？**
> 两个原因。第一，**排序**：`Arc` 内部的强引用计数用的是 `Relaxed` 操作，不建立 happens-before；而我们需要工作线程把结果写回内存后，用一次 `Release` 的「减计数」把那些写**发布**出去，让等待线程在观察到计数归零时能安全读到结果。第二，**「减计数→条件通知」必须是一个能返回旧值的原子 RMW**（`fetch_sub` 返回旧值以判断「我是不是最后一个」），`Arc::strong_count` 既不能返回减后的语义、也不能用来做这种条件通知。所以 `count` 与 `Arc` 的引用计数在数值上始终相等，但服务于完全不同的目的。

模块门面把 `WaitGroup` 从私有子模块重导出到公共路径——[src/sync/mod.rs:16-19](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/mod.rs#L16-L19)：

```rust
pub use self::{
    parker::{Parker, UnparkReason, Unparker},
    wait_group::WaitGroup,
};
```

#### 4.1.4 代码实践

1. **实践目标**：直观感受 `count` 从 1 开始、随 clone/drop 涨落的规律。
2. **操作步骤**：在 `new()` 之后立即打印 `{wg:?}`（利用下面的 `Debug` 实现），再 `clone()` 几次分别打印，然后逐个 `drop` 打印，观察 `count` 字段变化。
3. **需要观察的现象**：`Debug` 输出形如 `WaitGroup { count: 1 }`、`count: 3`、`count: 2` ……
4. **预期结果**：`count` 与「当前存活的句柄数」精确相等。
5. 该练习只是读取 `Debug` 里的 `Relaxed` 计数，仅用于观察数值规律，不涉及跨线程同步——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果 `new()` 把 `count` 初始化为 0 而不是 1，上面的 fork-join 示例会发生什么？
**答案**：主线程那份句柄没有被计入，于是「4 个子线程 + 主线程」实际只登记了 4 个参与者。当第 4 个子线程 `drop` 后 `count` 就归零并 `notify`，但此时主线程可能还没来得及调用 `wait()`，甚至主线程随后调用 `wait()` 时 `fetch_sub` 会让 `count` 下溢成 `usize::MAX`，永远等不到归零——典型的丢失同步。初始为 1 正是为了把主线程自己算进去。

**练习 2**：`WaitGroup` 是 `Send + Sync` 还是只 `Send`？结合它的预期用法判断。
**答案**：`WaitGroup` 需要 `Send`（要把句柄 `move` 进子线程）并且需要 `Clone`（每个子线程一份独立句柄），但**不需要** `Sync`（线程间不共享同一个 `&WaitGroup`，而是各持有一份 `Arc` 克隆）。其 `Send/Sync/Clone` 均由 `Arc<Inner>` 自动派生，符合这一用法。

---

### 4.2 wait / clone / drop 与计数

#### 4.2.1 概念说明

三个操作共同维护 `count`：

- `clone(&self)`：复制一个句柄交给新线程，`count += 1`。
- `drop`（任一句柄离开作用域时自动调用）：`count -= 1`；若归零则唤醒等待者。
- `wait(self)`：**消费**调用者这一份句柄（`count -= 1`），如果归零就直接返回，否则阻塞在 `Condvar` 上直到归零。

`drop` 与 `wait` 的区别只有一点：`drop` 永远不阻塞（它通常是工作线程收尾），`wait` 在自己不是最后一个时会阻塞。

#### 4.2.2 核心流程

判断「我是不是最后一个注销者」用的是 `fetch_sub` 的**返回值（旧值）**：

\[
\text{prev} = \text{count.fetch\_sub}(1), \qquad \text{我是最后者} \iff \text{prev} == 1
\]

即：减之前若是 1，减完就是 0。

```
clone:        count.fetch_add(1, Relaxed)            // 只涨计数，无需同步
drop:         prev = count.fetch_sub(1, Release)
              if prev == 1 { 归零 → 加锁 + notify_all }   // 见 4.3
wait(self):   （用 ManuallyDrop 拦截自动 Drop）
              prev = count.fetch_sub(1, AcqRel)
              if prev == 1 { 归零 → 加锁 + notify_all; 返回 }
              else         { 加锁; while count != 0 { cvar.wait }; 返回 }
```

#### 4.2.3 源码精读

`clone` 最简单，仅做一次 `Relaxed` 的自增——[src/sync/wait_group.rs:144-151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L144-L151)。这里用 `Relaxed` 即可，因为登记阶段不需要发布任何数据，真正的发布发生在注销时的 `fetch_sub(Release)`。

```rust
impl Clone for WaitGroup {
    fn clone(&self) -> Self {
        self.inner.count.fetch_add(1, Ordering::Relaxed);
        Self { inner: self.inner.clone() }
    }
}
```

`wait` 是全篇最精巧的方法——[src/sync/wait_group.rs:110-131](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L110-L131)。先看它如何「消费 self 但又不触发自动 `Drop`」：

```rust
pub fn wait(self) {
    // SAFETY: this is equivalent to let Self { inner } = self, without calling our Drop.
    let inner = unsafe {
        let slf = ManuallyDrop::new(self);      // 包起来，本函数返回时不会调 Drop
        core::ptr::read(&slf.inner)             // 把 Arc「搬」出来，self 不再拥有它
    };
    ...
}
```

`wait` 接的是 `self`（按值），按 Rust 惯例函数结束时编译器会自动调用 `Drop::drop`，那就等于**多减了一次**计数。这里用 `ManuallyDrop` 把析构关掉，再用 `ptr::read` 把内部的 `Arc` 安全地「移出」（`Arc` 的引用计数不变，只是所有权从 `self` 转移到局部变量 `inner`），随后所有逻辑都改在 `inner` 上做。

接下来是减计数与「归零快路径」——[src/sync/wait_group.rs:117-122](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L117-L122)：

```rust
if inner.count.fetch_sub(1, Ordering::AcqRel) == 1 {
    // Acquire lock after updating count, see below.
    drop(inner.lock.lock().unwrap());
    inner.cvar.notify_all();
    return;
}
```

若返回值不是 1，说明还有其他参与者没注销，进入慢路径——[src/sync/wait_group.rs:127-130](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L127-L130)：

```rust
let mut guard = inner.lock.lock().unwrap();
while inner.count.load(Ordering::Acquire) != 0 {
    guard = inner.cvar.wait(guard).unwrap();
}
```

注意这是一个 `while` 而非 `if`——它同时应对两类情况：**虚假唤醒**（`Condvar` 规范允许无通知返回）与「醒来时其实还有别的非最后注销路径在跑」。醒来后必须重新 `load(count)` 复核。

`Drop` 与 `wait` 的快路径几乎完全对称，只是它永不阻塞——[src/sync/wait_group.rs:134-142](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L134-L142)：

```rust
impl Drop for WaitGroup {
    fn drop(&mut self) {
        if self.inner.count.fetch_sub(1, Ordering::Release) == 1 {
            // Acquire lock after updating count, see wait().
            drop(self.inner.lock.lock().unwrap());
            self.inner.cvar.notify_all();
        }
    }
}
```

> **为何 `wait` 用 `AcqRel` 而 `Drop` 用 `Release`？** 两者的 `Release` 半边都用于「发布」当前线程在注销前对共享数据做的写。`wait` 多取一个 `Acquire` 半边，是因为它随后还要在循环里 `count.load(Acquire)` 复核计数——同一次操作顺手带上 `Acquire` 比额外再发一次原子读更直接、也更一致。`Drop` 没有「随后读计数」的需求，`Release` 即可。

最后是 `Debug`，仅用 `Relaxed` 读 `count` 供调试观察，不做同步保证——[src/sync/wait_group.rs:153-158](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L153-L158)。

#### 4.2.4 代码实践

阅读官方测试 [`tests/wait_group.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/wait_group.rs) 中的 `wait_and_drop`（[第 36-65 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/wait_group.rs#L36-L65)）。它用了**两个** `WaitGroup`：

- `wg2`：每个子线程进来就 `wg2.wait()`，全部阻塞；主线程 `drop(wg2)` 注销自己那份，`count` 归零，所有子线程被唤醒——这是把 `wait` 当作「会合点」用。
- `wg`：子线程干完活后 `drop(wg)`，主线程 `wg.wait()` 等待全部完成——这是 fork-join 用法。

跟踪 `count` 在两条线索里各自从 `1` 出发、被 clone/drop/wait 推动的全过程，体会「同一份原语，不同用法」。

#### 4.2.5 小练习与答案

**练习 1**：`wait` 里的 `while` 改成 `if` 会出什么问题？
**答案**：`Condvar::wait` 允许**虚假唤醒**——在没有 `notify` 的情况下也可能返回。若用 `if`，醒来后就不再复核 `count`，可能在 `count` 仍非 0 时误以为「大家都完成了」而提前返回，破坏同步语义。`while` 保证了「只有真正 `count == 0` 才退出」这一不变量。

**练习 2**：为什么 `wait` 要用 `ManuallyDrop` + `ptr::read`，而不是直接 `let Self { inner } = self;`？
**答案**：`wait(self)` 按值接收 `self`，函数体末尾编译器会自动插入 `Drop::drop(self)`，那会**再** `fetch_sub(1)` 一次。`ManuallyDrop` 关掉了自动析构，`ptr::read` 把 `Arc` 移出而不改变引用计数，从而保证「整条 `wait` 调用只减一次计数」。

---

### 4.3 归零后的加锁通知（防丢失唤醒）

#### 4.3.1 概念说明

本节是全讲的难点，也是与上一讲 `Parker::unpark` 呼应的关键。看 `wait` 与 `Drop` 里都出现的这两行：

```rust
if ... fetch_sub(...) == 1 {        // ① 先减计数（无锁）
    drop(...lock.lock().unwrap());  // ② 再加锁
    ...cvar.notify_all();           // ③ 最后通知
}
```

为什么**先减计数、再加锁、最后通知**？这是为了同时关掉「丢失唤醒」与「虚假唤醒」两个时间窗。直觉是：**「决定要睡」和「真正入睡」必须放进同一个临界区，否则唤醒方可能在这两步之间发信号。**

#### 4.3.2 核心流程

把等待方（慢路径）与最后一个注销方（归零方）的步骤对齐看：

```
等待方 wait()（非最后一个）            归零方（Drop 或 wait 的快路径）
─────────────────────────────────    ─────────────────────────────────
fetch_sub → 返回 >1（不是最后）
                                     ① count.fetch_sub(Release) == 1  ← 先减计数（计数已为 0）
② guard = lock.lock()                ② lock.lock()  ← 必须先抢到锁
③ loop: count.load(Acq) != 0 ?          （若锁被等待方持有，这里阻塞）
     - 若 0：直接返回                      ↓ 一直等到等待方调 cvar.wait
     - 若非 0：cvar.wait(guard)            （wait 会原子地释放锁并入睡）
          └─ 原子释放 lock + 睡眠      ③ 拿到锁 → notify_all → 唤醒等待方 → 释放锁
          └─ 被唤醒 → 重新拿锁 → 复核
```

关键不变量：**等待方「在锁内 `load(count)` 决定是否睡」与归零方「在锁内 `notify`」互斥。** 因此只可能有两种情形，且都不会丢通知：

- **归零方先减计数、还没拿到锁时**：等待方要么尚未持锁（那它会在归零方之后拿锁，`load(count)` 直接看到 0，根本不睡）；要么已经持锁——那它正卡在「`load` 完毕准备 `wait`」之间，归零方在锁外排队，**直到**等待方 `cvar.wait` 原子释放锁后才拿到锁并发通知，恰好唤醒刚入睡的等待方。
- **等待方先持锁并 `load` 到非 0**：随后 `wait` 原子地「释放锁 + 入睡」，归零方拿到锁 `notify_all`，等待方被唤醒。

这段逻辑在源码里被浓缩成一句注释——[src/sync/wait_group.rs:124-126](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L124-L126)：

```rust
// We check the counter while holding the lock, and notifiers acquire
// the lock between updating the counter and notifying, ensuring we
// can not miss the notification.
```

> 这与上一讲 [Parker::unpark](u3-l1-parker.md) 里「`swap(NOTIFIED)` 之后、`notify_one` 之前先 `lock.lock()`」是**同一招**：用一把锁把「通知」与「等待方对状态的观察」串行化，关闭丢失唤醒窗口。区别只是 Parker 用单 token + 三态机，而 WaitGroup 用多参与者计数 + `notify_all`。

#### 4.3.3 源码精读

把 4.2.3 已引用的两段并排看会更清楚。等待方慢路径——[src/sync/wait_group.rs:127-130](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L127-L130)：先持锁 `guard`，再在锁内 `load(count)` 决定是否 `wait`，`wait(guard)` 又把锁「转移」给运行时并在唤醒时归还。

归零方（`Drop` 形式）——[src/sync/wait_group.rs:136-140](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L136-L140)：

```rust
if self.inner.count.fetch_sub(1, Ordering::Release) == 1 {
    drop(self.inner.lock.lock().unwrap());   // 拿锁后立即丢弃：只为建立临界区
    self.inner.cvar.notify_all();
}
```

`drop(inner.lock.lock().unwrap())` 这一行的写法值得品味：**获取锁之后立刻丢弃 guard**。它并不需要临界区里做任何事，只是要保证「`fetch_sub` 与 `notify_all` 之间」被这把锁与等待方的「`load` + `wait`」串行化。guard 一丢弃，锁就释放，但它已经完成了「卡位」的使命。

至于 `happens-before` 的另一面：归零方的 `fetch_sub(Release)` 把工作线程先前的共享写**发布**出去；等待方在 `while` 里 `count.load(Acquire)` 观察到 `0` 时与之配对，于是「工作线程的结果」 happens-before 「等待方继续执行」——这就是 fork-join 里主线程能安全读取子线程成果的同步保证。

#### 4.3.4 代码实践

1. **实践目标**：用一个能稳定复现「通知早于入睡」竞态的思路，理解为何必须加锁。
2. **操作步骤**：阅读本节时序图后，**纸上推演**一个反例——若把 `Drop` 里的 `drop(self.inner.lock.lock().unwrap());` 这一行删掉，构造如下交错：等待方 `load(count)` 见非 0 → 归零方 `fetch_sub` 到 0 并 `notify_all`（此刻无线程在 `wait`，通知作废）→ 等待方进入 `cvar.wait` 永远沉睡。
3. **需要观察的现象**：删掉那行锁之后，上述交错会让测试偶尔挂死（由于是竞态，不一定每次必现）。
4. **预期结果**：恢复锁之后该窗口消失。**不要真的去改源码**（本讲禁止修改源码），只做推演；若想运行验证，可在自己的示例项目里仿写一份极简 `MiniWaitGroup` 并删除对应锁行观察挂死——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：归零方为什么先 `fetch_sub` 再 `lock`，而不是反过来「先 `lock` 再 `fetch_sub`」？
**答案**：先 `fetch_sub`（无锁）能让「计数归零」这件事对所有线程立即可见；等待方在自己的锁内 `load(count)` 时才能及时看到 0。若反过来先持锁再减计数，则减计数与通知都在锁内，虽然正确，但会让所有未归零的 `clone`/`drop` 的计数操作……其实 `count` 是独立原子、并不受这把锁保护，所以「先锁再减」并不会死锁，但它会**把减计数与通知都推迟到持锁期间**，增加临界区长度，且让等待方在拿锁时更晚看到归零。当前「先减后锁」是更优且仍正确的安排：减计数立刻生效，锁只用于协调「通知 vs 入睡」这一关键窗口。

**练习 2**：`notify_all` 之后 guard 被立刻 `drop`、锁立即释放。如果延迟释放锁（比如把 `notify_all` 放在仍持有 guard 时调用），会发生什么？
**答案**：仍正确，但会无谓地拖长临界区。`Condvar::notify_all` 的本意是「唤醒等待该条件变量的线程」，而被唤醒的等待方需要**重新获取锁**才能从 `wait` 返回；若通知方一直握着锁，被唤醒的线程即便被调度起来也拿不到锁、只能再阻塞，造成「惊群后又立刻睡回去」的额外上下文切换开销。所以源码选择「拿到锁只为卡位、立即丢弃」这一紧凑写法。

---

## 5. 综合实践

把本讲全部要点串起来。请完成下面这个对比实验：

**任务**：并发执行 `N = 8` 个任务（每个任务把一个本地计数累加到一个共享 `AtomicUsize`），主线程等待全部完成后再打印总和。请写出**两个**版本：

1. **`WaitGroup` 版**（fork-join 用法）：

   ```rust
   // 示例代码
   use crossbeam_utils::sync::WaitGroup;
   use std::sync::atomic::{AtomicUsize, Ordering};
   use std::thread;

   let wg = WaitGroup::new();
   let total = AtomicUsize::new(0);
   for _ in 0..8 {
       let wg = wg.clone();
       let total = &total;            // 借用即可：主线程在 wg.wait() 后才读
       thread::spawn(move || {
           // 干活，比如累加若干次
           total.fetch_add(1, Ordering::Relaxed);
           drop(wg);                  // 注销：count -= 1
       });
   }
   wg.wait();                         // 消费主线程这份并阻塞到 count 归零
   println!("total = {}", total.load(Ordering::Relaxed));
   ```

   注意：主线程在 `wg.wait()` 返回后读取 `total` 是安全的——正是 4.3 讲的 happens-before 在保护它。

2. **`std::sync::Barrier` 版**：用 `Barrier::new(9)`（8 个子线程 + 主线程，主线程也要 `barrier.wait()`）实现等价等待。

**对比要点**（写进你的笔记）：

| 维度 | `WaitGroup` | `std::sync::Barrier` |
| --- | --- | --- |
| 参与者数量 | 运行时 `clone` 动态登记，无需事先知道 | 构造时必须给出 `n` |
| 复用 | **一次性**，归零后不可重置 | 可重复使用（多轮同步） |
| 谁会阻塞 | 各线程**自选**：可 `wait` 阻塞，也可只 `drop` 不等 | 调用 `wait` 的线程**都会**阻塞到齐 |
| 典型场景 | fork-join：派一组任务、等它们结束 | 多阶段并行：所有线程到齐后再一起进入下一阶段 |
| 代码量 | 句柄随任务克隆，无需预先点数 | 需提前算好线程总数并传给 `new` |

**延伸思考**：若任务数在运行时才确定、或任务会动态派生子任务，`WaitGroup` 的「克隆即登记」远比 `Barrier` 灵活；若需要一个可复用的「全员到齐」栅栏，`Barrier` 才是对的工具。这两者的差异，正是 `wait_group.rs` 顶部文档「# Wait groups vs barriers」一节（[第 8-22 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L8-L22)）所概括的内容。

---

## 6. 本讲小结

- `WaitGroup` 是**引用计数式**同步原语：`clone` 增计数、`drop`/`wait` 减计数，计数归零时唤醒所有等待者。
- 内部状态 `Inner{cvar, lock, count}` 是「Condvar + Mutex + 原子计数」的经典组合，与 `Parker` 同源；`count` 与 `Arc` 的引用计数数值相等但目的不同——前者用于建立 happens-before 与条件通知。
- `new()` 把 `count` 初始化为 **1**，把主线程自己算作一个参与者。
- `wait` 用 `ManuallyDrop` + `ptr::read` 消费 `self` 而不触发自动 `Drop`，避免重复减计数；慢路径用 `while`（非 `if`）循环复核 `count` 以应对虚假唤醒。
- 防**丢失唤醒**的核心是「先 `fetch_sub` 减计数、再 `lock`、最后 `notify_all`」：等待方在锁内决定入睡，归零方在锁内发通知，两者互斥使通知不会被白白发出——这与 `Parker::unpark` 先加锁再通知是同一招。
- 与 `std::sync::Barrier` 相比：`WaitGroup` 动态登记、一次性、各线程可自选是否等待；`Barrier` 需预知数量、可复用、所有线程都阻塞到齐。

---

## 7. 下一步学习建议

- **下一讲 [u3-l3 ShardedLock](u3-l3-shardedlock.md)**：转向另一类同步原语——分片读写锁，它依赖上一讲 [u2-l6 CachePadded](u2-l6-cachepadded.md) 做缓存行填充，是「以空间换低争用」的典型设计。
- **回顾 [u3-l1 Parker](u3-l1-parker.md)**：把 Parker 的「三态机 + 单 token」与本讲的「计数 + notify_all」对照，体会「同一套 Condvar/Mutex 原语如何承载截然不同的同步语义」。
- **进阶 [u4-l1 thread::scope](u4-l1-thread-scope.md)**：作用域线程内部正是用一个 `WaitGroup` 来保证「scope 结束前所有子线程都已 join」，本讲是它的直接前置。
- **源码延伸阅读**：对照标准库 `std::sync::Barrier` 的实现，思考「可复用栅栏」相比「一次性 `WaitGroup`」需要额外维护哪些状态（如代际计数 generation）。
