# panic 传播、测试体系与 miri

## 1. 本讲目标

学完本讲，你应该能够：

1. 完整复述一次跨线程 panic 的传递路径：worker 线程上 `catch_unwind` 捕获 → `Channel` 装箱回传 → 发起线程 `resume_unwind` 重新抛出。
2. 解释 `join` 的三个 panic 出口（b 分支本地、a 分支本地、a 分支跨线程）为何最终都表现为"在调用者线程抛出"，以及为什么 worker 线程永远不会被用户 panic 杀死。
3. 读懂 `join_panic` 测试的"双保险"设计：`should_panic` + 线程 id 判定 + 人工通过，理解它如何避免假阳性与 flaky。
4. 会用 `cargo +nightly miri` 在本地验证并发 unsafe 代码，并看懂 CI 中 nextest 过滤与 `-Zmiri-many-seeds` 各自守护什么。

## 2. 前置知识

### 2.1 Rust 的 panic 与展开（unwinding）

`panic!` 默认不会立刻终止进程，而是沿着调用栈**展开**（unwind）：逐层析构局部变量，直到被某个边界捕获或跑出 `main`。标准库提供两个关键函数控制这条边界：

- `panic::catch_unwind(f)`：执行 `f`，若 `f` panic 则捕获展开，把 panic 载荷（payload）以 `Err(Box<dyn Any + Send>)` 返回。类型别名 `thread::Result<T> = Result<T, Box<dyn Any + Send + 'static>>`。
- `panic::resume_unwind(e)`：把捕获到的载荷**原样重新抛出**，继续展开。它与 `panic!` 的区别是：不再打印消息、不新建 backtrace 起点——因为"原始现场"已经打印过了，这里只是接力。

### 2.2 AssertUnwindSafe

`catch_unwind` 要求闭包满足 `UnwindSafe`（可展开安全）trait，防止"panic 发生后仍从两个路径访问同一可变状态"。`&mut T` 不满足该 trait。chili 的用户闭包要拿 `&mut Scope`，所以必须用 `AssertUnwindSafe` 显式断言"我知道跨 panic 边界是安全的"——这是库作者的责任声明，编译器不再检查。

### 2.3 `#[should_panic]` 测试属性

标注了 `#[should_panic(expected = "...")]` 的测试**只有以 panic 结束、且 panic 消息包含指定子串**才算通过。它带来一个设计难题：如果被测行为依赖"真的跨了线程"，那么在并行度不足的机器上测试可能不 panic——测试就会失败（flaky）。本讲会看到 chili 如何用"人工 panic"解决。

### 2.4 miri 与 nextest

- **miri**：Rust 官方的 MIR 解释器。它不编译成机器码，而是逐条解释执行，运行时检查未定义行为：越界访问、悬垂指针解引用、违反 Stacked Borrows 别名规则等。对多线程程序，miri 在单进程内模拟线程切换，代价是比原生慢几个数量级——所以只适合跑小测试。
- **nextest**：cargo 测试的替代运行器，每个测试跑在独立进程中，支持过滤表达式（`-E`）和并行度控制（`-j`）。miri 直接支持以 nextest 作为后端。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [src/job.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs) | `harness` 中唯一一次 `catch_unwind`；`Channel::val` 如何装箱 panic 载荷回传 |
| [src/lib.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs) | `join_heartbeat` 的三个 panic 出口、`resume_unwind` 的接力点；内嵌 `tests` 模块的 8 个测试（重点 `join_panic`） |
| [.github/workflows/ci.yml](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml) | 五个 CI 作业中的 miri 作业：nextest 过滤排除重测试、`-Zmiri-many-seeds` 盯紧 `join_wait` |

chili 全库只有这两个源文件加一个基准文件，本讲覆盖的行号都在其中。

## 4. 核心概念与源码讲解

### 4.1 panic 捕获与恢复：跨线程 panic 的完整传递路径

#### 4.1.1 概念说明

chili 的 `join(a, b)` 把 `a` 打包成 `Job`（留在当前栈帧的 `JobStack` 上），`b` 留在当前线程执行。如果 `a` 被 worker 线程偷走，它就在**另一个线程**上运行——此时如果 `a` panic，展开发生在 worker 线程上，直接抛出去会杀死 worker，线程池就废了。

chili 的解法是：**在 worker 线程的边界上把 panic 变成普通数据**。`harness` 用 `catch_unwind` 捕获用户闭包的 panic，把 `thread::Result<T>`（可能是 `Err`）当作普通返回值，通过上一讲精读的 `Channel` 传回发起 join 的线程；发起线程看到 `Err`，再用 `resume_unwind` 原样重新抛出。

这样对调用者来说，无论 `a` 实际在哪个线程执行，panic 总是"回到调用 `join` 的地方抛出"——与顺序执行的语义一致。worker 线程则完全不受影响，捕完结果继续回循环取下一个任务。

#### 4.1.2 核心流程

一次 `join_heartbeat(a, b)` 的 panic 出口有三条：

```text
join_heartbeat(a, b)
  ├─ a 打包入本地队列，可能被 heartbeat() 送上货架
  ├─ b(self)                          ← 出口①：b 在当前线程执行
  │                                      panic 直接在本线程展开，无需传递
  └─ take_receiver()
      ├─ Some(receiver)  （a 已出队）
      │   └─ wait_for_sent_job(receiver)
      │       ├─ 任务未被偷走 → 返回 None
      │       │     └─ (stack.take_once())(self)
      │       │        ← 出口②：a 收回本地执行，panic 直接在本线程展开
      │       └─ 任务已被偷走 → 帮忙执行 + receiver.recv()
      │             ← worker 线程：harness → catch_unwind(f)
      │                             → sender.send(Ok/Err) → 回循环
      │             ← 发起线程：recv() 得 Some(Err(e))
      │                             → panic::resume_unwind(e)
      │                              ← 出口③：跨线程 panic 接力重新抛出
      └─ None（a 从未出队）
          └─ pop_back() 后本地执行   ← 也属于出口②
```

worker 线程侧的时间线：

```text
execute_worker 循环
  → pop_earliest_shared_job() 拿到 JobShared
  → JobShared::execute
      → harness(scope, stack, sender)
          → f(scope) panic！
          → catch_unwind 捕获，得到 Err(Box<dyn Any + Send>)
          → sender.send(Err(...))          # panic 变成数据
  → 回到循环继续等下一个任务                # worker 若无其事
```

注意出口②的两处（[src/lib.rs:377](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L377) 与 [src/lib.rs:388](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L388)）是**裸调用** `take_once` 出的闭包，外面没有 `catch_unwind`——因为它们本来就在发起线程上，panic 直接向上展开即可，无须捕获再重放。

#### 4.1.3 源码精读

**捕获点：全库唯一的 `catch_unwind`。** 它藏在 `job.rs` 的 `harness` 函数体最后一行：

[src/job.rs:153-176](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L153-L176)

```rust
unsafe fn harness<F, T>(scope: &mut Scope<'_>, stack: NonNull<JobStack>, sender: Sender)
where
    F: FnOnce(&mut Scope<'_>) -> T + Send,
    T: Send,
{
    // ……cast 还原 F、take_once 取出闭包、transmute 还原 Sender<T> ……
    sender.send(panic::catch_unwind(AssertUnwindSafe(|| f(scope))));
}
```

这一行是整个 panic 机制的枢纽：

- `f(scope)` 是在 worker 线程上调用用户闭包；
- `AssertUnwindSafe` 必须写，因为闭包捕获了 `&mut Scope`，不是 `UnwindSafe`——库在此声明：panic 展开后 `Scope` 的内部状态仍可安全继续使用（worker 捕获后只做 `send`，然后回到 `execute_worker` 循环取下一个任务，不再触碰已展开闭包留下的状态）；
- `catch_unwind` 的返回值类型恰是 `thread::Result<T>`，与 `Sender::send` 的参数类型严丝合缝——成功值和 panic 载荷走同一条通道。

**装箱回传：`Channel::val` 的类型是为 panic 设计的。**

[src/job.rs:30-33](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L30-L33)

```rust
/// Can only be written only by the `Sender` and read by the `Receiver` if
/// `state` is `State::Ready`.
val: UnsafeCell<Option<Box<thread::Result<T>>>>,
```

`val` 存的是 `Option<Box<thread::Result<T>>>`——注意 `thread::Result` 的 `Err` 变体就是 `Box<dyn Any + Send>`，panic 载荷天然是装箱的。发送侧再包一层 `Box::new`（[src/job.rs:92-94](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L92-L94)），接收侧解一层（[src/job.rs:80](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L80)）。外层这层 `Box` 的目的在 u3-l3 讲过：让字段宽度与 `T` 无关（定宽指针），从而支撑 `repr(C)` 布局下的 `transmute`。panic 安全与布局技巧在此汇合于同一个字段。

**接力点：`resume_unwind`。**

[src/lib.rs:366-378](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L366-L378)

```rust
let rb = b(self);                        // 出口①：b 的 panic 在这里直接展开

if let Some(receiver) = job.take_receiver() {
    let ra = match self.wait_for_sent_job(receiver) {
        Some(Ok(val)) => val,
        Some(Err(e)) => panic::resume_unwind(e),   // 出口③：接力重新抛出
        // SAFETY: 任务未被真正送出，take_once 只会被调用一次……
        None => unsafe { (stack.take_once())(self) },  // 出口②
    };

    (ra, rb)
} else {
    self.job_queue.pop_back();
    // SAFETY: ……
    (unsafe { (stack.take_once())(self) }, rb)      // 出口②
}
```

`wait_for_sent_job` 的尾部把通道结果原样交回：

[src/lib.rs:297-316](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L297-L316)

```rust
while receiver.is_empty() {
    // ……从货架帮忙偷任务执行……
}

Some(receiver.recv())     // recv 返回 thread::Result<T>，Err 即跨线程 panic
```

`recv` 返回的就是 `harness` 里 `send` 进去的那个 `thread::Result`——`Ok` 装正常结果，`Err` 装 panic 载荷，两种情况对通道完全无差别。

**worker 侧：用户 panic 杀不死线程池。**

[src/lib.rs:118-131](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L118-L131)

```rust
loop {
    let job = {
        let mut lock = context.lock.lock().unwrap();
        lock.pop_earliest_shared_job()
    };

    if let Some(job) = job {
        // SAFETY: Any `Job` that was shared between threads is waited upon
        // before the `JobStack` exits scope.
        unsafe {
            job.execute(&mut scope);     // panic 已在 harness 里被吞掉
        }
    }
    // ……继续循环等待下一个任务
```

`job.execute` 调用链底部的 `catch_unwind` 保证：即使用户闭包 panic，`execute_worker` 的循环体也正常走完，worker 回到 `job_is_ready` 上继续等活。这正是"把 panic 变成数据"的价值——线程池的存活不依赖用户代码的行为。

一个值得记录的边界（承接 u4-l1 的待验证疑点）：如果 panic 发生在**出口①（b 分支）**，此时 `a` 可能已送上货架，而 `take_receiver` 永远不会被调用——发起线程的栈帧带着 `JobStack` 直接展开销毁，货架上的 `JobShared` 里留下指向已销毁栈帧的 `NonNull`。`ManuallyDrop` 保证这只是泄漏而非重复释放，但若 worker 之后取走该任务并执行，就是悬垂指针解引用。这是源码审读时发现的疑点，尚未在真实环境复现——本讲第 4.3 节的 miri 多种子正是排查此类时序敏感问题的工具。

#### 4.1.4 代码实践

**实践目标**：亲手验证三条 panic 出口中的两条——b 分支（本地直接展开）与 a 分支（可能跨线程、经通道回传）——在 `catch_unwind` 看来行为一致：panic 最终都回到调用线程、载荷可被 `downcast` 还原。

**操作步骤**（以下「示例代码」加到你本地 chili 克隆的 `src/lib.rs` 内嵌 `mod tests` 中，与本仓库源码区分）：

1. 克隆仓库并确认基线：`git checkout 6c49338` 后运行 `cargo test --lib`，8 个测试应全部通过。
2. 在 [src/lib.rs:635](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L635) 的 `mod tests` 内追加测试（`use super::*;` 已把 `panic`、`thread`、`Duration`、`NonZero` 引入作用域）：

```rust
// 示例代码：验证 join 的 panic 能被外层 catch_unwind 捕获
#[test]
fn catch_panic_from_join_branch_a() {
    let threat_pool = ThreadPool::with_config(Config {
        thread_count: Some(NonZero::new(2).unwrap()),
        heartbeat_interval: Duration::from_micros(1),
        ..Default::default()
    });

    let mut scope = threat_pool.scope();

    let result = panic::catch_unwind(panic::AssertUnwindSafe(|| {
        // TIMES = 1：每次 join 都检查心跳；先 sleep 给 worker 偷走任务的机会
        scope.join_with_heartbeat_every::<1, _, _, _, _>(
            |_| {
                thread::sleep(Duration::from_micros(100));
                panic!("my panic")
            },
            |_| 0u32,
        )
    }));

    let err = result.unwrap_err();
    let msg = err
        .downcast_ref::<&str>()
        .map(|s| s.to_string())
        .or_else(|| err.downcast_ref::<String>().cloned())
        .unwrap();
    assert!(msg.contains("my panic"));
}
```

3. 运行 `cargo test --lib catch_panic_from_join`。
4. 把 `panic!("my panic")` 从 a 分支移到 b 分支（第二个闭包），改名为 `catch_panic_from_join_branch_b` 再跑一次。

**需要观察的现象**：

- 测试输出前会打印一次 `thread '<unnamed>' panicked at src/lib.rs:...: my panic` 之类的消息——这是 panic 发生地（可能是 worker 线程）的默认 panic hook 打印的，属预期现象；`resume_unwind` 接力时**不会**再打印第二次。
- 两个测试都通过：无论 a 分支实际在哪个线程执行，`catch_unwind` 都在测试线程捕获到载荷。

**预期结果**：两个测试均绿。若在 a 分支版本中反复运行，偶尔能看到 panic 来自 unnamed 线程（worker）、偶尔来自主线程——恰好对应出口③与出口②的随机切换。具体打印的线程名与行号**待本地验证**（取决于机器并行度与调度）。

#### 4.1.5 小练习与答案

**练习 1**：为什么发起线程用 `resume_unwind(e)` 而不是重新 `panic!("...")`？

**答案**：`resume_unwind` 原样传递载荷并继续展开，不打印消息、不新建 panic 起点。panic 的原始现场在 worker 线程已经打印过 backtrace 了；若重新 `panic!`，会二次打印、丢失原始载荷类型（调用者将无法 `downcast` 出 `"my panic"` 这个 `&str`），且 backtrace 指向库内部而非用户代码。

**练习 2**：`Channel::val` 的类型是 `Option<Box<thread::Result<T>>>`，其中 `thread::Result` 的 `Err` 本身就是 `Box<dyn Any + Send>`。发送侧为何还要再 `Box::new` 一层？

**答案**：为了让 `val` 字段的宽度与 `T` 无关——`Box<thread::Result<T>>` 无论 `T` 是什么都只是一个定宽指针。这是 u3-l3 讲过的 `repr(C)` 布局保证下 `Sender → Sender<T>` transmute 的前提；顺带地，panic 载荷 `Err(Box<dyn Any>)` 也天然适配这个"装进箱子"的形态。

**练习 3**：出口②的两处 `take_once` 调用外为什么可以不包 `catch_unwind`？

**答案**：这两处是任务被收回**本地**执行的路径，闭包本来就在发起线程上运行，panic 直接沿调用栈向上展开即可被调用者的 `catch_unwind`（若有）或测试框架捕获，无须"先捕获再重放"的绕路。

### 4.2 单元测试设计：`join_panic` 的"双保险"技巧

#### 4.2.1 概念说明

[src/lib.rs:635-833](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L635-L833) 的内嵌测试模块共 8 个测试，覆盖从"池能启停"到"128 并发 scope"的行为面。其中最难写对的是 `join_panic`：它要验证的是 4.1 节那条跨线程 panic 通路，但"是否真的跨线程"受调度影响，不完全可控。

一个朴素的写法——在闭包里无条件 `panic!`，外面套 `#[should_panic]`——会有**假阳性**：panic 走出口②（本地执行）时测试也通过，但那根本没测到通道回传路径。chili 的答案是三重技巧：线程 id 判定确保只测跨线程路径、`should_panic` 双出口设计消除 flaky、单线程环境人工通过兜底。

#### 4.2.2 核心流程

`join_panic` 的通过路径有两条，殊途同归于同一条消息：

```text
join_panic（#[should_panic(expected = "panicked across threads")]）
  │
  ├─ 前置：available_parallelism() == 1
  │     └─ 直接 panic!("panicked across threads")     ← 人工通过路径 A
  │
  └─ 运行 increment（TIMES=1，a 分支 sleep 100µs）
        ├─ a 在别的线程执行（thread::current().id() != id）
        │     └─ panic!("panicked across threads")
        │           → catch_unwind → Channel → resume_unwind
        │           → 测试以该 panic 结束               ← 真实路径 B
        └─ 从未跨线程（调度不充分）
              └─ assert_eq!(vals, [1; 10])  先验证工作正确
                 panic!("panicked across threads")      ← 人工通过路径 C
```

最大化"真的跨线程"概率的手段，全部继承自 `join_wait` 的配置：

| 手段 | 位置 | 作用 |
| --- | --- | --- |
| `thread_count: 2` + `heartbeat_interval: 1µs` | 测试开头配置 | 1 个 worker + 极短心跳，让任务尽快被偷 |
| `join_with_heartbeat_every::<1, ...>` | 递归 join 处 | 每次 join 都查心跳，不做 1/64 降频 |
| a 分支先 `sleep(100µs)` | a 闭包开头 | 把偷任务的窗口拉大 100 倍 |

#### 4.2.3 源码精读

**测试签名与配置**：

[src/lib.rs:749-755](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L749-L755)

```rust
#[test]
#[should_panic(expected = "panicked across threads")]
fn join_panic() {
    let threat_pool = ThreadPool::with_config(Config {
        thread_count: Some(NonZero::new(2).unwrap()),
        heartbeat_interval: Duration::from_micros(1),
    });
```

`expected = "..."` 是**子串匹配**：最终 panic 消息包含该子串即可，不要求全等——因此人工通过路径 A/C 只要 panic 同样的字符串就能与真实路径 B 等价。

**单线程兜底**：

[src/lib.rs:757-762](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L757-L762)

```rust
if let Some(thread_count) = thread::available_parallelism().ok().map(NonZero::get) {
    if thread_count == 1 {
        // Pass test artificially when only one thread is available.
        panic!("panicked across threads");
    }
}
```

单核环境下跨线程行为不可靠，作者选择直接人工通过，保证测试在任何 runner 上都是确定性的——测试的职责是"验证跨线程 panic 能传播"，而不是"验证机器有多个核"。

**核心判定：线程 id 才是跨线程的证据**：

[src/lib.rs:764-790](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L764-L790)

```rust
fn increment(s: &mut Scope, slice: &mut [u32], id: ThreadId) -> bool {
    let mut threads_crossed = AtomicBool::new(false);

    match slice.len() {
        0 => (),
        1 => slice[0] += 1,
        _ => {
            let (head, tail) = slice.split_at_mut(1);

            s.join_with_heartbeat_every::<1, _, _, _, _>(
                |_| {
                    thread::sleep(Duration::from_micros(100));

                    if thread::current().id() != id {
                        threads_crossed.store(true, Ordering::Relaxed);
                        panic!("panicked across threads");
                    }

                    head[0] += 1;
                },
                |s| increment(s, tail, id),
            );
        }
    }

    *threads_crossed.get_mut()
}
```

关键在 `thread::current().id() != id` 这个守卫：`id` 是发起线程的 `ThreadId`，只有当 a 分支**真的在 worker 线程上**执行时才 panic。这就是防假阳性的闸门——本地执行（出口②）不会触发，测试唯一能"自然通过"的方式就是 panic 走完通道回传全程。

顺带一个批判性阅读素材：`threads_crossed` 的 `true` 值实际上永远传不出来——`store(true)` 之后紧跟着 `panic!`，而 panic 会（经 `resume_unwind`）把 `increment` 的所有栈帧展开掉，`*threads_crossed.get_mut()`（[src/lib.rs:789](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L789)）只在"从未跨线程"的成功路径上执行、此时值必为 `false`。真正驱动测试的是 panic 本身；这个 `AtomicBool` 更像是表达意图的文档式代码（笔者的分析，读者可自行验证：删掉它与 `store` 后测试行为不变——推导见练习 3）。

**第二个人工通过出口**：

[src/lib.rs:792-804](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L792-L804)

```rust
let mut vals = [0; 10];

let threads_crossed =
    increment(&mut threat_pool.scope(), &mut vals, thread::current().id());

// Since there was no panic up to this point, this means that the
// thread boundary has not been crossed.
//
// Check that the work was done and pass the test artificially.
if !threads_crossed {
    assert_eq!(vals, [1; 10]);
    panic!("panicked across threads");
}
```

走到这里说明全程没跨线程（否则早 panic 了）。注意作者没有直接让测试"失败"或"跳过"，而是先 `assert_eq!` 验证顺序路径把活干对了，再人工 panic 满足 `should_panic`。于是这个测试在任何机器上都能通过，同时不放弃任何一次断言机会。

**测试模块全家福**（本讲只精读 `join_panic`，其余供横向对照）：

| 测试 | 行号 | 验证点 |
| --- | --- | --- |
| `thread_pool_stops` | [src/lib.rs:643-646](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L643-L646) | 池创建 + Drop 优雅停机 |
| `thread_pool_with_one_thread` | [src/lib.rs:648-654](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L648-L654) | 零 worker 配置不炸 |
| `join_basic` | [src/lib.rs:656-667](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L656-L667) | 基本正确性 |
| `join_long` | [src/lib.rs:669-690](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L669-L690) | 链式剥离 1024 元素 |
| `join_very_long` | [src/lib.rs:692-714](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L692-L714) | 对半切分 100 万元素（miri 排除项） |
| `join_wait` | [src/lib.rs:716-747](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L716-L747) | 强制跨线程等待（miri 盯防项） |
| `join_panic` | [src/lib.rs:749-805](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L749-L805) | 本讲主角 |
| `concurrent_scopes` | [src/lib.rs:807-832](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L807-L832) | 128 线程并发建 scope |

#### 4.2.4 代码实践

**实践目标**：通过破坏性实验体会"线程 id 守卫"的必要性——去掉守卫后测试仍通过，但测的东西变了。

**操作步骤**：

1. 在本地副本中复制 `join_panic`，改名 `join_panic_no_guard`，把 a 闭包里的判定删掉，变成无条件 panic（「示例代码」）：

```rust
s.join_with_heartbeat_every::<1, _, _, _, _>(
    |_| {
        thread::sleep(Duration::from_micros(100));
        panic!("panicked across threads");   // 无条件 panic
    },
    |s| increment(s, tail, id),
);
```

2. 运行 `cargo test --lib join_panic_no_guard -- --nocapture`，确认它通过。
3. 对照运行原版 `cargo test --lib join_panic -- --nocapture`，观察 panic 打印中出现的线程名（`main` 与否）。

**需要观察的现象**：两个版本都绿；但去掉守卫的版本可能在本地路径（出口②）就 panic 完成了，从未走过 `Channel` 回传——`#[should_panic]` 无法区分这两条路径。

**预期结果**：由守卫版保证语义、无守卫版暴露"测试通过 ≠ 测到目标路径"的陷阱。具体哪个线程打印 panic **待本地验证**（取决于调度）。

#### 4.2.5 小练习与答案

**练习 1**：`join_panic` 为什么必须用 `thread::current().id() != id` 做守卫，而不能在 a 闭包里无条件 `panic!`？

**答案**：无条件 panic 时，若任务恰好没被偷走、在本地执行（出口②），测试照样通过——但这只验证了"本地 panic 会让测试失败"这种平凡事实，通道回传路径（出口③）完全没被覆盖。守卫确保唯一能触发 panic 的情形就是"真的跨了线程"，测试失败模式与被测目标严格对应。

**练习 2**：删掉 [src/lib.rs:801-804](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L801-L804) 的人工通过块，测试在什么环境下会失败？

**答案**：在跨线程从未发生的环境（单核、调度不充分、或恰好每次任务都在本地执行完），`increment` 正常返回、测试函数正常结束——而 `#[should_panic]` 要求测试以 panic 收场，于是报 "test did not panic as expected"。这正是 flaky 测试的典型来源。

**练习 3**：论证"`threads_crossed` 的 `true` 值永远传不出 `increment`"。

**答案**：`store(true)` 的下一行就是 `panic!("panicked across threads")`，此后无论任务在哪个线程执行，该 panic 要么本地展开、要么经 `resume_unwind` 接力，都会展开 `increment` 的全部调用栈——`threads_crossed` 作为栈上局部变量随之销毁，`*threads_crossed.get_mut()` 只在"无 panic 返回"的路径执行，而那条路径上它必然是 `false`。所以返回值恒为 `false`，判定完全由 panic 自身完成。

### 4.3 miri 与 nextest：CI 里的并发正确性防线

#### 4.3.1 概念说明

chili 的核心卖点是"精心论证的 unsafe"，但论证可能出错——尤其是 4.1 节末尾提到的时序敏感疑点。人审之外，项目在 CI 里部署了第二道防线：**miri 作业**，用解释器执行测试、在 UB 发生当场报错，而不是等它演化成诡异崩溃。

miri 慢几个数量级，所以策略是"广撒网排除重灾区 + 对最敏感的测试做多种子反复轰炸"：前者用 nextest 的过滤器把 100 万元素的 `join_very_long` 排除掉，其余测试 8 并发跑完；后者对跨线程压力最大的 `join_wait` 用 `-Zmiri-many-seeds` 换着随机种子重跑，每种种子对应一种弱内存交错/线程切换序列，最大化并发路径覆盖。

#### 4.3.2 核心流程

CI 五个作业的关系：

```text
push / PR
  ├─ check  （cargo check --all，稳定版 1.81）
  ├─ test   （cargo test --all）
  ├─ fmt    （cargo fmt --check）
  ├─ clippy （cargo clippy --all）
  └─ miri   （nightly + miri 组件）
        ├─ cargo +nightly miri setup
        ├─ cargo +nightly miri nextest run -j8 -E 'not (test(join_very_long))'
        └─ MIRIFLAGS=-Zmiri-many-seeds cargo +nightly miri test --lib -- join_wait
```

注意与 u1-l2 讲过的 crate 根部三条 deny lint 的分工：lint 在**编译期**强制 unsafe 块写 SAFETY 注释、公共项写文档（人审的物料）；miri 在**运行期**检查这些注释声称的不变量是否真的成立。两者互补，缺一不可。

#### 4.3.3 源码精读

**miri 作业全貌**：

[.github/workflows/ci.yml:49-61](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml#L49-L61)

```yaml
miri:
  name: Miri
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: dtolnay/rust-toolchain@nightly
      with:
        components: miri
    - uses: Swatinem/rust-cache@v2
    - uses: taiki-e/install-action@nextest
    - run: cargo +nightly miri setup
    - run: cargo +nightly miri nextest run -j8 -E 'not (test(join_very_long))'
    - run: MIRIFLAGS=-Zmiri-many-seeds cargo +nightly miri test --lib -- join_wait
```

与前四个作业的关键差异：toolchain 是 **nightly**（miri 组件只随 nightly 发放），而 check/test/fmt/clippy 全部钉在稳定版 `1.81.0`（如 [.github/workflows/ci.yml:15](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml#L15)）——库本身不依赖 nightly 特性，nightly 只服务于验证工具。

**第 60 行：nextest 过滤。**

```yaml
- run: cargo +nightly miri nextest run -j8 -E 'not (test(join_very_long))'
```

- `-E 'not (test(join_very_long))'` 是 nextest 的过滤器表达式：名字匹配 `join_very_long` 的测试被排除，其余全跑；
- 排除原因纯粹是成本：`join_very_long`（[src/lib.rs:692-714](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L692-L714)）要处理 1024×1024 个元素、递归上百万次 join，在解释执行下代价过高；它覆盖的代码路径（对半切分递归）与 `join_long`/`join_wait` 高度重叠，排除它损失很小；
- `-j8`：8 路并行，nextest 每测试独立进程，单测崩溃不连坐；
- 用 nextest 而非 `cargo miri test` 的收益就在这两点：过滤表达式的表达力与进程级隔离下的并行控制。

**第 61 行：多种子轰炸。**

```yaml
- run: MIRIFLAGS=-Zmiri-many-seeds cargo +nightly miri test --lib -- join_wait
```

`MIRIFLAGS=-Zmiri-many-seeds` 让 miri 用一批不同的随机种子反复运行同一测试——每个种子改变弱内存访问的交错结果与线程切换时机，等效于把同一段并发代码扔进很多个"平行宇宙"各跑一遍。被盯防的 `join_wait`（[src/lib.rs:716-747](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L716-L747)）是全测试套里跨线程压力最大的用例：`thread_count=2`、心跳 1µs、`TIMES=1`、a 分支 sleep 10µs——它反复走过的正是 `heartbeat()` 投递、worker 偷取、`wait_for_sent_job` 帮忙、`Channel` park/unpark 这条最依赖时序的链路，也就是 u3-l2/u4-l1 审读的那些不变量真正被压的地方。

**本地复现**：CI 的三行命令在装好 nightly + miri 后可以原样本地执行；最小验证是 `cargo +nightly miri test --lib -- join_wait`（不带 many-seeds，单种子较快）。

#### 4.3.4 代码实践

**实践目标**：在本地把 miri 跑起来，直观感受"解释执行检查 UB"与普通 `cargo test` 的差异。

**操作步骤**：

1. 安装工具链（一次性）：

```bash
rustup toolchain install nightly
rustup component add miri --toolchain nightly
```

2. 先跑普通测试作对照：`cargo test --lib`。
3. 跑 miri：`cargo +nightly miri test --lib`（首次会自动 `miri setup` 下载依赖的解释版）。
4. 挑一个跑：`cargo +nightly miri test --lib -- join_wait`。
5. 把 4.1.4 写的 `catch_panic_from_join_branch_a` 也用 `cargo +nightly miri test --lib -- catch_panic_from_join` 跑一遍。

**需要观察的现象**：

- miri 运行明显慢于普通测试（秒级 vs 毫秒级）；
- 全部测试无 UB 报错、正常通过；
- 若想看 miri "抓到东西"长什么样，可以临时在本地副本的某个测试里加一句越界索引（如 `let v = [0; 1]; v[1];`）再跑——会看到 `Undefined Behavior: index out of bounds` 的详细报告；观察完务必还原。

**预期结果**：步骤 3/4/5 全绿。miri 下各测试的具体耗时与种子数输出格式**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 miri 作业要单独排除 `join_very_long`，而不是把所有测试都多种子跑一遍？

**答案**：miri 是解释执行，慢几个数量级；`join_very_long` 递归上百万次 join，逐条解释代价不可接受，而它覆盖的路径与更小的测试重叠。工程取舍是：全部测试单遍跑（除最重者）保证广度，再对时序最敏感的 `join_wait` 多种子深挖保证深度。

**练习 2**：`MIRIFLAGS=-Zmiri-many-seeds` 改变的是什么？为什么对 `join_wait` 特别有价值？

**答案**：它让 miri 用多个随机种子重复运行，每个种子对应不同的弱内存交错与线程切换序列。`join_wait` 强制任务反复跨线程（TIMES=1、1µs 心跳、sleep 制造等待窗口），执行路径高度依赖时序——单种子跑一遍可能恰好没踩到某个交错，多种子大幅提高踩中错误交错的概率。

**练习 3**：CI 为什么 check/test/clippy 用稳定版 1.81，唯独 miri 用 nightly？这透露了库自身的什么性质？

**答案**：miri 组件只随 nightly 工具链发放，而库代码本身不依赖任何 nightly 特性——前四个作业能在稳定版编译通过就是证明。nightly 在这里纯粹是验证工具的载体：库向下游暴露最低版本门槛，验证手段却用最前沿的工具，两者互不绑架。

## 5. 综合实践

把本讲三块内容串成一个完整闭环——**写一个 panic 测试，并用 miri 为它做安全背书**：

1. **写测试**：在本地副本的 `mod tests` 里实现 4.1.4 的 `catch_panic_from_join_branch_a` 与 `branch_b` 两个版本（a 分支跨线程、b 分支本地），用 `catch_unwind` + `downcast` 断言消息包含 `"my panic"`。
2. **跑对**：`cargo test --lib catch_panic` 确认两版均通过；多跑几次（`-- --test-threads=1` 与默认各来一轮），观察 a 分支版本的 panic 打印是否有时来自 unnamed 线程（出口③）、有时来自主线程（出口②）。
3. **补守卫**：参照 `join_panic`，给 a 分支加上 `thread::current().id()` 守卫与 `#[should_panic]` 的双出口结构，改造成一个"只在真的跨线程时才 panic"的确定性测试，与你的 `catch_unwind` 版互为对照。
4. **做背书**：`cargo +nightly miri test --lib -- catch_panic` 与 `cargo +nightly miri test --lib -- join_wait` 各跑一遍，确认解释执行下无 UB 报错。
5. **写结论**：用一段文字回答——你的两个测试版本分别覆盖了 4.1.2 流程图中的哪条出口？miri 的介入让"panic 路径上没有悬垂解引用"这个命题的置信度提高了多少？4.1.3 末尾的"b 分支 panic 遗留悬垂 NonNull"疑点，你的实验能否构造出触发它的最小场景（提示：让 b 分支 panic 且 a 已上货架）？构造不出的话，说明缺少什么条件？

第 5 步没有标准答案，它把你推向 miri 的能力边界——这正是下一讲用基准测试量化"这些安全机制到底花了多少开销"的入口。

## 6. 本讲小结

- 跨线程 panic 的完整路径：worker 线程上 `harness` 里的 `catch_unwind(AssertUnwindSafe(|| f(scope)))` 把 panic 变成 `thread::Result` 的 `Err`，经 `Channel`（`val` 字段装箱回传）送到发起线程，`join_heartbeat` 中 `panic::resume_unwind(e)` 原样接力重新抛出。
- `join` 有三个 panic 出口：b 分支本地展开、a 分支收回本地裸调用 `take_once` 后展开、a 分支跨线程经通道回传——对调用者而言都表现为"在调用线程抛出"，与顺序执行语义一致。
- worker 线程永不被用户 panic 杀死：`catch_unwind` 在 harness 里吞掉展开，worker 发送完结果就回循环继续取任务。
- `resume_unwind` 优于重新 `panic!`：不二次打印、保留原始载荷与 backtrace 语义，调用者还能 `downcast` 出具体消息。
- `join_panic` 的测试设计三技巧：线程 id 守卫防止本地路径假阳性；`should_panic` + 双人工通过出口（单核兜底 + 未跨线程时先验证正确性再 panic）消除 flaky。
- CI 的 miri 作业分两层：nextest 过滤排除最重的 `join_very_long` 广撒网跑其余测试；`-Zmiri-many-seeds` 对跨线程压力最大的 `join_wait` 换种子反复轰炸，覆盖弱内存交错。

## 7. 下一步学习建议

panic 与正确性的验证到此闭环，下一讲 **u4-l3 基准测试与性能分析** 将转向"代价"：精读 [benches/overhead.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs) 中 `no_overhead` / `chili_overhead` / `rayon_overhead` 三个 divan 基准，看懂 README 中加速比与每节点约 3.5ns 开销的测量方法。建议带着一个问题去读：本讲学到的降频检查（TIMES=64）、心跳线程、panic 捕获机制，各自贡献了开销表格中的哪一部分？
