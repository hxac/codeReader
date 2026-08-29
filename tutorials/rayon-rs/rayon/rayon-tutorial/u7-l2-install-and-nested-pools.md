# install 与多线程池协作

## 1. 本讲目标

上一讲我们学会了用 `ThreadPoolBuilder` 配置并创建自定义线程池。本讲回答一个紧接着的问题：**池建好之后，任务怎么送进去、结果怎么拿回来、多个池之间如何相处**。

学完本讲，你应该能够：

1. 说出 `ThreadPool::install` 的完整执行协议：它按「调用线程是谁」分成三条路径，并且**同步等待**闭包结果返回。
2. 掌握一个关键事实：**不同线程池之间任务不互相窃取**，每个 `Registry` 是一座孤岛。
3. 区分两类等待——「帮工式等待」（install/join/scope）与「纯阻塞等待」（channel/mutex/condvar），理解跨池死锁的真正成因与规避方法。
4. 制定嵌套并行的策略：什么时候该分池、什么时候该合并成一个池。

## 2. 前置知识

本讲建立在前面几讲的概念之上，先用两段话把它们串起来。

**池与注册表**：`ThreadPool` 只是一个薄壳，真正拥有线程、队列和调度循环的是 `Registry`（u5-l3、u5-l4）。每个 `Registry` 有自己的本地 deque 数组、全局注入队列（injector）和广播队列；工作窃取只发生在**同一个** Registry 内部——线程找活的优先链是「本地队列 → 窃取同伴 → 全局注入队列」。`Registry` 之间没有共享队列，也没有指向彼此 stealer 的引用，这就是「池间隔离」的物理基础。

**任务与锁存器**：闭包要进池，得先被包装成任务（u5-l2）。`StackJob` 是挂在**调用者栈帧**上的任务，零堆分配，其安全性依赖一条不变量：*等待它的函数在返回之前必然等它执行完毕*。任务完成时置位 Latch（锁存器）唤醒等待方；`SpinLatch` 用于池内自旋等待，`LockLatch` 用于池外线程的阻塞等待。等待方式由**等待者的身份**决定——这条规则在 `install` 里体现得最完整，正是本讲主线。

另外一个容易混淆的点先澄清：`rayon::spawn`（顶层函数）把任务丢进**当前线程所在的池**，而 `pool.spawn`（`ThreadPool` 的方法）把任务丢进**指定的池**——后者正是通过持有该池的 `Arc<Registry>` 实现的定向投递。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `rayon-core/src/thread_pool/mod.rs` | `ThreadPool` 类型定义；`install` 及其全家（`join`/`scope`/`spawn`/`yield_now` 等定池版本） |
| `rayon-core/src/registry.rs` | `in_worker` 三分支调度、`in_worker_cold`/`in_worker_cross` 两条慢路径、`inject` 注入、`wait_until` 帮工等待循环、`find_work` 找活优先链 |
| `rayon-core/src/latch.rs` | `SpinLatch::cross`——专为跨池等待构造的锁存器 |
| `rayon-core/src/thread_pool/test.rs` | 单元测试：`self_install`、`mutual_install`、`mutual_install_sleepy`、`nested_scopes`、`in_place_scope_no_deadlock` 等 |
| `tests/cross-pool.rs` | 集成测试 `cross_pool_busy`：并行迭代器中逐元素跨池 install |

## 4. 核心概念与源码讲解

### 4.1 install 语义：把闭包送进指定池并同步等结果

#### 4.1.1 概念说明

`ThreadPool::install` 回答的问题是：「我希望这段计算**确定**发生在我指定的池里，并且我要在这里等到它的返回值」。

这与顶层函数形成对照：`rayon::join`、`rayon::scope` 等顶层 API 运行在「当前线程恰好所在的池」——在主线程上调用就是全局池，在某个池的工作线程内调用就是这个池。这种「隐式跟随」很方便，但当你明确想让计算落到某个池（例如那个池有专门的线程数、线程优先级或栈大小配置）时，就需要 `install` 显式指定。

两条重要语义：

1. **同步**：`install` 返回 `R`（闭包的返回值），调用点一直等到闭包执行完毕；闭包 panic 也会在调用点重放。
2. **上下文切换的传染性**：`install` 内部再调用任何 rayon 操作（`join`、`scope`、并行迭代器），都会运行在这个池里——因为闭包 physically 在池的工作线程上执行，「当前池」随之改变。

一个容易忽略的警告（文档中明确写出）：闭包运行在池线程上，**访问不到调用线程的 thread-local 数据**。

#### 4.1.2 核心流程

`install` 的本体只有一行，真正逻辑在 `Registry::in_worker`。它按「当前线程是谁」分三条路径：

```text
install(op):
    当前线程 = WorkerThread::current()
    ① 不是任何池的工作线程（如主线程）   → in_worker_cold
         把 op 包成 StackJob，注入目标池的全局队列
         当前线程睡在 LockLatch 上（真阻塞，不消耗 CPU 帮工）
    ② 是另一个池的工作线程（跨池）       → in_worker_cross
         把 op 包成 StackJob，注入目标池的全局队列
         用 SpinLatch::cross 等待；等待期间 wait_until 循环
         帮「原池」干活（本地队列/窃取/原池注入队列）
    ③ 就是本池的工作线程                → 就地执行 op（零开销）
    最后：取回 StackJob 的结果；若 op panic，在调用线程重放
```

注意路径②的精妙之处：等待期间线程帮的是**原池**（自己所属的池）干活，而不是目标池——它没有目标池的 stealer，也不需要：目标池的线程自然会从自己的注入队列领走这个 job。`StackJob` 的内存安全依赖「install 返回前 job 必然完成」，三条路径都在 `job.into_result()` 之前有等待语句，不变量成立。

#### 4.1.3 源码精读

`install` 的定义——本体只是把闭包转交给 `registry.in_worker`，并丢弃框架传入的上下文参数：

- [rayon-core/src/thread_pool/mod.rs:137-143](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L137-L143)：`install<OP, R>` 要求 `OP: FnOnce() -> R + Send`、`R: Send`（闭包和结果都要能跨线程送到池里），实现为 `self.registry.in_worker(|_, _| op())`。

`in_worker` 的三分支路由——本讲最核心的一段代码：

- [rayon-core/src/registry.rs:494-512](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L494-L512)：先读 thread-local 的 `WorkerThread::current()`；为 null 走 `in_worker_cold`（非池线程）；`registry().id() != self.id()` 走 `in_worker_cross`（跨池）；否则就地执行 `op(&*worker_thread, false)`。第二个参数 `injected` 标记闭包是否被注入执行（就地时为 false）。

路径①——非池线程的阻塞等待：

- [rayon-core/src/registry.rs:515-538](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L515-L538)：`in_worker_cold` 用一个 **thread-local 的 `LockLatch`**（可重置复用）构造 `StackJob`，`self.inject(job.as_job_ref())` 注入目标池，然后 `job.latch.wait_and_reset()` 让当前线程睡在条件变量上，醒来后 `job.into_result()`。

路径②——跨池等待，本讲的伏笔所在：

- [rayon-core/src/registry.rs:541-563](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L541-L563)：`in_worker_cross` 用 `SpinLatch::cross(current_thread)` 构造 `StackJob` 注入目标池，然后 `current_thread.wait_until(&job.latch)`——**当前线程没有睡死**，而是在帮工循环里边等边执行原池的任务。
- [rayon-core/src/latch.rs:170-179](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs#L170-L179)：`SpinLatch::cross` 与普通 `SpinLatch::new` 唯一区别是 `cross: true` 标志——置位时要通知的线程属于另一个 registry，必须保证那个 registry 还活着才能安全调用唤醒，注释写明了这一点。

注入的目标是全局注入队列，不是任何人的本地队列：

- [rayon-core/src/registry.rs:426-442](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L426-L442)：`inject` 把 `JobRef` 压进 `self.injected_jobs` 并调用 `sleep.new_injected_jobs(1, queue_was_empty)` 唤醒空闲线程（u5-l5 的睡眠协议在这里被触发）。

panic 语义有专门测试保证：

- [rayon-core/src/thread_pool/test.rs:10-16](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/test.rs#L10-L16)：`panic_propagate` 用 `#[should_panic(expected = "Hello, world!")]` 验证 install 的闭包 panic 会在调用线程原样重放。

#### 4.1.4 代码实践

**实践目标**：亲眼确认三条路径的存在——特别是「同池 install 零开销、就地执行」这一条。

**操作步骤**（示例工程，依赖 `rayon = "1"`，以下为示例代码）：

```rust
use rayon::{ThreadPoolBuilder, current_thread_index};

fn main() {
    let pool = ThreadPoolBuilder::new().num_threads(1).build().unwrap();

    // 路径①：主线程不是池线程，闭包被注入、由池线程执行
    let (host, val) = pool.install(|| {
        (current_thread_index(), 42)
    });
    println!("外层 install: 线程索引={host:?}, 值={val}");
    // 预期: 线程索引=Some(0), 值=42

    // 路径③：已在池内再 install 同一个池 —— 就地执行，不会死锁
    let nested = pool.install(|| pool.install(|| true));
    println!("嵌套同池 install: {nested}");
    // 预期: true（这正是单元测试 self_install 的断言）
}
```

**需要观察的现象**：两次输出都正常返回；嵌套 install 没有卡死——如果内层 install 是「把任务入队然后死等」，单线程池里将永远没人执行它。

**预期结果**：`外层 install: 线程索引=Some(0), 值=42` 与 `嵌套同池 install: true`。运行命令 `cargo run --release`。结果待本地验证（行为由上述源码与单元测试保证，具体打印格式以本地运行为准）。

#### 4.1.5 小练习与答案

**练习 1**：`install` 的闭包约束是 `FnOnce() -> R + Send`，为什么不需要 `'static`（对比 `ThreadPool::spawn` 的 `+ 'static`）？

**答案**：`install` 是同步调用，在返回之前必然等闭包执行完毕，因此闭包可以安全地借用调用方栈上的数据——这与 `join`、`scope` 同属「等待点写在签名里」的 API。`spawn` 是 fire-and-forget，调用立即返回、任务可能晚于当前栈帧存活，所以必须 `'static`（u6-l2 已建立这一对比）。

**练习 2**：为什么 `in_worker_cold` 里的 `LockLatch` 要用 `thread_local!` 定义并调用 `wait_and_reset()`？

**答案**：`install` 可能被同一个非池线程嵌套或反复调用，thread-local 的锁存器可以被重置复用，避免每次调用都新建同步原语；`wait_and_reset` 在醒来后把状态复位，供下一次等待使用。

**练习 3**：在 `pool_a` 的工作线程上调用 `pool_a.install(...)` 走哪条路径？在 `pool_b` 的工作线程上调用 `pool_a.install(...)` 呢？

**答案**：前者走第三分支，就地执行（`injected = false`）；后者因 `registry().id() != self.id()` 走 `in_worker_cross`，闭包被注入 `pool_a` 的全局队列，`pool_b` 的线程在帮工等待中让出执行权。

### 4.2 池间隔离：任务不跨池窃取

#### 4.2.1 概念说明

「不同池之间任务不互相窃取」不是文档里的君子协定，而是数据结构的必然：每个 `Registry` 拥有独立的线程集合、独立的本地 deque 数组（`ThreadInfo`）和独立的全局注入队列；窃取循环遍历的 stealer 列表只包含**本 registry** 的线程（u5-l4 的 `find_work` 环形扫描）。跨池投递任务的唯一通道是对方 registry 的注入队列——`install`、`pool.spawn`、`pool.broadcast` 都走这条路。

隔离带来两个直接后果：

1. **一个池繁忙或卡死，不会拖慢另一个池的调度**——它们不共享任何队列锁。
2. **一个池也救不了另一个池**——如果目标池的全部线程都被占住，注入的任务就只能排队。这正是下一模块死锁分析的出发点。

区分「属于哪个池」靠 `RegistryId`：一个普通的可比较整数标识（[rayon-core/src/registry.rs:608-609](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L608-L609) 定义，`in_worker` 用它做分支判断）。`ThreadPool::current_thread_index` 的文档也提到：不同池的线程可能共用相同的索引号——索引只在池内有意义。

#### 4.2.2 核心流程

把本讲涉及的跨池交互画成一张图：

```text
      主线程(非池线程)                 pool_a 工作线程                pool_b 工作线程
            │                               │                            │
 pool_b.install(op)                  pool_b.install(op)                 │
            │                               │                            │
            ▼                               ▼                            ▼
     in_worker_cold                  in_worker_cross              (本地任务照常跑)
     op 包成 StackJob                op 包成 StackJob
     注入 pool_b.injected_jobs ──►   注入 pool_b.injected_jobs ──► 从注入队列领走 op 并执行
     睡在 LockLatch 上                帮 pool_a 干活(本地/窃取/a的注入队列)
            ▲                               │                            │
            └────────── Latch set ───────────┴────────────────────────────┘
                      (op 完成，唤醒等待方，各自取回结果)
```

关键点：等待方醒来后 `job.into_result()`；对跨池路径，置位通知要穿过两个 registry（这就是 `SpinLatch::cross` 存在的原因）。

#### 4.2.3 源码精读

- [rayon-core/src/thread_pool/test.rs:156-172](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/test.rs#L156-L172)：`mutual_install`——两个单线程池互相 install 形成依赖环（pool1 → pool2 → pool1），注释直言「如果跨池 install 是阻塞等待，就没有线程能运行最内层闭包了」。测试通过，证明跨池 install 是帮工式的。
- [rayon-core/src/thread_pool/test.rs:176-200](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/test.rs#L176-L200)：`mutual_install_sleepy` 在环中插入两段 1 秒睡眠，确保两个池都有机会落入 u5-l5 的睡眠状态机，验证睡眠唤醒路径下互安装依然不死锁。
- [tests/cross-pool.rs:4-22](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/cross-pool.rs#L4-L22)：`cross_pool_busy`——在 pool1 的并行迭代器里对 100 个元素逐个 `pool2.install`。注释解释了它真正测试的角落：pool1 的线程在等待期间继续处理迭代器的其他 item；有时它**还没睡下就看到 latch 已置位**，随后会直接 drop 掉那个栈上的 job——因此 Latch 的实现不能假设「set 之后 job 还活着」。
- [rayon-core/src/registry.rs:780-817](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L780-L817)：`wait_until_cold`——帮工等待的循环体：先查本地队列，再进睡眠状态机并反复 `find_work`，找到就 `execute` 然后回到外层循环重查 latch。
- [rayon-core/src/registry.rs:835-844](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L835-L844)：`find_work` 的三级优先链，末级正是 `pop_injected_job()`——这解释了 4.3 修复方案为什么成立：**跨池等待的线程会消化自己池的注入队列**。

#### 4.2.4 代码实践

**实践目标**：验证「任务不跨池窃取」——两个池的任务始终各自由本池线程执行。

**操作步骤**（示例代码）：

```rust
use rayon::ThreadPoolBuilder;
use std::collections::HashSet;
use std::sync::mpsc::channel;

fn main() {
    let p1 = ThreadPoolBuilder::new().num_threads(2)
        .thread_name(|i| format!("pool1-{i}")).build().unwrap();
    let p2 = ThreadPoolBuilder::new().num_threads(2)
        .thread_name(|i| format!("pool2-{i}")).build().unwrap();

    let (tx, rx) = channel();
    // 各自 spawn 20 个任务，回报执行线程名
    for _ in 0..20 {
        let tx = tx.clone();
        p1.spawn(move || { tx.send(std::thread::current().name().unwrap().to_string()).unwrap(); });
        let tx = tx.clone();
        p2.spawn(move || { tx.send(std::thread::current().name().unwrap().to_string()).unwrap(); });
    }
    drop(tx);
    let names: HashSet<_> = rx.iter().collect();
    println!("执行线程集合: {:?}", names);
}
```

**需要观察的现象**：集合中只出现 `pool1-0`、`pool1-1`、`pool2-0`、`pool2-1` 四个名字；pool1 的任务从不落在 pool2 的线程上，反之亦然。

**预期结果**：恰好这四个线程名；`p1` 与 `p2` 的任务各自归属本池。注意 `pool.spawn` 是异步的，主线程需等 channel 关闭（`drop(tx)` + `rx.iter()`）收集完再退出，否则可能漏观察。结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`mutual_install` 里如果跨池 install 改为「把闭包入队后原地自旋且不执行任何任务」，会发生什么？

**答案**：pool1 的唯一线程等 pool2 的结果，pool2 的唯一线程等 pool1 的结果，两个线程互相占住对方需要的执行资源，程序永久卡死。测试通过的事实说明真实实现走的是 `in_worker_cross` 的帮工路径。

**练习 2**：`cross_pool_busy` 的注释说「latch 内部不能假设 job 在 set 之后仍然存活」。结合 `StackJob` 的存储位置解释这句话。

**答案**：`StackJob` 活在等待方的栈帧上。等待方可能在自己还没进入睡眠/帮工循环时就探测到 latch 已置位，随即返回并销毁栈帧——此时 job 已被 drop，而置位通知（`SpinLatch::cross` 的唤醒路径）可能还在另一个 registry 的线程上执行，因此通知代码只能触碰 registry 级的数据（如 sleep 状态），不能触碰 job 本身。

**练习 3**：为什么 `ThreadPool::current_thread_index` 返回 `Option<usize>` 而不是 `usize`？

**答案**：因为它先检查「当前线程是否属于**这个**池」——非池线程或属于其他池都返回 `None`（见 [rayon-core/src/thread_pool/mod.rs:232-235](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L232-L235)），索引是池内概念，跨池不唯一。

### 4.3 跨池死锁案例：成因与修复

#### 4.3.1 概念说明

既然 `mutual_install` 证明了跨池 install 互等不死锁，那死锁从哪里来？答案是：**把「不帮工的阻塞原语」放进池的工作线程**。

rayon 的等待 API（`install`/`join`/`scope`/`broadcast`）在等待时会让线程继续执行池内任务，是「活」的等待；而标准库的 `mpsc::Receiver::recv`、`Mutex::lock`、`Condvar::wait`、甚至 `thread::sleep` 是「死」的等待——线程被操作系统挂起，**不再消化任何队列**。

死锁的通用形状是一个资源依赖环，环上至少一条边由「死的等待」构成：

```text
pool_a 的唯一线程 ──(死等: rx.recv)──► 等 channel 消息
        ▲                                      │
        │                              消息由 pool_b 的任务发出
  该任务需要 pool_a 执行                          │
  一个注入的 job 才能完成                         ▼
        └──────────────────────────── pool_b 的任务(等 pool_a 的线程空出来)
```

只要池_a 的唯一线程卡在 recv 上，它永远不会去注入队列领 job，环就闭合了。注意死锁的**必要条件**是目标池的全部线程被占满（单线程池最容易踩中；多线程池只是让概率降低，不是消除风险——所有线程都可能同时阻塞）。

#### 4.3.2 核心流程

构造与修复的对照：

```text
【反例: 死锁】
主线程: pool_a.install(closure_a)          # closure_a 占住 pool_a 唯一线程
closure_a:
    pool_b.spawn(Tb)                        # Tb 进 pool_b
    rx.recv()                               # ★ 死等，不帮工
Tb (pool_b 线程):
    pool_a.install(|| 42)                   # job 注入 pool_a 队列，无人执行 → Tb 卡住
    tx.send(...)                            # 永远到不了

【修复: 把死等换成帮工等待】
主线程: pool_a.install(|| {
    pool_b.install(|| {
        pool_a.install(|| 42)               # 注入后，pool_a 线程在帮工循环中
    })                                      # 经 find_work 第三级 pop_injected_job 领走它
})                                          # —— 正是 mutual_install 的形状
```

修复的本质：把环上唯一的「死等」边替换成「帮工等待」边。等待中的 pool_a 线程会执行 `find_work()`（本地 → 窃取 → **注入队列**），于是自己注入给自己的 job 被自己消化，环被打破。

#### 4.3.3 源码精读

- [tests/cross-pool.rs:11-21](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/cross-pool.rs#L11-L21)：`cross_pool_busy` 的主体——`pool1.install` 内跑并行迭代器，每个元素 `pool2.install(move || i)` 再 `sum`。pool1 的线程每次跨池等待时都在帮工处理迭代器的其他 item，所以 100 个串行依赖的任务依然能全部完成，最终断言 `sum == n*(n+1)/2`。
- [rayon-core/src/thread_pool/test.rs:147-152](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/test.rs#L147-L152)：`self_install`——「如果内层 install 阻塞，就没人能运行它」；同池嵌套走第三分支就地执行，断言返回 `true`。
- [rayon-core/src/thread_pool/test.rs:354-366](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/test.rs#L354-L366)：`in_place_scope_no_deadlock`——同池「死等」反例的官方版本：单线程池上普通 `scope` 的 body 会占住唯一 worker 再 `recv`，body 内 spawn 的任务永远无法运行；`in_place_scope` 让 body 留在**调用线程**上执行，worker 空出来跑 spawn 的任务，测试通过。注释原文点破了这一对比。
- [rayon-core/src/thread_pool/mod.rs:78-113](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L78-L113)：`install` 文档的「Warning: execution order」一节——单线程全局池上，`install` 内的隐式让步允许两个 `do_it` 实例交错输出 `one one two two`。它从另一个角度说明同一件事：**跨池 install 的等待不是原地踏步，而是把当前线程借出去**。

#### 4.3.4 代码实践（本讲主线实践）

**实践目标**：亲手制造一个跨池死锁，解释成因，再用 install 改成同步等待版本使程序正确结束。

**操作步骤**：

第一步，在示例工程中写入反例（示例代码）：

```rust
// src/bin/deadlock_bad.rs —— 运行: cargo run --release --bin deadlock_bad
use rayon::ThreadPoolBuilder;
use std::sync::mpsc::channel;

fn main() {
    let pool_a = ThreadPoolBuilder::new().num_threads(1).build().unwrap();
    let pool_b = ThreadPoolBuilder::new().num_threads(1).build().unwrap();

    let (tx, rx) = channel();
    let result = pool_a.install(|| {
        // ① 向 pool_b 派发 Tb（fire-and-forget）
        pool_b.spawn(|| {
            // ② Tb 需要回到 pool_a 执行一个闭包：job 注入 pool_a 的队列
            let v = pool_a.install(|| 42);
            tx.send(v).unwrap();
        });
        // ③ pool_a 的唯一线程在这里纯阻塞等待——不帮工
        rx.recv().unwrap()
    });
    println!("result = {result}");
}
```

用超时保护运行：`timeout 10 cargo run --release --bin deadlock_bad; echo "exit=$?"`。

第二步，分析成因（对照 4.3.2 的依赖环）：③ 占住 pool_a 唯一线程且不消化队列 → ② 注入的 job 无人执行 → Tb 卡在 install → `tx.send` 永不发生 → ③ 永不返回。

第三步，写修复版（示例代码）：

```rust
// src/bin/deadlock_fixed.rs —— 运行: cargo run --release --bin deadlock_fixed
use rayon::ThreadPoolBuilder;

fn main() {
    let pool_a = ThreadPoolBuilder::new().num_threads(1).build().unwrap();
    let pool_b = ThreadPoolBuilder::new().num_threads(1).build().unwrap();

    // 把 spawn + recv（死等）换成嵌套 install（帮工等待）
    let result = pool_a.install(|| {
        pool_b.install(|| {
            pool_a.install(|| 42)
        })
    });
    println!("result = {result}");
}
```

**需要观察的现象**：反例在 10 秒后被 `timeout` 杀掉（exit=124），无输出；修复版立即打印 `result = 42`。

**预期结果**：如上。修复版即 `mutual_install` 的翻版（把断言换成打印）；其正确性由 `in_worker_cross` 的 `wait_until` 帮工循环（[rayon-core/src/registry.rs:560](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L560)）与 `find_work` 的注入队列末端（[rayon-core/src/registry.rs:843](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L843)）共同保证。结果待本地验证。

**延伸思考**：反例还有一个修法——把 `pool_b.spawn` + `rx.recv()` 整体换成 `pool_b.install(|| ...)`（去掉回环），或参考 `in_place_scope_no_deadlock` 用 `pool_a.in_place_scope` 让 body 留在主线程。核心原则只有一条：**池的工作线程上不要出现不帮工的阻塞**。

#### 4.3.5 小练习与答案

**练习 1**：把反例中两个池都改成 `num_threads(2)`，程序还会死锁吗？

**答案**：不确定，且大概率「看起来能跑」——pool_a 的另一个空闲线程可以领走注入的 job，环被偶然打破。但只要两个线程同时被这类死等占用（例如迭代器里成千上万个元素都这么做），死锁仍会发生。多线程只是降低概率，不是正确性保证。

**练习 2**：在反例的 ③ 处把 `rx.recv()` 换成 `thread::sleep(Duration::from_secs(5))`（并把 channel 换成池 b 里普通打印），会死锁吗？

**答案**：不会永久死锁——`sleep` 不依赖 Tb 完成，5 秒后 ③ 返回，pool_a 线程恢复消化队列，Tb 随即完成。但这是「白丢 5 秒并行度」的反模式：睡眠期间该线程不帮工，同池任务全部停滞。判断标准不是「会不会卡死」，而是「等待期间是否还干活」。

**练习 3**：为什么说 `in_place_scope_no_deadlock` 测试与本讲反例是同一个问题的两个面？

**答案**：两者都是「死等占住池内唯一执行线程」。该测试用 `in_place_scope` 把 body 留在池外的调用线程上，worker 得以空闲去执行 spawn 的任务；本讲反例则把死等换成帮工等待。一个是腾出线程，一个是让线程继续干活，殊途同归于「保证被等待的任务有线程可执行」。

### 4.4 嵌套并行策略

#### 4.4.1 概念说明

掌握机制之后，工程问题变成：**什么时候嵌套池、什么时候合并**。先立规则，再谈取舍。

规则一：**内层 rayon 操作跟随当前线程的池**。`install` 之内调用 `join`/`scope`/`par_iter`，它们都发生在目标池里（`install` 文档的 fib 示例正是如此：`pool.install(|| fib(20))`，fib 内部的 `rayon::join` 运行在 pool 中）。想让某段计算回到别的池，唯一的办法是再显式 `install`。

规则二：**`ThreadPool` 上的全家桶方法都是 install 的包装**。`pool.join`、`pool.scope`、`pool.scope_fifo` 本体就是 `self.install(|| join(...))` 这类一行转发；`pool.spawn`/`pool.spawn_broadcast` 则直接向该池的 registry 定向投递。理解了 install，这一整层 API 都是透明的。

规则三：**跨池 install 是一次隐式让步**。4.3.3 引用的「execution order」警告说明：跨池等待期间当前线程会被借出去执行别的任务，因此**不要假设 install 前后的代码在同一段不可分割的执行片里**——单线程池上的交错输出是合法行为。

#### 4.4.2 核心流程

选择策略的决策树：

```text
需要隔离吗？（不同优先级 / 避免互相饥饿 / 故障隔离 / 匹配不同硬件资源）
├─ 否 → 单池（全局池或一个自定义池），用 with_min_len/with_max_len 调粒度（u3-l3）
└─ 是 → 分池。然后问：谁等待谁？
    ├─ 外部代码等池 → pool.install / pool.join（帮工等待，安全）
    ├─ 池 A 的任务等池 B → 嵌套 install（帮工，安全，注意隐式让步）
    └─ 任何池内代码等「非 rayon 信号」(channel/mutex/IO 完成)
        → 死等。必须保证：被等的生产者不依赖本池的线程，或改用帮工等待
```

成本意识：每次跨池 install 都要走注入队列（慢于本地 push，见 `inject_or_push` 的注释「inject from the outside (which is slower)」），且等待方要付出帮工循环的开销；两个各 N 线程的池意味着 2N 个线程的内存与调度 footprint。**默认一个池，确有隔离需求才分池**。

#### 4.4.3 源码精读

- [rayon-core/src/thread_pool/mod.rs:267-275](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L267-L275)：`ThreadPool::join`——文档明说「Equivalent to `self.install(|| join(oper_a, oper_b))`」，实现就是这一行。
- [rayon-core/src/thread_pool/mod.rs:283-289](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L283-L289)：`ThreadPool::scope`——同样是 `self.install(|| scope(op))` 的包装；`scope_fifo`（[L298-304](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L298-L304)）与 `in_place_scope`（[L311-316](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L311-L316)）同构。
- [rayon-core/src/thread_pool/mod.rs:88-98](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L88-L98)：文档中 `do_it` 反例的代码——单线程全局池上两个 `do_it` 并发运行，`install` 前后的两个 `print!` 可能被交错。
- [rayon-core/src/thread_pool/test.rs:280-313](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/test.rs#L280-L313)：`nested_scopes`——10 个单线程池逐层嵌套 `pool.scope`，最内层用**同一个 `'scope` 生命周期**向所有池各 spawn 一个借用 `counter` 的任务。这展示了 scope 借用模型（u6-l1）跨多池组合的能力：只要等待点都在 scope 链上，栈数据可以安全地穿越任意多个池。
- [rayon-core/src/registry.rs:412-421](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L412-L421)：`inject_or_push`——入队路由的代价差异来源：本池线程走本地 `push`，否则 `inject`（注释直言注入更慢）。跨池等待的帮工循环里，线程执行的正是这些可能来自注入队列的任务。

#### 4.4.4 代码实践

**实践目标**：观察「跨池 install = 隐式让步」导致的执行顺序交错。

**操作步骤**（照抄文档示例，示例代码）：

```rust
// src/bin/order_demo.rs —— 运行: cargo run --release --bin order_demo
use rayon::{ThreadPoolBuilder, join};

fn main() {
    ThreadPoolBuilder::new().num_threads(1).build_global().unwrap();
    let pool = ThreadPoolBuilder::default().build().unwrap();
    let do_it = || {
        print!("one ");
        pool.install(|| {});
        print!("two ");
    };
    join(|| do_it(), || do_it());
    println!();
}
```

多运行几次（`for i in $(seq 20); do cargo run --release --bin order_demo; done`）。

**需要观察的现象**：多数时候输出 `one two one two`（全局池单线程的直觉顺序），但偶尔出现 `one one two two`——两次 `do_it` 在唯一的全局线程上被 install 的让步拆开交错。

**预期结果**：两种输出都合法且都可能出现；交错与否取决于 install 等待期间全局线程是否恰好偷到另一个 `do_it`。结果待本地验证（文档 [rayon-core/src/thread_pool/mod.rs:100-113](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L100-L113) 明确给出这两种合法输出）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `pool.join(a, b)` 不直接在本线程执行 `join(a, b)`，而要包一层 `install`？

**答案**：直接调用的话，`join` 会落在「当前线程所在的池」（可能是全局池或另一个池），而不是 `pool`。包一层 `install` 先把执行上下文切到 `pool` 的工作线程，内层 `join` 随之在 `pool` 中展开——这是「定池」语义的实现方式。

**练习 2**：服务 A 用 2 线程池处理延迟敏感请求，服务 B 在同一进程里跑 8 线程的批处理。分池相比共用全局池有什么收益和代价？

**答案**：收益是隔离——B 的大任务不会窃取/占用 A 的线程，A 的尾延迟不被 B 干扰；还能分别配置线程名、栈大小、优先级（配合 `spawn_handler`）。代价是 10 个线程的内存与上下文切换、跨服务协作时必须走帮工等待或外部线程等待（否则死锁风险翻倍），以及注入队列的额外延迟。

**练习 3**：`nested_scopes` 测试中，10 层嵌套 scope 全部结束后才断言 counter。这依赖 scope 的哪条性质？

**答案**：依赖「scope 返回前，其中 spawn 的所有任务（含嵌套 scope 内的）必然完成」这一运行期协议（u6-l1 的 CountLatch 计数归零才置位）。每个内层 `pool.scope` 的返回都保证该层任务完成，10 层串起来即所有池的任务全部结束，借用 `counter` 的任务不会再访问它。

## 5. 综合实践

把本讲三个模块串成一个完整的「跨池流水线」小程序（示例工程，依赖 `rayon = "1"`）：

**任务**：模拟「协调池 + 计算池」的架构。协调池（1 线程）负责拆分任务并汇总；计算池（2 线程）做实际乘法。要求：

1. 用 `ThreadPoolBuilder` 分别建 `coord`、`compute` 两个池并指定线程名前缀。
2. 在 `coord.install` 中对 `1..=100` 跑并行迭代器，每个元素 `compute.install(move || i * i)`，最后 `sum`——复刻 `cross_pool_busy` 的形状。
3. 断言结果等于 \( \sum_{i=1}^{100} i^2 = \frac{100 \times 101 \times 201}{6} = 338350 \)。
4. 打印每个 `compute.install` 实际执行的线程名（用 `std::thread::current().name()`），验证只有 compute 池的线程出现——验证隔离。
5.（选做）故意把第 2 步换成「`compute.spawn` + 主线程 `recv`」的错误写法，用 `timeout` 观察两种写法的差异，然后解释：为什么正确写法安全而错误写法危险（提示：本例中协调闭包在 coord 池线程上 recv 死等，而 compute 的任务恰好不依赖 coord，所以可能侥幸不死锁——说清「侥幸」与「必然」的边界在哪里）。

**验收标准**：程序打印正确的和、线程名只含 compute 池；能用自己的话写出 4.3.2 的依赖环分析。

## 6. 本讲小结

- `ThreadPool::install` 是同步入口：闭包被包成 `StackJob` 送进指定池，调用点等到结果（或重放 panic）才返回；`in_worker` 按「非池线程 / 跨池线程 / 本池线程」分三条路径。
- 跨池等待是**帮工式**的：`in_worker_cross` 用 `SpinLatch::cross`，等待线程在 `wait_until` 循环里继续消化原池任务（本地 → 窃取 → 注入队列），`mutual_install` 证明了互安装不会死锁。
- 池与池是孤岛：独立的线程、队列与窃取域，跨池投递只能走对方的注入队列；一个池救不了另一个池被占满的线程。
- 死锁的成因不是「跨池」本身，而是**池线程上的不帮工阻塞**（channel/mutex/condvar/sleep）切断了被等任务的执行资源；修复思路是把死等换成 install/join/scope 等帮工等待，或用 `in_place_scope` 腾出 worker。
- 嵌套规则：内层 rayon 操作跟随当前线程的池；`ThreadPool` 的 `join`/`scope` 全家都是 `install` 的包装；跨池 install 是一次隐式让步，不要假设它前后代码不可分割。
- 工程默认单池，确有隔离需求（优先级、防饥饿、故障隔离）才分池，并据此付出注入延迟与线程 footprint 的代价。

## 7. 下一步学习建议

下一讲 u7-l3「让出与线程信息」继续 `ThreadPool` 的工具箱：`current_thread_index`、`yield_now`/`yield_local`，以及 `current_thread_has_pending_tasks` 在「外层迭代器防饿死」中的用途——其中 yield 家族与本讲的帮工等待一脉相承，是把「让出执行权」从隐式行为变成显式 API。

建议顺带阅读的源码：

- [rayon-core/src/registry.rs:846-853](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L846-L853)：`WorkerThread::yield_now`——一次 find_work + execute 的最小让步，是帮工循环的单步版本。
- `rayon-core/src/scope/mod.rs`（u6-l1 已读，可带着本讲的「等待方式由等待者身份决定」视角重读 `ScopeBase` 的 Stealing/Blocking 两态等待）。
- 集成测试目录下的 `tests/cross-pool.rs` 与 `rayon-core/src/thread_pool/test.rs` 中其余测试（`workers_stop`、`sleeper_stop` 展示池 drop 后线程如何有序退出，衔接 u5-l3 的 terminate_count 协议）。
