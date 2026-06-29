# 工作区结构：核心 crate 与 async/blocking 三足鼎立

## 1. 本讲目标

本讲带你从「仓库整体」的角度看懂 ringbuf 项目。学完后，你应该能够：

- 看懂根 `Cargo.toml` 里 `[workspace]`、`[workspace.dependencies]`、`[workspace.package]`、`[package]` 各自的作用，理解「根 `Cargo.toml` 既是 workspace 定义、本身又是一个 crate」的双重身份。
- 说出仓库里一共有三个 crate：核心 `ringbuf`（0.5.0）、派生 `async-ringbuf`（0.3.6）、派生 `ringbuf-blocking`（0.1.0-rc.6），并画出它们的依赖方向。
- 解释为什么派生 crate 用 `ringbuf = { workspace = true }` 引用核心，并且核心在 `workspace.dependencies` 里被声明为 `default-features = false`——这是「派生 crate 保持 no_std 能力」的关键。
- 建立「核心 + 派生」的三 crate 心智模型，为后续按 crate 深入源码（trait、包装器、并发）打下导航基础。

承接上一讲：你已经知道 `HeapRb` 是 `SharedRb<Heap<T>>` 的别名、核心 crate 只提供非阻塞的 `try_*` 接口、想要「等待」语义需要派生 crate。本讲要回答的问题是——**这三个 crate 在文件层面、依赖层面、feature 层面究竟是怎么组织的**。

## 2. 前置知识

| 概念 | 通俗解释 |
| --- | --- |
| Cargo workspace | 一个「大仓库」里包含多个互相依赖的小 crate，用同一份根 `Cargo.toml` 统一管理版本、公共依赖、编译配置。 |
| crate | Rust 的最小编译单元，对应 crates.io 上发布的一个包，有自己的名字和版本。 |
| member | workspace 中被纳入统一管理的子 crate 目录。 |
| feature（feature flag） | 在 `Cargo.toml` 里声明的「可选开关」，用来在编译期裁剪功能（如 `std`、`alloc`）。 |
| `default-features` | 一个 crate 默认开启的 feature 集合。依赖方可以用 `default-features = false` 关掉默认值，再按需打开。 |
| `#![no_std]` | 声明这个 crate 不依赖标准库 `std`，只依赖 `core`（必要时加 `alloc`），可用于嵌入式/内核等无操作系统环境。 |
| `#[cfg(feature = "...")]` | 条件编译：只有某 feature 开启时，这段代码才会被编译进来。 |

如果你对 feature 之间的依赖（`std = ["alloc"]` 这种写法）还不熟悉，本讲第 4.4 节会结合真实配置详细讲。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `Cargo.toml`（仓库根） | 同时是 workspace 定义和核心 crate `ringbuf` 的清单：声明 members、公共依赖、feature、examples。 |
| `src/lib.rs` | 核心 crate 的库入口，`#![no_std]`，声明并导出核心模块（`rb`、`storage`、`traits`、`wrap` 等）。 |
| `async/Cargo.toml` | 派生 crate `async-ringbuf` 的清单：依赖核心 `ringbuf` + `futures-util`，声明 async 相关 feature。 |
| `async/src/lib.rs` | `async-ringbuf` 的库入口，`#![no_std]`，导出 `AsyncRb`、`async_transfer` 等。 |
| `blocking/Cargo.toml` | 派生 crate `ringbuf-blocking` 的清单：依赖核心 `ringbuf`，声明阻塞相关 feature。 |
| `blocking/src/lib.rs` | `ringbuf-blocking` 的库入口，`#![no_std]`，导出 `BlockingRb`、`BlockingProd`、`BlockingCons`、`WaitError`。 |

> 提示：仓库里还有 `async/README.md`、`src/`、`async/src/`、`blocking/src/` 等子目录，本讲只聚焦「清单文件 + 库入口」，看清楚三 crate 的骨架；具体实现模块会在后续讲义逐个展开。

## 4. 核心概念与源码讲解

### 4.1 Cargo workspace：根 Cargo.toml 的「双重身份」

#### 4.1.1 概念说明

当你打开一个 Rust 项目，如果根 `Cargo.toml` 里有 `[workspace]` 段，说明它是一个 **workspace（工作区）**——一个逻辑仓库下挂着多个 crate。ringbuf 的特殊之处在于：它的根 `Cargo.toml` **同时承担两个角色**：

1. 它是 workspace 的「根清单」，声明了哪些目录是 member、workspace 级别的公共依赖和元数据；
2. 它自己也是一个 crate（即核心 `ringbuf`），所以里面还有 `[package]` 段和 `src/` 目录。

这种「根 package 工作区」（root package workspace）是 Cargo 的常见写法：核心库自己住在仓库根，派生库住在子目录里，共用一份根清单。与之相对的是「虚拟清单」（virtual manifest）——只有 `[workspace]` 没有 `[package]`，仓库根本身不是任何 crate。ringbuf 选了前者。

#### 4.1.2 核心流程

要判断「仓库里有几个 crate、它们各自在哪」，按这个流程读根 `Cargo.toml`：

1. 找 `[workspace]` 段 → 看 `members = [...]` 列出的子 crate 目录。
2. 记住：如果根 `Cargo.toml` 自带 `[package]`，那么根目录也是一个 crate，**它会被自动算作 workspace 成员，不需要写进 `members`**。
3. 找 `[workspace.dependencies]` 段 → 这是 workspace 内部 crate 互相引用时共用的「依赖模板」。
4. 找 `[workspace.package]` 段 → 这是各 crate 共享的元数据（edition、作者、license 等）。

对应到 ringbuf：

```
仓库根 Cargo.toml
├── [workspace.package]      ← 共享元数据（edition 2024、作者、license…）
├── [workspace.dependencies] ← 公共依赖模板（关键：ringbuf = { default-features = false }）
├── [workspace]              ← members = ["async", "blocking"]
└── [package] name="ringbuf" ← 根目录本身就是核心 crate ringbuf（自动成为成员）
```

所以仓库里一共 **3 个 crate**：

- `ringbuf`（根目录，自动成员）
- `async-ringbuf`（`async/` 目录，显式 member）
- `ringbuf-blocking`（`blocking/` 目录，显式 member）

#### 4.1.3 源码精读

根 `Cargo.toml` 顶部先定义共享元数据和公共依赖，再声明 workspace，最后才是核心 crate 自己的 `[package]`：

[工作区共享元数据与公共依赖模板:Cargo.toml:1-10](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L1-L10) —— 注意第 9–10 行把 `ringbuf` 自己声明成了一个「workspace 级依赖」，并设了 `default-features = false`（第 4.4 节详解）。

[workspace 成员声明:Cargo.toml:12-13](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L12-L13) —— `members = ["async", "blocking"]`，只列了两个子目录；根 crate 不在里面，因为带 `[package]` 的根会自动入列。

[核心 crate 的 package 声明:Cargo.toml:15-25](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L15-L25) —— `name = "ringbuf"`、`version = "0.5.0"`，并用 `edition.workspace = true` 等复用上面 `[workspace.package]` 里的值，避免重复填写。

子 crate 则用 `edition.workspace = true` / `authors.workspace = true` / `repository.workspace = true` 来引用同一份元数据，例如：

[async-ringbuf 的 package 复用 workspace 元数据:async/Cargo.toml:1-11](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/Cargo.toml#L1-L11) —— `edition.workspace = true` 表示「edition 跟根 `[workspace.package].edition` 走」。

> 一个易错点：`members` 里没有 `.`（根目录本身）。有人会误以为仓库只有两个 crate。实际上带 `[package]` 的 workspace 根目录是**隐式成员**，加起来才是三个。

#### 4.1.4 代码实践

1. **实践目标**：确认仓库的 crate 数量与位置。
2. **操作步骤**：打开仓库根 `Cargo.toml`，定位 `[workspace]` 段，数一数 `members`；再确认根目录有 `[package]` 且 `name = "ringbuf"`。
3. **需要观察的现象**：`members = ["async", "blocking"]` 只有两条，但根目录本身是第三个 crate。
4. **预期结果**：仓库共有 3 个 crate：根 `ringbuf`、`async/` 下的 `async-ringbuf`、`blocking/` 下的 `ringbuf-blocking`。

#### 4.1.5 小练习与答案

**练习 1**：如果把根 `Cargo.toml` 里的 `[package]` 段整段删掉，仓库会发生什么变化？

**答案**：根 `Cargo.toml` 会退化成「虚拟清单」（只有 `[workspace]`），此时仓库根不再是任何 crate；核心 `ringbuf` 必须移到某个子目录并加进 `members` 才能继续编译。ringbuf 没有这么做，而是让核心库住在根目录。

**练习 2**：`members` 为什么不把根目录写进去（如 `members = [".", "async", "blocking"]`）？

**答案**：Cargo 规定：只要 workspace 根清单里有 `[package]`，它就自动是成员，不能再（也不需要）在 `members` 里列出自己；写 `.` 反而会报错。

---

### 4.2 最小模块 ringbuf：核心 crate 的职责与 feature 体系

#### 4.2.1 概念说明

核心 crate `ringbuf` 是整个项目的「基石」。它的职责非常纯粹：**提供无锁 SPSC 环形缓冲区的核心数据结构与非阻塞 `try_*` 接口**。它故意不提供任何「等待/阻塞」语义——`try_push` 满了就立刻返回 `Err`，`try_pop` 空了就立刻返回 `None`，绝不挂起线程。

把「等待语义」剥离出去是个关键设计决策：核心保持最小、保持 `no_std` 可用，而「怎么等」交给两个派生 crate 分别用 async（`async-ringbuf`）和阻塞线程（`ringbuf-blocking`）去实现。

核心 crate 通过一组 **feature flag** 来控制可裁剪的能力：

- `std`：启用标准库（默认开启）。
- `alloc`：启用堆分配（被 `std` 包含）。
- `portable-atomic`：在没有 64 位原子指令的小型目标上，用 `portable-atomic` 替代 std 原子。
- `bench` / `test_local`：仅供基准测试和切换被测实现用。

#### 4.2.2 核心流程

feature 之间存在依赖关系，读 `[features]` 段时要把这张「包含图」画出来：

```
default ──┐
          ▼
         std ──► alloc ──► portable-atomic-util?/alloc
          │        ▲
          └─► portable-atomic?/std
                 portable-atomic-util?/std
```

含义：开启 `std` 会自动开启 `alloc`；`alloc` 又会按需开启 `portable-atomic-util` 的 `alloc`。这样用户只需声明顶层 feature，下层会自动级联。

核心 crate 的库入口 `src/lib.rs` 用 `#![no_std]` 起步，再按 feature 把 `alloc`/`std` 显式引回：

```rust
#![no_std]
#[cfg(feature = "alloc")]
extern crate alloc;
#[cfg(feature = "std")]
extern crate std;
```

也就是说，**核心 crate 默认不依赖 std**，只有在开启 `std` feature 时才把 `std` 引入。这正是它能用于嵌入式/内核场景的前提。

#### 4.2.3 源码精读

核心 crate 的 feature 定义：

[ringbuf 的 feature 体系:Cargo.toml:27-33](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L27-L33) —— `default = ["std"]`、`std = ["alloc", "portable-atomic?/std", ...]`、`alloc = ["portable-atomic-util?/alloc"]`。注意 `portable-atomic?` 中的 `?` 表示「仅当该可选依赖存在时才开启其 feature」。

核心 crate 唯一的非可选依赖：

[核心依赖 crossbeam-utils:Cargo.toml:35-38](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L35-L38) —— `crossbeam-utils`（提供 `CachePadded` 防伪共享），`portable-atomic` 系列为可选依赖。

核心库入口与模块布局：

[核心 crate 的 no_std 入口与模块声明:src/lib.rs:149-171](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L149-L171) —— `#![no_std]` 起步，声明 `storage`、`rb`、`traits`、`wrap`、`transfer`、`utils` 等模块。这就是核心 crate 的全部「骨架」，后续讲义会逐个深入。

#### 4.2.4 代码实践

1. **实践目标**：理清核心 crate「哪些能力随 feature 开关」。
2. **操作步骤**：对照 `Cargo.toml` 的 `[features]` 段，在纸上列出：开 `default` 时哪些 feature 被点亮；只开 `alloc`（用 `--no-default-features --features alloc`）时点亮哪些。
3. **需要观察的现象**：`std` 会拉起 `alloc`；反过来 `alloc` 不拉起 `std`。
4. **预期结果**：核心 crate 可以在「只有 alloc、没有 std」的配置下编译（堆分配可用、但无操作系统 API）。「待本地验证」：可用 `cargo build -p ringbuf --no-default-features --features alloc` 自行确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么核心 crate 要把 `try_push`/`try_pop` 设计成「满了/空了立刻返回」，而不是直接阻塞等待？

**答案**：为了保持核心最小、保持 `no_std` 可用、保持「运行时无关」。阻塞/等待需要操作系统或异步运行时支持，把它们放进核心会破坏 `no_std` 与无运行时的目标。等待语义留给派生 crate 按需添加。

**练习 2**：`portable-atomic?/std` 里的 `?` 是什么意思？

**答案**：这是 Cargo 的「弱依赖 feature」语法——「只有当 `portable-atomic` 这个可选依赖被启用时，才去开启它的 `std` feature」。如果用户没开 `portable-atomic`，这行不会报错，而是被忽略。

---

### 4.3 最小模块 async_ringbuf / ringbuf-blocking：派生 crate 的复用与扩展

#### 4.3.1 概念说明

两个派生 crate 都遵循同一种套路：**把核心 `SharedRb` 包一层，再附加上「等待/唤醒」所需的同步原语**，从而在不变核心的前提下补上等待语义。它们的差异在于「怎么等」：

- `async-ringbuf`：用 `futures-util` 的 `AtomicWaker`，提供 `async/await` 接口，**不绑定任何运行时**（tokio、async-std 都能用）。
- `ringbuf-blocking`：用可插拔的 `Semaphore` trait（默认 `StdSemaphore` 基于 `Condvar+Mutex`），提供会挂起当前线程的阻塞接口。

两个派生 crate 的库入口同样是 `#![no_std]`，同样按 feature 引回 `alloc`/`std`，结构高度对称。

#### 4.3.2 核心流程

派生 crate「包一层」的统一模式可以概括为：

```
派生 RB = SharedRb（核心无锁缓冲区） + 两个同步原语（read 端 / write 端）
```

- 写操作推进 `write` 索引后 → 通知「read 端」的同步原语（告诉消费者：有新数据了）。
- 读操作推进 `read` 索引后 → 通知「write 端」的同步原语（告诉生产者：有空位了）。

具体到每个 crate：

| 派生 crate | 同步原语 | 写后动作 | 读后动作 | 等待方式 |
| --- | --- | --- | --- | --- |
| `async-ringbuf` | `AtomicWaker`（read/write 各一个） | `self.write.wake()` | `self.read.wake()` | `Future` 的 `poll` |
| `ringbuf-blocking` | `Semaphore`（read/write 各一个） | `self.write.give()` | `self.read.give()` | `take()` 阻塞线程 |

#### 4.3.3 源码精读

`AsyncRb` 的结构——核心 `SharedRb` 加两个 `AtomicWaker`：

[AsyncRb = SharedRb + 两个 AtomicWaker:async/src/rb.rs:22-26](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/rb.rs#L22-L26) —— `base: SharedRb<S>` 加上 `read: AtomicWaker` 与 `write: AtomicWaker`。

写操作推进索引后唤醒消费者：

[AsyncRb 写入后唤醒 write waker:async/src/rb.rs:74-79](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/rb.rs#L74-L79) —— 先调用核心 `set_write_index`，再 `self.write.wake()`。

`BlockingRb` 的结构——核心 `SharedRb` 加两个 `Semaphore`：

[BlockingRb = SharedRb + 两个 Semaphore（std 变体）:blocking/src/rb.rs:22-27](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/rb.rs#L22-L27) —— 在 `std` feature 下，`Semaphore` 默认取 `StdSemaphore`。

写操作推进索引后释放信号量：

[BlockingRb 写入后 give write 信号量:blocking/src/rb.rs:72-77](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/rb.rs#L72-L77) —— 先 `set_write_index`，再 `self.write.give()`。

两个 crate 库入口的对称布局（同样 `#![no_std]` + 条件引回 `alloc`/`std`）：

[async-ringbuf 库入口:async/src/lib.rs:1-19](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/lib.rs#L1-L19) —— 声明 `alias`/`rb`/`traits`/`wrap`/`transfer` 模块，导出 `AsyncRb`、`async_transfer`。

[ringbuf-blocking 库入口:blocking/src/lib.rs:1-21](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/lib.rs#L1-L21) —— 声明 `alias`/`rb`/`sync`/`wrap` 模块，导出 `BlockingRb`、`BlockingProd`、`BlockingCons`、`WaitError`。

> 一个值得注意的**差异**：两者都让用户能用到核心的 trait，但方式不同。`ringbuf-blocking` 直接把核心 trait 模块整个转发出来：`pub use ringbuf::traits;`（见 [blocking/src/lib.rs:17](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/lib.rs#L17)）；而 `async-ringbuf` 只转发了其中的子模块：`pub use traits::{consumer, producer};`（见 [async/src/lib.rs:18](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/src/lib.rs#L18)），它还有一套自己的 `AsyncProducer`/`AsyncConsumer` trait。这说明 blocking 更贴近核心 API（只是多了阻塞），async 则在核心之上另建了一层 async trait 体系。

#### 4.3.4 代码实践

1. **实践目标**：对比两个派生 crate 的「包装 + 同步原语」结构。
2. **操作步骤**：分别打开 `async/src/rb.rs` 和 `blocking/src/rb.rs`，找到各自 `Producer`/`Consumer` trait 的 `set_write_index`/`set_read_index` 实现。
3. **需要观察的现象**：两者都先调用 `self.base.set_*_index(...)`（核心逻辑），再各自调用 `wake()` / `give()`（同步逻辑）。
4. **预期结果**：能复述「核心负责搬数据，派生负责唤醒/放行」的分工。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `AsyncRb` 需要两个 `AtomicWaker`，而不是一个？

**答案**：因为读、写两端各自有独立的等待者——消费者等「有空位/有数据」，生产者也等「有空位/有数据」，二者互不相同。用两个 waker 分别针对 read、write 两端，唤醒时只唤醒真正关心该事件的一方，避免无效唤醒。

**练习 2**：`async-ringbuf` 的 `default = ["alloc", "std"]`，但它仍然声明 `#![no_std]`，这两者矛盾吗？

**答案**：不矛盾。`#![no_std]` 表示「默认不链接 std」；而 `std` feature 开启后会 `extern crate std;` 把 std 重新引回。所以默认构建里它确实用到了 std，但代码结构允许你在 `--no-default-features` 下把它编译成 no_std。`#![no_std]` 给的是「可以不用 std」的**能力**，而不是「一定不用 std」。

---

### 4.4 连接枢纽：workspace.dependencies 与 default-features = false

#### 4.4.1 概念说明

派生 crate 引用核心 crate 时，写的是一行很简洁的依赖：

```toml
[dependencies]
ringbuf = { workspace = true }
```

`workspace = true` 的意思是「不要在这里重新写版本和路径，去 `[workspace.dependencies]` 里取那份公共模板」。而那份公共模板长这样：

```toml
[workspace.dependencies]
ringbuf = { path = ".", version = "0.5.0", default-features = false }
```

最关键的就是末尾的 **`default-features = false`**。它回答了本讲的核心问题：**为什么派生 crate 默认不直接开启核心的 `std`？**

#### 4.4.2 核心流程

核心 crate 的默认 feature 是 `default = ["std"]`。如果不加干预，任何依赖 `ringbuf` 的 crate 都会把 `std` 拉进来。但两个派生 crate 都是 `#![no_std]` 库，它们要保留「不带 std 也能编译」的能力。解决链路是：

1. 在 `[workspace.dependencies]` 里把核心声明为 `default-features = false`，于是 `workspace = true` 的引用默认拿到的就是「裸核心」（不带 std）。
2. 派生 crate 在自己的 `[features]` 里，**按需**把核心 feature 重新点亮：
   - `async-ringbuf`：`alloc = ["ringbuf/alloc"]`、`std = ["alloc", "ringbuf/std", ...]`。
   - `ringbuf-blocking`：`alloc = ["ringbuf/alloc"]`、`std = ["ringbuf/std", "alloc"]`。
3. 这样，核心的 `std` 只在派生 crate 自己的 `std` feature 开启时才会被点亮，不会被默认「泄漏」进来。

一句话总结：`default-features = false` 让派生 crate **夺回对核心 feature 的控制权**，从而保住 no_std 能力。

> 诚实的补充：Cargo 有 **feature 统一（unification）** 机制。当你在仓库里整体 `cargo build` 时，因为派生 crate 自己的 `default = ["std"]` 会点亮 `ringbuf/std`，核心的 std 最终仍会被启用。所以 `default-features = false` 的意义不在于「整体构建永远没有 std」，而在于「派生 crate 可以被配置成不带 std 来构建，且核心默认值不会不受控地泄漏」。它关乎 feature 图的正确性与可控性。

#### 4.4.3 源码精读

公共依赖模板里的关键开关：

[workspace.dependencies 把 ringbuf 设为 default-features=false:Cargo.toml:9-10](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L9-L10) —— 派生 crate 通过 `workspace = true` 继承的正是这一条，含 `default-features = false`。

派生 crate 引用核心（极简一行）：

[async-ringbuf 依赖核心:async/Cargo.toml:25-26](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/Cargo.toml#L25-L26) 与 [ringbuf-blocking 依赖核心:blocking/Cargo.toml:23-24](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/Cargo.toml#L23-L24) —— 两者都写 `ringbuf = { workspace = true }`。

派生 crate 把核心 feature 重新点亮：

[async-ringbuf 的 feature 透传:async/Cargo.toml:13-23](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/async/Cargo.toml#L13-L23) —— `alloc = ["ringbuf/alloc"]`、`std = ["alloc", "ringbuf/std", "futures-util/io"]`，`portable-atomic` 也透传给 `ringbuf/portable-atomic`。

[ringbuf-blocking 的 feature 透传:blocking/Cargo.toml:13-21](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/Cargo.toml#L13-L21) —— `alloc = ["ringbuf/alloc"]`、`std = ["ringbuf/std", "alloc"]`。

对照表（从清单读出的真实版本与依赖）：

| crate | 版本 | 直接依赖核心？ | 还依赖 |
| --- | --- | --- | --- |
| `ringbuf` | 0.5.0 | （自己是核心） | `crossbeam-utils`，可选 `portable-atomic(-util)` |
| `async-ringbuf` | 0.3.6 | 是（`workspace = true`） | `futures-util`（默认仅 `sink`），可选 `portable-atomic(-util)` |
| `ringbuf-blocking` | 0.1.0-rc.6 | 是（`workspace = true`） | 可选 `portable-atomic(-util)` |

#### 4.4.4 代码实践

1. **实践目标**：亲手验证 `default-features = false` 的效果。
2. **操作步骤**：执行 `cargo tree -p async-ringbuf --no-default-features --features alloc`，观察 `ringbuf` 节点的 feature；再执行 `cargo tree -p async-ringbuf`（带默认 std）对比。
3. **需要观察的现象**：只开 `alloc` 时，`ringbuf` 上不应该出现 `std` feature；开默认时则会出现 `std`。
4. **预期结果**：证明核心的 `std` 确实由派生 crate 的 feature 控制，而非默认泄漏。**待本地验证**：`cargo tree` 的具体输出格式取决于本机 Cargo 版本。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `[workspace.dependencies]` 里的 `default-features = false` 删掉，会发生什么？

**答案**：派生 crate 通过 `workspace = true` 会继承核心的默认 feature `std`，于是即使你用 `--no-default-features --features alloc` 去构建 `async-ringbuf`，核心 `ringbuf` 仍会被点亮 `std`，派生 crate 就失去了 no_std 构建能力。这正是该开关存在的理由。

**练习 2**：为什么派生 crate 不直接写 `ringbuf = { path = "..", version = "0.5.0" }`，而要用 `workspace = true`？

**答案**：用 `workspace = true` 可以让版本号、路径、`default-features` 等设置集中维护在根 `Cargo.toml` 一处，三个 crate（以及未来新增的 crate）引用时保持一致，避免版本漂移和重复填写。

---

## 5. 综合实践

**任务**：画出三个 crate 的依赖关系图，并解释「派生 crate 的 default-features 都不直接开启核心的 std」的原因。

**步骤**：

1. **画依赖图**：根据本讲读到的清单，画出如下关系（节点标注 crate 名 + 版本）：

   ```
                   crossbeam-utils
                         ▲
                         │
   futures-util ──►  async-ringbuf (0.3.6) ──┐
                                              │
                                              ▼
                                          ringbuf (0.5.0)   ← 核心无锁库
                                              ▲
                                              │
                       ringbuf-blocking (0.1.0-rc.6) ──┘
   ```

   要点：依赖方向是「派生 → 核心」；核心不依赖任何派生；`async-ringbuf` 额外依赖 `futures-util`。

2. **标注 feature 控制点**：在图上标出 `[workspace.dependencies]` 里的 `ringbuf = { ..., default-features = false }`，以及派生 crate 里的 `alloc = ["ringbuf/alloc"]`、`std = ["ringbuf/std", ...]`。

3. **用一句话解释**：派生 crate 都是 `#![no_std]` 库，需要保留「不带 std 也能编译」的能力；通过 `default-features = false` 让核心的默认 `std` 不泄漏进来，再由派生 crate 自己的 `std`/`alloc` feature 决定是否点亮核心对应能力。

4. **动手验证**（可选）：运行 `cargo build --workspace`（整体构建）与 `cargo build -p async-ringbuf --no-default-features --features alloc`（最小构建），对比两者是否都能成功，从而体会 feature 控制的效果。**待本地验证**。

**预期产出**：一张清晰的依赖关系图 + 一段说明 default-features 控制逻辑的文字。这是后续按 crate 深入源码时的「导航地图」。

## 6. 本讲小结

- ringbuf 仓库是 **Cargo workspace**，根 `Cargo.toml` 身兼两职：既是 workspace 定义，又是核心 crate `ringbuf`（0.5.0）的清单。
- 仓库共 **3 个 crate**：根目录的 `ringbuf`（隐式成员）、`async/` 的 `async-ringbuf`（0.3.6）、`blocking/` 的 `ringbuf-blocking`（0.1.0-rc.6）。
- 核心 crate 提供**纯无锁、非阻塞**接口，刻意把「等待语义」剥离给派生 crate。
- 派生 crate 都用**「核心 `SharedRb` + 同步原语」**模式：`async-ringbuf` 加 `AtomicWaker`，`ringbuf-blocking` 加 `Semaphore`。
- 派生 crate 通过 `ringbuf = { workspace = true }` 引用核心，而 `[workspace.dependencies]` 里设了 `default-features = false`，让派生 crate 掌控核心 feature、保住 no_std 能力。
- 两个派生 crate 库入口结构对称（`#![no_std]` + 按 feature 引回 `alloc`/`std`），但转发核心 trait 的方式不同：blocking 整个转发 `ringbuf::traits`，async 只转发子模块并另建 async trait 层。

## 7. 下一步学习建议

- 想理解核心 crate 内部怎么组织模块，先读 [src/lib.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs) 的模块声明与文档注释，它会列出 `Storage / Indices / Hold flags` 三大组成。
- 下一单元（u2）将进入**环形缓冲区原理**：双索引与 `2*capacity` 模运算，以及 `Storage` 抽象如何支撑 `Array/Heap/Ref` 多种后端，建议接着学 `u2-l1` 与 `u2-l2`。
- 对 async / blocking 派生 crate 内部实现感兴趣的，可在学完核心 trait 体系（u3）和无锁并发（u5）后，再进入 u6（async-ringbuf）与 u7（ringbuf-blocking）。
- 动手建议：现在用 `cargo doc --workspace --open` 一次性生成三个 crate 的文档，对照本讲的依赖图浏览顶层导出类型，建立整体印象。
