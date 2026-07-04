# 通道架构与 flavors 模型总览

> 阶段：进阶层（intermediate）　|　依赖：u1-l4 克隆、共享、断开与迭代

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说出 crossbeam-channel 一共有哪 **六种 flavor（通道风味）**，以及它们各自解决什么问题。
2. 在源码层面画出「构造函数 → flavor → 内部实现」的分流表。
3. 解释 `Sender<T>` / `Receiver<T>` 为什么是「**公共类型壳**」，以及它们如何通过 `SenderFlavor` / `ReceiverFlavor` 枚举把方法调用**按 flavor 分发**到底层实现。
4. 看懂 `src/lib.rs` 的模块声明、`internal` 隐藏模块，以及末尾 `pub use` 的 re-export 组织方式。

本讲是整个进阶层的「**地图**」：不深入任何一种 flavor 的内部数据结构（那是 u2-l5 ~ u2-l8 的事），而是先建立全局架构认知。理解了这套「壳 + 多 flavor」的设计，后面读任何一段实现都会有方向感。

## 2. 前置知识

本讲假设你已经掌握（u1 层内容）：

- **通道（channel）** 的基本概念：`Sender` 发送、`Receiver` 接收。
- `unbounded()` 与 `bounded(cap)` 的用法，以及 **会合（rendezvous）**、**背压（backpressure）** 的含义。
- `Sender` / `Receiver` 的克隆是「共享同一通道」而非复制消息流。
- 三种阻塞模式（非阻塞 / 阻塞 / 超时）与对应错误类型。

本讲会引入几个新术语：

- **flavor（风味）**：crossbeam-channel 把「不同结构的通道」抽象成不同的 flavor，每一种 flavor 就是一种具体实现策略。
- **公共类型壳**：对外只暴露一个泛型 `Sender<T>` / `Receiver<T>`，内部用一个枚举字段持有「真正的底层通道」。用户看到的统一 API，内部其实是 `match` 后转发。
- **分发（dispatch）**：壳层方法根据当前持有的 flavor 变体，调用对应实现的同名方法。
- **`#[doc(hidden)]`**：Rust 属性，标记该项「真实存在、可被调用，但不出现在文档里」。本讲会看到 `internal` 模块用它给 `select!` 宏开后门。

## 3. 本讲源码地图

本讲只涉及三个文件，它们构成了 crossbeam-channel 的「骨架」：

| 文件 | 角色 |
|------|------|
| `src/lib.rs` | crate 入口。声明 `#![no_std]`、用 `cfg(feature = "std")` 门控所有模块、定义 `internal` 隐藏模块、末尾 `pub use` 集中对外导出全部公共 API。 |
| `src/flavors/mod.rs` | flavor 子系统入口。声明六种 flavor 子模块，是一份「风味清单」。 |
| `src/channel.rs` | 对外类型壳的定义。包含所有构造函数（`unbounded`/`bounded`/`after`/`at`/`tick`/`never`）、`Sender<T>` / `Receiver<T>` 及其全部公共方法、`SenderFlavor` / `ReceiverFlavor` 枚举、以及 `SelectHandle` 的分发实现。 |

> 进阶层后续讲义会逐个打开 `flavors/` 目录下的 `array.rs`、`list.rs`、`zero.rs`、`at.rs`、`tick.rs`、`never.rs`；本讲只读它们的「目录头」`flavors/mod.rs`。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 六种 flavor 总览与构造函数的分流**
- **4.2 公共类型壳：SenderFlavor / ReceiverFlavor 枚举与「按 flavor 分发」母题**
- **4.3 lib.rs 的模块组织、internal 隐藏模块与 re-export**

### 4.1 六种 flavor 总览与构造函数的分流

#### 4.1.1 概念说明

crossbeam-channel 并不是「一个通道实现」，而是「**一套统一 API + 六种可替换实现**」。不同的使用场景对通道结构的要求不同：

- 需要固定容量、低延迟 → 预分配数组（**array**）。
- 容量不限、能一直塞 → 链表（**list**）。
- 不存消息、要求收发同时在场（会合）→ 零容量（**zero**）。
- 只需要在「某个时间点」收到一条消息 → 定时器通道（**at**）。
- 需要周期性「心跳」→ 周期通道（**tick**）。
- 需要「永不投递」的占位接收端（常用于 `select!` 的条件分支）→ **never**。

这些就是六种 flavor。`flavors/mod.rs` 把它们列成一份清单，明确说明「本 crate 一共有六种风味」。

#### 4.1.2 核心流程

构造函数的分流逻辑可以用下面这张流程图理解：

```
用户调用构造函数
        │
        ├─ unbounded()            ──► flavors::list::Channel    （无界）
        ├─ bounded(cap)
        │     ├─ cap == 0         ──► flavors::zero::Channel    （会合）
        │     └─ cap > 0          ──► flavors::array::Channel   （有界数组）
        ├─ after(dur) / at(when)  ──► flavors::at::Channel      （定时）
        ├─ tick(dur)              ──► flavors::tick::Channel    （周期）
        └─ never()                ──► flavors::never::Channel   （永不投递）
```

注意三点：

1. **同一个 `bounded()` 会按容量分流到两种 flavor**：`cap == 0` 走 `zero`，`cap > 0` 走 `array`。
2. **`after` 和 `at` 共用同一种 flavor（`at`）**：`after(dur)` 其实就是先算出截止时刻 `Instant::now() + dur`，再等价于一次 `at(deadline)`。
3. **只有 `unbounded` / `bounded` 会同时返回 `Sender` 和 `Receiver`**；`after` / `at` / `tick` / `never` 只返回一个 `Receiver`——因为这些特殊通道「没有发送方」，消息是 `recv` 时**惰性生成**的。

#### 4.1.3 源码精读

先看「风味清单」本身。`flavors/mod.rs` 用注释说明了六种 flavor，并声明对应的子模块：

六种风味清单与子模块声明：
[flavors/mod.rs:1-17](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/mod.rs#L1-L17) —— 这段注释 + 六个 `pub(crate) mod` 就是整个 flavor 子系统的目录。

再看分流逻辑。`unbounded()` 直接构造 `list` flavor：
[channel.rs:50-59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L50-L59) —— `counter::new(flavors::list::Channel::new())` 把底层通道包进引用计数，再塞进 `SenderFlavor::List` / `ReceiverFlavor::List`。

`bounded()` 按 `cap == 0` 分流到 `zero` 或 `array`：
[channel.rs:113-133](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L113-L133) —— 注意 `cap == 0` 分支用 `flavors::zero::Channel::new()`，`else` 分支用 `flavors::array::Channel::with_capacity(cap)`。

特殊通道里，`after()` 会先尝试把 `Duration` 换算成 `Instant` 截止时刻，**溢出时退化为 `never()`**：
[channel.rs:181-188](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L181-L188) —— `checked_add` 失败返回 `never()`，这是「时间太远导致永不触发」的优雅退化。

`at()` 与 `tick()` 同样构造 `At` / `Tick` flavor，并用 `Arc` 包裹（不需要引用计数，因为它们永不 disconnect）：
[channel.rs:232-236](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L232-L236)（`at`）
[channel.rs:335-345](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L335-L345)（`tick`）

`never()` 是一个 `const fn`，构造零大小的 `Never` flavor：
[channel.rs:275-279](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L275-L279) —— 它能在 `const` 上下文中使用，例如作为静态默认值。

#### 4.1.4 代码实践

**实践目标**：亲手整理「构造函数 → flavor」对应表，巩固分流认知。

**操作步骤**：

1. 打开 `src/channel.rs`，定位六个构造函数（`unbounded` / `bounded` / `after` / `at` / `tick` / `never`）。
2. 对每个构造函数，记录：返回类型、命中的 flavor、底层 `flavors::xxx::Channel` 的构造调用。
3. 特别留意 `bounded()` 内部的 `if cap == 0` 分支。

**预期结果**：你应该得到下面这张表（建议自己画一遍）：

| 构造函数 | 返回类型 | flavor 变体 | 底层实现 | 计数/包装方式 |
|---------|---------|------------|---------|--------------|
| `unbounded()` | `(Sender<T>, Receiver<T>)` | `List` | `flavors::list::Channel` | `counter::new` |
| `bounded(cap)`（cap > 0） | `(Sender<T>, Receiver<T>)` | `Array` | `flavors::array::Channel` | `counter::new` |
| `bounded(0)` | `(Sender<T>, Receiver<T>)` | `Zero` | `flavors::zero::Channel` | `counter::new` |
| `after(dur)` | `Receiver<Instant>` | `At` | `flavors::at::Channel` | `Arc::new` |
| `at(when)` | `Receiver<Instant>` | `At` | `flavors::at::Channel` | `Arc::new` |
| `tick(dur)` | `Receiver<Instant>` | `Tick` | `flavors::tick::Channel` | `Arc::new` |
| `never()` | `Receiver<T>` | `Never` | `flavors::never::Channel` | 无（零大小类型） |

**需要观察的现象**：上表第四列说明了一个关键差异——三种「真实」通道（array/list/zero）走 `counter::new` 做**引用计数**（可克隆、会 disconnect）；而 at/tick 用 `Arc`、never 是零大小类型，三者都**永不 disconnect**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `after(Duration::from_secs(u64::MAX))` 不会 panic，但也不会按时投递？

> **答案**：`after` 内部用 `Instant::now().checked_add(duration)`，当 `duration` 太大导致加法溢出时返回 `None`，于是退化调用 `never()`，构造一个永不投递的通道（见 [channel.rs:182-187](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L182-L187)）。

**练习 2**：`bounded(0)` 和 `bounded(5)` 走的是同一种 flavor 吗？

> **答案**：不是。前者走 `flavors::zero`（会合，不存消息），后者走 `flavors::array`（预分配数组）。分流发生在 [channel.rs:114](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L114) 的 `if cap == 0`。

---

### 4.2 公共类型壳：SenderFlavor / ReceiverFlavor 枚举与「按 flavor 分发」母题

#### 4.2.1 概念说明

用户面对的永远是 `Sender<T>` 和 `Receiver<T>` 这两个**统一类型**。但底层通道结构因 flavor 而异（数组、链表、定时器……）。crossbeam-channel 的做法是：

- `Sender<T>` 内部只有一个字段 `flavor: SenderFlavor<T>`；
- `Receiver<T>` 内部只有一个字段 `flavor: ReceiverFlavor<T>`；
- 这两个 `SenderFlavor` / `ReceiverFlavor` 是**枚举**，每个变体就是一种具体 flavor 的句柄。

于是所有公共方法（`send` / `recv` / `try_recv` / `len` / `capacity` …）都长一个样：**`match self.flavor`，把调用转发给对应变体的同名方法**。这就是贯穿全文件的架构母题——「**按 flavor 分发**」。

这种「壳 + 枚举分发」的好处是：用户写一套代码就能用六种通道，新增 flavor 时只需在枚举里加一个变体、在所有 `match` 里补一个分支，公共 API 完全不变。

#### 4.2.2 核心流程

以 `Receiver::recv()` 为例，分发流程如下：

```
r.recv()
   │
   └─ match self.flavor
          ├─ Array(chan) ──► chan.recv(None)        // 有界数组
          ├─ List(chan)  ──► chan.recv(None)        // 无界链表
          ├─ Zero(chan)  ──► chan.recv(None)        // 零容量
          ├─ At(chan)    ──► chan.recv(None) + 类型换装   // 定时
          ├─ Tick(chan)  ──► chan.recv(None) + 类型换装   // 周期
          └─ Never(chan) ──► chan.recv(None)        // 永不投递
```

这里有一个**关键不对称**：

- `SenderFlavor` 只有 **3 个**变体（Array / List / Zero）——因为 at/tick/never 三种特殊通道**没有发送方**，消息是 `recv` 时惰性生成的，根本不存在 `Sender`。
- `ReceiverFlavor` 有 **6 个**变体（多出 At / Tick / Never）。

另一个关键点：`at` / `tick` 通道投递的消息类型固定是 `Instant`，但 `Receiver<T>` 是泛型的。为了让泛型 `Receiver<T>` 能装下 `Receiver<Instant>`，代码用 `mem::transmute_copy` 把 `Result<Instant, _>`「换装」成 `Result<T, _>`。这是一个 unsafe 的类型擦除技巧，前提是「调用方自己保证 `T == Instant`」（构造函数 `after` / `at` / `tick` 的返回类型已经把 `T` 钉死为 `Instant`，所以安全）。

#### 4.2.3 源码精读

先看两个壳结构本身，它们各自只持有一个 `flavor` 字段：
[channel.rs:366-368](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L366-L368)（`Sender` 结构体，仅一个 `flavor` 字段）。

`SenderFlavor` 枚举——注意只有 3 个变体，且每个变体都是 `counter::Sender<具体flavor::Channel<T>>`：
[channel.rs:370-380](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L370-L380)

`ReceiverFlavor` 枚举——有 6 个变体。前三个是 `counter::Receiver<...>`，后三个分别是 `Arc<at::Channel>` / `Arc<tick::Channel>` / `never::Channel<T>`：
[channel.rs:728-747](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L728-L747)

「按 flavor 分发」母题的典型样貌，以 `Receiver::try_recv` 为例——6 个分支，其中 `At` / `Tick` 用 `transmute_copy` 做类型换装：
[channel.rs:778-801](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L778-L801)

`Receiver::recv` 同样是 6 分支分发，At/Tick 走带超时的 `recv(None)` 再换装类型：
[channel.rs:831-854](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L831-L854)

壳层不只分发收发方法，**状态查询方法也是纯转发**。例如 `Receiver::is_empty` 对 6 个 flavor 各调一次：
[channel.rs:986-995](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L986-L995)

`SelectHandle` trait（`select!` 机制的后端）的分发也遵循同一母题。`Receiver` 的 `deadline()` 方法特别值得注意——只有 `At` / `Tick` / `Never` 返回 `Some`（它们是「时间相关」的），array/list/zero 返回 `None`：
[channel.rs:1460-1469](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1460-L1469)

#### 4.2.4 代码实践

**实践目标**：跟踪任意一个公共方法，体会「壳层只是转发、真正干活在 flavor 实现」。

**操作步骤**：

1. 在 `src/channel.rs` 中找到 `Receiver::try_recv`（[第 778 行起](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L778-L801)）。
2. 选定一个 flavor（例如 `Array`），跟到 `counter::Receiver<flavors::array::Channel<T>>::try_recv`。
3. 再打开 `src/flavors/array.rs`，找到 `Channel::try_recv`（本讲不深入它的实现，只要确认「转发目的地真实存在」即可）。
4. 重复一次，改选 `Never` 分支，跟到 `src/flavors/never.rs` 的 `try_recv`。

**需要观察的现象**：

- 壳层的 `try_recv` 没有任何业务逻辑，只有 `match` + 转发。
- `At` / `Tick` 分支比其他分支多一行 `transmute_copy`——因为它们的底层返回 `Result<Instant, _>`，需要换装成 `Result<T, _>`。

**预期结果**：你会清晰看到「公共壳 → 枚举分发 → 具体 flavor」的三层结构，并理解为什么新增 flavor 必须同时改动所有 `match`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SenderFlavor` 比 `ReceiverFlavor` 少三个变体？

> **答案**：因为 at/tick/never 三种特殊通道「没有发送方」——它们的消息在 `recv` 时惰性生成（由定时器/周期触发，或根本不触发）。所以不存在对应的 `Sender`，`SenderFlavor` 只需覆盖 array/list/zero 三种「真实」通道（见 [channel.rs:371-380](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L371-L380)）。

**练习 2**：`Receiver::try_recv` 里 `At` / `Tick` 分支的 `transmute_copy` 为什么是安全的？

> **答案**：`after` / `at` / `tick` 三个构造函数的返回类型都把泛型 `T` 钉死为 `Instant`（返回 `Receiver<Instant>`），所以当 `self.flavor` 是 `At` / `Tick` 时，`T` 在运行时必然就是 `Instant`。`transmute_copy` 把 `Result<Instant, TryRecvError>` 按 `Result<T, TryRecvError>` 读出，由于 `T == Instant`，内存布局完全一致，因此安全。

---

### 4.3 lib.rs 的模块组织、internal 隐藏模块与 re-export

#### 4.3.1 概念说明

`src/lib.rs` 是整个 crate 的「**总装车间**」，它只做三件事：

1. **设置编译策略**：顶部 `#![no_std]` 声明本 crate 不强依赖标准库运行时；再用 `cfg(feature = "std")` 把几乎所有模块门控起来——也就是说「禁用 std 时，目前几乎所有功能都不可用」。
2. **声明内部模块**：用一系列 `mod channel;`、`mod flavors;` 等把源码文件挂到 crate 树上。注意它们都是**私有模块**（不是 `pub mod`），外界看不到内部结构。
3. **集中对外导出**：在文件末尾用一个大的 `pub use crate::{ ... }`，把对外要暴露的类型、函数、宏一次性 re-export 到 crate 根。用户写 `use crossbeam_channel::Sender`，实际上拿到的是从 `channel::Sender` re-export 出来的。

此外还有一个 `internal` 模块，用 `#[doc(hidden)]` 标记：它「真实存在、但不写进文档」，专门给 `select!` 宏在展开后调用。这是宏与库之间常见的「后门」模式。

#### 4.3.2 核心流程

lib.rs 的组织可以用三层来理解：

```
#![no_std]                                   ← 编译策略
       │
       ├── cfg(feature = "std")              ← 每个模块都被 std 门控
       │       mod channel / context / counter / err / flavors
       │       mod select / select_macro / utils / waker / alloc_helper
       │
       ├── #[doc(hidden)] pub mod internal   ← 给 select! 宏的后门
       │       pub use select::{SelectHandle, select, try_select, ...}
       │
       └── pub use crate::{ ... }            ← 对外集中 re-export
               channel::{Sender, Receiver, unbounded, bounded, after, at, tick, never, ...}
               err::{SendError, TrySendError, RecvError, ...}
               select::{Select, SelectedOperation}
```

关键认知：

- **用户看到的 API 100% 来自末尾的 `pub use`**，内部模块名（`channel` / `flavors` 等）对用户不可见。
- **`internal` 模块是「半公开」**：它的项有 `pub use`，但因为外层 `#[doc(hidden)]`，文档里看不到。`select!` 宏展开后会生成 `crate::internal::select(...)` 这样的调用，所以必须对外可访问。
- **`std` feature 是「总开关」**：禁用它，几乎所有模块都不编译。这也是为什么文档里强调「no_std 尚不支持」。

#### 4.3.3 源码精读

顶部编译策略：`#![no_std]` 加上一组 lint 警告（要求写文档、禁止在 unsafe fn 里再写裸 unsafe 等）：
[lib.rs:328-339](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L328-L339)

每个内部模块都被 `#[cfg(feature = "std")]` 门控——这就是「禁用 std 暂不支持」的根源：
[lib.rs:341-366](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L341-L366)

`internal` 隐藏模块——注意它是 `pub mod`（宏要能访问），但带 `#[doc(hidden)]`（不出现在文档），re-export 了 `select!` 宏需要的几个底层符号：
[lib.rs:368-375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L368-L375)

末尾的对外 `pub use`——这才是用户真正用到的名字来源，分三组：壳类型与构造函数、错误类型、`Select` 动态 API：
[lib.rs:377-387](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L377-L387)

> 说明：`select!` / `select_biased!` 两个宏通过 `#[macro_export]` 在 `select_macro.rs` 里导出（本讲不展开，留到 u3-l3），它们与这里的 `pub use` 一起构成了完整的对外 API 面。

#### 4.3.4 代码实践

**实践目标**：弄清「用户写 `use crossbeam_channel::X` 时，`X` 到底来自哪个内部模块」。

**操作步骤**：

1. 打开 `src/lib.rs` 末尾的 `pub use`（[第 377 行起](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L377-L387)）。
2. 列一张表，把每个对外名字映射到它的来源模块。例如：`Sender` ← `channel`，`SendError` ← `err`，`Select` ← `select`。
3. 接着打开 `src/channel.rs`，确认 `Sender`、`unbounded`、`bounded`、`after`、`at`、`tick`、`never`、`IntoIter`、`Iter`、`TryIter` 确实都定义在那里（它们被 [lib.rs:379-381](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L379-L381) 那一行 re-export）。
4. 最后写一个最小示例验证「内部模块名不可见」：尝试 `use crossbeam_channel::channel::Sender;`，编译器应报「`channel` 是私有模块」。

**需要观察的现象**：

- `use crossbeam_channel::Sender;` 能编译通过。
- `use crossbeam_channel::channel::Sender;` 报私有性错误（除非 crate 内部）。

**预期结果**：你确认了「用户 API 面 = 末尾 `pub use` 的并集」，并理解了 `internal` 模块为何既要 `pub` 又要 `#[doc(hidden)]`。

> 如果不便编译，第 4 步可标注「待本地验证」，但前 3 步的源码阅读一定能完成。

#### 4.3.5 小练习与答案

**练习 1**：用户代码里 `crossbeam_channel::after` 这个名字，在 `src/` 中实际定义在哪个文件？通过哪一行被导出？

> **答案**：定义在 `src/channel.rs`（`pub fn after`，[第 181 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L181-L188)），通过 [lib.rs:380](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L379-L381) 的 `pub use crate::channel::{ ..., after, ... }` 导出到 crate 根。

**练习 2**：为什么 `internal` 模块要同时用 `pub mod` 和 `#[doc(hidden)]`？

> **答案**：`select!` 宏展开后会生成对 `crate::internal::select(...)` 等的调用，所以这些符号必须 `pub` 对外可见。但它们是实现细节、不应出现在用户文档里，于是加 `#[doc(hidden)]` 让 rustdoc 隐藏它们（见 [lib.rs:369-371](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L368-L375)）。

**练习 3**：禁用 `std` feature 后，`Sender` 还能用吗？

> **答案**：不能。`mod channel` 被 `#[cfg(feature = "std")]` 门控（[lib.rs:349-350](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L349-L350)），`Sender` 定义在 `channel` 模块中，禁用 std 时整个模块不参与编译，对应的 `pub use` 也不会导出。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「**架构自检表 + 分发跟踪**」任务。

**任务背景**：假设你要给团队新人讲解 crossbeam-channel 的整体架构，需要一份一页纸的图解。

**要求**：

1. **画分流表**：整理「构造函数 → flavor → 底层实现 → 包装方式」四列对应表（参考 4.1.4 的表，但要求自己从源码逐行核对）。
2. **标注不对称**：在表上用记号标出 `SenderFlavor`（3 变体）与 `ReceiverFlavor`（6 变体）的差异，并解释原因。
3. **跟踪一次分发**：任选一个公共方法（建议 `Receiver::recv` 或 `Sender::send`），在源码里用箭头画出「壳层 match → flavor 实现」的转发路径，标出每个 `match` 分支的行号。
4. **解释 internal**：用一句话说明 `select!` 宏为什么需要 `internal` 隐藏模块。

**预期成果**：一张分流表 + 一张分发路径图 + 一段 internal 说明。完成它，你就掌握了 crossbeam-channel 的「骨架地图」，后续阅读任何 flavor 内部实现时，都能立刻定位「这段代码是被壳层的哪个分支调用的」。

> 提示：本任务是「源码阅读型实践」，不需要运行任何程序，但所有结论都必须能在源码中找到对应行号佐证。

## 6. 本讲小结

- crossbeam-channel 是「**一套统一 API + 六种 flavor**」：array（有界数组）、list（无界链表）、zero（零容量会合）、at（定时）、tick（周期）、never（永不投递）。
- 构造函数按需分流：`unbounded` → list；`bounded(cap>0)` → array；`bounded(0)` → zero；`after`/`at` → at；`tick` → tick；`never` → never。其中 `after` 溢出会退化为 `never`。
- `Sender<T>` / `Receiver<T>` 是**公共类型壳**，内部只持有一个 `flavor` 枚举字段；所有公共方法都遵循「**match flavor 转发**」母题。
- **关键不对称**：`SenderFlavor` 只有 3 个变体（特殊通道无发送方），`ReceiverFlavor` 有 6 个；`At`/`Tick` 分支用 `transmute_copy` 把 `Instant` 换装成泛型 `T`（由构造函数保证 `T == Instant` 而安全）。
- `src/lib.rs` 用 `#![no_std]` + `cfg(feature = "std")` 门控全部模块，末尾用一个大 `pub use` 集中导出对外 API；`#[doc(hidden)]` 的 `internal` 模块是 `select!` 宏的后门。
- 三种「真实」通道（array/list/zero）走 `counter::new` 引用计数、可克隆可 disconnect；at/tick 用 `Arc`、never 是零大小类型，三者都永不 disconnect。

## 7. 下一步学习建议

本讲建立的是「骨架地图」。接下来按依赖顺序深入：

1. **先懂公共机制**：读 `u2-l2 引用计数与生命周期 counter.rs`，理解 array/list/zero 为什么需要 `counter::new`、克隆与 disconnect 如何与计数协作。
2. **再读错误体系**：读 `u2-l3 错误类型体系 err.rs`，把分发母题里的「错误归一化」部分补全。
3. **接着读阻塞唤醒**：读 `u2-l4 阻塞与唤醒机制 context.rs + waker.rs`，这是理解任何 flavor「阻塞时发生了什么」的钥匙。
4. **然后逐个 flavor**：`u2-l5 array` → `u2-l6 list` → `u2-l7 zero` → `u2-l8 特殊通道`，每读一种，回头对照本讲的「壳层 match 分支」，确认分发目的地。
5. **最后是 select**：`u2-l9 select! 宏` 与 `u2-l10 Select 动态 API`，届时再回看本讲提到的 `internal` 模块和 `SelectHandle` 分发，会豁然开朗。

建议在阅读每个 flavor 时，都带着本讲 4.2 画的「分发路径图」——它能帮你始终知道「自己现在站在架构的哪一层」。
