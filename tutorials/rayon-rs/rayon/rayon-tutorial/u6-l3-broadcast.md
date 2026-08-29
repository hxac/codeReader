# broadcast：向所有线程广播

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `broadcast` 与 `spawn_broadcast` 的语义：给线程池中**每一个**工作线程各投递一个任务副本，闭包在每个线程上各执行一次。
- 理解 `broadcast` 如何用「每线程一个 `StackJob` + 一个 `CountLatch`」实现阻塞等待，并按线程索引顺序收集出 `Vec<R>`。
- 学会使用 `BroadcastContext` 的 `index()` / `num_threads()` 拿到当前执行线程的身份信息，并理解它为什么是 `!Send + !Sync` 的。
- 区分两条结果返回路径：`broadcast` 同步返回有序 `Vec`，`spawn_broadcast` 立即返回、需用 channel 等手段自行汇总。
- 把 `broadcast` 放进已有认知坐标：与 `join`、`spawn`、`scope`、`Scope::spawn_broadcast` 逐一对比，知道什么场景选什么。

## 2. 前置知识

本讲是单元六第三讲，建立在前面几讲的概念之上，先快速回顾：

- **派发 API 的两档风格**（u6-l1、u6-l2）：`join`/`scope` 是阻塞式——调用点等所有子任务完成才返回，因此闭包可以借用栈上数据；`spawn` 是 fire-and-forget——立即返回，闭包必须 `'static + Send`。本讲的 `broadcast` 属于阻塞档，`spawn_broadcast` 属于异步档。
- **CountLatch**（u6-l1 在 `scope` 中见过）：计数锁存器，初始计数为 \( n \)，每完成一件事减 1，减到 0 才真正「置位」唤醒等待者。`scope` 用它数「body + 存活任务」，本讲用他数「n 个广播任务」。
- **Job 的三种形态**（u5-l2）：`StackJob` 活在调用者栈帧上（零堆分配，依赖「等待结束前栈帧不弹」这一不变量）；`HeapJob` 独占所有权、恰好执行一次；`ArcJob` 靠引用计数**可以被执行多次**——这正是「同一个任务在 n 个线程各跑一遍」的关键。
- **找活优先级链**（u5-l4）：工作线程找活顺序是「本地 deque → 窃取他人 → 全局注入队列」。本讲会精确化这条链：广播队列其实插在「本地 deque 空」与「窃取」之间。
- **线程池与 Registry**（u5-l3、u7 预习）：全局池是惰性单例，`ThreadPoolBuilder::build()` 可建用户池；`Registry` 内部持有所有线程的队列信息。

一个新术语：**广播队列（broadcast deque）**。每个工作线程除了自己的本地任务 deque 外，还独占一条专用的 FIFO 队列，`broadcast` 投递的任务就放在这里。它是理解「为什么广播任务一定在目标线程执行」的钥匙，4.1.3 会精读。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `rayon-core/src/broadcast/mod.rs` | 本讲主战场：`broadcast`/`spawn_broadcast` 顶层函数、`BroadcastContext`、内部实现 `broadcast_in`/`spawn_broadcast_in` |
| `rayon-core/src/broadcast/test.rs` | 行为验证测试：顺序、覆盖、嵌套、panic、优先级 |
| `rayon-core/src/thread_pool/mod.rs` | `ThreadPool::broadcast`/`ThreadPool::spawn_broadcast` 门面方法 |
| `rayon-core/src/registry.rs` | 广播队列的创建与归属、`inject_broadcast` 投递、`take_local_job` 消费、`catch_unwind` 与终止计数 |
| `rayon-core/src/job.rs` | `StackJob`（broadcast 用）与 `ArcJob`（spawn_broadcast 用） |
| `rayon-core/src/latch.rs` | `CountLatch::with_count`/`wait`/`set` 与 `LatchRef` |
| `rayon-core/src/scope/mod.rs` | `Scope::spawn_broadcast`：作用域版广播，可借 `'scope` 数据 |
| `rayon-core/src/lib.rs` / `src/lib.rs` | rayon-core 与上层 rayon 的再导出 |

## 4. 核心概念与源码讲解

### 4.1 broadcast：阻塞式全池执行与结果收集

#### 4.1.1 概念说明

前面学过的派发原语都是「把一个任务交给**某一个**线程」：`join` 交给当前线程加一个窃取者，`spawn` 丢进队列谁抢到算谁的。但有一类需求是「每个线程都得做一遍」，典型场景：

- 在每个工作线程上初始化/刷新线程本地资源（每线程缓存、每线程随机数种子、每线程日志缓冲）；
- 探测式查询：让每个线程汇报自己的状态（索引、当前负载）汇总成 `Vec`；
- 在 WebAssembly 等无线程平台的降级模式下，借低优先级的 `broadcast` 给 `spawn` 的任务一个执行机会（见 `rayon-core/src/lib.rs` 顶部文档）。

`broadcast` 就是这个「向全员投递」的原语，它是**阻塞式**的：调用线程把 n 个任务副本分别投进 n 个线程的广播队列，然后等待全部完成，最后把每个线程的返回值收集成一个 `Vec<R>` 返回。

两个最重要的语义保证（先记住，源码马上验证）：

1. **恰好每线程执行一次，且一定在「自己的线程」上执行**——广播任务不会被别的线程窃取走。
2. **返回的 `Vec<R>` 按线程索引排列**：第 i 个元素来自 index 为 i 的线程，与任务完成先后无关。

#### 4.1.2 核心流程

`broadcast_in` 的执行过程可以用伪代码概括：

```text
broadcast_in(op, registry):
    n = registry 线程数
    latch = CountLatch(计数 = n)            # 减到 0 才唤醒
    jobs  = [StackJob(闭包 op, latch 的引用); n 个]   # 每线程一个，结果槽各自独立
    registry.inject_broadcast(jobs 的 JobRef)        # 第 i 个 job 进第 i 个线程的广播队列
    latch.wait()                                     # 阻塞：池内线程边等边帮工，池外线程睡眠
    return [job.into_result() for job in jobs]       # 按线程索引顺序收集
```

配合计数锁存器的数学关系：latch 初始值为 \( n \)，每个任务执行完调用一次 `set`（内部 `fetch_sub(1)`），仅当旧值为 1（即本次减到 0）才真正触发唤醒。所以「全部完成」的判定是

\[ \text{完成数} = n \iff \text{latch 计数} = 0 \]

这里的 n 个 `StackJob` 共享同一个闭包 `op`（以引用形式），但各自持有独立的 `JobResult` 结果槽，因此每个线程写自己的槽，互不竞争，全程无锁。

#### 4.1.3 源码精读

**顶层入口。** 非池线程调用时落到全局池；注意闭包约束是 `Sync` 而非 `'static`——因为阻塞语义保证返回时栈帧数据必然使用完毕：

- [rayon-core/src/broadcast/mod.rs:19-26](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/mod.rs#L19-L26)：`broadcast` 函数签名，`OP: Fn(BroadcastContext<'_>) -> R + Sync`，`R: Send`，内部取 `Registry::current()` 转入 `broadcast_in`。
- [rayon-core/src/thread_pool/mod.rs:187-194](https://github.com/rayon-rs-rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L187-L194)：`ThreadPool::broadcast`，与顶层函数同签名，只是把目标池固定为 `self.registry`。其文档注释（[rayon-core/src/thread_pool/mod.rs:145-166](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L145-L166)）写明了执行时机与 panic 策略：广播任务在每线程**耗尽本地队列之后、尝试窃取之前**执行；panic 则等所有线程跑完后传播恰好一个。

**核心实现 `broadcast_in`：**

- [rayon-core/src/broadcast/mod.rs:97-122](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/mod.rs#L97-L122)：依次完成「包一层断言闭包 → 建 `CountLatch::with_count(n_threads)` → 生成 n 个 `StackJob` → `inject_broadcast` 投递 → `latch.wait` → `into_result` 收集」。其中 `f = move |injected: bool| { debug_assert!(injected); BroadcastContext::with(&op) }` 里的断言含义是：广播任务永远以「注入」路径执行（对照 [rayon-core/src/job.rs:223-228](https://github.com/rayon-rs-rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L223-L228)，`JobResult::call` 固定传 `true`）。

**等待与唤醒——CountLatch：**

- [rayon-core/src/latch.rs:368-382](https://github.com/rayon-rs-rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs#L368-L382)：`with_count(count, owner)` 按等待者身份选模式——在池内线程上等待用 `Stealing`（自旋边等边帮工），池外线程用 `Blocking`（睡在条件变量上）。
- [rayon-core/src/latch.rs:407-429](https://github.com/rayon-rs-rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs#L407-L429)：`CountLatch::set` 的 `fetch_sub` 计数逻辑，归零那次才置位内部锁存器并通知睡眠模块。
- [rayon-core/src/latch.rs:390-404](https://github.com/rayon-rs-rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs#L390-L404)：`wait`，按两种模式分发等待策略。

**任务载体——StackJob：**

- [rayon-core/src/job.rs:72-108](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L72-L108)：`StackJob` 定义。`broadcast_in` 把 n 个 job 放在**自己的栈上**（`jobs: Vec<_>` 是局部变量），安全性依赖「`latch.wait()` 返回前所有 job 必已执行完，栈帧不会提前弹出」。
- [rayon-core/src/job.rs:116-124](https://github.com/rayon-rs-rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L116-L124)：`StackJob::execute`——取出闭包、把返回值/panic 写进自己的结果槽、置位 latch。这一步解释了「每个线程写自己的槽」：结果是 `UnsafeCell<JobResult<R>>`，各 job 独立，天然无竞争。

**投递与消费——广播队列。** 这是本讲最关键的机制：

- [rayon-core/src/registry.rs:261-267](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L261-L267)：建池时为每个线程额外建一条 `Worker::new_fifo()` 广播队列；worker 半边集中存在 Registry 的 `broadcasts` 字段（[rayon-core/src/registry.rs:130](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L130)），stealer 半边在 [rayon-core/src/registry.rs:283-291](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L283-L291) 随 `ThreadBuilder` 发给对应工作线程，最终存进 `WorkerThread.stealer`（[rayon-core/src/registry.rs:651-652](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L651-L652)，注释明确写着 "stealer half of the worker's **broadcast** deque"）。注意：`ThreadInfo.stealer`（供他人窃取的那条，[rayon-core/src/registry.rs:629-630](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L629-L630)）连的是**本地任务 deque**，两者不是同一条队列——这是 u5-l4 提过的易混点。
- [rayon-core/src/registry.rs:463-487](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L463-L487)：`inject_broadcast`。断言任务数恰等于线程数，然后 `broadcasts.iter().zip(injected_jobs)` 逐对 `worker.push(job_ref)`——**第 i 个任务进第 i 个线程的队列**，这就是「结果按线程索引排列」「不会被别的线程偷走」的直接原因：这条队列的 stealer 半边只有线程 i 自己持有。投完对每个线程调用 `notify_worker_latch_is_set` 唤醒可能沉睡的线程。
- [rayon-core/src/registry.rs:749-763](https://github.com/rayon-rs-rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L749-L763)：`take_local_job`。先弹本地 deque，空了之后 `self.stealer.steal()`——线程从**自己的广播队列**里取任务。结合 `find_work`（[rayon-core/src/registry.rs:835-844](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L835-L844)），完整优先级链是：本地 deque → 自己的广播队列 → 窃取他人 → 全局注入队列。文档所说的「不太打扰现有工作」（not too disruptive）正是这个设计意图。

**行为验证测试：**

- [rayon-core/src/broadcast/test.rs:10-13](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/test.rs#L10-L13)：`broadcast_global`——全局池上 `broadcast(|ctx| ctx.index())` 的结果**不加排序**直接断言等于 `0..current_num_threads()`，证明返回顺序确定按线程索引排列。
- [rayon-core/src/broadcast/test.rs:28-32](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/test.rs#L28-L32)：`broadcast_pool`——7 线程用户池版本，断言等于 `0..7`。
- [rayon-core/src/broadcast/test.rs:48-52](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/test.rs#L48-L52)：`broadcast_self`——在池线程内调用 `crate::broadcast`（经由 `pool.install`），落点仍是当前池，说明「在池内调用 broadcast 广播到所在池」。
- [rayon-core/src/broadcast/test.rs:250-263](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/test.rs#L250-L263)：`broadcast_after_spawn`——先向本地 deque `spawn` 一个任务再 `broadcast`，断言 broadcast 返回后那个 spawn 必已执行，验证「广播在本地队列清空后才轮到」的优先级。

#### 4.1.4 代码实践

**实践目标**：亲手验证两条结论——`broadcast` 返回按线程索引有序的 `Vec`；`spawn_broadcast` 无返回值、需自行经 channel 汇总且无序，但两者覆盖的线程集合一致。

**操作步骤**（示例代码，新建独立 Cargo 工程，`rayon = "1"` 依赖，用 `--release` 运行）：

```rust
// src/main.rs —— 示例代码
use rayon::ThreadPoolBuilder;
use std::sync::mpsc::channel;

fn main() {
    let pool = ThreadPoolBuilder::new().num_threads(4).build().unwrap();

    // 1) 阻塞版：每个线程汇报自己的索引，同步拿回 Vec
    let v: Vec<usize> = pool.broadcast(|ctx| ctx.index());
    println!("broadcast 返回: {:?}", v);
    assert_eq!(v, vec![0, 1, 2, 3]); // 顺序由投递时的 zip 决定，确定有序

    // 2) 异步版：没有返回值，用 channel 汇总
    let (tx, rx) = channel();
    pool.spawn_broadcast(move |ctx| tx.send(ctx.index()).unwrap());

    let mut w: Vec<usize> = rx.into_iter().collect(); // 收满 4 个
    w.sort_unstable(); // 完成顺序不定，必须排序
    println!("spawn_broadcast 汇总: {:?}", w);
    assert_eq!(w, vec![0, 1, 2, 3]); // 覆盖的线程集合与 broadcast 一致
}
```

**需要观察的现象**：

1. 第一段断言通过且 `v` 直接就是 `[0, 1, 2, 3]`，无需排序。
2. 第二段若把 `sort_unstable()` 去掉直接比较，多跑几次可能偶尔失败——`spawn_broadcast` 的完成顺序不确定。
3. 把 `num_threads(4)` 改成别的值（如 7），两个断言随线程数自动成立。

**预期结果**：程序正常结束，两行输出一致。若机器可用核数少于 4 也没有关系——`num_threads(4)` 是显式指定，池一定有 4 个线程。（在 emscripten/wasm 等无线程目标上相关测试会被忽略，本实践假定桌面平台。）

#### 4.1.5 小练习与答案

**练习 1**：`broadcast` 的闭包约束是 `Sync` 而没有 `'static`，为什么它还能安全地引用调用方栈上的数据（如测试中的 `count: AtomicUsize`）？

**答案**：`broadcast` 是阻塞调用，`broadcast_in` 里 `latch.wait(current_thread)` 返回意味着 n 个任务全部执行完毕，此后才走 `into_result` 收集并返回，栈帧数据的使用期完全被函数调用期覆盖；类型上则由 `OP: Fn(...) -> R + Sync`（共享引用跨线程调用）+ `R: Send`（结果搬回调用线程）保证安全。这与 `scope` 的 `'scope` 生命周期协议同理，只是没有显式生命周期参数。

**练习 2**：为什么 n 个广播任务不会像普通任务那样被其他线程窃取？

**答案**：投递时 `inject_broadcast` 把第 i 个任务压进第 i 个线程的广播 deque，而这条 deque 的 stealer 半边只保存在第 i 个 `WorkerThread.stealer` 里；别的线程能窃取的 `ThreadInfo.stealer` 连的是本地任务 deque，不是广播队列。因此广播任务只可能被目标线程自己消费。

**练习 3**：如果调用 `broadcast` 时某线程正深陷一个长任务，会发生什么？

**答案**：不会死锁，但会等待。广播任务排在「本地 deque 清空之后、窃取之前」这个优先级点，长任务做完后线程才会执行广播副本；调用方在 `latch.wait` 里（池内则边帮工边等）等到全部 n 个副本完成才返回。这也是文档说广播「timely but not disruptive」（及时但不打扰）的原因。

### 4.2 BroadcastContext：闭包里的线程身份

#### 4.2.1 概念说明

`broadcast` 的闭包签名是 `Fn(BroadcastContext<'_>) -> R` 而不是普通的 `Fn() -> R`。这个额外参数解决两个问题：

1. **我是谁**：`index()` 返回本线程在池中的编号（`0..num_threads()`），典型用途是按线程分片初始化资源（如让线程 i 负责第 i 号槽位）。
2. **一共几个人**：`num_threads()` 返回本次广播涉及的线程总数。文档特别注明：未来 Rayon 可能允许池线程数动态变化，但该方法永远返回「本次广播实际覆盖的线程数」，所以写 `0..ctx.num_threads()` 的代码永远自洽。

`BroadcastContext` 还实现了 `Debug`，打印 `index`、`num_threads` 和 `pool_id` 三项，适合调试时用 `{:?}` 输出。

一个精巧的细节：结构体里有一个 `_marker: PhantomData<&'a mut dyn Fn()>` 字段，注释写明用途是「阻止 Send/Sync 等自动 trait」。上下文内部藏着指向 `WorkerThread` 的引用，它**必须**留在出生的线程上——如果允许把它发到别的线程，`index()` 就会张冠李戴。用 `PhantomData<&'a mut ...>` 关掉自动 trait 是 Rust 里表达「此类型线程不安全」的惯用法（`&mut` 既非 `Send` 也非 `Sync`）。

#### 4.2.2 核心流程

`BroadcastContext` 不能被用户直接构造（字段私有），唯一的创建入口是模块内部的 `with`：

```text
BroadcastContext::with(f):
    worker_thread = 读取 thread-local WORKER_THREAD_STATE
    断言非空（必须在工作线程上）
    f(BroadcastContext { worker: 该引用, ... })
```

调用时机有讲究：`broadcast_in` 与 `spawn_broadcast_in` 的任务体都在**工作线程上**执行到一半时才调用 `with`，此刻 thread-local 必然已由 `main_loop` 注册（见 u5-l3），断言因此成立。

#### 4.2.3 源码精读

- [rayon-core/src/broadcast/mod.rs:44-50](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/mod.rs#L44-L50)：结构体定义，`worker: &'a WorkerThread` 加防自动 trait 的 `PhantomData`。
- [rayon-core/src/broadcast/mod.rs:52-60](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/mod.rs#L52-L60)：`with` 构造入口，`pub(super)` 可见性保证外界无法伪造上下文。
- [rayon-core/src/broadcast/mod.rs:62-66](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/mod.rs#L62-L66)：`index()`，直接转发 `worker.index()`。
- [rayon-core/src/broadcast/mod.rs:68-78](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/mod.rs#L68-L78)：`num_threads()`，转发所属 registry 的线程数，附「未来兼容性说明」。
- [rayon-core/src/broadcast/mod.rs:81-89](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/mod.rs#L81-L89)：`Debug` 实现，输出 `index` / `num_threads` / `pool_id`。
- [rayon-core/src/registry.rs:697-704](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L697-L704)：`WorkerThread::current()` 从 thread-local 取当前线程指针——`with` 的数据来源，非工作线程上为空指针。

#### 4.2.4 代码实践

**实践目标**：观察 `BroadcastContext` 的 Debug 输出，验证 `index`/`num_threads`/`pool_id` 三字段的含义。

**操作步骤**（示例代码）：

```rust
// 在 4.1.4 的工程里追加 —— 示例代码
let pool_a = rayon::ThreadPoolBuilder::new().num_threads(3).build().unwrap();
let pool_b = rayon::ThreadPoolBuilder::new().num_threads(5).build().unwrap();

pool_a.broadcast(|ctx| println!("A: {:?}", ctx));
pool_b.broadcast(|ctx| println!("B: {:?}", ctx));
```

**需要观察的现象**：A 池打印三行，`index` 恰好取遍 0/1/2，`num_threads: 3`；B 池五行，`index` 取遍 0..5，`num_threads: 5`；两池的 `pool_id` 不同。

**预期结果**：形如 `A: BroadcastContext { index: 2, num_threads: 3, pool_id: RegistryId { addr: 0x... } }`。打印顺序（哪行先出现）不定，但 index 集合确定。`pool_id` 的具体地址每次运行不同，属正常现象（[rayon-core/src/registry.rs:608-609](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L608-L609) 可见它由 registry 地址派生）。

#### 4.2.5 小练习与答案

**练习 1**：想在闭包里区分「全局池线程」与「自建池线程」，只用 `BroadcastContext` 怎么做？

**答案**：用 `pool_id`。它是 `RegistryId`（由 registry 的地址派生），同一池内所有线程相等，不同池必不相同；配合 `Debug` 输出或先在池外记录一个基准 id 再比较即可。`index` 做不到这件事——两个池都有 index 为 0 的线程。

**练习 2**：下面的代码能编译吗？为什么？

```rust
let ctx_holder: Vec<BroadcastContext> = Vec::new();
// 想在 broadcast 里收集 ctx 再事后使用
```

**答案**：不能（`Vec<BroadcastContext>` 这一写法本身还缺生命周期参数，编译器会先要求标注 `'a`）。即使补上生命周期也无济于事：`BroadcastContext<'a>` 借用着 `'a` 的 `WorkerThread` 且 `!Send`，无法被闭包挪出线程边界收集回来；它的设计定位就是「只在广播闭包执行期间、留在本线程上」的一次性视图。`with` 的 `pub(super)` 可见性也堵死了手工构造的路径。

### 4.3 spawn_broadcast 与派发 API 全景对比

#### 4.3.1 概念说明

`spawn_broadcast` 是 `broadcast` 的异步版：把任务副本投进每个线程的广播队列后**立即返回**，不等执行。由此带来三处连锁差异：

1. **结果返回方式**：函数签名返回 `()`，拿不到任何返回值——想收集结果必须自带通道（channel / `Mutex<Vec<_>>` / 原子计数器），且各线程完成顺序不定，汇总后需要排序或做顺序无关的归并。
2. **闭包约束收紧**：`Fn(BroadcastContext<'_>) + Send + Sync + 'static`。`'static` 因为调用返回后任务可能还在跑，不能借用栈数据（通常要 `move` 闭包）；`Sync` 因为同一个闭包要被 n 个线程**共享**执行；`Send` 因为闭包要先从调用线程搬到池里。
3. **panic 处理失去「重放点」**：没有调用点可以传播 panic，于是走池的 `panic_handler`，每个线程独立捕获；没有 handler 则中止进程（对照 u6-l2 讲过的 `spawn` 同款处境）。

实现层面它与 `broadcast` 用的是同一对孪生类型：`broadcast` 用 n 个 `StackJob`（每线程独立结果槽，等完再收），`spawn_broadcast` 用**一个** `ArcJob`（引用计数任务，可执行多次，无结果槽）。`ArcJob` 的每次 `execute` 都从裸指针重建一个 `Arc` 再调用，引用计数随执行递减——天然记着「还剩几个线程没跑」。

#### 4.3.2 核心流程

```text
spawn_broadcast_in(op, registry):
    job = ArcJob(闭包: catch_unwind(op); registry.terminate())   # 单个共享任务
    for i in 0..n:
        registry.increment_terminate_count()    # 先给池加 n 个保活计数
        收集 job 的第 i 个 JobRef
    registry.inject_broadcast(job_refs)          # 投递，立即返回

# 每个工作线程稍后执行到自己那份 JobRef 时:
execute:
    Arc::from_raw 重建引用计数 -1
    registry.catch_unwind(|| BroadcastContext::with(&op))  # panic 不外泄，交 handler
    registry.terminate()                                    # (*) 归还一个保活计数
```

保活计数是安全性的关键：`ThreadPool` 被 drop 后池本可终止，但尚未执行的广播副本还攥着栈上……不，攥着**队列里**的 `JobRef`。每投一册先 `increment_terminate_count()`，每执行完一份 `terminate()` 抵扣，保证「所有副本执行完之前 registry 不会散伙」——这与 `spawn` 用任务自己持有 terminate 计数的思路一致（[rayon-core/src/registry.rs:575-585](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L575-L585) 的注释正好解释了这条规则）。

#### 4.3.3 源码精读

**入口与实现：**

- [rayon-core/src/broadcast/mod.rs:28-42](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/mod.rs#L28-L42)：`spawn_broadcast` 顶层函数，文档写明「运行在隐式全局作用域中，可能超出当前栈帧，因此不能捕获栈引用」。
- [rayon-core/src/broadcast/mod.rs:130-152](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/mod.rs#L130-L152)：`spawn_broadcast_in` 全文。注意 `ArcJob::new` 的闭包里 `registry.catch_unwind(...)` 之后紧跟 `registry.terminate(); // (*) permit registry to terminate now`，与投递侧的 `increment_terminate_count()` 一一配对。
- [rayon-core/src/thread_pool/mod.rs:362-372](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L362-L372)：`ThreadPool::spawn_broadcast` 门面。

**ArcJob——可执行多次的任务：**

- [rayon-core/src/job.rs:177-184](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L177-L184)：定义，注释点明「与 HeapJob 类似，但可转成多个 JobRef、被调用多次」。
- [rayon-core/src/job.rs:201-207](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L201-L207)：`as_static_job_ref`——投 n 份时并不复制任务数据，只是克隆 n 次 `Arc` 指针并擦除类型。
- [rayon-core/src/job.rs:210-219](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L210-L219)：`ArcJob::execute`——`Arc::from_raw` 重建强引用（计数 -1）后调用闭包，最后一次执行后 `Arc` 归零、任务自动释放。

**panic 与保活的支撑设施：**

- [rayon-core/src/registry.rs:373-382](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L373-L382)：`catch_unwind`——捕获 panic，有 `panic_handler` 就交给它，否则带着 `AbortIfPanic` 守卫直接中止。
- [rayon-core/src/registry.rs:585-589](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L585-L589) 与 [rayon-core/src/registry.rs:594-600](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L594-L600)：`increment_terminate_count` / `terminate` 这对计数操作。

**作用域版对照——可以借数据：**

- [rayon-core/src/scope/mod.rs:555-572](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L555-L572)：`Scope::spawn_broadcast`。约束放宽为 `Send + Sync + 'scope`——作用域保证「scope 返回前所有副本必已完成」，于是又能借栈数据了；且闭包额外收到 `&Scope` 参数，可在广播体内继续向作用域 spawn 任务（测试 [rayon-core/src/scope/test.rs:558-571](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/test.rs#L558-L571) 演示了嵌套用法）。

**行为验证测试：**

- [rayon-core/src/broadcast/test.rs:17-24](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/test.rs#L17-L24)：`spawn_broadcast_global`——channel 收集后 `sort_unstable()` 再断言 `0..current_num_threads()`，与 `broadcast_global` 的「免排序」形成鲜明对照。
- [rayon-core/src/broadcast/test.rs:234-247](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/test.rs#L234-L247)：`broadcast_after_spawn_broadcast`——先 `spawn_broadcast` 再 `broadcast`（空操作），断言后者返回时前者的副本已全数执行。因为每条广播队列是 FIFO 且两类广播走同一条队列，阻塞版起到了「屏障」的作用。
- [rayon-core/src/broadcast/test.rs:161-178](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/test.rs#L161-L178)：`spawn_broadcast_panic_one`——7 线程中仅 index 3 panic：所有副本照常执行（计数仍为 7），panic 恰好一次送达 `panic_handler`。对照 [rayon-core/src/broadcast/test.rs:144-157](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/test.rs#L144-L157) 的 `broadcast_panic_one`：等全部完成后在调用点重放恰好一个 panic。

**派发 API 全景对比表**（综合本单元与前几讲）：

| API | 每线程执行? | 阻塞? | 返回结果 | 闭包约束 | 可借栈数据? |
| --- | --- | --- | --- | --- | --- |
| `join(a, b)` | 否（2 个任务） | 是 | `(A, B)` | `FnOnce + Send` | 是 |
| `broadcast(op)` | 是（n 份副本） | 是 | `Vec<R>` 按线程索引有序 | `Fn(BroadcastContext) -> R + Sync` | 是 |
| `spawn(op)` | 否（1 个任务） | 否 | `()` | `FnOnce + Send + 'static` | 否 |
| `spawn_broadcast(op)` | 是（n 份副本） | 否 | `()`（channel 自理） | `Fn(BroadcastContext) + Send + Sync + 'static` | 否 |
| `Scope::spawn_broadcast(op)` | 是（n 份副本） | 否（scope 返回时等齐） | `()`（channel 自理） | `Fn(&Scope, BroadcastContext) + Send + Sync + 'scope` | 是（借 `'scope`） |

选型口诀：**要结果且要顺序 → `broadcast`；异步刷每线程状态 → `spawn_broadcast`；异步还要借数据 → 进 `scope` 用 `Scope::spawn_broadcast`**。

#### 4.3.4 代码实践

**实践目标**：用实验验证「广播队列 FIFO + 阻塞版可当屏障」的优先级语义。

**操作步骤**（示例代码，接 4.1.4 的工程）：

```rust
// 示例代码：验证 broadcast 的屏障作用
use std::sync::mpsc::channel;

let (tx, rx) = channel();
let n = pool.current_num_threads();

// 1) 先异步广播：让每个线程发一个"完成信号"
pool.spawn_broadcast(move |ctx| {
    std::thread::sleep(std::time::Duration::from_millis(50 * ctx.index() as u64));
    tx.send(ctx.index()).unwrap();
});

// 2) 紧跟一个阻塞广播当屏障（闭包什么都不做）
pool.broadcast(|_| {});

// 3) 屏障返回后，无需等待即可收满 n 个信号
let mut got: Vec<usize> = rx.try_iter().collect();
got.sort_unstable();
println!("屏障后立刻收到: {:?}", got);
assert_eq!(got.len(), n);
```

**需要观察的现象**：尽管每个副本人为 sleep 了不同的时长（0/50/100/...ms），`broadcast(|_| {})` 返回后 `try_iter()`（非阻塞！）已经能一次收齐 n 个信号。

**预期结果**：断言通过。原因：每线程的广播队列是 FIFO，`spawn_broadcast` 的副本排在 `broadcast` 的副本之前；后者等齐 n 份才返回，隐含前者也全部完成——这正是测试 `broadcast_after_spawn_broadcast` 论证的协议。（多跑几次确认稳定；若把第 2 步删去，`try_iter` 大概率收不满，程序行为变为「待本地验证」的对照实验。）

#### 4.3.5 小练习与答案

**练习 1**：`spawn_broadcast` 为什么必须 `Sync`，而 `spawn` 只需要 `Send`？

**答案**：`spawn` 的闭包整体搬进一个任务，任何时刻只有一个线程拥有它，`Send` 足够。`spawn_broadcast` 把**同一个**闭包包进 `Arc`，n 个线程各持一份引用**同时共享**调用——共享调用要求 `Sync`（`Arc<T>` 只有 `T: Sync` 才是 `Send + Sync`，任务才能跨线程入队）。

**练习 2**：`spawn_broadcast_in` 里为什么要在投递前 `increment_terminate_count()` n 次、每个副本执行完再 `terminate()` 一次？删掉会出什么问题？

**答案**：这是给 registry「保活」。用户 `drop(pool)` 后 terminate 计数可能提前归零、工作线程陆续退出，但队列里可能还压着未执行的广播副本——此刻 `JobRef` 指向的 `ArcJob` 与闭包数据仍需存活。每投一份先加一、每执行一份减一，保证最后一个副本执行完之前 registry 不会被拆除。删掉的话，池销毁后残留副本将引用已释放的 registry/任务内存（use-after-free），或者副本根本没机会执行就被丢弃。

**练习 3**：7 线程池上 `spawn_broadcast` 的闭包里有 4 个线程 panic（模仿 `spawn_broadcast_panic_many`），`panic_handler` 会收到几次？池还能继续用吗？

**答案**：4 次——panic 是每副本独立捕获、独立上报的，互不屏蔽；这也是它与 `broadcast`（只传播恰好一个）的差异。池不受影响：`catch_unwind` 挡住了展开，其余 3 个副本照常完成，池随后仍可接受新任务（对照测试 [rayon-core/src/broadcast/test.rs:199-216](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/test.rs#L199-L216) 断言 `panic_rx` 收到 4 次）。

## 5. 综合实践

**任务：双池相互广播，验证「覆盖数 = 两池线程数之积」。**

这个实验模仿官方测试 `broadcast_mutual`（[rayon-core/src/broadcast/test.rs:68-80](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/test.rs#L68-L80)）并扩展出异步版，综合本讲全部知识点：`ThreadPoolBuilder` 建池、`broadcast` 嵌套调用、`BroadcastContext`、`spawn_broadcast` 的 channel 汇总与排序。

```rust
// 示例代码：src/main.rs
use rayon::ThreadPoolBuilder;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc::channel;
use std::sync::Arc;

fn main() {
    // 第一部分：阻塞版相互广播，计数 3 * 7 = 21
    let count = AtomicUsize::new(0);
    let pool1 = ThreadPoolBuilder::new().num_threads(3).build().unwrap();
    let pool2 = ThreadPoolBuilder::new().num_threads(7).build().unwrap();

    pool1.install(|| {
        // 在 pool1 的线程上，向 pool2 的 7 个线程广播；
        // 每个副本内又向 pool1 的 3 个线程广播并计数
        pool2.broadcast(|_| {
            pool1.broadcast(|_| {
                count.fetch_add(1, Ordering::Relaxed);
            })
        })
    });
    let total_blocking = count.into_inner(); // into_inner 会消费 count，先存进局部变量
    println!("阻塞版计数 = {}", total_blocking);
    assert_eq!(total_blocking, 21);

    // 第二部分：异步版，用 channel 验证同样的 21 次覆盖
    let (tx, rx) = channel();
    let pool1 = Arc::new(ThreadPoolBuilder::new().num_threads(3).build().unwrap());
    let pool2 = ThreadPoolBuilder::new().num_threads(7).build().unwrap();

    pool1.spawn({
        let pool1 = Arc::clone(&pool1);
        move || {
            pool2.spawn_broadcast(move |_| {
                let tx = tx.clone();
                pool1.spawn_broadcast(move |_| tx.send(()).unwrap());
            })
        }
    });
    let total = rx.into_iter().count(); // 收满 21 个才返回
    println!("异步版计数 = {}", total);
    assert_eq!(total, 21);
}
```

**验收要点**：

1. 两部分都输出 21（= 3 × 7）：外层广播的每个副本都在某线程上执行，其内部的广播又覆盖另一池全部线程。
2. 注意第二部分必须把 `pool1` 包进 `Arc` 再 clone 进闭包——`spawn_broadcast` 的 `'static` 约束不允许借用栈上的 pool1，这正是 4.3 讲的约束在现实里的样子。
3. 嵌套结构没有死锁：`broadcast` 等待期间池内线程会「边等边帮工」（`CountLatch` 的 Stealing 模式），内外两层广播作用于**不同的池**，各自队列独立推进。
4. 想加大难度：在内外闭包里各加 `thread::sleep`（模仿 `broadcast_mutual_sleepy`，[rayon-core/src/broadcast/test.rs:102-117](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/test.rs#L102-L117)），让部分线程陷入睡眠再被广播唤醒，验证 u5-l5 的睡眠/唤醒协议在广播路径上也成立。断言数值不变。

## 6. 本讲小结

- `broadcast` / `spawn_broadcast` 把闭包副本投递到池中**每个**工作线程各执行一次；核心设施是 Registry 为每线程单独建的 FIFO 广播队列，其 stealer 半边只归该线程所有，因此副本必定落在目标线程、不被窃取。
- 广播任务在「本地 deque 清空之后、窃取他人之前」这个优先级点执行——及时但不打扰；每条广播队列 FIFO 还使阻塞版 `broadcast` 天然充当先前广播的屏障。
- `broadcast` = n 个 `StackJob` + 一个计数为 n 的 `CountLatch`：等齐后按线程索引顺序收集出有序 `Vec<R>`；闭包只需 `Sync`，可借调用方栈数据。
- `spawn_broadcast` = 1 个 `ArcJob` 生成 n 个 `JobRef`：立即返回无结果，需 channel 自行汇总（完成顺序不定）；闭包要 `Send + Sync + 'static`，靠「投递前加 n 个 terminate 计数、每副本执行完减一」给池保活。
- `BroadcastContext` 提供 `index()`/`num_threads()`（以及 Debug 中的 `pool_id`），借 `PhantomData<&mut dyn Fn()>` 关掉自动 trait，保证上下文留在出生线程。
- panic 语义分家：`broadcast` 等全部完成后在调用点重放恰好一个；`spawn_broadcast` 每副本独立走 `panic_handler`，几个线程 panic 就上报几次。

## 7. 下一步学习建议

- 下一讲 **u6-l4「panic 传播与 unwind 安全」**：本讲多次出现的 `halt_unwinding` / `catch_unwind` / `AbortIfPanic` 将在那里系统展开，包括 `JobResult::Panic` 的搬运与重放、排序等基础设施的 panic 安全测试。
- 若想先横向扩展，可跳到 **u7-l1「ThreadPoolBuilder」** 深入建池配置（本讲的 `num_threads` 只是其一），再回看 u7-l3 的 `current_thread_has_pending_tasks`——它与广播优先级共同构成防饿死协议。
- 源码阅读建议：重读 [rayon-core/src/registry.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs) 中 `take_local_job` / `find_work` / `inject_broadcast` 三处，把「三条队列 + 广播队列」的完整图景串成一张自己的调度草图；再对照 [rayon-core/src/scope/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs) 中 `ScopeBase::inject_broadcast`，看作用域版如何复用同一条投递通道。
