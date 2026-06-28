# 项目总览与定位：什么是无锁 SPSC 环形缓冲区

## 1. 本讲目标

本讲是 ringbuf 学习手册的第一篇。读完本讲后，你应该能够：

- 用自己的话说清楚 **ringbuf 到底解决什么问题**——它是一个什么样的数据结构。
- 读懂 ringbuf 在 README 里宣称的**核心特性**（无锁、直接内存访问、overwrite、多种存储后端、no_std 等）。
- 建立**三大 crate 的心智模型**：核心 crate `ringbuf`，以及派生出的 `async-ringbuf` 与 `ringbuf-blocking`。
- 在本地把项目文档（`cargo doc`）跑起来，能找到顶层导出的主要类型。

本篇**不**要求你掌握任何无锁并发、`unsafe` 或 `async` 知识，这些是后续讲义的主题。这里我们只做"认识项目"。

## 2. 前置知识

为了顺利入门，先建立几个通俗概念。后面会逐个与源码对照。

**什么是缓冲区（buffer）？**
当数据的"生产速度"和"消费速度"不一致时，我们需要一个中间容器先暂存数据，让快的一端不必等慢的一端。这个中间容器就是缓冲区。比如键盘敲得快、程序处理得慢，键盘事件可以先排进缓冲区。

**什么是 FIFO（先进先出）？**
FIFO = First In First Out。先放进去的元素先被取出来，就像排队：先排队的人先被服务。这与"栈"（后进先出，LIFO）正好相反。

**什么是环形缓冲区（ring buffer / circular buffer）？**
想象一个固定大小的数组，但它逻辑上首尾相连，像一个环。我们维护两个"指针"（在 ringbuf 里叫 `read` 和 `write` 索引）：

```
        write 指向下一个空槽
        ↓
[ A ][ B ][   ][   ][   ]
  ↑
  read 指向最旧的元素
```

写入时元素放进 `write` 指向的槽，`write` 往前走；读取时从 `read` 指向的槽取出，`read` 往前走。走到数组末尾就绕回开头，所以叫"环形"。好处是：**内存固定分配一次，反复复用，不需要反复申请/释放**。

**什么是 SPSC？**
SPSC = Single Producer, Single Consumer（单生产者、单消费者）。即同一时刻**至多只有一个写入端**和**至多只有一个读取端**。这是 ringbuf 的核心假设，许多高性能设计都建立在这个约束之上。后文会看到，ringbuf 用专门的"hold 标志"在运行时强制这一约束。

**什么是"无锁"（lock-free）？**
传统的线程同步用"锁"（mutex）：线程 A 拿到锁后，线程 B 必须干等（阻塞），直到 A 释放。无锁则用 CPU 提供的**原子操作**来协调：操作要么立刻成功、要么立刻失败返回，**绝不阻塞等待**。ringbuf 的读写操作都是"立即成功或立即失败"的，例如满了就立刻告诉你写不进。

> 小提示：如果你已经熟悉 Rust 的 `Cargo`、`cargo doc`、workspace 概念，可以直接跳到第 3 节。

## 3. 本讲源码地图

本讲主要阅读两个文件，它们是认识整个项目的"门面"：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/README.md) | 面向用户的介绍：项目定位、特性清单、用法示例、性能说明、派生 crate。 |
| [src/lib.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs) | 核心 crate 的库入口（crate root）：顶层文档、模块声明、对外导出的类型。 |

此外，本讲为了说清"项目结构"还会顺带引用三个佐证文件：

- [Cargo.toml](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml)：workspace 配置与核心 crate 元信息。
- [src/alias.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs)：常用类型的别名（`HeapRb`、`StaticRb` 等）。
- 派生 crate 的目录 `async/` 与 `blocking/`（仅作为依赖关系佐证，本讲不深入）。

## 4. 核心概念与源码讲解

本讲围绕三个最小模块展开：

1. **ringbuf 是什么**——定位与解决的问题（对应学习目标 1）。
2. **ringbuf 的核心特性**——逐条读懂 README 的特性清单（对应学习目标 2）。
3. **三个 crate 的关系与顶层导出**——核心 crate 与 async/blocking 派生 crate（对应学习目标 3）。

### 4.1 ringbuf 是什么：无锁 SPSC FIFO 环形缓冲区

#### 4.1.1 概念说明

把前置知识里的概念串起来，就能得到 ringbuf 的一句话定位：

> ringbuf 是一个**无锁（lock-free）的、单生产者单消费者（SPSC）的、先进先出（FIFO）环形缓冲区**，并且允许你**直接访问其内部内存**。

这条定位同时出现在 README 和 crate 文档的最开头，措辞完全一致：

- [README.md:L21-L21](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/README.md#L21-L21) —— README 顶部的一句话定位。
- [src/lib.rs:L1-L1](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L1-L1) —— crate 根模块的同一句文档注释（`//!` 开头表示"对整个 crate 的说明"）。

这句话里每个词都对应一个设计决策，本讲会逐个展开。它解决的核心问题是：**如何在不使用锁、不阻塞线程的前提下，在两个线程（或两个执行流）之间高效地传递一连串元素。**

典型应用场景包括：

- **线程间数据管道**：一个线程采集数据，另一个线程处理数据，中间用环形缓冲区解耦。
- **嵌入式 / `no_std` 系统**：内存受限、不能动态分配的环境下的消息传递。
- **字节流处理**：把环形缓冲区当成 `Read`/`Write` 的字节管道（类似一个内存中的 pipe）。

#### 4.1.2 核心流程

不管哪种应用，使用 ringbuf 的基本生命周期都是同一个"四步走"流程：

```
1. 创建缓冲区      →  HeapRb::<T>::new(capacity)
2. 拆分为两端      →  rb.split()  得到 (Producer, Consumer)
3. 生产者写入      →  prod.try_push(item)   ← 满了立即返回 Err，不等待
4. 消费者读取      →  cons.try_pop()        ← 空了立即返回 None，不等待
```

关键点：**写入和读取都是"立即成功或立即失败"**，这正是"无锁"在 API 层面的直接体现。生产者写满时不会停下来等消费者腾位置，而是直接告诉你"现在写不进去"（返回 `Err`）；消费者读空时也不会等生产者，而是直接返回 `None`。

> 这个流程图里只画了最常用的 `try_push` / `try_pop`。如果想要"写满就等"的阻塞行为，那是派生 crate `ringbuf-blocking` 的事；想要 `await` 的异步等待，那是 `async-ringbuf` 的事。核心 crate 本身只提供非阻塞的 `try_*` 接口。

#### 4.1.3 源码精读

**定位语句**

如上文所引，[README.md:L21-L21](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/README.md#L21-L21) 与 [src/lib.rs:L1-L1](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L1-L1) 用同一句话定义了项目。`lib.rs` 第 1 行的 `//!` 是 Rust 的"crate 级文档注释"，会出现在 `cargo doc` 生成的首页最上方。

**用法说明**

README 在"Usage"小节用三句话讲清了上面的四步流程：

- [README.md:L38-L41](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/README.md#L38-L41) —— "先创建缓冲区（推荐 `HeapRb`）→ 拆分为 `Producer`/`Consumer` → Producer 插入、Consumer 取出"。这正好对应 4.1.2 的流程。

**实现细节的预告**

[lib.rs 的"Implementation details"小节](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L106-L148) 告诉我们每个环形缓冲区由三部分组成：**Storage（存储区）、Indices（读/写索引）、Hold flags（持有标志）**。这三部分是后续讲义（u2、u3、u5）的主线，本讲你只需要知道"项目自己也是这么拆解的"即可。

**一个关键细节：为什么不浪费槽位**

[lib.rs:L122-L142](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L122-L142) 解释了索引机制：`read % capacity` 指向最旧元素，`write % capacity` 指向下一个空槽，并且索引取值范围是 `0..2*capacity` 而非 `0..capacity`，这样就能在不浪费一个槽位的前提下区分"空"和"满"。这是 ringbuf 最精巧的设计之一，本讲先记住结论，细节留到 u2-l1 详解。

#### 4.1.4 代码实践

**实践目标**：亲手确认"一句话定位"，并理解 README 是如何展开它的。

**操作步骤**：

1. 打开项目根目录的 [README.md](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/README.md)。
2. 找到第 21 行的那句定位（"Lock-free SPSC FIFO ring buffer ..."）。
3. 把它翻译成中文写在笔记里：**"____ 的 ____ 环形缓冲区，支持直接访问内部数据。"**

**需要观察的现象**：
README 的特性清单（Features）和示例（Examples）都是对这"一句话定位"的逐词展开。比如"SPSC"会体现在"只能 split 出一个 Producer 和一个 Consumer"，"Lock-free"会体现在 `try_push` 满了就返回 `Err`。

**预期结果**：你能用一句话向同事介绍 ringbuf 是什么，而不需要背诵 API。

> 说明：本实践是纯阅读型的，不运行任何代码。具体动手运行示例的程序将在 u1-l2 进行。

#### 4.1.5 小练习与答案

**练习 1**：ringbuf 是 SPSC 的。如果把 "SPSC" 展开成中文，是哪八个字？

> **答案**：单生产者、单消费者（Single Producer, Single Consumer）。

**练习 2**：在 4.1.2 的流程中，`prod.try_push(item)` 当缓冲区已满时返回什么？`cons.try_pop()` 当缓冲区为空时返回什么？

> **答案**：`try_push` 满时返回 `Err(item)`（把无法写入的元素原样还给你）；`try_pop` 空时返回 `None`。两者都**不会阻塞**，这正体现了"无锁"。

**练习 3**：根据 [lib.rs:L106-L148](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L106-L148)，一个 ringbuf 实例由哪三部分组成？

> **答案**：Storage（存储区，真正存放元素的地方）、Indices（read/write 两个索引）、Hold flags（用于强制 SPSC 约束的持有标志）。

### 4.2 ringbuf 的核心特性：读懂特性清单

#### 4.2.1 概念说明

README 用一个 bullet 列表列出了 ringbuf 的全部特性。理解这个清单，就理解了 ringbuf 区别于"随便一个队列"的价值所在。这些特性大致可以分成四组：

| 分组 | 特性 | 通俗解释 |
| --- | --- | --- |
| **并发模型** | 无锁操作 | 操作立即成功或失败，不阻塞、不等待。 |
| **类型与批量** | 任意元素类型（不要求 `Copy`）；单个或批量插入/取出 | 能存任何类型，还能一次搬一批以减少同步开销。 |
| **性能** | 直接访问内部内存；`Read`/`Write` 实现 | 能拿到内部切片直接读写，省掉逐元素拷贝；还能当字节流用。 |
| **存储与平台** | 多种缓冲区与存储后端；可不用 `std`、甚至不用 `alloc`；async/blocking 版本；可选 `portable-atomic` | 从桌面到嵌入式都能用。 |

其中**overwrite（覆盖写入）**是一个独特模式：当缓冲区已满时，新写入不会失败，而是**挤掉最旧的一个元素**再写入。这适合"只关心最新数据"的场景（比如实时显示最新的 N 个采样点）。

#### 4.2.2 核心流程

不同特性在 API 上有不同的"流程表现"：

- **无锁**：`try_push` / `try_pop` 永不阻塞（见 4.1.2）。
- **批量**：用 `push_slice` / `pop_slice`（`Copy` 类型一次搬一段切片）或 `push_iter` / `pop_iter`（迭代器批量）。README 在性能小节明确推荐批量操作以减少跨核同步次数：
  [README.md:L56-L59](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/README.md#L56-L59)。
- **直接访问**：通过 `vacant_slices_mut`（空闲内存）和 `occupied_slices`（已占用内存）拿到内部切片，写完再 `advance_*_index` 提交。
- **overwrite**：调用 `push_overwrite(item)`，满时会返回被挤掉的旧元素。

overwrite 模式有一个重要限制，README 也点明了：[README.md:L120-L121](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/README.md#L120-L121) —— `push_overwrite` **需要独占访问**，因为它会移动 read 索引（消费端的领地），与 SPSC 约束冲突，所以并发使用时必须自己加锁保护。

#### 4.2.3 源码精读

**特性清单本体**

[README.md:L23-L34](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/README.md#L23-L34) 就是那条完整的 Features bullet 列表。逐条对应：

```
+ Lock-free operations - ... without blocking or waiting.   ← 无锁
+ Arbitrary item type (not only `Copy`).                    ← 任意类型
+ Items can be inserted and removed one by one or many at once.  ← 批量
+ Thread-safe direct access to the internal ring buffer memory.  ← 直接访问
+ `Read` and `Write` implementation.                        ← io 集成
+ Overwriting insertion support.                            ← overwrite
+ Different types of buffers and underlying storages.       ← 多存储后端
+ Can be used without `std` and even without `alloc`.       ← no_std / no-alloc
+ Async and blocking versions.                              ← 派生 crate
+ Can optionally use the `portable-atomic` crate.           ← 小型系统支持
```

**性能取舍说明**

[README.md:L56-L61](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/README.md#L56-L61) 解释了核心权衡：多线程版 `SharedRb` 需要跨 CPU 核同步缓存，有开销；单线程版 `LocalRb` 因为不需要这种同步，会**稍快一些**。这是后续选择 `LocalRb` 还是 `SharedRb` 的依据。

**缓冲区类型一览**

[README.md:L43-L52](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/README.md#L43-L52) 列出了开箱即用的缓冲区类型：

- `LocalRb`：仅单线程。
- `SharedRb`：可跨线程；它的两个常用实例是 `HeapRb`（堆内存，最推荐）和 `StaticRb`（静态内存）。

#### 4.2.4 代码实践

**实践目标**：从源码里亲手"摘出"特性，而不是死记硬背。

**操作步骤**：

1. 打开 [README.md:L23-L34](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/README.md#L23-L34)。
2. 至少挑出 **4 条**特性，用你自己的话（中文）各写一句"它对我有什么用"。
3. 例如：把 "Overwriting insertion support" 改写成——"缓冲区满时，写入会自动丢弃最旧的元素，适合只关心最新数据的场景"。

**需要观察的现象**：你会发现特性之间并非孤立，而是层层递进——从"能做什么"（无锁队列）到"做得怎么样"（批量、直接访问降开销），再到"在哪里能做"（`no_std`、嵌入式）。

**预期结果**：你能口头复述至少 4 条特性及其价值。**待本地验证**：如果你身边有同事，试着不看资料向他/她介绍 ringbuf 的 3 个亮点。

#### 4.2.5 小练习与答案

**练习 1**：README 说 ringbuf 支持的元素类型有什么特点？是否要求元素必须实现 `Copy`？

> **答案**：支持任意元素类型，**不要求** `Copy`。这意味着你可以存放 `String`、自定义结构体等需要 drop 的类型。

**练习 2**：单线程场景下，README 推荐用 `LocalRb` 而非 `SharedRb`，原因是什么？

> **答案**：`SharedRb` 需要跨 CPU 核同步缓存（见 [README.md:L56-L57](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/README.md#L56-L57)），有额外开销；`LocalRb` 没有这种同步，所以单线程下稍快（[README.md:L61-L61](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/README.md#L61-L61)）。

**练习 3**：`push_overwrite` 为什么不能在已经 `split` 之后随便并发调用？

> **答案**：因为 overwrite 满时要移动 read 索引，而 read 索引属于消费者一方；在 SPSC 模型里这会破坏"只有一个消费者"的不变量。它需要独占访问，并发时必须自己用锁保护（[README.md:L120-L121](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/README.md#L120-L121)）。

### 4.3 三个 crate 的关系与顶层导出

#### 4.3.1 概念说明

ringbuf 不是一个孤立的 crate，而是一个 **Cargo workspace（工作区）**，由三个 crate 组成：

1. **核心 crate `ringbuf`**：提供无锁 SPSC 环形缓冲区的全部基础能力（`LocalRb`、`SharedRb`、各种 trait 和包装器）。它只提供**非阻塞**的 `try_*` 接口。
2. **派生 crate `async-ringbuf`**：在核心之上加 **async/await 同步**——写满时 `push().await` 会挂起，直到消费者腾出空间。
3. **派生 crate `ringbuf-blocking`**：在核心之上加**阻塞同步**——写满时 `push` 会阻塞当前线程，可带超时。

为什么要这样拆？因为"是否阻塞/异步"是两种不同的编程范式，把它们做成可选的派生 crate，可以让**只想要非阻塞核心的人**不必拖入一整套 async 运行时依赖。这就是"核心 + 派生"分层的好处。

README 末尾列出了这两个派生 crate：[README.md:L123-L126](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/README.md#L123-L126)。

#### 4.3.2 核心流程

三个 crate 的依赖关系是一个简单的"倒金字塔"：

```
        async-ringbuf              ringbuf-blocking
（async 同步，依赖核心）          （阻塞同步，依赖核心）
                \                    /
                 \                  /
                  \                /
                   v              v
                 ┌──────────────────┐
                 │   ringbuf (核心)  │  ← 无锁 SPSC 核心，非阻塞
                 └──────────────────┘
```

- 派生 crate 通过 `workspace.dependencies` 指向**同一个**本地核心 crate（路径依赖 `path = "."`），保证三者版本一致、共享同一份核心代码。
- 你作为最终用户，根据需要只引入其中一个或两个 crate。

> 后续讲义 u1-l3 会用 `Cargo.toml` 详细拆解这条依赖链；本讲你只需建立"核心在下、派生在上"的直觉。

#### 4.3.3 源码精读

**workspace 与核心 crate 元信息**

[Cargo.toml:L12-L13](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L12-L13) 声明了 workspace 的两个 member crate：`async` 和 `blocking`（核心 crate 本身就是 workspace 根）。[Cargo.toml:L16-L16](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L16-L16) 与 [Cargo.toml:L20-L20](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L20-L20) 给出核心 crate 的名字 `ringbuf` 和描述（与 README 那句定位一致）。

**核心 crate 的模块结构**

[src/lib.rs:L158-L171](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L158-L171) 声明了核心 crate 的全部模块。注意有的带 `pub`（对外公开），有的不带（内部实现）：

| 模块 | 是否公开 | 作用 |
| --- | --- | --- |
| `alias` | 否（通过 `pub use` 重导出） | 常用类型别名 |
| `rb` | 是 | 环形缓冲区实现（`LocalRb` / `SharedRb`） |
| `storage` | 是 | 存储后端 |
| `traits` | 是 | 核心 trait（Observer/Producer/Consumer 等） |
| `transfer` | 否（通过 `pub use` 重导出） | 缓冲区间数据搬运 |
| `utils` | 否 | 内部工具 |
| `wrap` | 是 | Producer/Consumer 包装器实现 |

**顶层导出的类型**

[src/lib.rs:L176-L180](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L176-L180) 是核心 crate 对外"直接甩到顶层"的类型，这也是你在 `cargo doc` 首页能直接看到的名字：

- `pub use alias::*`：导出所有别名（见下）。
- `pub use rb::{LocalRb, SharedRb}`：两种核心缓冲区。
- `pub use traits::{consumer, producer}`：消费/生产 trait 模块。
- `pub use transfer::transfer`：缓冲区间搬运函数。
- `pub use wrap::{CachingCons, CachingProd, Cons, Obs, Prod}`：常用包装器类型。

**别名文件：为什么有 `HeapRb` / `StaticRb`**

[src/alias.rs:L17-L17](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L17-L17) 定义 `StaticRb`：`pub type StaticRb<T, const N: usize> = SharedRb<Array<T, N>>;`——即"用静态数组 `Array` 作存储的 `SharedRb`"。
[src/alias.rs:L27-L27](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L27-L27) 定义 `HeapRb`：`pub type HeapRb<T> = SharedRb<Heap<T>>;`——即"用堆 `Heap` 作存储的 `SharedRb`"。

> 你现在已经能看到 ringbuf 的核心设计思想了：`SharedRb<S>` 是一个泛型结构，`S` 是"存储后端"。换不同的 `S` 就得到不同类型的缓冲区。这是"多种存储后端"特性的根源，u2-l2 会专门讲。

**no_std 的铁证**

[src/lib.rs:L149-L149](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L149-L149) 的 `#![no_std]` 说明核心 crate **默认不依赖标准库**，`std` 只是一个可选 feature（[Cargo.toml:L28-L28](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L28-L28) 默认开启 `std`，但可以关掉）。这正是"可用于 `no_std`"特性的来源。

#### 4.3.4 代码实践

**实践目标**：用 `cargo doc` 把文档跑起来，亲眼看到顶层导出的类型，建立"代码—文档"的对应关系。

**操作步骤**：

1. 在项目根目录执行：
   ```bash
   cargo doc --no-deps --open
   ```
   - `--no-deps` 只生成当前 crate 的文档（不生成依赖的文档，更快）。
   - `--open` 生成后自动用浏览器打开。
2. 在打开的文档首页，对照 [src/lib.rs:L176-L180](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L176-L180)，找到这些顶层类型：`HeapRb`、`StaticRb`、`LocalRb`、`SharedRb`、`Prod`、`Cons`。
3. 点进 `HeapRb`，确认它的定义确实是 `SharedRb<Heap<T>>`（对应 [alias.rs:L27-L27](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L27-L27)）。

**需要观察的现象**：文档首页的"Re-exports"区域会列出 `lib.rs` 里 `pub use` 的全部类型；点开 `HeapRb` 会跳转到 `SharedRb`，说明它只是个别名。

**预期结果**：你能在文档里找到本讲提到的每一个类型名，并理解它们都"最终指向 `SharedRb`"。**待本地验证**：`cargo doc` 的具体输出取决于本机工具链版本；如果 `--open` 在无图形环境的服务器上无效，去掉 `--open` 后到 `target/doc/ringbuf/index.html` 手动查看。

#### 4.3.5 小练习与答案

**练习 1**：这个 workspace 有几个 crate？分别叫什么？

> **答案**：三个。核心 crate `ringbuf`，派生 crate `async-ringbuf`（目录 `async/`）和 `ringbuf-blocking`（目录 `blocking/`）。其中 workspace 的 member 是 `async` 和 `blocking`，核心 crate 本身是 workspace 根（[Cargo.toml:L12-L13](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L12-L13)）。

**练习 2**：`HeapRb<T>` 和 `StaticRb<T, N>` 在底层其实是同一个泛型类型的不同实例。这个泛型类型是谁？它们的区别体现在哪个泛型参数上？

> **答案**：底层都是 `SharedRb<S>`。区别在存储后端 `S`：`HeapRb` 用 `Heap<T>`，`StaticRb` 用 `Array<T, N>`（见 [alias.rs:L17-L17](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L17-L17) 与 [alias.rs:L27-L27](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L27-L27)）。

**练习 3**：核心 crate `ringbuf` 默认是 `#![no_std]` 的吗？那为什么我们还能用它做带堆分配的程序？

> **答案**：是 `#![no_std]` 的（[src/lib.rs:L149-L149](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L149-L149)）。但默认 feature 开启了 `std`（[Cargo.toml:L28-L28](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L28-L28)），`std` 会带上 `alloc`，从而启用 `HeapRb` 等需要堆分配的类型。所以"no_std 的代码"和"能用于普通带堆程序"并不矛盾——是否使用 std 由 feature 控制。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**阅读 + 文档**型小任务：

1. **一句话定位**：打开 [README.md:L21-L21](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/README.md#L21-L21)，用一句中文写下 ringbuf 解决什么问题。要求涵盖"无锁""SPSC""FIFO""环形缓冲区"这几个关键词。

2. **特性清单**：从 [README.md:L23-L34](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/README.md#L23-L34) 至少列出 **4 条**特性，每条配一句你自己的解释。至少要包含一条"无锁"相关、一条"存储/平台"相关。

3. **浏览文档**：执行 `cargo doc --no-deps --open`，在首页的 Re-exports 区域找到 `HeapRb`，点进去确认它是 `SharedRb<Heap<T>>` 的别名。再翻到模块列表，确认存在 `rb`、`storage`、`traits`、`wrap` 四个公开模块（对照 [src/lib.rs:L158-L171](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L158-L171)）。

4. **画一张依赖图**：在纸上画出 4.3.2 那张"核心 + 派生"的三 crate 依赖关系图，并标注每个 crate 的职责（非阻塞核心 / async 同步 / 阻塞同步）。

**完成标志**：你能不看资料，用 30 秒向别人讲清"ringbuf 是什么、有什么特点、由哪几个 crate 组成"。

> 这一节没有要求运行示例程序——那是 u1-l2 的任务。如果你已经迫不及待，可以现在去试 `cargo run --example simple`，但理解其输出需要 u1-l2 的知识。

## 6. 本讲小结

- ringbuf 是一个**无锁、SPSC、FIFO 的环形缓冲区**，核心价值是在不用锁、不阻塞的前提下，在两个执行流之间高效传递一连串元素。
- 它的典型用法是四步走：**创建 → split 拆分为 Producer/Consumer → 生产者 `try_push` → 消费者 `try_pop`**，读写都是"立即成功或立即失败"。
- 核心特性包括：无锁、任意元素类型（不要求 `Copy`）、单个/批量操作、直接访问内部内存、`Read`/`Write` 集成、**overwrite 覆盖写入**、多种存储后端、`no_std`/`no-alloc` 支持。
- 单线程用 `LocalRb`（更快，无缓存同步），多线程用 `SharedRb`（常用实例 `HeapRb` 堆、`StaticRb` 静态）。
- 项目是一个 **Cargo workspace**：核心 crate `ringbuf` + 派生 crate `async-ringbuf`（async 同步）和 `ringbuf-blocking`（阻塞同步），派生 crate 复用同一个核心。
- 核心代码结构清晰：`Storage`（存储区）+ `Indices`（读写索引）+ `Hold flags`（SPSC 约束标志）三部分；对外通过 `lib.rs` 的 `pub use` 导出 `HeapRb`/`StaticRb`/`Prod`/`Cons` 等顶层类型。

## 7. 下一步学习建议

本讲只是"认识项目"。建议接下来：

- **u1-l2《快速上手：HeapRb 的创建、拆分与 push/pop》**：亲手写出第一段 ringbuf 代码，把本讲的四步流程跑起来，观察"满了返回 `Err`、空了返回 `None`"的真实行为。这是最自然的下一步。
- 在跑代码之前，可以先扫一眼 [examples/simple.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/simple.rs) 和 [examples/overwrite.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/overwrite.rs)，它们就是 u1-l2 / u3-l4 的实践素材。
- 如果你对"为什么索引要模 `2*capacity`""存储后端是怎么回事"好奇，可以直接跳到第二单元（u2）——但建议先做完 u1 的快速上手，带着感性认识再去啃原理会更顺。
