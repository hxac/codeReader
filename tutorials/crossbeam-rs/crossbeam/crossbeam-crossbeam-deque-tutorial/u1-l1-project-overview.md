# 项目概览：什么是 work-stealing deque

## 1. 本讲目标

本讲是整本 `crossbeam-deque` 学习手册的第一篇，目标是让你**先从外部认识这个 crate**，再深入源码。

学完后你应当能够：

1. 用自己的话解释什么是 **work-stealing（工作窃取）调度模型**，以及它为什么需要三种队列角色：**Worker / Stealer / Injector**。
2. 说出 `crossbeam-deque` 的版本（0.8.6）、最低支持 Rust 版本（MSRV = 1.74）、许可证（MIT / Apache-2.0 二选一）和它的两个核心依赖（`crossbeam-epoch`、`crossbeam-utils`）。
3. 能够新建一个 Cargo 二进制项目，把 `crossbeam-deque = "0.8"` 加进依赖，并用 `Worker::new_lifo()` 完成「push 三个整数、再依次 pop 出来」的最小可运行例子。
4. 看 `CHANGELOG.md` 时，能分辨出 0.7 → 0.8 这条主线上发生了哪些关键 API 变化（例如 `Injector` 的引入、`new_fifo`/`new_lifo` 取代了旧的 `fifo()`/`lifo()`）。

本讲**不**深入无锁算法、内存序或 `epoch` 回收——这些是后面进阶和专家层讲义的内容。本讲只做一件事：**建立一个清晰的项目地图，让你知道它是什么、怎么用、版本怎么演进的。**

## 2. 前置知识

在读这篇讲义之前，建议你先具备下面这些背景知识（不熟悉也没关系，下面会顺带解释）：

- **Rust 基础**：会写一个最小的 `cargo new` 项目，会读 `Cargo.toml`，知道 `Option<T>`、泛型、trait 的基本概念。
- **双端队列（deque，发音 "deck"）**：一种两端都能进出的线性数据结构。普通队列只能「队尾进、队头出」（FIFO），而 deque 允许两头都操作。
- **FIFO 与 LIFO**：
  - FIFO = First In First Out（先进先出），就像排队买饭，先来的先服务。
  - LIFO = Last In First Out（后进先出），就像一摞盘子，最后放上去的先被拿走。
- **多线程并发**：知道「多个线程同时访问同一份数据」会带来数据竞争（data race），需要同步手段保护。

> 名词速查：
> - **lock-free（无锁）**：不使用互斥锁（mutex），而是用原子操作（atomic）协调多线程。`crossbeam-deque` 就是一套无锁双端队列。
> - **MSRV**：Minimum Supported Rust Version，最低支持的 Rust 版本。本 crate 是 1.74。

## 3. 本讲源码地图

本讲涉及的关键文件都很短、很「元数据」，但信息量很大。先建立这张地图：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `README.md` | 面向用户的项目说明 | Usage（如何加依赖）、Compatibility（MSRV） |
| `Cargo.toml` | crate 的元数据与依赖清单 | 版本、依赖、`std` feature |
| `CHANGELOG.md` | 版本演进记录 | 0.7 → 0.8 的关键 API 变化 |
| `src/lib.rs` | 库入口与模块级文档 | 顶部关于 work-stealing 拓扑的说明、`find_task` 示例 |
| `src/deque.rs` | 全部实现所在（约 2200 行） | 本讲只看 `Worker` 结构体的定义与构造方法 |

> 重要事实：整个 crate 的**全部实现都集中在一个文件** `src/deque.rs` 里。这决定了本手册的讲解节奏——后面几乎所有进阶讲义都在引用这同一个文件的不同行段。本讲只取其中最顶层的 `Worker` 定义，让你先有个直观印象。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1** work-stealing 调度模型与三种队列角色
- **4.2** `README.md`：项目定位、Usage 与 Compatibility
- **4.3** `Cargo.toml`：版本、依赖与 `std` feature
- **4.4** `CHANGELOG.md`：从 0.1 到 0.8.6 的演进

### 4.1 work-stealing 调度模型与三种队列角色

#### 4.1.1 概念说明

设想你在写一个**任务调度器**（task scheduler），比如 Rayon、Tokio、Go runtime 这类并行运行时。你有一堆工作线程，每个线程手里都有一摞「待办任务」。

最朴素的方案是**一个全局共享队列**，所有线程都从这一个队列里抢任务。问题在于：这个共享队列会成为瓶颈，线程一多就互相挤。

**work-stealing（工作窃取）** 给出了一个更聪明的拓扑：

- 每个工作线程拥有一个**本地队列**，它往自己队列里放任务、从自己队列里取任务，这一步**没有竞争**（因为只有它自己碰这个队列）。
- 当某个线程的本地队列**空了**（它闲下来了），它就去看别的线程的队列，**「偷」**几个任务过来执行。

「偷」是关键：繁忙的线程会把自己的任务分给空闲的线程，从而自动实现负载均衡。

为了支持这套模型，`crossbeam-deque` 提供了**三种角色**的队列：

| 角色 | 形态 | 归属 | 典型用途 |
|------|------|------|----------|
| **Worker** | 每线程一个 | 单线程独占 | 本地任务队列，只能 `push` / `pop` |
| **Stealer** | 由 `Worker` 派生 | 可跨线程共享、可 `Clone` | 让别的线程能从该 `Worker` **偷**任务 |
| **Injector** | 全局唯一 | 多线程共享（MPMC） | 外部往调度器里**注入**新任务的入口 |

`src/lib.rs` 的模块级文档（注释）用一段话精确描述了这个拓扑：

> 「The typical setup involves a number of threads, each having its own FIFO or LIFO queue (*worker*). There is also one global FIFO queue (*injector*) and a list of references to *worker* queues that are able to steal tasks (*stealers*).」

#### 4.1.2 核心流程

一个典型的 work-stealing 调度循环是这样的（伪代码）：

```
loop {
    1. 从本地 Worker 队列 pop 一个任务；拿到就执行，回到 1。
    2. 本地空了 → 尝试从全局 Injector「批量偷」一批任务塞回本地，并顺手 pop 一个执行。
    3. Injector 也空了 → 遍历其它线程的 Stealer 列表，挨个偷一个任务。
    4. 全都偷不到 → 线程进入空闲/休眠，等待被唤醒。
}
```

注意第 2、3 步的「偷」操作可能**假性失败**（返回 `Steal::Retry`），需要重试——这点会在后面的 `Steal` 讲义里细讲，本讲先建立印象即可。

#### 4.1.3 源码精读

`Worker` 结构体的定义在 [src/deque.rs:197-209](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L197-L209)。注意最后一个字段 `_marker: PhantomData<*mut ()>`，它的作用是**把 `Worker` 标记为 `!Send + !Sync`**——也就是 `Worker` 不能跨线程共享，只能被单个线程拥有。这正是上面「本地队列只有自己碰」的体现。

模块级文档对三种角色和「偷」的三种变体（`steal` / `steal_batch` / `steal_batch_and_pop`）的权威说明在 [src/lib.rs:1-83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L1-L83)，其中还包含一个完整的 `find_task` 示例（本手册 u1-l4 会专门讲它，本讲先了解存在即可）。

关键片段（节选自 [src/deque.rs:157-209](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L157-L209)）：

```rust
/// A worker queue.
///
/// This is a FIFO or LIFO queue that is owned by a single thread, but other threads may steal
/// tasks from it. Task schedulers typically create a single worker queue per thread.
pub struct Worker<T> {
    /// A reference to the inner representation of the queue.
    inner: Arc<CachePadded<Inner<T>>>,
    /// A copy of `inner.buffer` for quick access.
    buffer: Cell<Buffer<T>>,
    /// The flavor of the queue.
    flavor: Flavor,
    /// Indicates that the worker cannot be shared among threads.
    _marker: PhantomData<*mut ()>, // !Send + !Sync
}
```

> 这里出现了 `Arc`、`CachePadded`、`Inner`、`Buffer`、`Flavor` 等类型。本讲只需记住：`Worker` 通过 `Arc` 与它的 `Stealer` **共享同一份底层状态**（`Inner`），这就是「派生出的 Stealer 能偷到 Worker 的任务」的物理基础。这些类型的细节留给进阶层讲义（u2-l1）。

#### 4.1.4 代码实践

**实践目标**：建立「三种角色」的直觉，先不追求能跑通，只读文档。

**操作步骤**：

1. 打开 [src/lib.rs:1-48](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L1-L48)，通读 `# Queues` 和 `# Stealing` 两节。
2. 在注释里用自己的话回答两个问题：
   - `Worker` 和 `Injector` 谁是 FIFO、谁是 LIFO？
   - 偷操作有哪三种变体？

**预期结果（你可以对照下面的参考答案）**：

- `Injector` 总是 FIFO；`Worker` 既可以是 FIFO（`new_fifo`）也可以是 LIFO（`new_lifo`）。
- 三种偷：`steal`（偷一个）、`steal_batch`（偷一批塞进另一个 worker）、`steal_batch_and_pop`（偷一批塞进去并顺手 pop 一个）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 work-stealing 调度器不直接用一个全局共享队列？

> **参考答案**：全局共享队列是所有线程的竞争焦点，线程数一多，锁/原子操作的争用会成为吞吐瓶颈。给每个线程配本地队列后，线程大部分时候只碰自己的队列（无竞争），只有空闲时才去偷，竞争被摊薄到「偷」这一相对低频的操作上。

**练习 2**：`Worker` 被标记为 `!Send + !Sync`，这对调度器设计意味着什么？

> **参考答案**：一个 `Worker` 不能被移动或共享到另一个线程，因此调度器里**每个线程各自创建并独占一个 `Worker`**，而不是多个线程共用一个。线程间共享的是 `Worker` 派生出的 `Stealer`（`!Send + !Sync` 的限制不适用于 `Stealer`）。

### 4.2 README.md：项目定位、Usage 与 Compatibility

#### 4.2.1 概念说明

`README.md` 是你认识一个 crate 的第一站。它非常短（不到 50 行），但回答了三个最关键的问题：

1. **这个 crate 是干什么的？**
2. **怎么把它加到我的项目里？**
3. **它要求什么 Rust 版本、什么许可证？**

对 `crossbeam-deque`，第一行就开门见山：

> 「This crate provides work-stealing deques, which are primarily intended for building task schedulers.」

翻译：本 crate 提供 work-stealing 双端队列，主要用于构建任务调度器。

#### 4.2.2 核心流程

`README.md` 的结构很简洁：

```
标题 + 徽章（CI / License / crates.io / docs.rs / Rust 版本 / Discord）
├── 一句话定位（work-stealing deques，用于 task schedulers）
├── Usage        → 怎么加依赖
├── Compatibility → MSRV 政策与当前值
└── License      → MIT 或 Apache-2.0 二选一
```

其中「Usage」告诉你依赖怎么写，「Compatibility」告诉你 MSRV 的**政策**（往后兼容至少 6 个月的 stable Rust，每提高一次 MSRV 就发一个新 minor 版本）。

#### 4.2.3 源码精读

**Usage 段**（[README.md:18-25](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/README.md#L18-L25)）：告诉你把下面这段加进 `Cargo.toml`：

```toml
[dependencies]
crossbeam-deque = "0.8"
```

注意写的是 `"0.8"` 而不是 `"0.8.6"`。在 Cargo 的 SemVer 规则里，`"0.8"` 等价于 `^0.8`，会自动取 `0.8.x` 的最新版（当前即 0.8.6）。

**Compatibility 段**（[README.md:27-31](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/README.md#L27-L31)）：明确写出「minimum supported Rust version is 1.74」，并解释了 MSRV 的升级政策。这一段和 `Cargo.toml` 里的 `rust-version = "1.74"` 是**手动保持同步**的（`Cargo.toml` 里有注释提醒「Sync with msrv badge and "Compatibility" section in README.md」）。

**徽章**（[README.md:1-13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/README.md#L1-L13)）：从徽章可以一眼看到许可证是 `MIT OR Apache-2.0`、MSRV 是 `1.74+`、crate 在 crates.io 上的版本号。

#### 4.2.4 代码实践

**实践目标**：确认你的环境满足运行条件。

**操作步骤**：

1. 运行 `rustc --version`，确认版本号 ≥ 1.74。
2. 浏览 [README.md:33-40](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/README.md#L33-L40) 的 License 段，记住是双许可证（任选其一）。

**需要观察的现象**：

- 若 `rustc` 版本低于 1.74，则本 crate 无法编译——这是后续所有实践的前提。
- 双许可证意味着你在自己项目里使用时，可以**任选** MIT 或 Apache-2.0 来遵守，无需同时满足。

**预期结果**：`rustc --version` 输出形如 `rustc 1.7x.0 (...)`，版本号 ≥ 1.74。

#### 4.2.5 小练习与答案

**练习 1**：在 `Cargo.toml` 里写 `crossbeam-deque = "0.8"` 会装到哪个具体版本？为什么？

> **参考答案**：会装到 `0.8.x` 的最新版本（当前 0.8.6）。因为 Cargo 把 `"0.8"` 解释为 `^0.8`，对 `0.x.y` 的 `^0.x` 允许 `0.x` 内的任意补丁/次版本更新。

**练习 2**：如果 `crossbeam-deque` 在某个新版本把 MSRV 提到 1.80，按它的政策，版本号会怎么变？

> **参考答案**：会发布一个新的 **minor** 版本（例如 0.8.7 → 0.9.0）。README 的 Compatibility 段明确说「every time the minimum supported Rust version is increased, a new minor version is released」。

### 4.3 Cargo.toml：版本、依赖与 std feature

#### 4.3.1 概念说明

`Cargo.toml` 是 crate 的「身份证 + 配料表」。对 `crossbeam-deque`，它能回答：

- 当前版本？ → `0.8.6`
- 依赖了什么？ → `crossbeam-epoch`（无锁内存回收）和 `crossbeam-utils`（并发原语）
- 有没有 feature 开关？ → 有 `std`，且**默认开启**

`crossbeam-epoch` 是理解本 crate 内存安全的关键依赖：因为无锁代码里，一个线程换掉 buffer 后，别的线程可能还拿着旧指针在读，不能立即释放，必须用 **epoch-based reclamation（基于纪元的回收）** 延迟回收。这个机制会在专家层讲义（u4-l2）专门讲，本讲只需知道「依赖里有它」。

#### 4.3.2 核心流程

`Cargo.toml` 的信息可以归成四块：

```
[package]        → name / version / edition / rust-version / license / 关键词
[package.metadata.*] → docs.rs 与类型检查的元配置
[features]       → default = ["std"]，且 std 不可禁用（注释说明）
[dependencies]   → crossbeam-epoch 0.9.17 + crossbeam-utils 0.8.18（均来自同仓库 path）
[dev-dependencies] → fastrand 2（仅测试用）
```

#### 4.3.3 源码精读

**包元数据**（[Cargo.toml:1-16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/Cargo.toml#L1-L16)）：可以看到 `version = "0.8.6"`、`edition = "2021"`、`rust-version = "1.74"`、`license = "MIT OR Apache-2.0"`，关键词是 `chase-lev / lock-free / scheduler / scheduling`——这四个词精确点出了本 crate 的算法血统（**Chase-Lev deque**，一种经典的无锁双端队列算法）。

> 顺带一提：发布新版本时要同步更新 `CHANGELOG.md` 和（必要时）`README.md`，再用仓库根目录的 `./tools/publish.sh crossbeam-deque <version>` 发布——这些注释就写在 [Cargo.toml:3-6](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/Cargo.toml#L3-L6)。

**features 段**（[Cargo.toml:26-33](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/Cargo.toml#L26-L33)）：

```toml
[features]
default = ["std"]
# Enable to use APIs that require `std`.
# This is enabled by default.
#
# NOTE: Disabling `std` feature is not supported yet.
std = ["crossbeam-epoch/std", "crossbeam-utils/std"]
```

关键点：**虽然代码顶层写了 `#![no_std]`，但 `std` feature 是默认开启且目前不可禁用的**。注释 `NOTE: Disabling 'std' feature is not supported yet.` 明确说明这一点。u1-l2 会讲为什么 `lib.rs` 里用 `#[cfg(feature = "std")]` 把整个 `deque` 模块包起来。

**依赖段**（[Cargo.toml:35-37](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/Cargo.toml#L35-L37)）：两个依赖都用 `path = "../crossbeam-epoch"` 这种**路径依赖**，因为 `crossbeam-rs` 是一个 monorepo，几个 crate 放在同一个仓库里协同开发。发布到 crates.io 时 path 会被替换成版本号要求。

#### 4.3.4 代码实践

**实践目标**：亲手把依赖加进一个新项目（这是综合实践的前置小步）。

**操作步骤**：

1. 在任意目录执行 `cargo new deque-demo`，进入目录。
2. 在生成的 `Cargo.toml` 的 `[dependencies]` 下加一行 `crossbeam-deque = "0.8"`。
3. 执行 `cargo build`。

**需要观察的现象**：Cargo 会拉取 `crossbeam-deque 0.8.6` 以及它的传递依赖 `crossbeam-epoch` 和 `crossbeam-utils`。

**预期结果**：编译成功，`cargo tree` 里能看到 `crossbeam-deque v0.8.6`、`crossbeam-epoch v0.9.x`、`crossbeam-utils v0.8.x`。

> 如果拉取失败，多半是网络问题或缓存问题，与代码无关——明确写「待本地验证网络与镜像配置」。

#### 4.3.5 小练习与答案

**练习 1**：`crossbeam-deque` 顶层声明了 `#![no_std]`，却又默认开启 `std` feature，这不矛盾吗？

> **参考答案**：不矛盾。`#![no_std]` 只是让 crate **不自动链接 `std`** prelude；`std` feature 是另一回事——它控制是否启用「需要 `std`/`alloc` 的 API」。本 crate 的核心队列用了堆分配和线程原语，所以目前 `std` 不可禁用（注释 `Disabling 'std' feature is not supported yet.`）。这是一种「为将来可能的 `no_std` 支持预留 feature」的前向兼容设计。

**练习 2**：为什么两个依赖写成 `path = "../crossbeam-epoch"` 而不是版本号？

> **参考答案**：因为它们同属 `crossbeam-rs` 这个 monorepo，开发时需要协同改动、相互引用本地最新代码。`path` 依赖让本地开发无需先发布到 crates.io。真正发布到 crates.io 时，`cargo publish` 会把 path 转成对应的版本号要求。

### 4.4 CHANGELOG.md：从 0.1 到 0.8.6 的演进

#### 4.4.1 概念说明

`CHANGELOG.md` 是一份「这个 crate 长大成了今天的样子」的编年史。读它能帮你：

1. 理解**今天 API 为什么长这样**——很多设计是历史演化的结果。
2. 看出**哪些版本被 yanked（撤回）了**以及为什么（`crossbeam-deque` 历史上有多版因同一个安全问题 `GHSA-pqqp-xmhj-wgcw` 被 yank）。
3. 知道**0.7 → 0.8 这条主线**上引入了哪些关键能力（`Injector`、批量偷取、`Steal` 组合子）。

#### 4.4.2 核心流程

把 CHANGELOG 当成一张时间线，按「里程碑版本」读最有价值：

```
0.1.0  Chase-Lev deque 的首次实现
0.5.0  把 Deque 改名 Worker，steal 返回 Option<T>，引入 fifo()/lifo()
0.6.0  引入批量偷取 Stealer::steal_many，pop 返回 Pop<T> 以手动处理自旋
0.7.0  ★ 大改：new_fifo/new_lifo 取代 fifo()/lifo()、引入 Injector(MPMC)、
       Steal::Data 改名 Success、加入 or_else/FromIterator、#[must_use]
0.8.0  (yanked) 加 Worker::len / Injector::len、引入 std feature
0.8.1  修 steal 竞争条件、加 Stealer::len
0.8.3  加 _with_limit 系列批量偷取
0.8.4  MSRV → 1.61
0.8.5  去掉 cfg-if 依赖
0.8.6  ★ 修向 Injector push 大对象时的栈溢出（#1146/#1147/#1159）
```

> 标 ★ 的是本讲建议重点理解的两个节点：0.7.0 塑造了今天的 API 骨架，0.8.6 是你正在学习的当前版本的重要修复。

#### 4.4.3 源码精读

**0.7.0 的关键变化**（[CHANGELOG.md:59-69](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/CHANGELOG.md#L59-L69)）：这一版是今天 API 的「定型点」。值得注意的是：

- 「Replace `fifo()` and `lifo()` with `Worker::new_fifo()` and `Worker::new_lifo()`」——这就是你今天在 [src/deque.rs:225](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L225) 看到的 `new_fifo` / `new_lifo` 构造方法的来源。
- 「Introduce `Injector<T>`, a MPMC queue」——这是三大角色之一 `Injector` 的诞生。MPMC = Multiple Producer Multiple Consumer。
- 「Rename `Steal::Data` to `Steal::Success`」——今天你看到的 `Steal::Success(...)` 是从 0.7.0 改名来的。
- 「Add `Steal::or_else()` and implement `FromIterator` for `Steal`」——这让 `find_task` 那种「回退链」写法成为可能（src/lib.rs 的示例就用了它们）。

**0.8.x 主线**（[CHANGELOG.md:1-34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/CHANGELOG.md#L1-L34)）：

- 0.8.0 引入了 `len()` 方法和 `std` feature（[CHANGELOG.md:27-33](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/CHANGELOG.md#L27-L33)），但**此版被 yanked**。
- 0.8.3 加了 `_with_limit` 变体（[CHANGELOG.md:13-16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/CHANGELOG.md#L13-L16)）——即 `steal_batch_with_limit` / `steal_batch_with_limit_and_pop`。
- 当前版本 0.8.6（[CHANGELOG.md:1-3](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/CHANGELOG.md#L1-L3)）修复了「向 `Injector` push 大对象时栈溢出」的 bug，链接到 PR #1146/#1147/#1159。

**关于 yank**：CHANGELOG 中 0.7.0–0.8.0 多个版本都标注了被 yank，并指向同一个安全公告 [GHSA-pqqp-xmhj-wgcw](https://github.com/crossbeam-rs/crossbeam/security/advisories/GHSA-pqqp-xmhj-wgcw)。`cargo` 不会自动选择被 yank 的版本，所以你用 `"0.8"` 拿到的总是安全的 0.8.6。这是个很好的工程习惯示范：发现问题→修复→yank 旧版→公告。

#### 4.4.4 代码实践

**实践目标**：把 CHANGELOG 当「设计考古」工具，理解一个 API 的来历。

**操作步骤**：

1. 打开 [CHANGELOG.md:59-69](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/CHANGELOG.md#L59-L69)（0.7.0 条目）。
2. 找到「Introduce `Injector<T>`」这一行，对照 [src/lib.rs:12-16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L12-L16) 里对 `Injector` 的描述，确认它在今天仍是「全局 FIFO 入口」。
3. 回答：在 0.7.0 之前，`Steal::Success` 叫什么名字？

**需要观察的现象 / 预期结果**：你会确认 `Injector` 自 0.7.0 引入后角色未变；`Steal::Success` 在 0.7.0 之前叫 `Steal::Data`。

#### 4.4.5 小练习与答案

**练习 1**：为什么用 `crossbeam-deque = "0.8"` 而不是 `= "0.8.0"`？

> **参考答案**：因为 0.8.0（以及更早的若干版本）因安全公告 `GHSA-pqqp-xmhj-wgcw` 被 yank。写 `"0.8"`（= `^0.8`）会让 Cargo 自动选到未被 yank 的最新 `0.8.x`（即 0.8.6）。如果硬写 `= "0.8.0"`，Cargo 会拒绝使用被 yank 的版本导致无法编译。

**练习 2**：`Steal::or_else` 和 `Steal` 的 `FromIterator` 是哪一版引入的？它们解决了什么问题？

> **参考答案**：0.7.0 引入（[CHANGELOG.md:68](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/CHANGELOG.md#L68)）。它们让多个可能失败的偷取操作可以**像链式调用一样组合**（`a.or_else(b).or_else(c)` 或 `iter.map(steal).collect::<Steal<_>>()`），从而优雅地写出 `find_task` 那种「本地→全局→其它线程」的回退链。

## 5. 综合实践

把本讲学的「加依赖 + 三种角色 + LIFO 语义」串起来，写一个**真正能跑**的最小例子。

**任务**：新建一个 Cargo 二进制项目，用 `Worker::new_lifo()` 创建一个 LIFO 队列，push 三个整数，再依次 pop 出来打印，验证 LIFO（后进先出）顺序。

**操作步骤**：

1. `cargo new deque-demo`，进入目录。
2. 编辑 `Cargo.toml`，确保 `[dependencies]` 下有：

   ```toml
   [dependencies]
   crossbeam-deque = "0.8"
   ```

3. 编辑 `src/main.rs` 为下面的**示例代码**（注意：这是为本讲新写的示例，不是项目原有代码）：

   ```rust
   use crossbeam_deque::Worker;

   fn main() {
       // 创建一个 LIFO 工作队列：后 push 进去的会先 pop 出来。
       let w: Worker<i32> = Worker::new_lifo();

       // 依次 push 三个整数。
       w.push(1);
       w.push(2);
       w.push(3);

       // 依次 pop，直到返回 None 表示队列空。
       while let Some(v) = w.pop() {
           println!("popped: {}", v);
       }
   }
   ```

4. `cargo run`。

**需要观察的现象 / 预期结果**：

按 LIFO 语义，最后 push 的 `3` 应最先 pop 出来，输出应当是：

```
popped: 3
popped: 2
popped: 1
```

这个顺序是**确定性的**，可以在源码里找到依据：[src/deque.rs:183-195](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L183-L195) 的 LIFO 文档示例里，push `1,2,3` 后 `pop()` 得到 `Some(3)`、再 `Some(2)`，与本实践一致。

> 进阶玩法（可选）：把 `new_lifo()` 换成 `new_fifo()`，重跑一次，观察输出变成 `1, 2, 3`（先进先出）。这就是 FIFO 与 LIFO 的核心区别，也是 u1-l3 会深入的内容。

## 6. 本讲小结

- `crossbeam-deque` 是一套**无锁（lock-free）work-stealing 双端队列**，源自经典的 **Chase-Lev** 算法，主要服务于任务调度器。
- work-stealing 模型有三角色：**Worker**（每线程独占的本地队列）、**Stealer**（可跨线程共享、用来偷任务）、**Injector**（全局 FIFO、MPMC 入口）。
- 当前版本 **0.8.6**，MSRV **1.74**，许可证 **MIT OR Apache-2.0**；依赖 `crossbeam-epoch`（无锁内存回收）与 `crossbeam-utils`。
- 全部实现集中在单文件 `src/deque.rs`（约 2200 行）；`Worker` 通过 `_marker: PhantomData<*mut ()>` 强制为 `!Send + !Sync`，即只能被单线程拥有。
- API 骨架在 **0.7.0** 定型（`new_fifo`/`new_lifo`、`Injector`、`Steal::Success`、`or_else`/`FromIterator`）；0.8.x 又补了 `len()`、`_with_limit` 批量偷取，并在 0.8.6 修复了 `Injector` 的大对象栈溢出 bug。
- `crossbeam-deque = "0.8"` 会自动选到安全的 0.8.6，因为早期多个版本因 `GHSA-pqqp-xmhj-wgcw` 被 yank。

## 7. 下一步学习建议

本讲只建立了「外部地图」。下一讲 **u1-l2《源码地图与构建配置》** 会带你走进 `src/lib.rs`，看清模块如何声明与导出、`#![no_std]` 与 `std` feature 如何配合、以及 `build.rs` 如何探测 ThreadSanitizer。

建议的阅读顺序：

1. **u1-l2**：源码地图与构建配置（`lib.rs` / `build.rs` / `alloc_helper.rs`）。
2. **u1-l3**：`Worker` 队列上手——`push`/`pop` 与 FIFO/LIFO 的出队顺序差异。
3. **u1-l4**：`Stealer`、`Injector` 与 `Steal` 结果工作流，读懂 `find_task`。

在进入 u1-l2 之前，建议你**亲手把综合实践跑通**，确认环境就绪、对 `Worker` 的 push/pop 有体感，再去看源码细节会顺很多。
