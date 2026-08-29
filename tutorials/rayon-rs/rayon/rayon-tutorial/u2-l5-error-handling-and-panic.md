# 错误处理与 panic 语义（u2-l5）

## 1. 本讲目标

学完本讲，你应该能够：

1. 在并行管道中用 `try_fold` / `try_fold_with` / `try_reduce` / `try_reduce_with` 传播 `Result` / `Option`，并理解它们「提前短路」的确切范围（本地短路 vs 全局短路）。
2. 用 `while_some` 把 `None` 当作全管道的停止信号。
3. 说清 panic 在并行执行中的传播路径：谁捕获它、在哪重放、为什么其余任务不一定立刻停止。
4. 用 `panic_fuse` 做跨线程快速止损，并理解它是「尽力而为」而非保证。

本讲是单元二（使用层）的收尾：前面几讲关注「怎么把数据送进并行管道、怎么把结果收回来」，本讲关注「管道中途出错了怎么办」。

## 2. 前置知识

本讲只假设你读过单元二前几讲（`ParallelIterator` 两条驱动路径、数据源、collect）。下面几个 Rust 概念先用一段话复习：

- **`Result<T, E>` 与 `Option<T>`**：Rust 里「可恢复失败」的两种类型。`Result` 携带错误信息 `E`，`Option` 表示「有值 / 没值」。串行迭代器里有 `try_fold` / `try_collect` 等短路操作；Rayon 提供的是它们的并行对应物。
- **`ControlFlow<B, C>`**：标准库里表达「继续（`Continue(C)`）还是提前退出（`Break(B)`）」的枚举。可以把 `Result` 想成 `ControlFlow` 的特例：`Ok` 是继续、`Err` 是退出。Rayon 内部正是用它来统一处理 `Result` / `Option` 等类型。
- **panic 与 unwind**：Rust 的 panic 默认走「栈回退（unwind）」——当前线程逐层析构局部变量，然后向上传播。`std::panic::catch_unwind` 可以把 panic 捕获成一个 `Err` 值，`std::panic::resume_unwind` 可以把它重新抛出。注意：catch_unwind **不是** try/catch，它不保证捕获所有 panic（比如已经在 `panic = "abort"` 编译选项下就完全无效）。
- **`AtomicBool` 与 `Ordering::Relaxed`**：一个所有线程共享的布尔原子变量。`Relaxed` 序只保证单个变量的原子性、不保证与其他变量的顺序关系——对于「通知大家停下来」这种尽力而为的信号已经足够，因为真正的同步由 panic 传播本身完成。
- **短路（short-circuit）**：一旦发现失败就不再处理剩余元素。串行世界里短路很容易，并行世界里「剩余元素」可能已经分散在多个线程上，这就是本讲所有源码要解决的核心矛盾。

一个直觉式的总纲：**Rayon 把「失败」分成两档处理**——

| 失败档位 | 表达方式 | 处理机制 | 本讲对应模块 |
|---|---|---|---|
| 可恢复 | `Result` / `Option` 值 | 消费者内部的 `ControlFlow` + 共享 `AtomicBool` | `try_*` 家族、`while_some` |
| 不可恢复 | panic | unwind 捕获 → 跨线程搬运 → 调用点重放 | panic 传播、`panic_fuse` |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/iter/mod.rs` | `ParallelIterator` 上 `try_reduce` / `try_fold` / `while_some` / `panic_fuse` 等方法的定义与文档；文末还有私有的 `Try` trait（对 `Result` / `Option` / `ControlFlow` / `Poll` 的统一抽象） |
| `src/iter/try_fold.rs` | `TryFold` / `TryFoldWith` 两个**惰性适配器**的结构体与消费者实现 |
| `src/iter/try_reduce.rs` | `try_reduce` **立即执行消费者**的实现（带全局短路标志） |
| `src/iter/try_reduce_with.rs` | `try_reduce_with` 的实现（无 identity，空迭代器返回 `None`） |
| `src/iter/while_some.rs` | `WhileSome` 适配器：遇到 `None` 立即全管道停止 |
| `src/iter/panic_fuse.rs` | `PanicFuse` 适配器：panic 发生后尽快停掉其他线程的剩余工作 |
| `tests/iter_panic.rs` | 集成测试：验证 panic 会传播、`panic_fuse` 确实少跑了任务 |
| `rayon-core/src/unwind.rs` | 内核侧的 panic 捕获/重放工具（`halt_unwinding` / `resume_unwinding`） |
| `rayon-core/src/job.rs` | `JobResult::call` 把 panic 存成 `JobResult::Panic`，`into_return_value` 在取结果时重放 |
| `rayon-core/src/join/mod.rs` | `join` 中一支 panic 后为什么还要等另一支完成 |
| `src/iter/plumbing/mod.rs` | `Consumer::full` / `Folder::full` 契约——所有短路机制赖以生效的钩子 |

## 4. 核心概念与源码讲解

### 4.1 panic 在并行世界的传播路径

#### 4.1.1 概念说明

先回答一个使用层面最关心的问题：**任务在某个工作线程里 panic 了，调用方会看到什么？**

答案是：panic 一定会传播到调用 `for_each` / `sum` / `collect` 的那一步，在那里重新抛出，表现得就像串行代码在原地 panic 一样。`tests/iter_panic.rs` 里第一个测试就是最小证明——它标注了 `#[should_panic(expected = "boom")]`：

[tests/iter_panic.rs:L16-L20](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/iter_panic.rs#L16-L20)

这段测试对 0..65536 并行执行 `for_each`，其中一个元素触发 `panic!("boom")`，最终测试断言这个 panic 如期出现在调用方。

但「panic 会传回来」不等于「其他任务立刻停」。这正是官方文档在 `panic_fuse` 一节里强调的（见 4.4 节引用）：由于 `join` 的内部语义，panic 发生后其余已切分出去的任务**通常仍会跑完**。要理解这句话，得先看内核是怎么搬运 panic 的。

#### 4.1.2 核心流程

panic 的完整旅程可以概括为五步：

```text
1. 工作线程执行用户闭包
        │  闭包 panic，开始 unwind
        ▼
2. halt_unwinding（catch_unwind）在 Job 边界拦下 panic
        │  panic 载荷装箱为 Box<dyn Any + Send>
        ▼
3. JobResult::Panic 与正常结果一起存放
        │  任务照常「完成」，只是结果是「panic 了」
        ▼
4. 调用方（join 点 / 迭代器驱动点）取结果
        │  into_return_value 发现是 Panic
        ▼
5. resume_unwinding 在调用线程重新抛出同一个 panic
```

另外一个关键细节：`join(a, b)` 中如果 `a` panic 了，当前线程**不能立刻跟着 unwind**，必须先等 `b` 真正结束。原因是 `b` 可能借用了 `join` 外层栈帧上的数据，如果栈帧提前销毁，`b` 就会访问已失效的内存。这就是「panic 不会立刻叫停其他任务」的根源。

#### 4.1.3 源码精读

**捕获与重放的工具函数**（rayon-core 内部）：

[rayon-core/src/unwind.rs:L9-L22](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/unwind.rs#L9-L22)

`halt_unwinding` 就是对 `panic::catch_unwind` 的薄封装，把 panic 变成 `Err(Box<dyn Any + Send>)`——这个装箱的载荷是 `Send` 的，所以能**跨线程搬运**。`resume_unwinding` 则在最终目的地把同一个载荷重新抛出，保留原始的 panic 信息。

**任务结果的三态**：

[rayon-core/src/job.rs:L222-L240](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/job.rs#L222-L240)

`JobResult::call` 执行闭包时用 `halt_unwinding` 包住：正常完成得到 `Ok(x)`，panic 得到 `JobResult::Panic(x)`。也就是说，在内核眼里「panic 的任务」和「正常的任务」走同一条完成路径，只是结果盒子里装的东西不同——等到 `into_return_value` 打开盒子时才发现是 panic，于是调用 `resume_unwinding` 重放。

**join 中一支 panic 后的等待**：

[rayon-core/src/join/mod.rs:L141-L146](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L141-L146)

这里 `call_a` 的结果若是 `Err(err)`（即闭包 a panic），并不直接返回，而是转入 `join_recover_from_panic`：

[rayon-core/src/join/mod.rs:L175-L186](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L175-L186)

注释写得很清楚：先 `wait_until` 等 job B 的 latch 置位（即 B 彻底完成），再 `resume_unwinding` 重放 a 的 panic。**等待是为了内存安全，不是为了结果**。

最后补充一个防御性细节：rayon 对**自己内部代码**的 panic 是零容忍的，`AbortIfPanic` 会在 Drop 时直接打印并 `abort` 整个进程（[rayon-core/src/unwind.rs:L24-L31](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/unwind.rs#L24-L31)），因为内部不变量被打破后无法安全继续。用户闭包的 panic 才走上面那条「搬运—重放」的温和路径。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认「panic 会传播回调用方，且线程池在 panic 之后仍然可用」。
2. **操作步骤**：新建一个 Cargo 项目（依赖 `rayon = "1"`），写入下面的程序（示例代码）：

   ```rust
   use rayon::prelude::*;

   fn main() {
       // 第一次：并行管道中途 panic，用 catch_unwind 接住
       let result = std::panic::catch_unwind(|| {
           (0..1000_i32).into_par_iter().for_each(|i| {
               assert!(i != 500, "boom at {}", i);
           });
       });
       assert!(result.is_err(), "panic 应该被传播到 for_each 调用点");

       // 第二次：同一个全局线程池继续接活
       let sum: i32 = (0..1000).into_par_iter().sum();
       println!("panic 之后线程池照常工作, sum = {}", sum);
   }
   ```

   运行前可以设置 `RUST_BACKTRACE=1` 观察回栈。默认会打印 panic 消息，程序继续执行到最后的 `println!`。
3. **需要观察的现象**：终端先出现 `thread '<unnamed>' panicked at ... boom at 500`（工作线程名是匿名的），随后仍然打印出 `sum = 499500`。
4. **预期结果**：panic 恰好在 `for_each` 那一行「重新发生」，`catch_unwind` 捕获到 `Err`；线程池没有损坏，第二次并行计算结果正确。（具体打印顺序「待本地验证」，不同线程调度下 panic 消息与 println 的先后可能不同。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `halt_unwinding` 能把 panic 从工作线程搬到调用线程，而不会丢掉原始的 panic 信息？

答案：`catch_unwind` 返回的 `Err(Box<dyn Any + Send>)` 装着原始 panic 载荷（比如 `String`），这个箱子是 `Send` 的，可以随 `JobResult::Panic` 一起跨线程传递；到调用点后 `resume_unwind` 用**同一个载荷**重新抛出，所以消息、类型都保持原样，而不是「new 一个新 panic」。

**练习 2**：`join(|| panic!(), || { /* 长任务 */ })` 中左支 panic 后，右支的长任务会被立刻杀死吗？

答案：不会。参见 `join_recover_from_panic`（[rayon-core/src/join/mod.rs:L175-L186](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L175-L186)）：当前线程会先 `wait_until` 右支的 latch 置位，右支跑完（或被别的线程偷走跑完）之后才重放 panic。原因见 4.1.2——右支可能引用外层栈帧。

**练习 3**：在 `panic = "abort"` 编译配置下，`tests/iter_panic.rs` 里的 `iter_panic_fuse` 测试还能验证短路效果吗？

答案：不能。文件在第 23 行给它标注了 `#[cfg_attr(not(panic = "unwind"), ignore)]`（[tests/iter_panic.rs:L22-L24](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/iter_panic.rs#L22-L24)），因为 abort 模式下没有 unwind，`catch_unwind` 与「捕获后继续」整套机制都不成立，测试直接被忽略。

### 4.2 try_* 家族：用 Result / Option 表达可恢复失败

#### 4.2.1 概念说明

panic 是「掀桌子」，多数业务错误更适合用 `Result` 表达。四个方法按「fold 还是 reduce」「要不要 identity」两个维度划分：

| 方法 | 性质 | 输入要点 | 返回 | 典型场景 |
|---|---|---|---|---|
| `try_fold` | 惰性适配器 | `identity: Fn() -> T`，`fold_op: Fn(T, Item) -> R` | 新迭代器，元素类型 `R` | 每个子任务局部累积 |
| `try_fold_with` | 惰性适配器 | `init: T`（可 `Clone` 的初值） | 新迭代器，元素类型 `R` | 同上，但初值是具体值 |
| `try_reduce` | 立即执行消费者 | `identity: Fn() -> T`，`op: Fn(T, T) -> Item` | `Self::Item`（如 `Result<T, E>`） | 最终归并 + 全局短路 |
| `try_reduce_with` | 立即执行消费者 | `op: Fn(T, T) -> Item` | `Option<Self::Item>` | 同上，但空迭代器返回 `None` |

回忆 u2-l1 的结论：**看返回类型即可判别惰性或立即**——`try_fold` 返回 `TryFold<...>` 结构体（还要继续往下接消费者），`try_reduce` 直接返回最终值。串行 `Iterator` 的 `try_fold` 是立即执行的，这一点与 Rayon 不同，是迁移代码时最容易踩的概念差。

它们能同时接受 `Result` 和 `Option`（甚至 `ControlFlow`、`Poll<Result<..>>`），靠的是一个私有 trait：

#### 4.2.2 核心流程

统一抽象是 `Try`（`std::ops::Try` 的稳定前克隆，不允许 crate 外实现）：

- `branch()` 把 `Result<T, E>` 拆成 `ControlFlow<Err(e), T>`——错误变 `Break`；
- `from_output` / `from_residual` 负责从 `ControlFlow` 还原回 `Result` / `Option`。

短路分两级，这是本模块最重要的一张图：

```text
                     try_fold（本地短路）                try_reduce（全局短路）
  ┌────────────┐     ┌──────────────────────┐          ┌──────────────────────┐
  │ 数据切片 A │ ──▶ │ Folder 内 ControlFlow │          │  共享 AtomicBool full │
  ├────────────┤     │ 一旦 Break：本切片    │          │  任何 Folder 遇到 Err │
  │ 数据切片 B │ ──▶ │ 不再 consume 剩余元素 │          │  就 store(true)，所有 │
  ├────────────┤     │ （不影响切片 C/D）    │          │  线程的 full() 变 true│
  │ 数据切片 C │ ──▶ └──────────────────────┘          └──────────────────────┘
  └────────────┘            │                                    │
        汇总各切片的 Result（可能已经有 Err）◀── Reducer：任一侧 Break 则结果 Break
```

`full()` 是 plumbing 层预留的钩子：`Consumer::full` / `Folder::full` 表示「我不想再要元素了」，驱动循环（`bridge`）会轮询它来决定是否停止切分与投喂（[src/iter/plumbing/mod.rs:L143-L145](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L143-L145)、[src/iter/plumbing/mod.rs:L184-L187](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L184-L187)）。官方文档也点明了配合方式：`try_fold` 之后常接 `try_reduce`，由后者完成「最终归并 + 全局短路」。

#### 4.2.3 源码精读

**`Try` trait 与 `Result` 的实现**：

[src/iter/mod.rs:L3479-L3491](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3479-L3491)

这是 `std::ops::Try` 的内部克隆，只有四个方法。`Result` 版实现如下：

[src/iter/mod.rs:L3530-L3548](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3530-L3548)

`branch` 把 `Err(e)` 映射成 `Break(Err(e))`。因为 `Try` 是私有的，公开方法签名里出现它时都要标 `#[expect(private_bounds)]`（例如 `try_reduce` 的定义处 [src/iter/mod.rs:L1076-L1084](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1076-L1084)）。

**`try_fold`：本地短路的 Folder**：

[src/iter/try_fold.rs:L124-L157](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/try_fold.rs#L124-L157)

三个关键点：

- `TryFoldFolder` 保存 `control: ControlFlow<U::Residual, U::Output>`，`consume` 里 `if let Continue(acc)` 保证 **Break 之后不再调用 `fold_op`**（本切片内短路）；
- `complete` 把 `ControlFlow` 还原成 `U`（`Result` 或 `Option`）后**作为一个元素**交给下游消费者（`self.base.consume(item).complete()`）；
- `full()` 返回 `self.control.is_break() || self.base.full()`——只反映本地状态。对照 `TryFoldConsumer::full`：

[src/iter/try_fold.rs:L100-L103](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/try_fold.rs#L100-L103)

它只转发 `base.full()`，**没有任何共享标志**——所以文档说 try_fold 的失败「stops processing the local set of items, without affecting other folds in the iterator's subdivisions」（[src/iter/mod.rs:L1299-L1307](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1299-L1307)）。

**`try_fold` 与 `try_fold_with` 的差异**：`TryFoldWithConsumer::split_at` 把初值 `item` `clone()` 给左半、右半拿原件（[src/iter/try_fold.rs:L235-L249](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/try_fold.rs#L235-L249)），代价是 `T: Clone`；`try_fold` 用 `identity: Fn() -> T` 每处现造一个初值，不需要 Clone。两者共用同一个 `TryFoldFolder`。

**`try_reduce`：全局短路的实现**：

[src/iter/try_reduce.rs:L8-L22](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/try_reduce.rs#L8-L22)

入口先造一个共享的 `AtomicBool full`，把它的引用塞进 consumer。之后：

[src/iter/try_reduce.rs:L106-L116](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/try_reduce.rs#L106-L116)

`consume` 一旦发现合并结果是 `Break`，立刻 `full.store(true, Relaxed)`——因为所有切分副本共享同一个 `AtomicBool`（consumer 是 `Copy` 的，`split_at` 直接复制自身，见 [src/iter/try_reduce.rs:L48-L50](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/try_reduce.rs#L48-L50)），任何一个线程遇到错误，其他线程的 `full()` 都会变 true：

[src/iter/try_reduce.rs:L60-L62](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/try_reduce.rs#L60-L62)

归并阶段，`Reducer` 保证任何一侧是 `Break` 就整体 `Break`：

[src/iter/try_reduce.rs:L80-L91](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/try_reduce.rs#L80-L91)

注意 `match (left.branch(), right.branch())` 的模式 `(Break(r), _) | (_, Break(r))`：两个都错时保留的是**模式匹配先命中的那个**，而并行下左右谁先到并不确定，所以官方文档明确「多个并行错误返回哪一个是不确定的」（[src/iter/mod.rs:L1050-L1055](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1050-L1055)）。

**`try_reduce_with`：三层嵌套的返回值**：

[src/iter/try_reduce_with.rs:L75-L90](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/try_reduce_with.rs#L75-L90)

它没有 identity，所以 Folder 内部是 `Option<ControlFlow<...>>`：`None` 表示这个切片还没见过任何元素。最终返回 `Option<Self::Item>`，于是有三种状态（文档举例很清楚，[src/iter/mod.rs:L1095-L1103](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1095-L1103)）：`None`＝迭代器为空；`Some(Err(e))`／`Some(None)`＝遇到错误停下；`Some(Ok(x))`／`Some(Some(x))`＝全部成功归并成 `x`。Reducer 里 `(None, x) | (x, None) => x` 就是「空的一侧不干扰另一侧」。

#### 4.2.4 代码实践

1. **实践目标**：体验 `try_fold`（本地短路）与 `try_reduce`（全局短路）的分工，并验证溢出场景返回 `None`。
2. **操作步骤**：在示例工程中运行官方文档里的两个断言（它们本身是 doctest，`cargo test` 会执行；这里抄进 `main` 更直观，示例代码）：

   ```rust
   use rayon::prelude::*;

   fn sum_squares<I>(iter: I) -> Option<i32>
   where
       I: IntoParallelIterator<Item = i32>,
   {
       iter.into_par_iter()
           .map(|i| i.checked_mul(i))            // 每个元素平方，溢出得 None
           .try_reduce(|| 0, i32::checked_add)   // 可失败地相加
   }

   fn main() {
       println!("{:?}", sum_squares(0..5));            // Some(30)
       println!("{:?}", sum_squares(0..10_000));       // 溢出 -> None

       // try_fold + try_reduce 组合（官方推荐搭配）
       let bytes = 0..22_u8;
       let sum = bytes
           .into_par_iter()
           .try_fold(|| 0_u32, |a: u32, b: u8| a.checked_add(b as u32))
           .try_reduce(|| 0, u32::checked_add);
       println!("{:?}", sum); // Some(231)，即 0+1+...+21
   }
   ```

3. **需要观察的现象**：三个输出分别是 `Some(30)`、`None`、`Some(231)`；全程没有任何 panic。
4. **预期结果**：与源码文档注释中的断言一致（[src/iter/mod.rs:L1063-L1074](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1063-L1074)、[src/iter/mod.rs:L1317-L1323](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1317-L1323)）。可以试着把 `0..10_000` 改成 `0..100_000`，确认结果仍是 `None`（先溢出的错误被保留）。

#### 4.2.5 小练习与答案

**练习 1**：`try_fold` 的元素类型是 `R`（如 `Result<u32, E>`），它流向下游时是什么形态？为什么 `try_fold` 之后必须再接一个消费者？

答案：`TryFold` 的 `Item = U`（即 `Result` / `Option` 本身，见 [src/iter/try_fold.rs:L39-L46](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/try_fold.rs#L39-L46)），每个子任务的局部结果作为一个元素交给下游。因为它是惰性适配器，不接 `try_reduce` / `collect` 等消费者就不会执行任何计算。

**练习 2**：如果把 `try_reduce` 的 `AtomicBool` 机制去掉（只保留 Reducer 里的 Break 优先），程序结果还会正确吗？会有什么损失？

答案：结果仍然正确——Reducer 保证任何 `Break` 都会传播到最终结果。损失的是**性能**：没有全局标志，其他线程不知道已经出错，会把各自切片全部算完才在归并阶段丢弃。`AtomicBool` 的意义就是让 `full()` 提前变 true，驱动循环尽早停止投喂元素。

**练习 3**：`try_reduce_with` 返回 `Option<Result<T, E>>`，请说明三个值 `None`、`Some(Err(e))`、`Some(Ok(3))` 分别代表什么。

答案：`None`＝迭代器为空（没有元素可归并）；`Some(Err(e))`＝处理中遇到错误 `e` 并短路停止；`Some(Ok(3))`＝全部元素成功归并为 `3`。参见 [src/iter/mod.rs:L1095-L1103](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1095-L1103) 的文档说明。

### 4.3 while_some：把 None 当作停止信号

#### 4.3.1 概念说明

`while_some` 处理一种更轻量的失败：元素类型是 `Option<T>` 时，它产出 `T`，**直到遇见第一个 `None` 为止**。它和 `try_reduce` 的关系类似于「`Option` 世界的提前退出」，但形态是适配器（惰性），后面可以继续接 `max()`、`collect()` 等任何消费者。

典型用法是「带有效性检查的转换」：闭包对无法处理的输入返回 `None`，`while_some` 让整条管道就此收工，而不是把 `Option` 一路漏到下游。

#### 4.3.2 核心流程

```text
上游元素: Some(a) Some(b) None Some(d) Some(e) ...
                          │
                 WhileSomeFolder::consume(None)
                          │ full.store(true)   ← 共享 AtomicBool，所有切片可见
                          ▼
下游收到:  a     b     （停止，d/e 不再投喂）
```

机制与 `try_reduce` 的全局短路同构：一个共享 `AtomicBool` + `full()` 钩子。区别在于触发条件从「ControlFlow 是 Break」变成「元素本身是 `None`」。

#### 4.3.3 源码精读

**入口与共享标志**：

[src/iter/while_some.rs:L23-L41](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/while_some.rs#L23-L41)

注意 `WhileSome` 只实现了 `ParallelIterator`（`drive_unindexed`），没有 indexed 实现——短路之后的长度不可知，索引能力自然无从谈起。

**Folder 的核心三行**：

[src/iter/while_some.rs:L106-L118](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/while_some.rs#L106-L118)

`Some` 就转交下游，`None` 就置位共享标志。`full()` 则把它暴露给驱动循环：

[src/iter/while_some.rs:L79-L81](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/while_some.rs#L79-L81)

**批量路径 `consume_iter`**：plumbing 允许消费者整批接住一个串行迭代器，这里用 `take_while` 在批内提前刹车，再 `map(Option::unwrap)` 展开元素：

[src/iter/while_some.rs:L120-L140](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/while_some.rs#L120-L140)

闭包 `some` 对每个元素先检查全局标志（别的线程可能已经置位），遇到 `None` 时置位并返回 false 终止 `take_while`。这是「检查—消费」双保险：既响应本切片的 `None`，也响应其他切片的 `None`。

官方文档的计数器示例直观展示了短路效果（`counter` 最终小于 2048）：

[src/iter/mod.rs:L1900-L1928](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1900-L1928)

#### 4.3.4 代码实践

1. **实践目标**：验证 `while_some` 遇到 `None` 后整条管道提前停止。
2. **操作步骤**：运行文档示例的改写版（示例代码）：

   ```rust
   use rayon::prelude::*;
   use std::sync::atomic::{AtomicUsize, Ordering};

   fn main() {
       let counter = AtomicUsize::new(0);
       let value = (0_i32..2048)
           .into_par_iter()
           .map(|x| {
               counter.fetch_add(1, Ordering::SeqCst);
               if x < 1024 { Some(x) } else { None }
           })
           .while_some()
           .max();

       println!("value = {:?}", value);
       println!("visited = {}", counter.load(Ordering::SeqCst));
   }
   ```

3. **需要观察的现象**：`value` 小于 `Some(1024)`；`visited` 小于 2048（文档断言 `counter < 2048`）。
4. **预期结果**：`visited` 明显小于 2048 但**不一定恰好是 1024**——切分边界与线程调度决定了哪些切片在标志置位前已经跑完。这正是「尽力短路」的含义。多运行几次，数值会波动（具体数值待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`while_some` 之后还能调用 `enumerate()` 或 `zip()` 吗？

答案：不能。`WhileSome` 只实现 `ParallelIterator`，没有实现 `IndexedParallelIterator`（见 [src/iter/while_some.rs:L23-L41](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/while_some.rs#L23-L41) 只有 `drive_unindexed`）。短路后元素个数在编译期未知，索引能力无法提供。`max()`、`collect()`、`for_each()` 这类无索引消费者仍可用。

**练习 2**：`while_some` 与 `filter_map(|x| x)` 的区别是什么？

答案：`filter_map(|x| x)`（或 `flatten`）会把所有 `Some` 都留下来、无视 `None` 继续处理剩余元素；`while_some` 在第一个 `None` 处让**全管道**尽快停止。前者关心「留下有值的」，后者关心「见到空值就收工」。

**练习 3**：为什么 `consume_iter` 里的 `take_while` 闭包要先 `full.load` 再对 `None` 做 `full.store`，两步都不能省？

答案：`load` 是为了响应**其他切片**已经置位的标志（本切片还没遇到 `None` 也应停下）；`store` 是为了把自己遇到的 `None` **广播**给所有切片。少了任何一步，短路就只在一个切片内生效（练习参考 [src/iter/while_some.rs:L124-L132](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/while_some.rs#L124-L132)）。

### 4.4 panic_fuse：跨线程快速止损

#### 4.4.1 概念说明

4.1 节说过：panic 一定会传播回来，但其余任务往往照跑不误。如果每个任务都很昂贵（比如文档示例里每个元素 `sleep` 一秒），就意味着 panic 之后程序还要白干很久才退出。`panic_fuse` 的作用就是在 panic 发生后**尽力**让所有线程尽快停手。

它的思路非常巧妙：**不去捕获 panic，而是利用 unwind 的析构顺序**。用一个持有 `&AtomicBool` 的小守卫（`Fuse`）伴随每份工作，用户闭包 panic 时 unwind 会析构途经的守卫，守卫在 `Drop` 里检查 `thread::panicking()` 并置位标志；其他线程通过 `full()` 轮询这个标志提前退出。

#### 4.4.2 核心流程

```text
线程 1: 消费元素 ... ──▶ 用户闭包 panic!
                              │ unwind 开始，逐层析构
                              ▼
                    Drop(Fuse) 检查 thread::panicking() == true
                              │ fused.store(true, Relaxed)
                              ▼
线程 2..N: full() == fused.load() == true
              │ 驱动循环停止切分；PanicFuseIter::next 返回 None；consume_iter 的 take_while 截断
              ▼
        各线程收尾，panic 沿 4.1 的路径传播回调用方
```

它同时包装了 **Producer、Consumer、Iterator** 三层（文件里三个 `// ///...` 分节注释对应三组实现），所以链上插在哪个位置都有效——`panic_fuse()` 可以放在 `inspect` 之前或之后。

#### 4.4.3 源码精读

**守卫 `Fuse`**：

[src/iter/panic_fuse.rs:L17-L35](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/panic_fuse.rs#L17-L35)

`Fuse` 只是 `&AtomicBool` 的包装，`Drop` 时若正处于 panic（`thread::panicking()`）就置位。它是 `Clone` 的——克隆只是复制引用，所以一次 `split_at` 产生的左右两半共享同一个标志。

**Producer 层：切分时克隆守卫、迭代时检查标志**：

[src/iter/panic_fuse.rs:L147-L159](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/panic_fuse.rs#L147-L159)

左右两个子 Producer 各拿一份 `Fuse` 克隆（共享同一个 `AtomicBool`）。包出来的串行迭代器在取元素前先看标志：

[src/iter/panic_fuse.rs:L184-L190](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/panic_fuse.rs#L184-L190)

`next` 一旦发现 `panicked()` 就返回 `None`——对上层来说「数据没有了」，自然提前结束。

**Consumer 层：`full()` 汇报止损意愿**：

[src/iter/panic_fuse.rs:L260-L262](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/panic_fuse.rs#L260-L262)

`fuse.panicked() || self.base.full()`——把「已经 panic」伪装成「消费者已满」，驱动循环就会停止继续切分和投喂。Folder 的批量路径同样用 `take_while` 截断：

[src/iter/panic_fuse.rs:L300-L314](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/panic_fuse.rs#L300-L314)

**官方文档对语义边界的描述**：

[src/iter/mod.rs:L1930-L1939](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1930-L1939)

注意措辞是「makes a **greater effort** to stop processing other items sooner」——尽力而为，并且有额外同步开销、可能抑制某些优化。文档的 `should_panic` 示例（[src/iter/mod.rs:L1947-L1958](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1947-L1958)）展示了典型场景：一百万元素每个 sleep 一秒，没有 `panic_fuse` 的话其余线程会继续睡很久。

**集成测试的数量化验证**：

[tests/iter_panic.rs:L24-L53](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/iter_panic.rs#L24-L53)

这个测试用两个技巧保证确定性：单线程线程池（`num_threads(1)`）+ `with_max_len(1)`（每元素一个任务）。然后用 `AtomicUsize` 统计真正访问过的元素数：

- 不加 `panic_fuse`：访问 \( n - 1 \) 个（除了触发 panic 的那个，其余全部跑完，[tests/iter_panic.rs:L41-L44](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/iter_panic.rs#L41-L44)）；
- 加 `panic_fuse`（无论插在 `inspect` 前还是后，甚至 `rev()` 之后）：访问数严格小于 \( n - 1 \)（[tests/iter_panic.rs:L46-L51](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/iter_panic.rs#L46-L51)）。`rev()` 那一行专门覆盖 Producer 侧（`PanicFuseIter`）的短路路径。

#### 4.4.4 代码实践

1. **实践目标**：亲手量化 `panic_fuse` 少跑了多少任务。
2. **操作步骤**：复刻测试思路，但把规模缩小（示例代码）：

   ```rust
   use rayon::prelude::*;
   use rayon::ThreadPoolBuilder;
   use std::panic;
   use std::sync::atomic::{AtomicUsize, Ordering};

   fn count(iter: impl ParallelIterator + panic::UnwindSafe) -> usize {
       let count = AtomicUsize::new(0);
       let result = panic::catch_unwind(|| {
           iter.for_each(|_| {
               count.fetch_add(1, Ordering::Relaxed);
           });
       });
       assert!(result.is_err()); // panic 必须传播回来
       count.into_inner()
   }

   fn main() {
       // 单线程 + 每个元素一个任务，保证行为确定
       let pool = ThreadPoolBuilder::new().num_threads(1).build().unwrap();
       pool.install(|| {
           let iter = (0..10_000_i32).into_par_iter().with_max_len(1);
           let check = |i: &i32| {
               if *i == 5_000 { panic!("boom") }
           };

           let no_fuse = count(iter.clone().inspect(check));
           println!("无 panic_fuse 访问 {} 个", no_fuse);

           let fused = count(iter.clone().inspect(check).panic_fuse());
           println!("有 panic_fuse 访问 {} 个", fused);
           assert!(fused < no_fuse);
       });
   }
   ```

3. **需要观察的现象**：第一行输出 `9999`（即 \( n-1 \)），第二行输出一个明显更小的数（通常在 5000 出头）。
4. **预期结果**：与测试断言一致（[tests/iter_panic.rs:L41-L51](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/iter_panic.rs#L41-L51)）：无 fuse 恰好 \( n-1 \)，有 fuse 严格更小。若把 `num_threads` 改成默认值（多线程），两个数都会变得不确定，但「有 fuse 更小」通常仍成立——多线程下的具体数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`panic_fuse` 为什么用 `Drop` 守卫而不是在消费者里 `catch_unwind`？

答案：因为 panic 可能发生在链条的**任何一层**（用户闭包在 map、inspect、for_each 里都有可能）。守卫伴随切分与消费的全过程，无论 unwind 从哪个深度穿过，途经的 `Fuse` 都会被析构并置位标志（[src/iter/panic_fuse.rs:L21-L28](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/panic_fuse.rs#L21-L28)）。这比在某一个固定位置捕获更通用，也不吞掉 panic 本身——它仍然照常传播。

**练习 2**：`Ordering::Relaxed` 在这里安全吗？会不会出现「标志置位了，别的线程却永远看不到」？

答案：安全。这里只需要单个布尔变量的原子可见性，不涉及与其他数据的顺序约束，`Relaxed` 足够。就算某个线程刚好错过标志、多跑几个元素，也只是止损晚了一点，不影响正确性——panic 最终仍由 4.1 的机制传播，join 点的等待提供了完整的同步。这正是「尽力而为」的设计取舍。

**练习 3**：`panic_fuse()` 插在 `map(f)` 之前还是之后有区别吗？

答案：对短路效果没有区别。测试同时验证了 `inspect(check).panic_fuse()` 和 `panic_fuse().inspect(check)` 两种顺序（[tests/iter_panic.rs:L46-L48](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/iter_panic.rs#L46-L48)），因为它在 Producer、Consumer、Iterator 三层都包了守卫。需要注意的是它毕竟多了一层包装与原子操作，官方文档提醒这「may also inhibit some optimizations」。

## 5. 综合实践

把本讲三个模块串成一个任务：**同一条「会除零」的管道，用三种方式处理失败**。

**任务设定**：对 `0..1_000_000` 的整数计算 `1_000_000_000 / (i - 500_000)`，其中 `i = 500_000` 时除数为零。

**版本一：不设防（观察 panic 传播）**

```rust
// 示例代码
use rayon::prelude::*;
use std::panic;
use std::sync::atomic::{AtomicUsize, Ordering};

let visited = AtomicUsize::new(0);
let r = panic::catch_unwind(|| {
    (0..1_000_000_i64).into_par_iter().for_each(|i| {
        visited.fetch_add(1, Ordering::Relaxed);
        let _ = 1_000_000_000 / (i - 500_000); // i == 500_000 时 panic
    });
});
assert!(r.is_err());
println!("v1 访问 {} / 1_000_000", visited.into_inner());
```

观察点：panic 消息 `attempt to divide by zero` 出现，程序不死锁、不返回错误值；默认多线程下 `visited` 接近 1_000_000——正如 4.1 所说，其余任务照跑。

**版本二：`.panic_fuse()` 止损**

把迭代器改成 `(0..1_000_000_i64).into_par_iter().panic_fuse().for_each(...)`（其余不变）。观察 `visited` 是否显著下降。想看到稳定差距，可参照 4.4.4 用 `ThreadPoolBuilder::new().num_threads(1).build()` 包住并加 `.with_max_len(1)`。

**版本三：`try_reduce_with` 返回 `Result`**

```rust
// 示例代码
let outcome: Option<Result<i64, String>> = (0..1_000_000_i64)
    .into_par_iter()
    .map(|i| {
        let d = i - 500_000;
        if d == 0 {
            Err("division by zero at 500000".to_string())
        } else {
            Ok(1_000_000_000 / d)
        }
    })
    .try_reduce_with(|a, b| Ok(a + b));

println!("v3 = {:?}", outcome);
// 预期: Some(Err("division by zero at 500000"))
```

观察点：**全程没有 panic**，错误作为普通值返回，调用方可以正常处理；受 `try_reduce_with` 的全局短路影响，除零之后的元素大多不再参与计算。

**验收清单**：

1. 版本一、二里 `catch_unwind` 都捕获到 `Err`，且三个版本结束后再跑一次 `par_iter().sum()` 仍正确（线程池未损坏）。
2. 版本二的 `visited` 小于版本一（单线程 + `with_max_len(1)` 时应严格小于）。
3. 版本能拿到 `Some(Err(...))`，程序以正常流程退出。
4. 回答一个问题：如果你的闭包代价低（纯算术），`panic_fuse` 的额外同步开销值得吗？什么时候才值得？（提示：每元素代价越高、元素越多，止损收益越大；参考 [src/iter/mod.rs:L1943-L1945](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1943-L1945) 的 sleep 示例。）

## 6. 本讲小结

- Rayon 把失败分两档：`Result` / `Option` 走**可恢复**的值语义短路；panic 走 **unwind 捕获—搬运—重放**，最终一定传播回调用点（4.1）。
- `try_fold` / `try_fold_with` 是**惰性**适配器，只在**本切片内**短路；全局短路要靠下游的 `try_reduce` / `try_reduce_with`，后者用共享 `AtomicBool` + `full()` 钩子让所有线程提前收工（4.2）。
- 多个并行错误同时存在时，`try_reduce` 返回哪一个是不确定的；`try_reduce_with` 的返回值有三层状态：空 / 失败 / 成功（4.2）。
- `while_some` 把第一个 `None` 变成全管道的停止信号，产出裸 `T`；它没有 indexed 实现（4.3）。
- panic 之后其余任务默认**仍会跑完**（`join` 必须等另一支结束以保证借用安全）；`panic_fuse` 用 `Drop` 守卫 + 共享标志做到「尽力」快速止损，代价是额外同步开销（4.4）。
- 所有短路机制都建立在 plumbing 的 `Consumer::full` / `Folder::full` 契约之上——驱动循环轮询它来决定停止切分与投喂。

## 7. 下一步学习建议

- **u3-l1（无状态适配器）**：本讲反复出现「适配器包装消费者」的结构，下一单元从 `map` / `filter` 与 `delegate!` 宏开始系统拆解适配器的实现套路。
- **u3-l2（fold 与 reduce）**：深入对照 `fold` / `reduce` 的完整实现，会看到 `try_*` 家族正是它们的可失败变体。
- **u6-l4（panic 传播与 unwind 安全）**：如果你对 4.1 的 `JobResult` / latch / `join_recover_from_panic` 意犹未尽，那一讲从 rayon-core 视角完整剖析 panic 的跨线程旅程与 `sort-panic-safe` 测试。
- 想立刻动手的读者，推荐通读 [src/iter/mod.rs:L1045-L1130](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1045-L1130)（`try_reduce` / `try_reduce_with` 的完整文档，含多个可运行的 doctest）并逐个改写验证。
