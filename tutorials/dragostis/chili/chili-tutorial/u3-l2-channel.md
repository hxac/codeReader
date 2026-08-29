# job.rs 的 Channel：手写 park/unpark 单值通道

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Channel` 为什么只用一个 `AtomicU8` 状态字 + 两个 `UnsafeCell` 字段就能表达一条完整的单值通道，以及 `Pending / Waiting / Ready` 三态各自锁定了谁的读写权限。
2. 逐行解释 `Receiver::recv`：为什么必须**先写 `waiting_thread`、再把状态 CAS 成 `Waiting`、最后才 `park`**，这个顺序如何杜绝「丢失唤醒」。
3. 逐行解释 `Sender::send`：为什么 `swap` 到 `Ready` 只有在观察到旧值是 `Waiting` 时才 `unpark`，以及 `Acquire / AcqRel` 内存序在这条通道里的精确配对关系。
4. 对照标准库文档，独立评估这段 unsafe 代码对 `thread::park` 语义的信任边界（虚假唤醒问题）。
5. 用纯 safe Rust（`Mutex` + `Condvar`）实现一个语义等价的最小单值通道，并用三个顺序不同的测试验证它。

## 2. 前置知识

### 2.1 单值（oneshot）通道是什么

「通道」就是两个线程之间传递数据的一根管子：一端 `send`，另一端 `recv`。标准库的 `std::sync::mpsc` 是**多值**通道，内部要维护队列、计数、关闭语义，`recv` 还要返回 `Result` 以处理「发送端已丢弃」的情形。

chili 需要的通道用途极其单一（见 u2-l4 的跨线程链路）：任务 `a` 被送到 worker 线程执行后，**恰好要把一个结果传回发起线程，一次，永远只有一次**。而且发送端 `Sender::send` 和接收端 `Receiver::recv` 都**消费 `self`**——「只能调用一次」由类型系统直接保证。这样一来 mpsc 的所有通用机制都是多余开销，于是 `job.rs` 手写了一个 24 行的核心实现。

### 2.2 UnsafeCell 与 Arc

- `UnsafeCell<T>` 是 Rust 中「关闭借用检查器」的原始开关：它告诉编译器「这个字段会被以别名方式读写，你别做独占假设」。用它换来的自由必须由**开发者自己书写的协议**来偿还。
- `Arc<T>` 把 `Channel` 放进引用计数堆分配里，让 `Sender` 和 `Receiver` 各持一个克隆句柄共享同一个 `Channel`。当两边都被消费销毁后，引用计数归零，`Channel` 自动释放。

### 2.3 原子操作与内存序速览

本讲只用到两个原子原语，都在 `state: AtomicU8` 上操作：

| 原语 | 语义 |
|------|------|
| `compare_exchange(期望值, 新值, 成功序, 失败序)` | 状态**等于**期望值才写成新值（CAS）；不等则失败，但失败路径也按「失败序」做一次读 |
| `swap(新值, 序)` | 无条件写成新值，并**返回旧值**（读-改-写，RMW） |

内存序（本讲用到的三种）：

| 序 | 效果 |
|----|------|
| `Release`（写半边） | 本次写之前的所有普通读写，对「读到这次写入的人」可见 |
| `Acquire`（读半边） | 读到 `Release` 写入的值后，之后的普通读写能看到对方发布之前的全部内容 |
| `AcqRel` | 用在 RMW 上，同时具备两者：写入按 `Release` 发布、读取按 `Acquire` 获取 |

一个重要规则：**RMW 操作总是读到修改序列（modification order）中最新的值**。所以 `swap` 与 `compare_exchange` 不会读到「过期」的 `state`，这是下文交错分析成立的前提。

### 2.4 thread::park / unpark 与「令牌」语义

`thread::park()` 把当前线程挂起；`Thread::unpark()` 唤醒指定线程。它的核心机制是每个线程一个**令牌（token）**，标准库文档（[std::thread::park](https://doc.rust-lang.org/std/thread/fn.park.html)）原文要点：

> 「`unpark` 原子地使令牌可用（如果它还不可用）。因为线程即使没有 parked 也能持有令牌，**`unpark` 之后再 `park` 会使后者立即返回**。」
>
> 「`unpark` 的调用与 `park` 的调用 *synchronize-with*：在 `unpark` 之前执行的内存操作，对消费令牌并从 `park` 返回的线程可见。用原子序的术语说，`unpark` 执行一次 `Release` 操作，`park` 执行对应的 `Acquire` 操作。」
>
> 「`park` 也可能**虚假返回（spuriously）**而没有消费令牌。」

这三句话分别给出：① 先 unpark 后 park 不丢唤醒（令牌可暂存）；② park 返回自带内存同步；③ park **不保证**返回就等于被人 unpark 过——记住第三点，4.2.5 会回来审它。

### 2.5 thread::Result 与 panic 载荷

`thread::Result<T>` 是 `Result<T, Box<dyn Any + Send>>` 的别名。跨线程无法直接传播 panic，所以 worker 线程用 `panic::catch_unwind` 把闭包的返回值或 panic 载荷装进 `Ok`/`Err`，**由这条通道运回**发起线程，再 `resume_unwind` 重新抛出（u4-l2 专门讲这条 panic 之路）。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `src/job.rs` | 本讲主战场。L17–L111 是完整通道实现：`State`、`Channel`、`Receiver`、`Sender`、`channel()` 构造函数；L255–L274 的 `JobQueue::pop_front` 是通道的**创建点**；L175 的 `harness` 是 `send` 的**唯一调用点** |
| `src/lib.rs` | 通道的使用方。`wait_for_sent_job`（L284–L316）里 `is_empty` 判空循环 + `recv` 收结果；`heartbeat`（L318–L333）里 `pop_front` 触发通道延迟创建 |

整条通道的数据流回顾（承接近况 u2-l4）：

```
发起线程 join_heartbeat
  └─ job_queue.pop_front()          ← 此刻才 channel() 创建通道（job.rs:265）
       ├─ Receiver 装回栈上的 Job    （job.rs:267）
       └─ Sender 随 JobShared 发往 worker 线程
worker 线程执行闭包 → catch_unwind → Sender::send   （job.rs:175）
发起线程 wait_for_sent_job：while receiver.is_empty() { 帮忙执行货架任务 }
  最后 Receiver::recv 拿到 Result     （lib.rs:297 / lib.rs:315）
```

## 4. 核心概念与源码讲解

### 4.1 Channel 三态状态机

#### 4.1.1 概念说明

并发通道的根本难题是：**「值放在普通的内存里」和「两边的线程何时可以碰这块内存」必须严格配套**。加锁是一种配套方式（锁的获取/释放自动划定了访问窗口），代价是每条消息都要走一次操作系统/原子协议。

chili 的选择是：把访问窗口编码进一个原子状态字，让两个 `UnsafeCell` 字段的读写权限**由状态机静态划定**——

- `state == Pending`（初始态）：谁都没动过，`val` 和 `waiting_thread` 无人可碰；
- `state == Waiting`：接收者已登记好自己的线程句柄、正要停车；此时 `waiting_thread` 已冻结为「只读」，`val` 只允许 Sender 写；
- `state == Ready`：Sender 已写完值；此时 `val` 冻结为「接收者独占读取」。

换句话说，**状态字本身就是一把「两阶段的细粒度锁」**：任何一方在触碰 `UnsafeCell` 之前，都必须先用一次原子操作把状态推进到「允许我碰」的那一格，而这次原子操作同时完成了内存序上的发布/获取。

#### 4.1.2 核心流程

三态迁移只有两条路径，且由「谁先抢到 `Pending`」决定，**不存在第三种交错**（因为只有一次 CAS 和一次 swap 会修改 `state`，而 RMW 总读到最新值）：

```
                     Receiver: 写 waiting_thread → CAS(Pending→Waiting)
   ┌──────────┐      ─────────────────────────────────────────────►  ┌──────────┐
   │ Pending  │                                                       │ Waiting  │
   └──────────┘      Sender: 写 val → swap(→Ready)                    └──────────┘
        │            ──────────────────────────────────────────────────────┼──►
        │                       （观察到旧值 Pending，不 unpark）           │ swap 观察到
        ▼                                                              ▼ 旧值 Waiting
   ┌──────────┐            Sender: 读 waiting_thread → unpark      （unpark 路径）
   │  Ready   │ ◄──────────────────────────────────────────────────────────┘
   └──────────┘
```

两条合法交错的逐帧对照：

| 时刻 | 交错 A：先 send 后 recv | 交错 B：先 recv 后 send |
|------|------------------------|------------------------|
| t1 | Sender 写 `val` | Receiver 写 `waiting_thread` |
| t2 | Sender `swap(Ready)`，读旧值 **Pending** → 不 unpark | Receiver CAS(Pending→Waiting) 成功 → `park()` |
| t3 | Receiver CAS 见到 **Ready** → **失败**（失败序 Acquire）→ 跳过 park | Sender 写 `val` |
| t4 | Receiver 直接读 `val` | Sender `swap(Ready)`，读旧值 **Waiting** → take `waiting_thread` → `unpark` |
| t5 | — | Receiver 从 `park` 返回，读 `val` |

字段写权限表（状态机的「宪法」）：

| 状态 | `waiting_thread` | `val` |
|------|------------------|-------|
| `Pending` | 无人读写 | 无人读写 |
| `Waiting` | Receiver 已写完；仅 Sender 在 swap 观察到 Waiting 后可读一次 | **仅 Sender 可写** |
| `Ready` | 无人再碰 | Receiver 独占读取（`take`） |

#### 4.1.3 源码精读

**三态枚举**——没有显式判别值，按 Rust 规则依次编号为 0、1、2：

[src/job.rs:17-21](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L17-L21)

```rust
enum State {
    Pending,
    Waiting,
    Ready,
}
```

**通道本体**——一个 `AtomicU8` 状态字加两个 `UnsafeCell` 字段，字段上的文档注释就是 4.1.2 那张权限表：

[src/job.rs:23-33](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L23-L33)

```rust
#[derive(Debug)]
#[repr(C)]
struct Channel<T = ()> {
    state: AtomicU8,
    /// Can only be written only by the `Receiver` and read by the `Sender` if
    /// `state` is `State::Waiting`.
    waiting_thread: UnsafeCell<Option<Thread>>,
    /// Can only be written only by the `Sender` and read by the `Receiver` if
    /// `state` is `State::Ready`.
    val: UnsafeCell<Option<Box<thread::Result<T>>>>,
}
```

三个细节值得停下来咀嚼：

1. `val` 里为什么要多套一层 `Box`？注意 `Sender::send` 写入的是 `Box::new(val)`。`Box` 让这个字段**无论 `T` 多大都恒为单指针大小**——这是 `harness` 里 `mem::transmute(Sender → Sender<T>)` 的布局前提之一（[src/job.rs:169-173](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L169-L173) 的 SAFETY 注释明说了这一点）。`#[repr(C)]` 固定字段顺序、`Box` 固定字段大小，两者合起来才敢做类型擦除（完整论证在 u3-l3）。
2. 默认类型参数 `<T = ()>`：`JobQueue` 里存的 `Job` 实际是 `Job<()>`，配套的通道就是 `Channel<()>`；真正的 `Channel<T>` 只在 `harness` 中经 transmute 还原。
3. `waiting_thread` 存 `Option<Thread>`：`None` 是初始值，接收者进入等待前把当前线程句柄写进去，Sender 取走（`take`）后复归 `None`。

**初始态为 Pending**：

[src/job.rs:35-43](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L35-L43)

```rust
impl<T> Default for Channel<T> {
    fn default() -> Self {
        Self {
            state: AtomicU8::new(State::Pending as u8),
            waiting_thread: UnsafeCell::new(None),
            val: UnsafeCell::new(None),
        }
    }
}
```

**构造函数**——`Arc` 让两端共享同一份 `Channel`；`Sender` 是 `job.rs` 私有类型（不进 `use` 列表），`Receiver` 则被 `lib.rs` 导入使用：

[src/job.rs:45-46](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L45-L46)、[src/job.rs:84-85](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L84-L85)、[src/job.rs:107-111](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L107-L111)、[src/lib.rs:65](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L65)

```rust
pub struct Receiver<T = ()>(Arc<Channel<T>>);
struct Sender<T = ()>(Arc<Channel<T>>);

fn channel<T: Send>() -> (Sender<T>, Receiver<T>) {
    let channel = Arc::new(Channel::default());
    (Sender(channel.clone()), Receiver(channel))
}
```

**通道的创建点在 `pop_front`，而不是任务入队时**——这印证了 u2-l4 的结论「channel 与 receiver 延迟到送出才建」：走 `join_seq` 顺序路径的任务永远不会付出这条通道的构造代价：

[src/job.rs:255-274](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L255-L274)

```rust
pub fn pop_front(&mut self) -> Option<JobShared> {
    let job = unsafe { self.0.pop_front()?.as_ref() };

    let (sender, receiver) = channel();   // ← 只有真的要跨线程送出，才创建通道

    job.receiver.set(Some(receiver));      // ← Receiver 装回栈上的 Job，供发起线程日后收结果
    ...
}
```

#### 4.1.4 代码实践

1. **实践目标**：验证「无显式判别值的枚举从 0 开始编号」这一前提，把状态字和三态枚举对应起来。
2. **操作步骤**：新建一个临时 crate（如 `cargo new state-demo`），在 `main.rs` 写入下面代码并 `cargo run`（示例代码）：

   ```rust
   enum State { Pending, Waiting, Ready }

   fn main() {
       println!("Pending = {}", State::Pending as u8);
       println!("Waiting = {}", State::Waiting as u8);
       println!("Ready   = {}", State::Ready as u8);
   }
   ```

3. **需要观察的现象**：三行输出分别是 0、1、2。
4. **预期结果**：`State as u8` 的取值与状态机图中的三个格子一一对应；这也解释了为什么 `Channel::default` 里写 `State::Pending as u8` 即初始值 0。
5. 具体输出以本地运行为准（该行为由 Rust 语言规范保证，属于稳定语义）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `val` 的类型是 `UnsafeCell<Option<Box<thread::Result<T>>>>`，而不是直接 `UnsafeCell<Option<thread::Result<T>>>`？

**参考答案**：`Box` 把「大小随 `T` 变化」的数据压成恒定的单指针大小，使 `Channel<T>` 与 `Channel<()>` 布局完全一致；配合 `#[repr(C)]` 固定字段顺序，`harness` 里的 `mem::transmute(Sender → Sender<T>)` 才是良定义的（详见 u3-l3）。若不用 `Box`，不同 `T` 的通道大小不同，类型擦除立刻破产。

**练习 2**：状态机里，`Waiting` 状态能被谁观察到？观察到意味着什么？

**参考答案**：只有 `Sender::send` 的 `swap` 能观察到——它拿到的**旧值**是 `Waiting`。观察到的语义是：接收者已把线程句柄写进 `waiting_thread` 并即将（或已经）停车，因此 Sender 必须在写完 `val` 之后 `unpark` 它。`Receiver` 自己从不读 `Waiting` 这个值。

**练习 3**：如果给 `Channel` 加一把 `Mutex` 保护两个字段行不行？为什么 chili 不这么做？

**参考答案**：功能上完全可行（第 5 节你就来实现一个），但每次 `recv`/`send` 都要两次加锁解锁，而本通道处于「任务已被跨线程送出」的冷路径上、每次只传一个值——状态机方案用一次原子 RMW 就完成了「加锁 + 传递窗口 + 内存序」三件事。这是 chili「用协议换锁」哲学的最小标本。

### 4.2 Receiver::recv 与 park

#### 4.2.1 概念说明

`recv` 要解决的问题是：**「我先到，值还没来」时如何安全入睡，并且保证睡下之前不会漏掉对方的唤醒**。并发编程里经典的「丢失唤醒（lost wakeup）」事故是这样的顺序——

```
① 接收者：检查「有值吗？」→ 没有
② 发送者：放值、喊「有了！」      ← 喊的时候没人在听
③ 接收者：park() 睡死            ← 再也没人喊了 → 死等
```

根治办法是把「登记自己」和「换状态」做成**一次原子的状态推进**，并严格排定三步顺序：**先写 `waiting_thread` → 再 CAS 到 `Waiting` → 最后 `park`**。这样发送者要么在接收者登记之前就送完了值（CAS 会失败，接收者根本不睡），要么必然能在 `Waiting` 状态下看到登记信息并 unpark 它——而 unpark 先于 park 到达也无所谓，令牌会替它「排队」（2.4 节的文档引语①）。

#### 4.2.2 核心流程

`recv` 的决策树：

```
recv(self)                         # self 被消费 → 终身只能调用一次
  ① 写 waiting_thread = Some(当前线程)     # 普通写，此刻无人可能读它
  ② CAS(Pending → Waiting, 成功序 AcqRel, 失败序 Acquire)
       ├─ 成功（交错 B）：
       │      park()                       # 安心停车；醒来即意味着 Sender 已 unpark
       │      → 读 val 并返回
       └─ 失败（交错 A：state 已是 Ready）：
              跳过 park                      # 失败序 Acquire 已与 send 的 Release 同步
              → 读 val 并返回
```

为什么「先写 `waiting_thread` 再 CAS」不可颠倒——反例逐帧推演：

```
（错误顺序：先 CAS 再写 waiting_thread）
t1  Receiver CAS(Pending→Waiting) 成功
t2  Sender swap(Ready)，旧值 Waiting → 读 waiting_thread —— 还是 None！
t3  Sender：if let Some(...) 不成立 → 不 unpark
t4  Receiver 写 waiting_thread，park() → 无人唤醒 → 死等
```

注意 t2 里 Sender 读到的是**未初始化的 `None`** 而非数据竞争（CAS 的 Release 半边尚未执行，这次普通读本身就不合法）——无论按哪种方式失败，协议都已崩塌。正确顺序下，CAS 成功序 `AcqRel` 的 **Release 半边**把 `waiting_thread` 的普通写「发布」出去，Sender 的 `swap`（Acquire 半边）读到 `Waiting` 时必然看得到它。

#### 4.2.3 源码精读

**判空接口**——`is_empty` 的含义是「通道里还没有值」，是发起线程「等待时偷任务」循环的出口判据：

[src/job.rs:48-51](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L48-L51)

```rust
pub fn is_empty(&self) -> bool {
    self.0.state.load(Ordering::Acquire) != State::Ready as u8
}
```

用 `Acquire` 而非 `Relaxed`：读到 `Ready` 的同时与 `send` 的 `swap`（Release）建立同步，使「`is_empty() == false`」成为一个有意义的承诺——此刻去读 `val` 是安全的。本库中真正的读取发生在 `recv` 里（那里另有 Acquire 兜底），但作为公共方法，`Acquire` 让它自身语义自洽。

`recv` 全文——三个动作（写句柄、CAS、park/直读）与 SAFETY 注释一一对应：

[src/job.rs:53-82](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L53-L82)

```rust
pub fn recv(self) -> thread::Result<T> {
    // SAFETY:
    // Only this thread can write to `waiting_thread` and none can read it yet.
    unsafe { *self.0.waiting_thread.get() = Some(thread::current()) };

    if self
        .0
        .state
        .compare_exchange(
            State::Pending as u8,
            State::Waiting as u8,
            Ordering::AcqRel,   // 成功序：Release 半边发布上面那句普通写
            Ordering::Acquire,  // 失败序：见到 Ready 时与 send 的 swap 同步，读 val 才安全
        )
        .is_ok()
    {
        thread::park();
    }

    // SAFETY:
    // To arrive here, either `state` is `State::Ready` or the above
    // `compare_exchange` succeeded, the thread was parked and then
    // unparked by the `Sender` *after* the `state` was set to `State::Ready`.
    //
    // In either case, this thread now has unique access to `val`.
    unsafe { (*self.0.val.get()).take().map(|b| *b).unwrap() }
}
```

读值的收尾 `(*self.0.val.get()).take().map(|b| *b).unwrap()`：`take` 把 `Option<Box<..>>` 拿空（状态机保证此后无人再写 `val`），`map(|b| *b)` 解引用 `Box` 得到 `thread::Result<T>`，`unwrap` 断言值一定在——由状态机协议背书。

**使用侧**：`is_empty` 循环 + `recv` 收尾，正是 u2-l4 讲过的「等待即帮忙」：

[src/lib.rs:297-315](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L297-L315)

```rust
while receiver.is_empty() {
    // …从货架上偷共享任务并用当前 scope 执行…
}
Some(receiver.recv())   // 值未到且货架已空，才真正阻塞在这里
```

#### 4.2.4 代码实践

1. **实践目标**：亲眼确认「先 unpark 后 park 不丢失」的令牌语义。
2. **操作步骤**：新建临时 crate，写入（示例代码）：

   ```rust
   use std::thread;

   fn main() {
       let me = thread::current();
       me.unpark();  // 先发令牌（此刻还没人 park）
       let start = std::time::Instant::now();
       thread::park();
       println!("park 立即返回，耗时 {:?}", start.elapsed());
   }
   ```

   运行后，再把 `me.unpark();` 那行注释掉运行一次作对照。
3. **需要观察的现象**：有 unpark 时程序立即打印耗时并正常退出；去掉 unpark 后程序挂起不退出（用 Ctrl-C 结束）。
4. **预期结果**：前者证明令牌可以「预存」——这正是 `recv` 敢在 CAS 成功后直接 `park`、而不担心 `unpark` 已经发生过的依据；后者说明没有令牌时 park 是真阻塞。
5. 具体耗时数值**待本地验证**（应为微秒量级以下）。

#### 4.2.5 小练习与答案

**练习 1**：`recv` 里 CAS 成功后为什么**不需要**再检查一次「值是否已经到了」就可以直接 `park`？

**参考答案**：两点合力。其一，若 `send` 尚未发生，它将来 `swap` 时必然读到 `Waiting`，于是必然 unpark，而令牌语义保证 unpark 先到也不丢失；其二，若 `send` 已经发生，它当时读到的旧值只能是 `Pending`（因为 CAS 还没执行），那样 `state` 已是 `Ready`，本次 CAS 会失败、根本走不到 `park`——「抢到 `Pending`」的只能有一方。

**练习 2**：标准文档说 `park` 可能虚假返回而不消费令牌。`recv` 没有用 `while` 复查状态、只 park 了一次，这隐含了什么假设？风险边界在哪里？

**参考答案**：它隐含假设「`park` 返回当且仅当被本通道的 `Sender` unpark 过」。严格按标准文档模型，虚假返回时 `state` 仍是 `Waiting`、`val` 仍是 `None`：轻则 `unwrap()` 直接 panic，重则与并发的 `send` 写 `val` 构成数据竞争（UB）。实际平台上 Rust 的 futex 实现不会产生虚假唤醒，miri 也按此模型执行，所以测试全绿——但这是一个「信任平台行为而非仅信任文档」的薄弱点，值得在代码审计（u4-l1）时单独立项。用 `Condvar::wait` 的经典 `while` 循环写法（第 5 节）正是对这种不确定性的系统防御。

**练习 3**：CAS 的失败序为什么必须是 `Acquire`？改成 `Relaxed` 会破坏什么？

**参考答案**：失败路径上 `state` 已是 `Ready`，接下来要**普通读** `val`。`Relaxed` 不与 `send` 的 `swap`（Release）同步，读到的可能是 Sender 写入之前的旧值——一个既非数据正确、又构成数据竞争的结果。失败序 `Acquire` 正是为「CAS 失败后直接读值」这条捷径配备的同步边。

### 4.3 Sender::send 与 unpark

#### 4.3.1 概念说明

`send` 是通道的「写端」，在 worker 线程上被 `harness` 调用，把 `catch_unwind` 的结果（闭包返回值或 panic 载荷）送回发起线程。它要同时完成三件事：

1. **写值**：把 `Box` 装的结果放进 `val`；
2. **换状态**：用 `swap` 无条件推进到 `Ready`——swap 总会成功，且返回的**旧值**顺便告诉它接收者处于哪种状态；
3. **按需唤醒**：只有旧值是 `Waiting` 才去 `unpark`。

第 3 点的「按需」是协议闭环的另一半：如果旧值是 `Pending`（交错 A），说明接收者的 CAS 尚未发生，它稍后必然见到 `Ready` 而走不停车直读的路径——此时 unpark 不但多余，还可能给这个线程留下一个**悬空令牌**，被同线程未来无关的 `park` 意外消费（2.4 节文档引语的告诫）。唤醒必须精确投递，这也是 `waiting_thread` 存在的全部意义。

#### 4.3.2 核心流程

```
send(self, val)                    # self 被消费 → 只能发送一次
  ① 写 val = Some(Box::new(val))   # 普通写，此刻 state != Ready，接收者不可能读它
  ② swap(→ Ready, AcqRel)
       ├─ 旧值 Pending（交错 A）：什么都不做，直接返回
       └─ 旧值 Waiting（交错 B）：
              take waiting_thread → Some(thread) → thread.unpark()
              # unpark(Release) 与 park(Acquire) synchronize-with：
              # ① 中写入的 val 对醒来后的接收者可见
```

本通道内存序的**配对总账**（两条链，各自不可缺失）：

```
链① 值的可见性（Sender → Receiver）
    send: 普通写 val ─Release─► swap(Ready, AcqRel)
                                    │
        ┌───────────────────────────┴───────────────────────────┐
        ▼（交错 A）                                              ▼（交错 B）
  recv 的 CAS 失败序 Acquire                        unpark 与 park synchronize-with
        │                                                       │
        └──────────────► 接收者普通读 val 安全 ◄─────────────────┘

链② 唤醒目标的可见性（Receiver → Sender）
    recv: 普通写 waiting_thread ─Release─► CAS 成功序 AcqRel
                                                    │
    send: swap(Ready, AcqRel) 的 Acquire 半边 ◄─────┘
        │
        └─► Sender 普通读 waiting_thread 安全 → take → unpark
```

用 happens-before 记号总结：\(\text{write}(val) \prec \text{swap}_{Rel} \prec \text{swap}_{Acq}\ldots\) 不引入公式也能一句话说清——**每一条普通读写都被夹在「发布它的 Release」与「读到它的 Acquire」之间，且两端由同一个原子变量 `state` 上的 RMW 链条连接**。

#### 4.3.3 源码精读

`send` 全文：

[src/job.rs:87-105](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L87-L105)

```rust
impl<T: Send> Sender<T> {
    pub fn send(self, val: thread::Result<T>) {
        // SAFETY:
        // Only this thread can write to `val` and none can read it yet.
        unsafe {
            *self.0.val.get() = Some(Box::new(val));
        }

        if self.0.state.swap(State::Ready as u8, Ordering::AcqRel) == State::Waiting as u8 {
            // SAFETY:
            // A `Receiver` already wrote its thread to `waiting_thread`
            // *before* setting the `state` to `State::Waiting`.
            if let Some(thread) = unsafe { (*self.0.waiting_thread.get()).take() } {
                thread.unpark();
            }
        }
    }
}
```

逐条对账：

- 第一段 SAFETY 注释守护的是权限表：「此刻 `state` 还不是 `Ready`，所以接收者无权读 `val`」——写操作因此无竞争。
- 第二段 SAFETY 注释**一字不差地重述了 4.2 的顺序约定**（接收者先写线程号再置 `Waiting`），这是 `swap` 的 `Acquire` 半边能安全读到 `waiting_thread` 的依据——两条注释互为因果，删掉任何一侧协议都不完整。
- `swap` 用 `AcqRel` 而非 `Release`：Release 半边发布 ① 的 `val` 写入；**Acquire 半边专门服务链②**——与接收者 CAS 成功序的 Release 配对，使 `waiting_thread` 的普通读合法。两头都载重，一个不能省。
- `if let Some(thread)`：按协议此处的 `Option` 必为 `Some`（Receiver 在置 `Waiting` 前已写好句柄），这个判断是防御性写法；`take` 顺带把句柄清空，避免 `Channel` 多活一个线程引用。

**send 的唯一调用点**——worker 线程执行完闭包后立刻发送，panic 也在此时被装进 `Err`：

[src/job.rs:175](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L175)

```rust
sender.send(panic::catch_unwind(AssertUnwindSafe(|| f(scope))));
```

发起线程一侧，`recv` 的返回值在 `join_heartbeat` 里被拆开，`Err` 走 `resume_unwind` 重新抛出（详见 u4-l2）：

[src/lib.rs:368-371](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L368-L371)

```rust
if let Some(receiver) = job.take_receiver() {
    let ra = match self.wait_for_sent_job(receiver) {
        Some(Ok(val)) => val,
        Some(Err(e)) => panic::resume_unwind(e),
```

#### 4.3.4 代码实践

1. **实践目标**：在真实调度中观察「swap 观察到 Waiting → unpark」这条路真的会被走到，并看清它与 `join_wait` 测试的对应关系。
2. **操作步骤**（本地临时副本，观察完务必还原，不要提交）：
   - 打开 `src/job.rs`，在 `send` 的 `if let Some(thread) = ...` 分支内、`thread.unpark();` 之前加一行 `eprintln!("[send] unpark a waiting receiver");`；
   - 运行 `cargo test --lib -- --nocapture join_wait`（该测试用 `thread_count = 2`、`heartbeat_interval = 1µs` 构造跨线程执行，见 [src/lib.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs) 内嵌测试模块）；
   - 还原代码，再跑一遍 `cargo test --all` 确认全绿。
3. **需要观察的现象**：加了打印后，测试输出中应能出现 `[send] unpark a waiting receiver`，且测试仍通过。
4. **预期结果**：证明 `join_wait` 的负载下确实发生了交错 B（接收者先进入 `Waiting` 再被唤醒）；对照无打印时的静默输出，体会这条冷路径平时多么低调。
5. 打印条数取决于调度，**待本地验证**（可能不止一条，也可能偶发为 0 条后需重跑）。

#### 4.3.5 小练习与答案

**练习 1**：把 `send` 的 `swap` 从 `AcqRel` 改成 `Release`，链②会在哪一步断裂？

**参考答案**：`swap` 将不再与接收者 CAS 的 Release 同步（链②断裂），Sender 读 `waiting_thread` 时可能看不到接收者的普通写——读到过期/未初始化的内容，这本身构成数据竞争（UB）；也可能读到 `None` 而跳过 unpark，接收者永久停车。链①（`val` 的发布）不受影响，所以小负载测试可能仍然全绿——这类 bug 最危险之处正是「平时不炸」。

**练习 2**：`send` 和 `recv` 都拿 `self`（而非 `&self`），这除了「只能调用一次」之外，对 unsafe 协议还有什么意义？

**参考答案**：消费 `self` 同时消费了 `Arc` 的一个引用，配合「只调用一次」，编译器层面保证了**每个普通字段在生命周期内只有一个写者、且写窗口唯一**——`UnsafeCell` 协议最怕的「同一端被两个线程先后调用」在类型上即被杜绝。若改成 `&self` 允许重复调用，`val` 可能被写两次、与读并发，状态机的前提整体瓦解。

**练习 3**：为什么 `unpark` 必须放在 `swap` **之后**，而不是写完 `val` 就 unpark？

**参考答案**：unpark 与 park 的 synchronize-with 只把「unpark 之前的操作」发布给醒来的接收者。若在 `swap` 之前 unpark，接收者醒来后读 `state`/`val` 时，`swap` 尚未发生或尚未对它可见，链①断裂——可能读到未初始化的 `val`。「先写完值、换好状态、最后投递唤醒」是发布唤醒的标准节奏。

## 5. 综合实践

**任务**：参照 `Channel` 的语义，用纯 safe Rust（`Mutex` + `Condvar`）实现一个等价的最小单值通道 `MiniChannel`，并用三个测试覆盖三种到达顺序。你会亲身体会：状态机里手写的每一条规则，在锁的世界里对应 `while` 循环与条件判断的哪一句。

新建独立 crate（`cargo new mini-channel --lib`），写入以下内容（示例代码）：

```rust
use std::sync::{Arc, Condvar, Mutex};

struct Shared<T> {
    slot: Mutex<Option<T>>, // 通道里唯一的"值"，“有没有值”就是全部状态
    ready: Condvar,
}

pub struct Sender<T>(Arc<Shared<T>>);
pub struct Receiver<T>(Arc<Shared<T>>);

pub fn channel<T>() -> (Sender<T>, Receiver<T>) {
    let shared = Arc::new(Shared {
        slot: Mutex::new(None),
        ready: Condvar::new(),
    });
    (Sender(shared.clone()), Receiver(shared))
}

impl<T> Sender<T> {
    /// 消费 self：与 job.rs 的 Sender::send 一致——只能发送一次。
    pub fn send(self, val: T) {
        let mut slot = self.0.slot.lock().unwrap();
        *slot = Some(val);
        drop(slot); // 先解锁再通知，唤醒的接收者不必再等锁
        self.0.ready.notify_one();
    }
}

impl<T> Receiver<T> {
    /// 消费 self：与 job.rs 的 Receiver::recv 一致——只能接收一次。
    pub fn recv(self) -> T {
        let mut slot = self.0.slot.lock().unwrap();
        while slot.is_none() {
            // Condvar::wait 文档同样警告虚假唤醒，必须用 while 复查——
            // 对比 job.rs 中"裸 park 一次"的写法（见 4.2.5 练习 2）
            slot = self.0.ready.wait(slot).unwrap();
        }
        slot.take().unwrap()
    }
}
```

三个顺序测试（示例代码，加入同一文件的 `#[cfg(test)] mod tests`）：

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        sync::atomic::{AtomicBool, Ordering},
        thread,
        time::{Duration, Instant},
    };

    // 顺序 1：先 send 后 recv —— 值已在通道里，recv 不应阻塞
    #[test]
    fn send_before_recv() {
        let (sender, receiver) = channel::<u32>();
        thread::spawn(move || sender.send(42));
        thread::sleep(Duration::from_millis(100)); // 确保 send 先完成
        let start = Instant::now();
        assert_eq!(receiver.recv(), 42);
        assert!(start.elapsed() < Duration::from_millis(50));
    }

    // 顺序 2：先 recv 后 send —— 接收线程先进入 recv，再被 send 唤醒
    #[test]
    fn recv_before_send() {
        let (sender, receiver) = channel::<u32>();
        let entered = Arc::new(AtomicBool::new(false));
        let flag = entered.clone();
        let handle = thread::spawn(move || {
            flag.store(true, Ordering::SeqCst); // 下一行马上进 recv
            receiver.recv()
        });
        while !entered.load(Ordering::SeqCst) {}
        thread::sleep(Duration::from_millis(100));
        sender.send(7);
        assert_eq!(handle.join().unwrap(), 7);
    }

    // 顺序 3：recv 中途阻塞再被唤醒 —— 用耗时断言证明 recv 确实睡过
    #[test]
    fn recv_blocks_then_woken() {
        let (sender, receiver) = channel::<&'static str>();
        let handle = thread::spawn(move || {
            let begin = Instant::now();
            let val = receiver.recv();
            (val, begin.elapsed())
        });
        thread::sleep(Duration::from_millis(200));
        sender.send("hello");
        let (val, waited) = handle.join().unwrap();
        assert_eq!(val, "hello");
        assert!(
            waited >= Duration::from_millis(150),
            "recv 应阻塞约 200ms，实测 {:?}",
            waited
        );
    }
}
```

**操作与验收**：

1. `cargo test` 三个测试应全部通过（具体耗时数值待本地验证）。
2. **对照阅读**：拿你的实现与 [src/job.rs:53-105](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L53-L105) 逐行对表——
   - `Mutex<Option<T>>` 的「锁 + is_none 判断」= 状态机里 `state` 的三态裁决；
   - `while slot.is_none()` = 对 `Condvar` 虚假唤醒的防御，正对应 4.2.5 练习 2 指出的裸 `park` 薄弱点；
   - `notify_one` 只在放值后调用 = `send` 只在观察到 `Waiting` 时 `unpark`；
   - `send`/`recv` 消费 `self` = 「单次调用」契约。
3. **思考题**（可选挑战）：把 `MiniChannel` 的 `Mutex<Condvar>` 换成 `thread::park`/`unpark` + 原子状态字重写一遍——你就基本重写了 `job.rs` 的通道；写完后问自己：`waiting_thread` 为什么必须先写再换状态？（答案就在 4.2.2 的反例里。）

## 6. 本讲小结

- `Channel` 用一个 `AtomicU8` 状态字（`Pending`/`Waiting`/`Ready`）划定两个 `UnsafeCell` 字段的读写权限，把「锁的职责」编码成状态机，换来单次 RMW 完成的零锁通道。
- `recv` 的三步定序——**先写 `waiting_thread`、再 CAS 到 `Waiting`、最后 `park`**——配合 park/unpark 的令牌语义（先 unpark 不丢失）与「只有一方能抢到 `Pending`」的 RMW 性质，从两个方向封死了丢失唤醒。
- `send` 的 `swap` 返回旧值即是裁决：旧值 `Pending` 则接收者自己会见到 `Ready`（免唤醒），旧值 `Waiting` 则精确 `unpark` 登记过的线程。
- 内存序是**成对的账**：`val` 的可见性由「swap 的 Release ↔ CAS 失败序的 Acquire / unpark↔park」双保险；`waiting_thread` 的可见性由「CAS 成功序的 Release ↔ swap 的 Acquire」单向担保。
- `Box` + `#[repr(C)]` 让 `Channel<T>` 布局与 `T` 无关，是下一讲 `Sender → Sender<T>` transmute 的地基。
- 裸 `park`（不循环复查）对标准文档允许的虚假唤醒未设防，实际平台不触发所以测试全绿——这是审计时应单独记录的信任边界。

## 7. 下一步学习建议

1. **u3-l3（Job 体系：类型擦除与任务队列）**：本讲埋下的两颗种子在那里发芽——`pop_front` 中 `Receiver<()>` 如何被装回真正的 `Job<T>`，以及 `harness` 中 `mem::transmute(Sender → Sender<T>)` 依赖 `repr(C)` + `Box` 定长的完整论证（[src/job.rs:155-176](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L155-L176)）。
2. **u4-l1（unsafe 安全不变量全面审读）**：把本讲的「信任边界」清单（裸 park、字段权限表、单次调用契约）并入全库 SAFETY 注释的系统性审计。
3. **u4-l2（panic 传播、测试体系与 miri）**：沿着 `catch_unwind → send(Err) → recv → resume_unwind` 走完 panic 的跨线程之旅，并用 `cargo +nightly miri test --lib` 让解释器替你复查本讲的每一条同步链。
4. 延伸阅读：标准库 [std::thread::park 文档](https://doc.rust-lang.org/std/thread/fn.park.html)（令牌与内存序的官方表述）与 [std::sync::mpsc](https://doc.rust-lang.org/std/sync/mpsc/index.html) 源码（对照通用通道为「多值 + 关闭语义」付出的复杂度）。
