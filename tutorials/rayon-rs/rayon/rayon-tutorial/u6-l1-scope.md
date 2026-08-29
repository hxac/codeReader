# scope：借用安全的作用域

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `rayon::scope` 解决的核心矛盾：**异步 spawn 任务要求 `'static`，而 fork-join 并行恰恰想借用栈上数据**——scope 是如何用「作用域结束前所有任务必然完成」这一承诺 + 生命周期 `'scope` 化解矛盾的。
2. 读懂 `Scope::spawn` 的完整链路：闭包装箱成 `HeapJob`、`CountLatch` 计数加一、`inject_or_push` 入队，以及任务执行完毕后的计数减一与 panic 收集。
3. 区分 `scope` / `scope_fifo` / `in_place_scope` / `in_place_scope_fifo` 四个变体：本地任务先执行谁（LIFO 还是 FIFO）、闭包到底跑在哪个线程上。
4. 亲手写出「多个并行任务向主线程栈上的 `&mut Vec` 写结果」的代码——这是 `rayon::spawn` 做不到、而 `scope` 的看家本领。

本讲全部源码集中在 rayon-core 的 scope 模块（789 行）及其依赖的 latch / job / registry 少数几个函数，是上一单元内核知识的第一次「用户可感知」应用。

## 2. 前置知识

### 2.1 `'static` 约束与栈借用的矛盾

Rust 里一个值要跨线程「随意存活」，通常要求 `'static`（要么是字面量/静态量，要么是拥有的堆数据）。`rayon::spawn` 是 fire-and-forget：任务丢进池后可能活到任意时刻，调用方无法等待它，所以闭包必须 `FnOnce() + Send + 'static`——**借用了调用方栈上变量的闭包一律编译失败**。

而经典的 fork-join 场景（分治、树遍历）天然想这样做：

```rust
let mut buf = vec![0; n];
// 希望：两个任务各写 buf 的一半，然后我继续用 buf
```

`join` 能做到（当前线程就地执行一半、并等待另一半），但 `join` 只能固定分两个分支。想要「循环里 spawn 任意多个任务、还能借用栈数据」，就是 `scope` 的领地。

### 2.2 生命周期作为「安全合同」

`Scope<'scope>` 带一个生命周期参数。spawn 的闭包签名是 `FnOnce(&Scope<'scope>) + Send + 'scope`：闭包里的一切借用必须**活得至少和 `'scope` 一样长**。而 `'scope` 的边界正是 `scope()` 调用返回的那一刻——因为实现保证「所有任务在 `scope()` 返回前完成」，所以任务期间访问的栈数据必然还有效。这是「运行期协议 + 编译期生命周期检查」的双重保证。

### 2.3 本讲要用的内核知识回顾（来自单元五）

| 概念 | 出处 | 一句话回顾 |
|---|---|---|
| `HeapJob` / `JobRef` | u5-l1、u5-l2 | 闭包装箱成堆上任务；`JobRef` 是「数据指针+执行函数指针」的类型擦除 |
| `CountLatch` | u5-l2 | 计数闩锁：`set()` 一次减一，减到零才算「置位」，专门为实现 scope 设计 |
| `inject_or_push` | u5-l4 | 入队路由：本池工作线程压本地 deque（LIFO），否则进全局注入队列 |
| `in_worker` | u5-l3 | 「若已在池线程就直接执行，否则把闭包注入池并阻塞等结果」 |
| `halt_unwinding` / `resume_unwinding` | u5-l1、u6 前置 | 捕获 panic 载荷跨线程搬运、在等待方重放 |
| 工作窃取 deque | u5-l4 | 本地端从**尾部**弹（LIFO，保缓存热度），窃取端从**头部**偷（FIFO） |

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [rayon-core/src/scope/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs) | 本讲主角：`Scope`/`ScopeFifo` 结构体、`scope` 等四个入口函数、spawn 与完成协议，文档注释本身是一份优秀教程 |
| [rayon-core/src/scope/test.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/test.rs) | 620 行测试：借用、顺序、panic、栈增长、broadcast 全覆盖 |
| [rayon-core/src/latch.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs) | `CountLatch`：scope 的任务计数与等待 |
| [rayon-core/src/job.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs) | `HeapJob`（scope 任务的形态）与 `JobFifo`（FIFO 变体的间接队列） |
| [rayon-core/src/registry.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs) | 本讲只用到 `in_worker` 与 `inject_or_push` 两个函数 |
| [rayon-core/src/thread_pool/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs) | `ThreadPool` 上的 `scope`/`scope_fifo`/`in_place_scope`/`in_place_scope_fifo` 四个方法 |
| [rayon-core/src/lib.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs) | 对外的再导出 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**scope 借用模型**（为什么安全）、**spawn 到作用域**（任务如何入队、如何知道全部完成）、**fifo 变体与 in_place 家族**（顺序与执行地点的差异）。

### 4.1 scope 借用模型：为什么任务可以借你的栈

#### 4.1.1 概念说明

`scope` 是一个「带围栏的任务容器」：

```rust
rayon::scope(|s| {
    s.spawn(|s| { /* 任务 1，可借用围栏外的栈数据 */ });
    s.spawn(|_| { /* 任务 2 */ });
});  // ← 围栏：返回前保证所有任务（含任务自己再 spawn 的）都已完成
```

它解决的问题是 `rayon::spawn` 的 `'static` 约束。安全论证分两层：

1. **运行期协议**（实现方的责任）：`scope()` 返回前阻塞等待所有已 spawn 的任务完成，任务再 spawn 的任务也被计入。因此任务执行期间，调用方栈帧一定还活着。
2. **编译期检查**（Rust 类型系统的责任）：spawn 的闭包必须满足 `+ 'scope`，即闭包捕获的所有引用活得不比 `'scope` 短。`'scope` 恰好止于 `scope()` 返回点，两层互相咬合。

注意一个细节：`scope` 的**围栏只约束「完成时刻」，不约束「开始时刻」**——任务可能在 `spawn()` 被调用的瞬间就开跑（文档明确说明 Task execution potentially starts as soon as `spawn()` is called，见 [rayon-core/src/scope/mod.rs:L101-L113](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L101-L113)）。所以「先 spawn 完再统一开工」这类假设是错的。

#### 4.1.2 核心流程

一个 `scope(body)` 调用的完整时间线：

```text
调用 scope(body)
  ├─ in_worker：取当前线程
  │    ├─ 已是池线程 → 直接拿 &WorkerThread
  │    └─ 不是 → 把 body 包装成 StackJob 注入全局池，调用线程阻塞等它跑完
  ├─ 构造 ScopeBase：记录 registry、CountLatch(计数=1)、panic 槽
  ├─ 执行 body(&scope)
  │    ├─ body 内可任意次 s.spawn(...)：计数 +1、任务入队
  │    └─ 任务内还能继续 s.spawn（拿到的是同一个 scope 的句柄）
  ├─ body 返回 → 计数 -1
  ├─ CountLatch::wait：计数未到 0 就等待（池内线程边等边偷活干）
  ├─ 计数到 0 → 所有任务完成
  └─ maybe_propagate_panic：有 panic 则在此重放，否则返回 body 的返回值
```

借用安全的关键不变量可以写成：

\[ \text{scope 返回} \iff \text{计数} = 0 \iff \text{所有 spawn 过的任务都已执行完毕} \]

只要这个等式成立，任务闭包里通过 `'scope` 借来的任何引用都不会悬空。

#### 4.1.3 源码精读

先看数据结构。`Scope` 本体几乎是空壳，真正的东西在 `ScopeBase`：

- [rayon-core/src/scope/mod.rs:L24-L26](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L24-L26)：`Scope<'scope>` 只包一个 `base: ScopeBase<'scope>`，`'scope` 参数就挂在这里。
- [rayon-core/src/scope/mod.rs:L36-L54](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L36-L54)：`ScopeBase` 三个字段各司其职：
  - `registry`：这个 scope 的任务发到哪个池；
  - `panic: AtomicPtr<Box<dyn Any + Send>>`：第一个 panic 的存放槽，等 scope 结束重放；
  - `job_completed_latch: CountLatch`：任务计数闩锁，实现「围栏」的核心；
  - `marker: PhantomData<Box<dyn FnOnce(&Scope<'scope>) + Send + Sync + 'scope>>`：幽灵数据，作用是把 `'scope` 编进类型（影响协变性与 drop 检查），运行期零开销。注释还解释了为何 `Scope` 可以安全地实现 `Sync`：闭包只是被**移动**到别的线程执行，并不需要真的 `Sync`。

入口函数 `scope`：

- [rayon-core/src/scope/mod.rs:L277-L286](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L277-L286)：注意两点——`OP` 要求 `+ Send`（因为 body 可能被搬进池线程执行）；`in_worker` 返回的 `owner_thread` 被传给 `Scope::new`，决定了等待方式是「自旋帮工」还是「阻塞睡眠」（见 4.2）。
- [rayon-core/src/registry.rs:L951-L967](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L951-L967)：模块级 `in_worker`：已在任一池线程就直接执行；否则交给全局 registry 处理（注入+阻塞，即 u5-l3 读过的 `in_worker_cold`）。

**借用规则**在文档里有一组经典示例（`ok`/`bad`）：

- [rayon-core/src/scope/mod.rs:L177-L199](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L177-L199)：`ok` 声明在 scope 之外，活得比 `'scope` 长，任务可以借；`bad` 声明在 body 内部，任务借它就编译失败。
- 后续三段（L201-L265）给出三种解法：`move` 整体拿走所有权、先造影子引用 `let ok = &ok;` 再 `move`、或在闭包内 `let bad = bad;` 单独转移一个变量。共同思想：**共享引用 `&T` 可以复制给多个任务，所有权只能给一个**。

最后是 spawn 闭包如何「回指」scope——任务签名是 `FnOnce(&Scope<'scope>)`，任务体里还能继续 spawn：

- [rayon-core/src/scope/mod.rs:L537-L539](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L537-L539)：`BODY: FnOnce(&Scope<'scope>) + Send + 'scope`——这就是编译期那份「安全合同」。
- [rayon-core/src/scope/mod.rs:L772-L789](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L772-L789)：`ScopePtr`——任务需要把 `&Scope` 存进堆上的 `HeapJob`，但 `&Scope<'scope>` 的生命周期没法直接写进任务类型，于是存裸指针、用 `unsafe impl Send/Sync` 放行（注释指出裸指针的 `!Send` 只是 lint 不是安全问题）。安全性依据正是 4.1.1 的不变量：**任务执行时 scope 必然还没结束，指针不可能悬空**。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证 scope 的借用边界——什么样的栈数据能借、什么样的不能。
2. **操作步骤**（在 u1-l3 创建的示例工程中，或新建一个小项目）：
   - 把下面示例代码写入 `src/main.rs` 并运行：

     ```rust
     // 示例代码：改写自 scope 文档注释
     use rayon::scope;

     fn main() {
         let ok: Vec<i32> = vec![1, 2, 3];
         scope(|s| {
             let bad: Vec<i32> = vec![4, 5, 6];
             s.spawn(move |_| {
                 println!("ok: {:?}", ok); // ok 在 scope 外声明，可以 move 或借用
                 println!("bad: {:?}", bad); // 必须 move（见下）
             });
         });
     }
     ```

   - 第一步先**去掉 `move`** 再 `cargo build`，读编译错误（借用局部变量 `bad` 不满足 `'scope`，预期是 E0373「closure may outlive the current function」一类，具体报错文本待本地验证）。
   - 第二步改回 `move`，构建运行。
   - 第三步：把 `ok` 的使用改成两个任务**共享借用**——先 `let ok = &ok;` 影子化，再两个 `move` 闭包各自捕获这份 `&Vec<i32>`，验证可以编译。
3. **需要观察的现象**：无 `move` 时编译失败的位置与错误码；`move` 后两个任务共享 `&ok` 不冲突。
4. **预期结果**：所有权被 move 后原变量不可再用；共享引用可被任意多任务同时持有。这与 u2 阶段「共享读要 `Sync`、按值要 `Send`」的规则一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `rayon::spawn` 的闭包必须 `'static`，而 `scope` 内 spawn 的闭包只需 `'scope`？

**答案**：`rayon::spawn` 是 fire-and-forget，任务完成时刻没有任何上限，调用方栈帧可能早已销毁，所以只能引用永远活着的数据（`'static`）。`scope` 用「返回前等齐所有任务」的运行期协议把任务寿命压进 `'scope` 区间，编译器据此允许闭包借用一切活得不比 `'scope` 短的数据。

**练习 2**：下面代码能编译吗？为什么？

```rust
// 示例代码
let mut v: Vec<i32> = vec![];
rayon::scope(|s| {
    for i in 0..4 {
        s.spawn(move |_| v.push(i));
    }
});
```

**答案**：不能。循环里四个闭包都想 `move` 走 `v`，但所有权只有一个；第一个 `move` 之后 `v` 已被消耗，后续循环再 move 会报 use of moved value。改法：用 `&Mutex<Vec<i32>>` 共享借用，或预先 `vec![0; 4]` 再让每个任务写自己的下标（见第 5 节综合实践）。

**练习 3**：`Scope` 实现了 `Sync`，但它装的闭包并不要求 `Sync`，为什么是安全的？

**答案**：见 [rayon-core/src/scope/mod.rs:L48-L53](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L48-L53) 的注释——闭包只会被**移动**到某个线程执行一次（`FnOnce`），从不会被多线程同时调用，`Sync`（可共享引用调用）根本不会被用到；`PhantomData` 只是把 `'scope` 编进类型。

### 4.2 spawn 到作用域：计数闩锁与完成协议

#### 4.2.1 概念说明

围栏要能「等齐所有任务」，必须回答三个问题：

1. **何时知道任务数？** 任务是动态 spawn 的（任务还能再 spawn 任务），所以不能事先数好，只能动态计数：spawn 一次 +1，完成一次 −1。
2. **怎么等？** 池内线程等待时不该傻睡（那会浪费一个工人），要边等边帮全池干活；池外调用线程才真正阻塞睡眠。
3. **panic 怎么办？** 任务在别的线程 panic，不能就地展开（会把池搞坏），要把载荷搬运回 scope 调用点统一重放；且文档承诺：**一旦 spawn，任务必然执行**，即使别的任务先 panic 了。

还有一个性能注脚（文档 L63-L68）：scope 的任务必须**堆分配**（`HeapJob`），而 `join` 的闭包可以活在栈上（`StackJob`），所以官方建议「能用 join / 并行迭代器就别用 scope」——scope 买来的是灵活性，不是速度。

#### 4.2.2 核心流程

计数闩锁维护的核心不变量：

\[ \text{counter} = 1\;(\text{scope body 自身}) + \text{存活的任务数} \]

初始为 1（把 body 也算一个「任务」）；每个 `spawn` 先 `increment` 再入队；每个任务（以及 body）执行完都 `set`（减一）；当某次 `set` 发现旧值是 1（减完变 0），才真正置位内部闩锁、唤醒等待者。伪代码：

```text
spawn(body):
    CountLatch.increment()          # 计数 +1（必须先加再入队！）
    job = HeapJob::new(包装 body)   # 堆分配，闭包 + ScopePtr
    registry.inject_or_push(job)    # 本池线程 → 本地 deque 尾部；否则全局注入

任务执行包装器 execute_job_closure(f):
    r = halt_unwinding(f)           # 捕获 panic，不让它在池线程展开
    if r 是 Err: job_panicked(err)  # 抢第一个槽位存起来
    CountLatch.set()                # 计数 -1，减到 0 则置位闩锁并唤醒

complete(body):
    r = execute_job_closure(body)   # body 也是"任务"，set 一次
    CountLatch.wait(owner)          # 未到 0：池内边干边等 / 池外睡眠
    maybe_propagate_panic()         # 有 panic → 在调用点重放
    return r
```

顺序上的一个要点：`increment` 发生在入队**之前**，所以不存在「任务已被别的线程执行完、计数却还没加上」的竞态；`increment` 里的 `debug_assert!(old_counter != 0)` 则防的是「scope 已经结束还在 spawn」这种逻辑错误。

#### 4.2.3 源码精读

**spawn 一侧**：

- [rayon-core/src/scope/mod.rs:L537-L553](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L537-L553)：`Scope::spawn` 全貌。`ScopePtr(self)` 捕获 scope 指针；`HeapJob::new` 把包装闭包装箱；包装闭包做的事就是解引用 `scope_ptr` 然后调 `ScopeBase::execute_job`（跑 body + 计数减一）。最后 `inject_or_push` 入队——注释解释了为什么不能直接压本地队列：`Scope` 是 `Sync` 的，句柄可能被带到**别的池**的线程上调用 spawn，必须查 registry 身份。
- [rayon-core/src/job.rs:L134-L152](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L134-L152)：`HeapJob`——所有权随 `Box` 转移，执行它的线程负责释放（对照 u5-l2 的三种 Job 形态）。
- [rayon-core/src/registry.rs:L412-L421](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L412-L421)：`inject_or_push`——当前线程是**本池**工作线程就 `push` 进本地 deque（尾部，本地 LIFO），否则进全局 `inject` 队列（u5-l4 读过的路由）。
- [rayon-core/src/scope/mod.rs:L656-L664](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L656-L664)：`heap_job_ref`——`increment` 与 `into_job_ref` 的组合点，即「先计数、后交出任务」的顺序保证所在。

**执行与完成一侧**：

- [rayon-core/src/scope/mod.rs:L703-L718](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L703-L718)：`execute_job_closure`——每个任务（含 body）的统一包装：`halt_unwinding` 捕获 panic、`job_panicked` 存储、`Latch::set` 计数减一。注意 `set` 用 `SeqCst` 的 `fetch_sub`，且注释提醒置位后 `this` 可能立即失效（等待方可以马上返回并销毁 scope）。
- [rayon-core/src/scope/mod.rs:L720-L737](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L720-L737)：`job_panicked`——用 CAS 抢 `panic` 槽：**第一个 panic 胜出**，后来者发现自己的指针丢掉（`ManuallyDrop` 精细处理避免双重释放）。这就是「多个 panic 时传播哪一个不确定」的来源。
- [rayon-core/src/scope/mod.rs:L681-L689](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L681-L689)：`complete`——body 执行、`job_completed_latch.wait(owner)` 等齐、`maybe_propagate_panic` 重放、返回结果。`result.unwrap()` 的注释点明：能走到 unwrap 说明没有 panic（有 panic 的话上一步已经重放展开出去了）。
- [rayon-core/src/scope/mod.rs:L739-L748](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L739-L748)：`maybe_propagate_panic`——`swap` 取走 panic 槽，`resume_unwinding` 在**调用 scope 的线程**上重放。

**CountLatch 三件套**（u5-l2 曾侧面看过，这里对上用法）：

- [rayon-core/src/latch.rs:L319-L328](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs#L319-L328)：结构注释说得直白——`set()` 不一定置位，只是减计数，减到零才算 set。
- [rayon-core/src/latch.rs:L363-L382](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs#L363-L382)：`with_count`——按 `owner` 是否存在选择两种形态：`Stealing`（在池线程上创建：等待时参与工作窃取）或 `Blocking`（非池线程创建：睡在 `LockLatch` 条件变量上）。这正对应 4.1.2 流程图里「body 跑在哪」的分岔。
- [rayon-core/src/latch.rs:L390-L404](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs#L390-L404)：`wait`——Stealing 走 `owner.wait_until(latch)`（u5 读过的「边等边帮工」循环）；Blocking 走 `LockLatch::wait` 睡眠。
- [rayon-core/src/latch.rs:L407-L429](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/latch.rs#L407-L429)：`Latch::set`——`fetch_sub(1, SeqCst) == 1` 即旧值为 1、减后为 0，才置位内部闩锁并（对 Stealing）唤醒 `notify_worker_latch_is_set`。`Stealing` 变体还保存了 registry 与 worker_index，用于「A 池线程在 B 池上开 scope」这种跨池唤醒（为 u7-l2 埋好伏笔）。

**测试佐证**：

- [rayon-core/src/scope/test.rs:L23-L36](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/test.rs#L23-L36)：`scope_two`——两个任务对**同一个栈上原子变量**累加，scope 返回后断言 11。这就是「借栈」的最小验证。
- [rayon-core/src/scope/test.rs:L213-L227](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/test.rs#L213-L227)：`panic_propagate_still_execute_1`——job A panic 后，job B **仍然执行**（`x = true` 被断言）。同系列 `_2/_3/_4`（L229-L275）覆盖「先 spawn 的先 panic」「body panic 后已 spawn 任务照跑」等排列，坐实文档承诺。
- [rayon-core/src/scope/test.rs:L39-L59](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/test.rs#L39-L59)：`divide_and_conquer`——任务内继续 `scope.spawn` 的递归分治，与串行版对比叶子计数，验证「孙子任务也被围栏罩住」。
- [rayon-core/src/scope/test.rs:L92-L107](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/test.rs#L92-L107)：`update_in_scope`——树节点把 `&mut children` 的每个子树借用给不同任务，`op(value)` 在本线程就地执行。这是「互不相交的 `&mut` 切片分给不同任务」的官方范式，综合实践会复用它。

#### 4.2.4 代码实践

1. **实践目标**：用仓库自带测试验证完成协议与 panic 语义，再做一个计数观察实验。
2. **操作步骤**：
   - 在仓库根目录运行：`cargo test -p rayon-core -- scope`（按名字过滤 scope 模块的测试）。
   - 重点阅读 `panic_propagate_still_execute_1..4`（test.rs L213-L275）与 `scope_spawn_broadcast_panic_one`（L586-L601）的断言。
   - 然后在示例工程里写一个小实验（示例代码）：

     ```rust
     use rayon::scope;
     use std::sync::atomic::{AtomicUsize, Ordering};

     fn main() {
         let counter = AtomicUsize::new(0);
         scope(|s| {
             for i in 0..100 {
                 let counter = &counter; // 每轮复制一份共享引用，move 进任务
                 s.spawn(move |s1| {
                     counter.fetch_add(1, Ordering::SeqCst);
                     if i == 0 {
                         // 任务内再造任务：围栏必须连它一起等
                         s1.spawn(move |_| counter.fetch_add(1000, Ordering::SeqCst));
                     }
                 });
             }
         });
         println!("total = {}", counter.load(Ordering::SeqCst));
     }
     ```

3. **需要观察的现象**：测试全绿；实验输出 `total = 1100`——100 个任务各 +1，第一个任务额外 spawn 的孙任务再 +1000。注意内层 `s1.spawn` 的闭包直接 `move` 走 `counter`（`&AtomicUsize` 是 `Copy`，外层先用过也没关系），正是 4.1 借用规则的活用。
4. **预期结果**：若把内层 `s1.spawn` 去掉注释外的任何等待逻辑，总数不变——因为围栏已经保证孙任务完成。多线程下任务**执行顺序**乱序，但总数确定（待本地验证运行耗时与线程分布）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `heap_job_ref` 里必须**先** `increment` **再**把任务交出入队？

**答案**：入队后任务立刻可能被其他线程取走执行；若先入队后计数，执行线程可能在计数加一之前就 `set` 减一，计数被减穿、围栏提前放行，等待方会在任务未完成时返回——借用安全不变量被打破。先加后发保证计数永远不小于存活任务数。

**练习 2**：scope body 本身 panic 了（还没 spawn 任何任务），会发生什么？

**答案**：`complete` 里 body 也是经 `execute_job_closure` 执行的（[rayon-core/src/scope/mod.rs:L681-L689](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L681-L689)），panic 被 `halt_unwinding` 捕获存入槽、计数从 1 减到 0、围栏放行（已 spawn 的任务照常执行完），最后 `maybe_propagate_panic` 在调用点重放——对应测试 `panic_propagate_still_execute_3`（body panic 前已 spawn 的任务仍执行）。

**练习 3**：`scope()` 从一个普通主线程调用时，CountLatch 是 `Stealing` 还是 `Blocking`？body 实际跑在哪？

**答案**：`Blocking` 形态与否取决于构造时 `owner` 参数，而 `scope()` 先过 `in_worker`：主线程不是池线程时 body 被注入池内执行，`Scope::new(Some(owner_thread), None)` 拿到的是**池内**线程，所以 CountLatch 是 `Stealing`、由池线程在 `wait_until` 里边帮工边等；主线程则阻塞在 `in_worker_cold` 的 LockLatch 上等 body 的结果。

### 4.3 fifo 变体与 in_place 家族

#### 4.3.1 概念说明

**顺序问题**。本地 deque 是 LIFO（尾部弹），理由是「最新 spawn 的任务数据大概率还在缓存里」，而其他线程从头部偷走「最旧」的任务（[rayon-core/src/scope/mod.rs:L166-L175](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L166-L175)）。但有些算法（如按层遍历）更希望本地也按 spawn 顺序执行——`scope_fifo` / `spawn_fifo` 提供逐作用域的 FIFO 开关（取代了废弃的全局 `breadth_first` 选项，设计见 RFC #1）。两种顺序**只在没有窃取时严格成立**，多线程下只保证「同一线程同 scope 内 spawn 的相对顺序」这一优先级语义。

**地点问题**。`scope` 永远把 body 送进池执行（`in_worker`）；`in_place_scope` 则让 body 留在**调用线程**上执行，只有 spawn 出的任务进池。由此带来两个可见差异：

1. `in_place_scope` 的 `OP` **没有 `+ Send` 约束**（body 不跨线程，自然不需要）；
2. 闭包里可以安全使用调用线程的 thread-local 状态（文档也提醒 `scope` 的 body 在池里跑、thread-local 值可能「出乎意料」）。

**池路由问题**。文档（[rayon-core/src/scope/mod.rs:L384-L393](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L384-L393)）明确：只有通过 scope 句柄（`spawn`/`spawn_fifo`/`spawn_broadcast`）派的任务进 scope 的池；body 里调用的 `join`、`rayon::spawn`、并行迭代器走的是**调用者所在池或全局池**。这个区分在自定义 `ThreadPool` 上开 scope 时至关重要（u7-l2 的跨池死锁就埋在这）。

#### 4.3.2 核心流程

FIFO 的实现思路很巧：**不改变 deque 的 LIFO 本性，而是在任务外面再包一层队列**。

```text
spawn_fifo(job):
    若当前线程属于本池:
        job_ref' = fifos[当前线程下标].push(job)   # 把 job 压进该线程专属的小队列，
        worker.push(job_ref')                     # 再把"队列本身"作为一个任务压进 deque
    否则:
        registry.inject(job)                      # 外部线程直接全局注入（无 FIFO 效果）

执行到 JobFifo 这个"任务"时:
    从小队列头部取出最早的那个真实任务执行     # 无论本地弹还是被偷，取的都是最旧的
```

于是同一 scope 同一线程 spawn 的任务，无论从 deque 哪端取出，真正执行的都是**最早入队**的那个——本地相对顺序从 LIFO 翻转成 FIFO，而全局调度结构一点没动。

#### 4.3.3 源码精读

- [rayon-core/src/scope/mod.rs:L31-L34](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L31-L34)：`ScopeFifo` = `ScopeBase` + `fifos: Vec<JobFifo>`，**每线程一个**专属队列。
- [rayon-core/src/scope/mod.rs:L575-L581](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L575-L581)：`ScopeFifo::new` 按 `registry.num_threads()` 建队列。
- [rayon-core/src/scope/mod.rs:L596-L618](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L596-L618)：`spawn_fifo` 的路由——`registry.current_thread()` 是「当前线程**且属于本池**」才走 fifo（对照 `spawn` 用的 `inject_or_push`，两者判断的是同一件事，只是一个要走专属队列）。
- [rayon-core/src/job.rs:L243-L262](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L243-L262)：`JobFifo::push`——压入内部 `Injector`，返回「指向自己」的 `JobRef`。注释直述原理：deque 两端无论怎么取，最终都从这个队列的**前端**拿。
- [rayon-core/src/job.rs:L264-L278](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L264-L278)：`Job for JobFifo`——「执行一个队列 = 执行队列里的第一个任务」。这就是 u5-l2 提过的「把队列自身变成任务」的第四种 Job 形态在实战中的用途。
- [rayon-core/src/scope/mod.rs:L366-L375](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L366-L375)：`scope_fifo` 入口——与 `scope` 只差 `ScopeFifo::new`。
- [rayon-core/src/scope/mod.rs:L405-L419](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L405-L419)：`in_place_scope` / `do_in_place_scope`——注意签名差异：`OP: FnOnce(&Scope<'scope>) -> R`，**无 Send**。`do_in_place_scope` 是 `ThreadPool::in_place_scope` 的共用后端（可传指定 registry）。
- [rayon-core/src/scope/mod.rs:L421-L433](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L421-L433)：`get_in_place_thread_registry`——当前线程不是池线程且未指定 registry 时，先 `global_registry()`（可能触发全局池懒初始化）再重查自身；注释点出 WebAssembly 上新全局池可能复用当前线程，所以必须复查。
- [rayon-core/src/thread_pool/mod.rs:L283-L323](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L283-L323)：`ThreadPool` 上的四个方法 `scope`/`scope_fifo`/`in_place_scope`/`in_place_scope_fifo`——把闭包绑到**该池**（通过 `do_in_place_scope(Some(&self.registry), ...)` 一类调用），这是自定义池上开作用域的入口。
- [rayon-core/src/lib.rs:L86-L87](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L86-L87)：四个函数与两个句柄类型的再导出（rayon 上层原样转发，用户只需 `use rayon::*`）。

**顺序的测试证据**（单线程池排除窃取，顺序完全确定）：

- [rayon-core/src/scope/test.rs:L277-L298](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/test.rs#L277-L298)：`test_order!` 宏——1 线程池、10×10 嵌套 spawn，把 `i*10+j` 压入 Mutex<Vec>。
- [rayon-core/src/scope/test.rs:L300-L307](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/test.rs#L300-L307)：`lifo_order` 期望 **99..=0 倒序**；[L309-L316](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/test.rs#L309-L316)：`fifo_order` 期望 **0..=99 正序**。两者只换 scope 函数，其余一字不差。
- [rayon-core/src/scope/test.rs:L393-L415](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/test.rs#L393-L415) 与 [L417-L451](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/test.rs#L417-L451)：`test_mixed_order!` 与四个 mixed 用例——内外层各用不同 scope 种类时的精确顺序断言（内层 scope 的围栏会让外层部分任务提前执行，所以「不是完美的 LIFO/FIFO」，预期序列逐个写死在断言里，读这些断言是理解调度顺序的最佳练习）。

**栈增长测试**（scope 相对递归 join 的一个隐藏优势）：

- [rayon-core/src/scope/test.rs:L144-L168](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/test.rs#L144-L168) 与 [L170-L187](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/test.rs#L170-L187)：`linear_stack_growth`——T0 spawn T1 spawn T2……的链长从 5 变 500，断言栈用量比例在 ±10% 内。原因：spawn 是**异步入队**，等待的线程从 deque 逐个弹出任务、每个任务完整返回后再取下一个，链长不会叠加成 N 层栈帧；而递归写法的 `join` 是当前线程就地递归执行 A 分支，深度即栈深。这正是「用循环 + scope 代替递归 + join」能防爆栈的根据（文档 L63-L68 也提到「a loop can be used to spawn any number of tasks without recursing」）。

**生命周期的两个补充测试**：

- [rayon-core/src/scope/test.rs:L453-L473](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/test.rs#L453-L473)：`static_scope`——`'scope` 取 `'static` 时，spawn 闭包只能碰静态数据，但 body 内照样自由使用局部迭代器。
- [rayon-core/src/scope/test.rs:L497-L513](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/test.rs#L497-L513)：`mixed_lifetime_scope`——`'scope` 可以是多个生命周期里**较短**的那个（这里取 `'counter`），body 里可以借用更短的 `'slice` 数据，但 spawn 出去的闭包只能借 `'counter`。

#### 4.3.4 代码实践

1. **实践目标**：在排除窃取的条件下，亲眼看到 LIFO 与 FIFO 的差别。
2. **操作步骤**（示例代码）：

   ```rust
   use rayon::{scope, scope_fifo};

   fn main() {
       print!("scope      LIFO: ");
       scope(|s| {
           for i in 0..8 {
               s.spawn(move |_| print!("{i} "));
           }
       });
       println!();

       print!("scope_fifo FIFO: ");
       scope_fifo(|s| {
           for i in 0..8 {
               s.spawn_fifo(move |_| print!("{i} "));
           }
       });
       println!();
   }
   ```

   运行两遍，第二遍加环境变量：`RAYON_NUM_THREADS=1 cargo run --release`。
3. **需要观察的现象**：多线程时两行输出顺序都不定（任务会被其他线程偷走）；单线程时第一行稳定为 `7 6 5 4 3 2 1 0`，第二行稳定为 `0 1 2 3 4 5 6 7`。
4. **预期结果**：单线程输出与 `lifo_order`/`fifo_order` 测试的期望完全同构（那两个测试断言的就是 0..100 的倒序与正序）；多线程下顺序不确定但 8 个任务一个不少。打印顺序细节待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`JobFifo` 为什么必须**每线程一个**，而不是整个 scope 共用一个？

**答案**：队列是靠「压进执行线程自己的本地 deque」生效的；若全 scope 共用一个，A 线程 spawn 的任务会把 B 线程的队列任务混进来，B 弹出「队列任务」时执行的可能是 A 排的程序，无法保证「同一线程内 spawn 的相对 FIFO」。见 [rayon-core/src/scope/mod.rs:L575-L581](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L575-L581) 按 `num_threads` 建表、[L610-L615](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L610-L615) 按当前线程下索引。

**练习 2**：从池内工作线程调用 `scope(f)` 和调用 `in_place_scope(f)`，行为有区别吗？从普通主线程调用呢？

**答案**：池内没区别——`scope` 的 `in_worker` 发现已在本池线程就直接执行，两者 body 都在该线程上跑、CountLatch 都是 Stealing 形态。普通线程上有本质区别：`scope` 把 body 注入池（主线程阻塞在 LockLatch 上），`in_place_scope` 的 body 留在主线程执行、只有 spawn 的任务进池（所以 `OP` 不需要 `Send`）。

**练习 3**：在 `pool_a` 的工作线程里调用 `pool_b.scope(|s| s.spawn(...))`，spawn 的任务去哪个池？该线程此时在等什么？

**答案**：任务进 `pool_b`（`ScopeBase.registry` 记录的是创建时传入的池）。等待者是 `pool_a` 的工作线程，`CountLatch` 是 Stealing 形态但 registry 记的是 `pool_a`——所以任务完成减到零后要跨池唤醒 `pool_a` 的线程（这正是 latch.rs 中 `Stealing` 变体保存 `registry`+`worker_index` 的原因）。等待期间该线程在 `wait_until` 里继续帮 `pool_a` 干活。

## 5. 综合实践

把本讲三块内容串成一个程序：**scope 借用主线程栈上的 `&mut Vec` 写结果（对照 `rayon::spawn` 做不到）+ 对比 `scope` 与 `scope_fifo` 的完成顺序**。

```rust
// 示例代码：src/main.rs（cargo new scope_lab && cargo add rayon）
use rayon::{scope, scope_fifo};

fn main() {
    // ── 第一部分：借用主线程栈上的 &mut Vec ──────────────────────
    // 预分配 8 个槽位，chunks_mut 切出 8 段互不相交的 &mut [u32]，
    // 每段交给一个任务——多个任务同时持有"同一个 Vec 的不同部分"。
    let mut results: Vec<u32> = vec![0; 8];
    scope(|s| {
        for (i, chunk) in results.chunks_mut(1).enumerate() {
            s.spawn(move |_| chunk[0] = (i as u32) * (i as u32));
        }
    }); // 围栏：这里返回时 8 个任务必然全部完成
    assert_eq!(results, (0..8).map(|i| i * i).collect::<Vec<_>>());
    println!("results = {results:?}");

    // ── 第二部分：完成顺序对比 ───────────────────────────────────
    // 先用 LIFO 的 scope：
    print!("scope      : ");
    scope(|s| {
        for i in 0..8 {
            s.spawn(move |_| print!("{i} "));
        }
    });
    println!();
    // 再用 FIFO 的 scope_fifo：
    print!("scope_fifo : ");
    scope_fifo(|s| {
        for i in 0..8 {
            s.spawn_fifo(move |_| print!("{i} "));
        }
    });
    println!();
}
```

**操作步骤**：

1. `cargo new scope_lab && cd scope_lab && cargo add rayon`，写入上述代码。
2. `cargo run --release`（默认线程数）观察一遍。
3. `RAYON_NUM_THREADS=1 cargo run --release` 再跑几遍。
4. 把第一部分改成 `rayon::spawn` 版本试试：

   ```rust
   // 示例代码：预期编译失败
   let mut results: Vec<u32> = vec![0; 8];
   for (i, chunk) in results.chunks_mut(1).enumerate() {
       rayon::spawn(move || chunk[0] = (i as u32) * (i as u32));
   } // ← 编译错误：chunk 借用了局部变量 results，闭包不满足 'static
   ```

   读编译错误，体会「fire-and-forget 借不到栈」的硬约束（报错文本待本地验证，预期为 closure may outlive the current function / borrowed value does not live long enough 一类）。

**需要观察的现象**：

- 第一部分无论跑多少遍 `results` 恒为 `[0, 1, 4, 9, 16, 25, 36, 49]`——并行写、无锁、顺序仍确定（每个任务的写入位置由 `chunks_mut` 的切分决定，与执行顺序无关，和 u4-l4 collect 的「落位由区间决定」是同一个思想）。
- 第二部分：单线程时 `scope` 行稳定倒序（`7 6 5 4 3 2 1 0`）、`scope_fifo` 行稳定正序；多线程时两行都可能乱序，但数字不重不漏。
- 第四步的 `rayon::spawn` 版本编译失败。

**预期结果**：三件事分别验证了本讲的三个知识点——借用模型（栈上 `&mut` 切片可安全分给并行任务）、完成协议（围栏保证 assert 不会在任务未完成时执行）、顺序变体（LIFO/FIFO 只在无窃取时严格成立）。

## 6. 本讲小结

- `scope` 用「返回前等齐所有任务」的运行期协议 + `'scope` 生命周期约束，让异步 spawn 的任务可以安全借用调用方栈上数据——这是 `rayon::spawn`（`'static`）做不到的。
- 完成协议由 `CountLatch` 实现：计数 = 1（body）+ 存活任务数，spawn 先 `increment` 后入队，每个任务完 `set` 减一，减到零才置位唤醒；等待方式由创建者身份决定（池内边帮工边等，池外睡眠）。
- 任务必经 `HeapJob` 堆分配，panic 被 `halt_unwinding` 捕获、第一个抢到槽的胜出、在 scope 调用点重放；**已 spawn 的任务即使遇到他人 panic 也保证执行**。
- `ScopePtr` 的裸指针是全模块唯一的 unsafe 关键点，安全性完全押在「任务先于 scope 结束执行」这一不变量上。
- `scope_fifo` 用「每线程一个 `JobFifo`、把队列本身当任务压进本地 deque」的间接层，把本地相对顺序从 LIFO 翻成 FIFO，调度结构不动；顺序保证仅在无窃取（单线程）时严格。
- `in_place_scope` 家族让 body 留在调用线程执行（`OP` 因此不需要 `Send`），只有经 scope 句柄 spawn 的任务进目标池；body 里的 `join`/迭代器仍走调用者的池。

## 7. 下一步学习建议

- **下一讲 u6-l2（spawn：异步任务派发）**：从 scope 的对立面看 `rayon::spawn`——没有围栏、没有借用、`'static` 约束的完整推导，以及它如何与 scope/channel 配合取回结果。
- **u6-l3（broadcast）**：本讲已两次遇到 `spawn_broadcast`（ArcJob、每线程一个副本、计数按副本增加），届时会展开 `BroadcastContext` 与结果收集。
- **u7-l2（install 与跨池死锁）**：本讲埋了两处伏笔——`CountLatch::Stealing` 的跨池唤醒字段、scope 池路由与 ambient 池的区分——都将在死锁分析中派上用场。
- **源码再读一遍**：带着「counter 此刻是几」的问题重走 [rayon-core/src/scope/mod.rs:L681-L718](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L681-L718)，再读 [RFC #1: scope scheduling](https://github.com/rayon-rs/rfcs/blob/main/accepted/rfc0001-scope-scheduling.md) 看 FIFO 变体的设计动机。
