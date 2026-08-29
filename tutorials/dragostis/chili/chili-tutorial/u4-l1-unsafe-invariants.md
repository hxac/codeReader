# unsafe 安全不变量全面审读

## 1. 本讲目标

chili 全库只有两个源文件，却是「unsafe 密度」极高的代码：每一处裸指针解引用、每一次 `transmute`、每一个 `unsafe impl`，都依赖一条**写在注释里的约定**。本讲把视角从前面几讲的「它怎么工作」切换到「它为什么没出事、什么情况下会出事」。

学完本讲你应该能够：

1. 独立清点全库所有 `SAFETY` 注释的位置，并按**内存安全 / 线程安全 / 单次性**三类归类。
2. 逐条复述每条 `SAFETY` 注释守护的不变量（invariant），并能说出「违反它会触发什么 UB」。
3. 讲清 `Job` 从 `push_back` 入队到被 `pop` 之间必须存活的生命周期契约，以及 `join_heartbeat` 的三条出路如何兑现它。
4. 讲清 `JobStack::take_once` 的单次性契约：为什么只能调用一次、为什么「忘记调用」只是泄漏而「调用两次」是 UB、`ManuallyDrop` 在其中的角色。
5. 掌握一套可复用的 unsafe 审计方法：先清点、再归类、再对每条契约构造违反场景。

本讲是全手册「审读」味道最重的一讲：我们不只是读代码，还要像审阅者一样**主动攻击**这些注释的论证。

## 2. 前置知识

本讲默认你已读完 u3 系列（Channel 状态机、Job 类型擦除）。这里补齐几个审计必备的概念，全部用通俗语言解释。

### 2.1 UB（未定义行为）与 safe Rust 的承诺

Rust 把错误分成两档：**可恢复的错误**（panic、`Result`）和 **UB（Undefined Behavior，未定义行为）**。safe Rust 的核心承诺是：只要不写 `unsafe`，编译器保证你的程序不可能触发 UB——数据竞争、悬垂指针、未初始化读取都被类型系统挡在门外。

一旦写下 `unsafe`，你就接管了这份担保。编译器不再检查你做的事是否合法，只检查语法；**正确性论证转移到了注释里**。这就是本讲的主角 `SAFETY` 注释的由来：它是作者写给编译器（和读者）的「我为什么有资格这么做」的书面证词。

一个关键的区别要记住：

- **泄漏（leak）不是 UB**。一个值永远不被 drop，在 Rust 语义里是合法的（`std::mem::forget` 就是 safe 函数）。
- **重复移出（double move-out）是 UB**。从同一个位置把值移走两次，第二次读到的是未初始化内存。

这个区别是理解 `ManuallyDrop` 设计的钥匙（见 4.4 节）。

### 2.2 unsafe fn 的「契约」观点

`pub unsafe fn take_once(&self) -> F` 这种签名表达的是：**函数本身不检查前条件，调用者负责保证**。函数文档里的 `/// SAFETY:` 就是契约条款。审计 unsafe 代码的核心工作之一，就是核对**每一个调用点是否真的满足了契约**。

与之配套，chili 在 crate 根部开了三条强制 lint（见 4.1.3），把「契约必须写下来、unsafe fn 体内的操作必须显式包块」变成了 CI 硬约束。

### 2.3 本讲会用到的工具类型

| 类型 | 一句话解释 | 在 chili 中的角色 |
|---|---|---|
| `NonNull<T>` | 保证非空的裸指针，但**不保证指向的对象还活着** | 队列里存 `NonNull<Job>`，擦除 `T` 的同时把「是否悬垂」变成调用者的责任 |
| `UnsafeCell<T>` | 「这个字段会被别名写入」的信号，令包含它的类型自动 `!Sync` | `Channel` 的两个数据字段 |
| `ManuallyDrop<T>` | 包装后**不会自动 drop** 的值，取出需手动 `take` | `JobStack` 存闭包 `F` 的容器 |
| `repr(C)` | 固定字段声明顺序、去掉重排自由度 | `Channel` 与 `Job` 跨类型 `cast`/`transmute` 的布局保证 |
| `Send` / `Sync` | 「可移交给别的线程」/「可被多线程引用共享」 | `unsafe impl Send for JobShared` 是全库唯一的手动 trait 实现 |

`Send` 与 `Sync` 的推导关系在本讲会反复用到：`Arc<T>: Send` 要求 `T: Send + Sync`；而 `UnsafeCell` 是 `!Sync`，所以 `Arc<Channel>` 天生不是 `Send`——这正是 `JobShared` 需要手动 `unsafe impl Send` 的根源（4.2 节展开）。

### 2.4 与前几讲的衔接

- u3-l2 已逐行读过 `Channel` 的三态状态机（`Pending`/`Waiting`/`Ready`）。本讲不再重复推导，只从「权限划分」的角度复用其结论。
- u3-l3 已讲过 `JobStack`/`Job`/`JobShared`/`JobQueue` 的类型擦除链条。本讲把这些结构当作**契约的载体**，追问每一步的 unsafe 凭什么成立。

## 3. 本讲源码地图

| 文件 | 角色 | unsafe 标注点数量 |
|---|---|---|
| [src/job.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs) | 任务与通道的私有模块：`Channel`/`Sender`/`Receiver`、`JobStack`/`Job`/`JobShared`/`JobQueue` 全部 unsafe 机制的提供方 | 15 处 |
| [src/lib.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs) | 公共 API 与调度：`join_heartbeat`、`wait_for_sent_job`、`execute_worker`——这些是**兑现** job.rs 契约的调用方 | 5 处 |

另外本讲会用只读 git 命令回溯 `src/job.rs` 的历史（提交 `617cbd4` 之前存在 `Future` 结构与 `Job::execute` 方法），用于解释三处「提到已不存在符号」的遗留注释。审计时**以代码为准、以注释为线索**，这本身就是本讲要传授的习惯。

## 4. 核心概念与源码讲解

### 4.1 审计起点：lint 纪律与 SAFETY 全景清单

#### 4.1.1 概念说明

对一个陌生库做 unsafe 审计，最忌讳一头扎进某个 `unsafe` 块的细节。正确的前两步是：

1. **清点**：把所有 unsafe 相关的位置列成清单，保证没有遗漏。
2. **归类**：给每处标注它守护的不变量属于哪一类。类别决定了你接下来用什么手段去攻击它。

chili 的三类契约：

| 类别 | 问题 | 典型问题句 |
|---|---|---|
| **内存安全** | 指针指向的东西还存在吗？布局对得上吗？ | 「这个 `NonNull` 解引用时对象还活着吗？」「`transmute` 两边大小一样吗？」 |
| **线程安全** | 两个线程同时碰它会怎样？ | 「这个类型凭什么 `Send`？」「这个字段的写入会被谁看见？」 |
| **单次性** | 这个操作会不会被执行两次？ | 「`take_once` 真的只被调用一次吗？」「`send` 呢？」 |

#### 4.1.2 核心流程

审计的完整流程可以写成伪代码：

```text
audit(crate):
    sites = grep 所有 "SAFETY" 注释 + 所有 unsafe 块/fn/impl
    for site in sites:
        category = 内存安全 | 线程安全 | 单次性
        contract = 用自己的话复述注释的论证
        proof    = 在代码里找到兑现 contract 的那个调用点/控制流
        attack   = 构造一个违反 contract 的具体场景，说明它会变成什么 UB
    输出: sites 表 + attack 草稿
```

其中 `proof` 一步最容易被跳过，也最重要：**一条契约如果没有任何代码负责兑现，它就只是愿望**。chili 的特殊之处在于：job.rs 写下契约，lib.rs 负责兑现——两份文件必须对照着读。

#### 4.1.3 源码精读

先看纪律从何而来。chili 在 crate 根部声明了三条 deny：

```rust
#![deny(missing_docs)]
#![deny(unsafe_op_in_unsafe_fn)]
#![deny(clippy::undocumented_unsafe_blocks)]
```

> [src/lib.rs:L1-L3](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L1-L3)：第一条强制所有公共项写文档；第二条强制 **unsafe fn 体内的每个 unsafe 操作也要显式包 `unsafe` 块**（契约写在签名上、操作写在块上，两者分开标注）；第三条强制**每个 unsafe 块必须有 `SAFETY` 注释**。三条合起来保证了「审计对象 = 注释集合」是完备的——不存在没有书面论证的 unsafe 操作。

下面是全库 20 处标注点的完整清单（15 个 unsafe 块 + 4 个 unsafe fn 契约 + 1 个 unsafe impl），已按三类归好。建议先自己 `grep` 一遍再对照（4.1.4 就是这件事）。

**内存安全类（10 处）——对象存活与布局同构**

| # | 位置 | 代码动作 | 契约要点 |
|---|---|---|---|
| 1 | [src/job.rs:L153-L155](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L153-L155) | `harness` 的 unsafe fn 契约 | 只能在 `stack` 仍然存活时调用 |
| 2 | [src/job.rs:L160-L162](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L160-L162) | `stack.cast().as_ref()` | 存活性由 `JobShared::execute` 的契约接力 |
| 3 | [src/job.rs:L168-L173](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L168-L173) | `mem::transmute(Sender → Sender<T>)` | `repr(C)` + 唯一依赖 `T` 的字段是定宽 `Box`，布局与 `T` 无关 |
| 4 | [src/job.rs:L215-L218](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L215-L218) | `JobShared::execute` 的 unsafe fn 契约 | `JobStack` 仍存活 **且** 已从队列弹出 |
| 5 | [src/job.rs:L219-L224](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L219-L224) | 调用 `(self.harness)(...)` | 同上，契约由调用方保证 |
| 6 | [src/job.rs:L244-L248](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L244-L248) | `push_back` 的 unsafe fn 契约 | 入队的 `Job` 必须存活到被 pop 为止 |
| 7 | [src/job.rs:L256-L263](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L256-L263) | `pop_front` 里 `as_ref()` 并按 `Job` 解读 | 存活契约 + `Job<T>`→`Job` 的布局同构论证 |
| 8 | [src/lib.rs:L125-L131](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L125-L131) | worker 执行 `job.execute(&mut scope)` | 被共享的 `Job` 会在其 `JobStack` 离开作用域前被等到 |
| 9 | [src/lib.rs:L304-L310](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L304-L310) | 帮忙线程执行 `job.execute(self)` | 同上 |
| 10 | [src/lib.rs:L358-L361](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L358-L361) | `push_back(&job)` | `job` 活到本作用域结束（兑现 #6 的契约） |

**线程安全类（5 处）——并发协议与 Send**

| # | 位置 | 代码动作 | 契约要点 |
|---|---|---|---|
| 11 | [src/job.rs:L54-L57](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L54-L57) | `recv` 写 `waiting_thread` | 只有本线程会写、且此刻无人读 |
| 12 | [src/job.rs:L73-L80](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L73-L80) | `recv` 取 `val` | 抵达此处即独占 `val`（状态机保证） |
| 13 | [src/job.rs:L89-L94](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L89-L94) | `send` 写 `val` | 只有本线程会写、且此刻无人读 |
| 14 | [src/job.rs:L97-L102](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L97-L102) | `send` 取 `waiting_thread` 并 unpark | 接收方先写线程号再换状态，顺序保证可见 |
| 15 | [src/job.rs:L227-L234](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L227-L234) | `unsafe impl Send for JobShared` | `stack` 只被唯一执行线程访问；`sender` 的两种访问不竞争 |

**单次性类（5 处）——只能发生一次的操作**

| # | 位置 | 代码动作 | 契约要点 |
|---|---|---|---|
| 16 | [src/job.rs:L126-L133](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L126-L133) | `take_once` 的 unsafe fn 契约 + 体内 `ManuallyDrop::take` | 只能调用一次；此刻闭包尚未被取走 |
| 17 | [src/job.rs:L163-L167](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L163-L167) | `harness` 内调用 `take_once` | 任务已被 pop ⇒ 这是首次调用 |
| 18 | [src/lib.rs:L372-L378](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L372-L378) | `None` 分支本地 `(stack.take_once())(self)` | 任务没被真正送出 ⇒ harness 没跑过 ⇒ 首次 |
| 19 | [src/lib.rs:L384-L389](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L384-L389) | `pop_back` 分支本地 `(stack.take_once())(self)` | 任务从未出队 ⇒ harness 没跑过 ⇒ 首次 |

> 注意 #16 计为一处但含两段论证（签名契约 + 体内块）。另外 `Sender::send` 的单次性靠 `fn send(self, ..)` **消费 self** 的 move 语义在类型系统层面保证，不需要 unsafe——对照之下更能体会「能交给类型系统就不要交给注释」的设计取向。

后续三节分别深入三类。

#### 4.1.4 代码实践

1. **实践目标**：独立复现上面的清单，验证「20 处、三类 10/5/5」的分类确实来自源码而非本讲杜撰。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -n "SAFETY" src/job.rs src/lib.rs`（或用编辑器全局搜索）。
   - 对每一处，抄下行号和注释原文，先**自己**判断类别，再对照 4.1.3 的表。
   - 额外执行 `grep -cn "unsafe" src/job.rs src/lib.rs`，把命中数与清单对照，确认没有「有 unsafe 块却没有 SAFETY 注释」的漏网之鱼（`clippy::undocumented_unsafe_blocks` 应当保证这一点）。
3. **需要观察的现象**：`grep -n "SAFETY"` 在 `src/job.rs` 命中 15 行、在 `src/lib.rs` 命中 5 行，共 20 行，且行号与 4.1.3 表格一致。
4. **预期结果**：清单完全对得上；如果对不上，优先怀疑你的分类口径（unsafe fn 签名契约算不算一处），而不是源码变了。

#### 4.1.5 小练习与答案

**练习 1**：`#![deny(unsafe_op_in_unsafe_fn)]` 对审计者有什么好处？

**答案**：它强制 unsafe fn 体内的每个 unsafe 操作都显式包上 `unsafe { }` 块。于是「函数级契约」（签名上的 `/// SAFETY:`）和「操作级论证」（块旁的 `// SAFETY:`）被分开标注，审计者可以逐块核对论证，而不是把整个函数体当成一个不可分割的黑箱。

**练习 2**：如果删掉 `#![deny(clippy::undocumented_unsafe_blocks)]`，最坏会发生什么？

**答案**：某个新增的 unsafe 块可能不带任何书面论证就合入主分支。审计方法「先清点注释再逐条攻击」的前提是注释集合**完备**；丢掉这条 lint 就丢掉了完备性 guarantee，审计必须退回到肉眼扫全部 `unsafe` 关键字。

**练习 3**：清单里 20 处标注点对应的语法元素各是什么？

**答案**：15 个 unsafe 块（job.rs 10 个 + lib.rs 5 个）、4 个 unsafe fn 的签名契约（`take_once`、`harness`、`JobShared::execute`、`push_back`）、1 个 `unsafe impl Send for JobShared`。

### 4.2 线程安全类：`unsafe impl Send` 论证与通道的权限划分

#### 4.2.1 概念说明

`JobShared` 是任务跨线程旅行的形态：worker 从货架 `shared_jobs` 上取下它、在**另一个线程**上执行。要跨线程移动，类型必须满足 `Send`。问题在于它**天生不满足**：

- `stack: NonNull<JobStack>` —— 裸指针本身可以靠 `unsafe impl` 说清；
- `sender: Sender`（即 `Sender<()>` = `Arc<Channel<()>>`）—— 这才是症结。`Channel` 含两个 `UnsafeCell` 字段，`UnsafeCell` 是 `!Sync`，于是 `Channel: !Sync`，于是 `Arc<Channel>: !Send`。

所以 `JobShared` 需要一个手动的 `unsafe impl Send`。这类实现是 unsafe 审计里最需要警惕的：它声称「我有办法让这个天生不可共享的东西安全地跨线程」，而办法本身必须**逐字段**论证。

通道（#11–#14）的四条注释则属于同一类下的另一种技术：**静态权限划分**——用状态机的状态规定「此刻谁有权写哪个字段」，从而让两个 `UnsafeCell` 在没有任何锁的情况下安全协作。u3-l2 已详细推导过时序，这里只保留审计视角的结论。

#### 4.2.2 核心流程

`JobShared` 的一生中被谁碰过：

```text
发起线程                                     执行线程（worker 或帮忙者）
────────                                    ────────
heartbeat(): 货架上放入 JobShared
（此后发起线程只通过 Receiver 观察）
      │
      └──── pop_earliest_shared_job 移交 ────→ unsafe { job.execute(scope) }
                                                  │
wait_for_sent_job:                              harness 内:
  receiver.is_empty()  ←── 读 state 原子位 ──   写 val（UnsafeCell!）
  receiver.recv()      ←── park 等待 ───────    swap(state → Ready)、unpark
```

两条注释分别对应两个字段的访问纪律：

- **`stack` 字段**：只有最终 `execute` 它的那一个线程会解引用（`harness` 里的 `cast().as_ref()` 与 `take_once()` 都在 `JobShared::execute` 内部），且 `execute(self)` **消费** `self`——独占且一次性。
- **`sender` 字段**：只存在两种访问——等待方轮询 `Receiver::is_empty`（仅做 `state` 的原子 `load`，不碰 `UnsafeCell` 数据字段），与执行方调用 `Sender::send`（写 `val` 后原子 `swap` 状态）。数据字段的真实读写被状态机串行化。

通道权限划分可以形式化成一条不变量：对数据字段 \( f \in \{\text{val},\ \text{waiting\_thread}\} \) 的写权限由状态唯一决定——

\[ \text{state} = \text{Pending} \Rightarrow \text{双方均未触碰数据字段}, \quad \text{Waiting} \Rightarrow \text{仅 Receiver 可写 } waiting\_thread, \quad \text{Ready} \Rightarrow \text{仅 Receiver 可读 } val \]

（`val` 的写只发生在 `send` 内、且在状态切换到 `Ready` **之前**完成；`waiting_thread` 的读只发生在观测到旧值 `Waiting` 之后。）内存序的配对记账（swap 的 Release 对 CAS 的 Acquire 等）在 u3-l2 已推导，本讲不重复。

#### 4.2.3 源码精读

全库唯一的手动 trait 实现：

```rust
// SAFETY:
// The job's `stack` will only be accessed exclusively from the thread
// `JobShared::execute`ing the job which also consumes it.
//
// The job's `sender` will be accessed either from one thread to check if
// `Receiver::is_empty` or from the executing thread to `JobShared::execute`
// which calls `Sender::send` which can be only called once.
unsafe impl Send for JobShared {}
```

> [src/job.rs:L227-L234](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L227-L234)：这段论证分两段，分别覆盖 `stack` 与 `sender` 两个字段。注意它的正确性**依赖**单次性类契约（`send` 只调一次）——三类契约不是孤立的，审计时要画出依赖边。

兑现「`stack` 只被一个线程访问」的地方在 worker 主循环：

```rust
if let Some(job) = job {
    // SAFETY:
    // Any `Job` that was shared between threads is waited upon before
    // the `JobStack` exits scope.
    unsafe {
        job.execute(&mut scope);
    }
}
```

> [src/lib.rs:L124-L131](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L124-L131)：worker 从货架弹出 `JobShared` 后独占地 `execute`。注释里的「waited upon before the `JobStack` exits scope」实际上是**跨文件引用了 4.3 节的生命周期契约**——发起线程会等到结果才销毁栈帧，所以解引用非悬垂。

`Sender`/`Receiver` 侧的四条注释（#11–#14）举一例即可看到风格：

```rust
// SAFETY:
// To arrive here, either `state` is `State::Ready` or the above
// `compare_exchange` succeeded, the thread was parked and then
// unparked by the `Sender` *after* the `state` was set to
// `State::Ready`.
//
// In either case, this thread now has unique access to `val`.
unsafe { (*self.0.val.get()).take().map(|b| *b).unwrap() }
```

> [src/job.rs:L73-L80](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L73-L80)：`recv` 读取 `val` 前的论证——能走到这一行，说明状态已经翻到 `Ready`（无论发送方先完成还是唤醒了自己），此刻发送方不会再碰 `val`。这是「用状态换独占权」的标准写法。

#### 4.2.4 代码实践

1. **实践目标**：让编译器亲口告诉你 `Arc<Channel>` 为什么不是 `Send`，从而理解这条 `unsafe impl` 「买」来了什么。
2. **操作步骤**：新建一个临时 crate（`cargo new send_demo`），在 `src/main.rs` 写入如下**示例代码**（复刻 `Channel` 的最小骨架）：

   ```rust
   // 示例代码（非 chili 源码）
   use std::cell::UnsafeCell;
   use std::sync::atomic::AtomicU8;
   use std::sync::Arc;

   fn is_send<T: Send>() {}

   fn main() {
       struct Channel {
           state: AtomicU8,
           waiting_thread: UnsafeCell<Option<()>>,
           val: UnsafeCell<Option<()>>,
       }
       is_send::<Arc<Channel>>();
   }
   ```

   然后 `cargo check`。观察错误后，把 `is_send::<Arc<Channel>>()` 换成 `is_send::<AtomicU8>()` 再 `cargo check`，对比结果。
3. **需要观察的现象**：第一次 `cargo check` 在 `is_send::<Arc<Channel>>()` 处报 **E0277**（`Arc<Channel>` cannot be sent between threads safely），错误链会一路指到 `UnsafeCell` 不是 `Sync`；第二次通过。
4. **预期结果**：确认「`UnsafeCell` → `!Sync` → `Arc<..>: !Send`」这条推导链，即 `JobShared` 不加 `unsafe impl Send` 就无法放进跨线程的 `Context`。本实践是编译期演示，结果确定；不需要运行程序。

#### 4.2.5 小练习与答案

**练习 1**：删掉 `unsafe impl Send for JobShared {}` 后，编译器会在哪里拒绝？

**答案**：`JobShared` 要存进 `LockContext.shared_jobs`，而 `LockContext` 位于 `Arc<Context>` 之后、被 worker 线程与发起线程共用，`thread::spawn(move || execute_worker(context, ..))`（[src/lib.rs:L527-L535](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L527-L535)）要求闭包环境 `Send`，推导链最终落到 `JobShared: Send` 失败，报 E0277。

**练习 2**：等待方线程调用 `Receiver::is_empty` 与执行方调用 `Sender::send` 可能同时发生，为什么注释认为这不是数据竞争？

**答案**：`is_empty` 只做 `state` 的原子 `load`（[src/job.rs:L49-L51](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L49-L51)），完全不触碰 `UnsafeCell` 数据字段；`send` 对 `val` 的写入发生在状态切换到 `Ready` 之前，而 `recv` 只在状态为 `Ready` 之后才读 `val`。两个 `UnsafeCell` 字段的每次读写都被状态机串行化，剩下的并发访问全部落在原子变量上。

**练习 3**：假设有人把 `JobShared` 克隆成两份交给两个 worker 同时 `execute`，写出 UB 的链条。

**答案**：两个线程同时进入 `harness` → 都执行 `stack.take_once()` → 第二次 `ManuallyDrop::take` 从已被移出的位置读值 → 把未初始化内存当作闭包 `F` 调用 → UB（典型表现为调用垃圾函数指针）。这正是「`stack` 只被唯一执行线程访问」这条论证要挡住的情形；当前代码用 `execute(self)` 的 move 语义在类型层面阻止了第二次 execute。

### 4.3 内存安全类：生命周期契约与布局同构

#### 4.3.1 概念说明

chili 最激进的省开销设计是：**闭包不住在堆上，而住在 `join_heartbeat` 的栈帧里**。u3-l3 讲过类型擦除怎么做，本讲追问代价：队列里存的是 `NonNull<Job>`，一个**不保证目标存活的裸指针**。于是整条链上每一次解引用，都挂在同一条生命周期契约上：

> **从 `push_back` 入队，到任务被 `pop` 并执行完毕（或被 `pop_back` 撤回）之间，栈上的 `JobStack` 必须一直存活。**

这条契约由三方共同签署：

- **提供方** `push_back`（unsafe fn 契约，#6）与 `JobShared::execute` / `harness`（#4/#1）：声明「调用者必须保证存活」。
- **兑现方** `join_heartbeat`（#10）与两个执行点（#8/#9）：用控制流证明「在栈帧销毁前，指针一定已经失效或任务已完成」。

布局同构（#3、#7）是内存安全的另一半：`transmute(Sender → Sender<T>)` 和把 `NonNull<Job<T>>` 当 `NonNull<Job>` 读，都要求「擦除前后的类型只是 `T` 的标签不同、比特布局完全一致」。

#### 4.3.2 核心流程

`join_heartbeat` 在 `push_back` 之后的**三条出路**，就是生命周期契约的全部兑现方式：

```text
push_back(&job)                      ← job/stack 在本栈帧上
        │
        ├─ 出路 A：任务被送出（take_receiver() == Some）
        │    └─ wait_for_sent_job:
        │         ├─ 货架仍在 → 返回 None ──→ 出路 B
        │         └─ 已被偷走 → 帮忙干活直到 receiver.recv() 返回
        │              └─ recv 只会在 harness 内 send 完成后返回
        │                 ⇒ 栈帧销毁时解引用必然已结束 ✓
        │
        ├─ 出路 B：None 分支（任务上过货架但未被偷走）
        │    └─ 货架条目已在 wait_for_sent_job 里 remove
        │       ⇒ 没有任何指针还指向本帧 ──→ 本地 take_once ✓
        │
        └─ 出路 C：任务从未出队（take_receiver() == None）
             └─ pop_back 把指针从队列摘除
                ⇒ 没有任何指针还指向本帧 ──→ 本地 take_once ✓
```

三条出路殊途同归：**栈帧销毁的那一刻，世界上不再存在指向它的 `NonNull`，或所有解引用都发生在销毁之前**。注意 LIFO 纪律对出路 C 的贡献：`join` 递归嵌套时，后入队的任务先被各自的 `pop_back` 摘走，轮到外层 `pop_back` 时队尾恰好还是自己的 `job`。

#### 4.3.3 源码精读

契约的签署方，三个 unsafe fn 的签名：

```rust
/// SAFETY:
/// Any `Job` pushed onto the queue should alive at least until it gets
/// popped.
pub unsafe fn push_back<T>(&mut self, job: &Job<T>) {
```

> [src/job.rs:L244-L248](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L244-L248)：`push_back` 只存指针不存数据，「活到被 pop」是调用者的责任。

```rust
/// SAFETY:
/// It should only be called while the `JobStack` it was created with is
/// still alive and after being popped from a `JobQueue`.
pub unsafe fn execute(self, scope: &mut Scope<'_>) {
```

> [src/job.rs:L215-L218](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L215-L218)：`JobShared::execute` 的双重前条件——存活 + 已出队（后者同时服务于单次性契约）。

契约的兑现方，`join_heartbeat` 的入队与收尾：

```rust
let stack = JobStack::new(a);
let job = Job::new(&stack);

// SAFETY:
// `job` is alive until the end of this scope.
unsafe { self.job_queue.push_back(&job) };
```

> [src/lib.rs:L355-L361](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L355-L361)：`stack` 与 `job` 都是本函数的局部变量，`&job` 借用在函数返回时失效——所以「alive until the end of this scope」这句话是**靠下面三条出路的控制流兑现**的，不是靠借用检查器（裸指针已经逃出了借用系统）。

```rust
if let Some(receiver) = job.take_receiver() {
    let ra = match self.wait_for_sent_job(receiver) {
        Some(Ok(val)) => val,
        Some(Err(e)) => panic::resume_unwind(e),
        None => /* 出路 B：本地执行 */,
    };
    ...
} else {
    self.job_queue.pop_back();
    /* 出路 C：本地执行 */
}
```

> [src/lib.rs:L368-L389](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L368-L389)：出路 A 藏在 `wait_for_sent_job` 的返回值里——`Some(..)` 意味着 `receiver.recv()` 已经返回，而 `recv` 返回的前提是 `send` 已执行完毕，`send` 又在 `harness` 对 `stack` 的最后一次解引用之后。因果链闭合。

执行侧的接力注释（worker 与帮忙者措辞相同）：

```rust
// SAFETY:
// Any `Job` that was shared between threads is waited upon
// before the `JobStack` exits scope.
unsafe {
    job.execute(self);
}
```

> [src/lib.rs:L303-L310](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L303-L310)：执行线程相信发起线程会等（出路 A），所以此刻 `stack` 仍活着。**这条论证的正确性不在本文件、也不在本线程，而在千里之外的 `join_heartbeat`**——跨文件、跨线程的契约接力是 unsafe 审计里最容易被忽略的攻击面。

布局同构的两处。先是 `harness` 里的 transmute：

```rust
// SAFETY:
// `Sender` can be safely transmuted to `Sender<T>` since the
// `Channel`'s size is the same as `Channel<T>` because the only
// field referencing `T` has constant size (`Box`), and the order
// of its fields is preserved given that it is `repr(C)`.
let sender: Sender<T> = unsafe { mem::transmute(sender) };
```

> [src/job.rs:L168-L173](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L168-L173)：`Channel` 的唯一依赖 `T` 的字段是 `Option<Box<thread::Result<T>>>`——`Box` 是定宽指针（`Option` 的 niche 优化不改变宽度），加上 `#[repr(C)]`（[src/job.rs:L23-L33](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L23-L33)）钉死字段顺序，`Channel<T>` 对任何 `T` 布局相同。

再看 `pop_front` 的 cast 论证，以及一处值得留意的**历史遗留**：

```rust
// SAFETY:
// `Job` is still alive as per contract in `push_back`.
//
// The previously pushed `Job<T>` is safe to cast to `Job` since the
// only field that depends on `T` is of type
// `Cell<Option<NonNull<Future<T>>>>` which has constant size, while
// being `repr(C)` guarantees identical field order.
let job = unsafe { self.0.pop_front()?.as_ref() };
```

> [src/job.rs:L256-L263](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L256-L263)：注释把 `Job` 的 `T` 依赖字段写作 `Cell<Option<NonNull<Future<T>>>>`，但当前代码里这个字段是 `receiver: Cell<Option<Receiver<T>>>`（[src/job.rs:L140-L145](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L140-L145)），且库里根本没有 `Future` 类型。用 `git show 617cbd4^:src/job.rs` 可以看到重构前的版本确实有 `Future` 结构和 `fut: Cell<Option<NonNull<Future<T>>>>` 字段——注释是提交 `617cbd4`「Refactored and simplified jobs」之后忘了同步的化石。**论证思路（定宽指针 + repr(C)）在当前代码下依然成立**（`Receiver<T>` 是 `Arc<Channel<T>>`，同样定宽），但审计教训很重要：注释可能过时，结论必须自己在当前代码上重新推导。

最后是一个**审计示范：一条值得报告的疑点**（推导结论，待本地验证）。对照三条出路检查 panic 路径：闭包 `b` 在 [src/lib.rs:L366](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L366) 被直接调用 `b(self)`，**没有** `catch_unwind` 包裹。若 `b` 在 `push_back` 之后、三条出路之前 panic：unwind 会立刻销毁 `stack`/`job` 所在栈帧，而此刻可能仍有 `NonNull` 指向它——本地队列里的（worker 的 `JobQueue` 是长命的，[src/lib.rs:L115](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L115)），或更糟，货架 `shared_jobs` 上已送出的那份 `JobShared`（`Scope` 与 `JobStack` 都没有实现 `Drop` 来做清理）。此后 worker `pop_earliest_shared_job` 弹出它并 `execute`，`harness` 里 `stack.cast().as_ref()` 解引用悬垂栈指针 → 栈上 use-after-return → UB。作者显然考虑过 panic——`harness` 里用 `catch_unwind` 包住了闭包 `a`（[src/job.rs:L175](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L175)），但那条防线只保护「在自己线程外执行的分支」；`b` 走的是裸调用。现有测试 `join_panic` 也只在 `a` 分支里 panic。这条疑点说明：**SAFETY 论证默认了非 panic 的直线控制流，而 unwind 是生命周期契约的天敌**——它正好也是下一讲（panic 传播与 miri）的入口。

#### 4.3.4 代码实践

1. **实践目标**：完成两件事——(a) 亲手画出 `NonNull` 的完整旅程，把 4.3.2 的文字图落实到每个解引用点；(b) 用 git 考古独立验证「遗留注释」的判断。
2. **操作步骤**：
   - (a) 在纸上画五个格子：`join_heartbeat 栈帧`、`JobQueue(VecDeque)`、`货架 shared_jobs`、`worker/帮忙者线程`、`Channel`。沿调用顺序把 `NonNull<JobStack>` 的移动画成箭头，在每个箭头旁标注对应的 SAFETY 编号（#1、#2、#4–#10）；用三种颜色分别标出「指针诞生」「解引用」「指针失效/被摘除」的时刻。
   - (b) 依次执行：
     ```bash
     git log --oneline -- src/job.rs
     git show 617cbd4^:src/job.rs | grep -n "Future\|Job::execute\|fn unwrap"
     ```
3. **需要观察的现象**：(a) 图上每个「解引用」时刻都应能回溯到一条尚未失效的箭头，且失效全部发生在栈帧销毁之前（出路 A/B/C）；(b) 旧版文件里出现 `pub struct Future`、`fut: Cell<Option<NonNull<Future<T>>>>` 与 `Job::execute`，而当前文件中这些符号都不存在。
4. **预期结果**：旅程图与 4.3.2/4.3.3 的分析一致；git 输出证实 `pop_front` 注释里的 `Future<T>` 与 `JobStack` 文档里的 `Job::execute`/`Self::unwrap`（[src/job.rs:L113-L117](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L113-L117)）都是 `617cbd4` 重构前的化石。顺带一提，[src/job.rs:L130](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L130) 的 "No `Job` has has been executed" 里连续两个 `has` 也是从旧版本原样抄来的笔误——审计时这种小痕迹往往是「注释没有随代码更新」的信号。

#### 4.3.5 小练习与答案

**练习 1**：出路 C（`pop_back` 分支）依赖队列的什么性质才能保证摘掉的就是自己的 `job`？

**答案**：LIFO 纪律。嵌套 `join` 中每个内层 `join_heartbeat` 在返回前都会用 `pop_back` 摘掉自己的任务（或它已被 `pop_front` 送走），所以轮到外层执行 `pop_back` 时，队尾恰好是外层自己的 `job`。期间队列只发生过 `push_back`（队尾）与 `pop_front`（队头）两类操作，不会把外层的任务从中间抽走。

**练习 2**：`transmute(Sender → Sender<T>)` 依赖哪两个布局事实？如果 `Channel` 的 `val` 字段直接写成 `UnsafeCell<Option<T>>` 会怎样？

**答案**：事实一：`Channel` 标了 `#[repr(C)]`，字段顺序固定、无重排自由度；事实二：唯一依赖 `T` 的字段 `Option<Box<thread::Result<T>>>` 中 `Box` 是定宽指针，所以 `Channel<T>` 的尺寸与对齐与 `T` 无关。若改成 `UnsafeCell<Option<T>>`，`T` 的大小会直接进入布局，`Channel<u8>` 与 `Channel<[u8; 16]>` 尺寸不同，transmute 后读 `receiver`/`sender` 字段会按错误的偏移解读内存，产生垃圾指针 → UB。

**练习 3**：4.3.3 末尾的疑点里，为什么说 `catch_unwind`（[src/job.rs:L175](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L175)）保护的是 `a` 分支而不是 `b` 分支？

**答案**：`harness` 只在任务被送到**其他线程**执行时运行，而被送出的闭包恰是 `a`（`b` 永远留在发起线程顺序执行）。`catch_unwind` 位于 `harness` 内、包裹 `f(scope)`（即 `a`），所以 `a` 的 panic 被拦下并装进 `thread::Result` 经通道送回。`b` 在 [src/lib.rs:L366](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L366) 是裸调用 `b(self)`，panic 会直接展开穿过 `join_heartbeat` 的栈帧——这正是疑点的来源。

### 4.4 单次性类：`take_once` 契约与 `ManuallyDrop` 的双向守护

#### 4.4.1 概念说明

闭包 `a` 被存进 `JobStack` 后，有**恰好两条**消费路径：要么被 `harness` 取走送到别的线程执行，要么被发起线程就地执行。无论哪条，`F` 只能被**移出一次**。这就是 `take_once` 的单次性契约。

`ManuallyDrop<F>` 在这里同时封住两个方向的失败：

- **忘记取**（零次调用）：`F` 永远不被 drop → 内存泄漏。**泄漏不是 UB**——这正是选择的妙处：把「清理遗漏」的后果从 UB 降级为泄漏，错误从「不可饶恕」变成「不可原谅但安全」。
- **取两次**：第二次 `ManuallyDrop::take` 从已被移出的位置读值 → 把未初始化内存当作合法的 `F` → **UB**。

`JobStack` 的文档直白地写明了零次调用的后果：

```rust
pub struct JobStack<F = ()> {
    /// All code paths should call either `Job::execute` or `Self::unwrap` to
    /// avoid a potential memory leak.
    f: UnsafeCell<ManuallyDrop<F>>,
}
```

> [src/job.rs:L113-L117](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L113-L117)：注意这又是化石注释——`Job::execute` 与 `Self::unwrap` 在当前代码里都不存在（`git show 617cbd4^:src/job.rs` 可见二者是重构前的 API）。当前真正消费闭包的路径是：`harness` 内的 `take_once`（经 `JobShared::execute` 触发）与 `join_heartbeat` 的两个本地分支。契约本身（每条路径都要消费一次）没有变，变的是路径的名字。

#### 4.4.2 核心流程

三个 `take_once` 调用点两两互斥的论证，是本模块的核心。判定装置只有一个：`job.take_receiver()`——`receiver` 这个 `Cell` 槽位**只在** `pop_front` 里被塞入值（[src/job.rs:L267](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L267)），所以「有 receiver」⇔「任务曾从队头被弹出」：

```text
take_receiver() 的返回值        谁已经调用过 take_once？           本线程的动作
────────────────────          ─────────────────────────       ─────────────────
None（从未出队）               没有人（harness 只在弹出后跑）    pop_back 后本地 take_once
Some + 货架仍在（未被偷）       没有人（JobShared 没被任何线程取） remove 后本地 take_once
Some + 已被偷走                harness 里的那次（且仅一次）      等待 recv 拿结果，不再触碰 stack
```

三行覆盖全部情况，且每行的第二列都排除了另外两个调用点——这就是「单次」的证明。它同时解释了 `send` 为什么不需要 unsafe：`fn send(self, ..)` 消费 `self`（[src/job.rs:L88](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L88)），move 语义让「第二次 send」根本无法编译。

#### 4.4.3 源码精读

契约本体：

```rust
/// SAFETY:
/// It should only be called once.
pub unsafe fn take_once(&self) -> F {
    // SAFETY:
    // No `Job` has has been executed, therefore `self.f` has not yet been
    // `take`n.
    unsafe { ManuallyDrop::take(&mut *self.f.get()) }
}
```

> [src/job.rs:L126-L133](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L126-L133)：签名契约管「次数」，体内注释管「此刻尚未被取」。注意接收者是 `&self`（非 `&mut self`），意味着借用检查器帮不上忙——别名完全可能存在（`JobStack` 被 `NonNull` 共享），全靠调用方自律。顺带一提 "has has" 的重复是从旧版本原样继承的笔误。

`harness` 内的调用点及其论证：

```rust
// SAFETY:
// This is the first call to `take_once` since `Job::execute`
// (the only place where this harness is called) is called only
// after the job has been popped.
let f = unsafe { stack.take_once() };
```

> [src/job.rs:L163-L167](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L163-L167)：论证依赖一个事实链：`harness` 只被 `JobShared::execute` 调用（注释里的 `Job::execute` 又是化石名）→ `execute` 的契约要求任务**已从队列弹出**（#4）→ 弹出意味着发起线程要么走「等待」、要么走「remove」路线，都不会再本地 `take_once` → 首次调用。单次性契约在这里与生命周期契约**共享同一个前条件**（已弹出），两条契约拧成一股。

发起线程侧的两个本地调用点：

```rust
// SAFETY:
// Since the `job` didn't have the chance to be actually
// sent across threads, it cannot take the closure out of the
// `JobStack` anymore. `JobStack::take_once` is thus called
// only once.
None => unsafe { (stack.take_once())(self) },
```

> [src/lib.rs:L372-L378](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L372-L378)：`None` 由 `wait_for_sent_job` 在「货架条目仍在」时返回——`JobShared` 从未被任何 worker 取走，`harness` 从未运行，所以本地 `take_once` 是首次。

```rust
// SAFETY:
// Since the `job` was popped from the back of the queue, it cannot
// take the closure out of the `JobStack` anymore.
// `JobStack::take_once` is thus called only once.
(unsafe { (stack.take_once())(self) }, rb)
```

> [src/lib.rs:L381-L389](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L381-L389)：`take_receiver()` 返回 `None` 说明 `receiver` 槽位从未被 `pop_front` 填充，即本任务从未出队，`harness` 无从运行。`pop_back` 同时履行了 4.3 的生命周期义务（摘除悬垂指针）。

#### 4.4.4 代码实践

1. **实践目标**：独立核验「三个调用点两两互斥」的论证，并写出双重 `take_once` 的 UB 场景描述。
2. **操作步骤**：
   - 执行 `grep -n "take_once" src/job.rs src/lib.rs`，确认命中 4 行：1 处定义（[src/job.rs:L128](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L128)）加 3 处调用（[src/job.rs:L167](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L167)、[src/lib.rs:L377](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L377)、[src/lib.rs:L388](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L388)）。
   - 对每一对调用点组合（harness↔None 分支、harness↔pop_back 分支、None↔pop_back 分支），在笔记上写出「为什么这一对不可能都发生」，依据只允许引用代码事实（`receiver` 槽位何时被填充、货架条目何时被 remove）。
   - 最后写 UB 场景草稿：假设互斥被破坏（例如 `None` 分支的判断写反了），描述第二次 `take_once` 时机器层面发生了什么。
3. **需要观察的现象**：grep 命中行与你标注的调用点一致；三对互斥论证中，每对都能落到一个明确的代码事实（`Cell` 槽位的填充时机或 `BTreeMap::remove` 的返回值语义）。
4. **预期结果**：互斥论证闭环。UB 场景参考描述：`ManuallyDrop::take` 会把 `F` 按位移出并留下未初始化的内存；第二次调用把这段未初始化字节当作合法的 `F`（通常含捕获变量与函数指针/上下文），随后 `(f)(self)` 对其调用 → 未定义行为，典型表现为跳转到垃圾地址或读到野指针捕获。

#### 4.4.5 小练习与答案

**练习 1**：`take_once` 从未被调用会发生什么？这和调用两次的后果有何本质区别？

**答案**：从未调用 ⇒ `F` 连同其捕获的变量永不析构 ⇒ 内存泄漏。泄漏在 Rust 中合法（`mem::forget` 是 safe 的），不是 UB。调用两次 ⇒ 第二次从已移出的未初始化位置读值并当作 `F` 使用 ⇒ UB。`ManuallyDrop` 的设计意义就在于把「零次」这个更容易犯的错的代价从 UB 降级为泄漏。

**练习 2**：`Sender::send` 同样是「只能发生一次」的操作，为什么它不需要 unsafe fn 契约？

**答案**：`send` 的签名是 `pub fn send(self, val: ..)`（[src/job.rs:L88](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L88)）——消费 `self`，第二次调用无法通过编译。对比 `take_once(&self)`：因为 `JobStack` 经 `NonNull` 跨线程共享，拿不到独占的 `&mut self`，只能放宽到 `&self` 并把单次性交给注释。**能用类型系统解决的绝不用注释解决**，这两个签名放在一起就是这条原则的最佳教具。

**练习 3**：为什么说 `harness` 里的 `catch_unwind`（[src/job.rs:L175](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L175)）也在守护单次性契约？

**答案**：它保证闭包 `a` 的 panic 被拦截在 `harness` 内部、转换成 `thread::Result` 经通道送回。若 panic 直接穿透 `harness` 展开，`sender.send` 将永不执行，发起线程的 `recv` 会永远阻塞（或按 4.3 的疑点演变成悬垂指针问题）。把 panic 困在 harness 里，`take_once → f(scope) → send` 这条消费链才能完整走完恰好一次。

## 5. 综合实践

把本讲的方法论完整走一遍，产出一份**chili unsafe 审计报告**（这是本讲规格要求的实践任务）。

**任务 A：清单与归类。** 执行 `grep -n "SAFETY" src/job.rs src/lib.rs`，独立制作 20 行的清单表（位置 / 代码动作 / 契约复述 / 类别），然后与 4.1.3 的表格互相校对。要求契约复述**用你自己的话**——能复述才算是读懂，照抄不算。

**任务 B：三类各一个 UB 场景草稿。** 对每个类别，写一段「违反契约的剧情」，必须包含：违反了哪条注释（引用行号）、控制流如何走到这一步、机器层面读/写了什么非法内存、预期 manifested 的现象。参考答案的骨架：

- **线程安全**：`JobShared` 被两个线程同时 `execute` → 两次 `take_once` → 从已移出位置读闭包 → 调用垃圾函数指针（4.2.5 练习 3）。
- **内存安全**：`join_heartbeat` 在 `push_back` 之后提前离开栈帧（例如 `b` panic 展开，见 4.3.3 疑点）→ 货架/队列残留指向已销毁栈帧的 `NonNull` → worker 弹出并 `execute` → 栈上 use-after-return → UB。**待本地验证**：可以写一个 `b` 分支 `panic!` 的压测程序（`thread_count: 2`、`heartbeat_interval: 1µs`、`join_with_heartbeat_every::<1, ..>` 提高送出概率），观察是否出现崩溃；用 `cargo +nightly miri test` 跑小规模版本看能否报 use-after-return（miri 下线程调度不同，需多次尝试，结果**待本地验证**）。
- **单次性**：互斥判定被破坏（假设 `wait_for_sent_job` 的 `None` 判断写反）→ 本地 `take_once` 与 `harness` 内的 `take_once` 都执行 → 第二次从未初始化内存读出 `F` 并调用 → UB（4.4.4 的参考描述）。

**任务 C：契约依赖图。** 在草稿上把 20 处标注点连成有向图：A → B 表示「A 的论证依赖 B 成立」。至少应能连出这些边：`unsafe impl Send`（#15）→ `send` 单次（move 语义）；`harness` 的 `take_once`（#17）→ `execute` 的「已弹出」前条件（#4）；worker 执行（#8/#9）→ `join_heartbeat` 的存活契约（#10）。完成后你会得到一个直观印象：**chili 的 unsafe 论证不是 20 条独立的注解，而是一张拧在一起的网**——这也解释了为什么改任何一个环节都要重新审一遍全图。

**验收标准**：清单与源码一致；三个 UB 场景都能指到具体行号；依赖图至少包含上述三条边。

## 6. 本讲小结

- chili 全库共 **20 处 SAFETY 标注点**（15 个 unsafe 块 + 4 个 unsafe fn 契约 + 1 个 `unsafe impl Send`），按内存安全（10）/ 线程安全（5）/ 单次性（5）三类归类后即可逐类攻击。
- `unsafe impl Send for JobShared` 的论证按字段拆开：`stack` 靠「唯一执行线程 + `execute(self)` 消费」，`sender` 靠「`is_empty` 只做原子读 + 状态机串行化数据字段」；根源是 `UnsafeCell` 令 `Arc<Channel>` 天生 `!Send`。
- 生命周期契约「栈上 `JobStack` 活到被 pop 并执行完毕」由 `join_heartbeat` 的三条出路兑现：送出则 `recv` 等到 `send` 完成、未被偷则 remove 后本地执行、从未出队则 `pop_back` 后本地执行。
- 布局同构靠 `repr(C)` + 「唯一依赖 `T` 的字段是定宽指针（`Box`/`Arc`）」支撑 `transmute` 与 `NonNull` 的跨类型解读。
- `ManuallyDrop` 双向守护 `take_once`：零次调用只是泄漏（安全），两次调用是 UB（从未初始化内存移出）；三个调用点靠 `receiver` 槽位与货架 `remove` 的语义两两互斥。
- 审计还收获了三处**化石注释**（`Future<T>`、`Job::execute`、`Self::unwrap`，均为 `617cbd4` 重构遗留）与一条**待验证疑点**（`b` 分支 panic 可能留下悬垂 `NonNull`）——注释会过时，论证必须在当前代码上重新推导。

## 7. 下一步学习建议

本讲发现了「`b` 分支 panic 穿越 `join_heartbeat`」这条疑点，而 panic 恰好是下一讲的主角：**u4-l2《panic 传播、测试体系与 miri》** 将精读 `catch_unwind`/`resume_unwind` 的跨线程传递路径、`join_panic` 测试如何在单线程环境人工通过，以及 CI 中 `cargo +nightly miri test` 与 `-Zmiri-many-seeds` 如何给这些 unsafe 论证做机器体检——你在此写下的三个 UB 场景草稿，正是 miri 要替你验证的假说。若你想先缓一缓并发，也可以跳读 u4-l3《基准测试与性能分析》，从 `benches/overhead.rs` 回头看这些 unsafe 换来了多低的每节点开销（约 3.5ns），再决定什么时候值得为安全牺牲这点性能。
