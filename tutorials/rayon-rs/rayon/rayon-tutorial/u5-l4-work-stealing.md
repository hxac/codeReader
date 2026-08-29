# u5-l4 工作窃取队列与调度循环

## 1. 本讲目标

前两讲我们认识了任务的底层表示（Job/JobRef/Latch）和线程注册表（Registry）。本讲把两者接起来，回答三个问题：

1. 任务被放进哪个队列？为什么每个线程要有一个自己的双端队列（deque），而不是全池共用一个队列？
2. 一个线程手头没活时，按什么顺序去找活干？本地弹出与远程窃取为什么方向相反？
3. 窃取失败之后会发生什么？随机起点扫描和 `Steal::Retry` 重试各解决什么问题？

学完本讲，你应该能独立读懂 `registry.rs` 中 `find_work` → `steal` → `pop_injected_job` 这条找活链路，并能通过实验观察工作窃取带来的负载均衡效果。

## 2. 前置知识

### 2.1 双端队列（deque）

deque（double-ended queue）是两端都能进出元素的结构。工作窃取调度里，它被用出了一个关键花样：**两端被不同的人使用**。

- 线程自己从**本地端**（back，新任务进入的一端）压入和弹出任务；
- 其他线程从**窃取端**（front，旧任务所在的一端）偷任务。

两端操作几乎不撞车，于是「自己干活」和「别人偷活」可以高度并发，这正是 Chase-Lev 工作窃取队列的核心思想。Rayon 没有手写这个无锁结构，而是直接使用 `crossbeam-deque`（见依赖声明 [rayon-core/Cargo.toml:L27-L30](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/Cargo.toml#L27-L30)，`crossbeam-deque.workspace = true`）。

### 2.2 crossbeam-deque 的三个角色与 Steal 三态

crossbeam-deque 把一条队列拆成两半，外加一个全局队列，共三个角色：

| 角色 | 归属 | 操作 | 在 rayon 中的位置 |
| --- | --- | --- | --- |
| `Worker<T>` | 队列主人（某工作线程） | `push`（本地端入队）、`pop`（本地端出队） | `WorkerThread.worker` |
| `Stealer<T>` | 其他线程持有 | `steal`（从窃取端取一个） | `ThreadInfo.stealer` |
| `Injector<T>` | 全池共享的无锁队列 | `push` / `steal` | `Registry.injected_jobs` |

`steal()` 的返回值是三态枚举 `Steal<T>`：

- `Success(job)`：偷到了；
- `Empty`：对端真的没有任务；
- `Retry`：与对端的并发操作撞车了（队列正处于中间状态），**应该重试**，不代表没有任务。

区分 `Empty` 和 `Retry` 是读懂窃取循环的前提：只有 `Empty` 才意味着「换下一个受害者」。

### 2.3 与前两讲的衔接

- u5-l2：`JobRef` 是「裸指针 + execute 函数指针」的类型擦除任务，固定两机器字，可以放心地当队列元素搬来搬去（[rayon-core/src/job.rs:L27-L39](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L27-L39)）。
- u5-l3：`Registry::new` 为每个线程创建队列、启动线程，线程进入 `main_loop`。本讲就从 `main_loop` 说起。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [rayon-core/src/registry.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs) | 本讲主战场：队列的创建、任务的入队路由、窃取循环、调度主循环 |
| [rayon-core/src/job.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs) | `JobRef` 定义、`JobFifo`（spawn_fifo 的间接队列） |
| [rayon-core/src/lib.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs) | 模块文档中对「任务进 deque、可被窃取」的总述，以及符号再导出 |
| [rayon-core/src/spawn/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/mod.rs) | `spawn`/`spawn_fifo` 的入队调用方，含 LIFO/FIFO 语义的官方注释 |
| [rayon-core/src/join/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs) | `join` 中闭包 B 的本地入队与认领循环 |

## 4. 核心概念与源码讲解

### 4.1 deque 双端队列：一个线程的三种任务来源

#### 4.1.1 概念说明

Rayon 池里的任务**不是**放在一个全局队列里大家抢，而是分成三个层次：

1. **本地 deque**：每个工作线程一条，`Worker` 半边归自己（压入/弹出），`Stealer` 半边登记在 `Registry.thread_infos` 里供别人偷。`join`、`spawn` 产生的任务优先进这里。
2. **全局注入队列 `Injector`**：池外线程（比如你的 main 线程）丢进来的任务先落这里，「谁闲谁取」。
3. **广播队列**：每线程一条 FIFO 队列，`broadcast` 的任务副本按线程投递，只能被该线程自己消费（详见 u6-l3）。

为什么这么设计？核心是**局部性**：任务由某线程创建，数据多半还在这个线程的 CPU 缓存里，让它优先自己干（LIFO，最新的先干）；别的线程来偷时，从另一端偷**最旧**的任务——旧任务的数据大概率已经凉了，偷走也不心疼。`spawn` 的文档注释把这条设计理由写得非常直白：

> they are generally prioritized in a LIFO order on the thread from which they were spawned. Other threads always steal from the other end of the deque, like FIFO order. The idea is that "recent" tasks are most likely to be fresh in the local CPU's cache, while other threads can steal older "stale" tasks.

见 [rayon-core/src/spawn/mod.rs:L26-L33](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/mod.rs#L26-L33)。

#### 4.1.2 核心流程

队列在 `Registry::new` 中创建，随后两半被拆开存放：

```text
Registry::new
 ├─ 每线程: worker = Worker::new_lifo()（或 new_fifo，由 breadth_first 决定）
 │          stealer = worker.stealer()          ← 本地 deque 的窃取半边
 ├─ 每线程: broadcast 的 fifo Worker + 其 stealer ← 广播队列
 └─ 组装:
     ├─ Registry.injected_jobs = Injector::new()  ← 全局队列
     ├─ ThreadInfo { stealer, ... }               ← 窃取半边登记进注册表
     └─ ThreadBuilder { worker, stealer(广播的), index, ... } → 交给线程启动
```

线程启动后，`ThreadBuilder` 转成 `WorkerThread`，`worker`（本地 deque 的操作半边）随线程走，窃取半边留在注册表里给同伴。

**易混淆点（本讲最重要的细节之一）**：代码里有两个 `stealer` 字段，指向完全不同的队列——

- `ThreadInfo.stealer`（registry 结构体侧）：**别人偷我本地 deque** 用的手柄；
- `WorkerThread.stealer`（线程侧）：**我自己取广播任务** 用的手柄（来自 broadcast 队列那一组）。

#### 4.1.3 源码精读

每个线程的本地 deque 在创建时就决定了方向。默认 LIFO，配置 `breadth_first` 后改 FIFO：

[rayon-core/src/registry.rs:L248-L259](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L248-L259)

```rust
let (workers, stealers): (Vec<_>, Vec<_>) = (0..n_threads)
    .map(|_| {
        let worker = if breadth_first {
            Worker::new_fifo()
        } else {
            Worker::new_lifo()
        };

        let stealer = worker.stealer();
        (worker, stealer)
    })
    .unzip();
```

这段代码为每个线程创建本地 deque：`Worker` 是自己用的半边，`worker.stealer()` 生成的 `Stealer` 是给别人用的半边，`unzip` 把两组分别收集。紧接着同样地为广播队列各建一条 FIFO（[rayon-core/src/registry.rs:L261-L267](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L261-L267)）。

组装 `Registry` 时，本地 deque 的窃取半边存入 `thread_infos`，全局队列在这里落地：

[rayon-core/src/registry.rs:L269-L274](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L269-L274)

```rust
let registry = Arc::new(Registry {
    thread_infos: stealers.into_iter().map(ThreadInfo::new).collect(),
    sleep: Sleep::new(n_threads),
    injected_jobs: Injector::new(),
    broadcasts: Mutex::new(broadcasts),
    ...
```

注意 `broadcasts` 存的是 Worker 一侧（投递端在 Registry 手里），而本地 deque 的 Stealer 一侧进了 `ThreadInfo`：

[rayon-core/src/registry.rs:L613-L631](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L613-L631)

```rust
struct ThreadInfo {
    primed: LockLatch,
    stopped: LockLatch,
    terminate: OnceLatch,
    /// the "stealer" half of the worker's deque
    stealer: Stealer<JobRef>,
}
```

`ThreadInfo` 是注册表视角的「每个线程一条记录」：三个生命周期锁存器（u5-l3 讲过）加上别人偷我任务的 `Stealer` 手柄。

线程自己这一侧则是 `WorkerThread`，注意两个字段的注释——`worker` 是本地任务 deque 的操作半边，`stealer` 却是**广播**队列的窃取半边：

[rayon-core/src/registry.rs:L647-L663](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L647-L663)

```rust
pub(super) struct WorkerThread {
    /// the "worker" half of our local deque
    worker: Worker<JobRef>,

    /// the "stealer" half of the worker's broadcast deque
    stealer: Stealer<JobRef>,

    /// local queue used for `spawn_fifo` indirection
    fifo: JobFifo,

    index: usize,

    /// A weak random number generator.
    rng: XorShift64Star,

    registry: Arc<Registry>,
}
```

从 `ThreadBuilder` 到 `WorkerThread` 的搬运过程可以证实「谁拿了哪个半边」（[rayon-core/src/registry.rs:L283-L291](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L283-L291) 中 `zip(broadcast_stealers)` 说明 ThreadBuilder 携带的是广播 stealer；[rayon-core/src/registry.rs:L674-L685](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L674-L685) 完成搬运）。

`rng` 字段是下一切分模块的主角：窃取时的随机起点生成器。

#### 4.1.4 代码实践

**实践目标**：在纸面上理清「三种队列、两个 stealer」的归属关系，并用单线程池验证窃取的前置条件。

**操作步骤**（源码阅读型实践）：

1. 画出你机器上的队列拓扑图：假设 4 线程，画出 4 条本地 deque（每条标注 Worker 半边在线程手里、Stealer 半边在 `ThreadInfo` 里）、1 个 `Injector`、4 条广播队列。
2. 对照 [rayon-core/src/registry.rs:L629-L630](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L629-L630) 与 [rayon-core/src/registry.rs:L651-L652](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L651-L652)，在你的图上分别标出这两个 `stealer` 字段各自连到哪条队列。
3. 阅读 [rayon-core/src/registry.rs:L880-L884](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L880-L884)，回答：单线程池里调用 `steal()` 会发生什么？

**需要观察的现象**：第 3 步的答案在代码里是确定性的——`num_threads <= 1` 时 `steal()` 直接返回 `None`，根本不会去碰任何队列。

**预期结果**：拓扑图中「本地 deque 的两个半边」分居 `WorkerThread` 和 `ThreadInfo` 两处，而 `WorkerThread.stealer` 连的是广播队列。此实践为纯阅读，无需运行验证；若想运行，可用 `RAYON_NUM_THREADS=1` 跑 4.2 节的实验，观察所有任务是否都落在 0 号线程（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Rayon 不用一个全局队列装所有任务？

**答案**：全局队列是所有线程的竞争热点，每次取任务都要原子竞争；本地 deque 让线程绝大多数时候只操作自己的队列（两端分离后连偷都很少撞车），全局 `Injector` 只用于池外投递这类低频路径。同时本地 LIFO 让最新任务优先执行，缓存局部性最好。

**练习 2**：`Worker::new_lifo()` 和 `Worker::new_fifo()` 分别对应什么配置？影响谁的行为？

**答案**：由 `ThreadPoolBuilder` 的 `breadth_first` 选项决定（[rayon-core/src/registry.rs:L246-L254](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L246-L254)）。默认 `new_lifo`：本地线程从新任务端弹出（深度优先）；`breadth_first = true` 时 `new_fifo`：本地改为从另一端出队，整体接近广度优先。注意这只改变**本地弹出方向**，窃取端行为由 crossbeam-deque 的语义保持一致。

**练习 3**：`ThreadInfo.stealer` 和 `WorkerThread.stealer` 分别偷谁的队列？

**答案**：`ThreadInfo.stealer` 是「本线程本地任务 deque」的窃取半边，**别的线程**通过它偷本线程的任务；`WorkerThread.stealer` 是「本线程广播队列」的窃取半边，**本线程自己**用它取出投递给自己的广播任务，广播任务不会被别的线程偷走。

### 4.2 窃取循环：find_work 三级优先链与随机起点扫描

#### 4.2.1 概念说明

工作线程的一生就是 `main_loop` → `wait_until_out_of_work` → `wait_until(terminate 锁存器)`：一边等「池子关闭」这个信号，一边不停地找活干。找活的优先级链写在 `find_work` 里，注释一句话点题：**先做完自己开始的，再去拿新的**（finish what we started before we take on something new）：

1. 本地 deque 弹一个（`take_local_job`）；
2. 本地空了 → 随机挑受害者偷一个（`steal`）；
3. 偷不到 → 去全局注入队列取一个（`pop_injected_job`）；
4. 全都空 → 进入睡眠退避（`sleep.no_work_found`，下一讲 u5-l5 的主题）。

工作窃取调度的经典理论结论（Blumofe–Leiserson 定理）保证：只要切分出的子任务足够多、窃取随机进行，则总执行时间满足

\[ T_P \le \frac{W}{P} + O(S) \]

其中 \( W \) 是总工作量、\( S \) 是关键路径（span）长度、\( P \) 是线程数。随机起点扫描正是「窃取随机进行」的实现：如果所有空闲线程都从 0 号线程开始扫，0 号会被围观，而队尾的受害者无人问津。

#### 4.2.2 核心流程

`take_local_job` 的两步：

```text
take_local_job():
    1. worker.pop()                  ← 本地 deque 弹出一个（LIFO：最新的）
    2. 若本地为空：stealer.steal()    ← 从自己的广播队列取（Retry 则自旋后重试）
```

`steal` 的完整流程：

```text
steal():
    断言本地 deque 已空（窃取是最后手段）
    若线程数 <= 1：返回 None
    循环：
        retry = false
        start = rng.next_usize(num_threads)        ← 随机起点
        按 start, start+1, ..., 环形绕回 0..start 的顺序扫描受害者（跳过自己）:
            victim.stealer.steal():
                Success(job) → 命中，返回
                Empty        → 该受害者没活，试下一个
                Retry        → retry = true，试下一个
        若拿到任务 或 整圈都没遇到 Retry：返回结果
        否则 spin_loop() 后换一个随机起点重来
```

#### 4.2.3 源码精读

先是 `take_local_job`，注意它内部那个 `stealer` 是广播队列的手柄：

[rayon-core/src/registry.rs:L744-L763](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L744-L763)

```rust
/// Attempts to obtain a "local" job -- typically this means
/// popping from the top of the stack, though if we are configured
/// for breadth-first execution, it would mean dequeuing from the
/// bottom.
pub(super) fn take_local_job(&self) -> Option<JobRef> {
    let popped_job = self.worker.pop();

    if popped_job.is_some() {
        return popped_job;
    }

    loop {
        match self.stealer.steal() {
            Steal::Success(job) => return Some(job),
            Steal::Empty => return None,
            Steal::Retry => std::hint::spin_loop(),
        }
    }
}
```

这段先从本地 deque `pop`（默认配置下拿到的是最新压入的任务）；本地空了再去自己的广播队列取投递给自己的任务；`Retry` 表示撞上并发操作，`spin_loop` 提示 CPU 后再试。注释中的 top/bottom 与我们在 2.1 节说的 back/front 是同一件事的两种视角。

三级优先链本体只有三行，是本讲最重要的代码：

[rayon-core/src/registry.rs:L835-L844](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L835-L844)

```rust
fn find_work(&self) -> Option<JobRef> {
    // Try to find some work to do. We give preference first
    // to things in our local deque, then in other workers
    // deques, and finally to injected jobs from the
    // outside. The idea is to finish what we started before
    // we take on something new.
    self.take_local_job()
        .or_else(|| self.steal())
        .or_else(|| self.registry.pop_injected_job())
}
```

`Option::or_else` 的短路语义恰好表达了优先级：前一级返回 `Some` 就不再尝试后一级。

窃取函数逐行读，重点是环形扫描和 Retry 处理：

[rayon-core/src/registry.rs:L871-L908](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L871-L908)

```rust
fn steal(&self) -> Option<JobRef> {
    // we only steal when we don't have any work to do locally
    debug_assert!(self.local_deque_is_empty());

    let thread_infos = &self.registry.thread_infos.as_slice();
    let num_threads = thread_infos.len();
    if num_threads <= 1 {
        return None;
    }

    loop {
        let mut retry = false;
        let start = self.rng.next_usize(num_threads);
        let job = (start..num_threads)
            .chain(0..start)
            .filter(move |&i| i != self.index)
            .find_map(|victim_index| {
                let victim = &thread_infos[victim_index];
                match victim.stealer.steal() {
                    Steal::Success(job) => Some(job),
                    Steal::Empty => None,
                    Steal::Retry => {
                        retry = true;
                        None
                    }
                }
            });
        if job.is_some() || !retry {
            return job;
        }
        std::hint::spin_loop();
    }
}
```

逐行拆解：

- `debug_assert`：窃取是最后手段，本地还有任务时不许偷（与 `find_work` 的优先级一致）；
- `num_threads <= 1`：单线程池无处可偷，直接放弃；
- `self.rng.next_usize(num_threads)`：用线程私有的 xorshift* 弱随机数发生器（[rayon-core/src/registry.rs:L969-L1007](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L969-L1007)）选一个随机起点，避免所有窃取者扎堆扫同一名受害者；
- `(start..num_threads).chain(0..start)`：从起点扫到末尾再绕回开头，构成一个不重复的环形遍历；`filter(i != self.index)` 跳过自己；
- `find_map` 对每个受害者尝试一次 `steal()`：`Success` 立即返回；`Empty` 视为「这家没活」继续找下一家；`Retry` 记下标志后也继续找下一家（也许别家有活）；
- 整圈扫完：拿到任务就返回；没拿到但一圈遇到过 `Retry`，说明可能只是撞车了而非真没活，`spin_loop` 退避一下换新起点再来；没拿到且没遇到 `Retry`（全是 `Empty`），才确认全池无活，返回 `None`。

找到任务后执行只是一行转发（[rayon-core/src/registry.rs:L866-L869](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L866-L869)），真正的调用者是外层的等待循环：

[rayon-core/src/registry.rs:L780-L817](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L780-L817)

```rust
unsafe fn wait_until_cold(&self, latch: &CoreLatch) {
    ...
    'outer: while !latch.probe() {
        // Check for local work *before* we start marking ourself idle,
        if let Some(job) = self.take_local_job() {
            unsafe { self.execute(job) };
            continue;
        }

        let mut idle_state = self.registry.sleep.start_looking(self.index);
        while !latch.probe() {
            if let Some(job) = self.find_work() {
                self.registry.sleep.work_found();
                unsafe { self.execute(job) };
                continue 'outer;
            } else {
                self.registry.sleep
                    .no_work_found(&mut idle_state, latch, || self.has_injected_job())
            }
        }
        ...
    }
    ...
}
```

这就是工作线程的调度循环：外层先查本地活；没有则向 sleep 模块登记「我开始找了」（`start_looking`），内层循环反复 `find_work`——找到就 `work_found` 撤销空闲状态、执行任务；找不到就交给 `no_work_found` 处理（逐步退避直至睡眠，细节在 u5-l5）。整个 `main_loop`（[rayon-core/src/registry.rs:L913-L944](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L913-L944)）最终就是 `wait_until_out_of_work` → `wait_until(terminate)`（[rayon-core/src/registry.rs:L819-L833](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L819-L833)），即「一边等关闭信号一边循环执行上面的找活逻辑」。

顺带一提，`yield_now` / `yield_local` 这两个公开 API 就是这条链路的直接复用：前者调 `find_work`（三级都找），后者只调 `take_local_job`（只做本地活），见 [rayon-core/src/registry.rs:L846-L864](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L846-L864)（u7-l3 会展开它们的防饿死用途）。

#### 4.2.4 代码实践

**实践目标**：给窃取循环加上自己的注释，再写一个生产者—消费者小实验，观察 100 个任务在线程间的分布，直观感受工作窃取的负载均衡。

**操作步骤**：

1. **注释练习**（在你自己 fork/副本的 `rayon-core/src/registry.rs` 上做，只加注释、不改代码，不影响构建；不要改动原仓库）：在 `steal()` 函数的 `loop`、`next_usize`、`chain`、三个 `Steal::` 分支和末尾 `spin_loop()` 各写一行中文注释，说明每步动作。
2. **实验程序**：新建独立 Cargo 项目（`cargo new ws-demo`），在 `Cargo.toml` 加入 `rayon = "1.12"`，把下面的示例代码写入 `src/main.rs`，用 `cargo run --release` 运行。

示例代码（非项目原有代码）：

```rust
use rayon::{current_num_threads, current_thread_index};
use std::collections::BTreeMap;
use std::sync::mpsc;

fn main() {
    let (tx, rx) = mpsc::channel::<usize>();
    for _ in 0..100 {
        let tx = tx.clone();
        rayon::spawn(move || {
            // 记录本任务实际运行在哪个工作线程上
            let idx = current_thread_index().expect("spawn 的任务应运行在池内线程");
            tx.send(idx).unwrap();
        });
    }
    drop(tx); // 关闭发送端，recv 才能在收满 100 条后结束

    let mut counts = BTreeMap::new();
    for _ in 0..100 {
        *counts.entry(rx.recv().unwrap()).or_insert(0) += 1;
    }

    println!("池内线程数: {}", current_num_threads());
    for (idx, n) in &counts {
        println!("线程 {:>2}: {} ({})", idx, "#".repeat(*n), n);
    }
}
```

3. 变体运行：`RAYON_NUM_THREADS=2 cargo run --release` 与 `RAYON_NUM_THREADS=1 cargo run --release` 各跑一次，对比直方图。

**需要观察的现象**：

- 默认（线程数 = 逻辑核数）时，100 个任务大致均匀铺在所有线程上——因为 main 线程不是池内线程，`spawn` 走的是全局 `Injector`（见 4.3 节路由规则），各工作线程在 `find_work` 的第三级抢走它们，先闲先得；
- `RAYON_NUM_THREADS=1` 时，所有任务都落在 0 号线程，且窃取代码路径按 [rayon-core/src/registry.rs:L882-L884](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L882-L884) 直接短路。

**预期结果**：默认运行的分布接近均匀（例如 8 线程上每线程 10~15 个）；任务极轻时可能有个别线程偏多或为 0，因为 100 个任务瞬间入队、执行飞快，谁抢到多少有随机性。具体数值待本地验证，但「不会由单个线程包办」是确定的。

#### 4.2.5 小练习与答案

**练习 1**：`find_work` 为什么把「偷别人」排在「取全局注入」前面？

**答案**：注入队列是池外线程投递的全局共享队列，取一次要和其他所有空闲线程竞争；而偷邻居拿到的往往是**已被切分好的子任务**，做完它通常离「自己参与的整棵任务树完成」更近。先完成已开始的计算、再承接全新工作，能减少在途任务数并改善缓存局部性（注释原话：finish what we started before we take on something new）。

**练习 2**：`steal` 里 `(start..num_threads).chain(0..start)` 实现了什么遍历？为什么要随机化 `start`？

**答案**：以 `start` 为起点的环形遍历，保证每轮扫描不重不漏地访问所有其他线程各一次。随机化起点是为了让多个空闲线程的扫描序列互相错开：若都从 0 开始，0 号线程的队列会被过度竞争（反复撞车出 `Retry`），而排在后面的受害者被冷落，负载均衡变差。

**练习 3**：一圈扫描全是 `Empty` 与遇到过 `Retry` 但没偷到，两种情况的后续处理有何不同？为什么？

**答案**：前者直接返回 `None`（全池确认无活，交给 sleep 模块退避）；后者 `spin_loop()` 后换一个新随机起点整圈重来。因为 `Retry` 只表示与受害者的并发操作撞车、**并不能证明对端没有任务**，直接放弃可能漏掉实际存在的活。

### 4.3 任务入队策略：push、inject 与 FIFO 变体

#### 4.3.1 概念说明

前两个模块讲「怎么取」，这个模块讲「怎么放」。任务入队的路由规则只有一条判断，但影响深远：

> **如果当前线程就是本池的工作线程 → 压入自己的本地 deque（快、可被偷）；否则 → 注入全局 Injector（慢一点的共享队列）。**

不同入口在这条规则上的表现：

| 入口 | 入队位置 | 顺序语义 |
| --- | --- | --- |
| `join` 的闭包 B | 本地 deque（池内执行） | LIFO：A 执行完自己 pop B |
| `spawn`（池外调用） | 全局 Injector | 无序，谁闲谁取 |
| `spawn`（池内调用） | 本地 deque | LIFO：本地优先自己干 |
| `spawn_fifo`（池内调用） | 本地 deque + JobFifo 间接层 | FIFO：同线程先 spawn 先执行 |
| `broadcast` | 每线程广播队列 | 每线程一个副本，仅本线程消费 |

LIFO 作为默认值还有一个吞吐层面的好处：`join` 递归切分时，本地栈顶永远是**最大**的那块未处理任务，空闲线程一次窃取就能拿到大粒度工作，切分开销被摊薄。

#### 4.3.2 核心流程

`spawn` 的入队决策伪代码：

```text
spawn_in(func, registry):
    job_ref = spawn_job(func, registry)     # 装箱 HeapJob + terminate 计数 +1
    inject_or_push(job_ref):
        if 当前线程是本 registry 的工作线程:
            worker_thread.push(job_ref)     # 本地 deque + 通知 sleep 模块
        else:
            registry.inject(job_ref)        # 全局 Injector + 唤醒空闲线程
```

`push_fifo` 的间接层把戏：

```text
push_fifo(job):
    fifo.push(job)        # 真任务进 JobFifo 内部的 Injector（先进先出）
    → 返回一个指向 JobFifo 自身的特殊 JobRef
    push(该特殊 JobRef)   # 本地 deque 里只占一个位置
# 执行该特殊 JobRef 时，从 JobFifo 里弹出最早的任务执行 —— 顺序反转完成
```

#### 4.3.3 源码精读

路由判断本体：

[rayon-core/src/registry.rs:L409-L421](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L409-L421)

```rust
/// Push a job into the given `registry`. If we are running on a
/// worker thread for the registry, this will push onto the
/// deque. Else, it will inject from the outside (which is slower).
pub(super) fn inject_or_push(&self, job_ref: JobRef) {
    let worker_thread = WorkerThread::current();
    unsafe {
        if !worker_thread.is_null() && (*worker_thread).registry().id() == self.id() {
            (*worker_thread).push(job_ref);
        } else {
            self.inject(job_ref);
        }
    }
}
```

两个条件缺一不可：当前线程**是工作线程**且**属于本池**（跨池时即使对方是工作线程，也不能往人家的本地 deque 里塞别人的任务，只能走注入）。

本地入队与全局注入的实现：

[rayon-core/src/registry.rs:L728-L737](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L728-L737)

```rust
pub(super) unsafe fn push(&self, job: JobRef) {
    let queue_was_empty = self.worker.is_empty();
    self.worker.push(job);
    self.registry.sleep.new_internal_jobs(1, queue_was_empty);
}

pub(super) unsafe fn push_fifo(&self, job: JobRef) {
    unsafe { self.push(self.fifo.push(job)) };
}
```

`push` 压入本地端后要通知 sleep 模块「来了新活」（队列从空变非空时可能要唤醒睡眠的同伴，`queue_was_empty` 就是为此记录的）。`push_fifo` 先经 `JobFifo` 包装再压队。

[rayon-core/src/registry.rs:L426-L442](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L426-L442)

```rust
pub(super) fn inject(&self, injected_job: JobRef) {
    ...
    let queue_was_empty = self.injected_jobs.is_empty();

    self.injected_jobs.push(injected_job);
    self.sleep.new_injected_jobs(1, queue_was_empty);
}
```

`inject` 是池外投递的唯一入口：压入全局 `Injector` 并唤醒空闲线程。工作线程在 `find_work` 的第三级用 `pop_injected_job` 取走它（循环处理 `Retry`，见 [rayon-core/src/registry.rs:L448-L456](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L448-L456)）。

`spawn` 调用方一侧，还能看到任务如何与池的生命周期挂钩：

[rayon-core/src/spawn/mod.rs:L69-L82](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/mod.rs#L69-L82)

```rust
pub(super) unsafe fn spawn_in<F>(func: F, registry: &Arc<Registry>)
where
    F: FnOnce() + Send + 'static,
{
    let abort_guard = unwind::AbortIfPanic;
    let job_ref = unsafe { spawn_job(func, registry) };
    registry.inject_or_push(job_ref);
    mem::forget(abort_guard);
}
```

`spawn_job`（[rayon-core/src/spawn/mod.rs:L84-L100](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/mod.rs#L84-L100)）装箱 `HeapJob`，闭包体内先执行用户函数再 `registry.terminate()` 平衡 `increment_terminate_count()`——这保证 fire-and-forget 的任务在池被 drop 前一定有机会跑完。

`join` 的闭包 B 走的正是本地 `push`：

[rayon-core/src/join/mod.rs:L132-L142](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L132-L142)

```rust
registry::in_worker(|worker_thread, injected| unsafe {
    let job_b = StackJob::new(call_b(oper_b), SpinLatch::new(worker_thread));
    let job_b_ref = job_b.as_job_ref();
    let job_b_id = job_b_ref.id();
    worker_thread.push(job_b_ref);

    // Execute task a; hopefully b gets stolen in the meantime.
    let status_a = unwind::halt_unwinding(call_a(oper_a, injected));
```

B 被压入本地队尾（对其他线程可见、可被偷），当前线程随即就地执行 A——这就是 u5-l1 讲过的「B 入队、先做 A」在此处的落点。A 做完后通过 `take_local_job` 逐个弹回认领 B，认领失败则说明 B 被偷，转入 `wait_until` 帮工模式（[rayon-core/src/join/mod.rs:L153-L169](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L153-L169)）。

最后看 `JobFifo` 如何用「把队列本身变成任务」实现 FIFO：

[rayon-core/src/job.rs:L243-L262](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L243-L262)

```rust
/// Indirect queue to provide FIFO job priority.
pub(super) struct JobFifo {
    inner: Injector<JobRef>,
}

impl JobFifo {
    pub(super) unsafe fn push(&self, job_ref: JobRef) -> JobRef {
        // A little indirection ensures that spawns are always prioritized in FIFO order.  The
        // jobs in a thread's deque may be popped from the back (LIFO) or stolen from the front
        // (FIFO), but either way they will end up popping from the front of this queue.
        self.inner.push(job_ref);
        unsafe { JobRef::new(self) }
    }
}
```

真任务进 `JobFifo` 自己的 `Injector`，本地 deque 里只放一个「执行我 = 从 JobFifo 弹出最早任务」的代理 JobRef（[rayon-core/src/job.rs:L264-L278](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L264-L278)）。无论本地 LIFO 弹出还是被别人偷走，触达的都是这同一个代理，代理再按 FIFO 吐出真任务——一行注释道破了 LIFO/FIFO 两端方向与代理的兼容性。`spawn_fifo` 的调用侧路由见 [rayon-core/src/spawn/mod.rs:L141-L160](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/mod.rs#L141-L160)：池内走 `push_fifo`，池外照旧 `inject`。

#### 4.3.4 代码实践

**实践目标**：对比「池外 spawn」与「池内 spawn」的任务分布，验证入队路由规则带来的可见差异。

**操作步骤**：在 4.2 的示例工程里改造 `main`（示例代码，非项目原有代码）：

```rust
use rayon::{current_thread_index, ThreadPoolBuilder};
use std::collections::BTreeMap;
use std::sync::mpsc;

fn spawn_100(tx: &std::sync::mpsc::Sender<(&'static str, usize)>, group: &'static str) {
    for _ in 0..100 {
        let tx = tx.clone();
        rayon::spawn(move || {
            let idx = current_thread_index().expect("应在池内线程执行");
            tx.send((group, idx)).unwrap();
        });
    }
}

fn main() {
    let pool = ThreadPoolBuilder::new().num_threads(4).build().unwrap();
    let (tx, rx) = mpsc::channel();

    // 实验二：先在池内 spawn（任务进当前线程的本地 deque，LIFO）
    pool.install(|| spawn_100(&tx, "in-pool"));

    // 实验一：再从池外 spawn（任务进全局 Injector）
    spawn_100(&tx, "out-pool");
    drop(tx);

    let mut counts: BTreeMap<(&str, usize), usize> = BTreeMap::new();
    for _ in 0..200 {
        let key = rx.recv().unwrap();
        *counts.entry(key).or_insert(0) += 1;
    }
    for ((group, idx), n) in &counts {
        println!("{group:>8} 线程 {idx}: {n}");
    }
}
```

**需要观察的现象**：

- `in-pool` 组：任务极轻时，大多数会集中在**同一个**线程索引上——因为它们 LIFO 压入执行 `install` 闭包的那个线程的本地 deque，该线程飞快地逐个 pop，别的线程几乎偷不到；
- `out-pool` 组：分布明显更均匀——它们走全局 Injector，四个线程都能在 `find_work` 第三级取到。

**预期结果**：两组计数呈现「一头独大」与「大致均摊」的对比。若给每个任务加 `std::thread::sleep(std::time::Duration::from_millis(1))` 再跑，池内组的分布也会被拉平——本地线程被 sleep 卡住，窃取窗口打开，同伴从队列另一端把旧任务偷走了。具体数值待本地验证。

**注意**：`install` 只等待闭包本身返回，不等 fire-and-forget 任务完成；上面用 `rx.recv()` 收满 200 条来兜底等待（能收满是 `spawn_job` 的 terminate 计数在保证池不提前关闭）。

#### 4.3.5 小练习与答案

**练习 1**：`inject_or_push` 为什么要同时检查「是工作线程」和「registry id 相同」两个条件？

**答案**：`WorkerThread::current()` 非空只说明当前线程属于**某个**池；跨池场景（A 池线程向 B 池投任务）若直接 `push` 进 A 池线程的本地 deque，任务会被 A 汓执行而不是 B 池。id 不匹配时必须走 `inject`，保证任务只进入目标池的调度范围（死锁风险与跨池隔离见 u7-l2）。

**练习 2**：`spawn_fifo` 为什么不直接把任务压进一个 FIFO 方向的本地 deque，而要引入 `JobFifo` 间接层？

**答案**：本地 deque 的方向在**建池时**由 `breadth_first` 一次性决定（`Worker::new_lifo` 或 `new_fifo`），而 `spawn_fifo` 需要让**同一池内**的普通任务保持 LIFO、只有 `spawn_fifo` 的任务按 FIFO，两个需求共用一条 deque 无法表达。间接层把 FIFO 顺序藏在代理 JobRef 背后：真任务在 `JobFifo` 的 Injector 里排队，本地 deque 只见代理，任何方式取出代理都等价于「按 FIFO 执行下一个 fifo 任务」。

**练习 3**：`push` 里记录 `queue_was_empty` 传给 `sleep.new_internal_jobs` 有什么用？

**答案**：队列从空变非空是一个需要「广播」的瞬间——可能有线程已经或正要因无活而睡眠，必须把它唤醒，否则任务会滞留队列无人执行。队列原本非空时，说明同伴们还有活可找、大概率醒着，就可以省掉昂贵的唤醒操作。这是与 u5-l5 睡眠协议的接口。

## 5. 综合实践

把三个模块串起来，做一个「窃取观察器」：

1. 复用 4.3 的示例工程，把任务改成带参数的版本：每个任务 `sleep(work_ms)` 后上报自己的线程索引。
2. 跑三组配置并记录四线程池上的分布直方图：
   - A：池外 `spawn`，`work_ms = 0`；
   - B：池内（`install` 中）`spawn`，`work_ms = 0`；
   - C：池内 `spawn`，`work_ms = 5`。
3. 为三组结果各写一段解释，必须引用本讲讲过的机制作答，自查要点：
   - A 组为何均匀？（入队走 [inject_or_push](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L409-L421) 的 else 分支 → Injector → `find_work` 第三级）
   - B 组为何集中？（LIFO 本地 pop 快于远程窃取，且本地端与窃取端方向相反，偷到的总是最旧任务）
   - C 组为何重新均匀？（sleep 打开窃取窗口，空闲线程在 [steal](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L871-L908) 的环形扫描中偷走积压的旧任务）
4. 进阶（可选）：把 `spawn` 全部换成 `spawn_fifo`，比较 B 组分布是否有可见变化，并结合 [JobFifo](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L243-L262) 的代理机制解释原因。

本实践不修改 rayon 源码；所有结论都应能在「入队路由 + 三级优先链 + 两端方向相反」三个机制内自洽解释。具体数值待本地验证。

## 6. 本讲小结

- 每个工作线程拥有**本地 deque**（`Worker` 半边自己用、`Stealer` 半边登记在 `ThreadInfo` 供他人偷），另有全局 `Injector` 装池外投递的任务、每线程一条广播队列装 broadcast 副本；`ThreadInfo.stealer` 与 `WorkerThread.stealer` 连的是**不同**队列。
- 本地与远程方向相反：本地端（back）LIFO 弹出最新任务、保持缓存热度；窃取端（front）FIFO 偷走最旧任务，两端并发几乎不冲突。
- 找活走三级优先链 `find_work`：本地 `take_local_job` → 环形随机扫描 `steal` → 全局 `pop_injected_job`，全都落空才交给 sleep 模块退避（u5-l5）。
- `steal` 用线程私有 xorshift* 随机数选扫描起点避免扎堆；`Steal::Retry` 只代表并发撞车，遇到要换新起点重试，全是 `Empty` 才确认无活；单线程池直接返回 `None`。
- 入队路由 `inject_or_push`：本池工作线程走本地 `push`（LIFO），否则全局 `inject`；`spawn_fifo` 用 `JobFifo` 代理在共享 deque 上叠加 FIFO 语义；`join` 的闭包 B 也是本地 `push` 后先执行 A。

## 7. 下一步学习建议

本讲结尾处，`find_work` 返回 `None` 后线程的去向被有意留白了：`sleep.no_work_found` 里的分级退避与唤醒协议正是下一讲 **u5-l5 睡眠与唤醒协议** 的主题，建议按顺序阅读 [rayon-core/src/sleep/README.md](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/README.md) 与 counters.rs 中的原子计数器不变量。之后可进入单元六（scope/spawn/broadcast），其中 broadcast 队列的消费路径会在 u6-l3 与本讲的广播 `stealer` 呼应；想动手实验的读者也可以提前阅读 [rayon-core/src/spawn/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/mod.rs) 的 `spawn_fifo_in`，验证自己对 JobFifo 间接层的理解。
