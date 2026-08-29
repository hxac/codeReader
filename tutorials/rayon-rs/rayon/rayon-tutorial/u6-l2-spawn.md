# spawn：异步任务派发

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `rayon::spawn` 的约束为什么写成 `F: FnOnce() + Send + 'static`，以及它为什么「拿不到返回值」。
2. 说明 spawn 与 join/scope 在「等待点」上的本质差异：spawn 是 fire-and-forget，等待语义要靠 channel 或 scope 自己补上。
3. 区分 `spawn`（本地 LIFO 栈序）与 `spawn_fifo`（本地 FIFO 序）的队列行为，并读懂让 FIFO 在 LIFO 双端队列上得以实现的 `JobFifo` 间接层。
4. 掌握 spawn 任务里发生 panic 时的去向：没有人在等待点重放它，所以它要么交给 panic handler、要么直接中止进程。

## 2. 前置知识

本讲是单元六第二讲，建立在你已读完 u6-l1（scope）与单元五（join、Job/Latch、工作窃取）的基础上。用三句话唤醒记忆：

- **fire-and-forget（发射后不管）**：把任务丢进线程池后立即返回，不索取结果、不等待完成。与之相对的是 fork-join 模型——`join` 会同步等两支都结束才返回。
- **`'static` 约束**：Rust 里「类型可以任意久地活着」的承诺。`std::thread::spawn` 的闭包同样要求 `'static`，因为新线程可能活得比创建它的函数还久，栈上借用在函数返回后就会变成悬垂指针。
- **双端队列的两个端口**（u5-l4）：每个工作线程有一条本地 deque，拥有者从**栈顶**弹出（后进先出，LIFO，最新任务最热）；其他线程从**另一端**窃取（先进先出，FIFO，偷最老的任务）。本讲讨论的「栈序/FIFO 序」都指**本线程、无窃取**时的顺序。

一个常被忽略的事实：`join` 的闭包**不需要** `'static`（只需 `Send`），因为借用检查把 `join` 调用视为一个会阻塞的点——`join` 返回前两支闭包必然结束，所以闭包借用当前栈是安全的。而 `spawn` 调用**立即返回**，任务可能在函数返回之后才被某个线程执行，类型系统里不存在「spawn 会等」这个事实，于是只能要求 `'static`。这是本讲最重要的一根主线。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `rayon-core/src/spawn/mod.rs` | 本讲主战场：`spawn` / `spawn_fifo` 的公开入口与内部 `spawn_in` / `spawn_job` / `spawn_fifo_in` |
| `rayon-core/src/spawn/test.rs` | 行为验证：spawn+channel 取值、池存活语义、panic handler、四种顺序测试 |
| `rayon-core/src/scope/mod.rs` | 对照物：`Scope::spawn` 的 `'scope` 约束与 CountLatch 等待（u6-l1 已精读，本讲只取对照片段） |
| `rayon-core/src/registry.rs` | 入队路由 `inject_or_push` / `inject`、`Registry::current`、`catch_unwind`、终止计数 |
| `rayon-core/src/job.rs` | `HeapJob`（堆任务）与 `JobFifo`（FIFO 间接队列） |
| `rayon-core/src/unwind.rs` | `AbortIfPanic`：无人接管的 panic 如何演变成 abort |
| `rayon-core/src/lib.rs` | `spawn` / `spawn_fifo` 的再导出 |

再导出位置在 [rayon-core/src/lib.rs:88](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L88)：`pub use self::spawn::{spawn, spawn_fifo};`——用户经由 `rayon` crate 直接调用到它们（见 u1-l4 的再导出地图）。

## 4. 核心概念与源码讲解

### 4.1 模块一：spawn 约束——为什么是 `FnOnce() + Send + 'static` 且返回 `()`

#### 4.1.1 概念说明

`rayon::spawn(move || { ... })` 把一个闭包作为任务丢进「当前」线程池，然后立即返回。它的签名刻意做得极简：

```rust
pub fn spawn<F>(func: F)
where
    F: FnOnce() + Send + 'static,
```

三个约束各有分工：

| 约束 | 原因 |
| --- | --- |
| `FnOnce()` | 任务恰好执行一次；且**签名上就不产出值**——闭包类型是 `FnOnce()`，连「算出个东西」都写不出来 |
| `Send` | 任务入队后可能被池中**任意**线程偷走执行，闭包及其捕获的数据必须能跨线程移动 |
| `'static` | `spawn` 调用立即返回，任务可能在本次函数调用结束**之后**才运行，因此不能借用任何栈上数据 |

「拿不到返回值」其实有两层：第一层在签名上（闭包是 `FnOnce()`，无返回值类型）；第二层在 API 形态上（`spawn` 返回 `()`，不像 `std::thread::spawn` 那样给你一个 `JoinHandle<T>`）。Rayon 刻意不做句柄：官方文档明说这一 API「假定闭包纯粹为副作用而执行——发消息、改互斥锁保护的数据之类」。想取回结果，就要自己造通道（4.2 节）。

#### 4.1.2 核心流程

`spawn(func)` 的完整路径（结合 u5 的调度链）：

```text
rayon::spawn(func)
  └─ Registry::current()              // 池内线程→其所属池；池外→全局池
  └─ spawn_in(func, registry)         // unsafe：断言 registry 尚未终止
       ├─ spawn_job(func, registry)
       │    ├─ registry.increment_terminate_count()   // 池的"存活计数"+1
       │    ├─ HeapJob::new(move || {
       │    │     registry.catch_unwind(func);        // 执行用户闭包并捕获 panic
       │    │     registry.terminate();               // (*) 计数 -1
       │    │   })
       │    │   .into_static_job_ref()                // 堆分配 + 类型擦除成 JobRef
       │    └─ 返回 JobRef
       └─ registry.inject_or_push(job_ref)            // 入队路由
            ├─ 当前线程属于本池 → worker.push（本地 deque 栈顶，LIFO）
            └─ 否则（池外调用）→ registry.inject（全局注入队列）
```

三个值得注意的设计：

1. **任务自带「保活」计数**。`spawn` 可能发生在任何阻塞作用域之外，没人替它兜底，所以任务自己先 `increment_terminate_count`，执行完再 `terminate`。这保证「池正在跑异步任务」时线程池不会被提前释放。
2. **panic 有去无回**。`catch_unwind` 捕获 panic 后交给池注册的 panic handler；没有 handler 时由 `AbortIfPanic` 中止进程。与 `join`/`scope`「在调用点重放」不同，spawn 没有调用点在等它。
3. **入队走 u5-l4 的老路由** `inject_or_push`：池内压本地 deque（栈序），池外进全局注入队列。

#### 4.1.3 源码精读

**入口与约束**。[rayon-core/src/spawn/mod.rs:58-64](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/mod.rs#L58-L64) 是公开入口：`spawn` 取 `Registry::current()` 后转调 unsafe 的 `spawn_in`，注释里明确「我们断言当前 registry 尚未终止」——这正是该函数标 `unsafe` 的原因（终止后的池再入队会导致任务永不执行且泄漏）。`Registry::current` 的取池逻辑见 [rayon-core/src/registry.rs:322-332](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L322-L332)：线程局部 `WorkerThread` 非空则用它所属的池，否则退回全局池。**所以池内 spawn 进的是「自己所在的池」，池外 spawn 进的是全局池。**

**文档对 `'static` 与副作用的说明**。[rayon-core/src/spawn/mod.rs:7-24](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/mod.rs#L7-L24) 写得非常直白：任务「与标准线程一样，不绑定当前栈帧，因此不能持有任何非 `'static` 的引用；要引用栈数据请用 `scope()`」，并且「本 API 假定闭包纯粹为副作用执行」。这印证了 4.1.1 的表格。

**内部实现与保活计数**。[rayon-core/src/spawn/mod.rs:69-100](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/mod.rs#L69-L100) 中的 `spawn_in` 先造一个 `AbortIfPanic` 守卫（如果入队前的代码意外 panic，直接中止而不是泄漏带终止计数的任务），成功入队后 `mem::forget` 掉守卫。`spawn_job` 是核心：

```rust
registry.increment_terminate_count();          // 池保活 +1

HeapJob::new({
    let registry = Arc::clone(registry);
    move || {
        registry.catch_unwind(func);            // 执行用户闭包，捕获 panic
        registry.terminate();                   // (*) 允许池此刻之后终止
    }
})
.into_static_job_ref()
```

`increment_terminate_count` 上方的注释 [rayon-core/src/registry.rs:575-589](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L575-L589) 点名了这条特例：「例外是 `::spawn()`，它可以在任何阻塞作用域之外创建任务；此时任务自身持有一个终止计数，并在结束时负责调用 `terminate()`」。计数归零时 [registry.rs:594-600](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L594-L600) 才置位各线程的 terminate 锁存器、开始收池。测试 [spawn/test.rs:59-84](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/test.rs#L59-L84)（`termination_while_things_are_executing`）验证的正是这件事：`ThreadPool` 句柄被 drop 后，仍在运行的 spawn 任务甚至还能再 `spawn` 出新任务，池一直活到它们全部完成。

**HeapJob：spawn 的任务形态**。[rayon-core/src/job.rs:134-147](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L134-L147) 定义 `HeapJob`——闭包直接 `Box` 进堆；执行时 [job.rs:165-175](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L165-L175) 用 `Box::from_raw` 收回所有权、调用、释放。对照 u5-l1：`join` 的闭包 B 用的是 **StackJob**（挂在调用者栈帧、零堆分配），因为「join 返回前必等 B 完成」让栈内存始终有效；spawn 没有这样的等待点，**必须堆分配**。

**panic 的去向**。[rayon-core/src/registry.rs:373-382](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L373-L382) 的 `catch_unwind`：panic 被捕获后，若有注册的 panic handler 就把 `Box<dyn Any + Send>` 交给它；若没有，局部变量 `abort_guard` 正常 drop，触发 [unwind.rs:24-31](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/unwind.rs#L24-L31) 的 `AbortIfPanic`——打印「detected unexpected panic; aborting」后 `std::process::abort()`。测试 [spawn/test.rs:86-112](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/test.rs#L86-L112) 展示了 handler 的用法（`ThreadPoolBuilder::panic_handler` 收到 `"Hello, world!"`）。文档 [spawn/mod.rs:35-42](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/mod.rs#L35-L42) 也只承诺「传播到 panic handler」而非「传播给调用者」。

#### 4.1.4 代码实践

**实践目标**：亲手验证三件事——spawn 立即返回、`'static` 约束由编译器强制、无 handler 的 panic 会中止进程（后者仅作为源码阅读结论，不建议在正式工程里试）。

**操作步骤**（示例代码，新建独立 Cargo 工程）：

1. `cargo new spawn-demo && cd spawn-demo`，在 `Cargo.toml` 加 `rayon = "1"`。
2. `src/main.rs` 写：

```rust
// 示例代码
use std::sync::mpsc::channel;

fn main() {
    let start = std::time::Instant::now();
    rayon::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(200));
        println!("task done on {:?}", std::thread::current().id());
    });
    println!("spawn returned after {:?}", start.elapsed()); // 打点 1

    // let msg = String::from("hi");
    // rayon::spawn(|| println!("{msg}"));   // 打开注释，观察编译错误

    let (tx, rx) = channel();
    let _ = tx.send(()); // 占位，防止 main 过早退出时任务还没跑到
    let _ = rx.recv();
    std::thread::sleep(std::time::Duration::from_millis(300));
}
```

3. 先原样运行，再打开注释的那两行重新编译。

**需要观察的现象**：

- 「spawn returned」的耗时是微秒级，远小于 200ms——证明 spawn 没有等待任务。
- main 结尾若不 sleep，`task done` 可能根本不打印：main 返回时任务尚未被调度（fire-and-forget 没人等它）。
- 打开注释后 `cargo build` 失败。

**预期结果**：编译错误为「closure may outlive the current function, but it borrows \`msg\`」（E0373），编译器建议使用 `move` 闭包——这正是 `'static` 约束在起作用；改成 `move || println!("{msg}")` 即可编译，代价是 `msg` 的所有权被任务夺走，主函数随后不能再用它。具体诊断措辞待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`std::thread::spawn` 返回 `JoinHandle<T>`，`rayon::spawn` 返回 `()`。为什么 Rayon 不提供句柄？

**答案**：句柄的语义是「持有者会在未来的某个 join 点等待并取回返回值」，这要求句柄与任务结果的生命周期可追踪。Rayon 的池是共享的、任务可能被任意线程偷走执行，而 spawn 的设计定位就是纯副作用（官方文档原话），加句柄会让「谁在等、等多久、池何时能释放」复杂化。源码里也留了线索：[scope/mod.rs:498-499](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L498-L499) 注释写着「意向是未来与 Rust futures 集成，支持派发能计算值的函数」。

**练习 2**：下面这段代码能编译吗？为什么？

```rust
// 示例代码
fn f() {
    static N: i32 = 5;
    rayon::spawn(|| println!("{N}"));
}
```

**答案**：能编译。`N` 是 `'static` 的静态常量，闭包对它的借用天然满足 `'static` 约束，无需 `move`。约束针对的是**借用的存活时长**，不是「必须拥有数据」。

**练习 3**：spawn 出的任务里发生 panic 且池没有注册 panic handler，进程会怎样？对照 `join` 的行为说明差异。

**答案**：会 abort（中止整个进程）。路径：`catch_unwind` 捕获 → 无 handler → `AbortIfPanic` 的 `Drop` 打印告警并 `process::abort()`（[registry.rs:373-382](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L373-L382) + [unwind.rs:24-31](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/unwind.rs#L24-L31)）。而 `join` 存在同步等待点，panic 载荷存进 `JobResult::Panic`，由等待方 `resume_unwinding` 在调用点重放（u5-l1）——区别的根源就是 spawn 没有等待者。

### 4.2 模块二：join 阻塞等待——spawn 没有句柄，等待点要自己造

#### 4.2.1 概念说明

spawn 把「派发」和「等待」彻底解耦了：派发是 O(1) 的入队，等待则需要你自己引入同步手段。Rayon 生态里有三种典型补法：

1. **channel**：任务把结果 `send` 出去，发起方 `recv` 阻塞等待。最通用，池外池内都适用。
2. **scope**：`rayon::scope(|s| s.spawn(...))` 的返回点就是等待点——u6-l1 讲过，scope 靠 `CountLatch` 计数（1 + 存活任务数）归零才放行，而且**任务可以借用 outlive `'scope` 的栈数据**。
3. **join**：如果恰好是「两个闭包、同步取双结果」，直接用 `join` 更好——它不需要 `'static`，还省掉堆分配。

三种 API 的约束与代价对照（本讲核心表格）：

| API | 任务闭包约束 | 等待点 | 能否借用栈数据 | 任务形态 | 返回结果 |
| --- | --- | --- | --- | --- | --- |
| `join(oper_a, oper_b)` | `FnOnce() -> R + Send`（**无 `'static`**） | `join` 调用自身 | 能（借用检查视 join 为阻塞点） | StackJob（栈上） | `(RA, RB)` 同步返回 |
| `rayon::spawn(f)` | `FnOnce() + Send + 'static` | **无**，需自造 | 不能 | HeapJob（堆上） | `()` |
| `s.spawn(f)`（scope 内） | `FnOnce(&Scope) + Send + 'scope` | scope 返回点 | 能（outlive `'scope` 者） | HeapJob（堆上） | `()` |
| `std::thread::spawn(f)` | `FnOnce() -> T + Send + 'static` | `JoinHandle::join` | 不能 | 独立线程 | `JoinHandle<T>` |

#### 4.2.2 核心流程

「spawn + channel」与「scope + 借用」两条等待路径：

```text
路径 A：channel
  main: (tx, rx) = channel()
  main: spawn(move || tx.send(compute()))   // 任务算完把结果送进通道
  main: rx.recv()                            // 阻塞直到收到 → 这就是 join 点
  （所有权随 tx move 进任务，无借用，满足 'static）

路径 B：scope（u6-l1）
  main: let mut acc = 0;                     // 栈上变量
  main: rayon::scope(|s| {
            s.spawn(|_| { acc += compute(); })   // 闭包约束是 'scope，可借用 acc
        })                                   // scope 返回前 CountLatch 归零
  main: 使用 acc                              // 此时所有任务已完成，借用安全
```

路径 B 之所以能借用 `acc`：编译器把闭包约束放宽到 `'scope`，而 `'scope` 的长度正是「scope 返回之前」——运行期由 CountLatch 协议保证任务全部完成在先，编译期由生命周期检查保证借用不越界在后。两把锁合起来，就是 u6-l1 说的「scope 的借用安全」。路径 A 里 `rx.recv()` 扮演了 `JoinHandle::join` 的角色。

#### 4.2.3 源码精读

**测试即文档：两种等待方式**。[spawn/test.rs:9-25](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/test.rs#L9-L25) 用两个最小测试示范了「把 spawn 变成同步」的标准写法：

```rust
fn spawn_then_join_in_worker() {          // 版本一：池内（scope 里）
    let (tx, rx) = channel();
    scope(move |_| {
        spawn(move || tx.send(22).unwrap());
    });                                    // scope 返回 = 任务已完成
    assert_eq!(22, rx.recv().unwrap());
}
fn spawn_then_join_outside_worker() {     // 版本二：池外，直接 recv 等待
    let (tx, rx) = channel();
    spawn(move || tx.send(22).unwrap());
    assert_eq!(22, rx.recv().unwrap());
}
```

版本二里 `rx.recv()` 就是全部的等待语义——没有任何 Rayon API 参与等待。

**Scope::spawn 与 spawn 的三点差异**。[scope/mod.rs:537-553](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L537-L553)：

```rust
pub fn spawn<BODY>(&self, body: BODY)
where
    BODY: FnOnce(&Scope<'scope>) + Send + 'scope,   // ① 'scope 而非 'static
{
    let scope_ptr = ScopePtr(self);
    let job = HeapJob::new(move || unsafe { ... ScopeBase::execute_job(...) });
    let job_ref = self.base.heap_job_ref(job);       // ② CountLatch 计数 +1
    self.base.registry.inject_or_push(job_ref);      // ③ 同样的入队路由
}
```

① 把 `'static` 换成 `'scope`，任务闭包就能借用栈数据，还能拿到 scope 句柄继续嵌套 spawn；② `heap_job_ref` 内部先 `job_completed_latch.increment()`（[scope/mod.rs:656-664](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L656-L664)），任务执行完在 `execute_job_closure` 里 `Latch::set` 减一，`complete` 在 [scope/mod.rs:681-689](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L681-L689) 里 `wait`——这是 spawn 完全没有的一整套等待协议；③ 入队路由与全局 spawn 一样是 `inject_or_push`，只是 registry 换成了 scope 所属的池。

**为什么 join 能免 `'static`**：回看 [scope/mod.rs:76-92](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L76-L92) 的文档示例——用 scope 模拟 join 时，闭包约束是 `A: FnOnce() -> RA + Send`，函数签名里闭包可以借用外部变量（如 `result_a`），因为模拟版的等待点（scope 返回）与真 join 的等待点一样，都发生在被借用的栈帧存活期间。真 `join` 更进一步用 StackJob 把闭包 B 放在调用者栈帧上（u5-l1），连堆分配都省了。

**多池场景**：想让任务进特定池而不是「当前池」，用 `ThreadPool::spawn`——[thread_pool/mod.rs:338-344](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L338-L344) 表明它只是把 `spawn_in` 的 registry 参数固定为 `self.registry`，其余行为完全一致。

#### 4.2.4 代码实践

**实践目标**：用同一份计算（求 1..=100 的平方和）分别走「spawn 无等待」「spawn+channel」「scope+借用栈变量」三条路，体会等待点与借用能力的差异。

**操作步骤**（示例代码）：

```rust
// 示例代码
use std::sync::mpsc::channel;

fn main() {
    // 版本 1：fire-and-forget，只能打印，取不回值
    rayon::spawn(move || {
        let s: u64 = (1..=100).map(|i| i * i).sum();
        println!("v1 computed {s} inside the task");
    });

    // 版本 2：channel 取回结果
    let (tx, rx) = channel();
    rayon::spawn(move || {
        tx.send((1..=100u64).map(|i| i * i).sum::<u64>()).unwrap();
    });
    let v2 = rx.recv().unwrap();

    // 版本 3：scope + 借用主线程栈上的变量
    let mut v3 = 0u64;
    rayon::scope(|s| {
        s.spawn(|_| v3 = (1..=100u64).map(|i| i * i).sum());
    }); // scope 返回处隐式 join：所有任务已完成

    // 版本 4（对照）：join，两个闭包直接借用 result_a/result_b
    let mut result_a = 0u64;
    let mut result_b = 0u64;
    rayon::join(|| result_a = 25 * 25, || result_b = 75 * 75 + 2 * 25 * 75 + 2 * (1..=24u64).map(|i| i * i).sum::<u64>());
    println!("v2={v2} v3={v3} a+b={} total={}", result_a + result_b, v2 + result_a + result_b);
}
```

**需要观察的现象**：v1 的打印可能出现在 v2/v3 输出之后（它没人等）；v3 里 `s.spawn(|_| v3 = ...)` 直接写主线程的栈变量而无需任何通道；版本 4 的闭包同样直接借用外部 `mut` 变量。

**预期结果**：`v2 == v3 == 338350`（前 100 个自然数平方和）。版本 3 若写成 `rayon::spawn(move || v3 = ...)` 则无法编译——`move` 会把 `v3` 移走而闭包返回 `()` 后值就丢了；不写 `move` 则违反 `'static`。这正是「取回结果必须自造等待点」的体感。版本 4 各数值待本地验证（重点是借用能力而非算术）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `join` 的闭包不需要 `'static`，而 `spawn` 的需要？用「借用检查器能看到什么」来回答。

**答案**：借用检查器只认「调用是否阻塞」这一类型层面的事实。`join` 是普通函数调用，调用返回时两支闭包必然已结束——这保证闭包借用的栈数据在整个执行期间有效，所以约束只需 `Send`。`spawn` 的调用立即返回，类型系统里没有任何东西承诺「任务先于本函数结束」，于是借用栈数据就是不安全的，只能要求闭包 `'static`。换言之：**join 把等待点写进了签名，spawn 没有。**

**练习 2**：测试 `spawn_then_join_in_worker` 里的 `assert_eq!(22, rx.recv().unwrap())` 放在 scope 之外。既然 scope 返回时任务已完成，这一行 `recv` 会不会死等？

**答案**：不会死等，只会立刻返回。scope 返回保证了任务**已完成**，`tx.send(22)` 必然已执行，消息已在通道里（或 tx 已 drop），`recv` 立即拿到 22。这是「上游更强的同步保证让下游的等待变成零成本」的常见模式。

**练习 3**：想给「每 100 毫秒刷新一次缓存」的后台任务选 API：`join`、`spawn`、`scope::spawn` 各合适吗？

**答案**：都不适合周期任务（它们都是一次性闭包，`join`/`scope` 还要求同步等待点）。一次性派发层面应选 `spawn`：任务长生、与栈无关、纯副作用。实现上通常再配 `std::thread` 定时器或 async 运行时触发重复 spawn——本练习意在划清「一次性派发 API」与「周期调度」的边界。

### 4.3 模块三：fifo 与栈序——spawn 的 LIFO 与 spawn_fifo 的 FIFO

#### 4.3.1 概念说明

spawn 并入本地 deque 栈顶，所以**同一线程上先后 spawn 的任务，无窃取时按后进先出执行**（最新的先跑——它最可能还热在 CPU 缓存里，这正是 u5-l4 讲的缓存友好取向）。有些算法（如某些图遍历、希望按提交序处理的流水线）更想要**先进先出**：`spawn_fifo` 就是为此准备的。两者唯一区别是入队方式，闭包约束、panic 语义、池保活完全一致。

棘手之处在于：底层 deque 天生两端有序（拥有者 LIFO 弹、窃取者 FIFO 偷），没法直接对拥有者改造成 FIFO。Rayon 的解法是一个精巧的**间接层 `JobFifo`**：把真正的任务推进一个 FIFO 容器，再把「这个容器本身」作为一个代理任务压进 deque。无论代理任务何时被弹出/窃取，它执行时总是从 FIFO 容器里取**最老**的任务——于是对拥有者而言顺序恒为 FIFO。

注意两点边界（官方文档 [spawn/mod.rs:26-33](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/mod.rs#L26-L33) 反复强调）：

- 顺序保证**只在不发生窃取时**成立。其他线程随时可能偷走任务，所以任何依赖顺序的正确性都要有窃取下的兜底。
- `spawn_fifo` 的作用域仅限**同一线程上的相对顺序**，不像已废弃的 `breadth_first` 选项那样改变整个池的行为。

#### 4.3.2 核心流程

`spawn_fifo` 的路径与 `spawn` 只差最后一步（[spawn/mod.rs:141-160](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/mod.rs#L141-L160)）：

```text
spawn_fifo(func)
  └─ spawn_fifo_in(func, registry)
       ├─ spawn_job(...)            // 与 spawn 完全相同（保活 + HeapJob）
       └─ match registry.current_thread()
            ├─ Some(worker) → worker.push_fifo(job_ref)
            │     └─ fifo.push(job)  // ① 真任务进本线程 JobFifo（Injector）
            │        返回指向 fifo 自身的 JobRef
            │     └─ worker.push(该代理 JobRef)  // ② 代理任务压本地 deque 栈顶
            └─ None → registry.inject(job_ref)   // 池外直接进全局注入队列
```

代理任务被执行时（`JobFifo::execute`）：从 Injector 里 steal 出**最先进去**的那个真任务并执行它。用一张图看 4.2 节 mixed 测试的队列演化（同一线程、单线程池）：

```text
依次调用: spawn(0), fifo(-1), spawn(1), fifo(-2), spawn(2), fifo(-3), spawn(3)

本地 deque（底 → 顶）:        JobFifo 容器（队首 → 队尾）:
 [fifo代理, 0, fifo代理, 1, fifo代理, 2, fifo代理, 3]     [-1, -2, -3]

拥有者从顶弹出并执行：
 pop 3        → 发送 3
 pop fifo代理 → 容器吐出队首 -1 → 发送 -1
 pop 2        → 发送 2
 pop fifo代理 → -2
 pop 1        → 1
 pop fifo代理 → -3
 pop 0        → 0
最终顺序: 3, -1, 2, -2, 1, -3, 0   ← 与测试断言的 expected 完全一致
```

要点：fifo 代理像一个「占位符」躺在 LIFO 栈里，它自己被弹出的时机遵循 LIFO，但它吐出的内容遵循 FIFO——两种顺序因此可以混用且各自独立。

#### 4.3.3 源码精读

**spawn_fifo 入口**。[spawn/mod.rs:130-160](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/mod.rs#L130-L160)：`spawn_fifo` 与 `spawn` 的差异全在尾部分派——池内走 `worker.push_fifo`，池外退回 `registry.inject`。文档 [spawn/mod.rs:109-115](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/mod.rs#L109-L115) 指出它「与已废弃的 breadth_first 类似，但效果只隔离在相对的 spawn_fifo 调用之间，不影响全池任务」，设计详见 Rayon RFC #1。

**JobFifo 的间接实现**。[rayon-core/src/job.rs:243-262](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L243-L262) 核心就两步：

```rust
pub(super) unsafe fn push(&self, job_ref: JobRef) -> JobRef {
    // 一点间接保证了 spawn 恒按 FIFO 优先执行。线程 deque 里的任务
    // 可能被本地弹（LIFO）或被偷（FIFO），但两种情况都会
    // 从本队列的队首弹出。
    self.inner.push(job_ref);
    unsafe { JobRef::new(self) }        // 返回指向 JobFifo 自身的代理
}
```

[rayon-core/src/job.rs:243-262](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L243-L262)——`JobFifo` 内部就是一个 crossbeam `Injector<JobRef>`；注释点破了设计的普适性：**无论代理任务从哪一端离开 deque，它吐出的都是队首任务**。

**「执行一个队列」**。[rayon-core/src/job.rs:264-278](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L264-L278) 让 `JobFifo` 自己实现 `Job` trait：`execute` 时循环 `steal()` 直到 `Success`，然后执行拿到的任务（`Empty` 则 panic「FIFO is empty」——每个代理对应一次 push，正常不会空）。这呼应 u5-l2 的「JobRef 是数据指针 + 执行函数指针」：队列本身也是一个可执行任务。

**每线程一条 fifo**。[rayon-core/src/registry.rs:728-737](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L728-L737)：`WorkerThread::push_fifo` 就是 `self.push(self.fifo.push(job))`——先装进本线程的 fifo，再把代理压本地 deque（并照常通知 sleep 模块有新任务）。scope 侧的 `ScopeFifo` 则自带 `fifos: Vec<JobFifo>`（每线程一个，[scope/mod.rs:31-34](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L31-L34)、[scope/mod.rs:596-618](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L596-L618)），让 FIFO 顺序按 scope 隔离——机制与 spawn_fifo 同构，只是容器挂在 scope 上而非线程上。

**四个顺序测试**。[spawn/test.rs:151-171](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/test.rs#L151-L171) 的 `test_order!` 宏用**单线程池** + `pool.install` 排除窃取干扰：外层 10 个任务各再 spawn 10 个内层任务，把编号 `i*10+j` 发进通道，最后比对序列。[spawn/test.rs:173-212](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/test.rs#L173-L212) 四个用例的断言可当规格书读：

| 测试 | 外层 × 内层 | 期望序列 |
| --- | --- | --- |
| `lifo_order` | spawn × spawn | `0..100` **反转**（最老的最后） |
| `fifo_order` | spawn_fifo × spawn_fifo | `0..100` 自然序 |
| `lifo_fifo_order` | spawn × spawn_fifo | 外层倒序、每层内部自然序 |
| `fifo_lifo_order` | spawn_fifo × spawn | 外层自然序、每层内部倒序 |

混合用例 [spawn/test.rs:223-255](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/test.rs#L223-L255) 断言 `[3, -1, 2, -2, 1, -3, 0]`（正数走 spawn、负数走 spawn_fifo）——即 4.3.2 那张推演图，说明两种顺序在同一 deque 里互不干扰。

#### 4.3.4 代码实践

**实践目标**：亲眼看到同一线程上 spawn 的 LIFO 与 spawn_fifo 的 FIFO，并理解为什么测试要用单线程池。

**操作步骤**：

1. 在仓库根目录跑官方测试：`cargo test -p rayon-core spawn::test -- --nocapture lifo fifo`（过滤出四个顺序测试与两个混合测试；`--nocapture` 可看到 panic 输出）。
2. 在自己的示例工程里复现（示例代码）：

```rust
// 示例代码
fn main() {
    let pool = rayon::ThreadPoolBuilder::new().num_threads(1).build().unwrap();
    pool.install(|| {
        for i in 0..5 {
            rayon::spawn(move || println!("spawn {i}"));
        }
        for i in 0..5 {
            rayon::spawn_fifo(move || println!("fifo  {i}"));
        }
    });
}
```

3. 把 `num_threads(1)` 改成默认线程数（删掉 `.num_threads(1)`）再跑几次。

**需要观察的现象**：单线程下 `spawn` 打印 4,3,2,1,0；`spawn_fifo` 打印 0,1,2,3,4（两组之间也满足相对顺序：spawn 组整体在 fifo 组之前入栈……实际上 fifo 组在栈顶，会先执行，即先打印 fifo 组再打印 spawn 组——注意观察这个细节）。多线程下顺序开始不稳定。

**预期结果**：单线程运行稳定复现上述顺序；多线程运行每次输出可能不同（窃取介入），偶发仍呈规整序——这印证「顺序只是优先级提示，不是协议保证」。具体打印分组顺序待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `JobFifo::push` 返回的是「指向 JobFifo 自身的 JobRef」而不是指向新任务？如果直接把新任务的 JobRef 压进 deque 会怎样？

**答案**：直接压新任务的 JobRef，它就受 deque 规则支配——本地弹出是 LIFO，FIFO 语义立刻失效。压「容器自身」作代理后，任务在容器内排队，容器无论何时、从哪端被取走执行，都只会吐出**队首**（最老）的任务，FIFO 语义与 deque 的取用方式解耦。

**练习 2**：`spawn_fifo` 从池外（例如 main 线程）调用时走哪条路？此时还有 FIFO 保证吗？

**答案**：走 `registry.inject(job_ref)` 进全局注入队列（[spawn/mod.rs:153-158](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/mod.rs#L153-L158)）。文档承诺的 FIFO 只针对「同一线程上的相对顺序」；池外调用不经本线程 fifo，任务由任意空闲 worker 从注入队列取走，只剩注入队列自身顺序 + 调度不确定性，不应依赖其相对顺序。

**练习 3**：`scope_fifo` 与 `spawn_fifo` 都实现了 FIFO，它们的 JobFifo 容器分别挂在哪？为什么 scope 版要 `Vec<JobFifo>` 每线程一条？

**答案**：`spawn_fifo` 用 `WorkerThread.fifo`，容器属于**线程**；`scope_fifo` 用 `ScopeFifo.fifos: Vec<JobFifo>`，容器属于 **scope 实例**、按下标为每个线程备一条（[scope/mod.rs:575-581](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L575-L581)）。每线程一条是因为任务的执行线程可能任意分布：任何一个 worker 拿到该 scope 的任务并继续 `spawn_fifo` 时，都要往「自己那条」fifo 里排队，才能保证该线程视角下的相对 FIFO。这也让顺序保证按 scope 隔离，互不污染。

## 5. 综合实践

**任务：把一个「派发—计算—回收」流程写成三个版本，逐版本收紧约束。**

背景：计算 1..=1000 中每个数的 `digit_sum`（数位和）之总和。要求如下。

1. **版本一（fire-and-forget）**：`rayon::spawn` 派发任务，在任务内打印结果。确认：spawn 之后主线程立刻继续，你没有任何手段拿到这个值；为了让打印有机会发生，main 结束前需要 sleep 或 channel 等待。
2. **版本二（channel 取回）**：`spawn(move || tx.send(total))` + `rx.recv()`，把结果拿回主线程打印。思考：`tx` 为什么必须 `move`？
3. **版本三（scope + 分治）**：`rayon::scope` 中用 `s.spawn` 把区间 1..=1000 分成 8 段并行求和，各段直接 `+=` 到主线程栈上的 `&mut total`——这是 spawn 永远做不到的借用写法。对照 4.1 的约束表解释为什么这里可以。
4. **顺序彩蛋**：在版本二的 scope 之外（或之内）用单线程池对比 `spawn` 与 `spawn_fifo` 各派 4 个任务的打印顺序，验证 4.3 的结论。
5. 验收：三个版本结果一致（可先笔算/串行算出期望值再断言）；能口头回答「每个版本的等待点在哪里、闭包约束是什么、任务为何能/不能借用栈数据」。

参考骨架（示例代码，需自行补全）：

```rust
fn digit_sum(mut n: u64) -> u64 { let mut s = 0; while n > 0 { s += n % 10; n /= 10; } s }

// 版本三骨架
let mut total = 0u64;
rayon::scope(|s| {
    for chunk in (1..=1000u64).collect::<Vec<_>>().chunks(125) {
        let lo = chunk[0]; let hi = chunk[chunk.len() - 1];
        s.spawn(move |_| {
            total += (lo..=hi).map(digit_sum).sum::<u64>();  // 借用主线程栈变量
        });
    }
});
assert_eq!(total, /* 串行计算结果 */);
```

注意骨架里每段的 `lo/hi` 是从借来的 `chunk` 里**拷贝**出来的——`chunk` 本身活不过本次循环迭代，直接借用会违反 `'scope`。这个细节就是「scope 放宽但不取消生命周期检查」的最好练习。

## 6. 本讲小结

- `rayon::spawn` 是纯 fire-and-forget：约束 `FnOnce() + Send + 'static`、返回 `()`、没有 JoinHandle；`Send` 因为任务可能被任意线程偷走，`'static` 因为 spawn 立即返回、任务可能在本次调用结束后才运行。
- spawn 任务自带池保活：`increment_terminate_count` → 执行（`catch_unwind` 包住用户闭包）→ `terminate()`，所以正在跑异步任务的池不会被提前释放，测试 `termination_while_things_are_executing` 是这一语义的规格。
- 等待点不会凭空出现：channel 的 `recv` 或 scope 的返回就是等待点；`join` 之所以闭包免 `'static`、还能用栈上 StackJob 零堆分配，是因为它把「等待」写进了函数签名，而 spawn 没有。
- spawn 的 panic 没有重放点：交给 `ThreadPoolBuilder::panic_handler`；无 handler 时经 `AbortIfPanic` 直接 abort 进程——与 join/scope「在调用点重放」形成对照。
- 本地队列序：`spawn` 压栈顶、同线程无窃取时 LIFO；`spawn_fifo` 通过 `JobFifo` 间接层（真任务进 Injector、代理 JobRef 压 deque）实现恒定 FIFO；两者顺序都只是优先级提示，窃取一旦发生即不保证。

## 7. 下一步学习建议

- **下一讲 u6-l3（broadcast）**：`spawn_broadcast` 是 spawn 的「每线程一份副本」亲戚，同样走 spawn 模块的保活与入队思路，但用 `ArcJob`（引用计数、可执行多次）替代 `HeapJob`，并把结果收进 `Vec` 返回——正好补上本讲「spawn 无返回值」的另一半故事。
- **单元七（自定义线程池）**：本讲的 `Registry::current()` 取池规则在多池下会变成真实陷阱；学 `ThreadPool::spawn`、`install` 与跨池死锁（u7-l2）后回看 4.1.3 的「池内取本池、池外取全局池」会有更深的体会。
- **源码延伸阅读**：Rayon [RFC #1（scope scheduling）](https://github.com/rayon-rs/rfcs/blob/main/accepted/rfc0001-scope-scheduling.md)（`spawn_fifo` 文档中引用）解释了 FIFO/LIFO 调度取舍的原始动机；[rayon-core/src/job.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs) 里 `JobFifo` 与 `StackJob`/`HeapJob`/`ArcJob` 并排读一遍，能一次看全「任务形态 × 生命周期协议」的对应关系。
