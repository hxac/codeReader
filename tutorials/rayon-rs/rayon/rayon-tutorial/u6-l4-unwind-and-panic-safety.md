# panic 传播与 unwind 安全

## 1. 本讲目标

学完本讲，你应该能够：

- 说出一个 panic 在 Rayon 里的完整旅程：在工作线程内被 `halt_unwinding` 捕获 → 装进 `JobResult::Panic` 跨线程搬运 → 在等待方的栈上由 `resume_unwinding` 重放。
- 区分四类入口（`join` / `scope` / `broadcast` / `spawn`）的「重放点」差异，特别是 `spawn` 这类无人等待的任务：panic 交给 `panic_handler`，没配置就**中止整个进程**。
- 理解 `AbortIfPanic` 防线的逻辑：Rayon 自己的调度代码绝不允许展开穿过，「宁可中止，不可带伤运行」。
- 读懂 `tests/sort-panic-safe.rs` 如何用「Drop 计数器 + 定时 panic 扫描」验证排序在任意时刻 panic 都不泄漏、不双重释放。
- 掌握 `panic_fuse` 的止损边界：它是一个**尽力而为**的性能止损器，不是正确性机制。

## 2. 前置知识

本讲是单元六第四讲，把前面各讲零散提到的 panic 语义收拢成一条完整链路。先回顾基础：

### 2.1 Rust 的 panic 与 unwind（纯 std 层）

- **panic** 触发后默认走 **unwind（栈展开）**：从 panic 点向外逐帧退出，逐帧执行局部变量的 `Drop`，直到被 `std::panic::catch_unwind` 拦下或进程结束。展开过程中会先调用全局 **panic hook**（默认打印消息与位置）。
- `catch_unwind(f)` 返回 `thread::Result<R>`，即 `Result<R, Box<dyn Any + Send>>`——错误侧就是 **panic 载荷（payload）**，一个类型擦除的盒子。`panic!("boom")`（纯字面量）的载荷是 `&'static str`，`panic!("boom: {}", x)`（带格式化）的载荷是 `String`。
- `resume_unwind(payload)` 以给定载荷**继续展开**，且**不再经过 panic hook**——这是「搬运别人的 panic」的标准姿势，不会二次打印。
- `catch_unwind` 要求闭包 `UnwindSafe`（证明「捕获后继续用这些状态」是安全的）。Rayon 明确知道自己会把 panic 原样重放，所以用 `AssertUnwindSafe` 直接断言安全。
- 若以 `panic = "abort"` 编译，`catch_unwind` 拦不住任何东西，第一个 panic 就直接中止进程。本讲讨论的都是默认 unwind 模式。

### 2.2 已有认知坐标（来自前置讲义）

| 来自 | 已建立的结论 |
| --- | --- |
| u5-l1 | `join` 中 A panic 后必须**先等 B 执行完**才能重放；双 panic 时以 A 的载荷为准 |
| u5-l2 | `JobRef` 必须恰好执行一次否则泄漏；任务三形态 `StackJob`/`HeapJob`/`ArcJob`；`Latch::set` 恰好一次 |
| u6-l1 | `scope` 用 panic 槽存首个 panic，`maybe_propagate_panic` 在 scope 调用点重放 |
| u6-l2 | `spawn` 是 fire-and-forget，panic 没有重放点 |
| u6-l3 | 阻塞式 `broadcast` 在调用点重放恰好一个 panic；`spawn_broadcast` 每副本独立处理 |
| u2-l5 | 用户视角的 `panic_fuse`：加个「保险丝」让其他线程尽快停工 |

本讲要回答的，是这些结论背后的统一机制：**捕获在哪里发生、载荷存在哪里、又在哪个线程的哪一帧上重放**。

### 2.3 新术语

- **重放（replay）**：在另一个线程上用 `resume_unwinding` 重新抛出捕获到的 panic 载荷，让 panic 看起来像是在等待方本地发生的一样。
- **重放点**：等待该任务完成的地方（`join` 调用点、`scope` 调用点、迭代器消费者调用点）。`spawn` 没有等待点，所以没有重放点。
- **panic 安全（panic safety）**：一段代码在任意时刻被 panic 打断后，仍不违反内存安全——每个对象恰好析构一次、无泄漏、无未初始化内存暴露。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `rayon-core/src/unwind.rs` | 本讲主战场之一：`halt_unwinding` / `resume_unwinding` / `AbortIfPanic` 三件套，全文仅 31 行 |
| `rayon-core/src/job.rs` | `JobResult` 枚举与 `StackJob::execute`：捕获发生的地方，`into_return_value`：重放发生的地方 |
| `rayon-core/src/join/mod.rs` | `join_context` 的 panic 分支、`join_recover_from_panic`、`run_inline` 的直通路径 |
| `rayon-core/src/registry.rs` | `Registry::catch_unwind`（handler 或 abort）、`main_loop` 与 `wait_until_cold` 的 `AbortIfPanic` 防线 |
| `rayon-core/src/spawn/mod.rs` | `spawn_job`：无重放点任务如何包装 panic |
| `rayon-core/src/lib.rs` | `ThreadPoolBuilder::panic_handler` 的文档：默认中止的原则 |
| `tests/sort-panic-safe.rs` | 排序 panic 安全的扫描式测试（本讲第三个模块的主样本） |
| `src/slice/sort.rs` | 排序内部的 hole 守卫模式：panic 时把搬出的元素写回去 |
| `src/iter/panic_fuse.rs` / `src/iter/mod.rs` | `panic_fuse` 适配器的实现与公开文档 |

## 4. 核心概念与源码讲解

### 4.1 unwind 机制：捕获、搬运、重放的三件套

#### 4.1.1 概念说明

设想一个工作线程正在执行你 `par_iter().for_each(...)` 里的闭包，某个元素让闭包 panic 了。此时这个线程的调用栈大约是：

```text
main_loop            ← 调度主循环（持有 deque、睡眠状态等）
└─ execute(job)      ← 取出 JobRef 并执行
   └─ 用户闭包        ← panic 在这里爆发！
```

如果放任展开自然向上冒，会**穿过 `main_loop`**：线程带着半更新的本地队列与睡眠状态直接退场，线程池从此缺一个工人，任何假设「这个线程还活着」的等待者都可能永远等下去。所以铁律是：

> **用户代码的 panic 绝不允许穿过 Rayon 自己的调度栈帧。**

做法就是把展开「打包」：在离用户代码最近的地方用 `catch_unwind` 拦下，把载荷装盒（`Box<dyn Any + Send>`），存进任务的结果槽，照常置位 Latch 通知等待方；等待方拿到载荷后在自己的栈上 `resume_unwind`，让 panic 在「语义上应该发生的地方」重演。`unwind.rs` 的三件套正好对应三个角色：

| 成员 | 角色 |
| --- | --- |
| `halt_unwinding` | 捕获：把 panic 变成 `Err(载荷)`，调用点继续正常运行 |
| `resume_unwinding` | 重放：在等待方栈上以原载荷继续展开（不经过 panic hook） |
| `AbortIfPanic` | 兜底：守护 **Rayon 自身代码**，若意外展开则打印一句后 `abort()` |

#### 4.1.2 核心流程

一个会 panic 的任务，它的一生可以这样描述：

```text
用户闭包 panic（工作线程 T_worker）
  ↓ halt_unwinding = catch_unwind
载荷装盒 → JobResult::Panic(载荷) 存入 StackJob 的结果槽
  ↓
Latch::set（照常通知：'我做完了，虽然是以失败告终'）
  ↓
T_worker 回到 main_loop 继续找下一个任务（池完好无损）
  ↓
等待方（可能是另一个线程 T_waiter）看到 Latch 已置位
  ↓ 取回结果槽
into_return_value 匹配到 Panic(载荷)
  ↓ resume_unwinding(载荷)
panic 在 T_waiter 的栈上重演 → 向上冒泡（可能又被外层 join 捕获，逐级传递）
  ↓ 最终
在最初发起调用的用户线程上冒出（可被用户的 catch_unwind 捕获）
```

注意两条关键设计：

1. **捕获点与重放点分离**：捕获在工作线程，重放在等待方线程——载荷必须 `Send`，这正是 `Box<dyn Any + Send>` 的由来。
2. **panic 不影响 Latch 置位**：先捕获、后置位的顺序保证等待方永远不会因为任务 panic 而永远等待。

#### 4.1.3 源码精读

`unwind.rs` 全文只有 31 行，却是整条链路的地基。[rayon-core/src/unwind.rs:L1-L3](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/unwind.rs#L1-L3) 的模块注释点明分工：捕获/重放用于用户代码，`AbortIfPanic` 用于保护 Rayon 自身。

**捕获**——[rayon-core/src/unwind.rs:L13-L18](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/unwind.rs#L13-L18)：

```rust
pub(super) fn halt_unwinding<F, R>(func: F) -> thread::Result<R>
where
    F: FnOnce() -> R,
{
    panic::catch_unwind(AssertUnwindSafe(func))
}
```

注释说明了一个重要假设：panic 以后会用 `resume_unwinding` 传播，所以 `f` 可以被当作「异常安全」对待——这也是敢用 `AssertUnwindSafe` 抹掉 `UnwindSafe` 检查的理由。

**重放**——[rayon-core/src/unwind.rs:L20-L22](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/unwind.rs#L20-L22)：

```rust
pub(super) fn resume_unwinding(payload: Box<dyn Any + Send>) -> ! {
    panic::resume_unwind(payload)
}
```

返回类型 `!`（永不返回）提醒调用方：这一行之后的时代不存在，写在其后的代码是死代码。

**兜底守卫**——[rayon-core/src/unwind.rs:L24-L31](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/unwind.rs#L24-L31)：`AbortIfPanic` 是一个零大小的空结构体，唯一的本领在 `Drop` 里——若它在守卫范围内**因展开而被 drop**，就打印 `Rayon: detected unexpected panic; aborting` 并 `process::abort()`。用法是「先创建守卫，确认一切正常后 `mem::forget` 它」：忘了它就不会触发 Drop，守卫只对中途爆发的展开生效。

**载荷的容器**在 job.rs。[rayon-core/src/job.rs:L9-L13](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L9-L13) 定义三态结果：`None`（尚未执行）、`Ok(T)`、`Panic(Box<dyn Any + Send>)`。捕获与写入的连接点是 [rayon-core/src/job.rs:L222-L228](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L222-L228)：`JobResult::call` 用 `halt_unwinding` 执行闭包，把 `Err` 映射成 `Panic` 变体。重放的连接点是 [rayon-core/src/job.rs:L230-L240](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L230-L240)：`into_return_value` 遇到 `Panic` 就调用 `resume_unwinding`，文档注释直言「NB. This will panic if the job panicked」。

最后看捕获点在执行现场的嵌套——[rayon-core/src/job.rs:L116-L125](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L116-L125) 的 `StackJob::execute`：

```rust
unsafe fn execute(this: *const ()) {
    unsafe {
        let this = &*(this as *const Self);
        let abort = unwind::AbortIfPanic;
        let func = (*this.func.get()).take().unwrap();
        (*this.result.get()) = JobResult::call(func);   // ① 用户 panic 在此被捕获
        Latch::set(&this.latch);                        // ② 之后才置位通知
        mem::forget(abort);                             // ③ 正常路径解除守卫
    }
}
```

三行的顺序就是前面流程图的代码化：①处用户 panic 被 `JobResult::call` 内的 `halt_unwinding` 吸收，执行流**不会**被展开带走，所以②的 `Latch::set` 一定能执行到——等待方因此永远不会被 panic 吊死。若①②之间 Rayon 自己的代码（而不是用户闭包）出了问题，`abort` 守卫会按④之前的 Drop 路径中止进程。

#### 4.1.4 代码实践

**实践目标**：在纯 std 环境下亲手完成一次「捕获 → 观察载荷 → 重放」，建立对载荷类型的直觉。

1. 新建一个独立小工程（或在本讲的示例工程里加一个 bin），写入以下**示例代码**：

   ```rust
   use std::panic;

   fn main() {
       // 捕获：模拟 halt_unwinding
       let payload = panic::catch_unwind(|| panic!("boom: {}", 42)).unwrap_err();

       // 观察载荷类型：带格式化参数的 panic! 载荷是 String
       if let Some(s) = payload.downcast_ref::<String>() {
           println!("捕获到 String 载荷: {s}");
       } else if let Some(s) = payload.downcast_ref::<&str>() {
           println!("捕获到 &str 载荷: {s}");
       }

       // 重放：模拟 resume_unwinding，注意它不会再触发 panic hook、不会再打印
       panic::resume_unwind(payload);
   }
   ```

2. `cargo run` 运行。

**需要观察的现象**：先打印出一行载荷内容，随后进程以非零退出码（101）结束，但**没有**第二条默认的 panic 消息——因为 `resume_unwinding` 不经过 panic hook。

**预期结果**：输出 `捕获到 String 载荷: boom: 42`。可再把 `panic!("boom: {}", 42)` 改成不带参数的 `panic!("boom")`，观察载荷变成 `&str` 分支。

#### 4.1.5 小练习与答案

**练习 1**：为什么载荷类型是 `Box<dyn Any + Send>`，而不是某个具体的错误类型？

**答案**：`panic!` 能抛出任意类型（`panic!(my_string)`、`panic!(MyStruct)`），标准库只能用 `dyn Any` 做类型擦除来统一接收；而任务可能在 A 线程 panic、在 B 线程重放，载荷必须跨线程搬运，所以再加 `+ Send`。接收方用 `downcast_ref::<String>()` / `downcast_ref::<&str>()` 尝试还原。

**练习 2**：`resume_unwinding` 为什么不直接用 `panic!(...)` 重新抛？

**答案**：`panic!` 会走完整的 panic 流程：再次调用全局 panic hook（打印一条多余的消息）、可能受 hook 改写影响、载荷类型也要重新构造。`resume_unwinding` 是「以原载荷原地继续展开」，忠实搬运、零副作用，正适合跨线程转发。

**练习 3**：如果把 `StackJob::execute` 里的 `Latch::set` 挪到 `JobResult::call` **之前**会发生什么？

**答案**：置位本身仍会执行（因为 panic 已在 `JobResult::call` 内被捕获，不会中断 `execute`），但等待方可能在结果槽尚未写入时就观察到「已完成」，读到 `JobResult::None`——`into_return_value` 对 `None` 会 `unreachable!()` 崩溃。这正是源码坚持「先写结果、后置位」的原因。

### 4.2 panic 传播路径：四类入口的重放点

#### 4.2.1 概念说明

捕获解决了「怎么搬」，这一节回答「搬给谁」。原则只有一句话：

> **谁在等这个任务，panic 就在谁的栈上重放。**

对照四类入口：

| 入口 | 等待者 | 捕获点 | 载荷去向 | 重放点 |
| --- | --- | --- | --- | --- |
| `join` | 调用线程 | A 分支：`join_context` 内；B 分支：`StackJob::execute` | 局部变量 / `JobResult::Panic` | `join` 调用点 |
| `scope` | 调用线程 | `execute_job_closure` 内 | `ScopeBase` 的 panic 槽 | `scope` 调用点（首个抢到槽者胜出） |
| `broadcast` | 调用线程 | `StackJob::execute` | 每线程 `JobResult` | `broadcast` 调用点，恰好重放一个 |
| `spawn` / `spawn_fifo` | **没有** | `Registry::catch_unwind` 内 | 直接交给 `panic_handler` | 无重放点；无 handler 则**中止进程** |

`spawn` 为什么特殊？它是 fire-and-forget：函数立即返回，没有任何人在某个点「等它完成」，panic 无处可放。Rayon 的选择写在文档里（下面 4.2.3 精读）：交给用户注册的 `panic_handler`；若没注册，默认**中止进程**——「panic 不应无人观测」（panics should not go unobserved）。

还有一类特殊的「等待者内爆」情形：并行迭代器。`for_each` 等消费者的每一层切分本质上是一次 `join`（u4-l3 的 bridge 递归），所以深层任务 panic 后会**逐级冒泡**：最内层 `join` 捕获 → 等兄弟分支 → 重放 → 这个展开又成为上一层 `join` 某个分支里的 panic → 再捕获、再等、再重放……直到最外层在用户线程上的 `for_each` 调用处冒出。每一层都要陪兄弟分支跑完，这正是 `panic_fuse` 文档里「panic 不总能让其余迭代立刻停下」的机制根源，也是 4.4 节的伏笔。

#### 4.2.2 核心流程

以 `join` 为例（其他入口同构），panic 的三条出口路径：

```text
join(|| A, || B) 在执行：
  B 已 push 进本地队列，等待被偷或被自己认领

路径一：A panic
  halt_unwinding(A) 返回 Err
  → join_recover_from_panic：wait_until(B 的 latch)  # 必须等 B！
  → resume_unwinding(A 的载荷)                       # 在 join 调用点重放

路径二：B 被别的线程偷走执行并 panic
  小偷线程：StackJob::execute 捕获 → JobResult::Panic → latch 置位 → 小偷继续找下一个活
  本线程：  等到 latch → into_result → into_return_value → resume_unwinding
                                                        # 载荷跨线程：小偷 → 等待方

路径三：B 没被偷，本线程在队列里认领回自己执行
  run_inline 直接调用闭包（不经过 JobResult！）
  → 若 B panic，就地直接展开冒出 join_context——合法，因为 B 的
    StackJob 就在本栈帧上，且已不在任何队列里
```

A、B 同时 panic 时走的正是「路径一 + 路径二/三」的组合：A 的载荷先被捕获在手，等 B 走完后重放 A——这就是文档承诺「双 panic 时以第一个闭包的载荷为准」的实现方式。

`spawn` 一侧则简单得多：

```text
rayon::spawn(f) 在某工作线程执行 f：
  HeapJob 的包装闭包 = { registry.catch_unwind(f); registry.terminate(); }
  catch_unwind 内：halt_unwinding(f)
    ├─ Ok  → 继续执行 terminate()（归还保活计数）
    └─ Err(载荷) → 有 panic_handler？→ handler(载荷)
                  └─ 没有（或 handler 自身 panic）→ AbortIfPanic → abort 进程
```

#### 4.2.3 源码精读

**join 的官方承诺**——[rayon-core/src/join/mod.rs:L86-L92](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L86-L92)：`# Panics` 文档写明三条：两个闭包**总会**都被执行；单个闭包 panic 时 `join` 以同一载荷 panic；双 panic 时以**第一个**闭包的载荷为准。

**路径一的代码**——[rayon-core/src/join/mod.rs:L142-L146](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L142-L146)：A 的执行结果先经 `halt_unwinding` 拿到 `status_a`，`Err` 分支转入 `join_recover_from_panic`。而 [rayon-core/src/join/mod.rs:L175-L186](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L175-L186) 的注释解释了为什么必须等 B：「B 可能包含指向外围栈帧的引用」——若 A panic 后立刻展开返回，B 还在别的线程上摸着已经失效的栈内存。这个 `#[cold]` 函数体只有两步：`wait_until(job_b 的 latch)`，然后 `resume_unwinding(err)`。等待期间本线程照常帮全池干活（u5-l1 讲过的 `wait_until` 语义）。

**路径二的代码**——[rayon-core/src/join/mod.rs:L161-L166](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L161-L166)：认领循环里 `run_inline` 的注释是「Note that this could panic, but it's ok if we unwind here」——B 直接执行的 panic 无需捕获，因为它发生在调用线程自己的栈帧里，且任务已出队。而 B 被偷的情形落在 [rayon-core/src/join/mod.rs:L171](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L171)：`job_b.into_result()` 走 4.1.3 读过的 `into_return_value`，在**本线程**重放小偷捕获的载荷。

**broadcast 同构**——[rayon-core/src/broadcast/mod.rs:L116-L120](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/broadcast/mod.rs#L116-L120)：等 `CountLatch` 归零后逐个 `job.into_result()`，与 `join` 的路径二完全一致（`scope` 的 panic 槽机制见 u6-l1 精读过的 [rayon-core/src/scope/mod.rs:L703-L718](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L703-L718) 与 [rayon-core/src/scope/mod.rs:L739-L748](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L739-L748)）。

**spawn 一侧**——[rayon-core/src/spawn/mod.rs:L84-L100](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/mod.rs#L84-L100)：`spawn_job` 把用户闭包包成 `move || { registry.catch_unwind(func); registry.terminate(); }`。注意 `catch_unwind` 吞掉 panic 后 `terminate()` 仍会执行（保活计数正确归还）；若走到 abort，进程都没了，计数自然无从谈起。[rayon-core/src/spawn/mod.rs:L35-L42](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/mod.rs#L35-L42) 的 `# Panic handling` 文档写明：panic 交给 `ThreadPoolBuilder` 注册的 panic handler（若有）。

**默认 abort 的原则**——[rayon-core/src/lib.rs:L553-L566](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L553-L566)：`panic_handler` 的文档说得直白——`spawn` 类 API 没有合理的传播目标，「若未设置 handler，默认中止进程，依据的原则是 panic 不应无人观测」；handler 自己 panic 也会中止（建议在 handler 里自己 `catch_unwind`）。实现就在 [rayon-core/src/registry.rs:L373-L382](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L373-L382)：`halt_unwinding` 捕获后先立起 `AbortIfPanic` 守卫，有 handler 就调用并 `mem::forget` 守卫，没有则守卫在函数结尾 drop，进程中止。

**Rayon 自身的两道防线**——用户 panic 会被捕获重放，但如果 **Rayon 自己的调度代码** panic 了怎么办？两处 `AbortIfPanic`：

- [rayon-core/src/registry.rs:L923-L926](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L923-L926)：`main_loop` 开头的注释「工作线程不应 panic；若有则直接中止，因为线程池内部状态已损坏。注意**用户代码** panic 应被捕获并改道」，随后立起守卫、正常结束时 `mem::forget`（[L936](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L936)）。
- [rayon-core/src/registry.rs:L781-L786](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L781-L786)：`wait_until_cold`（等待锁存器时的「边等边干活」循环）开头注释解释了为什么这里更不能展开：别处的代码可能已经假设「锁存器已置位」，展开会把它变成**随机内存访问**，比中止糟糕得多。

#### 4.2.4 代码实践

**实践目标**：验证「双 panic 时 A 的载荷胜出」与「panic 之后线程池完好可用」两件事。

1. 在示例工程（`rayon = "1"` 依赖）里写入以下**示例代码**：

   ```rust
   use rayon::prelude::*;
   use std::panic;

   fn main() {
       panic::set_hook(Box::new(|_| {})); // 静音默认 hook，避免刷屏

       let payload = panic::catch_unwind(|| {
           rayon::join(
               || panic!("A"),
               || panic!("B"),
           )
       })
       .unwrap_err();
       let msg = payload
           .downcast_ref::<&str>()
           .map(|s| s.to_string())
           .or_else(|| payload.downcast_ref::<String>().cloned())
           .unwrap();
       println!("join 重放的载荷: {msg}");

       // panic 之后池子还活着吗？再派发一个正常任务
       let sum: i64 = (1..=1000).into_par_iter().sum();
       println!("线程池仍可用, sum = {sum}");
   }
   ```

2. `cargo run --release` 运行。

**需要观察的现象**：程序**不会**崩溃退出——`catch_unwind` 在最外层接住了重放；打印出的载荷是 A 的消息。

**预期结果**：输出 `join 重放的载荷: A` 与 `线程池仍可用, sum = 500500`。若把两个闭包对调（先 `panic!("B")` 后 `panic!("A")`），载荷应变成 `B`——「第一个闭包」指的是 `join` 参数顺序里的第一个。

#### 4.2.5 小练习与答案

**练习 1**：`rayon::spawn(|| panic!("oops"))` 在没有配置 `panic_handler` 的默认全局池上运行，程序会怎样？

**答案**：进程被中止（abort，通常伴随 `Rayon: detected unexpected panic; aborting`）。路径是：`HeapJob` 包装闭包 → `Registry::catch_unwind` 捕获 → 没有 handler → `AbortIfPanic` 守卫 drop → `process::abort()`。想观察或记录这类 panic，必须用 `ThreadPoolBuilder::panic_handler` 注册处理函数。

**练习 2**：为什么 A panic 后不能立刻展开返回，非要等 B 执行完？

**答案**：B 的闭包可能借用 `join` 调用者栈帧上的数据（这正是 join 免 `'static` 约束的卖点）。若 A 展开导致调用帧销毁而 B 仍在别的线程执行，B 手里就是悬垂引用。所以 `join_recover_from_panic` 先 `wait_until`（期间还帮全池干活），确认 B 落地后才重放。

**练习 3**：并行迭代器里深层任务 panic 后，为什么不会立刻传到 `for_each` 调用处，而要「拖一会儿」？

**答案**：bridge 的每层递归都是一次 `join_context`。最内层捕获 panic 后要先等它的兄弟分支跑完才重放；重放的展开又冒进上一层的某个分支，被上一层的 `halt_unwinding` 再次捕获、再次等兄弟……逐级向上，每级都陪兄弟跑完。所以其余已切分出去的任务不会被立即叫停——这正是 `panic_fuse` 要解决的问题。

### 4.3 排序的 panic 安全测试：hole 守卫与 Drop 计数扫描

#### 4.3.1 概念说明

前面两节解决了「线程池不被 panic 破坏」，这一节解决「**数据结构**不被 panic 破坏」。样本是并行排序——对 panic 安全要求最苛刻的基础设施。

排序为了性能大量使用 `ptr::read` / `ptr::write`（按位搬移，不走 borrow checker）：把元素从切片「拿出来」放进临时变量、 pivot 槽或合并缓冲区，排好再放回去。如果比较函数 `is_less` 在搬移到一半时 panic，会出现两类灾难：

- **泄漏**：某些元素只存在于临时位置，unwind 时没人负责 drop 它们；
- **双重释放**：同一元素在切片和临时位置各有一份「名义存在」，unwind 时被 drop 两次。

Rust 的线性类型语义要求每个对象**恰好析构一次**。Rayon 排序的解法是 **hole 守卫模式**：每把元素搬出切片，就建一个守卫对象记录「洞」的位置；守卫的 `Drop` 负责把元素填回去。unwind 逐帧执行 `Drop`，正好顺着守卫把所有散落的元素物归原主——panic 之后切片依然完整持有全部初始元素。

而 `tests/sort-panic-safe.rs` 的任务，就是**穷举式验证**这套守卫在任意 panic 时刻都成立。它的思路极其漂亮，值得单独学习：

1. 先用普通比较器排一遍，数出**总比较次数** `count`；
2. 然后反复重排，让比较函数在「第 N 次比较」时 panic，N 从 `count` 开始按步长递减——panic 点像探针一样**扫过排序的全过程**；
3. 每次 panic 后断言两件事：每个元素恰好 drop 一次；drop 掉的是「最新版本」的值。

#### 4.3.2 核心流程

测试的扫描参数：\( \text{step} = \max(1, \lfloor \text{count} / 10 \rfloor) \)（元素少于等于 100 时步长取 1），N 依次取 \( \text{count},\ \text{count}-\text{step},\ \text{count}-2\cdot\text{step}, \ldots \) 直到小于 step——每个数据形态约扫 10 个 panic 点。

```text
对每个 (len, modulus, has_runs) 组合、每个排序函数：
  生成随机输入（modulus 控制重复度，has_runs 制造近似有序段）
  ├─ 第一遍：计数比较器正常排序 → 得到 count
  └─ 循环（panic 点 N 从 count 递减）：
       重置 DROP_COUNTS / VERSIONS
       在独立线程里排序，比较器第 N 次比较时 panic
       （用 thread_local 的 SILENCE_PANIC 标志让自定义 hook 闭嘴）
       join 该线程（忽略 Err —— panic 已被逐级重放到此并捕获）
       断言 ①  所有 DROP_COUNTS[i] == 1   （无泄漏、无双重 drop）
       断言 ②  VERSIONS == 0              （drop 的都是最新版本）
```

断言②的原理：`DropCounter` 每参与一次比较就把自己的 `version` 加一，同时全局 `VERSIONS` 加二；drop 时把自己的 `version` 从全局里减掉。若 unwind 时错误地 drop 了一份**旧版本**的拷贝（比较计数是过期的），减掉的数就对不上，`VERSIONS` 无法归零——这个断言比「drop 一次」更精细，能抓到「填回洞里的是过期值」这类错误（sort.rs 自己的注释也强调要用 `tmp` 而非原位置参与后续比较，防止「拷回错误的值」）。

#### 4.3.3 源码精读

**排序侧的 hole 守卫**。第一处：插入排序把尾部元素读出到 `tmp`（`ManuallyDrop` 包裹，夺走自动析构权），随后建 `InsertionHole` 守卫——[src/slice/sort.rs:L66-L80](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L66-L80)，注释写明守卫的两个职责：「保护切片不被 `is_less` 的 panic 破坏」与「最后把洞填上」；若比较中途 panic，守卫被 drop，用 `tmp` 回填洞口，「保证 `v` 仍恰好持有它最初持有的每个对象各一次」。第二处：快速排序划分时把 pivot 读到栈上——[src/slice/sort.rs:L585-L590](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L585-L590)，注释「若后续比较 panic，pivot 会被自动写回切片」，`_pivot_guard` 同样是 `InsertionHole`。

第三处在并行归并。[src/slice/sort.rs:L1325-L1332](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1325-L1332) 的 `par_merge` 文档把契约写成安全条款：「即使 `is_less` 在合并过程中任意时刻 panic，本函数也会把 `left` 与 `right` 的**全部**元素拷入 `dest`（未必有序）」；实现见 [src/slice/sort.rs:L1348-L1362](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1348-L1362)：`State` 结构记录两侧剩余区间与写入位置，panic 时其 `Drop` 把剩余部分原样搬进 `dest`。注意这里的目标从「排好序」降级为「搬完整」——panic 路径上只求内存安全，不求语义正确。

**测试侧的计数器**。[tests/sort-panic-safe.rs:L17-L51](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/sort-panic-safe.rs#L17-L51)：`DropCounter` 携带值 `x`、身份 `id` 与 `version: Cell<usize>`；比较一次双方 `version` 各加一、全局 `VERSIONS` 加二（[L31-L38](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/sort-panic-safe.rs#L31-L38)）；drop 时给自己的 `DROP_COUNTS[id]` 计数并从 `VERSIONS` 减掉自己的 version（[L46-L51](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/sort-panic-safe.rs#L46-L51)）。

**扫描主体**是 [tests/sort-panic-safe.rs:L53-L116](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/sort-panic-safe.rs#L53-L116) 的 `test!` 宏：[L59-L63](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/sort-panic-safe.rs#L59-L63) 先跑一遍计数排序得到总次数；[L80-L92](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/sort-panic-safe.rs#L80-L92) 在独立线程里用 `fetch_sub` 倒计时的比较器触发 panic（`== 1` 判定保证恰好触发一次），线程的 `.join()` 结果被丢弃——panic 已经由 4.2 的传播链重放到了这个线程，`join` 返回 `Err` 即「捕获成功」；[L94-L108](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/sort-panic-safe.rs#L94-L108) 是两条断言；[L110-L113](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/sort-panic-safe.rs#L110-L113) 推进 panic 点。[L120-L164](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/sort-panic-safe.rs#L120-L164) 的入口先安装「按 `SILENCE_PANIC` 标志过滤」的 panic hook（[L118](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/sort-panic-safe.rs#L118) 定义该 thread_local），再用三重循环铺开测试矩阵：长度 1～20 000、取模 5～20 000（重复密度）、有无近似有序段，`par_sort_by` 与 `par_sort_unstable_by` 各来一遍。

#### 4.3.4 代码实践

**实践目标**：亲手跑通上游的 panic 安全扫描测试，并观察它的工作量。

1. 在仓库根目录执行：

   ```bash
   cargo test --test sort-panic-safe
   ```

2. 若想看单次运行的耗时构成，可加 `-- --nocapture`（该测试本身不打印，主要看总耗时）。

**需要观察的现象**：测试会运行较长时间（12 种长度 × 4 种取模 × 2 种数据形态 × 2 个排序函数，每个组合约 10 个 panic 点，每个点都要完整排一遍 20 000 以内的数组），最终报告通过。

**预期结果**：输出 `test sort_panic_safe ... ok`。这条测试在 stable 工具链上即可运行（u1-l2 的结论：`cargo test -p rayon` 不需要 nightly）。具体耗时依机器而定，属「待本地验证」的数值，但「通过且无卡死」是确定的。

#### 4.3.5 小练习与答案

**练习 1**：为什么测试要用 `thread::spawn(...).join()` 把排序包起来，而不是直接在当前线程调用 `par_sort_by`？

**答案**：两个作用。其一，`par_sort_by` 的 panic 会沿 4.2 的传播链在**发起线程**上重放，`join()` 返回 `Err` 正好把它捕获在一个隔离边界内，主测试循环才能继续扫描下一个 panic 点；其二，每次扫描用全新线程，排除上一次 panic 留下的线程局部状态（如 `SILENCE_PANIC`）的干扰。

**练习 2**：`par_merge` 的 panic 契约为什么只承诺「把元素全部拷入 `dest`」而不承诺「保持有序」？

**答案**：panic 路径上唯一的目标是内存安全：每个对象恰好析构一次、`dest` 指向的内存完全初始化。追求数学意义上的「部分有序」既做不到（比较已中断）也无必要——反正 panic 会上抛，调用方拿到的数组值本来就是未定义的业务结果。这是「panic 安全 ≠ panic 后语义正确」的标准取舍。

**练习 3**：如果没有 hole 守卫，`sort-panic-safe.rs` 的哪条断言会先失败？

**答案**：断言①（`DROP_COUNTS[i] == 1`）。元素被 `ptr::read` 搬出后没人负责回填或释放：泄漏的情形下某些 `DROP_COUNTS[i]` 为 0（总 drop 数不足），双重释放的情形下为 2。断言②（`VERSIONS == 0`）抓的是更隐蔽的「回填了过期值」错误，它成立的前提是断言①先成立。

### 4.4 panic_fuse 的止损边界

#### 4.4.1 概念说明

4.2 节留下的问题在这里收尾：panic 一定会传播，但 join 的语义（陪兄弟分支跑完）决定了**其余任务不会立刻停工**。如果你的管道每处理一个元素要一秒钟、共一百万个元素，其中一个元素 panic 后，其他线程还会傻乎乎地把剩下的元素都跑完——纯粹的浪费。

`panic_fuse()` 适配器就是为此设计的「保险丝」。它的思路承自 u3-l4/u2-l5 见过的「包装消费者 + 共享原子变量」骨架，但探测手段很巧妙：

> 不在用户代码里插桩，而是利用 **unwind 本身**——panic 展开穿过保险丝所在的栈帧时，帧上的 `Drop` 守卫被调用，此刻 `thread::panicking()` 为真，据此把共享标志置位。

标志一旦置位，散布在各个任务里的检查点（迭代器 `next`、plumbing 的 `full()` 短路钩子）就会让所有任务尽快「空转结束」，不再领取新元素。

必须强调它的**边界**（这是本讲的学习目标之一）：

1. **尽力而为，不是正确性机制**：标志用 `Relaxed` 序检查，只在元素边界与切分点生效；已经在执行中的长任务（比如正在 `sleep` 的闭包）无法被打断。
2. **panic 的传播路径不变**：最终的重放仍走 4.2 的标准链路，`panic_fuse` 只减少**陪跑**的工作量。
3. **有代价**：文档明说额外的同步开销「可能抑制某些优化」。
4. **对 `spawn` 出去的任务无效**：那些任务根本不在这条迭代器管道里。

#### 4.4.2 核心流程

```text
管道：(0..N).into_par_iter().panic_fuse().for_each(f)
                                    └─ 共享 AtomicBool（一开始为 false）

切分时：Fuse（=&AtomicBool 的薄包装）随 Consumer/Producer 克隆到每个任务
正常运行：每个任务的 Folder/Iter 拿着自己的 Fuse，元素一个个流过

某任务中 f panic：
  unwind 穿过该任务的栈帧 → Fuse::drop 探测 thread::panicking() == true
  → panicked 标志置位（Relaxed）

其他任务的三个检查点先后生效：
  ① PanicFuseIter::next     → 标志已置位则返回 None（串行迭代立刻枯竭）
  ② Folder::consume_iter    → take_while(!panicked) 截断喂入
  ③ Consumer::full / Folder::full → 返回 true，bridge 递归在切分点短路

最终：panic 按标准路径在 for_each 调用处重放；Reducer 照常合并已产出的部分结果
```

#### 4.4.3 源码精读

**公开文档**——[src/iter/mod.rs:L1930-L1939](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1930-L1939)：官方措辞完整复述了本讲 4.2 的结论——「并行迭代器里的 panic 总会传播给调用者，但由于 `join` 的内部语义，它们不总是能立刻停下其余迭代；本适配器付出额外同步开销的代价，努力更快停止处理其他元素」。方法本体在 [src/iter/mod.rs:L1960-L1962](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1960-L1962)，只是包一层 `PanicFuse`。

**探测核心 `Fuse`**——[src/iter/panic_fuse.rs:L17-L35](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/panic_fuse.rs#L17-L35)：`Fuse(&AtomicBool)` 可克隆（多个任务共享同一标志）；`Drop` 里 `thread::panicking()` 为真才置位——正常完成的任务 drop 自己的 Fuse 时什么也不做。这就是「让 unwind 自己按下开关」的全部实现，只有几行。

**三个检查点**：

- [src/iter/panic_fuse.rs:L184-L190](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/panic_fuse.rs#L184-L190)：`PanicFuseIter::next` 先查标志，置位则直接 `None`（`next_back` 对称处理，[L201-L207](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/panic_fuse.rs#L201-L207)）；
- [src/iter/panic_fuse.rs:L260-L262](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/panic_fuse.rs#L260-L262)：`PanicFuseConsumer::full` 返回 `self.fuse.panicked() || self.base.full()`——`full()` 正是 u4-l3 讲过的 bridge 递归短路钩子，标志置位后每个切分点都拒绝再分；
- [src/iter/panic_fuse.rs:L300-L314](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/panic_fuse.rs#L300-L314)：`consume_iter` 用 `take_while(cool)` 在喂入端截断——内层串行迭代器一旦发现标志置位就停止供给。

**驱动入口**——[src/iter/panic_fuse.rs:L50-L60](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/panic_fuse.rs#L50-L60)：`drive_unindexed` 创建那个局部 `AtomicBool`，把下游消费者包进 `PanicFuseConsumer` 再转发给上游；索引版 `drive`（[L71-L81](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/panic_fuse.rs#L71-L81)）与 `with_producer` 的回调（[L87-L114](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/panic_fuse.rs#L87-L114)）同构。注意 `Reducer` 是纯转发（[L334-L336](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/panic_fuse.rs#L334-L336)）——已产出部分结果的合并完全不受影响。

#### 4.4.4 代码实践

**实践目标**：用耗时任意的元素放大「陪跑」浪费，量化 `panic_fuse` 的止损效果。

1. 写入以下**示例代码**（改编自 `panic_fuse` 的官方文档示例）：

   ```rust
   use rayon::prelude::*;
   use std::panic;
   use std::thread;
   use std::time::{Duration, Instant};

   fn timed(f: impl FnOnce()) -> Duration {
       let start = Instant::now();
       let _ = panic::catch_unwind(f);
       start.elapsed()
   }

   fn main() {
       panic::set_hook(Box::new(|_| {})); // 静音，避免刷屏

       let plain = timed(|| {
           (0..512)
               .into_par_iter()
               .for_each(|i| {
                   thread::sleep(Duration::from_millis(10));
                   assert!(i > 0, "boom");
               });
       });
       println!("无 panic_fuse: {plain:?}");

       let fused = timed(|| {
           (0..512)
               .into_par_iter()
               .panic_fuse()
               .for_each(|i| {
                   thread::sleep(Duration::from_millis(10));
                   assert!(i > 0, "boom");
               });
       });
       println!("有 panic_fuse: {fused:?}");
   }
   ```

2. `cargo run --release` 运行（多跑几次取稳定值）。

**需要观察的现象**：两个版本都以 panic 结束（被 `catch_unwind` 捕获），但耗时差距显著——无保险丝版本要把约 512 个各睡 10 毫秒的元素全部陪跑完（耗时量级约为 \(512 \times 10\text{ms} / \text{线程数} \)）；有保险丝版本在 panic 后各任务于最近的检查点退出。

**预期结果**：`fused` 明显短于 `plain`（例如在 8 线程机器上 `plain` 约 0.6 秒上下、`fused` 约 0.1 秒以内）。方向性结论确定，具体数值随机器与线程数而变，标注「待本地验证」具体数字。可进一步把 `assert!(i > 0)` 的触发位置改到别的元素，观察 `plain` 基本不变、`fused` 略有波动。

#### 4.4.5 小练习与答案

**练习 1**：`Fuse::drop` 里为什么要判断 `thread::panicking()`，而不是无条件置位？

**答案**：`Fuse` 存在于**每个**任务的栈上，正常完成的任务也会 drop 自己的 Fuse。无条件置位会把「任务正常结束」误报成「有人 panic」，整个管道提前枯竭、结果错误。`thread::panicking()` 只有在展开途中才为真，恰好区分「因 panic 而 drop」与「自然结束而 drop」。

**练习 2**：`panic_fuse` 能不能中断一个正在 `sleep(10ms)` 中的元素处理？

**答案**：不能。检查点都在元素边界（`next` / `take_while` / `full`），任务一旦开始处理某个元素就会把它做完——包括其中的 sleep。正在睡眠的任务只能等自己醒来、在下一个边界看到标志后退出。所以实验里 `fused` 的时间下限大约是一个元素的处理时长，而不是零。

**练习 3**：为什么 `Fuse` 的读写都用 `Ordering::Relaxed` 就够了？

**答案**：这个标志只影响「还要不要继续干活」的**性能决策**，不承担任何正确性同步义务。就算某任务晚一瞬才观察到置位，多陪跑几个元素也无害；panic 本身的传播与结果正确性由 4.1/4.2 的机制独立保证。`Relaxed` 是原子序中最便宜的一档，正适合这种「晚看到没关系」的提示性标志。

## 5. 综合实践

把本讲三条主线（传播路径、池的存活、panic 安全验证）串成一个完整实验。**实践目标**：仿照 `sort-panic-safe.rs` 的扫描思路，构造一个比较函数会定时 panic 的并行排序，验证 panic 之后线程池仍可继续接受新任务、无卡死，且元素无泄漏。

在示例工程写入以下**示例代码**：

```rust
use rayon::prelude::*;
use std::panic;
use std::sync::atomic::{AtomicUsize, Ordering::Relaxed};

static DROPS: AtomicUsize = AtomicUsize::new(0);

/// 自带 Drop 计数的元素：panic 后每个元素必须恰好析构一次
#[derive(Clone, Eq, PartialEq, Ord, PartialOrd)]
struct Tracked(u32);

impl Drop for Tracked {
    fn drop(&mut self) {
        DROPS.fetch_add(1, Relaxed);
    }
}

fn main() {
    panic::set_hook(Box::new(|_| {})); // 静音
    const LEN: usize = 5_000;

    let base: Vec<Tracked> = (0..LEN as u32)
        .map(|i| Tracked((i.wrapping_mul(2_654_435_761)) % 101))
        .collect();

    // 第一遍：正常排序，统计总比较次数
    let comparisons = AtomicUsize::new(0);
    base.clone().par_sort_by(|a, b| {
        comparisons.fetch_add(1, Relaxed);
        a.cmp(b)
    });
    let total = comparisons.into_inner();

    // 第二遍：让 panic 点以 10 档扫过整个排序过程
    let step = usize::max(1, total / 10);
    let mut countdown = total;
    loop {
        DROPS.store(0, Relaxed);
        let n = AtomicUsize::new(0);
        let target = countdown;
        let mut v = base.clone();
        let caught = panic::catch_unwind(panic::AssertUnwindSafe(move || {
            v.par_sort_by(|a, b| {
                if n.fetch_add(1, Relaxed) + 1 >= target {
                    panic!("comparator boom at ~{target}");
                }
                a.cmp(b)
            });
        }));
        assert!(caught.is_err(), "第 {target} 次比较处应当 panic");

        let dropped = DROPS.load(Relaxed);
        assert_eq!(dropped, LEN, "panic 点 {target}: 元素必须恰好各析构一次");

        if countdown < step {
            break;
        }
        countdown -= step;
    }

    // 第三遍：panic 轰炸之后，线程池还活着吗？
    let mut alive: Vec<u32> = (0..10_000).map(|i| (i * 7) % 1_009).collect();
    alive.par_sort();
    assert!(alive.windows(2).all(|w| w[0] <= w[1]));

    println!("全部 {total} 次比较、10 个 panic 点检查通过：无泄漏、无卡死，线程池可用");
}
```

**操作步骤**：

1. `cargo run --release`（务必用 `--release`，否则调试构建下排序极慢）。
2. 把 `step` 改成 `usize::max(1, total / 50)` 增加扫描密度，再跑一次。
3. 把 `par_sort_by` 换成 `par_sort_unstable_by`，对比两种排序在同样扫描下是否都通过。

**需要观察的现象**：程序在十次（或五十次）人为 panic 后依然完整跑完，最终打印通过信息；没有任何一次扫描出现断言失败或卡死。

**预期结果**：最终输出「检查通过」一行。这验证了三件事：panic 按传播链在 `catch_unwind` 处被捕获（4.1/4.2）、hole 守卫让排序在任意 panic 点都不泄漏（4.3）、工作线程吞掉 panic 后回到调度循环继续干活（4.1 的「池完好无损」结论）。若扫描中 `DROPS` 断言失败，那意味着发现了 Rayon 的 bug——上游的 `sort-panic-safe.rs` 正是为此而存在的回归防线。

## 6. 本讲小结

- **捕获三件套**（`unwind.rs`）：`halt_unwinding` 把用户闭包的 panic 变成 `Err(Box<dyn Any + Send>)` 载荷；`resume_unwinding` 在等待方栈上原载荷重放（不经过 panic hook）；`AbortIfPanic` 守卫 Rayon 自身代码，意外展开即 `abort`——「宁可中止，不可带伤运行」。
- **重放点由等待者决定**（`job.rs` + `join`/`registry`/`spawn`）：`join`/`scope`/`broadcast` 有人等，panic 存入 `JobResult::Panic` 或 panic 槽，在调用点重放（join 双 panic 以 A 为准，A panic 必须先等 B 以保借用安全）；`spawn` 无人等，panic 交给 `panic_handler`，没注册就中止进程。
- **顺序铁律**：`StackJob::execute` 先写结果、后置 Latch——panic 不影响通知，等待方永不被吊死。
- **panic 安全 = 每个对象恰好析构一次**：排序用 hole 守卫（`InsertionHole`、`State`）在展开时把搬出的元素回填，`par_merge` 的契约是「panic 时也要搬完整，但不保证有序」。
- **`sort-panic-safe.rs` 的扫描法**：先数总比较次数，再让 panic 点按步长扫过全过程，用 Drop 计数与版本计数双断言验证无泄漏、无过期值析构。
- **`panic_fuse` 是止损不是保险**：靠 unwind 触发 `Fuse::drop` 置位共享 `AtomicBool`（`Relaxed`），在 `next` / `consume_iter` / `full()` 三个元素边界检查点生效；打不断正在执行的任务，传播路径本身不变。

## 7. 下一步学习建议

- **u7-l1（ThreadPoolBuilder）**：本讲反复出现的 `panic_handler` 就在那里注册——下一步应当完整学习线程池的配置面，动手给 `spawn` 的 panic 装上观测器，验证「无 handler 即 abort」。
- **u8-l2（并行归并排序）**：本讲只读了 `sort.rs` 的 panic 安全注释，归并的缓冲区管理、切分阈值与任务组织值得整讲细读。
- **u9-l4（测试体系）**：把 `sort-panic-safe.rs` 放回 rayon 的三层测试策略（单元 / 集成 / compile_fail）中理解，并尝试为自己写的并行代码补一个「定时 panic 扫描」测试。
- 源码延伸阅读：`rayon-core/src/sleep/README.md`（为何调度代码不可展开的又一处论证）与 `src/iter/panic_fuse.rs` 的 `size_hint`（fuse 后迭代器的长度声明为何保持不变）。
