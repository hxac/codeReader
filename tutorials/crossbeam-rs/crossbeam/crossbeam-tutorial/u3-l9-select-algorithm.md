# select 动态选择算法

## 1. 本讲目标

本讲剖析 crossbeam-channel 的**动态选择**（dynamic select）机制，主角是 `crossbeam-channel/src/select.rs`。读完本讲你应当能够：

- 理解 `Select<'a>` 如何在运行期动态构建一份「发送/接收操作列表」，并对任意多个通道端点做多路选择。
- 掌握 `run_select` 的「五阶段选择算法」：`try_select`（乐观尝试）→ `register`（注册阻塞）→ `wait_until`（等待唤醒）→ `unregister`（注销）→ `accept`（确认完成），以及它如何杜绝丢失唤醒（lost wakeup）。
- 区分两种使用模式：`select` 系列（必须完成操作）与 `ready` 系列（只返回就绪索引、不提交）。
- 认识 `Selected` 状态机如何用一个 `AtomicUsize` 编码「等待 / 中止 / 断开 / 命中操作」四种状态，并用一次 CAS 仲裁谁能完成。

本讲承接 u3-l7（`Context` 与 `Waker` 的阻塞唤醒机制），是 u3-l10（`select!` 宏）的运行时基础。

## 2. 前置知识

在进入算法前，先用一句话厘清三个容易混淆的词：

- **就绪（ready）**：一个操作「不需要阻塞就能执行」就算就绪——哪怕执行后只会得到一个「通道已断开」的错误。例如空通道的 `recv` 不是就绪；有消息或已断开的 `recv` 是就绪。
- **选中（selected）**：在一次选择里，某个操作被原子地「预订」下来，预订者必须把它执行完。这是比就绪更强的一步：多个操作可能同时就绪，但只有一个是被选中的。
- **完成（completed）**：把选中的操作真正落地——`recv` 取走消息、`send` 写入消息。

select 要解决的核心难题是：**当一组操作一开始都不就绪时，如何在不丢失唤醒、不忙等的前提下，等到任意一个就绪并原子地选中它？** 这正是 u3-l7 里「先登记进 `Waker`，再复查就绪，靠 CAS 仲裁」那套思想的算法化展开。如果你对 `Context::try_select` / `wait_until` / `Waker::register` 还不熟，建议先回看 u3-l7。

一个直觉类比：select 像在多个收银台前排队——你先快速扫一眼有没有空的（`try_select`）；都没有，就在每个柜台挂一个「叫号器」（`register`），然后坐等（`wait_until`）；任一柜台叫你，你立刻摘掉所有叫号器（`unregister`），去那个柜台结账（`accept` + 完成）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crossbeam-channel/src/select.rs` | 本讲主角：`Select` 构建器、`run_select`/`run_ready` 算法、`SelectHandle` trait、`Selected`/`Operation`/`Token`/`SelectedOperation` 类型 |
| `crossbeam-channel/src/context.rs` | 提供 `Context::with`/`try_select`/`selected`/`wait_until`，是 select 阻塞唤醒的底座（u3-l7 已详述） |
| `crossbeam-channel/src/utils.rs` | `shuffle`（xorshift 公平洗牌）与 `sleep_until` |
| `crossbeam-channel/src/channel.rs` | `Sender`/`Receiver` 的 `SelectHandle` 派发、`addr()` 身份、`channel::read`/`write` 完成操作 |
| `crossbeam-channel/src/flavors/array.rs` | 以 array flavor 为例，看每个 flavor 如何实现 `register`/`accept`/`is_ready` |
| `crossbeam-channel/examples/matching.rs` | 用 `select!` 宏在同一通道上「既发又收」的示例 |
| `crossbeam-channel/examples/stopwatch.rs` | 用 `select!` 宏 + `tick` 做周期定时与退出的示例 |

> 说明：两个 examples 用的是 `select!` 宏（u3-l10 主题），但它们展示了「同时多路等待」的典型用法，本讲会借它们做阅读型实践。

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：先打地基（4.1 操作身份与 `Selected` 状态机），再讲如何构建操作列表（4.2），接着精读核心算法（4.3 五阶段选择），最后对比两种模式（4.4 select vs ready）。

### 4.1 操作身份与 Selected 状态机

#### 4.1.1 概念说明

一次 select 涉及「多个操作」与「一个等待的线程」。算法需要回答两个问题：

1. **如何标识「某个操作」？** —— 用 `Operation`。
2. **如何用最少的内存表达「线程此刻处于什么状态」？** —— 用 `Selected` 状态机，并把它压进一个 `AtomicUsize`。

还有一个关键载体 `Token`：它是「每 flavor 一字段」的临时草稿结构，在选中阶段被命中 flavor 写入，在完成阶段被同一 flavor 读出——从而把「选中」和「完成」解耦成两步。

#### 4.1.2 核心流程

- `Operation` 的唯一性来自「一个线程内、一次操作期间存活的栈变量地址」。`Operation::hook(&mut r)` 把引用地址转成 `usize`，并断言它 `> 2`，以免与状态机的特殊值冲突。
- `Selected` 有四个变体：`Waiting`（还在等）/ `Aborted`（放弃阻塞等待）/ `Disconnected`（因通道断开而就绪）/ `Operation(Operation)`（某个操作被选中）。它们与 `usize` 双向转换：`0/1/2` 分别对应前三个，`≥3` 就是操作地址。
- 整个状态机只允许**一次**「从 `Waiting` 转出」的成功 CAS——这就是「谁能完成」的唯一仲裁点。
- `Token` 默认值是一个全空的草稿；命中的 flavor 只填自己的那一个字段。

状态编码可写成：

\[
\text{usize} = \begin{cases}
0 & \text{Waiting} \\
1 & \text{Aborted} \\
2 & \text{Disconnected} \\
\text{addr} \;(\geq 3) & \text{Operation}(\text{addr})
\end{cases}
\]

#### 4.1.3 源码精读

`Operation` 由栈地址派生身份，`assert!(val > 2)` 是为了躲开状态机的 `0/1/2`：

[crossbeam-channel/src/select.rs:36-52](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L36-L52) —— 把可变引用的地址转成操作 id，断言它大于 2。

`Selected` 枚举与 `usize` 的双向转换（注意 `Operation(val)` 直接用地址值，故必须 `>2`）：

[crossbeam-channel/src/select.rs:56-92](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L56-L92) —— 四态枚举与 `usize` 的互转，是状态机能压进单个原子字的关键。

`Token` 是「每 flavor 一字段」的胖结构体，命中的 flavor 写自己的字段：

[crossbeam-channel/src/select.rs:24-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L24-L32) —— 临时草稿，承载一次操作在「选中」与「完成」之间传递的 flavor 专属信息（如 array 的 slot/stamp）。

`SelectHandle` trait 是 select 与各 flavor 之间的统一契约，八个方法分两组：

[crossbeam-channel/src/select.rs:99-123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L99-L123) —— `try_select/register/unregister/accept` 服务「提交型」select；`is_ready/watch/unwatch` 服务「就绪查询型」ready；`deadline` 用于阻塞时限。

`Inner` 就是这个状态机的物理载体——一个 `AtomicUsize` 加一个跨线程交接消息的 `packet` 指针槽：

[crossbeam-channel/src/context.rs:27-39](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L27-L39) —— `select: AtomicUsize` 编码 `Selected` 状态，`packet` 用于 zero flavor 等场景的跨线程数据交接。

而「唯一一次 CAS」就在 `Context::try_select` 里：

[crossbeam-channel/src/context.rs:98-109](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L98-L109) —— 从 `Waiting` CAS 到目标值，成功用 `AcqRel`、失败用 `Acquire`，是多线程抢「选中权」的唯一入口。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：亲眼看到 `Operation` 地址为何必须 `>2`。
2. **步骤**：打开 `select.rs:45-51`，读 `Operation::hook`。再对照 `select.rs:70-80` 的 `From<usize>`，确认 `0/1/2` 已被 `Waiting/Aborted/Disconnected` 占用。
3. **现象与预期**：若把断言改成 `assert!(val > 0)`，一个恰好地址为 `1` 的栈变量会被误判成 `Aborted`，破坏状态机。你不需要真的改源码，推理即可。

#### 4.1.5 小练习与答案

- **练习**：为什么 `Selected` 要能和 `usize` 互转，而不能只是一个普通枚举？
  - **答**：因为它要被存进 `Inner.select: AtomicUsize`，供多线程用 CAS 原子读写。`usize` 编码让「比较并交换」可以直接在一个机器字上完成，无需加锁。
- **练习**：`Operation::hook` 为什么用「栈变量地址」而非一个全局自增计数器当 id？
  - **答**：栈地址天然「每线程、每操作唯一」且零分配、无线程间共享，免去了全局计数器的同步开销；只要该变量在整次 select 期间存活即可。

---

### 4.2 Select：动态操作列表的构建

#### 4.2.1 概念说明

`Select<'a>` 是一个**构建器**：你在运行期往里 `send(&s)` / `recv(&r)` 任意多个端点，它收集成一份列表，之后调用 `select()` / `ready()` 等方法在这份列表上做选择。它的关键能力是「**列表长度运行期才确定**」——这是 `select!` 宏做不到的（宏的分支数量在编译期固定）。文档原话强调：宏「cannot select over a dynamically created list of channel operations」。

#### 4.2.2 核心流程

- `Select::new()` 创建空列表（预分配容量 4）；`new_biased()` 创建「带偏置」的列表。
- `send(&s)` / `recv(&r)` 把端点以三元组 `(handle, index, addr)` 推入 `handles`，`index` 单调递增、删除后不复用；返回该 index。
- `remove(index)` 用 `swap_remove` 摘除某操作——常用于「某路因断开被选中、想换一路重试」。
- 选择方法（`select` / `try_select` / `select_timeout` / `ready` / …）都只是把 `handles` 交给底层 `run_select` / `run_ready`。
- **公平性**：除非 `biased`，否则每次进入算法都会 `shuffle(handles)` 打乱顺序，使「多个操作同时就绪」时随机选一个，避免饿死靠后的操作。

#### 4.2.3 源码精读

`Select` 的字段就三样：操作列表、下一个 index、是否偏置：

[crossbeam-channel/src/select.rs:616-625](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L616-L625) —— `handles: Vec<(&'a dyn SelectHandle, usize, usize)>`，三元组分别是「trait 对象引用、index、端点地址」。

`recv` 如何登记一个端点（`send` 对称）：

[crossbeam-channel/src/select.rs:708-714](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L708-L714) —— 取下一个 index、记录端点 `addr()`、推入列表、index 自增并返回。

`remove` 用 `swap_remove` 保证 O(1)，且不复用 index：

[crossbeam-channel/src/select.rs:752-769](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L752-L769) —— 按 index 定位再 `swap_remove`；注释明确「removed indices will not be reused」。

`biased` 字段决定了 `run_select` 是否调用 `shuffle`：

[crossbeam-channel/src/select.rs:196-199](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L196-L199) —— 非偏置时先 `utils::shuffle(handles)` 做公平洗牌。

洗牌本身用一个线程局部的 32 位 xorshift RNG，避免 `rand` 依赖：

[crossbeam-channel/src/utils.rs:7-40](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/utils.rs#L7-L40) —— Fisher–Yates 洗牌 + Lemire 快速取模，保证多就绪操作被均匀选中。

`select()` 公共方法只是把 `Timeout::Never` 传给 `run_select`：

[crossbeam-channel/src/select.rs:860-862](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L860-L862) —— `Select::select` → 底层 `select(&mut handles, biased)`。

#### 4.2.4 代码实践

1. **目标**：体会「运行期任意长度列表」这一动态能力。
2. **步骤**（示例代码）：

```rust
// 示例代码：动态收集 N 个 receiver 做选择
use crossbeam_channel::{unbounded, Select};

let senders_receivers: Vec<_> = (0..5).map(|_| unbounded::<i32>()).collect();
let rs: Vec<_> = senders_receivers.iter().map(|(_, r)| r.clone()).collect();

// 列表长度在运行期才确定——这是 select! 宏做不到的
let mut sel = Select::new();
for r in &rs {
    sel.recv(r);
}

// 给第 2 路发一条消息
senders_receivers[2].0.send(42).unwrap();

let oper = sel.select();
let i = oper.index();
let msg = oper.recv(&rs[i]).unwrap();
assert_eq!(msg, 42);
```

3. **现象**：无论如何洗牌，被选中的必然是第 2 路（只有它就绪）。
4. **预期**：打印/断言 `msg == 42`。
5. **运行**：可放入一个临时二进制 crate 的 `main` 里 `cargo run`（待本地验证）。

#### 4.2.5 小练习与答案

- **练习**：`Select` 被标记为 `unsafe impl Send/Sync`（select.rs:627-628），但它内部存的是 `&'a dyn SelectHandle`。为何需要手写这两个 impl？
  - **答**：`&'a dyn SelectHandle` 的 `Send/Sync` 取决于具体 flavor，trait 对象的 `Send/Sync` 不会自动传递到容器；作者通过手写 impl 断言「所有可加入 select 的端点都是线程安全共享的」，使 `Select` 能跨线程使用。
- **练习**：为什么删除操作后 index 不复用？
  - **答**：调用方可能持有旧的 index 变量；不复用可避免「旧的 index 突然指向另一个操作」造成的歧义与误用。

---

### 4.3 run_select：五阶段选择算法

#### 4.3.1 概念说明

`run_select` 是整个 select 的心脏。它要同时满足三个苛刻条件：① 不忙等（没就绪就真正阻塞）；② 不丢失唤醒（阻塞期间别人让操作就绪时一定能叫醒我）；③ 原子选中（多个线程同时争一个就绪操作时，恰有一个成功）。做法是把一次「等待—选中」拆成五个阶段，并把阻塞前后用「先登记、后复查、CAS 仲裁」三重保险缝起来（这正是 u3-l7 思想的落地）。

#### 4.3.2 核心流程（五阶段）

下面是 `run_select` 主循环的伪代码（`handles` 已洗牌）：

```
阶段① start/try（乐观尝试，不阻塞）：
    for handle in handles:
        if handle.try_select(token):        # 命中则直接返回
            return token, index, addr

阶段② ~ ⑤ 在一个 loop 里，每次迭代复用线程局部 Context：
    Context::with(|cx| {
        if 超时模式 == Now: cx.try_select(Aborted)        # 表示「我不打算真等」

      ② register（注册阻塞）：
        for handle in handles:
            ready = handle.register(hook(handle), cx)     # 进 Waker 队列
            if ready:                                     # 登记中发现刚就绪
                if cx.try_select(Aborted) 成功:
                    记下 index_ready; break              # 自己放弃等待
                else: 拿到别人替我选中的状态; break
            if cx.selected() != Waiting: break           # 别人已替我选中

      ③ decide（等待唤醒）：
        if 仍是 Waiting:
            deadline = min(外部超时, 各 handle.deadline())
            sel = cx.wait_until(deadline)                 # park 线程，直到被唤醒/超时

      ④ unregister（注销）：
        for handle in handles[:registered_count]:
            handle.unregister(hook(handle))               # 从 Waker 队列摘除

      ⑤ accept（确认完成）：
        match sel:
          Aborted 且有 index_ready -> 对该 handle 再 try_select，成功则返回
          Operation(addr)        -> 找到匹配 handle，handle.accept(token, cx)，成功则返回
          Disconnected           -> 返回 None（外层会重试）
    })

    若上面返回 Some -> 直接返回
    否则再跑一遍阶段① try_select（防止伪唤醒/竞态漏检）
    再按超时策略决定是否继续 loop
```

五个阶段的对应关系：

| 阶段 | 调用 | 作用 |
| --- | --- | --- |
| ① start | `handle.try_select(token)` | 乐观非阻塞尝试，命中即走快路径 |
| ② register | `handle.register(oper, cx)` | 把本线程登记进各 flavor 的 `Waker`，顺带复查就绪 |
| ③ decide | `cx.wait_until(deadline)` | 真正 `park` 阻塞，直到被 `notify`/超时/断开唤醒 |
| ④ unregister | `handle.unregister(oper)` | 醒来后从所有 `Waker` 摘除自己 |
| ⑤ accept | `handle.accept(token, cx)` | 确认并锁定被选中的操作，填好 `token` 供完成使用 |

#### 4.3.3 源码精读

**阶段① 快路径**：进算法先全量 `try_select`，命中即返回，全程不阻塞、不登记：

[crossbeam-channel/src/select.rs:206-211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L206-L211) —— 乐观遍历所有操作，任一 `try_select` 成功就带着 `token` 返回。

**阶段② register**：逐个登记，并在登记时复查就绪。注意 `register` 返回 `true` 表示「登记时发现已经就绪」——此时用 `cx.try_select(Aborted)` 放弃等待并记下 `index_ready`，留到阶段⑤自己 `try_select` 它：

[crossbeam-channel/src/select.rs:224-246](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L224-L246) —— `register` 返回 true 即「就绪」，随即 `try_select(Aborted)` 抢占；同时每次登记后都读 `cx.selected()`，若别的线程已替我选中则提前 break。

**阶段③ decide**：合并「外部超时」与「各 flavor 自报的 deadline（如 `at`/`tick`）」取最小值，再 `wait_until` 阻塞：

[crossbeam-channel/src/select.rs:248-264](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L248-L264) —— 计算最早 deadline 后 `cx.wait_until(deadline)` 真正 park，直到被唤醒。

`wait_until` 本身是个「load 状态 → 若仍 Waiting 则 park」的循环，天然容忍伪唤醒：

[crossbeam-channel/src/context.rs:144-169](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L144-L169) —— 到点则自己 `try_select(Aborted)`，否则 `park`/`park_timeout`。

**阶段④ unregister**：只注销真正登记过的前 `registered_count` 个：

[crossbeam-channel/src/select.rs:266-269](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L266-L269) —— 醒来后用 `take(registered_count)` 精确摘除，避免误注销未登记项。

**阶段⑤ accept**：根据 `sel` 分派。`Aborted`+`index_ready` 时自己 `try_select`；`Operation(_)` 时找到匹配的 handle 调 `accept`：

[crossbeam-channel/src/select.rs:271-297](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L271-L297) —— `accept` 成功才返回 `Some`，把「选中」最终落定为「已确认」。

**循环收尾的重试**：闭包返回 `None` 时，再跑一遍阶段① `try_select`，并按超时决定是否继续。这一步是防「伪唤醒或注销竞态导致漏检」的兜底：

[crossbeam-channel/src/select.rs:302-323](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L302-L323) —— 重试快路径后，按 `Timeout::{Now,Never,At}` 决定返回 `None` 还是继续 loop。

**为什么不会丢失唤醒？** 关键在「先 register 进 Waker，再查就绪，最后才 park」的顺序：登记之后，任何让该操作就绪的对端线程都会在 `Waker` 里看到我并 `notify`；即便它在我「复查就绪」与「park」之间动手，CAS（`try_select`）和 `park` 的令牌语义（u2-l5）也能保证唤醒不丢。这正是 u3-l7 三重保险在算法层的体现。

**每个 flavor 如何实现 register/accept**：以 array flavor 的接收端为例——`register` 先把自己塞进 `receivers` 这个 `Waker`，再返回 `is_ready()`（即「非空或已断开」）；`accept` 直接复用 `try_select`：

[crossbeam-channel/src/flavors/array.rs:622-637](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L622-L637) —— `register` = 入队 + 查就绪；`accept` = 再试一次 `start_recv`；`is_ready` = 有消息或已断开。

#### 4.3.4 代码实践

1. **目标**：用动态 `Select` 在两个 unbounded 通道间选择，打印先到的消息；再加一路 `after(...)` 做超时。
2. **步骤**（示例代码）：

```rust
// 示例代码：两路动态选择 + 超时第三路
use crossbeam_channel::{unbounded, after, Select};
use std::time::Duration;

let (s1, r1) = unbounded::<&str>();
let (_s2, r2) = unbounded::<&str>();      // 这一路永远不会来消息

let to = after(Duration::from_millis(100));

let mut sel = Select::new();
let i1 = sel.recv(&r1);
let i2 = sel.recv(&r2);
let it = sel.recv(&to);

s1.send("hello").unwrap();                // 第一路就绪

let oper = sel.select();
match oper.index() {
    x if x == i1 => println!("r1: {}", oper.recv(&r1).unwrap()),
    x if x == i2 => println!("r2: {}", oper.recv(&r2).unwrap()),
    x if x == it => { oper.recv(&to).unwrap(); println!("timeout"); }
    _ => unreachable!(),
}
```

3. **对照 select.rs 说明 register 与 accept**：
   - 进入 `run_select`，阶段① 对 `r1/r2/to` 各调一次 `try_select`——`r1` 非空，命中并填好 `token.array`，直接返回，**根本不会走到 register/accept**。这就是快路径。
   - 若把 `s1.send` 注释掉，则阶段① 全失败 → 阶段② 对三路各调 `register`（`r1/r2` 进各自 `Waker`，`to` 进 at flavor 的等待结构），三路都未就绪 → 阶段③ `wait_until(min(各 deadline))`，其中 `to` 的 deadline 最近（100ms）→ 到点 `try_select(Aborted)` 唤醒 → 阶段④ 注销 → 阶段⑤ `Aborted` 但无 `index_ready`，返回 `None` → 外层重试 `try_select`，此时 `to` 已到点就绪，`try_select` 命中 → 选中 `it`。
4. **预期**：原样运行打印 `r1: hello`；注释掉 `s1.send` 后打印 `timeout`（待本地验证时序）。

#### 4.3.5 小练习与答案

- **练习**：阶段② 里若 `register` 返回 `true`（就绪），代码为什么用 `try_select(Aborted)` 而不是直接 `try_select(Operation(...))`？
  - **答**：`Aborted` 在这里是「本线程主动放弃等待」的信号，配合 `index_ready` 在阶段⑤ 由自己再 `try_select` 那个就绪操作来收尾；这种「先标记放弃、再自己抢」的写法避开了在 register 临界区内直接完成操作的复杂性，也和「超时放弃」复用同一个 `Aborted` 语义。
- **练习**：为什么循环收尾（select.rs:308-312）还要再 try_select 一遍？
  - **答**：`wait_until` 可能在状态仍为 `Waiting` 时返回（伪唤醒），或注销期间另一线程刚好让某操作就绪；重试快路径能及时抓住这些「漏网」的就绪，避免无谓地再阻塞一轮。

---

### 4.4 select 与 ready：两种模式对比

#### 4.4.1 概念说明

`Select` 提供两组语义不同的方法，对应两套底层算法：

- **select 系列**（`select`/`try_select`/`select_timeout`/`select_deadline`，底层 `run_select`）：返回 `SelectedOperation`，**操作已被原子预订，必须完成**。底层走 `try_select`/`register`/`accept`。
- **ready 系列**（`ready`/`try_ready`/`ready_timeout`/`ready_deadline`，底层 `run_ready`）：只返回**就绪索引**，**不预订**。你拿到索引后自己调 `try_recv`/`try_send`，并应放在重试循环里——因为别的线程可能抢先消费，且文档警告它「may return with success spuriously」（可能伪就绪）。底层走 `is_ready`/`watch`/`unwatch`。

一句话区别：**select 替你把操作「锁死」并交出完成句柄；ready 只敲门告诉你「这路可能有戏」，干不干还得你自己来。**

#### 4.4.2 核心流程

`run_ready` 与 `run_select` 形似但关键不同：

1. 先用 `Backoff::snooze()` 自旋反复查 `handle.is_ready()`（无锁快查），退避满了才进入阻塞分支。
2. 阻塞分支用 `watch`（而非 `register`）登记「就绪通知」，用 `unwatch` 注销，命中后只返回 `index`，**不调用 `accept`、不带 `token`**。
3. 因不预订，调用方拿到索引后必须自己用 `try_*` 重试，且要容忍 `is_empty()`（已被别人抢走）。

#### 4.4.3 源码精读

`run_ready` 的自旋快查段——纯查 `is_ready`，不登记、不预订：

[crossbeam-channel/src/select.rs:352-367](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L352-L367) —— 用 `Backoff` 自旋查就绪，退避满才转去阻塞分支。

`run_ready` 的阻塞分支用 `watch` 登记就绪通知，命中只返回 `index`（对比 `run_select` 会 `accept` 并带 `token`）：

[crossbeam-channel/src/select.rs:381-444](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L381-L444) —— `watch`/`unwatch` 与 `Operation` 选中后仅 `return Some(*i)`，没有任何 `accept` 或 `token`。

文档用两段等价示例直观对比两种模式——`select` 版一步到位 `oper.recv`，`ready` 版必须 `loop` + `try_recv` + 重试：

[crossbeam-channel/src/select.rs:561-608](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L561-L608) —— 上半段 `sel.select()` 后直接 `oper.recv`；下半段 `sel.ready()` 后 `loop { try_recv(); if empty { continue } }`，正是两种语义的对照。

`SelectedOperation` 的「必须完成」纪律由 `Drop` 强制——未完成就 panic：

[crossbeam-channel/src/select.rs:1327-1331](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1327-L1331) —— `#[must_use]` 加 `Drop` 双保险，防止「选中了却不完成」导致死锁或泄漏。

完成时 `SelectedOperation::recv` 用 `addr` 校验「确实是这一路被选中」，再调 `channel::read`，最后 `mem::forget(self)` 避免 Drop panic：

[crossbeam-channel/src/select.rs:1310-1318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1310-L1318) —— `addr` 校验 + `channel::read(token)` 完成读 + `forget`。

`channel::read` 按 flavor 派发，用 select 阶段填好的 `token` 把消息真正取走（完成阶段）：

[crossbeam-channel/src/channel.rs:1550-1565](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1550-L1565) —— 完成阶段：每个 flavor 用自己 token 字段收尾。

`addr()` 返回共享 `Counter` 指针地址，作为端点稳定身份（同一通道的多个 clone 端点 `addr` 相同，不同通道不同）：

[crossbeam-channel/src/counter.rs:138-141](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L138-L141) —— `counter.as_ptr() as usize`，是 `SelectedOperation` 完成校验的依据。

#### 4.4.4 代码实践（阅读 + 推理）

1. **目标**：用一个场景同时体会「select 预订」与「ready 不预订」的差异。
2. **步骤**：
   - 阅读 `select.rs:561-608` 两段示例，确认 `ready` 版多了 `loop` 与 `is_empty` 重试。
   - 思考：若把 `ready` 版的 `loop` 去掉、直接 `rs[index].try_recv()`，当两个消费者同时 `ready()` 命中同一路时会发生什么？
3. **现象与预期**：去掉重试后，其中一个消费者会 `try_recv` 到 `Empty`（消息被另一个抢走），必须重试；这正是 ready「不预订、需自重试」的体现。
4. **结论**：当你要把 select 结果交给一个「可能失败、想自己决定是否继续」的流程时用 `ready`；当你只想「挑一路、一口气做完」时用 `select`。

#### 4.4.5 小练习与答案

- **练习**：`ready` 系列为何可能「伪就绪」（spuriously）返回成功？
  - **答**：它走的是 `watch`/`is_ready` 通知路径，只表示「这路可能就绪」，并未像 `accept` 那样原子地把消息预订下来；在你拿到索引到真正 `try_recv` 之间，别的线程可能已把消息消费掉，故需重试复核。
- **练习**：`SelectedOperation::Drop` 为何要 panic？
  - **答**：select 已经把操作原子预订（如 array 占了槽、zero 配了对），若不完成就丢弃，会留下未释放的占位，导致同通道其它操作永久阻塞或死锁；panic 是把「必须完成」这条不变量强制化的工程手段。

---

## 5. 综合实践：动态扇入（fan-in）消息收集器

把本讲四个模块串起来：用**动态 `Select`**（4.2）从**运行期才确定数量**的一组 receiver 里收集消息，体现 `SelectedOperation` **必须完成**（4.4）的语义，并对照五阶段算法（4.3）理解一次成功选中走的是哪条路径。

**任务**：有 N 个生产者线程，各自往自己的通道发若干消息后退出；主线程用 `Select` 同时监听这 N 个 receiver，收到任一条就打印，直到所有通道都断开（收到 `RecvError`）。

```rust
// 示例代码：动态扇入收集器
use crossbeam_channel::{unbounded, RecvError, Select};
use std::thread;

fn main() {
    let n = 4;
    // 运行期决定通道数量——这正是动态 Select 的用武之地
    let chans: Vec<_> = (0..n).map(|i| {
        let (s, r) = unbounded();
        thread::spawn(move || {
            for k in 0..3 {
                s.send(format!("p{i}-{k}")).unwrap();
            }
            // s 在此 drop，通道断开
        });
        r
    })
    .collect();

    // 用一个 bool 标记每一路是否已断开，断开则 remove
    let mut sel = Select::new();
    let mut alive: Vec<bool> = vec![true; n];
    let indexes: Vec<_> = chans.iter().map(|r| sel.recv(r)).collect();

    let mut open = n;
    while open > 0 {
        let oper = sel.select();                 // 阶段①~⑤：选中一路
        let i = oper.index();
        let idx = indexes.iter().position(|&x| x == i).unwrap();
        match oper.recv(&chans[idx]) {           // 必须 complete，否则 panic
            Ok(msg) => println!("got {msg}"),
            Err(RecvError) => {                  // 这一路断开
                alive[idx] = false;
                sel.remove(i);                    // 从列表摘除，换一路重试
                open -= 1;
            }
        }
    }
    println!("all channels closed");
}
```

**对照算法的观察点**：

- 大多数命中走**阶段①快路径**（`try_select` 直接命中非空通道），根本不阻塞、不 register。
- 当所有通道都暂时为空时，才会进入**阶段②register**（登记进各 `Waker`）→ **阶段③wait_until**（park）→ 任一生产者发消息时 `notify` 唤醒 → **④unregister** → **⑤accept**。
- 断开的通道在 `try_select` 里会被当作「就绪」（`is_ready` 含 `is_disconnected`），`recv` 返回 `Err(RecvError)`，于是我们 `remove` 它——这正是 `Select::remove` 的设计用途（select.rs:716-724 注释）。
- `oper.recv(...)` 是**必须**的：忘了调用会让 `SelectedOperation` 的 `Drop` panic（4.4）。

**预期**：打印出全部 12 条消息（顺序不确定，因 `shuffle` 公平洗牌），最后打印 `all channels closed`。可 `cargo run` 验证（待本地验证）。

> 进阶思考：把 `sel.select()` + `oper.recv` 换成 `sel.ready()` + `loop { try_recv() }`，代码会复杂多少？这正是 select 系列替你省下的「预订 + 重试」负担。

## 6. 本讲小结

- `Selected` 用一个 `AtomicUsize` 编码 `Waiting/Aborted/Disconnected/Operation` 四态，`Context::try_select` 的一次 CAS 是「谁能完成」的唯一仲裁点；`Operation::hook` 用栈地址作操作身份（须 `>2`），`Token` 是「每 flavor 一字段」的完成草稿。
- `Select<'a>` 是运行期可变长度的操作列表构建器，`send`/`recv`/`remove` 维护 `(handle, index, addr)` 三元组；非偏置时每次 `shuffle` 保证多就绪时随机公平选中。
- `run_select` 的五阶段——①`try_select` 乐观尝试、②`register` 登记+复查、③`wait_until` 阻塞、④`unregister` 注销、⑤`accept` 确认——以「先登记、后复查、CAS 仲裁」三重保险杜绝丢失唤醒，是 u3-l7 思想的算法化。
- 两套模式：`select` 系列（`run_select`）原子预订并交出 `SelectedOperation`，**必须完成**（Drop 强制）；`ready` 系列（`run_ready`）只返回就绪索引、不预订、可能伪就绪，需调用方自己 `try_*` 重试。
- `SelectedOperation::send/recv` 用 `addr()` 校验端点身份，调 `channel::write/read` 用 token 完成操作，`mem::forget` 规避 Drop panic。

## 7. 下一步学习建议

- 下一讲 **u3-l10 `select!` 宏**：本讲的 `run_select`/`try_select`/`SelectedOperation` 都是「私有 API，供 select 宏使用」（见 select.rs 多处注释）。宏在编译期把 `recv(r) -> msg => { .. }` 这种声明式语法展开成对 `internal::select`/`try_select` 的调用，建议带着「宏展开后如何落到本讲的五阶段」这一视角去读 `select_macro.rs`。
- 可用 `cargo expand`（需 nightly + `cargo-expand`）查看 `examples/matching.rs`、`examples/stopwatch.rs` 中 `select!` 的展开结果，对照本讲的 `Select::select` 与 `SelectedOperation::recv`。
- 想再深入正确性保证，可结合 u7-l3（loom/miri/tsan）阅读 select 相关测试，体会这种五阶段阻塞算法如何在模型检查下被验证无丢失唤醒、无死锁。
