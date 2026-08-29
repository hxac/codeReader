# join：最小的并行原语

## 1. 本讲目标

本讲是单元五（rayon-core 调度内核）的第一讲。我们从整个 Rayon 中**最小、最底层**的并行原语 `join` 入手，学完后你应当能够：

1. 说出 `join(oper_a, oper_b)` 的执行协议：闭包 B 先入队、当前线程先执行 A、之后再「认领」B——理解为什么这叫工作窃取式执行。
2. 逐行读懂 `join_context` 的主干源码：`StackJob` 如何挂在栈帧上、`push`/`take_local_job`/`run_inline`/`wait_until` 各自在调用链中扮演什么角色。
3. 区分 `join` 与 `join_context`：`FnContext::migrated()` 到底在什么情况下为 `true`，并用它实际观察到一次工作窃取。
4. 解释 panic 的处理协议：为什么 A 分支 panic 之后，`join` 仍然必须等 B 分支执行完才能展开（unwind）。

## 2. 前置知识

### 2.1 闭包与它的约束

`join` 的两个参数是闭包。签名要求 `FnOnce() -> R + Send`：

- `FnOnce`：闭包按值捕获环境，只能被调用一次——任务执行一次就消费掉，符合直觉。
- `Send`：闭包**可能**被另一个线程执行（被窃取），所以它捕获的一切都必须能安全跨线程转移。
- 返回值 `RA: Send` / `RB: Send`：结果要从执行线程送回调用处。

注意这些约束是「可能被偷」这个事实在类型系统上的投影——即使实际从未发生窃取，约束也必须在编译期满足。

### 2.2 工作窃取：一分钟回顾

在 [u1-l1](u1-l1-project-overview.md) 我们已经建立了整体印象：Rayon 维护一个固定大小的工人线程池，每个工人有一个**双端队列**（deque）存放任务；工人自己从一端取活，空闲工人从别人队列的另一端「偷」活。`join` 正是这套机制的最直接暴露点：它把「把 B 挂出去给别人偷，我先做 A」这个 Cilk 式策略写成了一行 API。

### 2.3 栈帧生命周期：为什么 `StackJob` 能零堆分配

`join` 里的任务 B 并没有被 `Box` 到堆上，而是作为一个 `StackJob` 直接放在 `join_context` 的**栈帧里**。这依赖一个关键事实：`join` 在返回之前一定会等 B 执行完毕（无论 B 是本地执行还是被偷），所以「栈帧还活着」就能保证「B 的内存还活着」。这个不变量也是本讲 4.4 节 panic 协议的根源。

### 2.4 一句话预告 Latch

`Latch` 是 rayon-core 内部的一次性信号量：「任务做完了吗？」。`join` 用 `SpinLatch` 标记 B 是否完成。本讲只用到它的 `probe()`（查询）和「set 之后会唤醒等待者」这两个事实，完整剖析留给下一讲 [u5-l2](u5-l2-job-and-latch.md)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [rayon-core/src/join/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs) | `join` / `join_context` 的全部实现，共不到 190 行，本讲主战场 |
| [rayon-core/src/join/test.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/test.rs) | join 的行为验证测试：快排正确性、panic 传播、`join_context` 迁移语义 |
| [rayon-core/src/lib.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs) | crate 入口：`FnContext` 定义、`join` 的再导出 |
| [rayon-core/src/registry.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs) | 线程注册表与调度循环：本讲引用 `in_worker`、`push`、`take_local_job`、`wait_until` |
| [rayon-core/src/job.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs) | `JobRef` 与 `StackJob`：闭包如何被包装成可入队的任务 |
| [rayon-core/src/latch.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs) | `SpinLatch`：B 是否完成的标志 |
| [rayon-demo/src/fibonacci/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/fibonacci/mod.rs) | 官方基准：用 `join` 并行计算斐波那契，本讲综合实践的参考 |

按 [u1-l4](u1-l4-repo-structure-map.md) 的分层地图，本讲正式从「rayon 使用层」下沉到「rayon-core 内核层」；`registry.rs` 的调度循环细节在 [u5-l4](u5-l4-work-stealing.md) 展开，本讲只借用它的几个方法。

## 4. 核心概念与源码讲解

### 4.1 join 语义：公开 API 与执行协议

#### 4.1.1 概念说明

`join(oper_a, oper_b)` 接受两个闭包，**可能**并行地执行它们，并返回二者的结果组成的二元组 `(RA, RB)`。

「可能」是关键词：`join` 不承诺一定并行。它承诺的是一套**调度协议**——

- 若当前有空闲线程在 B 被执行完之前把 B 偷走，两个闭包就并行跑；
- 若 A 先跑完了还没人偷 B，当前线程就自己把 B 做掉，全程零线程切换。

这套「乐观并行」的设计让 `join` 的开销极低：最好的情况（没人偷）退化为一次普通的函数调用加几次原子操作，最坏的情况（B 被偷）也能立刻获得并行加速。这正是 [u1-l1](u1-l1-project-overview.md) 提到的 \( T \approx W/P + O(S) \) 理想加速模型在 API 层的落点。

#### 4.1.2 核心流程

从调用者视角看，`join` 的行为可以这样描述：

```text
join(A, B):
    场景一：当前线程不是池线程（例如 main）
        → 整个 join 包装成一个任务注入全局池，调用线程阻塞等结果
    场景二：当前线程已是池线程
        1. 把 B 包装成任务压入本线程的本地队列（对外可窃取）
        2. 立即就地执行 A
        3. A 完成后尝试取回 B：
           a. 本地队列弹出的就是 B → 就地执行 B
           b. B 已被别人偷走 → 一边等它完成一边找别的活干
        4. 返回 (A 的结果, B 的结果)
```

#### 4.1.3 源码精读

先看公开函数本体。`join` 的实现只有 13 行——它是一个纯粹的「参数适配」壳：

[join/mod.rs:93-106](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L93-L106)

```rust
pub fn join<A, B, RA, RB>(oper_a: A, oper_b: B) -> (RA, RB)
where
    A: FnOnce() -> RA + Send,
    B: FnOnce() -> RB + Send,
    RA: Send,
    RB: Send,
{
    #[inline]
    fn call<R>(f: impl FnOnce() -> R) -> impl FnOnce(FnContext) -> R {
        move |_| f()
    }

    join_context(call(oper_a), call(oper_b))
}
```

这段代码做了两件事：

1. **声明契约**（`where` 子句）：两个闭包及其返回值都必须 `Send`——这就是「数据竞争自由」在 join 层的体现，不满足直接编译失败。
2. **适配参数**：内嵌的 `call` 把「无参数闭包」包装成「接收一个 `FnContext` 参数但忽略它的闭包」，然后转发给真正干活的 `join_context`。换句话说，`join` = `join_context` + 「我不关心执行上下文」。

`join` 通过 crate 入口再导出为公共 API：

[rayon-core/src/lib.rs:84](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L84)

```rust
pub use self::join::{join, join_context};
```

再经上层 rayon crate 转一道手，用户只需 `use rayon::join`：

[src/lib.rs:117](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L117)

```rust
pub use rayon_core::{join, join_context};
```

执行协议的权威描述就在 `join` 的文档注释里，值得逐句读原文：

[join/mod.rs:23-32](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L23-L32)

> When `join` is called from outside the thread pool, the calling thread will block while the closures execute in the pool. When `join` is called within the pool, the calling thread still actively participates in the thread pool. It will begin by executing closure A (on the current thread). While it is doing that, it will advertise closure B as being available for other threads to execute. Once closure A has completed, the current thread will try to execute closure B; if however closure B has been stolen, then it will look for other work while waiting for the thief to fully execute closure B.

翻译成要点：池外调用 → 阻塞；池内调用 → 先做 A、同时把 B「挂出去广告」；A 完成后若 B 被偷，就边找活边等小偷做完。文档紧接着强调 B 可能被 A 之前弹出的嵌套任务压在栈下面（见 4.2 节认领循环）。

最后注意文档里的 **Warning about blocking I/O**（[join/mod.rs:76-84](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L76-L84)）：`join` 的设计假设闭包是 CPU 密集型的；在闭包里做阻塞 I/O 或让 A、B 互相等待（例如用 channel）可能造成死锁——因为池的并行度是固定的，所有工人都卡住时没人能推进任务。

#### 4.1.4 代码实践

**实践目标**：跑通 `join` 的经典用例——并行快速排序，验证「分治 + join」这一模式。

**操作步骤**（示例工程，来自 [u1-l3](u1-l3-first-parallel-program.md) 的独立 Cargo 项目）：

1. 把 `join/mod.rs` 文档里的快排示例抄进 `src/main.rs`（这就是官方 doctest 本身）：

```rust
fn quick_sort<T: PartialOrd + Send>(v: &mut [T]) {
    if v.len() > 1 {
        let mid = partition(v);
        let (lo, hi) = v.split_at_mut(mid);
        rayon::join(|| quick_sort(lo), || quick_sort(hi));
    }
}
```

（`partition` 的完整实现见 [join/mod.rs:62-73](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L62-L73)，此处为项目原有代码的节选。）

2. 在 `main` 里对一个 10 万元素的随机 `Vec<u32>` 调用 `quick_sort`，再与 `v.sort()` 的结果断言相等，用 `std::time::Instant` 分别计时。
3. 也可以直接在仓库里运行同一份代码的测试版本：`cargo test -p rayon-core sort`。

**需要观察的现象**：并行版本在多核机器上明显快于串行版本；排序结果与 `sort()` 完全一致。

**预期结果**：测试通过；并行快排获得接近核数级别的加速（具体加速比「待本地验证」，取决于机器）。注意 `split_at_mut` 把切片分成两半分别交给两个闭包——两个可变借用不重叠，这正是 `Send` 约束能被满足、编译器能接受的原因。

#### 4.1.5 小练习与答案

**练习 1**：`join` 的两个闭包为什么必须满足 `Send`，而返回值也必须 `Send`？

**参考答案**：因为 B **可能**被池中另一个线程窃取执行，闭包及其捕获的环境必须能安全跨线程转移（`Send`）；B 的结果在别的线程产出后要送回调用线程，同样要 `Send`。A 虽然总是就地执行，但「就地」是指就在执行 `join` 的那个线程上——若调用线程本身是池外线程，整个 `join` 会被注入池内，A 也会在池线程上跑，所以 A 同样需要 `Send`。

**练习 2**：`join(|| ..., || ...)` 在单核机器（`RAYON_NUM_THREADS=1`）上还有意义吗？

**参考答案**：仍然安全且正确，但退化为顺序执行：B 入队后无人可偷，A 做完由当前线程自己弹回 B 执行。这体现了 `join` 的「乐观并行」本质——并行与否是运行期自适应的，语义不变。

**练习 3**：为什么不把 B 用 `Box` 分配到堆上，而要放在栈帧里（提示：`StackJob`）？

**参考答案**：因为 `join` 返回前必然等待 B 完成，B 的生存期被栈帧覆盖，栈分配即可保证有效性，省去一次堆分配和释放。这也是 `join` 低开销的一部分；只有 `scope`/`spawn` 这类任务可能活过当前栈帧的 API 才需要 `HeapJob`（见 [job.rs:128-140](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L128-L140)）。

### 4.2 入队、执行、认领：join_context 的主干

#### 4.2.1 概念说明

`join_context` 是真正的实现本体，比 `join` 多给每个闭包传一个 `FnContext` 参数（4.3 节专门讲它）。主干由四步组成，对应源码里四个连续的代码段：

1. **进场**（`registry::in_worker`）：确保接下来的逻辑运行在某个池工人线程的上下文里。
2. **入队**：把 B 包成 `StackJob`，连同 `SpinLatch` 压入当前工人的本地队列——从这一刻起 B 对所有空闲工人可见、可偷。
3. **执行 A**：当前线程立刻就地执行 A（希望此时正好有人把 B 偷走）。
4. **认领 B**（claim loop）：A 完成后循环弹本地队列找 B——找到就内联执行；发现 B 已被偷走（锁存器已置位或队列已空）就进入「边等边找活」的等待路径。

这个「先推后做再认领」的顺序是工作窃取调度的标准姿势：任务先挂出去，机会窗口期内别的工人可以拿走它；窗口过后自己兜底。

#### 4.2.2 核心流程

```text
join_context(A, B):
    in_worker(|worker, injected|                      # ① 进场
        job_b = StackJob::new(B 包装, SpinLatch::new(worker))
        worker.push(job_b)                            # ② B 入队，可被窃取
        result_a = halt_unwinding(A)                  # ③ 执行 A（捕获 panic）
        if A panic: join_recover_from_panic → 等 B 完成 → 重新抛出
        while !job_b.latch.probe():                   # ④ B 还没完成？
            match worker.take_local_job():
                None  → wait_until(job_b.latch)       # 本地没活了，阻塞等待 B
                        break
                Some(job):
                    if job.id() == job_b.id() → return (a, job_b.run_inline())
                    else               → worker.execute(job)   # 干别的活
        return (result_a, job_b.into_result())        # B 已被别人做完
```

#### 4.2.3 源码精读

**① 进场**。[join/mod.rs:132](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L132) 的 `registry::in_worker(...)` 会检查当前线程身份，三种情况三种处理：

[registry.rs:494-512](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L494-L512)

```rust
pub(super) fn in_worker<OP, R>(&self, op: OP) -> R {
    unsafe {
        let worker_thread = WorkerThread::current();
        if worker_thread.is_null() {
            self.in_worker_cold(op)          // 池外线程：注入并阻塞等待
        } else if (*worker_thread).registry().id() != self.id() {
            self.in_worker_cross(&*worker_thread, op) // 别的池的线程：跨池注入
        } else {
            op(&*worker_thread, false)       // 本池工人：直接执行，injected=false
        }
    }
}
```

第一个分支 `in_worker_cold`（[registry.rs:514-538](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L514-L538)）就是文档说的「池外调用会阻塞」：把整个操作包成 `StackJob` 注入池中，主线程在 `LockLatch` 上睡等。注意它给闭包传的第二个参数是 `true`（`injected=true`）——记住这个标志，4.3 节要用。

**② 入队**。回到 join 主干：

[join/mod.rs:133-139](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L133-L139)

```rust
// Create virtual wrapper for task b; this all has to be
// done here so that the stack frame can keep it all live
// long enough.
let job_b = StackJob::new(call_b(oper_b), SpinLatch::new(worker_thread));
let job_b_ref = job_b.as_job_ref();
let job_b_id = job_b_ref.id();
worker_thread.push(job_b_ref);
```

`StackJob` 的定义在 [job.rs:72-81](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L72-L81)：它把「闭包 + 结果槽 + 锁存器」打包放在**调用者的栈帧**上；`as_job_ref`（[job.rs:97-99](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L97-L99)）把它擦除类型成一个「裸指针 + 执行函数指针」的 `JobRef`（定义见 [job.rs:27-66](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L27-L66)），才能进队列。`job_b_id` 记下 B 的身份（指针 + 执行函数的二元组，见 [job.rs:57-60](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L57-L60)），供第④步认领时比对。

`push` 本身只有三行：

[registry.rs:727-732](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L727-L732)

```rust
pub(super) unsafe fn push(&self, job: JobRef) {
    let queue_was_empty = self.worker.is_empty();
    self.worker.push(job);
    self.registry.sleep.new_internal_jobs(1, queue_was_empty);
}
```

把任务压入 crossbeam-deque 的 `Worker`，并通知睡眠模块「来了新活」——如果有工人正在睡觉就该被叫醒（睡眠协议详见 [u5-l5](u5-l5-sleep-and-wakeup.md)）。

**③ 执行 A**：

[join/mod.rs:141-146](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L141-L146)

```rust
// Execute task a; hopefully b gets stolen in the meantime.
let status_a = unwind::halt_unwinding(call_a(oper_a, injected));
let result_a = match status_a {
    Ok(v) => v,
    Err(err) => join_recover_from_panic(worker_thread, &job_b.latch, err),
};
```

注释一句话点破设计意图：「执行 A，希望这期间 B 被偷走」。`halt_unwinding` 把可能的 panic 捕获成 `Err(Box<dyn Any + Send>)` 而不是当场展开——因为栈帧上还挂着 B，必须先处理完 B 才允许 unwinding（详见 4.4 节）。

**④ 认领循环**：

[join/mod.rs:148-171](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L148-L171)

```rust
while !job_b.latch.probe() {
    let Some(job) = worker_thread.take_local_job() else {
        // Local deque is empty. Time to steal from other threads.
        worker_thread.wait_until(&job_b.latch);
        debug_assert!(job_b.latch.probe());
        break;
    };
    if job_b_id == job.id() {
        // Found it! Let's run it.
        let result_b = job_b.run_inline(injected);
        return (result_a, result_b);
    }
    worker_thread.execute(job);
}

(result_a, job_b.into_result())
```

逐行拆解：

- **循环条件** `!job_b.latch.probe()`：锁存器已置位说明 B 已经（被别人）执行完，直接落到最后一行 `into_result` 取走结果。
- **`take_local_job`**（[registry.rs:749-763](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L749-L763)）：先 `worker.pop()` 弹本地队列；空了再从本线程的注入队列（`stealer.steal()`）捞外部塞进来的任务。注意方向：本地主人从**底部**弹出（后进先出，缓存友好），窃取者从**顶部**偷（先进先出，偷的是最老最大的任务）——这是工作窃取双向队列的经典分工。
- **为什么弹出来的可能不是 B**：A 执行期间若嵌套了别的 `join`/`spawn`，会往同一队列压更多任务，B 被压在下面。循环把这些「拦路」任务逐一执行掉（`worker_thread.execute(job)`），直到挖到 B。这也是 join/mod.rs:148-152 注释明确解释的场景。
- **`run_inline(injected)`**（[job.rs:101-103](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L101-L103)）：确认弹回来的是 B 本人，直接把闭包取出来就地调用，把结果直接返回——不经结果槽、不置锁存器，最短路径。
- **`wait_until`**（[registry.rs:772-817](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L772-L817)）：本地彻底没活了，说明 B 八成被偷了。这个等待**不是干等**：`wait_until_cold` 的循环里先找本地活、再 `find_work()`（偷别人的，[registry.rs:835-844](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L835-L844)），实在没有才进入睡眠协议。「等 B 的时候顺便帮全池干活」，这是 rayon 吞吐量的重要来源。

至于 `SpinLatch`（[latch.rs:145-168](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs#L145-L168)），本讲只需要知道：它绑定了一个目标工人线程，`set()` 时若该线程在睡觉会顺带唤醒它。谁在 B 执行完时调用 `set`？答案是 `StackJob::execute`（[job.rs:116-125](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L116-L125)）：取出闭包、执行并把结果写进结果槽，然后 `Latch::set(&this.latch)` 通知全世界「B 好了」。

#### 4.2.4 代码实践

**实践目标**：用跑测试的方式验证主干源码中读到的行为。

**操作步骤**：

1. 在仓库根目录运行 `cargo test -p rayon-core join`，应命中 `join` 模块的测试（`sort`、`sort_in_pool`、`join_context_*`、`join_counter_overflow` 等）。
2. 运行 `cargo test -p rayon-core join_context -- --nocapture`，对照 [join/test.rs:88-130](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/test.rs#L88-L130) 阅读三个 `join_context` 测试（4.3 节详细分析）。
3. 源码阅读任务：拿一张纸，把 4.2.2 的伪代码与 [join/mod.rs:132-172](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L132-L172) 逐行对照，在纸上标注每一步调用的函数分别定义在哪个文件，画出一条从 `join` 到 `wait_until` 的调用链。

**需要观察的现象**：测试全部绿色；`join_context_second` 在双线程池中稳定通过（它用 `Barrier` 强制两个分支同时在场，让窃取必然发生）。

**预期结果**：你能在不看讲义的情况下复述「入队 → 执行 A → 认领 B」三步各自对应的源码行号区间。绘制调用链时若对 `take_local_job` 与 `steal` 的方向有疑问，回到 [registry.rs:744-763](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L744-L763) 的注释再读一遍。

#### 4.2.5 小练习与答案

**练习 1**：认领循环里为什么需要 `if job_b_id == job.id()` 这个比较，直接执行弹出的任务不行吗？

**参考答案**：不行。A 执行期间可能向同一队列压入了嵌套任务，B 会被压在下面；弹出的可能是这些「拦路任务」。只有比对 `JobRef::id()`（指针 + 执行函数）确认弹出的正是 B 本人时，才能走 `run_inline` 的最短路径直接拿返回值；其他任务用 `execute` 正常执行掉即可。

**练习 2**：`wait_until` 等待期间当前线程在做什么？为什么这样设计？

**参考答案**：它在循环里先弹本地队列、再尝试偷其他工人的任务、再去注入队列找活，全都空了才真正睡眠。设计动机：B 被偷后当前线程反正闲着，与其空转不如帮全池消化任务，既提高吞吐也避免无意义的自旋耗电。

**练习 3**：`take_local_job` 里 `worker.pop()` 与窃取者的 `stealer.steal()` 操作的是同一个队列的两端。为什么要从两端操作？

**参考答案**：主人从底部（栈端）弹出的是最新压入的任务，与 `join` 递归分治的局部性吻合（刚分裂出的子任务数据还在缓存里）；窃取者从顶部（队头）偷走的是最老的任务，通常是更大的子树，一次偷窃的收益更大，同时两端操作把竞争窗口减到最小，这正是 crossbeam-deque 的设计目的。

### 4.3 FnContext 与 migrated：观察工作窃取

#### 4.3.1 概念说明

`join_context` 与 `join` 唯一的差别：闭包签名从 `FnOnce() -> R` 变成 `FnOnce(FnContext) -> R`。`FnContext` 只暴露一个方法 `migrated()`——**闭包是否运行在「与提交处不同的线程」上**。

它存在的意义是可观测性：工作窃取是运行期动态行为，通常对用户完全透明；但有些算法（如带有线程局部缓存、或者需要统计负载分布的代码）想知道「我的闭包到底有没有搬家」。`FnContext` 把这个信息带出来，且它自身被刻意做成 `!Send + !Sync`（内部有 `PhantomData<*mut ()>`），只能在闭包内就地查看、不能跨线程传递。

#### 4.3.2 核心流程

`migrated` 的取值规则（对照源码可精确推导）：

| 分支 | 执行路径 | `migrated()` 的值 |
| --- | --- | --- |
| A | 总是就地执行（[join/mod.rs:142](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L142) `call_a(oper_a, injected)`） | 等于 `injected`：池外调用注入时为 `true`，池内调用为 `false` |
| B（没人偷） | 认领循环弹回，`run_inline(injected)`（[join/mod.rs:165](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L165)） | 等于 `injected`，同上 |
| B（被偷） | 窃取者执行 `StackJob::execute` → `JobResult::call` → `func(true)`（[job.rs:223-228](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L223-L228)） | 恒为 `true` |

结论：**`migrated() == true` 就是「发生过一次线程转移」的直接证据**——要么整个 `join` 被从池外注入（A、B 都迁移），要么 B 被别的工人偷走了（只有 B 迁移）。

#### 4.3.3 源码精读

两个适配闭包负责把布尔值包装成 `FnContext`：

[join/mod.rs:122-130](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L122-L130)

```rust
#[inline]
fn call_a<R>(f: impl FnOnce(FnContext) -> R, injected: bool) -> impl FnOnce() -> R {
    move || f(FnContext::new(injected))
}

#[inline]
fn call_b<R>(f: impl FnOnce(FnContext) -> R) -> impl FnOnce(bool) -> R {
    move |migrated| f(FnContext::new(migrated))
}
```

注意不对称性：`call_a` 的布尔来自外层的 `injected`（进场时已知），`call_b` 的布尔要等运行期才知道（没人偷时是 `run_inline` 传入的 `injected`，被偷时是 `func(true)`），所以 B 的闭包类型是 `FnOnce(bool) -> R`，正好匹配 `StackJob` 对闭包 `F: FnOnce(bool) -> R` 的要求（[job.rs:72-76](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L72-L76) 的注释也写明「The function parameter indicates `true` if the job was stolen」）。

`FnContext` 本体在 crate 入口：

[rayon-core/src/lib.rs:834-860](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L834-L860)

```rust
pub struct FnContext {
    migrated: bool,

    /// disable `Send` and `Sync`, just for a little future-proofing.
    _marker: PhantomData<*mut ()>,
}

impl FnContext {
    /// Returns `true` if the closure was called from a different thread
    /// than it was provided from.
    #[inline]
    pub fn migrated(&self) -> bool {
        self.migrated
    }
}
```

行为验证由三个测试完成，它们分别对应「池外注入 / 单线程池无人偷 / 双线程池强制偷」三种场景：

[join/test.rs:88-95](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/test.rs#L88-L95) —— 池外调用，两个分支都被注入池中执行，因此都 `migrated`：

```rust
fn join_context_both() {
    // If we're not in a pool, both should be marked stolen as they're injected.
    let (a_migrated, b_migrated) = join_context(|a| a.migrated(), |b| b.migrated());
    assert!(a_migrated);
    assert!(b_migrated);
}
```

[join/test.rs:97-106](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/test.rs#L97-L106) —— 单线程池里没有别的工人，B 只能被本地弹回执行，两者都 `!migrated`：

```rust
fn join_context_neither() {
    // If we're already in a 1-thread pool, neither job should be stolen.
    let pool = ThreadPoolBuilder::new().num_threads(1).build().unwrap();
    let (a_migrated, b_migrated) =
        pool.install(|| join_context(|a| a.migrated(), |b| b.migrated()));
    assert!(!a_migrated);
    assert!(!b_migrated);
}
```

[join/test.rs:108-130](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/test.rs#L108-L130) —— 双线程池 + `Barrier` 让两个分支必须同时在场上，B 必然被第二个线程偷走：`assert!(!a_migrated); assert!(b_migrated);`。这是三个测试里唯一直接证明「窃取发生」的。

#### 4.3.4 代码实践

**实践目标**：亲手制造并观察到一次工作窃取。

**操作步骤**（示例代码，写入你的示例工程）：

```rust
use rayon::{join_context, ThreadPoolBuilder};

fn main() {
    // 双线程池 + 屏障，复刻 join_context_second 的场景
    let pool = ThreadPoolBuilder::new().num_threads(2).build().unwrap();
    let barrier = std::sync::Barrier::new(2);

    let (a, b) = pool.install(|| {
        join_context(
            |ctx| {
                barrier.wait(); // 两边都到齐才放行，制造并行窗口
                (format!("A: migrated={}", ctx.migrated()),
                 format!("A: thread={:?}", std::thread::current().id()))
            },
            |ctx| {
                barrier.wait();
                (format!("B: migrated={}", ctx.migrated()),
                 format!("B: thread={:?}", std::thread::current().id()))
            },
        )
    });

    println!("{}", a.0);
    println!("{}", a.1);
    println!("{}", b.0);
    println!("{}", b.1);
}
```

**需要观察的现象**：输出形如 `A: migrated=false`、`B: migrated=true`，且 A、B 的线程 id 不同；去掉 `barrier.wait()` 后 B 的 `migrated` 结果会变得不稳定（有时 false——A 太快做完，B 还没来得及被偷就被本地弹回了）。

**预期结果**：带屏障时稳定复现 `!a_migrated && b_migrated`。多跑几次感受窃取的时序敏感性；若在单核环境或 `RAYON_NUM_THREADS=1` 下运行，B 永远不会被偷（`migrated=false`），与 `join_context_neither` 的断言一致。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `FnContext` 要用 `PhantomData<*mut ()>` 禁掉 `Send` 和 `Sync`？

**参考答案**：`FnContext` 描述的是「当前这一次执行的现场信息」，离开这个闭包就没有意义了（换个线程，`migrated` 的语义随之改变）。禁掉 `Send`/`Sync` 在编译期阻止用户把它存下来、发到别的线程再用，避免误读。

**练习 2**：在池内调用 `join_context` 时，A 分支的 `migrated()` 有可能为 `true` 吗？

**参考答案**：不能。池内调用走 `in_worker` 的第三个分支，`injected=false`，A 由 `call_a(oper_a, false)` 就地执行，恒为 `false`。只有整个 `join_context` 从池外被注入时（`in_worker_cold`），A 才会在池线程上执行并报告 `migrated=true`——但严格说那时它仍是「就地」执行的，只是调用者本人不在池里。

**练习 3**：假设你写了一个每分支都检查 `ctx.migrated()` 的递归分治算法，发现顶层调用后 A 报告了一次 `migrated=true`，其后所有递归层的 A 都是 `false`。为什么？

**参考答案**：顶层从 main（池外线程）调用，整个操作被注入池，`injected=true`，所以顶层 A 报告 `true`；此后所有递归都发生在池工人线程内部，`in_worker` 直接执行且 `injected=false`，A 恒为 `false`。这也是综合实践中「预期 A 迁移计数恰好等于 1」的推理依据。

### 4.4 panic 与另一分支的等待

#### 4.4.1 概念说明

`join` 对 panic 的承诺写在文档里：**无论发生什么，两个闭包都会被执行**；单个闭包 panic 时 `join` 以相同的 panic 值继续 panic；两个都 panic 时，以第一个闭包（A）的 panic 值为准（[join/mod.rs:86-92](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L86-L92)）。

「两个闭包都执行」不是性能考量，而是**内存安全**要求：B 是挂在 `join_context` 栈帧上的 `StackJob`，而 B 的闭包可能借用了外层栈帧的数据（比如快排例子里的 `lo`/`hi` 切片）。如果 A panic 后立刻展开当前栈帧，而 B 还在被别的线程执行、正访问那些栈上数据，就是悬垂指针。所以协议是：**先等 B 彻底结束，再重放 panic**。

#### 4.4.2 核心流程

```text
执行 A:
    halt_unwinding(A)         # 捕获 panic 载荷，不立即展开
    成功 → 继续认领 B
    panic → join_recover_from_panic:
                wait_until(B 的 latch)   # 阻塞等 B 完成（期间照常帮池干活）
                resume_unwinding(err)    # 现在才安全地重新展开
B 侧 panic:
    StackJob::execute 把载荷存进 JobResult::Panic
    认领路径 into_result / 被偷路径执行完毕后，由 JobResult 转换回返回值时重放
```

#### 4.4.3 源码精读

panic 恢复函数只有 8 行，注释直击要害：

[join/mod.rs:175-186](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L175-L186)

```rust
/// If job A panics, we still cannot return until we are sure that job
/// B is complete. This is because it may contain references into the
/// enclosing stack frame(s).
#[cold] // cold path
unsafe fn join_recover_from_panic(
    worker_thread: &WorkerThread,
    job_b_latch: &SpinLatch<'_>,
    err: Box<dyn Any + Send>,
) -> ! {
    unsafe { worker_thread.wait_until(job_b_latch) };
    unwind::resume_unwinding(err)
}
```

两步：`wait_until` 等 B 的锁存器置位（用的正是 4.2 节读过的「边等边找活」等待），然后 `resume_unwinding` 重放 panic。`#[cold]` 提示编译器这是冷路径，把优化预算留给主干。

B 侧的 panic 载荷如何回到调用者？在 [job.rs:116-125](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L116-L125) 的 `StackJob::execute` 里，`JobResult::call(func)` 用 `halt_unwinding` 捕获执行结果存成 `JobResult::Panic`；随后无论是本地认领的 `into_result`（[job.rs:105-107](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L105-L107)）还是正常路径取结果，`JobResult` 的转换都会把 `Panic` 重新抛出。于是「两个都 panic 时以 A 为准」也自然成立：A 的 panic 先在 [join/mod.rs:142-146](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L142-L146) 被捕获，B 的 panic 在等待结束后才会经 `wait_until` 之后的路径显现，A 的先重放。完整的 unwind 机制（`halt_unwinding`/`resume_unwinding`/`AbortIfPanic`）在 [u6-l4](u6-l4-unwind-and-panic-safety.md) 展开。

行为由测试锁定：

[join/test.rs:60-76](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/test.rs#L60-L76) —— 三个 `#[should_panic]` 测试验证无论 A、B 还是两者同时 panic，`join` 都以 `"Hello, world!"`（A 的载荷）panic：

```rust
#[should_panic(expected = "Hello, world!")]
fn panic_propagate_a() {
    join(|| panic!("Hello, world!"), || ());
}
```

[join/test.rs:78-86](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/test.rs#L78-L86) —— 最关键的语义测试：A panic 后 **B 仍然执行了**：

```rust
fn panic_b_still_executes() {
    let mut x = false;
    match unwind::halt_unwinding(|| join(|| panic!("Hello, world!"), || x = true)) {
        Ok(_) => panic!("failed to propagate panic from closure A,"),
        Err(_) => assert!(x, "closure b failed to execute"),
    }
}
```

#### 4.4.4 代码实践

**实践目标**：亲眼验证「A panic 时 B 照样执行完」。

**操作步骤**：

1. 运行仓库测试：`cargo test -p rayon-core panic`（命中 `panic_propagate_a/b/both` 与 `panic_b_still_executes`）。
2. 在示例工程里写一个最小复现（示例代码）：

```rust
fn main() {
    let mut x = false;
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        rayon::join(|| panic!("A 出错了"), || x = true);
    }));
    assert!(result.is_err(), "A 的 panic 应当传播出来");
    assert!(x, "尽管 A panic，B 也必须已经执行完");
    println!("B 已执行: {x}，panic 已被捕获重放");
}
```

**需要观察的现象**：程序打印 `B 已执行: true`；去掉 `catch_unwind` 直接运行则主线程以 `A 出错了` panic 退出。

**预期结果**：与 `panic_b_still_executes` 测试一致——panic 传播与「两闭包都执行」两个承诺同时成立。注意 B 里若也 panic，最终重放的是 A 的载荷（可自行改造验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `join_recover_from_panic` 必须先 `wait_until` 再 `resume_unwinding`，顺序能反过来吗？

**参考答案**：不能。`resume_unwinding` 会展开当前栈帧，而 B（`StackJob`）正挂在这个栈帧上、且其闭包可能借用外层栈数据；若还有线程在执行 B，栈帧销毁即产生悬垂访问。必须先等 B 的锁存器置位（B 彻底结束），才允许展开。

**练习 2**：`join(|| panic!(...), || panic!(...))` 两个闭包都 panic，调用者看到哪个 panic？

**参考答案**：A 的。A 先被 `halt_unwinding` 捕获，进入 `join_recover_from_panic` 等 B 完成，然后重放 A 的载荷；B 的 panic 载荷虽也存进了 `JobResult::Panic`，但 A 的重放先发生，程序在此之前已经展开退出了。这正是文档「If both closures panic, `join()` will panic with the panic value from the first closure」的实现依据。

**练习 3**：`#[cold]` 属性在这里起什么作用？

**参考答案**：告诉编译器这个函数很少被调用（panic 是异常路径），优化时可以不为它优化主干代码的布局，让正常路径的指令缓存更密集。这是 Rayon 源码里「快路径极致优化、慢路径正确即可」风格的典型细节。

## 5. 综合实践

把本讲三个模块串起来：用 `join` 实现并行斐波那契（参考 [rayon-demo/src/fibonacci/mod.rs:44-58](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/fibonacci/mod.rs#L44-L58) 的 `fibonacci_join_1_2` 基准），再用 `join_context` 统计工作窃取的实际发生情况。

demo 模块文档（[rayon-demo/src/fibonacci/mod.rs:1-14](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/fibonacci/mod.rs#L1-L14)）解释了为什么斐波那契是观察窃取的好素材：递归分裂是不平衡的——\( T(n) = T(n-1) + T(n-2) \)，\( T(n-1) \) 的工作量约是 \( T(n-2) \) 的两倍——不平衡会持续留下空闲线程去偷任务。

**实践目标**：

1. 跑通 `join` 版并行 fib 并与串行版对比计时；
2. 用 `FnContext::migrated()` 统计 B 分支被偷的次数、用 `current_thread_index()` 统计负载在线程间的分布，验证 4.2/4.3 节读到的调度行为。

**操作步骤**（示例代码，完整程序）：

```rust
use rayon::join_context;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::OnceLock;
use std::time::Instant;

/// join 调用总次数（即内部节点数）
static JOINS: AtomicUsize = AtomicUsize::new(0);
/// B 分支被迁移（窃取或注入）执行的次数
static B_MIGRATED: AtomicUsize = AtomicUsize::new(0);
/// A 分支被迁移执行的次数
static A_MIGRATED: AtomicUsize = AtomicUsize::new(0);
/// 每个工人线程执行分支的次数（负载分布直方图）
fn hits() -> &'static Vec<AtomicUsize> {
    static HITS: OnceLock<Vec<AtomicUsize>> = OnceLock::new();
    HITS.get_or_init(|| {
        (0..rayon::current_num_threads())
            .map(|_| AtomicUsize::new(0))
            .collect()
    })
}

fn record(a_or_b: &str, migrated: bool) {
    if let Some(i) = rayon::current_thread_index() {
        hits()[i].fetch_add(1, Ordering::Relaxed);
    }
    if migrated {
        if a_or_b == "A" {
            A_MIGRATED.fetch_add(1, Ordering::Relaxed);
        } else {
            B_MIGRATED.fetch_add(1, Ordering::Relaxed);
        }
    }
}

/// join_context 版斐波那契：与 join 版等价，只是记录执行上下文
fn fib(n: u32) -> u32 {
    if n < 2 {
        return n;
    }
    JOINS.fetch_add(1, Ordering::Relaxed);
    let (a, b) = join_context(
        |ctx| {
            record("A", ctx.migrated());
            fib(n - 1)
        },
        |ctx| {
            record("B", ctx.migrated());
            fib(n - 2)
        },
    );
    a + b
}

/// 串行基线，取自 rayon-demo/src/fibonacci/mod.rs 的 fib_recursive
fn fib_serial(n: u32) -> u32 {
    if n < 2 {
        n
    } else {
        fib_serial(n - 1) + fib_serial(n - 2)
    }
}

fn main() {
    const N: u32 = 32;
    const EXPECTED: u32 = 2_178_309; // 来自 rayon-demo 的校验值

    // 预热线程池，避免首次注入的建池开销污染计时
    rayon::join(|| (), || ());

    let t0 = Instant::now();
    let s = fib_serial(N);
    let t_serial = t0.elapsed();

    let t1 = Instant::now();
    let p = fib(N);
    let t_parallel = t1.elapsed();

    assert_eq!(s, EXPECTED);
    assert_eq!(p, EXPECTED);

    println!("fib({N}) = {p}");
    println!("串行:   {t_serial:?}");
    println!("并行:   {t_parallel:?}");
    println!("join 调用次数:       {}", JOINS.load(Ordering::Relaxed));
    println!("A 迁移执行次数:      {}", A_MIGRATED.load(Ordering::Relaxed));
    println!("B 迁移执行次数:      {}", B_MIGRATED.load(Ordering::Relaxed));
    for (i, h) in hits().iter().enumerate() {
        println!("线程 {i} 执行分支次数: {}", h.load(Ordering::Relaxed));
    }
}
```

**需要观察的现象**：

1. 并行版结果与串行版一致（均为 2_178_309，即 demo 里的 `FN` 常量，见 [rayon-demo/src/fibonacci/mod.rs:16-17](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/fibonacci/mod.rs#L16-L17)）。
2. `A 迁移执行次数` 几乎总是 **1**——只有顶层那次从 main 注入（4.3 节练习 3 的推理）。
3. `B 迁移执行次数` 明显大于 0，且机器核数越多越大；负载直方图显示多个工人线程都有命中。
4. 再跑一次 `RAYON_NUM_THREADS=1 cargo run --release`：`B 迁移` 归零、直方图只剩线程 0，但结果仍然正确——对应 `join_context_neither` 场景。

**预期结果**：多核机器上并行版显著快于串行版（具体加速比与 `join` 开销占比「待本地验证」；注意 demo 文档也提醒：每个任务工作量极小时 rayon 调度开销占比大，必须用 `--release` 运行才有参考价值）。若想进一步实验，可仿照 demo 的 `fibonacci_join_2_1`（[rayon-demo/src/fibonacci/mod.rs:60-74](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/fibonacci/mod.rs#L60-L74)）把小分支 F(n-2) 放在 A 的位置，对比 `B 迁移` 次数的变化并思考原因（提示：A 就地执行、B 才会被偷，分支顺序决定了「哪半边更有机会被偷走」）。

## 6. 本讲小结

- `join` 是 `join_context` 的参数适配壳（[join/mod.rs:93-106](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L93-L106)）：把无参闭包包装成忽略 `FnContext` 的闭包，所有真正的逻辑都在 `join_context` 里。
- 执行协议三步曲：**B 包装成 `StackJob` 压入本地队列 → 当前线程就地执行 A → 认领循环弹队列找 B**；B 被偷则经 `wait_until`「边等边帮全池干活」。
- `StackJob` 挂在调用者栈帧上而非堆中，靠「join 返回前必等 B 完成」这一不变量保证内存有效，这是 `join` 低开销与 panic 协议的共同根源。
- `FnContext::migrated()` 是工作窃取的可观测窗口：A 恒等于 `injected`；B 没被偷时等于 `injected`、被偷时恒为 `true`；`join_context_second` 测试用 `Barrier` 强制复现了窃取。
- panic 协议：A panic 后先 `wait_until(B)` 再 `resume_unwinding`，因为 B 可能借用当前栈帧的数据；无论哪个分支 panic，两个闭包都保证执行完毕，双 panic 时以 A 的载荷为准。

## 7. 下一步学习建议

本讲我们把 `join` 的调用链追到了 `StackJob`、`SpinLatch` 和 `WorkerThread` 的方法边界，但都只是「借用」：

- 下一讲 [u5-l2：Job 与 Latch](u5-l2-job-and-latch.md) 深入这两个类型：`JobRef` 如何用裸指针加函数指针模拟 trait 对象、`Job` 的栈/堆两种形态、`Latch` 家族（`SpinLatch`/`LockLatch`/`TickLatch`）的 set/wait 协议与各自适用场景。
- 之后 [u5-l3：Registry](u5-l3-registry.md) 讲全局线程池的惰性初始化与工人主循环，[u5-l4：工作窃取队列](u5-l4-work-stealing.md) 展开 `take_local_job`/`steal` 背后的 crossbeam-deque 与窃取循环，[u5-l5：睡眠与唤醒](u5-l5-sleep-and-wakeup.md) 解释 `push` 里那句 `sleep.new_internal_jobs` 的完整协议。
- 如果你更关心「join 之上」的世界：[u6-l1](u6-l1-scope.md) 的 `scope` 与 [u6-l2](u6-l2-spawn.md) 的 `spawn` 都建立在本讲的 `Job`/`Latch` 机制上；而 [u4-l1](u4-l1-plumbing-overview.md) 里迭代器的 `bridge` 递归正是通过 `join_context` 落到这套调度上的——读完单元五回头看 plumbing，两边的知识会合拢。
