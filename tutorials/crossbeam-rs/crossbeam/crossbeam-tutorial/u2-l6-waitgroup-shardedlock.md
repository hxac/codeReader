# WaitGroup 与 ShardedLock

## 1. 本讲目标

本讲承接 [u2-l5 Parker](u2-l5-parker.md) 已经建立的「阻塞—唤醒」直觉，进入 crossbeam-utils 的另外两个同步原语：`WaitGroup` 与 `ShardedLock`。学完后你应当能够：

- 说清 `WaitGroup` 如何用一个 `AtomicUsize` 引用计数 + 一个 `Mutex`/`Condvar` 实现「等一组任务全部结束」，并解释它为何不会丢失唤醒。
- 说清 `ShardedLock` 为什么把一把读写锁拆成 8 个分片，读路径只锁一个分片、写路径锁全部分片，从而在读多写少场景下提升可扩展性。
- 理解「稳定的线程索引」如何由一个线程局部（thread-local）注册表 + 全局 `OnceLock` 提供，并把它和分片选择联系起来。
- 能写出结合两者的最小并发程序，并知道如何用仓库自带的测试去验证行为。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：引用计数也能当同步信号。**
u2-l5 里我们用「令牌（token）」表示「有没有人来唤醒」。本讲 `WaitGroup` 用另一种信号——「还剩几个未完成的任务」。每克隆一次引用计数 `+1`（多一个任务），每 drop 一次 `-1`（一个任务完成）。当计数归零，等待者就被唤醒。它本质上是一个**带条件的 condvar 等待**，条件就是 `count == 0`。

**直觉二：读写锁的瓶颈是「读—读」争用本身。**
`std::sync::RwLock` 允许多个读者并发进入，但读者仍要原子地更新同一把锁内部的「读者计数」字段。8 个线程同时 `read()`，会让这个计数字段所在的缓存行在核间反复失效——这正是 [u2-l2 CachePadded](u2-l2-cache-padded.md) 讲过的**伪共享（false sharing）**。`ShardedLock` 的对策是：与其用 1 把锁的 1 个计数字段，不如用 8 把独立的读写锁，让不同线程大概率去碰不同的锁、不同的缓存行。

**直觉三：线程需要一个稳定的「编号」。**
分片选择要靠 `thread_index & 7` 把线程映射到分片。但标准库的 `ThreadId` 是个不保证连续的大整数，直接拿它做掩码分布会很差。crossbeam 维护了一张全局注册表，给每个线程分配一个**连续、可回收**的小整数索引。这一节会用到本讲第三个源码文件 `once_lock.rs`，它是一个基于 `std::sync::Once` 的「一次性初始化」容器，用来懒加载这张全局注册表。

> 补充：`OnceLock` 在标准库里曾是不稳定 API（在本仓库 MSRV 1.74 下），所以 crossbeam 自行 vendor 了一份实现。我们会在第 4.3 节看到它如何被 `ShardedLock` 使用。

## 3. 本讲源码地图

本讲涉及三个源码文件，都在 `crossbeam-utils/src/sync/` 下：

| 文件 | 作用 | 本讲扮演的角色 |
|------|------|----------------|
| [`wait_group.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs) | `WaitGroup` 类型实现 | 主角一：引用计数 + condvar 的同步原语 |
| [`sharded_lock.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs) | `ShardedLock` 类型实现 | 主角二：分片读写锁 + 线程注册表 |
| [`once_lock.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs) | `OnceLock<T>`（私有） | 配角：为线程注册表提供一次性全局初始化 |

另外会引用两个**真实测试文件**作为实践依据：[`crossbeam-utils/tests/wait_group.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/wait_group.rs) 与 [`crossbeam-utils/tests/sharded_lock.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/sharded_lock.rs)。最后还会提到一处**生产环境真实使用点**：作用域线程 `scope` 内部用 `WaitGroup` 等待所有嵌套子作用域退出（见 `crossbeam-utils/src/thread.rs`）。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：4.1 `WaitGroup`、4.2 `ShardedLock` 的分片与读写路径、4.3 线程局部注册表与 `OnceLock`。

### 4.1 WaitGroup：引用计数 + 条件变量

#### 4.1.1 概念说明

`WaitGroup` 解决的问题是「主线程要等一组子任务全部完成后再继续」。它来自 Go 语言的同名原语，文档里把它和 `std::sync::Barrier` 做了三点对比（见源码文档注释）。把它和已经学过的原语放一起对比：

| 原语 | 触发唤醒的条件 | 是否需要预先知道线程数 | 是否可复用 |
|------|----------------|------------------------|------------|
| `Barrier` | 所有线程都到达屏障 | 是（构造时指定） | 是 |
| `Parker`（u2-l5） | 有人投递令牌 | 否 | 是（二值令牌） |
| `WaitGroup` | 所有 clone 都被 drop | 否（边跑边 clone） | 否（一次性） |

关键差异：`Barrier` 必须在构造时知道人数；`WaitGroup` 允许「**边 spawn 边 clone**」动态登记参与者，并且每个线程可以**自主选择**是 `wait()` 等别人，还是干完就 `drop` 走人。

#### 4.1.2 核心流程

`WaitGroup` 的全部状态藏在 `Arc<Inner>` 里，`Inner` 含三个字段：一个 `Condvar`、一个 `Mutex<()>`、一个 `AtomicUsize count`。计数协议如下：

```
new()      : count = 1          # 根引用本身占 1
clone()    : count += 1 (Relaxed)   # 多一个参与者
wait(self) : count -= 1 (AcqRel)    # 消费「自己」这个引用
drop       : count -= 1 (Release)   # 子线程结束时减 1
当某次减法「减之前 == 1」：我是最后一个 → 抢锁 + notify_all
```

注意两个细节：

1. **初始计数是 1，不是 0。** 这个 `1` 代表「调用 `wait()` 的那个根引用」。只要 `wait()` 还没被调用，计数永远 ≥ 1，不会提前归零误唤醒。
2. **`wait(self)` 消费 self。** 它不是 `&self`，所以调用者一旦 `wait()`，自己就退出了「参与者」名单，转而成为「等待者」。

`wait` 有两条路径——快速路径与阻塞路径，用伪代码表示：

```
fn wait(self):
    inner = 取出 Arc<Inner>（但不能跑 Drop！）   # ManuallyDrop 技巧
    if count.fetch_sub(1, AcqRel) == 1:         # 我是最后一个
        抢锁; notify_all; return                 # 快速路径：直接返回
    # 否则还有人没结束，阻塞
    guard = lock()
    while count.load(Acquire) != 0:              # 防伪唤醒 + 防丢唤醒
        guard = cvar.wait(guard)
```

**为什么不丢失唤醒？** 这是本模块最关键的正确性论证，和 u2-l5 Parker 的「谁置位谁唤醒」一脉相承：

- 通知方（`drop` 或 `wait` 的快速路径）总是**先 `fetch_sub` 更新计数，再抢锁，再 `notify_all`**。
- 等待方总是**先 `fetch_sub`，再抢锁，再在持锁状态下检查 `count`**。
- 因为通知方在「改计数」与「notify」之间必须穿过同一把 `Mutex`，等待方又在持锁时读 `count`：若通知方先于等待方读 `count`，等待方会看到 `0` 而根本不睡；若通知方晚于等待方入睡，通知方抢锁时必然排在 `cvar.wait` 释放锁之后，`notify_all` 一定叫醒它。两种顺序都安全。

#### 4.1.3 源码精读

**内部状态：** 计数、互斥锁、条件变量三件套，外加一层 `Arc` 共享。

[wait_group.rs:50-59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L50-L59) — `WaitGroup` 只是 `Arc<Inner>` 的包装；`Inner` 含 `cvar`/`lock`/`count` 三字段。

[wait_group.rs:61-71](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L61-L71) — `Default` 实现，注意 `count` 初始化为 `1`，预留根引用的位。

**`wait` 的实现：** 注意 `ManuallyDrop` 技巧——它消费 self 但**不能**走正常的 `Drop`（否则会重复 `fetch_sub`），于是用 `ManuallyDrop::new(self)` + `ptr::read` 把内部的 `Arc<Inner>` 偷出来。

[wait_group.rs:110-131](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L110-L131) — `wait` 全文。摘录关键两段：

```rust
// 快速路径：我是最后一个引用，无需阻塞
if inner.count.fetch_sub(1, Ordering::AcqRel) == 1 {
    drop(inner.lock.lock().unwrap());   // 先抢锁再 notify
    inner.cvar.notify_all();
    return;
}
// 阻塞路径：在持锁状态下循环检查计数
let mut guard = inner.lock.lock().unwrap();
while inner.count.load(Ordering::Acquire) != 0 {
    guard = inner.cvar.wait(guard).unwrap();
}
```

`while` 而非 `if`，是为了应对条件变量的**伪唤醒（spurious wakeup）**——线程可能在没有 `notify` 的情况下醒来，必须重新检查条件。

**`Drop` 与 `Clone`：** 这两个 trait 把「任务登记/完成」具象化。

[wait_group.rs:134-142](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L134-L142) — `Drop`：`fetch_sub(1, Release)`，若减前为 `1`（我是最后离开的）就抢锁 + `notify_all`，唤醒阻塞中的 `wait`。

[wait_group.rs:144-151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L144-L151) — `Clone`：`fetch_add(1, Relaxed)`。这里只要计数自增、不需要建立 happens-before，所以用最便宜的 `Relaxed`。

**生产环境真实使用点：** 作用域线程 `scope` 用 `WaitGroup` 等待所有嵌套子作用域退出。

[thread.rs:159-175](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L159-L175) — `scope` 创建 `WaitGroup`，每个嵌套 `Scope` 克隆一份存进 `wait_group` 字段；外层 `scope` 收尾时 `drop(scope.wait_group); wg.wait();`，确保所有嵌套作用域都先于自己结束。这是 [u2-l7 作用域线程实现内幕](u2-l7-scoped-threads-internals.md) 会展开的内容，这里先记住「`WaitGroup` 在 crossbeam 自己的代码里就被用来做 join 同步」。

#### 4.1.4 代码实践

**实践目标：** 用 `WaitGroup` 协调 4 个工作线程，主线程等它们全部完成后汇总结果。

**操作步骤：**

1. 仓库自带测试是最权威的范例。先读 [`tests/wait_group.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/wait_group.rs)，看懂 `wait()` 与 `wait_and_drop()` 两个测试的时序。
2. 运行测试，确认环境正常：

   ```bash
   cargo test -p crossbeam-utils --test wait_group
   ```

3. 仿照文档注释里的例子，自己写一个最小示例（**示例代码**，非项目原有）：

   ```rust
   use crossbeam_utils::sync::WaitGroup;
   use std::sync::Arc;
   use std::sync::atomic::{AtomicUsize, Ordering};
   use std::thread;

   fn main() {
       let wg = WaitGroup::new();
       let sum = Arc::new(AtomicUsize::new(0));

       for _ in 0..4 {
           let wg = wg.clone();          // count: 1 -> 5（4 个 clone + 根）
           let sum = Arc::clone(&sum);
           thread::spawn(move || {
               sum.fetch_add(25, Ordering::Relaxed);
               drop(wg);                  // 任务完成，count - 1
           });
       }

       wg.wait();                         // 消费根引用并等待 count 归零
       println!("total = {}", sum.load(Ordering::Relaxed));
   }
   ```

**需要观察的现象：**
- `wg.wait()` 之前的 `println` 不可能出现「total = 100」之外的值——它一定在所有 4 个 `drop(wg)` 之后才执行。
- 若注释掉 `wg.wait()`，主线程可能提前打印一个小于 100 的值。

**预期结果：** 打印 `total = 100`。测试命令应输出 `2 passed`（`wait` 与 `wait_and_drop`）。

> 若你只是源码阅读型实践：对照 4.1.3 的源码，画出「主线程 `wait` fetch_sub 到 4，阻塞；4 个 worker 依次 drop，最后一个 fetch_sub(Release)==1 抢锁 notify_all；主线程醒来 count==0 退出循环」的时序，标注每一步的 `Ordering`。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `Clone` 用 `Ordering::Relaxed`，而 `Drop`/`wait` 用 `Release`/`AcqRel`？

**参考答案：** `clone` 只是给计数 `+1`，不发布任何需要被其他线程看到的数据，也不需要读取别人的写入，`Relaxed` 足矣且最便宜。`Drop`/`wait` 减计数时，可能伴随「任务产生的结果数据已写好」这一事实，需要 `Release` 把这些写入发布出去；`wait` 的 `AcqRel` 还要在快速路径里同时具备 release（发布自己的减法）和 acquire（快速路径不阻塞就返回前要看到先前状态）语义。等待方在 `while` 里用 `Acquire` 读取，正是为了和这些 `Release` 配对，确保看到最后一个减法。

**练习 2：** 若把 `wait` 里的 `while` 改成 `if`，会出现什么问题？

**参考答案：** 条件变量允许**伪唤醒**——线程可能在没人 `notify` 的情况下醒来。用 `if` 时，一次伪唤醒就会让等待者在 `count != 0` 的情况下错误地继续往下走，破坏「等所有人完成」的语义。`while` 保证每次醒来都重新校验条件。

**练习 3：** `new()` 时计数初始化为 `1` 而非 `0`。如果初始化成 `0`，上面的示例会发生什么？

**参考答案：** 计数会从 `0` 开始，4 次 clone 后变成 `4`。主线程调用 `wait` 时 `fetch_sub` 把它从 `4` 减到 `3`，不等于 `1`，于是阻塞；4 个 worker 各 drop 一次，最后一个把计数从 `1` 减到 `0` 时 `== 1` 成立，notify 唤醒主线程——看起来也能跑通。但「初始为 1」的真正意义在于语义清晰：它代表「`wait` 的调用者本身就是一个引用」，并保证「在 `wait` 被调用前计数永不为 0」，从而避免在「还没决定谁来 wait」时被误判为「所有任务完成」。

---

### 4.2 ShardedLock：分片读写锁

#### 4.2.1 概念说明

`ShardedLock<T>` 在 API 上和 `std::sync::RwLock<T>` 几乎一样：`read()` 返回共享读锁、`write()` 返回独占写锁。它的设计取舍写在文档里——**读更快、写更慢**。

为什么读能更快？回想 [u2-l2 伪共享](u2-l2-cache-padded.md)：标准 `RwLock` 即便允许多读者并发，所有读者仍要更新同一个「读者计数」字段，该字段所在缓存行在核间乒乓弹射，核越多越慢。`ShardedLock` 的对策是把锁**切成 `NUM_SHARDS = 8` 份**（必须是 2 的幂），每份是一个独立的、按缓存行对齐的 `RwLock`：

- **读路径：** 根据当前线程的索引选**其中一个**分片，只锁这一把锁。8 个线程各选不同分片时，互不干扰，几乎零争用。
- **写路径：** 必须依次锁**全部 8 个**分片，才能保证没有任何读者在访问数据。所以写更慢。

用一句话总结：**用更贵的写，换更便宜的读**，适用于读多写少的高并发场景。

#### 4.2.2 核心流程

读路径的选片公式（`NUM_SHARDS = 8`，故掩码是 `8 - 1 = 7`）：

\[ \text{shard\_index} = \text{thread\_index} \ \& \ (\text{NUM\_SHARDS} - 1) \]

读流程：

```
read():
    idx = current_index().unwrap_or(0)     # 线程局部注册表给的稳定索引
    shard = shards[idx & 7]
    return shard.lock.read()               # 只锁一个分片
```

写流程要锁全部，并且要安全地「同时持有 8 把写锁」：

```
write():
    for shard in shards:                   # 依次锁全部 8 个
        g = shard.lock.write()
        把 g 存进 shard.write_guard 单元    # 见下方 unsafe 技巧
    返回一个轻量 ShardedLockWriteGuard（本身不持有 RwLockWriteGuard）
# guard 的 Drop：逆序遍历 8 个分片，逐个 take 并 drop 写锁
```

这里有个**关键 unsafe 技巧**：`RwLockWriteGuard` 借自 `&self.shards`，其真实生命周期是 `ShardedLock` 的存活期（记作 `'a`），但源码把它 `mem::transmute` 成 `'static` 存进分片内的 `UnsafeCell`。为什么安全？因为返回的 `ShardedLockWriteGuard<'a, T>` 持有 `&'a ShardedLock<T>`，它的生命周期把整个 `ShardedLock` 钉住——guard 不 drop，`ShardedLock` 就不能被销毁，那些 `'static` 写锁也就不会悬空。guard 的 `Drop` 又保证在 `ShardedLock` 还活着时逆序释放全部写锁。

**毒化（poisoning）语义：** 文档明确说——只有写操作 panic 才会毒化锁，读操作 panic 不会。这继承自标准库 `RwLock` 的行为（读 guard 不会毒化），再加上写路径**必定写锁第 0 个分片**这一事实，于是 `is_poisoned()` 只查 `shards[0]` 就足够了。

#### 4.2.3 源码精读

**分片数与分片布局：**

[sharded_lock.rs:21-34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L21-L34) — `NUM_SHARDS = 8`；`Shard` 含 `lock: RwLock<()>` 与 `write_guard: UnsafeCell<Option<RwLockWriteGuard<'static, ()>>>`。

[sharded_lock.rs:82-88](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L82-L88) — `ShardedLock` 持有 `Box<[CachePadded<Shard>]>` 和数据 `UnsafeCell<T>`。每个分片外面包了 `CachePadded`（[u2-l2](u2-l2-cache-padded.md)），让 8 把锁各占独立缓存行，**分片之间不互相伪共享**——这正是分片能降争用的物理前提。

**构造：**

[sharded_lock.rs:106-118](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L106-L118) — `new` 生成 `NUM_SHARDS` 个 `CachePadded<Shard>`，每个内置一把空 `RwLock`。

**读路径：** 选片 + 只锁一个分片。

[sharded_lock.rs:290-308](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L290-L308) — `read()` 全文。核心两行：

```rust
let current_index = current_index().unwrap_or(0);
let shard_index = current_index & (self.shards.len() - 1);   // & 7
// ...
self.shards[shard_index].lock.read()
```

[sharded_lock.rs:230-252](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L230-L252) — `try_read()` 用相同选片逻辑，只是把 `read` 换成 `try_read`，并把标准库的 `Poisoned` 错误正确转译出来。

**写路径：** 锁全部 + 把 guard 存进各分片。

[sharded_lock.rs:414-447](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L414-L447) — `write()` 全文。关键片段：

```rust
for shard in self.shards.iter() {
    let guard = shard.lock.write()?;              // 依次写锁每个分片
    unsafe {
        let guard: RwLockWriteGuard<'static, ()> = mem::transmute(guard); // 擦成 'static
        *shard.write_guard.get() = Some(guard);    // 存进分片单元
    }
}
```

返回的 `ShardedLockWriteGuard` 结构体里**并不持有**这些 `RwLockWriteGuard`——它们散落在 8 个分片的 `write_guard` 单元里，由 guard 的 `Drop` 负责回收（这就是为何需要 `'static` 擦除：guard 不能直接持有「借自 `self`」的引用，否则自引用导致无法构造）。

[sharded_lock.rs:334-382](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L334-L382) — `try_write()`：与 `write` 类似，但一旦某个分片 `WouldBlock`，就要**逆序释放**已经锁上的前 `i` 个分片，避免死锁/残留。

**写 guard 的释放：**

[sharded_lock.rs:529-540](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L529-L540) — `ShardedLockWriteGuard::drop`：逆序遍历 8 个分片，`take()` 出每把写锁并 drop。逆序是惯例（与加锁顺序相反），避免在某些锁实现下产生不必要的等待。

**毒化检查：**

[sharded_lock.rs:173-175](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L173-L175) — `is_poisoned()` 只查 `shards[0].lock.is_poisoned()`。因为写路径必锁全部分片（含第 0 个），写 panic 一定会让第 0 个分片毒化；读路径只读锁一个分片且标准库读 guard 不毒化，故查第 0 个就够。测试 [`arc_no_poison_rr`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/sharded_lock.rs#L77-L88) 与 [`arc_no_poison_sl`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/sharded_lock.rs#L89-L100) 印证了「读 panic 不毒化」。

#### 4.2.4 代码实践

**实践目标：** 直观感受「读多写少时 `ShardedLock` 比 `std::sync::RwLock` 扩展性更好」。

**操作步骤：**

1. 先跑仓库自带的 `ShardedLock` 测试，确认行为符合文档：

   ```bash
   cargo test -p crossbeam-utils --test sharded_lock
   ```

   重点观察 `frob` 测试（[sharded_lock.rs:25-49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/sharded_lock.rs#L25-L49)）：10 个线程、每个线程随机做 1000 次 read 或 write，验证高并发混合负载下不崩溃。

2. 写一个最小对比基准（**示例代码**，非项目原有，需依赖 `std::sync::RwLock` 与 `crossbeam_utils::sync::ShardedLock`）：

   ```rust
   use std::sync::{Arc, RwLock};
   use std::thread;
   use std::time::Instant;
   use crossbeam_utils::sync::ShardedLock;

   fn bench_rwlock(readers: usize, iters: usize) -> u128 {
       let v = Arc::new(RwLock::new(0u64));
       let start = Instant::now();
       let hs: Vec<_> = (0..readers).map(|_| {
           let v = Arc::clone(&v);
           thread::spawn(move || {
               let mut sum = 0u64;
               for _ in 0..iters { sum += *v.read().unwrap(); }
               sum
           })
       }).collect();
       for h in hs { h.join().unwrap(); }
       start.elapsed().as_millis()
   }

   fn bench_sharded(readers: usize, iters: usize) -> u128 {
       let v = Arc::new(ShardedLock::new(0u64));
       let start = Instant::now();
       let hs: Vec<_> = (0..readers).map(|_| {
           let v = Arc::clone(&v);
           thread::spawn(move || {
               let mut sum = 0u64;
               for _ in 0..iters { sum += *v.read().unwrap(); }
               sum
           })
       }).collect();
       for h in hs { h.join().unwrap(); }
       start.elapsed().as_millis()
   }

   fn main() {
       let (readers, iters) = (8, 5_000_000);
       println!("RwLock   : {} ms", bench_rwlock(readers, iters));
       println!("Sharded  : {} ms", bench_sharded(readers, iters));
   }
   ```

**需要观察的现象：**
- 在 8 线程纯读、单核或双核机器上差距可能不明显；在 ≥ 4 物理核上，`ShardedLock` 的耗时通常更低，因为它把读者计数争用分摊到了 8 个缓存行。
- 把 `readers` 从 1 调到 16：`RwLock` 的耗时会随线程数上升更快（真共享争用），`ShardedLock` 上升更平缓。

**预期结果：** **待本地验证**——具体耗时取决于机器核数与缓存结构。可以确信的结论是「核越多、读越多，`ShardedLock` 的相对优势越大」；若在你的机器上未观察到差异，请检查是否物理核数不足或被超线程/SMT 影响。

> 源码阅读型替代实践：对照 4.2.3 的 `read()`，解释「为什么把 `NUM_SHARDS` 设成 16 不会让写变快、反而让单次写更慢」——因为写路径的循环要锁更多分片。

#### 4.2.5 小练习与答案

**练习 1：** `write()` 把每把 `RwLockWriteGuard` `transmute` 成 `'static` 存进 `UnsafeCell`，凭什么不会 use-after-free？

**参考答案：** 返回的 `ShardedLockWriteGuard<'a, T>` 持有 `&'a ShardedLock<T>`，借用检查保证只要 guard 还在，`ShardedLock`（连同其 `shards`）就不能被销毁；而那些 `'static` 写锁真正借用的内存就是 `shards`，所以 `ShardedLock` 活着它们就有效。guard 的 `Drop` 又在 `ShardedLock` 仍存活期间逆序释放全部写锁，不会留下悬空指针。`'static` 只是绕过自引用导致的「guard 不能含借自 self 的字段」的构造难题，并非真有跨线程随意持有的语义。

**练习 2：** 为什么 `NUM_SHARDS` 必须是 2 的幂？

**参考答案：** 选片用的是位运算 `index & (NUM_SHARDS - 1)`，这等价于 `index % NUM_SHARDS` **当且仅当** `NUM_SHARDS` 是 2 的幂。位掩码比取模快得多，且能保证结果落在 `[0, NUM_SHARDS)`。若 `NUM_SHARDS` 不是 2 的幂，`& (N-1)` 会产生越界或分布错误。

**练习 3：** `try_write()` 在第 `i` 个分片 `WouldBlock` 时，为什么要逆序释放前 `i` 个分片，而不是顺序释放？

**参考答案：** 释放顺序与加锁顺序相反是并发锁的标准惯例（防止「锁 convoy」/减少等待链路）。更重要的是，这里已经锁上的分片是 `shards[0..i]`，逆序释放 `rev()` 让最后锁上的分片最先释放，与 `ShardedLockWriteGuard::drop` 的释放顺序保持一致，行为统一、可预测。

---

### 4.3 线程局部注册表与 OnceLock

#### 4.3.1 概念说明

4.2 节的 `read()`/`write()` 都调用了 `current_index()` 来决定选哪个分片。这个索引从哪来？直接用 `ThreadId` 吗？不行——`ThreadId` 是个不保证连续的 `u64`，可能很大且分布稀疏，`& 7` 后分布不均。`ShardedLock` 需要的是「**每个线程一个连续的小整数编号，且线程退出后能回收复用**」。这就是 `sharded_lock.rs` 文件下半段的「线程注册表」要解决的问题。

注册表本身是个全局单例。Rust 里实现「懒加载的全局可变单例」的标准做法是 `OnceLock`（一次性初始化锁）：第一次访问时跑初始化闭包，之后所有访问直接拿现成值。本仓库 MSRV 1.74 下标准库的 `OnceLock` 仍有可用性顾虑，于是 crossbeam 在 `once_lock.rs` 里 vendor 了一份，基于更底层的 `std::sync::Once`。

#### 4.3.2 核心流程

整套机制由三部分协作：

```
① 全局注册表（OnceLock<Mutex<ThreadIndices>>）
   mapping:  ThreadId -> 线程索引
   free_list: 已回收的可复用索引
   next_index: 下一个全新索引

② 线程局部变量 REGISTRATION: Registration
   首次访问时（lazy）：
       - 查 free_list 有没有回收的索引，有就复用，没有就 next_index++
       - 在 mapping 里登记 (ThreadId -> index)
   线程退出时（Registration::drop）：
       - 从 mapping 移除自己
       - 把自己的 index 推回 free_list

③ current_index()：直接读 TLS 里的 REGISTRATION.index（O(1)，无锁）
```

这样每个线程拿到一个**稳定、连续、可复用**的索引，分片选择就均匀了。

`OnceLock<T>` 本身的核心流程：

```
get_or_init(f):
    if once.is_completed():    # 快速路径：已初始化，直接返回
        return get_unchecked()
    initialize(f):             # 慢速路径：call_once 保证 f 只跑一次
        once.call_once(|| slot.write(f()))
    return get_unchecked()
```

#### 4.3.3 源码精读

**`current_index`：** 极简——只读 TLS。

[sharded_lock.rs:577-580](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L577-L580) — `current_index()` 通过 `REGISTRATION.try_with(|reg| reg.index)` 读 TLS。`.ok()` 把「TLS 正在销毁」的情况转成 `None`，调用方（4.2 节的 `read`/`write`）用 `unwrap_or(0)` 兜底为第 0 个分片。

**全局注册表与 `OnceLock`：**

[sharded_lock.rs:583-604](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L583-L604) — `ThreadIndices` 结构（`mapping`/`free_list`/`next_index`）与 `thread_indices()` 函数。后者用 `static THREAD_INDICES: OnceLock<Mutex<ThreadIndices>>` 懒加载：

```rust
fn thread_indices() -> &'static Mutex<ThreadIndices> {
    static THREAD_INDICES: OnceLock<Mutex<ThreadIndices>> = OnceLock::new();
    THREAD_INDICES.get_or_init(init)   // 首次访问才初始化
}
```

**线程退出时回收索引：**

[sharded_lock.rs:606-620](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L606-L620) — `Registration::drop`：从 `mapping` 移除自己，把自己的 `index` 推回 `free_list`，供后续新建的线程复用。这就是「索引可回收」的来源。

**线程首次访问时分配索引：**

[sharded_lock.rs:622-642](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L622-L642) — `thread_local! { static REGISTRATION }` 的初始化表达式：先 `free_list.pop()` 试图复用，没有就 `next_index += 1` 新分配，再写入 `mapping`。

**`OnceLock` 实现：** 把 `Once`（保证闭包只跑一次）与 `UnsafeCell<MaybeUninit<T>>` 组合。

[once_lock.rs:8-13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs#L8-L13) — `OnceLock<T>` 由 `once: Once` 与 `value: UnsafeCell<MaybeUninit<T>>` 组成。`MaybeUninit` 因为值在首次 `get_or_init` 之前确实未初始化。

[once_lock.rs:43-69](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs#L43-L69) — `get_or_init`：先用 `is_completed()` 做无锁快速路径；未完成则进 `#[cold]` 的 `initialize`，用 `Once::call_once` 保证 `f()` 全局只跑一次并写入槽位。多个线程并发 `get_or_init` 时，`Once` 内部会阻塞其他线程直到第一个完成初始化。

[once_lock.rs:80-89](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs#L80-L89) — `Drop`：仅当 `once.is_completed()` 时才 `drop_in_place` 释放内部 `T`，避免对未初始化内存调用析构。

#### 4.3.4 代码实践

**实践目标：** 验证「线程退出后索引被回收复用」，从而理解为何分片分布长期保持均匀。

**操作步骤：**

1. 在 `sharded_lock.rs` 的 `Registration` 初始化处与 `drop` 处临时加调试打印（**示例代码**，仅用于观察，验证后请还原）：

   ```rust
   // 在 thread_local! 初始化表达式里，拿到 index 后：
   eprintln!("[+] thread {:?} -> index {}", thread_id, index);
   // 在 Registration::drop 里：
   eprintln!("[-] thread {:?} <- index {}", self.thread_id, self.index);
   ```

2. 写一个反复 spawn-join 的程序（**示例代码**），让线程不断创建与销毁：

   ```rust
   use crossbeam_utils::sync::ShardedLock;
   use std::thread;

   fn main() {
       let l = ShardedLock::new(0u64);
       for round in 0..3 {
           let mut hs = vec![];
           for _ in 0..10 {
               let l = &l;            // 注意：这里需要 Arc 才能 move，见下方说明
           }
           // （为简洁略去 Arc 包装；实际请用 Arc::new 包 ShardedLock 再 clone 进线程）
       }
       let _ = l.read().unwrap();
   }
   ```

   > 说明：上面这段只为示意调用结构。要真正跨线程共享 `ShardedLock` 必须用 `Arc<ShardedLock<_>>`（参考 [sharded_lock.rs:103-138](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/sharded_lock.rs#L103-L138) 的 `arc` 测试）。请补全为 `Arc::new(ShardedLock::new(0u64))`，每个 spawn 线程 `Arc::clone` 后做一次 `l.read()`。

**需要观察的现象：**
- 第一轮 spawn 10 个线程，`[+]` 会打印索引 `0..10`（或从主线程已占用的之后开始）。
- 每轮线程 join 后会打印对应的 `[-]`。
- 第二轮再 spawn 10 个线程时，`[+]` 打印的索引会**复用**上一轮回收到 `free_list` 的那些小整数，而不是继续增长到 `20+`。

**预期结果：** 索引在长期运行中保持稳定的小范围（围绕并发峰值，而非累计线程总数）。**待本地验证**具体的索引数值——不同 Rust 版本/线程库调度下，线程获得索引的先后可能略有差异，但「回收复用」这一行为是一定的。

> 纯源码阅读型实践：对照 [once_lock.rs:43-56](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs#L43-L56) 解释：若把快速路径的 `is_completed()` 检查删掉，程序仍正确但变慢——为什么？答：`Once` 本身能保证只初始化一次，但每次都走 `call_once` 的慢路径（含原子同步），快速路径的 `is_completed()` 让已初始化后的访问退化为一次 `Relaxed`-ish 读取。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `current_index()` 用线程局部变量，而不是每次都查全局 `mapping`？

**参考答案：** 查全局 `mapping` 需要锁 `Mutex`，每次 `read()`/`write()` 都要锁一次全局表，会把分片好不容易降下来的争用又集中到这一把锁上。线程局部变量让索引在首次访问时算好并缓存到本线程栈，之后 `current_index()` 是无锁的 `O(1)` 读取，零争用。

**练习 2：** `OnceLock::get_or_init` 里，为什么先 `if self.once.is_completed()` 判断再走 `initialize`？

**参考答案：** 这是经典的「双重检查」快速路径。绝大多数调用发生在初始化完成之后，`is_completed()` 是一次很便宜的读取，让这些调用不必进入 `call_once` 的慢路径（后者涉及 heavier 的原子同步与可能的阻塞等待）。只有首个（或并发竞争中的少数）调用才会真正进入 `initialize`。

**练习 3：** 若线程不在 `ShardedLock` 上做任何操作，它的 `REGISTRATION` 会被创建吗？

**参考答案：** 不会。`thread_local!` 的变量是**延迟初始化**的——只有在该线程首次访问它（即首次调用 `current_index()`，也就是首次 `read()`/`write()`）时才会运行初始化表达式。从不碰 `ShardedLock` 的线程不会占用注册表里的索引。

---

## 5. 综合实践

把本讲三个模块串起来，搭一个「**读多写少的工作池，并用 WaitGroup 做汇合**」的小任务。

**任务描述：**
- 用 `Arc<ShardedLock<u64>>` 持有一份共享「配置值」。
- 用 `WaitGroup` 协调 8 个工作线程。
- 每个工作线程：循环 100 万次 `read()` 读配置值并累加；其中 1 个工作线程在循环到一半时做一次 `write()` 把配置值改大。
- 主线程 `wg.wait()` 后，再 `read()` 打印最终配置值。

**要求回答的问题（写进你的实验笔记）：**
1. 多个工作线程同时 `read()`，它们是否落在**不同的分片**上？（提示：依据 4.3 的注册表，前 8 个线程索引通常是 `0..8`，恰好均匀分布到 8 个分片。）
2. 当那个写线程调用 `write()` 时，它要锁几个分片？此时其它读线程会发生什么？
3. 把 `ShardedLock` 换成 `std::sync::RwLock` 重跑，在多核机器上对比总耗时。

**参考骨架（示例代码，非项目原有）：**

```rust
use std::sync::Arc;
use std::thread;
use crossbeam_utils::sync::{ShardedLock, WaitGroup};

fn main() {
    let cfg = Arc::new(ShardedLock::new(1u64));
    let wg = WaitGroup::new();

    for id in 0..8 {
        let cfg = Arc::clone(&cfg);
        let wg = wg.clone();
        thread::spawn(move || {
            let mut acc = 0u64;
            for i in 0..1_000_000 {
                acc = acc.wrapping_add(*cfg.read().unwrap());
                if id == 0 && i == 500_000 {
                    let mut w = cfg.write().unwrap();
                    *w += 1;                       // 写路径锁全部 8 个分片
                }
            }
            let _ = acc;                            // 防止优化掉
            drop(wg);                               // 通知完成
        });
    }

    wg.wait();                                      // 等全部 8 个线程结束
    println!("final cfg = {}", *cfg.read().unwrap());
}
```

**预期结果：** 程序正常结束，打印 `final cfg = 2`（唯一的写线程把 1 改成 2）。耗时差异**待本地验证**。本题的核心收获是：你能向别人解释清楚 `WaitGroup` 的计数何时归零、`ShardedLock` 的读写各锁了哪些分片、以及线程索引如何被分配与回收。

---

## 6. 本讲小结

- **`WaitGroup`** 是「引用计数 + 条件变量」的同步原语：`clone` 加计数、`drop`/`wait` 减计数，归零时唤醒等待者；通知方「先改计数再抢锁再 notify」、等待方「持锁时检查计数」，二者配合彻底杜绝丢失唤醒。
- **`ShardedLock`** 把一把读写锁切成 8 个 `CachePadded` 分片：读只锁一个分片（按线程索引选），写锁全部 8 个分片，用「更慢的写」换「更可扩展的读」，适合读多写少场景。
- **写路径的 `transmute 'static` 技巧**让写 guard 不必自引用那些「借自 self」的分片写锁，安全性由 guard 持有的 `&'a ShardedLock` 钉住生命周期、并由 `Drop` 逆序释放来保证。
- **线程局部注册表**通过全局 `OnceLock<Mutex<ThreadIndices>>` 给每个线程分配连续、可回收的小整数索引，使分片选择均匀；`OnceLock` 自身是 `Once + UnsafeCell<MaybeUninit<T>>` 的 vendor 实现，带快速路径。
- 这两个原语都**只启用 `crossbeam_loom` 之外的代码路径**（见 [`sync/mod.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/mod.rs)），而 `WaitGroup` 在 crossbeam 自己的 `scope` 实现里就被用来做 join 同步，是连接本讲与 u2-l7 的桥梁。

## 7. 下一步学习建议

- 下一步推荐学习 **[u2-l7 作用域线程实现内幕](u2-l7-scoped-threads-internals.md)**：你会看到 `scope` 如何用本讲的 `WaitGroup` 等待嵌套子作用域、用 `SharedVec` 管理句柄，以及把 `'env` 闭包转成 `'static` 的 unsafe 技巧与 panic 收集。本讲已经为它铺垫了 `WaitGroup` 这一关键积木。
- 如果你更关心消息传递，可以跳到 **[u3 channel 总览](u3-l1-channel-overview.md)**：channel 的阻塞/唤醒（context.rs/waker.rs）会反复用到本讲和 u2-l5 建立的 condvar/通知模型。
- 建议继续精读的源码：`crossbeam-utils/src/sync/wait_group.rs`（约 160 行，建议逐行读完）、`sharded_lock.rs` 的 `write()`/`try_write()`/`Drop` 三处 unsafe（体会自引用与生命周期擦除的等价性）、`once_lock.rs`（作为「标准库不稳定 API 的 vendor 范例」）。
