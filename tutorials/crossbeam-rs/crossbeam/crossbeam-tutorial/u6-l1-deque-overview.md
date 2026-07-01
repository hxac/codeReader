# u6-l1 工作窃取模型与公共 API

## 1. 本讲目标

本讲是 crossbeam-deque 单元的入门篇。我们不深入 Chase-Lev 双端队列的算法细节（那是 u6-l2 的主题），而是先站在「使用者」视角，把整个 crate 的**公共 API 表面**看清楚。

读完本讲，你应当能够：

1. 说清楚工作窃取（work-stealing）调度模型为什么需要三类角色：`Injector`（全局 FIFO 注入器）、`Worker`（线程私有 FIFO/LIFO 队列）、`Stealer`（可共享的窃取句柄）。
2. 用 `Worker::new_fifo` / `new_lifo` / `stealer()`、`Injector::new` / `push` 搭出最基本的生产者结构。
3. 区分三种窃取操作 `steal` / `steal_batch` / `steal_batch_and_pop` 的语义。
4. 理解 `Steal` 枚举的三个变体 `Empty` / `Success` / `Retry`，并能用 `or_else` 与 `FromIterator` 写出正确的「重试循环」。

本讲只读两个文件：门面 [crossbeam-deque/src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs) 与实现 [crossbeam-deque/src/deque.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs)。后者虽大，本讲只取其中「公开类型与方法」的骨架。

---

## 2. 前置知识

### 2.1 为什么需要工作窃取

设想一个线程池有 N 个工作线程。最朴素的方案是所有任务都丢进**一个**全局队列，每个线程去抢——但这把所有争用集中到一个点上，队列成了瓶颈。

工作窃取（work-stealing）的解法是**分布式队列**：

- 每个工作线程拥有一个**本地队列**，自己 push/pop 的任务绝大多数只跟自己打交道，几乎无争用。
- 当本地队列空了，线程再去**偷（steal）**别人的任务。偷是低频、并发的操作，可以容忍稍重的同步代价。

这样「高频的自取」走快路径、「低频的互偷」走慢路径，整体吞吐远高于单一全局队列。这套模型被 Go runtime、Java ForkJoinPool、Tokio、Rayon 等广泛采用。

### 2.2 两个方向：FIFO 与 LIFO

双端队列（deque = double-ended queue）两端都能进出。crossbeam 的 `Worker` 提供两种 flavor：

- **FIFO**（先进先出）：push 和 pop 在**相反**的两端，任务按入队顺序执行。
- **LIFO**（后进先出）：push 和 pop 在**同一**端，刚 push 的任务最先被自己 pop 出来。

为什么调度器常选 LIFO？因为它带来**缓存局部性**（最近 push 的任务数据还在 cache 里）和**递归任务窃取的平衡**（自己从栈顶拿最新任务，被偷的从栈底拿最老任务，两者不撞）。而 `Injector`（全局入口）永远是 FIFO，保证外部注入任务的公平性。

### 2.3 复习：并发原语基础

本讲依赖你在前面几讲建立的认知：

- **CachePadded 与伪共享**（u2-l2）：队列的 head/tail 索引被多核高频原子改写，必须按缓存行对齐。你会看到 `Arc<CachePadded<Inner<T>>>` 这样的字段。
- **wrapping 算术防 ABA**（u4-l1）：用单调递增的 `isize` 索引而非「真实下标」标识位置，借回绕天然区分「同一槽的新旧两次占用」。
- **epoch Guard 与延迟回收**（u5-l3）：窃取时要读 buffer 指针，而 owner 可能正在扩容换 buffer，旧 buffer 不能立即释放——这要用到 `epoch::pin()`。本讲只点出它的存在，细节留给 u6-l2。

> 关键术语速查：**deque**（双端队列）、**owner**（worker 持有者线程）、**stealer**（窃取者）、**flavor**（FIFO/LIFO 二选一的风味）、**spurious failure**（伪失败，CAS 竞争未成功但非真错）。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲取用部分 |
| --- | --- | --- |
| `crossbeam-deque/src/lib.rs` | crate 门面：`#![no_std]`、模块文档（含工作窃取说明与 `find_task` 典范示例）、`pub use` 导出 4 个公开类型 | 模块文档、`pub use` |
| `crossbeam-deque/src/deque.rs` | 全部实现：`Buffer`、`Inner`（Chase-Lev 内核）、`Worker`、`Stealer`、`Injector`、`Steal` | 仅 4 个公开类型与公开方法 |

crate 对外只暴露 **4 个公开类型**（见 [lib.rs 的 pub use](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L103-L109)）：

```rust
#[cfg(feature = "std")]
pub use crate::deque::{Injector, Steal, Stealer, Worker};
```

注意三件事：

1. `#![no_std]`（[lib.rs:85](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L85)）：声明 no_std，但整个实现挂在 `feature = "std"` 后（[lib.rs:106-109](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L106-L109)）。Cargo.toml 也明确写：「Disabling `std` feature is not supported yet」——也就是说 crossbeam-deque 目前**只在 std 下可用**，no_std 只是声明意图、为未来留口子。
2. 实现依赖 `crossbeam-epoch` 与 `crossbeam-utils`（见 [Cargo.toml:35-37](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/Cargo.toml#L35-L37)），二者都 `default-features = false`，再由 `std` 特性连带点亮（[Cargo.toml:33](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/Cargo.toml#L33)）。这正是 u1-l2 讲过的「特性层层传递」。
3. 关键词 `chase-lev` / `lock-free` / `scheduler` 写在 [Cargo.toml:15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/Cargo.toml#L15)，点明算法血统。

---

## 4. 核心概念与源码讲解

### 4.1 角色与构造：Worker / Stealer / Injector

#### 4.1.1 概念说明

crossbeam-deque 用三种类型把工作窃取模型的三类角色直接固化进类型系统：

| 类型 | 角色 | 所有权 / 并发标记 | 主要操作 |
| --- | --- | --- | --- |
| `Worker<T>` | 线程私有队列 | `Send`，但 `!Sync`（只能单线程用） | `push` / `pop` |
| `Stealer<T>` | 他人偷本 worker 任务的句柄 | `Send + Sync + Clone` | `steal` / `steal_batch` / `steal_batch_and_pop` |
| `Injector<T>` | 全局共享 FIFO 入口 | `Send + Sync` | `push` / `steal` / `steal_batch` / `steal_batch_and_pop` |

核心设计思想是**用所有权与 trait 约束区分快慢路径**：

- `Worker` 的 push/pop 是**单线程**操作（`!Sync` 由 `PhantomData<*mut ()>` 保证，见 [deque.rs:208](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L208)），因此可以省去大部分同步——这是「自取快路径」。
- `Stealer` 可被任意多线程共享并克隆，它的 `steal` 走完整 CAS 协议——这是「互偷慢路径」。
- 同一份内部状态 `Arc<CachePadded<Inner<T>>>` 被 `Worker` 与它的所有 `Stealer` 共享（见 `stealer()` 实现就是 `self.inner.clone()`，[deque.rs:282-287](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L282-L287)）。

`Injector` 与前两者不同：它没有 owner，本身就是共享 FIFO 队列，任何线程都能往里 push 或从中 steal。它对应调度器里那个「全局任务入口」。

> **为什么要分 Worker 与 Stealer 两个类型，而不是一个？** 因为 push/pop（owner 独占端）与 steal（他人端）的并发契约截然不同：前者单线程无需同步，后者多线程必须 CAS。把它们拆成两个类型，让编译器强制「只有 owner 能 pop」「只有别人需要 steal」，杜绝误用。

#### 4.1.2 核心流程

一个典型工作窃取调度器的数据结构搭建过程：

```text
1. 创建一个全局 Injector<T>（所有线程可见）。
2. 为每个工作线程 i 调用 Worker::new_fifo()（或 new_lifo()）得到 worker_i。
3. 对每个 worker_i 调用 stealer() 得到 stealer_i，收集成 Vec<Stealer<T>> 分发给所有线程。
4. 每个工作线程的运行循环：
   a. worker_i.pop()              # 先取本地（快路径，无争用）
   b. 失败 → Injector.steal_batch_and_pop(&worker_i)   # 再从全局入口批量偷
   c. 还失败 → 随机遍历 stealers，逐个 stealer.steal()  # 最后偷同伴
   d. 全空 → 让出 / 阻塞，等待新任务被注入。
5. 外部线程（或主线程）通过 Injector.push(task) 投递新任务唤醒空闲 worker。
```

这正是 [lib.rs 模块文档](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L1-L50) 描述的模型：

> 每个 worker 线程循环等待下一个任务，找到后执行；查找顺序是先本地 worker 队列，再 injector，再 stealers。

#### 4.1.3 源码精读

**Worker 的结构与构造函数。** `Worker` 内部持有一份共享内核 `inner`、一份本地可见的 buffer 快照、以及 flavor 标记：

> [crossbeam-deque/src/deque.rs:197-209](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L197-L209) —— `Worker` 结构：`inner: Arc<CachePadded<Inner<T>>>` 是与 stealer 共享的 Chase-Lev 内核；`buffer: Cell<Buffer<T>>` 是 owner 线程私有的 buffer 快照（单线程可变，故用 `Cell`）；`flavor` 区分 FIFO/LIFO；`_marker: PhantomData<*mut ()>` 让 `Worker` 既不 `Send` 错误地跨线程，也不 `Sync`。

`new_fifo` 与 `new_lifo` 几乎一模一样，唯一差别只在 `flavor` 字段：

> [crossbeam-deque/src/deque.rs:214-268](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L214-L268) —— `new_fifo` / `new_lifo`：分配初始 `Buffer::alloc(MIN_CAP)`（`MIN_CAP = 64`，见 [deque.rs:18](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L18)），构造 `Inner{front:0, back:0, buffer}`，把同一份 `Arc` 既存进 `inner`、又把 `buffer` 快照存进 `Cell`。两者唯一区别是 `flavor: Flavor::Fifo` vs `Flavor::Lifo`。

> [crossbeam-deque/src/deque.rs:147-155](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L147-L155) —— `Flavor` 枚举只有 `Fifo` / `Lifo` 两个变体，是 `Worker` 与 `Stealer` 都持有的标记。

**stealer()：派生一个共享句柄。**

> [crossbeam-deque/src/deque.rs:270-287](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L270-L287) —— `stealer(&self)` 只是 `inner.clone()`（Arc 引用计数 +1）并复制 flavor，所以 `Stealer` 与 `Worker` 看到的是**同一份内核**。这也是为什么 `Stealer` 要 `Send + Sync`（[deque.rs:582-583](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L582-L583)）而 `Worker` 只 `Send`。

**Injector：无 owner 的共享 FIFO。**

> [crossbeam-deque/src/deque.rs:1313-1341](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1313-L1341) —— `Injector` 用 `head` / `tail` 两个 `CachePadded<Position<T>>` 维护一条无锁链表式队列（结构上类似 u4-l2 的 SegQueue，内部用 `LAP` / `BLOCK_CAP` / `SHIFT` 编码，细节留 u6-l2）。它实现了 `Default`，`new()` 直接转调 `default()`（[deque.rs:1363-1375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1363-L1375)）。`Injector` 不需要 owner，因此 `Send + Sync`（[deque.rs:1343-1344](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1343-L1344)）。

**Worker::push 与 pop。** 这两个方法属于 owner 独占端，本讲只看它们如何受 flavor 影响：

> [crossbeam-deque/src/deque.rs:388-433](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L388-L433) —— `push`：在 `back` 端写入任务、`back.wrapping_add(1)` 推进尾部索引；满了就 `resize` 扩容（动态 Chase-Lev）。注意它对 front 用 `Acquire` 读、对 back 用 `Relaxed` 写，再插一道 `Release` fence——这是 owner 端「写数据 → 发布局」的典型次序，ThreadSanitizer 分支见 [deque.rs:422-429](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L422-L429)。

> [crossbeam-deque/src/deque.rs:435-545](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L435-L545) —— `pop`：**同一份 push 数据，FIFO 与 LIFO 取出顺序不同**。`Flavor::Fifo` 分支（[deque.rs:463-487](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L463-L487)）从 **front** 端 `fetch_add(1)` 取（与 push 的 back 端相反 → 先进先出）；`Flavor::Lifo` 分支（[deque.rs:489-543](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L489-L543)）从 **back** 端 `back.wrapping_sub(1)` 取（与 push 同端 → 后进先出）。这正是 2.2 节 FIFO/LIFO 的代码落脚点。

注意 `pop` 返回 `Option<T>`（空时 `None`），而 steal 返回 `Steal<T>`（三态，见 4.3）——这种**返回类型上的不对称**正是 owner 端与窃取端语义不同的体现。

#### 4.1.4 代码实践

**实践目标**：亲手验证 FIFO 与 LIFO worker 在「push 同一批数据」后 pop 顺序相反，并体会 `stealer()` 共享同一内核。

**操作步骤**（示例代码，可在依赖了 `crossbeam-deque` 的 crate 的 `tests/` 或 `examples/` 下运行）：

```rust
// 示例代码：验证 FIFO vs LIFO 与 stealer 共享内核
use crossbeam_deque::{Steal, Worker};

fn demo(flavor_name: &str, w: Worker<i32>) {
    let s = w.stealer();          // stealer 与 w 看同一份内核
    w.push(1);
    w.push(2);
    w.push(3);
    // stealer 永远从与 push 相反的一端偷，所以无论 FIFO/LIFO 都先偷到 1
    assert_eq!(s.steal(), Steal::Success(1));
    // 但 owner 自己 pop 的顺序取决于 flavor
    println!("{flavor_name}: pop -> {:?}, {:?}", w.pop(), w.pop());
}

fn main() {
    demo("FIFO", Worker::new_fifo()); // 期望 pop -> Some(2), Some(3)
    demo("LIFO", Worker::new_lifo()); // 期望 pop -> Some(3), Some(2)
}
```

**需要观察的现象**：

- 两次 `s.steal()` 不论 flavor 都先拿到 `1`——因为窃取永远发生在 push 的对端。
- FIFO 的 owner pop 顺序是 `2` 然后 `3`；LIFO 是 `3` 然后 `2`。

**预期结果**：与 [deque.rs:166-196](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L166-L196) 中官方文档断言完全一致（`assert_eq!(s.steal(), Steal::Success(1))`、FIFO `pop` 得 2/3、LIFO `pop` 得 3/2）。若输出不符，请确认你用的确实是 `crossbeam-deque` 0.8.x。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Worker` 要被标记成 `!Sync`？如果允许两个线程共享同一个 `&Worker` 并发 `pop` 会出什么问题？

> **答案**：`Worker::push/pop` 是 owner 独占端，没有用锁保护对 `back`/`buffer` 的多写访问（`buffer` 字段甚至是 `Cell`，本就非 `Sync`）。若两个线程并发 `pop`，会同时对 `back`/front 读写，既有数据竞争（UB）也会破坏 Chase-Lev 的不变量。`PhantomData<*mut ()>`（[deque.rs:208](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L208)）在编译期就拦下这种误用。

**练习 2**：`Stealer` 是 `Clone` 的，clone 出多份后再全部 drop，原来的 `Worker` 还能正常用吗？

> **答案**：能。`Stealer` 与 `Worker` 通过 `Arc<CachePadded<Inner<T>>>` 共享内核（[deque.rs:282-287](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L282-L287)）。所有 stealer drop 只是减少 Arc 引用计数，只要 `Worker` 还在，内核就活着。

---

### 4.2 窃取三件套：steal / steal_batch / steal_batch_and_pop

#### 4.2.1 概念说明

窃取（steal）是从「他人队列」拿任务的操作，它和 owner 的 pop 有三点本质不同：

1. **发生在 push 的对端**：owner 从一端 push/pop，窃取者只能从**另一端**拿。这保证 owner 与窃取者几乎不操作同一个槽，减少争用。
2. **多线程并发**：一个 worker 可能被多个 stealer 同时偷，所以 steal 必须用 CAS 保证每个任务**恰好被偷一次**。
3. **可能伪失败（Retry）**：CAS 竞争失败或 owner 正在换 buffer 时，steal 会返回 `Steal::Retry` 表示「这次没偷成，但队列里可能有货，请重试」——这与「队列真空」的 `Empty` 是两回事（详见 4.3）。

crossbeam 提供三种粒度的窃取，对应不同场景：

| 方法 | 返回 | 典型用途 |
| --- | --- | --- |
| `steal()` | `Steal<T>`（一个任务） | 偷同伴单个任务应急 |
| `steal_batch(&dest)` | `Steal<()>`（无任务返回，搬到 dest） | 把一批任务整体搬到本地，摊薄同步成本 |
| `steal_batch_and_pop(&dest)` | `Steal<T>`（搬一批并立刻 pop 一个） | 上者的「顺手拿一个」优化版，最常用 |

`Stealer` 和 `Injector` 都提供这三个方法（签名一致），因为对调用者而言「偷全局 injector」和「偷同伴 worker」在用法上没区别。

#### 4.2.2 核心流程

以 `Stealer::steal` 为例，单次窃取的协议（精简自 [deque.rs:641-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L641-L683)）：

```text
1. load(front, Acquire)            # 读 front 索引
2. 必要时 SeqCst fence（若已 pinned 才手动加）+ epoch::pin()
3. load(back, Acquire)             # 读 back 索引
4. if back - front <= 0: return Empty      # 队列空
5. load(buffer, Acquire, guard)    # 读当前 buffer 指针（可能被 owner 换掉）
6. task = buffer.read(front)       # 读 front 槽的数据（volatile）
7. if buffer 被换过 OR front.CAS(front→front+1) 失败:
       return Retry                # 伪失败：数据可能已被人拿走，或 owner 正换 buffer
8. return Success(task)            # CAS 成功，独占该任务
```

批量窃取 `steal_batch` / `steal_batch_and_pop` 的策略是「偷大约一半，但不超过 `MAX_BATCH`」：

> [crossbeam-deque/src/deque.rs:19-20](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L19-L20) —— `MAX_BATCH = 32`，批量窃取的上限。

「偷一半」是个工程折中：偷太少则每偷一个任务都要付一次同步代价，偷太多则把同伴队列掏空、破坏负载均衡。一半左右是经验上较好的平衡点。注意官方文档明确说**具体偷多少是未指定的实现细节**（[deque.rs:685-690](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L685-L690)、[deque.rs:927-931](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L927-L931)），未来版本可能调整。

#### 4.2.3 源码精读

**Stealer::steal 的失败判定。**

> [crossbeam-deque/src/deque.rs:626-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L626-L683) —— `steal`：第 660-662 行判空返回 `Steal::Empty`；第 668-679 行是 Retry 的两个来源——(a) 第 670 行「buffer 被换过」（owner 扩容了），(b) 第 671-675 行 front 的 `compare_exchange` 失败（被别的 stealer 抢先）。两种情况都返回 `Steal::Retry`。第 665 行用 `epoch::pin()` 拿到的 guard 读 buffer，正是 u5-l3 讲过的「读期间保证 buffer 不被回收」。

**三种批量窃取的对外入口。**

> [crossbeam-deque/src/deque.rs:685-710](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L685-L710) —— `steal_batch(&dest)` 转调 `steal_batch_with_limit(dest, MAX_BATCH)`，把偷到的任务**搬到 dest worker**，返回 `Steal<()>`（注意是不带数据的单元）。

> [crossbeam-deque/src/deque.rs:927-951](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L927-L951) —— `steal_batch_and_pop(&dest)`：在搬一批的基础上**立刻 pop 一个**返回 `Steal<T>`。这是工作窃取调度里最常用的操作——既补充了本地队列，又马上拿到一个可执行任务。

`steal_batch` 内部有个保护：若发现 `self.inner` 与 `dest.inner` 是**同一份内核**（自己偷自己），就不真偷，只按 `dest.is_empty()` 返回 `Empty` 或 `Success(())`（[deque.rs:746-754](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L746-L754)）——避免无意义工作。

`Injector` 同样提供这三个方法（[deque.rs:1464](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1464) `Injector::steal`、[deque.rs:1564](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1564) `Injector::steal_batch`、[deque.rs:1766](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1766) `Injector::steal_batch_and_pop`），签名与 `Stealer` 一致，所以「从 injector 取」和「从 stealer 取」在调用代码里可以无缝互换。

#### 4.2.4 代码实践

**实践目标**：观察 `steal_batch_and_pop` 「搬一批 + 立刻 pop 一个」的行为，理解它为何比单次 `steal` 更高效。

**操作步骤**（示例代码）：

```rust
// 示例代码：steal_batch_and_pop 把半队列搬到本地并立刻取出一个
use crossbeam_deque::Worker;

fn main() {
    let w1 = Worker::new_fifo();
    for i in 1..=4 {
        w1.push(i); // w1: [1,2,3,4]
    }
    let s = w1.stealer();
    let w2 = Worker::new_fifo();

    // 一次调用：从 w1 偷一批搬到 w2，并立刻 pop 一个返回
    let first = s.steal_batch_and_pop(&w2);
    println!("偷到的第一个: {first:?}");
    println!("w2 剩余数量: {}", w2.len());
    while let Some(t) = w2.pop() {
        println!("w2 还能 pop: {t}");
    }
}
```

**需要观察的现象**：

- `steal_batch_and_pop` 返回 `Steal::Success(1)`（官方断言见 [deque.rs:946](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L946)）。
- `w2.len()` 显示从 w1 搬来了若干个任务（具体数量是实现细节，4 个任务通常搬来约一半）。
- 之后 `w2.pop()` 还能取出剩余被搬来的任务。

**预期结果**：`first` 为 `Success(1)`，`w2` 非空。**若你在并发场景下偶尔看到 `Steal::Retry`**，那是正常的伪失败（4.3 详述），重试即可——不要把它当作错误。批量搬运的具体个数标记为「实现细节，待本地验证实际值」。

#### 4.2.5 小练习与答案

**练习 1**：`steal_batch` 返回 `Steal<()>` 而不是 `Steal<T>`，为什么？被偷走的任务去哪了？

> **答案**：被偷走的任务**直接 push 进 `dest` worker**（方法参数 `&Worker<T>`），所以调用方不需要拿到这些值，返回 `Steal<()>` 只表达「成功/空/重试」三态即可。真正需要执行任务时，由 `dest.pop()` 取出。

**练习 2**：什么场景下应该用 `steal`，什么场景下用 `steal_batch_and_pop`？

> **答案**：只想临时拿一个任务应急（如偷同伴）用 `steal`，开销小但每次只一个；想从「大概率有较多任务」的源（如全局 injector）一次性补充本地队列时用 `steal_batch_and_pop`——它把同步代价摊到多个任务上，并顺手返回一个立即可执行的任务，是调度循环里最常用的取任务方式（见 4.3.2 的 `find_task`）。

---

### 4.3 Steal 枚举与重试模式

#### 4.3.1 概念说明

`Steal<T>` 是所有窃取操作的统一返回类型，三个变体分别对应三种**必须区分**的情况：

```rust
pub enum Steal<T> {
    Empty,       // 队列此刻真空
    Success(T),  // 偷到了一个任务
    Retry,       // 伪失败：没偷到，但队列里可能有货，请重试
}
```

> 源码见 [crossbeam-deque/src/deque.rs:2083-2094](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2083-L2094)，`#[must_use]` 强制你必须处理返回值。

**为什么必须区分 `Empty` 和 `Retry`？** 这是本讲最关键的一点。

- `Empty` 表示「这个队列确实没东西」，调用方可以**安心去别处找**（比如转而偷下一个 stealer），不需要回头重试本队列。
- `Retry` 表示「这次 CAS 没抢赢别人 / owner 正在换 buffer」，**队列里很可能还有任务**，只是这次没轮到你。如果把它误当 `Empty`，就会漏偷任务、造成饥饿；正确做法是**重试**。

如果把 `Retry` 错当成「失败就放弃」，工作窃取调度器就会丢任务。所以 `Steal` 类型把这两种语义严格分开，强迫调用方写对重试逻辑。

> 标注 `#[must_use]`（[deque.rs:2083](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2083)）是为了防止你忽略返回值——忽略 `Retry` 等于丢任务。

#### 4.3.2 核心流程：重试循环的两个利器

crossbeam 为 `Steal` 提供了两个组合方法，让「多个窃取源 + 重试」的循环写起来很优雅：

**利器一：`or_else`。** 「如果本次没成功，就试另一个源；只要其中任一是 `Retry`，整体就保留 `Retry`」。

```text
self.or_else(f):
  self == Success => 直接返回 self（短路）
  self == Empty    => 返回 f()            （Empty 不传染，交给下一个源决定）
  self == Retry    => 若 f() == Success 则 Success，否则保留 Retry（Retry 会传染）
```

**利器二：`FromIterator`（对 `Vec<Steal<T>>` 调用 `.into_iter().collect::<Steal<T>>()`）。** 把多个窃取结果汇成一个：

```text
遍历所有 Steal：
  遇到 Success => 立即返回它
  遇到 Retry   => 记下「有过 Retry」
  遇到 Empty   => 忽略
全部看完若无 Success：有过 Retry 则返回 Retry，否则返回 Empty
```

把两者与 `iter::repeat_with(...).find(|s| !s.is_retry())` 组合，就得到 crossbeam 文档里那段**经典的工作窃取「找任务」函数**：

> [crossbeam-deque/src/lib.rs:52-76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L52-L76) —— `find_task`：先 `local.pop()`；失败则进入循环——每次尝试 `global.steal_batch_and_pop(local).or_else(|| stealers.iter().map(|s| s.steal()).collect())`，用 `.find(|s| !s.is_retry())` 一直循环直到拿到一个「非 Retry」的结果（要么 `Success` 要么 `Empty`），最后 `.and_then(|s| s.success())` 取出任务。

这段代码精妙在哪？

1. `global.steal_batch_and_pop(local)` 偷全局一批并拿一个，若 `Empty`/`Retry` 就 `.or_else(...)` 转去偷同伴。
2. `stealers.iter().map(|s| s.steal()).collect()` 把「偷每个同伴的结果」用 `FromIterator` 汇总——任一 `Success` 即胜，否则只要有一个 `Retry` 就整体 `Retry`。
3. 外层 `repeat_with(...).find(|s| !s.is_retry())`：只要结果是 `Retry` 就**重来一次**；只有拿到 `Success`（偷到）或 `Empty`（全空）才停下。
4. 最终 `.success()` 把 `Success(t)` 转成 `Some(t)`，其余转 `None`。

这正是 4.1.2 调度循环里「本地 → injector → stealers」三段查找的**惯用写法**。

#### 4.3.3 源码精读

**Steal 枚举本体与谓词方法。**

> [crossbeam-deque/src/deque.rs:2083-2094](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2083-L2094) —— 三变体定义。注意它 `Copy + Clone + PartialEq + Eq`，所以可以 `assert_eq!(s.steal(), Steal::Success(1))` 直接比较（见各方法的文档测试）。

> [crossbeam-deque/src/deque.rs:2097-2143](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2097-L2143) —— `is_empty` / `is_success` / `is_retry` 三个谓词，分别用 `matches!` 判定变体。`find_task` 里用的 `!s.is_retry()` 就来自这里。

> [crossbeam-deque/src/deque.rs:2145-2162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2145-L2162) —— `success()`：把 `Success(t)` 转成 `Some(t)`，其余变体转 `None`，是「从 Steal 提取任务」的标准出口。

**or_else 的 Retry 传染逻辑。**

> [crossbeam-deque/src/deque.rs:2164-2200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2164-L2200) —— `or_else`：`Empty` 时返回 `f()`；`Success` 时短路返回自身；`Retry` 时——若 `f()` 是 `Success` 则返回它，否则**保留 `Retry`**（第 2192-2198 行）。这条「Retry 传染」规则保证：只要任一源需要重试，整体就不会被误判为「真空」。

**FromIterator 的聚合规则。**

> [crossbeam-deque/src/deque.rs:2213-2233](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2213-L2233) —— `FromIterator for Steal<T>`：遍历遇 `Success` 立即返回；用 `retry: bool` 记录是否见过 `Retry`；循环结束后，见过 Retry 返回 `Retry`，否则 `Empty`。`find_task` 里 `stealers.iter().map(|s| s.steal()).collect()` 用的就是这个。

**官方文档对三态语义的总括。**

> [crossbeam-deque/src/lib.rs:38-39](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L38-L39) —— 原文：「与 push/pop 不同，steal 可能以 `Steal::Retry` 伪失败，此时需要重试该 steal 操作」。这是整个 `Steal` 设计的权威依据。

#### 4.3.4 代码实践

**实践目标**：在并发下亲眼看到 `Steal::Retry` 出现，并验证用 `find_task` 模式不会丢任务。

**操作步骤**（示例代码，需要 `crossbeam-deque` 与 `crossbeam-utils`）：

```rust
// 示例代码：观察 Retry 并验证重试模式不丢任务
use crossbeam_deque::{Injector, Stealer, Worker};
use crossbeam_utils::thread::scope;
use std::collections::HashSet;
use std::sync::Mutex;

// 直接照搬 lib.rs 文档里的 find_task
fn find_task<T>(
    local: &Worker<T>,
    global: &Injector<T>,
    stealers: &[Stealer<T>],
) -> Option<T> {
    local.pop().or_else(|| {
        std::iter::repeat_with(|| {
            global
                .steal_batch_and_pop(local)
                .or_else(|| stealers.iter().map(|s| s.steal()).collect())
        })
        .find(|s| !s.is_retry())
        .and_then(|s| s.success())
    })
}

fn main() {
    let global = Injector::new();
    let done: Mutex<HashSet<i32>> = Mutex::new(HashSet::new());
    const N: usize = 4;
    const TASKS: i32 = 1000;

    // 主线程预注入一些任务
    for i in 0..TASKS {
        global.push(i);
    }

    scope(|s| {
        // 为每个工作线程建 worker + 收集所有人的 stealer
        let workers: Vec<_> = (0..N).map(|_| Worker::new_fifo()).collect();
        let stealers: Vec<_> = workers.iter().map(|w| w.stealer()).collect();

        for (idx, w) in workers.into_iter().enumerate() {
            let global = &global;
            let stealers = &stealers;
            let done = &done;
            s.spawn(move |_| {
                // 把自己的 worker 「外移」出来用
                let local = w;
                loop {
                    if let Some(task) = find_task(&local, global, stealers) {
                        done.lock().unwrap().insert(task);
                        // 模拟任务执行后可能产生新任务，回吐到全局
                        // global.push(task + TASKS); // 可选
                    } else {
                        // 本地、全局、同伴都空：这里简化为跳出。
                        // 真实调度器应在此阻塞等待新任务注入。
                        if done.lock().unwrap().len() == TASKS as usize {
                            break;
                        }
                    }
                }
                let _ = idx;
            });
        }
    })
    .unwrap();

    let got = done.lock().unwrap();
    println!("完成任务数: {} / {}", got.len(), TASKS);
    assert_eq!(got.len(), TASKS as usize, "丢任务了！");
}
```

> 说明：上面 `scope` 的用法承接 u1-l4 / u2-l7。`scope` 块内借 `&` 引用栈上数据，省去 `Arc`。

**需要观察的现象**：

- 程序运行中，`find_task` 内部会因并发竞争**多次**走到 `Steal::Retry` 分支，但 `repeat_with(...).find(|s| !s.is_retry())` 会自动重试，对调用者透明。
- 最终断言 `got.len() == 1000` 通过，证明**没有一个任务因 Retry 被丢弃**。

**预期结果**：打印 `完成任务数: 1000 / 1000`，断言通过。如果出现卡死（程序不退出），多半是因为「全空」时缺少真正的阻塞/唤醒机制——本示例用 `len() == TASKS` 作为退出条件，仅作演示；真实调度器应配合 channel/Parker 在全空时 park、在 `Injector.push` 后唤醒。并发下的精确重试次数属运行期行为，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：把 `find_task` 里 `.find(|s| !s.is_retry())` 改成 `.find(|s| s.is_success())`，会有什么后果？

> **答案**：循环会一直重试直到拿到 `Success` 为止，看起来「更严」，但会**忙等（busy spin）**——当所有队列真空（应返回 `Empty`）时，它不会停下，而是无限循环空转烧 CPU。原写法用 `!is_retry()` 表示「拿到 `Success` 或 `Empty` 都可停」，正是为了让「全空」时能返回 `None` 让调用者决定是否阻塞。

**练习 2**：`Steal::or_else` 里 `Retry` 为什么会「传染」（Retry + Empty = Retry），而不是退化为 Empty？

> **答案**：`Retry` 的语义是「这次没偷成，但**可能有货**」。即便后续 `f()` 返回 `Empty`（某个源此刻空），只要前面出现过 `Retry`，就**不能**断言「整体无货可偷」——那个 Retry 源里很可能还有任务。保留 `Retry` 提醒调用方重试，才能避免漏偷；若退化为 `Empty`，调用方会以为「全都空了」而去阻塞或放弃，造成任务被遗留在队列里。

**练习 3**：为什么 `Steal` 标了 `#[must_use]`，而 `Option`/`Result` 也标了——这里多出来的风险具体是什么？

> **答案**：忽略 `Option` 顶多是「没处理一个值」，但忽略 `Steal` 的 `Retry` 等于**在并发下丢弃一个本应重试的窃取**，直接导致任务被错误地「视作无货」而从调度器眼皮底下溜走。`#[must_use]`（[deque.rs:2083](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2083)）在编译期就警告这种危险的疏忽。

---

## 5. 综合实践：搭建一个迷你工作窃取调度器

**任务**：把 4.1～4.3 的知识串起来，实现一个最小可用的工作窃取线程池，并验证任务不丢失。

**要求**：

1. 用 `crossbeam_utils::thread::scope`（u1-l4）启动 N 个工作线程。
2. 创建 1 个 `Injector<i32>` 作为全局入口，每个工作线程一个 `Worker::new_lifo()`（选 LIFO 以获得缓存局部性），并互发 `stealer()`。
3. 主线程把 0..1000 共 1000 个任务 `push` 进 `Injector`。
4. 每个工作线程循环执行 4.3.4 的 `find_task`：本地 → injector（批量偷并 pop）→ 同伴。
5. 用一个 `Mutex<HashSet<i32>>` 收集已完成任务编号，作用域结束后断言集合大小为 1000 且无重复。

**进阶改造（可选）**：

- 让工作线程在执行任务时，以一定概率 `global.push(new_task)` 产生新任务（模拟递归分裂），验证动态任务也能被正确调度。
- 把 `find_task` 全空时的「忙等退出」改造成「`std::thread::yield_now()` 让出时间片」或配合 u2-l5 的 `Parker` 真正阻塞，并在主线程 push 后用 unpark 唤醒一个空闲线程。

**验收标准**：

- 断言 `HashSet.len() == 1000` 通过、无重复——证明 `Steal::Retry` 被正确重试、任务零丢失。
- 能用自己的话解释：为什么 worker 用 LIFO、injector 用 FIFO、stealer 永远从对端偷（见 2.2 与 4.2.1）。

> 提示：完整骨架已在 4.3.4 给出，本实践的重点是**理解并改造**它，而非从零重写。若运行出现死循环或 panic，先检查「全空退出条件」与「数据竞争下 HashSet 的加锁」。

---

## 6. 本讲小结

- crossbeam-deque 把工作窃取模型固化为三类公开类型：`Worker`（线程私有，`push/pop`，`!Sync`）、`Stealer`（可共享克隆，`steal` 家族，`Send+Sync`）、`Injector`（无 owner 的共享 FIFO 入口）。
- `Worker` 的 `new_fifo`/`new_lifo` 唯一区别是 `Flavor` 字段（[deque.rs:214-268](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L214-L268)）；`stealer()` 只是 `Arc::clone` 共享同一内核（[deque.rs:282-287](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L282-L287)）。
- pop 的方向取决于 flavor：FIFO 从 front 取、LIFO 从 back 取（[deque.rs:463-543](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L463-L543)）；而 steal 永远发生在 push 的对端。
- 窃取有三种粒度：`steal`（单个）、`steal_batch`（搬一批，`Steal<()>`）、`steal_batch_and_pop`（搬一批并立刻 pop 一个，最常用）；批量策略是「约一半，上限 `MAX_BATCH=32`」。
- `Steal{Empty,Success,Retry}` 三态中，`Empty`（真空）与 `Retry`（伪失败需重试）**必须区分**，混淆会丢任务；`#[must_use]` 强制处理。
- `or_else` 与 `FromIterator` 让「Retry 传染」语义可组合，配合 `repeat_with(...).find(|s| !s.is_retry())` 写出经典的 `find_task` 重试循环（[lib.rs:52-76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L52-L76)）。

---

## 7. 下一步学习建议

本讲只读了 crossbeam-deque 的**公共 API 表面**，刻意回避了三类问题：

1. `Worker` 的 Chase-Lev 双端队列如何用 `front`/`back` 两个原子索引 + 动态环形 buffer 实现 lock-free push/pop？
2. 扩容/缩容时旧 buffer 怎么用 `epoch::pin()` + `defer_unchecked` 安全回收（联系 u5-l3 / u5-l5）？
3. `Injector` 内部的 `LAP` / `BLOCK_CAP` / `SHIFT` 位编码（与 u4-l2 SegQueue 同源）如何防 ABA？

这些都在 **u6-l2「Chase-Lev 双端队列实现」** 中展开。建议你在进入 u6-l2 前：

- 重读 u4-l1（ArrayQueue 的 lap/index 编码）与 u4-l2（SegQueue 的 Block 链表），因为 `Injector` 的实现与它们高度同构。
- 重读 u5-l3（Guard/defer）与 u5-l5（epoch 推进/回收），因为 deque 的 buffer 回收完全依赖 epoch。
- 自己把 4.3.4 的迷你调度器跑通，带着「steal 时为什么要 `epoch::pin()`」这个问题去读 u6-l2 的 `Stealer::steal` 全文。
