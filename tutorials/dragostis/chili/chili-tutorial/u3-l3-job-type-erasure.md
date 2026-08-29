# Job 体系：类型擦除与任务队列

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `JobStack`、`Job`、`JobShared`、`JobQueue`、`Channel` 五个类型各自的职责，以及它们如何协作完成「一次 join 任务的入队、送出、执行、回传」。
2. 理解 chili 如何用「`unsafe fn` 指针 + `NonNull<JobStack>`」把任意 `FnOnce` 闭包擦除成统一的 `Job`，放进一个不含任何泛型参数的队列。
3. 理解「通道延迟创建」：为什么 `channel()` 直到 `pop_front` 才被调用，`Receiver` 又是怎么通过 `Cell` 装回 `Job::receiver` 的。
4. 理解 `Job`（本地队列，不可跨线程）与 `JobShared`（可跨线程的拷贝）的本质区别，以及 `mem::transmute(Sender → Sender<T>)` 依赖 `repr(C)` 与 `Box` 定长的完整论证。
5. 画出五者结构关系图，并标注两个源文件中所有 unsafe 调用点及其守护的不变量。

## 2. 前置知识

### 2.1 泛型单态化与类型擦除

Rust 的泛型在编译期为每种具体类型生成一份独立代码（单态化）。于是 `JobStack<闭包A>` 和 `JobStack<闭包B>` 是**两个不同的类型**，大小、内容都不同。但 `JobQueue` 想用一个 `VecDeque` 统一存放所有任务——它必须存一种**确定的**元素类型。把各种不同类型装进同一种容器类型的过程就叫**类型擦除**（type erasure），擦除后要想恢复，必须有人在恰当的地方把类型信息「变回来」。本讲的主线就是 chili 的这条「擦除 → 恢复」往返。

常见的擦除手段是 `Box<dyn FnOnce(...)>`（trait 对象）：但那需要**每个任务一次堆分配**，而 chili 的热路径（u2-l1 讲过，大多数 join 走 `join_seq`、少数走 `join_heartbeat`）连一次堆分配都不想要。chili 的选择是：闭包留在**栈上**，容器里只存裸指针 + 函数指针。

### 2.2 `NonNull<T>`

`NonNull<T>` 是「保证非空、但无生命周期」的裸指针包装（`*mut T` 的别名）。它和引用 `&T` 的区别：

- `&T` 携带生命周期，会传染给所在结构体；`NonNull<T>` 没有。
- `NonNull<T>` 默认既不是 `Send` 也不是 `Sync`，解引用需要 `unsafe`。

在本讲的语境里，`NonNull<JobStack>`（注意：`JobStack` 带默认参数，等价于 `JobStack<()>`）的含义是「指向某个 `JobStack<F>` 的指针，但 F 是什么我已经忘了」——这就是对 F 的擦除。

### 2.3 `UnsafeCell` 与 `Cell`（回顾 u3-l2）

u3-l2 已经精读过 `Channel`：`UnsafeCell` 是「内部可变性」的原语，告诉编译器「这块内存可能被共享引用背后的代码改写」。`Cell<T>` 是 `UnsafeCell` 之上「只能整体取出/放回」的封装。本讲会看到 `Cell<Option<Receiver<T>>>` 的一个特殊用法：**通过擦除后的类型视图写入，通过具体类型视图读出**。

### 2.4 `ManuallyDrop<T>`

`ManuallyDrop<T>` 是一个「包装后就不会在离开作用域时自动 drop」的包装器。它的价值在于把「什么时候释放」的决定权从编译器手里交给程序员。chili 用它表达：闭包 F 只能被 `take_once()` **移动出来一次**，之后 `JobStack`（在栈帧结束时）不得再对它做任何事。

### 2.5 `repr(C)` 与 `transmute`

Rust 默认（`repr(Rust)`）不承诺结构体字段的内存排列顺序，编译器可以重排。`#[repr(C)]` 强制按声明顺序排列、遵循 C 的对齐规则，于是「同形结构体布局一致」变成可论证的命题。`mem::transmute` 是「按位重新解释」的终极 unsafe 操作：把一段字节从类型 A 硬当成类型 B。它安全的前提是 A、B 布局完全兼容——本讲 4.2 会看到 chili 如何用 `repr(C)` + 「唯一依赖 T 的字段是定宽的 `Box`」把这个前提论证出来。

### 2.6 其他

- `VecDeque`：双端队列，`push_back`/`pop_back`/`pop_front` 都是 O(1)。u2-l1 讲过「队头 = 递归最外层、通常粒度最大的任务」。
- `Arc`：原子引用计数的共享指针，`channel()` 里那个 `Arc<Channel<T>>` 靠它让发送端、接收端共享同一块内存。
- 本库开启了 `#![deny(unsafe_op_in_unsafe_fn)]`（[src/lib.rs:L2](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L2)），所以 `unsafe fn` 内部的每个危险操作都还要再套一层显式 `unsafe { }` 块——数 unsafe 调用点时不会漏。

## 3. 本讲源码地图

| 位置 | 作用 |
| --- | --- |
| [src/job.rs:L113-L134](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L113-L134) | `JobStack`：闭包在栈上的住所，`take_once` 单次取出 |
| [src/job.rs:L136-L205](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L136-L205) | `Job`：擦除后的「任务把手」，含 `harness` 函数指针与 `receiver` 槽位 |
| [src/job.rs:L207-L234](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L207-L234) | `JobShared`：可跨线程的拷贝，`unsafe impl Send` 论证 |
| [src/job.rs:L236-L275](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L236-L275) | `JobQueue`：`VecDeque<NonNull<Job>>` 及 push/pop 三件套 |
| [src/job.rs:L23-L111](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L23-L111) | `Channel`/`Sender`/`Receiver`（u3-l2 已精读，本讲只引用其布局属性） |
| [src/lib.rs:L348-L390](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L348-L390) | `join_heartbeat`：`JobStack`/`Job` 的唯一诞生地，两条结局的分岔口 |
| [src/lib.rs:L318-L333](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L318-L333) | `heartbeat()`：调用 `pop_front` 把任务送上货架的唯一入口 |
| [src/lib.rs:L284-L316](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L284-L316) | `wait_for_sent_job`：原线程等待结果的循环 |
| [src/lib.rs:L112-L145](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L112-L145) | `execute_worker`：worker 线程消费 `JobShared` 的主循环 |

提醒：`mod job;` 是私有模块（[src/lib.rs:L63](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L63)），所以 `Job` 虽然标了 `pub`，外部用户在文档里也看不到它——整个 Job 体系是纯内部实现，公共 API 始终只有 `Scope`/`Config`/`ThreadPool`（u1-l2 已确认）。

## 4. 核心概念与源码讲解

### 4.1 JobStack 与 take_once

#### 4.1.1 概念说明

`join_heartbeat` 每次都要把闭包 `a` 暂存起来（先执行 `b`，`a` 可能被别人偷走）。存哪里？

- 存 `Box`：每次 join 一次堆分配，热路径不可接受（u1-l1 的「每节点约 3.5ns」就没了）。
- 存局部变量再 move 进队列：队列元素类型必须统一，而每个闭包类型都不同。

chili 的答案：**闭包留在 `join_heartbeat` 自己的栈帧里**，包一层 `JobStack`，队列只存指向它的指针。`JobStack` 的两个组成部分各有分工：

- `UnsafeCell`：允许「通过 `&JobStack` 把闭包 move 出去」——否则 `take_once(&self)` 拿的是共享引用，根本无法返回 `F` 的所有权。
- `ManuallyDrop`：闭包被 move 出去之后，`JobStack` 随栈帧结束时**不得再 drop 一次** F（否则是双重释放）；反过来，如果始终没人取走，F 也**不会被自动清理**——文档注释明确警告这会泄漏内存。

#### 4.1.2 核心流程

`JobStack` 的一生：

```
JobStack::new(a)                    闭包 a 进入栈帧，穿上 ManuallyDrop
        │
        │  随 Job 入队（见 4.3），等待三种结局之一：
        │
        ├─ 结局①：任务被送出并执行 ──► harness 内 stack.take_once()   [job.rs:L167]
        ├─ 结局②：送出后无人接手，从货架撤回 ──► 原线程 take_once()     [lib.rs:L377]
        └─ 结局③：从未被送出（pop_back 收回）──► 原线程 take_once()     [lib.rs:L388]
                │
                ▼
        栈帧结束，JobStack 离开作用域；ManuallyDrop 保证「已取走」时不重复 drop
```

三条结局**恰好发生一条**——这就是 `take_once` 的单次性契约。若一条都不发生（理论上当前代码路径不会，但注释提醒维护者），F 泄漏。

#### 4.1.3 源码精读

结构体定义（[src/job.rs:L113-L117](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L113-L117)）：`f: UnsafeCell<ManuallyDrop<F>>`，文档注释警告所有代码路径都应调用取走操作以避免泄漏。注意它上面的注释提到 `Job::execute` 和 `Self::unwrap`——这两个方法名在当前代码里**并不存在**，是 `617cbd4`（"Refactored and simplified jobs."）重构前的旧 API 名，可以用 `git show 901634b:src/job.rs` 验证。注释滞后于重构，但它描述的**不变量**（必须有人取走，否则泄漏）依然成立。

构造函数（[src/job.rs:L119-L124](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L119-L124)）：普通安全函数，只是把 `f` 包进 `UnsafeCell` + `ManuallyDrop`，无任何契约。

取出操作（[src/job.rs:L126-L133](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L126-L133)）：

```rust
/// SAFETY:
/// It should only be called once.
pub unsafe fn take_once(&self) -> F {
    unsafe { ManuallyDrop::take(&mut *self.f.get()) }
}
```

- 函数签名是 `unsafe fn ... (&self) -> F`：拿**共享引用**却返回**所有权**，这在安全 Rust 里不可能，必须靠 `UnsafeCell` + 调用方保证「只此一次」。
- `ManuallyDrop::take` 把 F 从 `ManuallyDrop` 中 move 出来，原位置进入「逻辑上未初始化」状态。第二次调用等于二次 move——未定义行为。
- 这也解释了为什么本库要 `deny(unsafe_op_in_unsafe_fn)`：`unsafe fn` 内部的 `ManuallyDrop::take` 仍需显式 `unsafe {}` 标注， SAFETY 注释逐块可查。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：验证「三个 `take_once` 调用点互斥」，理解单次性契约由谁保证。

**操作步骤**：

1. 在编辑器中打开 `src/lib.rs`，定位 `join_heartbeat`（L348-L390）。
2. 用搜索找到 `take_once` 的全部调用点（应共 3 处：`src/job.rs:L167`、`src/lib.rs:L377`、`src/lib.rs:L388`）。
3. 对每个调用点，阅读它上方紧挨着的 SAFETY 注释，在笔记里各写一句话回答：「这一处凭什么保证是第一次也是唯一一次调用？」

**需要观察的现象**：三处调用分别处于什么分支条件下；`lib.rs` 的两处是否被 `if/else` 严格隔开。

**预期结果**（参考答案）：

| 调用点 | 所在分支 | 为何只有这一次 |
| --- | --- | --- |
| `job.rs:L167` | 任务已被 `pop_front` 送出、`harness` 正在执行 | 注释指出 `Job::execute`（现即 `JobShared::execute`）只会在任务出队后调用，是第一次 |
| `lib.rs:L377` | `wait_for_sent_job` 返回 `None`（任务上了货架但被原线程 `remove` 撤回，无人执行过） | 撤回时持锁移除，worker 再也拿不到，harness 永不运行 |
| `lib.rs:L388` | `take_receiver()` 返回 `None`，任务经 `pop_back` 从队尾收回 | 从未出队，harness 无从运行 |

`lib.rs` 的两处位于 `if let Some(receiver) = ... { } else { }` 的两侧，天然互斥；`job.rs` 的一处则意味着 receiver 已被取走、原线程走 `recv` 等待路径，不会触碰 `take_once`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `ManuallyDrop` 去掉，直接用 `UnsafeCell<F>`，会发生什么？

**答案**：`take_once` move 出 F 后，`JobStack` 离开作用域时还会自动 drop 那块已移出的内存——双重释放（UB）。`ManuallyDrop` 的职责就是关闭自动 drop，把「取走即终结」变成显式契约。

**练习 2**：`take_once` 为什么拿 `&self` 而不是 `self` 或 `&mut self`？

**答案**：`JobStack` 被 `NonNull` 指向、可能同时被原线程（栈上变量）和队列里的指针「共享」视角访问，拿 `&mut self` 需要独占借用，调用点给不出；拿 `self` 会移走整个 `JobStack`，而闭包要留在这个栈地址上。`&self` + `UnsafeCell` + 单次性契约是唯一能表达「从共享指针后面移出内容」的组合。

**练习 3**：`JobStack<F = ()>` 的默认参数有什么用？

**答案**：让 `NonNull<JobStack>`（即 `NonNull<JobStack<()>>`）成为可写下的**具体类型**，供 `Job`/`JobShared`/`harness` 签名使用——这是擦除 F 的语法前提。

### 4.2 Job 与 harness 类型擦除

#### 4.2.1 概念说明

队列里存的不能是闭包本身，那就存一个「任务把手」`Job`，它只带三样**定宽**的东西：

```rust
#[repr(C)]
pub struct Job<T = ()> {
    stack: NonNull<JobStack>,                                    // 闭包在哪（擦除了 F）
    harness: unsafe fn(&mut Scope<'_>, NonNull<JobStack>, Sender), // 怎么执行（擦除了 F 和 T）
    receiver: Cell<Option<Receiver<T>>>,                          // 结果怎么取回（唯一依赖 T 的字段）
}
```

`harness` 是关键：`Job::new` 在单态化上下文里把具体的 `harness::<F, T>` 函数** coerce 成统一的 `unsafe fn` 指针**存起来。函数指针是定宽的（8 字节），于是 `Job<T>` 无论 F、T 是什么都同构。类型信息没有消失——它被**封印在函数指针指向的那份单态化代码里**，只有 harness 函数体知道如何恢复：

- `stack.cast().as_ref()` 把 `NonNull<JobStack>` 变回 `&JobStack<F>`——恢复 F；
- `mem::transmute(sender)` 把 `Sender` 变回 `Sender<T>`——恢复 T。

对照_trait 对象_方案：`Box<dyn FnOnce>` 同样能擦除，但每次入队都要堆分配、调用要走 vtable，而且不同返回值 `T` 也没法统一成一种 trait 对象类型。chili 的方案让**未送出的 join 一次堆分配都没有**；唯一的堆分配（`Arc::new(Channel)`）发生在任务真正被送出的那一刻——见 4.3。

#### 4.2.2 核心流程

一次完整的「擦除 → 恢复」往返：

```
join_heartbeat（原线程，单态化上下文 F=闭包类型, T=返回值类型）
  │ JobStack::new(a)            闭包上栈
  │ Job::<T>::new(&stack)       造把手：stack 指针 + harness::<F,T> 函数指针 + receiver=None
  │ push_back(&job) ──cast──►   VecDeque<NonNull<Job>>          ← F、T 在此全部消失
  │
  │            ┌──────────── 冷路径：heartbeat() 调 pop_front ────────────┐
  ▼            ▼                                                      │
  热路径：b(self) 执行完毕                                             │
  │            pop_front() 内：channel() 新建通道；                     │
  │                      job.receiver.set(Some(receiver))（经擦除视图写入）│
  │                      组装 JobShared{stack, harness, sender}          │
  │                                          │ 送上货架 shared_jobs       │
  │                                          ▼ （worker 或帮忙等待的线程）
  │                              JobShared::execute(scope)               │
  │                                ├ stack.cast().as_ref() → &JobStack<F> │ ← 恢复 F
  │                                ├ stack.take_once()     → 闭包 a       │
  │                                ├ transmute(sender)     → Sender<T>    │ ← 恢复 T
  │                                └ sender.send(catch_unwind(|| a(scope)))
  ▼                                                                      │
  take_receiver() ──► Some(Receiver<T>) ──► recv() ──► thread::Result<T> ─┘
```

注意恢复动作**全部发生在 harness 函数体内**——因为只有这份单态化代码记得 F 和 T 是什么。

#### 4.2.3 源码精读

`Job` 结构体及其文档（[src/job.rs:L136-L145](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L136-L145)）：`#[repr(C)]` 保证字段顺序固定；文档注释点明 `Job` 只被**发送**、不被共享，出队时会**拷贝**成 `JobShared` 再跨线程（详见 4.3）。三个字段里只有 `receiver` 依赖 `T`，而 `Receiver<T>` 只是 `Arc<Channel<T>>` 的 newtype——一个指针宽，与 T 无关。

`Job::new` 与擦除点（[src/job.rs:L147-L183](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L147-L183)）：

```rust
Self {
    stack: NonNull::from(stack).cast(),   // &JobStack<F> → NonNull<JobStack>：擦除 F
    harness: harness::<F, T>,             // 具体函数 → 统一 unsafe fn 指针：擦除 F、T
    receiver: Cell::new(None),
}
```

`harness` 函数体（[src/job.rs:L153-L176](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L153-L176)）按顺序做四件事，每步都有 SAFETY 注释：

1. [L162](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L162)：`stack.cast().as_ref()` 恢复 `&JobStack<F>`，安全性依赖「JobStack 仍然活着」（由 `JobShared::execute` 的契约保证）。
2. [L167](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L167)：`take_once()` 取出闭包，安全性依赖「任务已出队、harness 只会被调用这一次」。
3. [L173](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L173)：`mem::transmute(sender)` 恢复 `Sender<T>`。SAFETY 注释给出完整论证：`Channel` 唯一引用 T 的字段是 `Option<Box<thread::Result<T>>>`，`Box` 定宽，且 `#[repr(C)]` 固定字段顺序，故 `Channel` 与 `Channel<T>` 布局相同。布局恒等式：

   \[ \texttt{Channel<T>} \;=\; \underbrace{\texttt{state}}_{1\,\text{字节}} \;+\; \underbrace{\text{padding}}_{7\,\text{字节}} \;+\; \underbrace{\texttt{waiting\_thread}}_{8\,\text{字节}} \;+\; \underbrace{\texttt{val}}_{8\,\text{字节}} \]

   右端每一项的宽度与对齐都不依赖 T（64 位平台下合计 24 字节，待本地验证），于是 \(\text{size}(\texttt{Channel}\langle T\rangle)\) 是与 T 无关的常量，`Sender`（`Arc<Channel>` 的 newtype）随之同构，transmute 只是把类型标签换掉、字节纹丝不动。
4. [L175](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L175)：`sender.send(panic::catch_unwind(AssertUnwindSafe(|| f(scope))))`——执行闭包、捕获 panic、把 `thread::Result<T>` 写进通道。panic 跨线程传播的细节属于 u4-l2 的主题。

`take_receiver`（[src/job.rs:L185-L188](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L185-L188)）：通过 `Cell::take` 把 receiver 整个取出（取出后槽位变 `None`）。返回 `Some` 当且仅当这个任务曾被 `pop_front` 送出——这是 `join_heartbeat` 判断两条结局的开关（[src/lib.rs:L368](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L368)）。

顺带一提，`Debug for Job`（[src/job.rs:L190-L205](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L190-L205)）也借助同一个 `Cell`：先把 receiver 取出来打印、再放回去——`Debug` 只拿到 `&self`，能改字段全靠内部可变性。小彩蛋：它把打印出来的字段命名为 `"sender"`，但值的类型其实是 `Receiver`（ cosmetic 级别的小笔误，阅读源码时别被迷惑）。

#### 4.2.4 代码实践

**实践目标**：用 `size_of`/`align_of` 亲手验证「布局与 T 无关」这条 transmute 安全性的基石。

**操作步骤**：

1. 新建一个独立 crate（`cargo new layout-check`），在 `main.rs` 里粘贴以下**示例代码**（简化复刻，仅用于验证布局论证，非项目原有代码；项目里的 `Channel`/`Job` 是私有类型，外部无法直接引用）：

   ```rust
   // 示例代码：复刻 job.rs 中 Channel / Job 的字段骨架，验证「与 T 无关」
   use std::{
       cell::{Cell, UnsafeCell},
       mem::{align_of, size_of},
       sync::Arc,
       sync::atomic::AtomicU8,
       thread,
   };

   #[repr(C)]
   struct Channel<T = ()> {
       state: AtomicU8,
       waiting_thread: UnsafeCell<Option<thread::Thread>>,
       val: UnsafeCell<Option<Box<thread::Result<T>>>>,
   }

   #[repr(C)]
   struct JobSkeleton<T = ()> {
       stack: *const (),                                // NonNull 也是一指针宽
       harness: unsafe fn(),                            // 函数指针均 8 字节
       receiver: Cell<Option<Arc<Channel<T>>>>,         // Receiver<T> 即 Arc<Channel<T>>
   }

   fn main() {
       for type_name in ["()", "u64", "[u8; 1024]"] {
           println!("type = {type_name}");
       }
       println!("Channel<()>         size = {:2}, align = {}", size_of::<Channel<()>>(),        align_of::<Channel<()>>());
       println!("Channel<u64>        size = {:2}, align = {}", size_of::<Channel<u64>>(),       align_of::<Channel<u64>>());
       println!("Channel<[u8;1024]>  size = {:2}, align = {}", size_of::<Channel<[u8;1024]>>(), align_of::<Channel<[u8;1024]>>());
       println!("JobSkeleton<()>    size = {:2}", size_of::<JobSkeleton<()>>());
       println!("JobSkeleton<u64>   size = {:2}", size_of::<JobSkeleton<u64>>());
   }
   ```

2. `cargo run` 运行。

**需要观察的现象**：三组 `Channel<...>` 的 size 和 align 是否完全一致；两个 `JobSkeleton<...>` 的 size 是否一致。

**预期结果**：64 位平台上 `Channel` 应为 24 字节、对齐 8；`JobSkeleton` 应为 24 字节（8+8+8），与 T 取什么无关（待本地验证）。若任何一行数字不同，transmute 的论证就破产——这正是这个实验的价值。

#### 4.2.5 小练习与答案

**练习 1**：`harness` 的函数指针类型是 `unsafe fn(&mut Scope<'_>, NonNull<JobStack>, Sender)`，签名里为什么容不下 F 和 T？

**答案**：这个指针要存进**非泛型**的 `Job`/`JobShared`，而 Rust 没有 Existential 类型让结构体「记住」某个未知泛型参数。签名固定 + 单态化函数自我记忆（cast/transmute 恢复）是唯一组合。

**练习 2**：去掉 `Channel` 上的 `#[repr(C)]`（[src/job.rs:L24](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L24)），transmute 还能论证安全吗？

**答案**：不能。`repr(Rust)` 下字段排列由编译器自由决定，且不同单态化实例可以采用不同排列，`Channel<()>` 与 `Channel<T>` 字段偏移一致就失去了保证（即便字段宽度恰好相同）。transmute 的安全性论证链条上 `repr(C)` 不可缺。

**练习 3**：为什么恢复 `Sender<T>` 的 transmute 放在 harness 里，而不是在 `pop_front` 里直接构造 `Sender<T>`？

**答案**：`pop_front` 手里只有擦除后的 `&Job`（即 `&Job<()>`），根本不知道 T；类型信息只存在于单态化的 `harness::<F, T>` 代码中，恢复只能发生在那里。

### 4.3 JobQueue 与 JobShared

#### 4.3.1 概念说明

现在回答本讲最后两个问题：队列长什么样？任务怎么跨线程？

**`JobQueue`** 是纯本地结构：`VecDeque<NonNull<Job>>`。队列元素是**指针**，指向 `join_heartbeat` 栈帧上的 `Job<T>`。注意 `Job` 自身**不是** `Send`（`NonNull` 非 Send、`Cell` 非 Sync），它从不离开本线程。

**`JobShared`** 是 `pop_front` 时从 `Job` **拷贝**出的三个字段：`stack`、`harness`（两者都是 Copy 的裸指针/函数指针）加上**新建的** `sender`。它不含 `Cell`、不含泛型，配上仔细论证的 `unsafe impl Send`，可以放进锁保护的货架 `shared_jobs: BTreeMap<usize, (u64, JobShared)>`（[src/lib.rs:L77](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L77)）跨线程流转。

**通道为何延迟到 `pop_front` 才建**：`channel()` 要 `Arc::new(...)` 堆分配（[src/job.rs:L107-L111](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L107-L111)）。而绝大多数任务永远不会被送出（u2-l1 的降频 + 队列长度门槛），提前建通道等于给热路径白加一次分配。只有 `pop_front` 真正发生（任务即将离开本地控制）的那一刻才值得付这笔钱——**冷路径付冷开销，热路径零分配**，这与 u2-l3「热路径不碰锁」是同一个设计哲学。

#### 4.3.2 核心流程

`JobQueue` 上的三条原语，对应一个任务的两种结局：

```
push_back(&job)   入队尾（join_heartbeat 每次都做）          [lib.rs:L358-L360]
        │
        ├── pop_back()   未被送出：从队尾收回指针、丢弃，       [lib.rs:L382]
        │                原线程 take_once 本地执行（结局③）
        │
        └── pop_front()  送出（仅 heartbeat() 或等待时偷任务触发）[lib.rs:L324]
                         ├─ channel() 新建 Sender/Receiver
                         ├─ job.receiver.set(Some(receiver))  ← 经 &Job<()> 视图写入，
                         │                                      原线程经 &Job<T> 视图读出
                         └─ 返回 JobShared{stack, harness, sender}
                                │ 送上货架，被 worker / 帮忙线程取走
                                ▼
                         JobShared::execute(scope) → harness（见 4.2）
```

`pop_front` 里那次 `Cell::set` 是全库最精妙的一笔：此刻内存里的真实对象是 `Job<RA>`，字段类型 `Cell<Option<Receiver<RA>>>`；而代码拿着的是擦除视图 `&Job<()>`，写入的是 `Receiver<()>`。**写的类型和读的类型不一样**（原线程随后在 [src/lib.rs:L368](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L368) 以 `&Job<RA>` 读出 `Receiver<RA>`），安全靠的正是 `#[repr(C)]` + 「唯一依赖 T 的字段定宽」——和 transmute 同一条布局论证，一次隐式、一次显式。

背后还有一条**生命周期契约**串起整个体系：`push_back` 的 SAFETY 要求「Job 在被 pop 之前存活」；而跨线程执行时 `JobStack` 活在**原线程的栈帧**上——原线程之所以不返回，是因为它在 `wait_for_sent_job` 里等到 `recv` 拿到结果才继续（[src/lib.rs:L284-L316](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L284-L316)）。`execute_worker` 里那句 SAFETY 注释（[src/lib.rs:L125-L127](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L125-L127)）说的就是它："Any Job that was shared between threads is waited upon before the JobStack exits scope."

#### 4.3.3 源码精读

`JobQueue` 定义与 `len`（[src/job.rs:L236-L242](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L236-L242)）：`VecDeque<NonNull<Job>>`——队列从头到尾不知道任何泛型参数；`len` 被 `join_with_heartbeat_every` 的 `job_queue.len() < 3` 门槛使用（u2-l1）。

`push_back`（[src/job.rs:L244-L249](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L244-L249)）：`NonNull::from(&*job).cast()` 把 `NonNull<Job<T>>` 擦成 `NonNull<Job>` 入队。SAFETY 要求调用方保证 Job 存活到出队——由 `join_heartbeat` 的栈帧满足（[src/lib.rs:L358-L360](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L358-L360) 的注释写明 "job is alive until the end of this scope"）。

`pop_back`（[src/job.rs:L251-L253](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L251-L253)）：只是把指针从队列丢弃。`NonNull` 不拥有对象、`Job` 本体仍安然活在栈上并在栈帧结束时正常 drop，所以这里**无需** unsafe。

`pop_front`（[src/job.rs:L255-L274](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L255-L274)）：出队、建通道、装回 receiver、组装 `JobShared`。SAFETY 注释里写的字段类型是 `Cell<Option<NonNull<Future<T>>>>`——同 4.1.3 的考证，`Future` 是重构前的旧类型名（当前字段是 `Cell<Option<Receiver<T>>>`），但「唯一依赖 T 的字段定宽 + `repr(C)` 保字段顺序」这条论证对两者都成立。注意 `channel()` 此刻被推断为 `channel::<()>`，与擦除视图的类型对齐。

`JobShared` 与 `execute`（[src/job.rs:L207-L225](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L207-L225)）：`execute` 消费 self（保证只执行一次），契约有两条——`JobStack` 仍活着、且已经出队。调用 `(self.harness)(scope, self.stack, self.sender)` 进入 4.2 精读过的 harness。全库调用它的只有两处：worker 主循环（[src/lib.rs:L128-L130](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L128-L130)）和帮忙式等待（[src/lib.rs:L307-L309](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L307-L309)），两处的 SAFETY 注释都引用同一条生命周期契约。

`unsafe impl Send`（[src/job.rs:L227-L234](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L227-L234)）：论证分两半——`stack` 只会被**执行该任务的线程**独占访问（且 `execute` 消费 self）；`sender` 要么被原线程做原子只读检查（`Receiver::is_empty`），要么被执行线程 `send` 一次。没有并发写共享数据，故 Send 成立。货架的另一端 `heartbeat()`（[src/lib.rs:L318-L333](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L318-L333)）在锁内 `Vacant` 去重后调 `pop_front` 并插入货架、`notify_one` 唤醒 worker——u2-l4 已精读时序，本讲只关注其中 `pop_front` 这一环。

#### 4.3.4 代码实践

**实践目标**：跟踪一个 `Job` 的两条结局，盯住 `receiver` 槽位的状态变化。

**操作步骤**：

1. 对照 [src/lib.rs:L348-L390](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L348-L390)，在笔记里抄下这张表并补全问号处：

| 路径 | 触发条件 | 队列里的指针 | `job.receiver` | 闭包最终由谁执行 | 结果如何回到原线程 |
| --- | --- | --- | --- | --- | --- |
| 收回（结局③） | `take_receiver()` 返回 `?` | 已被 `pop_back` 移除 | ? | ? | ? |
| 送出（结局①） | `take_receiver()` 返回 `?` | 已被 `pop_front` 移除 | ? | ? | ? |
| 撤回（结局②） | 送出后 `wait_for_sent_job` 里 `remove` 命中 | 已移除 | 已被取走 | ? | ? |

2. 运行专门制造跨线程执行的测试：`cargo test --release join_wait -- --nocapture`（该测试配置了 2 线程、1µs 心跳、`TIMES = 1`，见 [src/lib.rs:L717-L747](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L717-L747)）。

**需要观察的现象**：测试通过说明两条路径混合出现时结果仍正确（10 个元素全部 +1）。

**预期结果**（表格参考答案）：

| 路径 | 触发条件 | `job.receiver` | 闭包由谁执行 | 结果如何返回 |
| --- | --- | --- | --- | --- |
| 收回 | `take_receiver()` 返回 `None` | 始终为 `None` | 原线程 `take_once` 后直接调用 | 函数返回值直接就是结果 |
| 送出 | `take_receiver()` 返回 `Some` | `pop_front` 时经 `Cell::set` 变为 `Some`，随即被取走 | worker 或帮忙线程内的 harness | `sender.send(...)` → `receiver.recv()` |
| 撤回 | 货架 `remove` 命中（无人执行过） | 已被取走（`Some`） | 原线程 `take_once` 后直接调用 | 函数返回值直接就是结果 |

#### 4.3.5 小练习与答案

**练习 1**：`Job` 和 `JobShared` 都带 `stack` 指针和 `harness` 函数指针，为什么不直接让 `Job` 实现 `Send` 送过去，而要拷贝一份？

**答案**：`Job` 还带 `Cell<Option<Receiver<T>>>`，`Cell` 非 `Sync`、`NonNull` 非 `Send`，自动 trait 判定不过关；而且 `Job` 是泛型 `Job<T>`，货架是非泛型的 `BTreeMap<usize, (u64, JobShared)>`。`JobShared` 丢弃 `Cell`、擦掉 T、只携带可论证安全的三个字段，是为跨线程量身定制的「轻装拷贝」。

**练习 2**：`pop_back` 为什么是安全函数，而 `push_back` 是 `unsafe fn`？

**答案**：`pop_back` 只是丢掉一个不拥有对象的指针，`Job` 本体仍在栈上正常生存与 drop，无契约可言。`push_back` 则建立了新 invariant——「队列里这个指针指向的对象必须存活到出队」——编译器无法检查，必须由调用方承诺。

**练习 3**：如果原线程在任务送出后**不等待**就直接返回栈帧，会破坏什么？

**答案**：`JobStack`（以及闭包捕获的一切）随栈帧销毁，而 worker 线程的 harness 还拿着 `NonNull<JobStack>` 并将调用 `take_once`—— use-after-free。所以 `wait_for_sent_job` 的「等 `recv` 才返回」不仅是拿结果，更是整个体系的生命线。

## 5. 综合实践

本讲综合实践正是规格指定的任务：**画出 `JobStack` / `Job` / `JobShared` / `JobQueue` / `Channel` 五者结构关系图并标注所有 unsafe 调用点；然后用一段文字解释 `harness` 中 `mem::transmute(Sender → Sender<T>)` 依赖 `repr(C)` 与 `Box` 定长的原因。**

**实践目标**：把 4.1–4.3 的局部理解拼成一张全局地图，并用布局论证收束 transmute。

**操作步骤**：

1. 白纸或绘图工具上画五个框（`JobStack<F>`、`Job<T>`、`JobShared`、`JobQueue`、`Channel`），用箭头标出：谁持有谁的指针、`Sender`/`Receiver` 如何共享同一个 `Arc<Channel>`、`pop_front` 时字段从 `Job` 流向 `JobShared` 与 `receiver` 槽位。可对照 4.2.2 与 4.3.2 的两张流程图整合。
2. 在图中每个箭头/字段旁标注它对应的 unsafe 调用点（文件:行号）。
3. 完成后与下面的清单核对（两个源文件全部 unsafe 位点，共 21 处）：

| # | 位置 | 类别 | 守护的不变量 |
| --- | --- | --- | --- |
| 1 | [job.rs:L57](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L57) | 内存安全 | 无人读取前写 `waiting_thread`（状态机权限） |
| 2 | [job.rs:L80](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L80) | 内存安全 | Ready 后本线程独占 `val` |
| 3 | [job.rs:L92-L94](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L92-L94) | 内存安全 | 写 `val` 时无人读取 |
| 4 | [job.rs:L100](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L100) | 内存安全 | Waiting 时 `waiting_thread` 已写好 |
| 5 | [job.rs:L128](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L128) | 单次性 | `take_once` 只能调用一次（函数级契约） |
| 6 | [job.rs:L132](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L132) | 单次性 | F 尚未被取走 |
| 7 | [job.rs:L155](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L155) | 类型擦除 | harness 只在出队后调用（函数级契约） |
| 8 | [job.rs:L162](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L162) | 生命周期 | `JobStack` 仍然活着 |
| 9 | [job.rs:L167](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L167) | 单次性 | 第一次也是唯一一次 `take_once` |
| 10 | [job.rs:L173](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L173) | 布局 | `repr(C)` + 定宽字段 ⇒ `Channel` ≅ `Channel<T>` |
| 11 | [job.rs:L218](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L218) | 生命周期 | `execute` 的双重契约（函数级） |
| 12 | [job.rs:L221-L223](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L221-L223) | 生命周期 | 同上 |
| 13 | [job.rs:L234](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L234) | 线程安全 | `stack` 独占、`sender` 单次 |
| 14 | [job.rs:L247](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L247) | 生命周期 | Job 存活到出队（函数级契约） |
| 15 | [job.rs:L248](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L248) | 布局 | 同 #10 |
| 16 | [job.rs:L263](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L263) | 生命周期 + 布局 | 同 #8、#10 |
| 17 | [lib.rs:L128-L130](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L128-L130) | 生命周期 | 先等结果再退出 `JobStack` 作用域 |
| 18 | [lib.rs:L307-L309](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L307-L309) | 生命周期 | 同 #17 |
| 19 | [lib.rs:L358-L360](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L358-L360) | 生命周期 | `job` 活到本作用域末 |
| 20 | [lib.rs:L377](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L377) | 单次性 | 任务撤回、harness 未运行 |
| 21 | [lib.rs:L388](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L388) | 单次性 | 任务收回、harness 未运行 |

4. 撰写 transmute 论证段落，写完与下面的参考要点比对。

**transmute 论证参考要点**：`Sender` 只是 `Arc<Channel<T>>` 的单字段 newtype，transmute 的安全性完全取决于 `Channel` 与 `Channel<T>` 布局一致。`Channel` 三个字段中，`state: AtomicU8` 与 `waiting_thread: UnsafeCell<Option<Thread>>` 都不依赖 T；唯一依赖 T 的 `val: UnsafeCell<Option<Box<thread::Result<T>>>>` 中，`Box` 是一个定宽（非胖）指针，`Option` 又借 `Box` 的非空 niché 与指针同宽，因此该字段宽度与对齐不随 T 变化。`#[repr(C)]` 进一步把「字段按声明顺序排列」变成硬保证，使两个实例的字段偏移逐一相同。于是 \(\text{size}\)、\(\text{align}\)、字段偏移三个量都与 T 无关，transmute 只更换类型标签而不移动任何字节；再加上 `T: Send` 的 where 约束（[src/job.rs:L156-L158](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L156-L158)），跨线程搬运 `thread::Result<T>` 也不会引入数据竞争。若去掉 `repr(C)`（排列无保证）或把 `Box` 换成内联存储 T（宽度随 T 变化），此论证即失效。

**需要观察的现象 / 预期结果**：图中五个类型的关系应能覆盖 4.2.2 流程图中的每条箭头；unsafe 清单核对后无遗漏（21 处）；transmute 段落应同时提到「定宽、`repr(C)`、`T: Send`」三个要素。全程为源码阅读型实践，无需改动项目代码。

## 6. 本讲小结

- `JobStack` 把闭包留在 `join_heartbeat` 的栈帧上（`UnsafeCell<ManuallyDrop<F>>`），`take_once` 以 `&self` 移出闭包，靠「三条结局恰好发生一条」的单次性契约保证不双重释放、不遗漏 drop。
- `Job` = `NonNull<JobStack>`（擦除 F）+ `unsafe fn` 指针（擦除 F、T）+ `Cell<Option<Receiver<T>>>`；`harness::<F, T>` 的单态化代码是类型信息的唯一存放地，恢复靠 `cast`（还原 F）与 `transmute`（还原 T）。
- `JobQueue` 是非泛型的 `VecDeque<NonNull<Job>>`；`pop_front` 时才 `channel()` 堆分配通道，并经擦除视图 `&Job<()>` 把 `Receiver` 写进 `Cell`、由原线程以具体类型 `&Job<T>` 读出——一次隐式的布局兼容变换。
- `JobShared` 是出队时拷贝出的跨线程轻装版（无 `Cell`、无泛型 + `unsafe impl Send`），在锁保护的书架上流转，由 worker 或帮忙等待的线程 `execute`。
- transmute 与 Cell 双视图共用的安全基石：`#[repr(C)]` 固定字段顺序 + 唯一依赖 T 的字段是定宽的 `Box` 指针；全套 unsafe 依赖三条契约——生命周期（栈帧活到 recv 之后）、单次性（take_once/send 各一次）、布局（同构才准 cast/transmute）。

## 7. 下一步学习建议

本讲已把 Job 体系的静态结构讲完，下一讲 **u4-l1「unsafe 安全不变量全面审读」** 正是顺着第 5 节那张 21 处 unsafe 清单继续深挖：对每类契约构造「违反它会导致什么 UB」的具体场景。另外两条支线也值得一读：

- [src/job.rs:L175](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L175) 的 `catch_unwind` 是 panic 跨线程传播的起点，配合 u4-l2 的 `resume_unwind` 路径阅读。
- 用 `git show 901634b:src/job.rs` 对比重构前的 `Future<T>` 实现，观察 `617cbd4` 如何把它简化成 `Channel` 三件套——这是理解「注释为何滞后、不变量为何不变」的最好材料。
