# 实战：用 find_task 模式构建 work-stealing 调度器

## 1. 本讲目标

本讲是全手册的收尾实战篇。前面十几讲我们分别拆解了 `Worker`/`Stealer` 的 Chase-Lev 实现、`Injector` 的块链表实现、内存序、epoch 回收与测试体系。这一讲把这些零件**拼成一台能跑的机器**：一个最小的多线程 work-stealing 任务调度器。

学完后你应当能够：

- 画出 work-stealing 调度器的**标准拓扑**：每个线程一个本地 `Worker` + 一个全局 `Injector` + 所有线程的 `Stealer` 列表。
- 读懂并改写 `src/lib.rs` 顶部给出的 `find_task` 函数，理解它如何用 `Steal::or_else`、`FromIterator`、`is_retry`、`success` 把「本地 pop → 全局批量偷 → 遍历 stealer 偷」串成一条带 Retry 自动重试的回退链。
- 理解在受限线程作用域（scoped threads）中，`Worker`（`Send + !Sync`，move 进线程）与 `Stealer`/`Injector`（`Send + Sync`，共享引用）各自的所有权归属。
- 把 `Steal::Retry` 的处理、任务闭包的分发、线程退出协调这三件事落到真实调度循环里，并亲手跑通一个注入 100 个任务、4 个工作线程偷取执行的小调度器。

## 2. 前置知识

本讲是「拼装」而非「拆解」，不会再深入任何一段算法实现。读本讲前，请确认你已经建立以下认知（对应前置讲义）：

- **三种队列角色与三态结果**（u1-l1、u1-l4）：`Worker` 是单线程私有的本地队列，只能 `push`/`pop`；`Stealer` 是从 `Worker` 派生、可跨线程共享的只读偷取视图；`Injector` 是全局唯一、MPMC 的 FIFO 入口。任何偷取操作返回 `Steal<T>`（`Empty`/`Success(T)`/`Retry`），而不是 `Option<T>`——区分 `Empty`（真空）与 `Retry`（伪失败，需重试）是回退链正确性的关键。
- **Worker 的 Send/Sync 标注**（u1-l3）：`Worker` 靠 `PhantomData<*mut ()>` 加 `unsafe impl Send` 被限定为 `Send + !Sync`——可 move 进线程，但不能 `&` 共享；`Stealer`/`Injector` 则是 `Send + Sync`。
- **批量偷取语义**（u2-l4、u3-l4）：`steal_batch_and_pop(dest)` 会从源队列偷「约一半」任务，把其中 1 个直接弹出返回、其余倒进 `dest` 这个目的 `Worker`，返回类型是 `Steal<T>`。
- **scoped threads**：标准库的 `std::thread::scope`（1.63 起稳定，本 crate MSRV 1.74，可直接用）或 `crossbeam_utils::thread::scope`（项目测试里用的版本）允许子线程借用栈上数据，线程结束时自动 join，是写调度器循环的标配。

如果你对上面任何一点感到陌生，建议先回看对应讲义。

## 3. 本讲源码地图

本讲引用的真实源码文件只有两个，外加两个测试文件作为「惯用法样本」：

| 文件 | 作用 |
| --- | --- |
| [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs) | crate 门面。顶部 crate 级文档用文字描述了 work-stealing 拓扑，并用一段 doctest 给出 `find_task` 的参考实现——这是本讲的「图纸」。 |
| [src/deque.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs) | 全部实现。本讲只用到它的公开 API：`Steal` 枚举及其组合子（`or_else`/`FromIterator`/`is_retry`/`success`）、`Worker::pop`、`Stealer::steal`、`Injector` 的 `push`/`steal_batch_and_pop`。 |
| [tests/fifo.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/fifo.rs) | 集成测试。其 `stampede` 测试演示了「预填充队列 + 多 stealer 线程 + 主线程 pop + `AtomicUsize` 计数」的写法，是综合实践的直接参照。 |
| [tests/steal.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs) | 集成测试。其中 `busy_retry` 辅助函数把可能返回 `Retry` 的偷取包装成确定性结果，是理解 Retry 处理的最小样本。 |

> 说明：本讲的「示例代码」（完整调度器）不是项目原有代码，会明确标注；其余引用都是仓库里真实存在的源码。

## 4. 核心概念与源码讲解

### 4.1 work-stealing 调度器的标准拓扑

#### 4.1.1 概念说明

work-stealing（工作窃取）调度的核心思想是：**每个工作线程优先消费自己本地队列里的任务，只有当本地空了，才去「偷」别人的或全局的任务**。这样做的好处是——在任务分布均匀时，线程间几乎不需要争用，每个线程大多只碰自己的队列（无锁且无跨核通信），只有空闲线程才会主动出去找活干，把争用降到最低。

要落地这套思想，需要三种角色组网：

- **N 个本地 `Worker`**：每个线程独占一个，线程往里 `push` 自己新产生的子任务，并优先 `pop` 本地任务。
- **1 个全局 `Injector`**：外部（比如主线程、IO 线程）把「从天而降」的新任务 `push` 进这里，作为所有线程共享的入口。
- **N 个 `Stealer` 组成的列表**：每个线程持有**其它所有线程**本地队列的 `Stealer`，以便在本地和全局都空时去偷同伴的任务。

#### 4.1.2 核心流程

调度主循环的找任务顺序（也就是 `find_task` 的回退链）：

```
1. local.pop()              # 先弹本地队列（最快，无争用）
2. global.steal_batch_and_pop(local)   # 本地空 → 从全局批量偷，顺带弹一个
3. for s in stealers: s.steal()        # 全局也空 → 遍历偷同伴的单个任务
4. 全部返回 Empty → 真的没活了，线程进入休眠/让步
```

注意第 2 步用「批量偷并弹出一个」而不是「单偷」：从全局偷一批回来塞进本地，可以**摊薄**跨队列偷取的开销——偷一次够本线程消费好一阵子。

#### 4.1.3 源码精读

这个拓扑不是我们杜撰的，而是 crate 顶部文档第一段就写明的设计意图：

> [src/lib.rs:1-15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L1-L15) —— 用自然语言描述「若干线程各持一个 worker、一个全局 injector、一份 stealers 列表」，以及「新任务 push 进 injector、工作线程先看本地再看 injector 和 stealers」的调度循环。

文档还明确了三种偷取变体（[src/lib.rs:29-39](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L29-L39)）：`steal()` 偷一个、`steal_batch()` 偷一批、`steal_batch_and_pop()` 偷一批并弹一个；并强调偷取可能**伪失败**返回 `Steal::Retry`，需要重试——这正是第 4.2 节要处理的难点。

构造这三个角色的 API 入口分别是：

- [src/deque.rs:225](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L225) `Worker::new_fifo()` —— 创建本地队列（也可用 `new_lifo()`，见 [L253](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L253)）。
- [src/deque.rs:282](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L282) `Worker::stealer()` —— 从本地队列派生一个可跨线程共享的 `Stealer`（内部 `Arc::clone` 同一份 `Inner`）。
- [src/deque.rs:1373](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1373) `Injector::new()` —— 创建全局注入队列。

#### 4.1.4 代码实践

**实践目标**：在纸上把拓扑画出来，确认你理解三者关系。

**操作步骤**：

1. 假设有 4 个工作线程，画出 4 个 `Worker` 框、1 个 `Injector` 框、4 个 `Stealer` 框。
2. 用箭头标注：主线程 `push` → `Injector`；每个工作线程 `push`/`pop` → 自己的 `Worker`；每个工作线程的偷取箭头 → 另外 3 个 `Worker` 对应的 `Stealer` + `Injector`。
3. 回答：为什么 `Stealer` 列表里通常**不包含**当前线程自己的 `Stealer`？（提示：本地任务直接 `pop` 即可，没必要绕一圈偷自己。）

**预期结果**：得到一张「每线程一进（本地）、全局一入口、N×(N−1) 条偷取边」的图。第 3 问的答案是：偷自己的 `Stealer` 等价于 `pop`，但多一次 CAS 与 epoch pin，纯属浪费。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `steal_batch_and_pop` 换成 `steal`（只偷一个），调度器会变得「更慢」还是「更快」？为什么？

**答案**：更慢。`steal_batch_and_pop` 一次偷约半批并塞进本地，摊薄了跨队列偷取的固定开销（epoch pin、CAS、内存序 fence）；改成 `steal` 后，线程每次消费完一个任务都要重新去全局偷一次，争用和同步开销显著上升。

**练习 2**：拓扑里 `Injector` 为什么是 FIFO 而不是 LIFO？

**答案**：`Injector` 是所有线程共享的全局入口，外部注入的任务通常希望「先注入先被取走」以保持公平、避免饿死早期任务；FIFO 正好满足。而本地 `Worker` 才常选 LIFO（cache-friendly，子任务刚 push 就 pop，数据还在缓存里）。

---

### 4.2 find_task 回退链：Steal 组合子的串接

#### 4.2.1 概念说明

`find_task` 是本讲的核心函数，它的职责是「**给一个空闲线程找一个任务，找不到返回 `None`**」。难点在于：偷取操作可能返回 `Retry`（伪失败），而我们又要把「本地 pop」「全局批量偷」「遍历 stealer 偷」这三步串起来。直接用 `?` 或 `match` 会写成一坨嵌套，而且容易把 `Retry` 错当成 `Empty` 提前放弃。

`crossbeam-deque` 的解法是为 `Steal<T>` 提供三个组合子（combinator），让你用类似 `Option::or_else` 的链式风格表达「**尝试 A，A 没成功就尝试 B，并正确传递 Retry 信号**」：

- `or_else(f)`：当前是 `Success` 就直接返回；否则执行 `f`，**只要任意一边出现过 `Retry` 就保留 `Retry`**，只有两边都是 `Empty` 才返回 `Empty`。
- `FromIterator`（即 `collect::<Steal<_>>()`）：把一组 `Steal` 聚合成一个——遇到 `Success` 立即返回它；否则只要有任意一个是 `Retry` 就返回 `Retry`，全 `Empty` 才返回 `Empty`。
- `is_retry()` / `success()`：前者判断是否需要重试，后者从 `Success(T)` 取出 `T`（其余返回 `None`）。

#### 4.2.2 核心流程

`find_task` 的回退链用一句话概括：

```
local.pop()  →  或为空时  →  repeat_with{ global.steal_batch_and_pop.or_else(stealers 全偷) }
                             .find(!is_retry)   # 一直试，直到结果不再是 Retry
                             .and_then(success) # 从中取出任务
```

关键在 `repeat_with(...).find(|s| !s.is_retry())` 这一段：它构成一个**重试循环**——只要每轮组合结果都是 `Retry`，就不断重新尝试整条链；一旦某轮得到 `Success` 或 `Empty`（二者都不是 `Retry`），循环就停下。最后 `.and_then(|s| s.success())` 把 `Success(T)` 解包成 `Some(T)`、把 `Empty` 映射成 `None`。

为什么 `Empty` 也能终止循环？因为「全部队列都空」是一种稳定终态，应当返回 `None` 让上层线程休眠，而不是无限重试。`Retry` 才表示「这次没抢到但系统里可能有任务，立刻再试」。

#### 4.2.3 源码精读

下面是 `src/lib.rs` 顶部 doctest 给出的参考实现，逐行标注：

> [src/lib.rs:52-76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L52-L76) —— `find_task` 的完整定义。先 `local.pop()`；为空则进入 `repeat_with` 闭包：`global.steal_batch_and_pop(local).or_else(|| stealers.iter().map(|s| s.steal()).collect())`，再用 `.find(|s| !s.is_retry())` 重试到非 Retry，最后 `.and_then(|s| s.success())` 取任务。

它依赖的四个组合子，全部定义在 `Steal<T>` 上：

- `Steal` 三态枚举本身：[src/deque.rs:2085-2094](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2085-L2094) `Empty`/`Success(T)`/`Retry`。
- [src/deque.rs:2185-2200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2185-L2200) `or_else` —— 注意 `Retry` 分支：即便 `f()` 返回 `Empty`，最终也保留为 `Retry`（见 L2192-L2198），这正是「Retry 不会被 Empty 吞掉」的语义。
- [src/deque.rs:2213-2233](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2213-L2233) `FromIterator` —— 用 `retry` 标志位收集是否出现过 `Retry`，决定最终返回 `Retry` 还是 `Empty`。
- [src/deque.rs:2141-2143](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2141-L2143) `is_retry` 与 [src/deque.rs:2157-2162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2157-L2162) `success`。

#### 4.2.4 代码实践

**实践目标**：用源码阅读型实践，确认你理解 `or_else` 与 `FromIterator` 的 Retry 传播。

**操作步骤**：

1. 打开 [src/deque.rs:2172-2200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2172-L2200)，阅读 `or_else` 的文档示例与实现。
2. 对照下面的真值表，逐行用源码推导结论是否成立：

| `self`（全局偷的结果） | `f()`（stealers 偷的聚合结果） | `or_else` 最终返回 |
| --- | --- | --- |
| `Success(1)` | 任意 | `Success(1)` |
| `Empty` | `Success(2)` | `Success(2)` |
| `Empty` | `Empty` | `Empty` |
| `Empty` | `Retry` | `Retry` |
| `Retry` | `Empty` | `Retry` |
| `Retry` | `Retry` | `Retry` |

**预期结果**：上表 6 行全部成立。关键观察是最后两行——即便 stealers 全空（`Empty`），只要全局偷曾返回 `Retry`，最终仍是 `Retry`，于是 `find_task` 外层 `repeat_with` 会继续重试，而不会误判「没活」提前退出。

> 待本地验证：你也可以在 `cargo test` 的 doctest 之外，自己写个 `#[test]`，构造几种 `Steal` 组合断言上表。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `find_task` 里的 `.find(|s| !s.is_retry())` 误写成 `.find(|s| s.is_success())`，会出现什么 bug？

**答案**：当所有队列都空、整条链返回 `Empty` 时，`is_success()` 恒为假，`repeat_with` 会**无限循环**空转（因为永远找不到 `Success`）。正确的终止条件是「非 Retry」——`Empty` 和 `Success` 都应让循环停下。

**练习 2**：`stealers.iter().map(|s| s.steal()).collect()` 这里 `collect` 成的目标类型是 `Steal<T>`，靠的是哪个 trait？

**答案**：`FromIterator<Steal<T>> for Steal<T>`（[src/deque.rs:2213](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2213)）。它把遍历所有 stealer 得到的一组 `Steal` 聚合成一个：取到第一个 `Success` 即返回，否则按「有无 Retry」归约为 `Retry` 或 `Empty`。

**练习 3**：`or_else` 的签名里 `f: F where F: FnOnce() -> Self`，为什么是 `FnOnce` 而不是 `FnMut`？

**答案**：`or_else` 的语义是「前者没拿到任务时，**至多再尝试一次**后备方案」，闭包只会在 `self` 非 `Success` 时被调用一次，故 `FnOnce` 足够；这也让它能消费捕获的变量（比如 move 进来的 `Stealer` 引用）。

---

### 4.3 scoped threads 与所有权拓扑

#### 4.3.1 概念说明

要把上面的拓扑跑起来，必须把 `Worker`、`Stealer`、`Injector` 正确地分配给各个线程。这里有一个 Rust 所有权层面的硬约束：**`Worker` 是 `!Sync` 的**，不能 `&` 共享给多个线程；而 `Stealer`、`Injector` 是 `Sync` 的，可以共享引用。因此正确的分配方式是：

- 每个 `Worker` **move** 进它所属的线程（独占所有权）。
- `Injector` 和 `Stealer` 列表以 **`&` 借用**的方式被所有线程共享。

但「借用栈上数据给子线程」在传统 `std::thread::spawn` 里是不允许的（子线程生命周期可能超过父栈帧）。解决办法是 **scoped threads**：`std::thread::scope`（或项目测试用的 `crossbeam_utils::thread::scope`）保证所有子线程在闭包返回前 join 完毕，因此允许子线程借用外部数据。这正好契合调度器的需求——主线程创建好队列，工作线程借用它们跑循环，主线程等所有工作线程结束后再收尾。

#### 4.3.2 核心流程

调度器的初始化与线程分发顺序：

```
1. 创建 N 个 Worker（Vec<Worker<T>>）
2. 对每个 Worker 调 stealer()，收集成 Vec<Stealer<T>>（此时还未 move Worker）
3. 创建 1 个 Injector，push 全部初始任务
4. thread::scope(|s| {
       for w in workers {            # 逐个 move Worker 进线程
           s.spawn(move || {
               loop { find_task(&w, &injector, &stealers) ... }
           });
       }
   });                               # scope 返回时所有线程已 join
5. 断言全部任务完成
```

注意第 2 步必须在第 4 步 move `workers` **之前**完成——一旦 `for w in workers` 消费了 `Vec`，就不能再对它调 `stealer()` 了。所以典型的写法是先 `let stealers: Vec<_> = workers.iter().map(|w| w.stealer()).collect();`，再 move `workers`。

#### 4.3.3 源码精读

项目自己的集成测试就是这套模式的范本。以 `fifo.rs` 的 `stampede` 测试为例：

> [tests/fifo.rs:103-141](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/fifo.rs#L103-L141) —— 先 `Worker::new_fifo()` 预填充 `COUNT` 个任务，再用 `Arc<AtomicUsize>` 做剩余计数；在 `scope(|scope| { ... })` 里，给每个线程 `let s = w.stealer();` 后 `scope.spawn(move |_| { while remaining > 0 { if let Success(x) = s.steal() {...} } })`，主线程则同步 `w.pop()`，最后 `.unwrap()`。

导入语句见 [tests/fifo.rs:10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/fifo.rs#L10) `use crossbeam_utils::thread::scope;`——这是 crossbeam-utils 提供的 scoped threads。标准库的 `std::thread::scope`（1.63+ 稳定）API 几乎一致，本 crate MSRV 1.74，可直接用 `std` 版本，省去额外依赖。

> 提示：`stampede` 测试里 `Worker` 没有 move 进线程，而是留在主线程 pop、把 `Stealer` 派发给子线程——因为它只测「多 stealer 偷同一个队列」。完整的 work-stealing 调度器要更进一步：**每个线程都有自己的 `Worker`**，详见第 5 节综合实践。

#### 4.3.4 代码实践

**实践目标**：理解所有权约束，动手验证 `Worker` 能 move 但不能 `&` 共享。

**操作步骤**：

1. 写一个最小 `cargo` 二进制，依赖 `crossbeam-deque = "0.8"`。
2. 尝试编译下面两段代码，观察编译器报错差异：

```rust
// 代码 A（应能编译）：把 Worker move 进 scoped thread
use crossbeam_deque::Worker;
let w = Worker::new_fifo();
std::thread::scope(|s| {
    s.spawn(move || { let _ = w.pop(); });
});
```

```rust
// 代码 B（应编译失败）：试图 & 共享 Worker
use crossbeam_deque::Worker;
let w = Worker::new_fifo();
std::thread::scope(|s| {
    s.spawn(|| { let _ = w.pop(); });   // w 是 &，跨线程共享 &Worker
});
```

**预期结果**：代码 A 编译通过；代码 B 报类似 `Worker cannot be shared between threads safely`（`!Sync`）的错误。这印证了「`Worker` 只能 move、不能 `&` 共享」，因此调度器里每个线程必须独占一个 `Worker`。

#### 4.3.5 小练习与答案

**练习 1**：`Stealer` 和 `Injector` 为什么能以 `&` 跨线程共享？

**答案**：它们都被标注为 `Send + Sync`（`Stealer` 内部是 `Arc<Inner>`，`Injector` 借 `PhantomData<T>` 显式 `unsafe impl Send/Sync`，见 u3-l1）。`Sync` 意味着 `&Stealer` 可以安全地跨线程并发访问，所以放进 `&[Stealer]` 或作为 `&Injector` 共享给所有工作线程是合法的。

**练习 2**：为什么必须在 move `workers` 之前先建好 `stealers`？

**答案**：建 `stealers` 需要遍历 `&Worker` 调 `stealer()`；而 `for w in workers` 会消费（move）整个 `Vec<Worker>`，之后 `workers` 已不可用。若顺序反了，编译器会报 `use of moved value`。所以惯用法是「先 `iter().map(stealer).collect()`，再 move」。

---

### 4.4 调度主循环：Retry、任务闭包与退出协调

#### 4.4.1 概念说明

把拓扑、`find_task`、scoped threads 准备好之后，最后一块拼图是**调度主循环**本身。它要处理三件真实工程问题：

1. **Retry 处理**：`find_task` 内部已经用 `repeat_with` 把单次调用的 Retry 吃掉了，但当所有队列真返回 `Empty` 时，`find_task` 返回 `None`。此时线程不能直接退出——可能只是**这一瞬间**没任务，下一刻别的线程还没 push 完。所以主循环拿到 `None` 时要 `yield_now()` 让出 CPU 再重试，而不是死等或退出。
2. **任务闭包分发**：取到的 `T` 在真实调度器里通常是「要执行的任务闭包」，主循环 `find_task` 返回 `Some(task)` 后就执行 `task.run()`（本讲简化为直接处理值）。
3. **线程退出协调**：工作线程何时停？需要一个**终止条件**。最简单可靠的做法是用一个 `AtomicUsize` 计数「已完成的任务数」，当 `find_task` 返回 `None` 且 `done >= 总数` 时退出。这保证了：只要还有任务没被计数，就有线程会继续找活干，不会提前收摊。

#### 4.4.2 核心流程

单个工作线程的主循环（伪代码）：

```
loop {
    match find_task(&local, &global, &stealers) {
        Some(task) => {
            执行 task;
            done.fetch_add(1, SeqCst);          # 计数完成
        }
        None => {
            if done.load(SeqCst) >= TOTAL {      # 真的全部做完了
                break;
            }
            std::hint::spin_loop();              # 或 thread::yield_now() 让出 CPU
            # 然后回到 loop 顶端重试 find_task
        }
    }
}
```

关键不变式：**只要 `done < TOTAL`，就必然存在尚未被取走的任务**（要么在全局 `Injector`，要么在某个 `Worker` 本地），而 `find_task` 会遍历所有这些来源，因此循环不会在还有任务时永久误判为空。当 `done == TOTAL` 时，所有任务都已被 `fetch_add`，确认完成，可安全退出。

> 顺带一提：测试里处理 Retry 的另一种最小写法是 `busy_retry` 辅助函数——它不组合多个来源，只是「循环调用直到非 Retry」。[tests/steal.rs:7-14](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L7-L14) 就是其 8 行实现。`find_task` 用 `repeat_with(...).find(!is_retry)` 是它的「组合子化、多来源」升级版。

#### 4.4.3 源码精读

主循环调用的三个底层操作的真实签名（我们终于把它们串到了一起）：

- [src/deque.rs:450](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L450) `Worker::pop(&self) -> Option<T>` —— 本地弹一个，永远不返回 `Retry`（owner 独占，无竞争）。
- [src/deque.rs:641](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L641) `Stealer::steal(&self) -> Steal<T>` —— 偷一个，可能 `Retry`。
- [src/deque.rs:1766](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1766) `Injector::steal_batch_and_pop(&self, dest: &Worker<T>) -> Steal<T>` —— 从全局偷一批塞进 `dest` 并弹一个返回，可能 `Retry`。
- [src/deque.rs:1388](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1388) `Injector::push(&self, task: T)` —— 主线程注入任务，无返回值。

`stampede` 测试里 `done` 计数与单调性断言的写法（[tests/fifo.rs:112-140](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/fifo.rs#L112-L140)）是综合实践「`Arc<AtomicUsize>` 协调退出」的直接参照。

#### 4.4.4 代码实践

**实践目标**：单独验证「`None` 不等于终态」这一点，理解为什么主循环拿到 `None` 不能立刻退出。

**操作步骤**：

1. 创建一个 `Injector`，`push` 5 个任务；创建一个 `Worker` 和它的 `stealers` 列表（为简化，stealers 可为空 `&[]`）。
2. 调一次 `find_task(&worker, &injector, &[])`，应当返回 `Some(0)`（同时 injector 里剩余任务被批量倒进了 worker）。
3. 现在再连续调几次 `find_task`——你会观察到它从本地 `worker` 弹出剩余任务（因为上一步批量偷进来了）。
4. 等本地和全局都空，`find_task` 返回 `None`。此时**故意**再 `injector.push(99)`，然后再调 `find_task`——它又返回 `Some(99)`。

**预期结果**：这证明了 `None` 只代表「调用那一刻所有来源都空」，不代表「永远不会再有任务」。所以主循环拿到 `None` 必须 yield 后重试，并由外层 `done >= TOTAL` 来决定真正退出。

> 待本地验证：步骤 3 中「剩余任务被倒进本地 worker」的数量取决于 `steal_batch_and_pop` 内部「偷约一半」的策略（见 u3-l4），具体个数无需精确断言，只验证最终都能被取回即可。

#### 4.4.5 小练习与答案

**练习 1**：主循环里为什么用 `done.load(SeqCst) >= TOTAL` 作为退出条件，而不是 `find_task` 返回 `None`？

**答案**：`None` 只是「某一瞬间所有队列都空」，可能别的线程马上还会 push 子任务（在真实调度器里，任务执行中会产生新子任务）。只有「已经被取走并执行完毕的任务数达到总数」才是真正的完成信号。用计数协调能保证不提前退出、也不永久空转。

**练习 2**：`busy_retry`（tests/steal.rs）和 `find_task` 里的 `repeat_with(...).find(!is_retry)` 处理 Retry 的本质区别是什么？

**答案**：`busy_retry` 只对**单个偷取源**做「重试到非 Retry」；`find_task` 的 `repeat_with` 包住的是**整条组合链**（全局批量偷 + 所有 stealer），并且配合 `or_else`/`FromIterator` 把多个来源的结果正确归约——它既重试又组合，是 `busy_retry` 的多来源升级版。

---

## 5. 综合实践：实现一个最小的多线程 work-stealing 调度器

把第 4 节的四块拼图合起来：4 个工作线程、1 个全局 `Injector`、互相的 `Stealer` 列表，从 `Injector` 注入 100 个任务，验证全部被各线程协作取走执行、无丢失无重复、无 panic。

### 5.1 准备

新建一个 cargo 二进制项目，`Cargo.toml` 只需：

```toml
[dependencies]
crossbeam-deque = "0.8"
```

> 本示例用标准库的 `std::thread::scope`（1.63+ 稳定，无需额外依赖）。你也可以改用项目测试里的 `crossbeam_utils::thread::scope`，API 几乎一致。

### 5.2 完整示例代码

> ⚠️ 以下为**示例代码**（非仓库原有），综合了 `find_task`（来自 [src/lib.rs:52-76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L52-L76)）与 `stampede` 测试（[tests/fifo.rs:103-141](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/fifo.rs#L103-L141)）的写法。

```rust
// src/main.rs （示例代码）
use crossbeam_deque::{Injector, Stealer, Worker};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread;

/// 经典回退链：本地 pop → 全局批量偷并弹一个 → 遍历 stealer 偷。
/// 直接取自 src/lib.rs 顶部的 find_task doctest。
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
    const THREADS: usize = 4;
    const COUNT: usize = 100;

    // 1) 每个线程一个本地 Worker
    let workers: Vec<Worker<usize>> = (0..THREADS).map(|_| Worker::new_fifo()).collect();
    // 2) 先于 move 之前，收集每个 Worker 的 Stealer
    let stealers: Vec<Stealer<usize>> = workers.iter().map(|w| w.stealer()).collect();
    // 3) 全局 Injector，注入全部初始任务
    let injector = Injector::new();
    for i in 0..COUNT {
        injector.push(i);
    }

    // 4) 已完成计数，用于协调线程退出
    let done = AtomicUsize::new(0);

    let stealers = &stealers[..];
    let injector = &injector;

    // 5) scoped threads：Worker move 进线程，stealers/injector 借用共享
    thread::scope(|s| {
        for mut w in workers {
            let done = &done;
            s.spawn(move || {
                let id = thread::current().id();
                loop {
                    match find_task(&w, injector, stealers) {
                        Some(task) => {
                            // 这里「执行任务」简化为打印；真实调度器会 run(task)
                            println!("{id:?}: ran task {task}");
                            done.fetch_add(1, Ordering::SeqCst);
                        }
                        None => {
                            // 所有来源都空：若全部任务已完成则退出，否则让出 CPU 重试
                            if done.load(Ordering::SeqCst) >= COUNT {
                                break;
                            }
                            thread::yield_now();
                        }
                    }
                }
            });
        }
    });

    // 6) 断言：100 个任务各被取走恰好一次
    assert_eq!(done.load(Ordering::SeqCst), COUNT);
    println!("all {COUNT} tasks done");
}
```

### 5.3 操作步骤与观察

1. `cargo run`，预期看到 100 行 `ThreadId(..): ran task <n>` 与最后的 `all 100 tasks done`，无 panic、断言通过。
2. 多跑几次，观察 task 编号的**交错顺序**——不同线程会偷到不同子集，顺序非确定，但总数恒为 100。
3. 把 `Worker::new_fifo()` 改成 `Worker::new_lifo()`，重跑，观察本地队列消费顺序的变化（注意：从 `Injector` 批量偷进来的任务在 LIFO 本地队列里会逆序弹出，这正是 u2-l4 讲的「相对顺序由源 flavor 决定」）。
4. 把 `THREADS` 从 4 改成 8、`COUNT` 改成 10_000，验证在更高并发下仍然无丢失无重复。

### 5.4 进阶改造（可选）

- **「恰好一次」强验证**：把任务编号收集进一个 `Mutex<Vec<bool>>` 标记数组（长度 `COUNT`），每个任务执行时 `assert!(!flag[task]); flag[task] = true;`，最后断言全 `true`——这是 u4-l4 `stampede`/`mpmc` 测试的核心思路。
- **生产子任务**：让任务闭包在执行时往**自己的本地 `w`** 再 `push` 几个子任务，观察 work-stealing 如何让空闲线程偷走这些子任务——这才是「stealing」真正发挥作用的场景。
- **加 Backoff**：`None` 分支里用 `std::hint::spin_loop()` 自旋若干次后再 `yield_now()`，减少空闲时的 CPU 占用。

> 待本地验证：不同核数、不同 `COUNT` 下的吞吐与公平性表现。本示例保证「无丢失无重复」，但不保证任务公平性或优先级。

## 6. 本讲小结

- work-stealing 调度器的标准拓扑是 **N 个本地 `Worker` + 1 个全局 `Injector` + N 个互相的 `Stealer`**，三者各司其职：本地优先消费、全局共享入口、stealer 供空闲线程互相偷取。
- `find_task` 用 `Steal::or_else`（单步回退且保留 Retry）、`FromIterator`（聚合多 stealer）、`is_retry`/`success`（重试与解包）把三步找任务串成一条**自动重试 Retry、正确区分 Empty** 的回退链，核心是 `repeat_with(...).find(|s| !s.is_retry())`。
- 所有权拓扑：`Worker` 因 `!Sync` 必须 **move** 进各自线程；`Stealer`、`Injector` 因 `Sync` 可 `&` 共享；scoped threads（`std::thread::scope` 或 `crossbeam_utils::thread::scope`）让子线程安全借用栈上队列。
- 调度主循环的三件实事：`find_task` 内部消化单次 Retry、`None` 时 yield 后重试（`None` ≠ 终态）、用 `AtomicUsize` 计数协调线程在 `done >= TOTAL` 时退出。
- 完整调度器 = `find_task`（lib.rs 文档示例）+ scoped threads 拓扑 + `Arc/AtomicUsize` 计数协调（fifo.rs 的 `stampede` 范式），三者拼装即可跑通一个无丢失无重复的多线程 work-stealing 调度器。

## 7. 下一步学习建议

- **回到源码对照**：本调度器跑通后，建议带着「为什么不会丢任务」的直觉，重读 `Stealer::steal`（[src/deque.rs:641](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L641)）与 `Injector::steal`（[src/deque.rs:1464](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1464)），体会「CAS 推进 + 二次校验」如何保证每个任务恰好被偷一次。
- **看真实调度器如何用它**：Rayon、tokio 的 worker 调度、smol/async-executor 都基于这套 `Worker/Stealer/Injector + find_task` 模式，可以去读它们的「找任务 + 空闲休眠（parking）」实现，看生产级调度器如何处理本讲简化掉的「任务闭包分发、唤醒、优先级」。
- **横向阅读**：至此本手册 18 篇已完结。若想巩固并发正确性直觉，可重读 u4-l1（内存序）与 u4-l2（epoch 回收），把「调度器为何能在无锁下正确」与「换 buffer 后旧内存为何不 use-after-free」两条线索彻底打通。
