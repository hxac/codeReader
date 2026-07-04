# 使用 select! 宏

> 讲义 id：`u2-l9`　依赖：`u1-l4`（克隆、共享、断开与迭代）　阶段：intermediate
> 代码 HEAD：`6195355ef1862f2c6172365d00645cb6f77417dc`

## 1. 本讲目标

学完本讲，你应当能够：

- 用 `select!` 宏把多个通道操作（`recv` / `send`）组合在一个代码块里，等待其中**任意一个**变为「就绪（ready）」并执行它。
- 写出 `select!` 的三种分支形态：**纯阻塞**、**立即 `default`**、**带超时 `default(timeout)`**，并知道它们分别对应收发 API 里的哪一种阻塞模式。
- 说清「就绪」的精确定义——尤其是「通道已断开、操作会返回错误」也算就绪。
- 区分 `select!`（多个操作同时就绪时**随机**选一个，公平）与 `select_biased!`（总是选**列表最前面**的那个，有优先级）。
- 对照源码看懂：这两个宏其实只是给同一个内部宏 `crossbeam_channel_internal!` 传了一个 `_IS_BIASED` 布尔值。

本讲只从**使用者视角**讲 `select!` / `select_biased!` 宏本身，**不**深入宏的逐条模式展开（那是 u3-l3「select! 宏展开机制」的主题），也**不**深入 `Select` 动态 API 的运行时调度（那是 u2-l10 与 u3-l1 的主题）。我们只会「点到为止」地引用宏展开后的内部调用，用来解释你观察到的行为。

## 2. 前置知识

本讲假设你已经掌握（来自 u1 系列）：

- `Sender<T>` / `Receiver<T>` 的基本 `send` / `recv`，以及三种阻塞模式（非阻塞 `try_*`、阻塞、带超时 `*_timeout`）。见 u1-l3。
- 克隆共享、断开（disconnected）语义：发送端全员 drop 后，剩余消息仍可接收、排空后 `recv` 立即返回 `Err(RecvError)`。见 u1-l4。
- crossbeam-channel 是 mpmc 通道，`Receiver` 可以被多个线程同时持有、同时接收。

几个本讲会用到的术语：

- **select（选择）**：在多个通道操作里「等待第一个就绪的并执行它」的模式，灵感来自 Go 的 `select` 语句与 CSP（Communicating Sequential Processes）。
- **就绪（ready）**：一个操作「不需要阻塞就能立刻完成」就算就绪——**即使完成的结果是返回一个错误**（比如通道已断开）。
- **公平（fair）/ 无偏（unbiased）**：当多个操作同时就绪时，给每个相同的被选中机会（近似均匀随机），避免某一个分支被长期「饿死」。
- **有偏（biased）**：总是优先选择列表里靠前的分支，可用来表达优先级。
- **flavor（风味）**：通道的底层实现种类（array/list/zero/at/tick/never），见 u2-l1。`select!` 对所有 flavor 一视同仁。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的地方 |
| --- | --- | --- |
| `src/select_macro.rs` | 定义 `select!` / `select_biased!` 两个公开宏，以及它们共同委托的内部宏 `crossbeam_channel_internal!`。 | 宏入口、`_IS_BIASED` 开关、三种 `default` 形态、随机/有偏的源头。 |
| `src/lib.rs` | crate 入口，含 `select!` 的文档级示例与 `pub use` 导出，以及供宏调用的 `internal` 隐藏模块。 | 两个 `recv` + 超时的示例、`internal` 后门。 |
| `src/select.rs` | `Select` 动态 API 与宏展开后调用的内部函数 `select` / `try_select` / `select_timeout`、`SelectedOperation`，以及 `utils::shuffle`（公平性的来源）。 | 解释宏展开后真正调到什么、随机打乱发生在哪里。 |

> 提示：`src/select_macro.rs` 是一个**声明宏（`macro_rules!`）**，全篇都是模式匹配规则。本讲不会逐条解读这些规则，只摘取能说明「用户行为」的关键片段。

## 4. 核心概念与源码讲解

### 4.1 select! 解决什么问题与基本语法

#### 4.1.1 概念说明

设想你有一个线程，需要同时「盯着」两个 `Receiver`：哪个先来消息就先处理哪个。如果只用 `recv`，你只能**串行**等待——先阻塞在 `r1.recv()` 上，期间 `r2` 哪怕来了消息你也看不见。这正是 `select!` 要解决的问题：

> 在一组通道操作里**等待任意一个变为就绪**，执行它对应的代码块，其余操作**不会被真正执行**。

这与 u1-l3 学过的「单条 `recv` 的三种阻塞模式」是同一个思想的多路版本：把多条操作并到一起等，谁先就绪谁先跑。

`select!` 的核心语义在它的 rustdoc 里写得很清楚：

[select_macro.rs:994-999](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L994-L999) —— 定义 `select!`：「等待任意一个操作就绪并执行它；若多个同时就绪，**随机**选一个（即无偏选择），需要优先级语义时用 `select_biased!`」。

一句话：`select!` = 「多路通道操作 + 公平（随机）仲裁」。

#### 4.1.2 核心流程

一个 `select!` 块由若干**分支（case）**组成，分支之间用逗号分隔。每个分支三段式：

```
操作(参数) -> 结果绑定 => 处理代码
```

共三种分支：

| 分支语法 | 含义 | 会阻塞吗 |
| --- | --- | --- |
| `recv(r) -> msg => { ... }` | 等待从 `r` 收到一条消息 | 是（若无 `default`） |
| `send(s, value) -> res => { ... }` | 等待向 `s` 发出一条消息 | 是（若无 `default`） |
| `default => { ... }` | **至多一个**；当其他操作都未就绪时执行 | 否（立即） |
| `default(timeout) => { ... }` | 至多一个；其他操作在 `timeout` 内都未就绪则执行 | 否（限时） |

执行流程（伪代码）：

```text
1. 检查所有 recv/send 分支是否「就绪」
2. 若有就绪：
   - select!  ：在就绪者中随机选一个，执行其代码块
   - select_biased!：选就绪者中最靠前的一个
3. 若无就绪：
   - 没有 default      => 阻塞，直到某个操作就绪（或通道断开）后被唤醒，回到步骤 2
   - 有 default        => 立即执行 default
   - 有 default(timeout)=> 等待 timeout，期间一旦有操作就绪则执行它；超时则执行 default
```

注意第 3 步的「回到步骤 2」：被唤醒后会**重新**判断就绪并仲裁，这正是 u2-l4 讲过的「register → park → 被 unpark → 复查」机制在 `select!` 层面的体现。

#### 4.1.3 源码精读：宏入口与「随机」承诺

`select!` 与 `select_biased!` 的宏定义异常简短——它们几乎什么都没做，只是包了一层、设了一个常量 `_IS_BIASED`，然后把整段 token 原样交给内部宏 `crossbeam_channel_internal!`：

[select_macro.rs:1135-1146](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L1135-L1146) —— `select!` 宏本体：设 `const _IS_BIASED: bool = false;`，随后 `crossbeam_channel_internal!($($tokens)*)`。

[select_macro.rs:1157-1167](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L1157-L1167) —— `select_biased!` 宏本体：唯一区别是 `const _IS_BIASED: bool = true;`。

也就是说，**两个宏是同一套实现**，区别仅在于一个布尔标志。这个标志最终会一路传到 `src/select.rs` 的调度核心，决定要不要在仲裁前「洗牌」（见 4.3.3）。

内部宏 `crossbeam_channel_internal!` 的总体结构在文件顶部有说明：

[select_macro.rs:4-21](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L4-L21) —— 宏分两大阶段：**解析（parsing）**（`@list` / `@case` 把 token 解析成结构化的分支列表）与**代码生成（codegen）**（`@init` / `@count` / `@add` / `@complete` 生成真正的选择逻辑）。

而 `src/lib.rs` 里有一个最经典的「两个 `recv` + 1 秒超时」示例，正是本讲代码实践的样板：

[lib.rs:266-289](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L266-L289) —— crate 文档示例：两个线程各发一条消息，主线程用 `select!` 在 `r1` / `r2` 上接收，并带 `default(Duration::from_secs(1))` 超时。

#### 4.1.4 代码实践：第一个 select!

> **目标**：把两个 `Receiver` 同时挂进一个 `select!`，体会「谁先就绪谁先跑」。

把下面这段保存为 `src/bin/select_basic.rs`（确保 `Cargo.toml` 里 `name = "crossbeam-channel"` 之外，你是在一个**依赖了 `crossbeam-channel` 的 crate** 里运行；最简单的方式是新建一个依赖本 crate 的 bin 工程）：

```rust
// 示例代码
use std::thread;
use std::time::Duration;
use crossbeam_channel::{unbounded, select};

fn main() {
    let (s1, r1) = unbounded();
    let (s2, r2) = unbounded();

    // 两个线程分别延迟不同时间后发消息
    thread::spawn(move || {
        thread::sleep(Duration::from_millis(50));
        s1.send("来自 r1").unwrap();
    });
    thread::spawn(move || {
        thread::sleep(Duration::from_millis(200));
        s2.send("来自 r2").unwrap();
    });

    // select! 会先命中先就绪的 r1
    select! {
        recv(r1) -> msg => println!("收到: {:?}", msg),
        recv(r2) -> msg => println!("收到: {:?}", msg),
    }
}
```

**操作步骤**：

1. 在一个依赖了 `crossbeam-channel = "0.5"` 的工程里新建上面的 bin。
2. `cargo run`。

**需要观察的现象 / 预期结果**：因为 `r1` 的消息 50ms 就到、`r2` 要 200ms，`select!` 几乎总是先打印 `收到: Ok("来自 r1")`。注意 `msg` 的类型是 `Result<&str, RecvError>`——`recv(r) -> msg` 绑定的就是普通 `recv()` 的返回值（见 4.4）。

> ⚠️ 本讲给出的运行结果均基于源码语义与官方文档推导，**实际输出请以本地 `cargo run` 为准（待本地验证）**，尤其涉及线程调度时序的部分。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面例子里的 `select!` 换成「先 `r1.recv()` 再 `r2.recv()`」的两条普通语句，会发生什么问题？

**参考答案**：会**串行阻塞**——必须先等到 `r1` 的消息到达并处理完，才会去 `r2.recv()`。在此期间 `r2` 的消息即使早就到了也无法被及时处理；若 `r1` 永远不来消息（且未断开），`r2` 将永远等不到。`select!` 的意义就是打破这种串行依赖。

**练习 2**：`select!` 块里允许出现两个 `default` 分支吗？

**参考答案**：不允许。宏在解析阶段会检测重复 `default` 并报编译错误——见 [select_macro.rs:484-492](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L484-L492)：`"there can be only one \`default\` case in a \`select!\` block"`。

---

### 4.2 三种分支形态：阻塞 / default / default(timeout)

#### 4.2.1 概念说明

`select!` 的「是否阻塞、阻塞多久」完全由**有没有 `default`、`default` 带不带参数**决定。这与 u1-l3 讲过的单条收发 API 的三种阻塞模式**完全对应**，只是升级成了「多路」版本：

| `select!` 形态 | 对应的单条 API | 行为 |
| --- | --- | --- |
| 无 `default` | `recv()` / `send()` | 无限阻塞，直到某个操作就绪或通道断开 |
| `default => ...` | `try_recv()` / `try_send()` | 不阻塞；当前没有任何操作就绪就立即走 `default` |
| `default(dur) => ...` | `recv_timeout(dur)` / `send_timeout(dur)` | 限时阻塞；`dur` 内无操作就绪就走 `default` |

`default` 分支**不是**一个通道操作，而是一个「兜底动作」：当所有 `recv`/`send` 分支此刻都不就绪时，由它来兜底。

#### 4.2.2 核心流程

宏在代码生成阶段（`@add`）会根据 `default` 的形态，调用三个**不同的内部函数**之一。这三个函数定义在 `src/select.rs`，是宏与运行时之间的真正桥梁：

```text
无 default       => internal::select(&mut handles, biased)        返回 SelectedOperation（必有一个）
default          => internal::try_select(&mut handles, biased)    返回 Option<SelectedOperation>，None 即走 default
default(timeout) => internal::select_timeout(&mut handles, dur, biased) 返回 Option<SelectedOperation>，None 即走 default
```

注意「**至多选一个操作真正执行**」的不变量：哪怕有 10 个 `recv` 分支同时就绪，`select!` 也只会**真正完成**其中一个（拿走一条消息），其余分支就像从未发生过。

#### 4.2.3 源码精读

先看宏里**单分支优化**：当 `select!` 里**只有一个 `recv` 分支**时，宏会直接退化成对应的单条 API，根本不走 `Select` 调度——既更快也更清晰。这三条优化规则精确对应上表的三种形态：

[select_macro.rs:558-571](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L558-L571) —— 单 `recv` 且无 `default`：直接编译成 `_r.recv()`（对应「纯阻塞」）。

[select_macro.rs:537-557](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L537-L557) —— 单 `recv` + 立即 `default`：编译成 `_r.try_recv()`，若返回 `TryRecvError::Empty` 则走 default 分支（对应「非阻塞」）。

[select_macro.rs:572-592](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L572-L592) —— 单 `recv` + `default(timeout)`：编译成 `_r.recv_timeout(timeout)`，若返回 `RecvTimeoutError::Timeout` 则走 default 分支（对应「限时」）。

> 这也解释了一个常见困惑：**为什么 `recv(r) -> msg` 里 `msg` 是 `Result<T, RecvError>`？** 因为单分支优化后它就是 `recv()` 的返回值；多分支时则是通过 `SelectedOperation::recv()` 完成，返回类型一致。

当分支数 ≥ 2（或有 `send`）时，走通用路径：构建一个「句柄数组」，再调三个内部函数之一。三种 `default` 形态对应 `@add` 的三条规则：

[select_macro.rs:754-775](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L754-L775) —— 无 `default`：调 `internal::select(&mut _sel, _IS_BIASED)`，阻塞直到有操作就绪。

[select_macro.rs:776-805](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L776-L805) —— 立即 `default`：调 `internal::try_select(...)`，返回 `None` 即执行 default。

[select_macro.rs:806-835](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L806-L835) —— `default(timeout)`：调 `internal::select_timeout(..., $timeout, ...)`。

这三个内部函数的真身在 `src/select.rs`：

[select.rs:456-469](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L456-L469) —— `try_select`：`run_select(handles, Timeout::Now, is_biased)`，立即尝试一次。

[select.rs:474-489](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L474-L489) —— `select`：`run_select(handles, Timeout::Never, is_biased)`，永不超时。

[select.rs:494-503](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L494-L503) —— `select_timeout`：把 `Duration` 换算成 `Instant` 截止时间后委托 `select_deadline`（`Duration` 溢出时退化为永不超时的 `select`，与 u1-l3 的超时换算逻辑一致）。

> 可见 `default` 的三种形态，在底层就是 `Timeout` 枚举的三个值（`Now` / `Never` / `At(deadline)`）。这是一个很漂亮的「**用户语法 → 单一底层原语**」的归一化设计，与 u1-l3 讲过的「底层只有带截止时间一种原语」遥相呼应。

#### 4.2.4 代码实践：两个 Receiver + 1 秒超时

> **目标**：实现本讲规格要求的核心实践——用 `select!` 同时等两个 `Receiver` 并加 1 秒超时。

```rust
// 示例代码
use std::thread;
use std::time::Duration;
use crossbeam_channel::{select, unbounded};

fn main() {
    let (s1, r1) = unbounded();
    let (s2, r2) = unbounded();

    // r1 在 200ms 后发一条；r2 始终不发
    thread::spawn(move || {
        thread::sleep(Duration::from_millis(200));
        s1.send("hello").unwrap();
    });

    select! {
        recv(r1) -> msg => println!("从 r1 收到: {:?}", msg),
        recv(r2) -> msg => println!("从 r2 收到: {:?}", msg),
        default(Duration::from_secs(1)) => println!("1 秒内没有任何消息，超时退出"),
    }
}
```

**操作步骤**：

1. `cargo run`，观察输出。
2. 把 `s1` 那条线程的 `sleep` 改成 `Duration::from_secs(2)`（超过 1 秒），再次 `cargo run`。

**需要观察的现象 / 预期结果**：

- 原版：约 200ms 后打印 `从 r1 收到: Ok("hello")`（`r1` 先于超时就绪）。
- 改后：1 秒后打印 `1 秒内没有任何消息，超时退出`（两个 `recv` 都没就绪，`default(timeout)` 兜底）。

> ⚠️ 时间相关结果受调度影响，**以本地实测为准（待本地验证）**。

#### 4.2.5 小练习与答案

**练习 1**：`default`（不带参数）和 `default(Duration::ZERO)` 行为完全相同吗？

**参考答案**：从「最终是否阻塞」看，二者都是「不阻塞」——立即判断就绪、否则走 default。但实现路径不同：`default` 走 `try_select`（`Timeout::Now`，纯非阻塞）；`default(Duration::ZERO)` 走 `select_timeout`，会换算成一个「现在或已过去」的截止时间。语义上等价，工程上写 `default` 更清晰、也更可能命中单分支优化。

**练习 2**：为什么说「没有 `default` 的 `select!` 一定会执行某个 `recv`/`send` 分支或因断开返回错误」，而不会「什么都不做」？

**参考答案**：因为无 `default` 时调的是 `internal::select`，它返回 `SelectedOperation`（非 `Option`），[select.rs:474-489](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L474-L489) 表明它一定 `unwrap()` 出一个操作——要么某个操作就绪，要么通道断开（断开本身就是一种「就绪」，见 4.4）。

---

### 4.3 公平性：select! 的随机选择与 select_biased!

#### 4.3.1 概念说明

当**多个操作同时就绪**时，`select!` 与 `select_biased!` 的行为不同，这是二者唯一的本质区别：

- **`select!`（无偏 / 公平）**：在所有就绪的操作里**近似均匀随机**地选一个。长期来看每个就绪分支被选中的概率相等，不会出现某个分支被「饿死」。
- **`select_biased!`（有偏 / 优先级）**：总是选**列表里最靠前**的那个就绪分支。你可以利用这一点表达优先级——比如优先处理高优先级队列，只有它空了才处理低优先级队列。

数学上，若 `select!` 当前有 \(k\) 个操作同时就绪，那么每一个被选中的概率都是：

\[
P(\text{选中第 } i \text{ 个就绪操作}) = \frac{1}{k}, \quad i = 1, 2, \ldots, k
\]

而 `select_biased!` 则把全部概率集中在这 \(k\) 个里**最靠前**的一个上。

#### 4.3.2 核心流程

 Arbitration（仲裁）发生在 `run_select` 的「快速路径」里：先把所有句柄按某种顺序排好，再**逐个**尝试 `try_select`，第一个成功的就胜出。因此「顺序」决定了谁更容易赢：

```text
select!        : 进入快速路径前先 utils::shuffle(handles) 随机打乱顺序，再逐个 try_select
select_biased! : 跳过 shuffle，按用户书写的列表顺序逐个 try_select
```

所以「随机」并不是每次抛硬币决定分支，而是「**随机决定尝试顺序**，再按顺序取第一个能成的」——效果上等价于在就绪者里均匀随机选一个。

#### 4.3.3 源码精读

打乱的开关正是 4.1.3 看到的 `_IS_BIASED`。它一路传到 `run_select`：

[select.rs:196-199](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L196-L199) —— `if !is_biased { utils::shuffle(handles); }`：只有 `select!`（`_IS_BIASED == false`）才会在仲裁前洗牌；`select_biased!` 直接跳过，保持源代码顺序。

紧接着的「逐个尝试」循环：

[select.rs:206-211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L206-L211) —— 对（可能已打乱的）句柄逐个调 `handle.try_select(&mut token)`，第一个成功者胜出并返回。这就是「顺序决定胜负」的实现。

而洗牌算法本身（Xorshift RNG + Fisher–Yates）在 `src/utils.rs`，属于 u3-l5 的内容，这里只需知道它是一个不依赖外部 RNG 的、确定性的「随机」排列即可。

> 设计取舍：把「公平 vs 优先级」做成同一个宏、用布尔开关切换，而不是写两套独立实现——既消除了代码重复，又让两种语义的「正确性」由同一份调度核心保证。代价是 `_IS_BIASED` 这个看似神秘的常量（它是宏内部用来把开关一路透传下去的载体）。

#### 4.3.4 代码实践：对比 select! 与 select_biased!

> **目标**：当两个 `recv` 分支**始终同时就绪**时，对比两个宏的选择分布。

```rust
// 示例代码
use crossbeam_channel::{select, select_biased, unbounded};

fn count_unbiased(a: u32) -> u32 {
    let (s1, r1) = unbounded();
    let (s2, r2) = unbounded();
    let mut hit_r1 = 0;
    for _ in 0..a {
        // 每轮都让两个通道各有一条消息 => 两个 recv 始终同时就绪
        s1.send(()).unwrap();
        s2.send(()).unwrap();
        select! {
            recv(r1) -> _ => hit_r1 += 1,
            recv(r2) -> _ => {},
        }
    }
    hit_r1
}

fn count_biased(a: u32) -> u32 {
    let (s1, r1) = unbounded();
    let (s2, r2) = unbounded();
    let mut hit_r1 = 0;
    for _ in 0..a {
        s1.send(()).unwrap();
        s2.send(()).unwrap();
        select_biased! {
            recv(r1) -> _ => hit_r1 += 1,  // 列表最前
            recv(r2) -> _ => {},
        }
    }
    hit_r1
}

fn main() {
    let n = 10_000;
    println!("select!        命中 r1 的次数: {} / {}", count_unbiased(n), n);
    println!("select_biased! 命中 r1 的次数: {} / {}", count_biased(n), n);
}
```

**操作步骤**：`cargo run`（建议release 构建 `cargo run --release` 以减少抖动）。

**需要观察的现象 / 预期结果**：

- `select!`：命中 `r1` 的次数约为 **5000 次**（±少量统计抖动），即近似 50/50。
- `select_biased!`：命中 `r1` 的次数 **恒为 10000**——因为 `r1` 在列表最前且始终就绪，永远赢。

> ⚠️ `select!` 的具体计数是随机的，**每次运行不同（待本地验证）**；`select_biased!` 则是确定的 10000。

#### 4.3.5 小练习与答案

**练习 1**：如果你要实现「优先处理高优先级队列 `hq`，只有它为空时才处理低优先级队列 `lq`」，该用哪个宏？

**参考答案**：用 `select_biased!`，把 `recv(hq)` 写在 `recv(lq)` 之前。当 `hq` 有消息时它一定先被选中；只有 `hq` 不就绪（空）时，`lq` 才有机会被选中。用普通 `select!` 则两者概率相等，无法保证优先级。

**练习 2**：把 `select!` 的随机性描述成「每次抛硬币决定分支」准确吗？

**参考答案**：不准确。源码是「**先随机打乱所有操作的尝试顺序，再按该顺序逐个 `try_select`，取第一个成功的**」——见 [select.rs:196-211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L196-L211)。效果上等价于「在就绪者里均匀随机选一个」，但实现机制是「随机化顺序」而非「对每个分支独立抽样」。

---

### 4.4 send 分支、结果绑定与「就绪」的精确含义

#### 4.4.1 概念说明

`select!` 不只能 `recv`，也能 `send`。`send` 分支的写法是：

```rust
send(s, value) -> res => { ... }
```

它表示「等待 `s` 能够接收 `value` 时，把 `value` 发出去」。绑定变量 `res` 的类型是 `Result<(), SendError<T>>`——与 u2-l3 讲过的 `send()` 返回值一致；当接收端全部断开时，`res` 是 `Err(SendError(value))`，**被拒绝的消息会原样还给你**（通过 `SendError` 的 `into_inner` 取回）。

还有一个极其重要、却容易踩坑的「就绪」定义：

> 一个操作「不需要阻塞就能立刻完成」就算就绪——**即使完成的结果是返回一个错误**（比如通道已断开）。

也就是说：在一个**已断开**的 `Receiver` 上 `recv`，不会让 `select!` 阻塞，而是会让对应的 `recv` 分支**立即就绪**并返回 `Err(RecvError)`。这点在写 `select!` 循环时尤其关键，否则容易写出「断开后疯狂空转」的死循环。

#### 4.4.2 核心流程

`send` 分支在宏里的处理与 `recv` 完全对称：

```text
1. @add 阶段：把 send 分支登记进句柄数组，记录其 index 与 sender_addr
2. 调度核心选中某个分支后，返回带 index 的 SelectedOperation
3. @complete 阶段：按 index 找到对应分支，调用 SelectedOperation::send(s, value) 真正完成发送
   - 成功 => res = Ok(())
   - 接收端已全部断开 => res = Err(SendError(value))，消息原样返还
```

「就绪」的判定由各 flavor 的 `SelectHandle::is_ready` / `try_select` 负责（u3-l2 详述）。对 `recv` 而言，「通道空且已断开」是一种就绪；对 `send` 而言，「接收端全断」也是一种就绪（会立刻失败）。

#### 4.4.3 源码精读

「就绪即返回错误也算就绪」的官方表述：

[select_macro.rs:1004-1005](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L1004-L1005) —— `select!` rustdoc：「An operation is considered to be ready if it doesn't have to block. Note that it is ready even when it will simply return an error because the channel is disconnected.」

`send` 分支的登记与完成，在宏里分别由这两条规则实现：

[select_macro.rs:878-909](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L878-L909) —— `@add` 的 send 规则：把 `s` 借出引用、记下 `sender_addr`、写入句柄数组 `_sel[$i]`，并把「`[$i] send($var, $m)`」追加到待完成列表。

[select_macro.rs:932-952](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L932-L952) —— `@complete` 的 send 规则：若 `oper.index() == $i`，则调 `oper.send($s, $m)` 真正完成发送，把结果绑给 `res`。

而「按 index 分发到正确分支」的机制，是用 `SelectedOperation::index()` 做的一串 `if/else`：

[select_macro.rs:911-931](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L911-L931) —— `@complete` 的 recv 规则：`if oper.index() == $i { oper.recv($r); ... } else { 继续匹配下一个分支 }`。

[select.rs:1248-1250](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1248-L1250) —— `SelectedOperation::index()`：返回被选中操作在句柄数组里的下标。

最后，宏把这些内部细节全部藏在 `internal` 隐藏模块背后，普通用户在文档里看不到：

[lib.rs:368-375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L368-L375) —— `#[doc(hidden)] pub mod internal`，只 `pub use` 了 `SelectHandle / select / try_select / select_timeout / sender_addr / receiver_addr`，专供宏展开后调用。

#### 4.4.4 代码实践：send 分支与「断开即就绪」

> **目标**：体会 `send` 分支的返回类型，以及「断开也算就绪」带来的循环陷阱。

```rust
// 示例代码
use std::time::Duration;
use crossbeam_channel::{select, unbounded};

fn main() {
    let (s, r) = unbounded::<&str>();

    // 用一个零容量通道做 send 分支：只有当对端在 recv 时才能发出去
    select! {
        send(s, "hi") -> res => println!("send 完成: {:?}", res), // Ok(())
        default(Duration::from_millis(100)) => println!("没人收，超时"),
    }

    // 关键演示：drop 掉接收端后，recv 分支会“立即就绪”并返回 Err
    drop(r);
    select! {
        // 注意：这里只是演示“断开即就绪”，所以 send 会立刻失败
        send(s, "again") -> res => println!("第二次 send: {:?}", res), // Err(SendError("again"))
    }
}
```

**操作步骤**：

1. `cargo run`。
2. 思考：如果在 `drop(r)` 之后写一个 `loop { select! { recv(r) -> _ => {}, ... } }`（没有正确处理 `Err`），会发生什么？

**需要观察的现象 / 预期结果**：

- 第一次 `select!`：因为零容量通道对端没有 `recv`，100ms 内 `send` 不就绪，走 `default`，打印 `没人收，超时`。
- 第二次 `select!`：`r` 已 drop，`send` **立即就绪（以失败的方式）**，打印 `第二次 send: Err(SendError("again"))`——被拒绝的消息 `"again"` 完好地留在 `SendError` 里。

> ⚠️ 若把这种「断开即就绪」的分支放进无限 `loop` 且不 `break`，就会**忙等到死**（CPU 占满却不阻塞），这是 `select!` 最常见的误用。**以本地实测为准（待本地验证）**。

#### 4.4.5 小练习与答案

**练习 1**：`send(s, v) -> res => ...` 里的 `res` 是什么类型？发送失败时消息去哪了？

**参考答案**：`res: Result<(), SendError<T>>`。失败（接收端全断）时是 `Err(SendError(v))`，消息 `v` 被 `SendError` 原样携带，可用 `.into_inner()` 取回——与 u2-l3 的发送错误体系一致。

**练习 2**：在一个 `select!` 里同时写 `recv(r)` 和 `send(s, v)`，其中一个就绪、另一个未就绪时，会怎样？

**参考答案**：`select!` 只会执行**就绪的那个**；未就绪的那个就像没写过一样——`v` 不会被发出，`r` 也不会被消费。这正是「至多完成一个操作」的不变量。

---

## 5. 综合实践

> **任务**：写一个「通道合并器（merger）」——把两个输入 `Receiver` 合并成一个逻辑流，**两个输入都断开**后退出；若长时间（1 秒）没有任何事件，则报告 stall 并退出。要求综合用到：多 `recv` 分支、对 `Err`（断开）的处理、`default(timeout)` 兜底。

```rust
// 示例代码
use std::thread;
use std::time::Duration;
use crossbeam_channel::{select, unbounded};

fn main() {
    let (s1, r1) = unbounded();
    let (s2, r2) = unbounded();

    // 两个生产者各发 3 条后随线程结束被 drop（=> 输入断开）
    thread::spawn(move || {
        for i in 0..3 { s1.send(format!("A{i}")).unwrap(); }
    });
    thread::spawn(move || {
        for i in 0..3 { s2.send(format!("B{i}")).unwrap(); }
    });

    let mut merged: Vec<String> = Vec::new();

    loop {
        select! {
            recv(r1) -> msg => match msg {
                Ok(m)  => merged.push(m),
                Err(_) => { while let Ok(m) = r2.try_recv() { merged.push(m); } break; }
            },
            recv(r2) -> msg => match msg {
                Ok(m)  => merged.push(m),
                Err(_) => { while let Ok(m) = r1.try_recv() { merged.push(m); } break; }
            },
            default(Duration::from_secs(1)) => {
                println!("stall: 1 秒内无事件，提前退出");
                break;
            }
        }
    }

    println!("合并到 {} 条事件: {:?}", merged.len(), merged);
}
```

**操作步骤与观察要点**：

1. `cargo run`。正常情况下应合并到约 6 条事件（`A0..A2` + `B0..B2`，顺序因 `select!` 的随机性而**不固定**）后退出。
2. 注释掉两个生产者线程的 `send`（让输入立刻断开），再跑——应立即退出、合并到 0 条。
3. 把 `default` 的超时改小（如 1ms）再跑——由于生产者是另一个线程、存在调度延迟，可能触发 stall 提前退出；体会 `default(timeout)` 与「断开即就绪」竞争时的细微差别。

**这把本讲的知识串起来了**：

- 多个 `recv` 分支同处一块（4.1）；
- `Err(_)` 分支证明「断开即就绪」（4.4）；
- `default(timeout)` 提供限时兜底（4.2）；
- 输出顺序不固定，正是 `select!` 随机仲裁（4.3）的直接证据。

> ⚠️ 合并到的具体顺序、是否触发 stall 受线程调度影响，**以本地实测为准（待本地验证）**。

## 6. 本讲小结

- `select!` 把多个 `recv`/`send` 操作组合在一个块里，等待**任意一个就绪**并执行它，其余操作不会被执行——是「多路版本」的收发。
- 三种分支形态完全对应收发 API 的三种阻塞模式：无 `default`（阻塞）、`default`（非阻塞）、`default(timeout)`（限时）；底层分别调 `internal::select` / `try_select` / `select_timeout`，本质是同一个 `run_select` 配 `Timeout::Never/Now/At`。
- 「就绪」= 不需要阻塞就能完成，**即使结果是返回错误（通道断开）也算就绪**；写 `select!` 循环时务必处理 `Err`，否则会忙转。
- `recv(r) -> msg` 绑定 `Result<T, RecvError>`，`send(s, v) -> res` 绑定 `Result<(), SendError<T>>`（失败时消息原样返还）。
- `select!` 与 `select_biased!` 是**同一套实现**，唯一区别是 `_IS_BIASED` 布尔开关：前者在仲裁前 `utils::shuffle` 随机化顺序（公平），后者保留书写顺序（优先级）。
- 单 `recv` 分支的 `select!` 会被宏**优化**成直接的 `recv()` / `try_recv()` / `recv_timeout()`，不走 `Select` 调度。

## 7. 下一步学习建议

- **接下来学 u2-l10「使用 Select 动态 API」**：当你需要 select 的操作列表在**运行时**才能确定（比如动态数量的 `Receiver`），`select!` 宏就不够用了——这时要用 `Select` 结构体。你会看到 `select!` 宏展开后调用的那些 `internal::select` 等函数，正是 `Select` API 的底层。
- **之后学 u3-l1「select 核心算法 run_select」**：深入 `run_select` 的完整状态机（shuffle → try_select → register → wait_until → unregister → accept），理解 4.3 里「随机化顺序」和 4.2 里「`Timeout` 三态」的真正实现。
- **想搞懂宏本身的展开过程**：学 u3-l3「select! 宏展开机制」，逐条拆解 `crossbeam_channel_internal!` 的 `@list` / `@case` / `@init` / `@add` / `@complete` 规则，并用 `cargo expand` 观察真实展开结果。
- **推荐阅读的真实示例**：[`examples/stopwatch.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/stopwatch.rs) 用 `select!` 在 `tick`（周期心跳）与 `Ctrl+C`（信号通道）之间选择，是本讲「两个 `recv`」模式在真实程序里的落地。

---

> 本讲义覆盖的最小模块：**`select!` / `select_biased!` 宏入口（`src/select_macro.rs`）与 `src/lib.rs` 的 select 示例**——讲解了 `select!` 的基本语法、阻塞/`default`/`default(timeout)` 三种分支形态、随机公平与有偏优先级的区别及其 `_IS_BIASED` + `utils::shuffle` 源码来源，以及 `send` 分支、结果绑定和「就绪即返回错误也算就绪」的精确语义。
