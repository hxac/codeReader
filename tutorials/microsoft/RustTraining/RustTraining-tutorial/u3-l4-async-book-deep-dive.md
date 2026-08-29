# Async Rust 深潜：从 Future 到生产

## 1. 本讲目标

本讲从 u3-l2 建立的「章节写作范式」进入 async-book 的**内容本身**，走完它的三段式结构。学完后你应该能够：

1. 准确说出 `Future`、`poll()`、`Context`、`Waker` 四者之间的协作契约，并解释「忘记 wake = 程序静默挂起」的原因。
2. 解释 `Pin`/`Unpin` 为什么存在：`async fn` 会被编译成什么样的状态机、自引用结构为什么不能被移动、三种实际使用的 pin 手法各自适用什么场景。
3. 梳理全书的递进结构：手写一个 `Delay` future → 手写一个最小执行器 → 认识 Tokio 与运行时生态 → 掌握生产模式与常见陷阱。
4. 读懂 capstone 聊天服务器（ch17）如何把前面十几章的知识点组装成一个生产级应用。

## 2. 前置知识

本讲假设你已具备：

- **Rust 基础语法**：trait、泛型、`Arc`/`Mutex`、闭包。这是桥梁书（u3-l1 中的 Bridge 级别）前几章的内容。
- **会写 `async fn` 和 `.await`**：不需要理解它们的实现原理——恰恰相反，本讲就是要拆开这个黑盒。
- **u3-l2 的章节范式**：知道书中每章开头的「What you'll learn」目标框、Mermaid 图和可运行的 Rust 代码块（`book.toml` 中 `[output.html.playground]` 的 `editable = true` 让每个 `rust` 代码块带运行按钮，见 [async-book/book.toml:L19-L21](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/book.toml#L19-L21)）。

几个本讲反复出现的术语，先用一句话建立直觉：

| 术语 | 直觉解释 |
|------|----------|
| **轮询（poll）** | 执行器主动问 future：「完事了吗？」 |
| **Future** | 一个可以被反复询问「完事了吗」的值，回答只有两种：`Ready(结果)` 或 `Pending(还没好)` |
| **Waker** | future 留给执行器的「闹钟」：好了就按铃，执行器才会再来问一次 |
| **执行器（executor）** | 驱动一堆 future 的循环：有人按铃就 poll 它，没人按铃就睡觉 |
| **Pin** | 类型系统层面的「不许搬家」标记，防止 future 内部的自引用指针失效 |
| **协作式调度** | 任务自己让出 CPU（返回 `Pending`），而不是被内核抢占。让出不及时 = 饿死同伴 |

## 3. 本讲源码地图

async-book 是七本书中唯一的 Deep Dive 级别（见 u3-l1），也是结构最「线性」的一本——它的 `SUMMARY.md` 把 17 章切成三个教学阶段 + 附录：

| 文件 | 作用 |
|------|------|
| [async-book/src/SUMMARY.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md) | 目录：Part I 机理（1–5 章）、Part II 生态（6–10 章）、Part III 生产（11–15 章）、附录（16–17 章） |
| [async-book/src/ch02-the-future-trait.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md) | `Future` trait 定义、Waker 契约、手写 `Delay` future——本讲模块 4.1 的主战场 |
| [async-book/src/ch03-how-poll-works.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch03-how-poll-works.md) | 执行器的 poll 循环 + 一个 40 行的最小执行器 `block_on`——综合实践的关键素材 |
| [async-book/src/ch04-pin-and-unpin.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch04-pin-and-unpin.md) | Pin/Unpin 的动机与三种 pin 手法——本讲模块 4.2 |
| [async-book/src/ch07-executors-and-runtimes.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch07-executors-and-runtimes.md) | 执行器职责、mio/io_uring、五大运行时对比与选型决策树——模块 4.3 |
| [async-book/src/ch08-tokio-deep-dive.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch08-tokio-deep-dive.md) | Tokio 的运行时风味、`tokio::spawn` 的 `Send + 'static` 要求——模块 4.3 |
| [async-book/src/ch12-common-pitfalls.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch12-common-pitfalls.md) | 九大陷阱：阻塞执行器、跨 await 持锁、取消安全等——模块 4.4 |
| [async-book/src/ch13-production-patterns.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch13-production-patterns.md) | 生产模式：优雅关停、背压、结构化并发——模块 4.4 |
| [async-book/src/ch17-capstone-project.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch17-capstone-project.md) | capstone：多房间异步聊天服务器，六步搭建——模块 4.4 与综合实践 |

三段式结构在 [SUMMARY.md:L7-L13](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md#L7-L13)（Part I: How Async Works）、[SUMMARY.md:L17-L23](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md#L17-L23)（Part II: The Ecosystem）和 [SUMMARY.md:L27-L33](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md#L27-L33)（Part III: Production Async）三处分界，capstone 则放在附录 [SUMMARY.md:L37-L40](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/SUMMARY.md#L37-L40)。正如 u3-l1 所说：这本书的 Part 划分是认知递进式的，须按顺序整读。

## 4. 核心概念与源码讲解

### 4.1 Future 与 poll 契约：异步世界的最小接口

#### 4.1.1 概念说明

一切异步 Rust 代码——`async fn`、`.await`、Tokio 的任务——最终都落在一个只有两个成员的 trait 上。ch02 开篇给出定义：

```rust
pub trait Future {
    type Output;
    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output>;
}

pub enum Poll<T> {
    Ready(T),   // 完成了，拿走结果
    Pending,    // 还没好——稍后再来问我
}
```

这段定义出自 [ch02-the-future-trait.md:L13-L24](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L13-L24)。

要理解的关键不是语法，而是**契约**：

1. **Future 是惰性的**。创建一个 future 什么都不做，必须有人调用 `poll` 它才开始执行。这和 C# 的 `Task` 「热火朝天启动」完全不同。
2. **poll 绝不能阻塞**。没准备好就立刻返回 `Pending`，把线程让给别人。
3. **返回 `Pending` 之前必须登记 Waker**。`Context` 里装着执行器发来的 Waker；future 要把它注册到某个「事件源」上，事件发生时由事件源调用 `waker.wake()`。忘了注册，执行器就永远不会再 poll 你——程序**静默挂起**，没有任何报错。这正是 ch02 目标框里写的契约（[ch02-the-future-trait.md:L3-L7](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L3-L7)）。

#### 4.1.2 核心流程

ch02 用一张 Mermaid 时序图描绘完整回合（[ch02-the-future-trait.md:L30-L61](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L30-L61)），文字化如下：

```text
执行器                Future(任务)           OS(epoll/kqueue)      Reactor(运行时)
  │── poll(cx) ───────▶                      │                       │
  │                    │── read(socket) ────▶│                       │
  │                    │◀── EAGAIN(未就绪) ──│                       │
  │                    │── 登记 Waker ─────────────────────────────▶│
  │◀── Pending ────────│                      │                       │
  │ (把任务移出运行队列，去 poll 别人 / 睡觉)  │                       │
  │                                           │── 新数据到达 ───────▶│
  │◀──────────────────── wake() ◀───────────────────────────────────│
  │ (任务回到运行队列)                         │                       │
  │── poll(cx) ───────▶                      │                       │
  │◀── Ready(data) ────│                      │                       │
```

书中 `Delay` future 的单次 `poll` 流程（伪代码）：

```text
poll():
    若 completed == true        → 返回 Ready(())          # 先查条件
    把 cx.waker() 存入共享槽     # 登记 Waker（每次 poll 都要刷新）
    若尚未启动定时线程:
        启动后台线程：睡眠 duration → 置 completed = true → 取出 Waker 并 wake()
    若 completed == true        → 返回 Ready(())          # 双重检查，防竞态
    返回 Pending
```

「双重检查」防的是这样一个竞态窗口：第一次检查 `completed`（false）与存入 Waker 之间，后台线程可能恰好置位了 `completed` 并取走了**旧的**（空的）Waker 槽去 wake——唤醒落空后，新存的 Waker 再也没人触发，任务永久沉睡。存完 Waker 再查一次，正好堵上这个窗口。ch03 的 `FlagFuture` 练习解答把这个模式总结为「check → store waker → check again」（[ch03-how-poll-works.md:L176-L183](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch03-how-poll-works.md#L176-L183)），并说明这是一切 I/O future 内部都在用的真实模式。

#### 4.1.3 源码精读

**四要素拆解**：ch02 在 `Ready42` 示例后逐项解释 `Output`（产出类型）、`poll()`（执行器调用）、`Pin<&mut Self>`（防止 future 被移动，留到 4.2）、`Context`（携带 Waker），见 [ch02-the-future-trait.md:L82-L86](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L82-L86)。

**Waker 契约原文**：[ch02-the-future-trait.md:L88-L90](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L88-L90) 明确写道：返回 `Pending` 的 future *必须* 安排 `waker.wake()` 稍后被调用，否则执行器永远不会再 poll 它、程序挂起。

**`Delay` 的数据结构**：完成标志、Waker 槽、时长、启动标记四个字段（[ch02-the-future-trait.md:L100-L106](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L100-L106)）。注意 `Arc<Mutex<...>>` 的组合：完成标志和 Waker 槽要被后台线程和 poll 两端共享，所以放进 `Arc`。

**`poll` 实现的四个关键步点**（完整实现见 [ch02-the-future-trait.md:L119-L156](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L119-L156)）：

1. [L124-L126](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L124-L126)：先查完成标志——每次 poll 都要重新核实真实条件，不能因为「被 wake 了」就假定就绪（防**虚假唤醒**）。
2. [L129](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L129)：无条件刷新 Waker——执行器每次 poll 传来的 Waker 可能不同。
3. [L132-L147](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L132-L147)：首次 poll 时才启动后台定时线程（又是惰性！），线程睡够后置位标志并调用 `w.wake()`——[L142-L145](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L142-L145) 的注释标出这是「CRITICAL」一行。
4. [L149-L154](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L149-L154)：存完 Waker 后的二次检查 + 返回 `Pending`。

ch03 把这些实践收束成 `poll` 的四条规则：**永不阻塞、每次重新登记 Waker、正确处理虚假唤醒、Ready 之后不得再 poll**（[ch03-how-poll-works.md:L129-L133](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch03-how-poll-works.md#L129-L133)）——这是手写 future 的完整检查清单。

#### 4.1.4 代码实践：亲手制造一次「静默挂起」

1. **实践目标**：验证「返回 `Pending` 却不 wake = 程序挂起」这条契约，而不只是背下它。
2. **操作步骤**：
   - 在仓库外新建一个一次性工程（不要改动本仓库）：

     ```bash
     cargo new ~/countdown-lab
     cd ~/countdown-lab
     cargo add futures
     ```

   - 把书中 `CountdownFuture` 的解答代码（[ch02-the-future-trait.md:L175-L205](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L175-L205)）原样抄进 `src/main.rs`（去掉重复的 `use`，保留一组），再补一个驱动器 `main`（示例代码，书中解答本身只定义类型、不包含驱动）：

     ```rust
     fn main() {
         let s = futures::executor::block_on(CountdownFuture::new(3));
         println!("result: {s}");
     }
     ```

   - `cargo run`，记录输出。
   - 然后把 `poll` 里的 `cx.waker().wake_by_ref();`（[L200](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L200)）这一行注释掉，再次 `cargo run`。
3. **需要观察的现象**：第一次运行应打印 `3...`、`2...`、`1...`、`Liftoff!`、`result: Liftoff!`；注释掉 wake 后，程序只打印 `3...` 就停住不动（`futures` 的 `block_on` 是事件驱动的：没有 wake 就永远 park 线程），需要 Ctrl+C 才能退出。
4. **预期结果**：上述现象即 Waker 契约的直接体现——任务被移出运行队列后再无人叫醒它。若你的机器上行为不符，请以本地实测为准并回读 [ch03-how-poll-works.md:L23-L24](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch03-how-poll-works.md#L23-L24) 的警告。

顺带一个不动手也能做的小观察：在渲染好的书页上，`CountdownFuture` 解答的代码块右上角有运行按钮（playground 注入隐藏 `main` 的机制见 u1-l4 / u3-l2）。点一下会发现它**编译通过但没有任何输出**——因为这段代码只定义了类型，从未被任何执行器驱动。「定义 future」和「驱动 future」是两件事，这正是本实践要补的后一半。

#### 4.1.5 小练习与答案

**练习 1**：`Ready42` 的 `poll` 为什么可以完全忽略参数 `cx`？
**答案**：它第一次 poll 就返回 `Ready`，永远不会 `Pending`，自然不需要登记 Waker；`cx` 对它毫无用处（[ch02-the-future-trait.md:L76-L78](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L76-L78)）。反过来，任何可能 `Pending` 的 future 都必须用 `cx.waker()`。

**练习 2**：如果把 `Delay::poll` 里「存 Waker 之后的二次检查」（L149-L152）删掉，什么场景下会挂起？
**答案**：当后台线程在「第一次检查 completed」与「存入 Waker」之间完成了睡眠并尝试 wake（此时槽里还是旧值或空值）时，唤醒会落空；poll 随后存入的新 Waker 再也没有人触发，任务永久 `Pending`。二次检查正好在这个窗口内补查一次条件，直接以 `Ready` 收场。

**练习 3**：书中说「在 C# 里 TaskScheduler 自动负责唤醒，Rust 里你要自己（或你用的 I/O 库）负责调用 `waker.wake()`」（[ch02-the-future-trait.md:L159-L161](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L159-L161)）。这句话对日常用 Tokio 写业务的开发者意味着什么？
**答案**：意味着日常几乎不会手写 Waker——`async fn`/`.await` 和 Tokio 的 I/O 原语已经替你实现了契约；但一旦程序挂起且无报错，第一反应应当是「哪个 future 返回了 `Pending` 却没人 wake」，而不是怀疑死锁或 CPU 问题。ch12 的调试工具节（见 4.4）正是沿这条线索排查的。

### 4.2 Pin 与 Unpin：为什么 `self` 是 `Pin<&mut Self>`

#### 4.2.1 概念说明

`poll` 的第一个参数不是 `&mut Self` 而是 `Pin<&mut Self>`——ch04 被标为全书最难（标题带 🔴）。动机分三步：

1. **编译器会把 `async fn` 变成状态机**。函数里每个 `.await` 点是一个暂停点，局部变量成为状态机的字段，整体变成一个 enum（`State0`/`State1`/`Complete`）。
2. **状态机可能自引用**。如果 `.await` 期间还持有一个指向自身另一个字段的引用（例如先 `let r = &data;` 再 `some_io().await`），状态机里就同时装着 `data` 和指向 `data` 的指针——一个**自引用结构**。
3. **自引用结构不能搬家**。整体移动到新地址后，内部指针仍指向旧地址，变成悬垂指针。Rust 的移动语义到处都是（赋值、传值、放进 `Vec`），必须用类型系统阻止对这类结构的移动——这个标记就是 `Pin<P>`。

`Unpin` 则是逃生舱：绝大多数类型（`i32`、`String`、`Vec`、`Arc`……）没有自引用，pin 它们是无操作；只有编译器生成的 async 状态机是 `!Unpin`。对 `Unpin` 类型，`Pin` 什么也不限制。

#### 4.2.2 核心流程

ch04 给出编译器变换的示意（[ch04-pin-and-unpin.md:L17-L38](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch04-pin-and-unpin.md#L17-L38)）：

```text
async fn example() {              enum ExampleStateMachine {
    let data = vec![1, 2, 3];         State0 { data: Vec<i32> },
    let reference = &data;    ⇒       State1 { data: Vec<i32>,
    use_ref(reference).await;                  reference: *const Vec<i32> },  // 指向自己
}                                      Complete,
                                   }
```

移动前后的对比（对应 [ch04-pin-and-unpin.md:L40-L57](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch04-pin-and-unpin.md#L40-L57) 的图）：

```text
移动前（有效）                     移动后（悬垂！）
┌──────────────────┐              ┌──────────────────┐
│ data @ 0x1000    │              │ data @ 0x2000    │  ← 搬了家
│ ref  = 0x1000 ───┼──┐           │ ref  = 0x1000 ───┼──┐
└──────────────────┘  │           └──────────────────┘  │
                      ▼                                 ▼
              指向 data，OK                     0x1000 已是垃圾 💥
```

实践中的决策表（对应 ch04 的 Quick Reference，[ch04-pin-and-unpin.md:L128-L135](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch04-pin-and-unpin.md#L128-L135)）：

| 场景 | 手法 |
|------|------|
| 存进集合、从函数返回 | `Box::pin(fut)` —— 堆上钉死，移动的只是指针 |
| `select!` / 手动 poll 的局部 future | `std::pin::pin!` 或 `tokio::pin!` —— 栈上钉死 |
| 函数签名接收已钉 future | `fut: Pin<&mut F>` |
| 需要创建后再移动 future | 约束 `F: Future + Unpin` |

#### 4.2.3 源码精读

**这不是学术玩具**：[ch04-pin-and-unpin.md:L59-L74](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch04-pin-and-unpin.md#L59-L74) 指出，**任何**在 `.await` 点持有引用的 `async fn` 都会生成自引用状态机——示例 `problematic()` 里 `slice` 借用 `data` 并跨过 `some_io().await`，生成的状态机搬一下家就是悬垂指针。

**`Pin<P>` 的真实约束**：[ch04-pin-and-unpin.md:L76-L95](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch04-pin-and-unpin.md#L76-L95) 演示被钉住的值仍可正常使用（`pinned.as_ref().get_ref()`），但拿不回会允许 `mem::swap`/移动的 `&mut String`——对 `!Unpin` 的状态机这条路被编译器堵死。

**日常会遇见 Pin 的三个位置**（[ch04-pin-and-unpin.md:L97-L109](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch04-pin-and-unpin.md#L97-L109)）：`poll` 签名、`Box::pin()`、`tokio::pin!()`。普通业务代码到这三个位置为止，几乎不需要自己写 `unsafe`。

**`Unpin` 逃生舱**：[ch04-pin-and-unpin.md:L111-L126](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch04-pin-and-unpin.md#L111-L126) 说明大多数类型是 `Unpin`，并给出实践建议：手写 future 若无自引用，就显式 `impl Unpin for MySimpleFuture {}`，让调用方好过。

**三段编译练习**：[ch04-pin-and-unpin.md:L137-L159](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch04-pin-and-unpin.md#L137-L159) 给出 Snippet A/B/C，解答在 [L161-L181](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch04-pin-and-unpin.md#L161-L181)——这是 4.2.4 实践的素材。

#### 4.2.4 代码实践：三段代码，先预测再编译

1. **实践目标**：用编译器验证 `Box::pin` / `tokio::pin!` / `Pin::new` 三者的边界。
2. **操作步骤**：

   ```bash
   cargo new ~/pin-lab
   cd ~/pin-lab
   cargo add tokio --features full
   ```

   把书中三个 Snippet（[ch04-pin-and-unpin.md:L142-L159](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch04-pin-and-unpin.md#L142-L159)）分别放进三个 `async fn`（示例代码，包装方式仅为可编译的完整工程）：

   ```rust
   #[tokio::main]
   async fn main() {
       snippet_a().await;
       snippet_b().await;
       snippet_c();
   }

   async fn snippet_a() { /* 原样粘贴 Snippet A */ }
   async fn snippet_b() { /* 原样粘贴 Snippet B */ }
   fn snippet_c()       { /* 原样粘贴 Snippet C */ }
   ```

   先在纸上写下「哪几个能编译」，再运行 `cargo check`，最后与书中解答 [L161-L181](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch04-pin-and-unpin.md#L161-L181) 对照。
3. **需要观察的现象**：`cargo check` 只对 Snippet C 报错，错误形如 `Pin::new` 要求 `T: Unpin`（E0277 一族）。
4. **预期结果**：A ✅（移动 `Box` 只移动指针）、B ✅（移动的是 `Pin<&mut>` 包装器，栈上的 future 没动）、C ❌（async 块是 `!Unpin`，安全 API `Pin::new` 拒绝它）——与书中解答一致。若想看 C 通过，按书中修复改成 `Box::pin(fut)` 即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么移动一个 `Pin<Box<F>>` 是安全的，明明 `Box` 本身可以移动？
**答案**：移动 `Box` 移动的是**指向堆的指针**，堆上那个 future 原地不动，自引用指针依然有效。`Pin` 约束的是「被指向的值」不搬家，而不是「包装器」不能动（[ch04-pin-and-unpin.md:L164](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch04-pin-and-unpin.md#L164)）。

**练习 2**：我们在 4.1 精读的 `Delay` 需要 pin 吗？
**答案**：不需要特殊处理。它的字段全是 `Unpin` 类型（`Arc`、`Duration`、`bool`），自动 trait 推导使 `Delay: Unpin`，pin 它是无操作。但 `poll` 签名统一是 `Pin<&mut Self>`，所以 `Delay` 的 `poll` 里直接 `self.started = true` 也能编译——`Pin<&mut T>` 对 `T: Unpin` 提供 `DerefMut`（[ch02-the-future-trait.md:L132-L133](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L132-L133)、[ch04-pin-and-unpin.md:L113](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch04-pin-and-unpin.md#L113)）。

**练习 3**：`Pin` 防移动的本质是防哪几个具体操作？
**答案**：防一切能拿到 `&mut Self`（进而 `mem::swap`/`mem::replace`/赋值移动）的路径。`Pin<&mut T>`（`T: !Unpin`）不再暴露 `DerefMut` 到「取出裸 `&mut T`」的 API，于是编译器层面挡住了移动；只有 `unsafe` 的 `new_unchecked`/`into_inner` 能绕过（[ch04-pin-and-unpin.md:L91-L94](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch04-pin-and-unpin.md#L91-L94)）。

### 4.3 执行器与运行时生态：从手写 block_on 到 Tokio

#### 4.3.1 概念说明

Part II（6–10 章）回答的问题是：**谁来回调 `poll`？** 答案是执行器（executor），它只有两个职责（[ch07-executors-and-runtimes.md:L9-L13](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch07-executors-and-runtimes.md#L9-L13)）：

1. 有人 wake 时去 poll 对应的 future；
2. 没人 wake 时**高效地睡觉**（靠 epoll/kqueue/io_uring 这类 OS 通知 API，而不是空转）。

围绕这两件事，ch07 把生态拆成层次：

- **mio**：最底层的跨平台 I/O 通知库，包装 epoll/kqueue/IOCP，本身不是执行器（[ch07-executors-and-runtimes.md:L49-L76](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch07-executors-and-runtimes.md#L49-L76)）。
- **io_uring**：Linux 的「完成式」I/O 模型，与 epoll 的「就绪式」相对（[ch07-executors-and-runtimes.md:L82-L92](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch07-executors-and-runtimes.md#L82-L92)）——它要求内核持有缓冲区所有权，与 Rust 惯用的借用式 `AsyncRead` 冲突，所以 `tokio-uring` 的 API 形态不同（buffer 被 move 进去再还回来）。
- **tokio**：生态默认选择，自带定时器、I/O、信号、同步原语、通道（[ch07-executors-and-runtimes.md:L148-L170](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch07-executors-and-runtimes.md#L148-L170)）。
- **async-std / smol / embassy**：std 镜像 API / 极简 / 嵌入式 no_std 三种替代路线，对比表见 [ch07-executors-and-runtimes.md:L265-L276](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch07-executors-and-runtimes.md#L265-L276)。

#### 4.3.2 核心流程

**最小执行器**（ch03，[ch03-how-poll-works.md:L26-L75](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch03-how-poll-works.md#L26-L75)）：

```text
block_on(future):
    把 future 钉在栈上（unsafe Pin::new_unchecked）
    造一个 no-op Waker（wake 了也什么都不做）
    循环:
        poll(future)
        Ready(v) → 返回 v
        Pending  → thread::yield_now()   # 真执行器这里应 park 线程等事件
```

**真实执行器的概念主循环**（[ch03-how-poll-works.md:L85-L102](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch03-how-poll-works.md#L85-L102)）：

```text
loop:
    while 队列里有被唤醒的任务:
        poll 它；Ready 则收尾，Pending 则留在队列等下一次唤醒
    睡觉，直到 epoll_wait/kqueue/io_uring 报告事件或某个 waker 触发
```

**选型决策树**（[ch07-executors-and-runtimes.md:L228-L263](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch07-executors-and-runtimes.md#L228-L263)）文字化：写网络服务且要 Axum/Hyper/Tonic 生态 → tokio；写库 → 只依赖 `std::future::Future` 保持运行时中立；嵌入式/no_std → embassy；追求极小依赖 → smol。

#### 4.3.3 源码精读

**`block_on` 的三处细节**（[ch03-how-poll-works.md:L36-L65](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch03-how-poll-works.md#L36-L65)）：

- L40：`unsafe { Pin::new_unchecked(&mut future) }`——泛型 `F` 可能 `!Unpin`，只能用不安全 API 钉在栈上，安全性注释写明「此后绝不再移动它」。这是 4.2 知识的直接应用。
- L43-L51：手工构造 `RawWaker`/`RawWakerVTable` 得到一个 no-op Waker——这解释了 `Waker` 本质上就是「函数指针 + 数据指针」的回调句柄。
- L55-L64：Pending 时 `yield_now()` 空转。书里立刻警告它浪费 CPU、不可用于生产（[L77-L79](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch03-how-poll-works.md#L77-L79)），但足以证明「执行器只是一个调用 poll 的循环」。

**Tokio 的两条硬约束**：`tokio::spawn` 要求 future 是 `Send + 'static`（[ch08-tokio-deep-dive.md:L63-L101](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch08-tokio-deep-dive.md#L63-L101)）。书里各用一句话解释了原因：

- **为什么 `'static`**（[L99](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch08-tokio-deep-dive.md#L99)）：spawn 出的任务独立运行，可能活得比创建它的作用域久，借用无法被证明有效，必须拿所有权。
- **为什么 `Send`**（[L101](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch08-tokio-deep-dive.md#L101)）：任务可能在另一条线程上恢复执行，跨 `.await` 持有的所有数据必须能跨线程。

**运行时风味**：默认多线程 work-stealing vs `current_thread` 单线程（后者任务无需 `Send`，适合简单工具与 WASM），见 [ch08-tokio-deep-dive.md:L9-L39](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch08-tokio-deep-dive.md#L9-L39)。

**递进结构小结**：手写 future（ch02/ch06）→ 手写执行器（ch03/ch07 前半）→ 用别人的执行器（tokio，ch07 后半/ch08）→ 知道何时不用它（ch09）——这条线正是本模块的骨架，也呼应书名 *From Futures to Production*。

#### 4.3.4 代码实践：并发 vs 顺序，用秒表说话

1. **实践目标**：体感理解 ch12 指出的「连续两个 `.await` 是顺序执行」陷阱，并认识 `tokio::join!`。
2. **操作步骤**：

   ```bash
   cargo new ~/join-lab
   cd ~/join-lab
   cargo add tokio --features full
   ```

   把下面代码写入 `src/main.rs`（示例代码，改编自 [ch12-common-pitfalls.md:L231-L254](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch12-common-pitfalls.md#L231-L254) 的 `slow`/`fast`）：

   ```rust
   use std::time::{Duration, Instant};

   async fn fetch(tag: &str) -> String {
      tokio::time::sleep(Duration::from_millis(500)).await;
      format!("done-{tag}")
   }

   #[tokio::main]
   async fn main() {
      let t0 = Instant::now();
      let a = fetch("a").await;
      let b = fetch("b").await;
      println!("sequential: {:?} ({a}, {b})", t0.elapsed());

      let t1 = Instant::now();
      let (c, d) = tokio::join!(fetch("c"), fetch("d"));
      println!("concurrent: {:?} ({c}, {d})", t1.elapsed());
   }
   ```

   `cargo run` 并对比两行耗时。
3. **需要观察的现象**：sequential 约 1.0 s，concurrent 约 0.5 s（各自有几十毫秒调度开销）。
4. **预期结果**：顺序执行总耗时 \( T = t_a + t_b \)，`join!` 并发总耗时 \( T = \max(t_a, t_b) \)。future 是惰性的：`fetch("c")` 创建后不开始跑，直到 `join!` 同时 poll 两者。

#### 4.3.5 小练习与答案

**练习 1**：ch03 的 `block_on` 用 no-op Waker 空转，为什么用它跑 `Delay`（哪怕删掉 `w.wake()`）也能正常完成？
**答案**：因为它根本不睡觉——`Pending` 之后照样一圈圈 poll，完成标志一变就被下一圈发现。no-op Waker 的空转执行器**掩盖**了忘记 wake 的 bug；只有事件驱动执行器（park 线程等 waker）才会把契约违约暴露成挂起。这解释了 4.1.4 实验为什么必须用 `futures::executor::block_on`。

**练习 2**：把一个持有 `Rc` 的 future 交给 `tokio::spawn` 会发生什么？
**答案**：编译失败。`Rc: !Send`，而 spawn 要求 `Send + 'static`（[ch08-tokio-deep-dive.md:L91-L96](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch08-tokio-deep-dive.md#L91-L96) 给出的反例注释）。出路：改 `Arc`，或用 `current_thread` 风味运行时（任务无需 `Send`，[ch08-tokio-deep-dive.md:L22-L27](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch08-tokio-deep-dive.md#L22-L27)）。

**练习 3**：为什么库作者应当只依赖 `std::future::Future` 而不是 tokio？
**答案**：业务逻辑对所有运行时是中立的——ch07 的三运行时练习证明「async 代码相同，只有入口和定时器/I/O API 不同」（[ch07-executors-and-runtimes.md:L281-L345](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch07-executors-and-runtimes.md#L281-L345) 的练习与解答）。绑定 tokio 会强迫库的使用者背上整套运行时（决策树也把「写库」指向 runtime-agnostic，[L249-L251](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch07-executors-and-runtimes.md#L249-L251)）。

### 4.4 生产模式与陷阱：Part III 与 capstone

#### 4.4.1 概念说明

Part III（11–15 章）假设你已会用 async，开始讲「用对」。ch12 列出九大陷阱，最致命的五个：

1. **阻塞执行器**：在 async 线程上跑 `std::fs::read` 或 `std::thread::sleep`——该线程上所有任务被饿死，这是 async Rust 的头号错误（[ch12-common-pitfalls.md:L10-L34](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch12-common-pitfalls.md#L10-L34)：错误/正确/另一正确三连对比；`std::thread::sleep` 的版本在 [L55-L67](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch12-common-pitfalls.md#L55-L67)）。修法：`spawn_blocking` 或 tokio 的异步 fs。
2. **跨 `.await` 持有 `std::sync::MutexGuard`**：锁住了 OS 线程而非任务（[ch12-common-pitfalls.md:L69-L140](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch12-common-pitfalls.md#L69-L140)）。书里给出两种修复——收窄作用域（Option 1）或换 `tokio::sync::Mutex`（Option 2）——并强调不能盲目拆分临界区：两半若不独立，拆开反而引入 TOCTOU 竞态，此时应持异步锁跨 await。
3. **取消即 Drop**：future 在任意 `.await` 点被放弃，部分完成的操作会留下不一致状态——转账扣了款没入账（[ch12-common-pitfalls.md:L142-L173](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch12-common-pitfalls.md#L142-L173)）。修法：事务化或补偿。
4. **没有 async Drop**：`drop()` 里不能 `.await`，只能 spawn 清理任务或提供显式 `async fn close`（[ch12-common-pitfalls.md:L175-L199](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch12-common-pitfalls.md#L175-L199)）。
5. **`select!` 饥饿与「看似并发的顺序执行」**：常备的流永远赢、慢流饿死，可用 `biased;` 显式排序（[L201-L229](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch12-common-pitfalls.md#L201-L229)）；连续 `.await` 是顺序执行（[L231-L258](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch12-common-pitfalls.md#L231-L258)，4.3.4 已实验）。

ch13 则给正面模式：**优雅关停**（watch 通道广播信号 + `select!` + 带超时等待，[ch13-production-patterns.md:L11-L83](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch13-production-patterns.md#L11-L83)）、**背压**（有界通道让生产者自然减速，[ch13-production-patterns.md:L106-L120](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch13-production-patterns.md#L106-L120)）、超时/重试/结构化并发等。

调试与测试同样属于生产技能：`tokio-console` 看 hung 任务、`#[tracing::instrument]` 让 span 跨 `.await` 存活、`#[tokio::test]` + `time::pause()` 无痛测超时逻辑（[ch12-common-pitfalls.md:L352-L416](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch12-common-pitfalls.md#L352-L416) 与 [L418-L607](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch12-common-pitfalls.md#L418-L607)）。

#### 4.4.2 核心流程

**优雅关停的信号流**（对应 [ch13-production-patterns.md:L85-L104](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch13-production-patterns.md#L85-L104) 的时序图）：

```text
Ctrl+C → main 收到 signal::ctrl_c() → shutdown_tx.send(true)
      → watch 通道通知所有 worker 的 shutdown.changed()
      → 每个 worker：做完手头这个请求 → break
      → main 带超时地等所有任务收尾 → 全部完成则干净退出，超时则强制退出
```

**capstone 的六步构建流程**（[ch17-capstone-project.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch17-capstone-project.md) 全文骨架）：

1. TCP accept 循环 + `tokio::spawn` 每连接一个任务（回显服务器）
2. 用 `broadcast` 通道实现房间，`Arc<RwLock<HashMap>>>` 管理房间表
3. 命令协议 `/join` `/nick` `/rooms` `/quit`
4. `watch` 通道 + `signal::ctrl_c()` 优雅关停
5. 生产加固：处理 `RecvError::Lagged`、昵称校验、5 分钟空闲超时
6. `#[tokio::test]` 双客户端集成测试

#### 4.4.3 源码精读

**陷阱的「教科书式案例」**：ch12 的挂死服务排障记（[ch12-common-pitfalls.md:L260-L283](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch12-common-pitfalls.md#L260-L283)）：服务 10 分钟后无响应、CPU 0%；`tokio-console` 看到 200+ 任务 Pending 在同一个锁上；根因是一个任务持 `std::sync::MutexGuard` 跨 `.await` 时 panic 毒化了锁。修复表把 `std::sync::Mutex`→`tokio::sync::Mutex`、加锁超时等逐项列出——这是把 4.4.1 陷阱 2 从理论变成事故复盘的最好材料。

**capstone 骨架代码**：

- Step 1 的 accept 循环（[ch17-capstone-project.md:L49-L80](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch17-capstone-project.md#L49-L80)）：`TcpListener::bind(...).await` → `listener.accept().await` → `tokio::spawn(async move { ... read_line 循环 ... })`。短短 30 行同时用到了 4.3 的 spawn/`'static`、ch11 的按行读取（`BufReader` + `read_line`）。
- Step 2 的房间状态（[ch17-capstone-project.md:L88-L103](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch17-capstone-project.md#L88-L103)）：`RoomMap = Arc<RwLock<HashMap<String, broadcast::Sender<String>>>>`，`entry().or_insert_with()` 建 100 条容量的有界 broadcast 通道——这正是 ch13 背压模式的落地。
- 客户端任务的双循环骨架（[ch17-capstone-project.md:L110-L144](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch17-capstone-project.md#L110-L144)）：`tokio::select!` 同时等「TCP 来了一行」和「房间广播来了消息」——`select!` 的实战用法，也埋着 ch12 讲的取消安全与饥饿问题。
- Step 4 的关停（[ch17-capstone-project.md:L168-L187](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch17-capstone-project.md#L168-L187)）：accept 循环里 `select! { accept, ctrl_c }`，Ctrl+C 后 `shutdown_tx.send(true)`——与 ch13 的模式逐字对应。
- Step 5 的空闲超时（[ch17-capstone-project.md:L200-L208](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch17-capstone-project.md#L200-L208)）：`tokio::time::timeout` 包住 `read_line`，`Ok(Ok(0)) | Ok(Err(_)) | Err(_)` 三个分支分别处理 EOF、错误、超时。

**书自己给出的知识点映射**：capstone 开篇的「What you'll practice」框（[ch17-capstone-project.md:L7-L14](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch17-capstone-project.md#L7-L14)）已把每一项练习标注了来源章节（Ch8 的 spawn 与通道、Ch11 的流、Ch12 的陷阱、Ch13 的模式、Ch10 的 async trait）——综合实践 Part C 将在此基础上扩展到六步全流程。评估标准表在 [L235-L244](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch17-capstone-project.md#L235-L244)，扩展方向（WebSocket、限流、TLS）在 [L246-L254](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch17-capstone-project.md#L246-L254)。

#### 4.4.4 代码实践：给一段带四个 bug 的代码排雷

1. **实践目标**：综合运用本模块的陷阱清单，完成 ch12 的「Spot the Bugs」练习。
2. **操作步骤**：
   - 题目代码在 [ch12-common-pitfalls.md:L290-L306](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch12-common-pitfalls.md#L290-L306)：`process_requests` 逐个抓 URL、`std::thread::sleep` 限速、持 `MutexGuard` 跨 `.await`。先不看书，列出你找到的全部 bug。
   - 在 `~/join-lab` 里做一个副本验证（示例代码改写：把 `reqwest::get(url)` 换成 4.3.4 的 `fetch` mock、`expensive_parse` 换成 `tokio::time::sleep`，即可脱离网络运行）：

     ```rust
     use std::sync::Mutex;

     async fn expensive_parse(s: &str) { tokio::time::sleep(std::time::Duration::from_millis(10)).await; }

     async fn process_requests(urls: Vec<String>) -> Vec<String> {
         let results = Mutex::new(Vec::new());
         for url in &urls {
             let response = fetch(url).await;                       // mock 抓取
             std::thread::sleep(std::time::Duration::from_millis(100)); // 限速？
             let mut guard = results.lock().unwrap();
             guard.push(response);
             expensive_parse(&guard.join(" ")).await;               // ???
         }
         results.into_inner().unwrap()
     }
     ```

   - 修复后再与书中解答 [L309-L345](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch12-common-pitfalls.md#L309-L345) 对照（解答用 `stream::iter(...).buffer_unordered(10)` 做并发 + 事后统一解析，直接消灭了 mutex）。
3. **需要观察的现象**：修复前后各跑一次，注意修复后总耗时的变化，以及 `cargo build` 时关于 `MutexGuard` 跨 await 的隐患是否被编译器提示（书中 L81-L84 解释了为何直接 `.await` 它能编译、`tokio::spawn` 它才会报 `Send` 错误）。
4. **预期结果**：四个 bug——顺序抓取、`std::thread::sleep` 阻塞执行器、guard 跨 await、整体无并发；修复后并发度上来、无锁、耗时显著缩短。行为以本地实测为准。

#### 4.4.5 小练习与答案

**练习 1**：慢客户端收不到消息时 `broadcast::recv()` 返回 `RecvError::Lagged(n)`，capstone 要求怎么处理？
**答案**：记日志后继续，不要崩溃——Lagged 本质是有界通道（100 条）实施背压的表现：慢消费者错过的是旧消息，断线重连或惩罚策略才是进一步动作（[ch17-capstone-project.md:L191-L199](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch17-capstone-project.md#L191-L199)）。

**练习 2**：什么时候必须用 `tokio::sync::Mutex` 而不是收窄 `std::sync::Mutex` 的作用域？
**答案**：当 `.await` 前后的两段临界区**不是相互独立**时——拆开会造成「检查时刻与使用时刻」之间的 TOCTOU 竞态，此时持异步锁跨 `.await` 才能保住事务语义（[ch12-common-pitfalls.md:L94-L140](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch12-common-pitfalls.md#L94-L140)，尤其是 L131-L140 的选型规则）。

**练习 3**：capstone Step 4 为什么选 `watch` 通道做关停信号，而不是 `mpsc`？
**答案**：关停信号要**广播给任意多个订阅者**（每个客户端任务一份 `shutdown_rx.clone()`），且各任务在任何时刻都能读到当前值；`watch` 正是「单值、多读、可轮询变化」的形态，`mpsc` 每条消息只能被一个接收者消费，无法广播（[ch17-capstone-project.md:L168-L189](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch17-capstone-project.md#L168-L189)、[ch13-production-patterns.md:L19-L33](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch13-production-patterns.md#L19-L33)）。

## 5. 综合实践

把本讲四个模块串成一次端到端演练：**手写 Delay future → 手写执行器驱动它 → 换真执行器复现 Waker 契约 → 读 capstone 做知识点映射**。全程在本仓库之外的一次性工程中进行（仓库本身只有 xtask 一段 Rust 代码，不允许改动）。

### Part A：Delay + 最小执行器（4.1 + 4.2 + 4.3）

```bash
cargo new ~/delay-lab
cd ~/delay-lab
```

把 `src/main.rs` 替换为以下内容——上半段 `block_on` 原样抄自 [ch03-how-poll-works.md:L36-L65](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch03-how-poll-works.md#L36-L65)，下半段 `Delay` 原样抄自 [ch02-the-future-trait.md:L92-L157](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L92-L157)，`main` 为拼装的示例代码：

```rust
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll, RawWaker, RawWakerVTable, Waker};
use std::thread;
use std::time::Duration;

// ---- 最小执行器（抄自 ch03）----
fn block_on<F: Future>(mut future: F) -> F::Output {
    let mut future = unsafe { Pin::new_unchecked(&mut future) };

    fn noop_raw_waker() -> RawWaker {
        fn no_op(_: *const ()) {}
        fn clone(_: *const ()) -> RawWaker { noop_raw_waker() }
        let vtable = &RawWakerVTable::new(clone, no_op, no_op, no_op);
        RawWaker::new(std::ptr::null(), vtable)
    }

    let waker = unsafe { Waker::from_raw(noop_raw_waker()) };
    let mut cx = Context::from_waker(&waker);

    loop {
        match future.as_mut().poll(&mut cx) {
            Poll::Ready(value) => return value,
            Poll::Pending => std::thread::yield_now(),
        }
    }
}

// ---- Delay future（抄自 ch02）----
struct Delay {
    completed: Arc<Mutex<bool>>,
    waker_stored: Arc<Mutex<Option<Waker>>>,
    duration: Duration,
    started: bool,
}

impl Delay {
    fn new(duration: Duration) -> Self {
        Delay {
            completed: Arc::new(Mutex::new(false)),
            waker_stored: Arc::new(Mutex::new(None)),
            duration,
            started: false,
        }
    }
}

impl Future for Delay {
    type Output = ();

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<()> {
        if *self.completed.lock().unwrap() { return Poll::Ready(()); }
        *self.waker_stored.lock().unwrap() = Some(cx.waker().clone());
        if !self.started {
            self.started = true;
            let completed = Arc::clone(&self.completed);
            let waker = Arc::clone(&self.waker_stored);
            let duration = self.duration;
            thread::spawn(move || {
                thread::sleep(duration);
                *completed.lock().unwrap() = true;
                if let Some(w) = waker.lock().unwrap().take() { w.wake(); }
            });
        }
        if *self.completed.lock().unwrap() { return Poll::Ready(()); }
        Poll::Pending
    }
}

fn main() {
    let start = std::time::Instant::now();
    println!("start");
    block_on(Delay::new(Duration::from_secs(1)));
    println!("done in {:?}", start.elapsed());
}
```

`cargo run`，预期输出：立即打印 `start`，约 1 秒后打印 `done in ~1s`。

**观察点**：即便你删掉 `w.wake()` 那一行，这个 busy-loop 执行器照样 1 秒后完成——因为它从不睡觉、不依赖 wake（4.3.5 练习 1 的结论落地）。

### Part B：换事件驱动执行器，复现「静默挂起」

```bash
cargo add futures
```

把 `main` 里的 `block_on(...)` 换成 `futures::executor::block_on(...)`（注意调用的是 `futures::` 的版本，我们自己写的那个可以留着对比）。分两次运行：

1. **原版 Delay**：正常 1 秒后完成——后台线程的 `w.wake()` 把 park 的执行器叫醒。
2. **注释掉 `w.wake()` 所在的 `if let` 块**：程序打印 `start` 后**永久停住**，Ctrl+C 退出。`futures` 的 `block_on` 是事件驱动的，Waker 契约被违反时它一动不动。

两相对照，ch02 目标框里那句「never call `wake()` = your program silently hangs」（[ch02-the-future-trait.md:L6](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch02-the-future-trait.md#L6)）就不再是书上的断言，而是你亲手造出过一次的事故。预期行为如上；若与本机观测不符，以本地为准并复查你是否注释对了行。

### Part C：capstone 知识点映射

通读 [ch17-capstone-project.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch17-capstone-project.md)（约 250 行，含六步代码骨架），然后独立完成下表（书只在 [L7-L14](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch17-capstone-project.md#L7-L14) 给了按机制的映射，你要补齐「按步骤」的映射）。参考答案：

| capstone 步骤 | 用到的机制 | 来源 |
|---------------|-----------|------|
| Step 1 accept 循环 | `tokio::spawn` 与 `Send + 'static`（socket 被_move_进任务） | ch08（[ch08-tokio-deep-dive.md:L63-L101](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch08-tokio-deep-dive.md#L63-L101)） |
| Step 1 按行读 TCP | `BufReader` + `read_line`（流的用法） | ch11 Streams |
| Step 2 房间表 | `Arc<RwLock<HashMap>>`；`/rooms` 用读锁避免阻塞其他客户端 | ch17 L158 本身的提示 |
| Step 2 消息分发 | `broadcast::channel(100)`——有界 = 天然背压 | ch13 背压（[ch13-production-patterns.md:L106-L120](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch13-production-patterns.md#L106-L120)） |
| Step 2 提示框 | 客户端任务用 `select!` 同时跑「读 TCP」与「收广播」两个循环 | ch13 优雅关停中的 select / ch12 饥饿 |
| Step 4 关停 | `watch` 通道广播 + `signal::ctrl_c()` + 停止接新连接、排空在途消息 | ch13（[ch13-production-patterns.md:L11-L83](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch13-production-patterns.md#L11-L83)） |
| Step 5 加固 | `RecvError::Lagged` 处理、`timeout` 空闲断连、取消安全意识 | ch12（[ch12-common-pitfalls.md:L142-L173](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch12-common-pitfalls.md#L142-L173)） |
| Step 6 测试 | `#[tokio::test]`、端口 0 让 OS 分配 | ch12 测试节（[ch12-common-pitfalls.md:L418-L460](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch12-common-pitfalls.md#L418-L460)） |
| 扩展方向 | 可插拔后端（历史持久化、WebSocket） | ch10 Async Traits |

完成映射后自问一个问题检验理解：**为什么每连接一个 `tokio::spawn` 的任务不会互相饿死？**（答案在 4.3：任务只在被 poll 时占用线程，`read_line` 未就绪即返回 `Pending`，线程立刻去跑别的任务——这正是 Part I 机理的最终回报。）

## 6. 本讲小结

- **Future 契约**：`poll` 只回答 `Ready`/`Pending`；返回 `Pending` 前必须登记 `Context` 里的 Waker，否则任务被移出队列后无人叫醒，程序静默挂起——`Delay` 的「check → store waker → check again」是所有 I/O future 的通用骨架。
- **Pin/Unpin**：`async fn` 编译成可能自引用的状态机，移动会使内部指针悬垂；`Pin` 在类型层面禁止移动，`Unpin` 是多数类型的逃生舱；日常只需认得 `poll` 签名、`Box::pin`、`tokio::pin!` 三个位置。
- **执行器与生态**：执行器 = 「有人 wake 就 poll，没人 wake 就睡」的循环；手写 40 行 `block_on` 可去魅，生产选 tokio（`spawn` 要求 `Send + 'static`），写库保持运行时中立。
- **生产与陷阱**：五大陷阱（阻塞执行器、跨 await 持锁、取消即 Drop、无 async Drop、select 饥饿/假并发）与两大模式（watch 优雅关停、有界通道背压）；调试靠 `tokio-console` + `tracing`，测试靠 `#[tokio::test]` + `time::pause()`。
- **递进结构**：手写 future（ch02/ch06）→ 手写执行器（ch03）→ 用 Tokio（ch07/ch08）→ 生产模式（ch12/ch13）→ capstone 组装（ch17），Part I→III 的分层就是「从 Future 到生产」的书名本身。

## 7. 下一步学习建议

- **补齐 Part I 剩余两章**：[ch05-the-state-machine-reveal.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch05-the-state-machine-reveal.md) 把 4.2 的示意状态机展开成完整代码，[ch06-building-futures-by-hand.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch06-building-futures-by-hand.md) 提供比 `Delay` 更复杂的手写案例——两者都是本讲 4.1/4.2 的自然延伸。
- **本讲跳过的章节**：ch09（Tokio 不适用的场景）、ch10（async trait）、ch11（流与 AsyncIterator）、ch14（「async 是优化而非架构」的工程观）、ch16（速查卡）——其中 ch11 是读懂 capstone Step 1 按行读取的前提，建议优先。
- **横向对照**：u3-l5 将进入 rust-patterns-book 与 type-driven-correctness-book，看「把约束编码进类型」如何与 `Pin`/`Send` 这些本讲出现的 marker trait 一脉相承；u3-l6 的 engineering-book 则把本讲 4.4 的调试/测试话题放进完整的生产 CI/CD 语境。
- **动笔验证**：若你想真正做完 capstone，直接从 [ch17-capstone-project.md:L45](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/async-book/src/ch17-capstone-project.md#L45) 的 Step 1 开始，按评估表（L235-L244）自查；做完可按 u4-l5 的流程把心得整理成对本仓库的文档贡献。
