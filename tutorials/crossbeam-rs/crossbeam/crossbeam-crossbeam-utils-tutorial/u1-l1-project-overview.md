# 项目概览与定位

## 1. 本讲目标

本讲是整本《crossbeam-utils 学习手册》的第一讲，目标是让一个**从未接触过这个 crate** 的读者，在读完之后能够清楚地回答下面几个问题：

1. crossbeam-utils 是什么？它解决的是哪一类问题？
2. 它提供了哪些主要类型？这些类型大致可以分成哪几个功能类别？
3. 它对 Rust 工具链有什么要求（版本、特性、`no_std`）？
4. 它的源码在仓库里是怎样组织的？顶层入口在哪里？

本讲**不深入任何实现细节**，只建立全局认知。具体的机制（无锁路径、SeqLock 回退、Parker 状态机、作用域线程等）会在后续讲义中逐一展开。

## 2. 前置知识

在开始之前，建议你先具备以下基础概念。如果某一项还不熟悉，下面的简短解释足够你继续往下读。

### 2.1 并发编程（concurrent programming）

「并发」指的是**多个任务在同一时间段内推进**。在 Rust 里，最常见的并发形式是用 `std::thread::spawn` 启动多个线程，让它们同时访问同一片内存。

当多个线程同时**读写同一个变量**时，就会产生**数据竞争（data race）**：例如两个线程同时对一个 `i32` 做 `+= 1`，由于「读取—加一—写回」不是一步完成的，最终结果可能比预期少。Rust 的「所有权」和「Send/Sync」机制能阻止你写出**不安全**的数据竞争，但「写正确的并发逻辑」仍需要专门的工具——这正是 crossbeam-utils 要提供的东西。

### 2.2 原子操作与 `std::sync::atomic`

标准库的 `std::sync::atomic` 模块提供了 `AtomicUsize`、`AtomicBool` 等类型，它们用 CPU 的原子指令保证「读取—修改—写回」是不可被打断的。但标准库的原子类型有几个限制：

- 只支持少数**固定大小**的基本类型（整数、布尔、指针），不能直接对一个自定义结构体做原子读写；
- 没有提供「分片读写锁」「线程挂起」「作用域线程」等更高层的并发原语。

crossbeam-utils 的 `AtomicCell<T>` 正是「对任意 `T` 提供类原子访问」的尝试，而 `Parker`、`ShardedLock`、`scope` 等则填补了标准库没有覆盖的高层原语。

### 2.3 `no_std` 是什么

`no_std` 表示这段代码**可以脱离 Rust 标准库（`std`）运行**，只用更底层的 `core`。这很重要，因为它意味着这部分代码可以跑在内核、嵌入式、固件等**没有操作系统**的环境里。后面你会看到，crossbeam-utils 的部分类型（如 `AtomicCell`、`Backoff`、`CachePadded`）是 `no_std` 友好的，而依赖线程、锁的类型（如 `Parker`、`thread::scope`）则只能在有 `std` 的环境使用。

### 2.4 Crate 与 `Cargo.toml`

Crate 是 Rust 的编译/发布单元。每个 crate 的元信息（名字、版本、依赖、特性开关）都写在根目录的 `Cargo.toml` 里。理解 `Cargo.toml` 是读懂任何一个 Rust 项目的第一步。

> 如果你已经熟悉上述概念，可以直接进入第 3 节。

## 3. 本讲源码地图

本讲只看三个文件，它们构成了 crossbeam-utils 的「门面」：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 面向用户的项目说明：它是什么、提供哪些类型、怎么用、MSRV 是多少。 |
| `Cargo.toml` | crate 的元信息：名字、版本、依赖、特性（features）、MSRV。 |
| `src/lib.rs` | crate 的源码入口：顶层文档注释、`no_std` 声明、各模块的导出与 feature 门控。 |

> 这三个文件对应本讲的三个最小模块，第 4 节会逐一精读。

永久链接基准（本讲所有链接都基于这个 commit）：

```
https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/
```

## 4. 核心概念与源码讲解

### 4.1 README 概述与功能分类

#### 4.1.1 概念说明

crossbeam-utils 是 crossbeam 项目家族中的一个 crate，自我定位非常明确，README 开头一句话就说清了：

> This crate provides miscellaneous tools for concurrent programming.
> （本 crate 提供用于并发编程的各类工具。）

这句话告诉我们两件事：

1. **领域**：并发编程（concurrent programming）。
2. **形态**：一组「工具（tools）」的集合（miscellaneous），而不是一个单一功能库。这些工具之间彼此相对独立，你可以只挑需要的用。

README 把这些工具分成了**三大类**，这种分类是我们理解整个 crate 的骨架。

#### 4.1.2 核心流程

README 的功能列表按下面的结构组织，读者可以这样建立心智模型：

```
crossbeam-utils
├── Atomics（原子）
│   ├── AtomicCell     —— 任意类型 T 的「线程安全可变内存位置」
│   └── AtomicConsume  —— 以 consume 内存序读取原生原子类型
├── Thread synchronization（线程同步）
│   ├── Parker         —— 线程挂起/唤醒原语
│   ├── ShardedLock    —— 分片读写锁，读极快、可扩展
│   └── WaitGroup      —— 引用计数式「等待一组任务」同步
└── Utilities（工具）
    ├── Backoff        —— 自旋重试时的指数退避
    ├── CachePadded    —— 把值对齐/填充到缓存行长度，避免 false sharing
    └── scope          —— 作用域线程，能借用栈上局部变量
```

你可以先不用理解每个类型怎么实现，只要记住「它按 Atomics / 同步 / 工具 三类来组织」即可。后续每一讲基本对应其中一两个类型。

另外，README 给部分类型标注了 <sup>(no_std)</sup>，表示它们在 `no_std` 环境也可用。这是一个**贯穿全 crate 的重要设计取舍**：能用 `core` 实现的就尽量不依赖 `std`。

#### 4.1.3 源码精读

README 第 15–34 行就是上面的功能清单原文：

[README.md:L15-L34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/README.md#L15-L34) —— 这一段把所有公开类型分成 Atomics / Thread synchronization / Utilities 三组，并标注了哪些是 `no_std`。

其中关键几条：

- [README.md:L19](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/README.md#L19)：`AtomicCell`，标注了 <sup>(no_std)</sup>，是「线程安全的可变内存位置」——这是本手册第二单元的主角。
- [README.md:L20](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/README.md#L20)：`AtomicConsume`，同样 <sup>(no_std)</sup>。
- [README.md:L25-L27](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/README.md#L25-L27)：线程同步三件套 `Parker` / `ShardedLock` / `WaitGroup`（注意：这三者**没有** <sup>(no_std)</sup> 标记，说明它们依赖 `std`）。
- [README.md:L30-L32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/README.md#L30-L32)：工具三件套 `Backoff` / `CachePadded`（都 <sup>(no_std)</sup>）和 `scope`（依赖 `std`，因为是线程相关）。

> **小结**：标注 <sup>(no_std)</sup> 的有 `AtomicCell`、`AtomicConsume`、`Backoff`、`CachePadded`；其余依赖 `std`。这个区分在 `src/lib.rs` 里会用 feature 门控精确体现（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：通过阅读 README，亲手整理出 crossbeam-utils 的「类型—类别—是否 no_std」对照表，建立全局印象。

**操作步骤**：

1. 打开本仓库根目录的 `crossbeam-utils/README.md`。
2. 定位到 `#### Atomics` / `#### Thread synchronization` / `#### Utilities` 三个小节。
3. 对每个类型，记录：所属类别、是否带 <sup>(no_std)</sup> 标记、一句话描述。

**需要观察的现象**：你会发现「原子类」和「纯工具类（Backoff/CachePadded）」大多带 <sup>(no_std)</sup>，而「线程同步类（Parker/ShardedLock/WaitGroup）」和 `scope` 都不带——因为后者本质需要操作系统线程支持。

**预期结果**：得到一张 8 个类型 × 3 列的表格，与本讲 4.1.2 的树形图一致。

**待本地验证**：无（纯阅读型实践，不需要运行命令）。

#### 4.1.5 小练习与答案

**练习 1**：如果让你在一段运行于微控制器（`no_std`、无操作系统）的固件代码里，想用一个跨线程的「最新值快照」容器，README 列出的类型中哪些**候选可用**？

> **答案**：`AtomicCell` 和 `AtomicConsume`，因为它们标注了 <sup>(no_std)</sup>。`Parker`、`WaitGroup`、`ShardedLock`、`scope` 都依赖 `std`，不可用；`Backoff` / `CachePadded` 虽然也 `no_std`，但它们不是「存储值的容器」。

**练习 2**：`ShardedLock` 的描述是「a sharded reader-writer lock with fast concurrent reads」。仅凭这句话，你推测它在「多读少写」还是「多写少读」场景下更有优势？

> **答案**：多读少写。因为它强调 **fast concurrent reads（快速的并发读）」，分片（sharded）的设计正是为了降低多个读者之间的争用；相应地，写者通常需要锁住所有分片，所以写会比较慢（这一点会在 u3-l3 详细讲）。

---

### 4.2 Cargo.toml 元信息与 MSRV

#### 4.2.1 概念说明

`Cargo.toml` 是 crate 的「身份证」。要了解一个 crate，最该先看的几个字段是：

- `name` / `version`：叫什么、当前版本号。
- `edition`：用哪一版 Rust Edition（影响语法和默认行为）。
- `rust-version`：**MSRV**（Minimum Supported Rust Version，最低支持的 Rust 版本）。
- `features`：可选的**特性开关**，用户可以在 `Cargo.toml` 里按需开启/关闭。
- `dependencies`：依赖哪些别的 crate。

这里有两个对初学者比较新的概念：

- **MSRV**：即「想用这个 crate，你的 Rust 编译器至少要新到什么版本」。crossbeam-utils 承诺支持「至少最近 6 个月的稳定版 Rust」，每次提升 MSRV 都会发布一个新的 minor 版本。
- **Feature（特性）**：可以把一部分功能用条件编译开关包起来，用户不需要时就不编译，从而减少依赖、适配 `no_std` 等场景。

#### 4.2.2 核心流程

crossbeam-utils 的版本与工具链关系可以这样理解：

```
发布版本 0.8.21 (edition 2021)
        │
        ├── 默认: MSRV = Rust 1.56   ← 普通用户用 default features 时的门槛
        │     (default = ["std"])
        │
        └── 开启 atomic feature 时: 需要 Rust 1.74  ← 更高的版本要求
              (atomic = ["atomic-maybe-uninit"])
```

也就是说，**特性开关会改变 MSRV**：你只用默认功能时 1.56 就够；一旦开启 `atomic`，就需要 1.74。这是一个容易被忽视、但很实际的兼容性约束。

特性之间的依赖关系如下：

- `default = ["std"]`：默认开启 `std`。
- `std`：启用需要标准库的 API（即 4.1 中没标 <sup>(no_std)</sup> 的那些）。
- `atomic`：启用整个 `atomic` 模块（`AtomicCell`、`AtomicConsume`）；它依赖 `atomic-maybe-uninit`。

#### 4.2.3 源码精读

关键元信息在 `Cargo.toml` 顶部：

[Cargo.toml:L7-L16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L7-L16) —— 版本 `0.8.21`、edition `2021`、MSRV `1.56`，描述为 "Utilities for concurrent programming"，分类包含 `no-std`。

其中：

- [Cargo.toml:L7](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L7)：`version = "0.8.21"`。
- [Cargo.toml:L8](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L8)：`edition = "2021"`。
- [Cargo.toml:L10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L10)：`rust-version = "1.56"`，即 MSRV（注释提醒要和 README 里的「Compatibility」段落同步）。
- [Cargo.toml:L16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L16)：`categories` 含 `"no-std"`，说明这是一个 `no_std` 友好的 crate。

特性定义在 features 段：

[Cargo.toml:L27-L36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L27-L36) —— `default = ["std"]`；`std = []`；`atomic = ["atomic-maybe-uninit"]` 且注释明确「This requires Rust 1.74」。

要点：

- [Cargo.toml:L28](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L28)：默认开启 `std`。
- [Cargo.toml:L34-L36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L34-L36)：`atomic` feature 依赖 `atomic-maybe-uninit`，且需要 Rust 1.74。

依赖方面，`atomic-maybe-uninit` 是一个**可选依赖**（`optional = true`），只有在开启 `atomic` 时才会被引入：

[Cargo.toml:L38-L39](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L38-L39) —— `atomic-maybe-uninit` 作为可选依赖。

> **小结**：`Cargo.toml` 告诉我们——版本 0.8.21、edition 2021、默认 MSRV 1.56（开 `atomic` 则需 1.74）、默认带 `std`、`atomic` 是独立 feature。

#### 4.2.4 代码实践

**实践目标**：用 `cargo` 在本地确认 features 与 MSRV 的实际效果。

**操作步骤**：

1. 进入 `crossbeam-utils` 目录。
2. 分别用三种配置做一次「只检查不运行」的编译：
   - 默认：`cargo +stable check`
   - 关闭默认特性：`cargo +stable check --no-default-features`
   - 开启 atomic：`cargo +stable check --features atomic`
3. 观察每条命令是否成功。

**需要观察的现象**：

- 默认配置下，`sync`、`thread` 模块可用（因为带 `std`）。
- `--no-default-features` 时，依赖 `std` 的模块（`sync`/`thread`）被禁用（见 4.3 的 feature 门控）。
- 开启 `atomic` 会额外拉入 `atomic-maybe-uninit`。

**预期结果**：三种配置都能通过 `cargo check`（假设你的 stable 工具链 ≥ 1.74）。

**待本地验证**：如果本机 stable 工具链低于 1.74，开启 `atomic` 时可能报版本不满足——这恰好验证了「`atomic` 需要 Rust 1.74」这条约束。具体行为以本机实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：某用户的 `Cargo.toml` 写了 `crossbeam-utils = { version = "0.8", default-features = false }`，但他又想用 `AtomicCell`。他需要怎么改？

> **答案**：需要把 `atomic` feature 显式开起来：`crossbeam-utils = { version = "0.8", default-features = false, features = ["atomic"] }`。因为 `atomic` 不在 `default` 里，关掉默认特性后默认拿不到 `AtomicCell`。同时要确保工具链 ≥ 1.74。

**练习 2**：为什么 MSRV 是 1.56 而不是「最新版」？

> **答案**：crossbeam-utils 在 README 的 Compatibility 段承诺支持「至少最近 6 个月的稳定版 Rust」，并把 MSRV 锁在 1.56，目的是让更多下游项目（尤其是自身 MSRV 较低的库）能够依赖它。每次提升 MSRV 都会发布新 minor 版本，便于下游按需锁定。

---

### 4.3 lib.rs 顶层模块文档

#### 4.3.1 概念说明

`src/lib.rs` 是一个 crate 的**源码根入口**。它通常做三件事：

1. 用 `//!` 写 crate 级别的文档注释（会显示在 docs.rs 的首页）。
2. 用 `#![...]` 写**全局属性**（如 `#![no_std]`、lint 设置）。
3. 声明并导出各个子模块（`pub mod ...` 或 `mod ...` + `pub use ...`）。

crossbeam-utils 的 `lib.rs` 就是教科书式的范本：顶部是和 README 几乎一致的功能清单，紧接着是 `#![no_std]`，然后用 **feature 门控** 决定哪些模块对外可见。理解这里的 `#[cfg(...)]` 是读懂「4.1 里 no_std 标记如何落地」的关键。

#### 4.3.2 核心流程

crate 顶层模块的导出逻辑可以画成一张「条件门控图」：

```
#![no_std]   ← 整个 crate 默认 no_std
   │
   ├─ feature = "atomic"  ──►  pub mod atomic;        (AtomicCell / AtomicConsume)
   │
   ├─ (无条件)            ──►  mod cache_padded; pub use CachePadded;
   │
   ├─ (无条件)            ──►  mod backoff;     pub use Backoff;
   │
   ├─ feature = "std"     ──►  pub mod sync;            (Parker / ShardedLock / WaitGroup)
   │
   └─ feature = "std" 且 非 crossbeam_loom ──►  pub mod thread;   (scope / 作用域线程)
```

这张图直接对应 README 的三大类：

- `atomic` 模块 → Atomics 类。
- `sync` 模块 → Thread synchronization 类（需要 `std`）。
- `thread` 模块 → Utilities 类里的 `scope`（也需要 `std`）。
- `cache_padded`、`backoff` → Utilities 类里的 `CachePadded`、`Backoff`（无条件，`no_std` 可用）。

所以 README 里「带不带 <sup>(no_std)</sup>」的差异，本质就是 `lib.rs` 里这个模块是否被 `feature = "std"` 门控。

此外，`lib.rs` 还有一个 `primitive` 模块（第 47–83 行），它是一个**内部抽象层**：在普通编译下指向标准库的 `atomic`/`Arc`/`Mutex`/`Condvar`，在开启 `crossbeam_loom` 时指向 `loom` 提供的版本——这是为了做并发交错测试。它属于内部机制，本讲只需知道「有这么一层」，具体会在 u5-l3 展开。

#### 4.3.3 源码精读

crate 级文档注释几乎复述了 README 的分类：

[src/lib.rs:L1-L25](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L1-L25) —— 顶部 `//!` 注释，列出 Atomics / Thread synchronization / Utilities 三组类型，并在 `[AtomicCell]: atomic::AtomicCell` 等链接处给出模块内路径。

关键的 `no_std` 声明：

[src/lib.rs:L27](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L27) —— `#![no_std]`，把整个 crate 设为默认不依赖标准库；后续需要 `std`/`alloc` 的地方再在 `feature = "std"` 下显式引入（见 [src/lib.rs:L41-L45](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L41-L45)）。

模块导出区是本讲最核心的一段：

[src/lib.rs:L85-L100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L85-L100) —— 这一段用 `#[cfg(...)]` 决定每个模块是否对外可见。

逐条说明：

- [src/lib.rs:L85-L87](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L85-L87)：`pub mod atomic`，只在 `feature = "atomic"` 开启时编译。这就是「想用 `AtomicCell` 必须开 `atomic` feature」的源头。
- [src/lib.rs:L89-L90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L89-L90)：`cache_padded` 模块**无 cfg 门控**，并通过 `pub use` 重导出 `CachePadded`——所以它在 `no_std` 也可用。
- [src/lib.rs:L92-L93](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L92-L93)：`backoff` 模块同样无门控，`pub use Backoff`。
- [src/lib.rs:L95-L96](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L95-L96)：`pub mod sync` 在 `feature = "std"` 下才可见——对应 `Parker`/`ShardedLock`/`WaitGroup` 依赖 `std`。
- [src/lib.rs:L98-L100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L98-L100)：`pub mod thread` 需要 `feature = "std"` 且**非 `crossbeam_loom`**（因为 loom 下线程模型不同），对应 `scope` 作用域线程。

内部抽象层：

[src/lib.rs:L47-L83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L47-L83) —— `primitive` 模块在 `crossbeam_loom` 下指向 loom 的 `atomic`/`Arc`/`Mutex`/`Condvar`，否则指向标准库——这是可测试性抽象，本讲只作了解。

> **小结**：`lib.rs` 用 `#![no_std]` + 一组 `#[cfg(feature = "...")]`，精确实现了 README 里描述的「哪些类型 `no_std` 可用、哪些需要 `std`、`atomic` 独立开关」。

#### 4.3.4 代码实践

**实践目标**：把「README 的功能分类」与「lib.rs 的 feature 门控」对照起来，验证它们一一对应。

**操作步骤**：

1. 打开 `src/lib.rs` 第 85–100 行，找到 5 个模块声明（`atomic`、`cache_padded`、`backoff`、`sync`、`thread`）。
2. 对每个模块，记录它头顶的 `#[cfg(...)]` 条件。
3. 用 `cargo doc --no-deps --features atomic --open`（或在线 docs.rs）查看生成的文档首页，确认各类型在 `atomic`/`std` 下是否出现。

**需要观察的现象**：

- `cache_padded`、`backoff` 头顶**没有** `#[cfg(feature = "std")]` → 与 README 的 <sup>(no_std)</sup> 标记一致。
- `sync`、`thread` 头顶**有** `#[cfg(feature = "std")]` → 与 README 中它们不带 <sup>(no_std)</sup> 一致。
- `atomic` 头顶是 `#[cfg(feature = "atomic")]`，是一个独立开关。

**预期结果**：得到一张「模块 → cfg 条件 → README 是否标 no_std」的三列表，三列自洽。

**待本地验证**：`cargo doc` 的实际产物取决于本机工具链与 feature 组合，以本机生成为准。

#### 4.3.5 小练习与答案

**练习 1**：在 `--no-default-features` 下编译时，下列哪些模块**仍然可见**？`atomic`、`cache_padded`、`sync`、`thread`。（假设不额外开 `atomic`）

> **答案**：只有 `cache_padded`（以及 `backoff`）可见。`atomic` 需要 `feature = "atomic"`；`sync`、`thread` 需要 `feature = "std"`，而 `std` 来自 `default`，关掉默认特性后它们都被禁用。

**练习 2**：为什么 `pub mod thread` 还多加了一个 `#[cfg(not(crossbeam_loom))]`？

> **答案**：`thread` 模块基于真实的操作系统线程（`std::thread`）。而 `crossbeam_loom` 是一种「把线程交错执行穷举化」的测试模式（loom），它替换了线程与同步原语的实现；在这种测试模式下，真实的 `std::thread::scope` 语义无法用 loom 模型表达，因此 `thread` 模块在 loom 下被禁用。具体机制在 u5-l3 讲。

---

## 5. 综合实践

把本讲三部分（定位、特性、模块导出）串起来，完成一个**真正可运行**的小任务：用 `AtomicCell` 写一个多线程计数器。

这个任务同时验证三件事：

- 你能正确地在自己的项目里加入 crossbeam-utils 依赖（对应 4.2 的 features）。
- 你知道 `AtomicCell` 在 `atomic` 模块下（对应 4.3 的门控）。
- 你体会到「`AtomicCell` 提供线程安全的可变内存位置」这句话的实战含义（对应 4.1 的定位）。

**实践目标**：新建一个独立的 binary crate，多个线程并发对一个 `AtomicCell<usize>` 做 `fetch_add`，主线程汇总，验证结果等于预期（无丢更新）。

**操作步骤**：

1. 在任意目录新建一个项目（下面是示例命令，可在本地执行）：

   ```bash
   cargo new cb-utils-demo
   cd cb-utils-demo
   ```

2. 在 `Cargo.toml` 的 `[dependencies]` 里加入依赖（注意要开 `atomic` feature）：

   ```toml
   [dependencies]
   crossbeam-utils = { version = "0.8", features = ["atomic"] }
   ```

3. 编辑 `src/main.rs`（**示例代码**，非 crossbeam-utils 仓库原有文件）：

   ```rust
   use crossbeam_utils::atomic::AtomicCell;
   use std::thread;

   fn main() {
       // 8 个线程，每个线程对计数器自增 1000 次。
       const THREADS: usize = 8;
       const PER_THREAD: usize = 1000;

       // AtomicCell<usize> 就是一个「线程安全的 usize」。
       let counter = AtomicCell::new(0usize);
       let mut handles = Vec::new();

       for _ in 0..THREADS {
           // 注意：这里需要拿到 counter 的引用，跨线程移动。
           // 实际写法上通常用 Arc<AtomicCell<...>> 或在 thread::scope 内借用栈；
           // 这里为了演示用裸引用 + 'static 的简化写法需要 unsafe 或改用 scope。
           // 更安全的推荐写法见第 4 单元 thread::scope（u4-l1）。
           // 下面给出可直接编译的安全写法：用 Arc 共享。
           // （为了简洁，下面的最终代码直接用 Arc。）
           let _ = handles; // 占位，实际代码见下方完整版
       }

       // —— 直接给出可编译的安全版本 ——
       use std::sync::Arc;
       let counter = Arc::new(AtomicCell::new(0usize));
       let mut handles = Vec::new();
       for _ in 0..THREADS {
           let c = Arc::clone(&counter);
           handles.push(thread::spawn(move || {
               for _ in 0..PER_THREAD {
                   c.fetch_add(1);
               }
           }));
       }
       for h in handles {
           h.join().unwrap();
       }

       let total = counter.load();
       println!("expected = {}", THREADS * PER_THREAD);
       println!("actual   = {}", total);
       assert_eq!(total, THREADS * PER_THREAD, "发生了丢更新！");
       println!("OK: 无丢更新");
   }
   ```

   > 上半段「裸引用」的伪代码只为说明思路，**真正可编译运行的是下半段 `Arc` 版本**。把伪代码那部分删掉，只保留 `Arc` 版本即可。本书第 4 单元会教你用 `thread::scope` 避免 `Arc`。

4. 运行：`cargo run`。

**需要观察的现象**：

- `actual` 总是等于 `expected`（8000），即使多个线程在并发自增。
- 如果把 `AtomicCell::new(0)` 换成普通 `Cell` 或裸 `usize`（并相应改成非线程安全写法），在高并发下会观察到 `actual < expected`（丢更新）——但**请不要真的去写不安全代码**，这里只是作为思维对照。

**预期结果**：多次运行，`actual` 恒等于 8000，断言通过，打印 `OK: 无丢更新`。

**待本地验证**：实际并发行为依赖本机 CPU 核数与线程调度；只要断言通过即说明 `AtomicCell` 的原子性正确。

> **提示**：这一步里出现的 `Arc` 共享、以及「能不能不用 `Arc` 直接借用栈变量」的疑问，正是后面 `thread::scope`（u4-l1）要解决的问题。保留这个疑问，带着它学下去。

## 6. 本讲小结

- crossbeam-utils 是一个面向**并发编程**的工具集 crate，定位是「miscellaneous tools」，各类型相对独立。
- 它把公开类型分为三类：**Atomics**（`AtomicCell`、`AtomicConsume`）、**Thread synchronization**（`Parker`、`ShardedLock`、`WaitGroup`）、**Utilities**（`Backoff`、`CachePadded`、`scope`）。
- 元信息：版本 **0.8.21**、edition **2021**、默认 MSRV **1.56**；开启 `atomic` feature 则需要 **Rust 1.74**。
- 特性开关：`default = ["std"]`；`atomic` 是独立 feature 且依赖 `atomic-maybe-uninit`。
- `src/lib.rs` 用 `#![no_std]` + 一组 `#[cfg(feature = ...)]` 精确实现了 README 描述的「哪些类型 `no_std` 可用、哪些需要 `std`、`atomic` 独立开关」。
- README 的 <sup>(no_std)</sup> 标记 ↔ `lib.rs` 的 feature 门控，二者一一对应，这是理解整个 crate 可见性的钥匙。

## 7. 下一步学习建议

本讲建立了全局认知。接下来建议：

1. **u1-l2 目录结构与模块地图**：深入 `src/` 目录，看清 `atomic/mod.rs`、`sync/mod.rs` 内部的子模块组织，画出更细的模块树。
2. **u1-l3 构建特性、build.rs 与运行测试**：理解 `build.rs` 如何检测目标平台原子支持、loom 如何用于并发测试，掌握用不同 feature 跑测试与基准的命令。
3. 之后进入**第二单元 atomic**，从 **u2-l1 AtomicCell 公共 API** 开始，亲手拆开 `AtomicCell<T>` 这个「对任意 `T` 的线程安全容器」。

> 建议在进入 u2 之前，先把本讲综合实践里的 `AtomicCell` 多线程计数器跑通——它会成为你后续理解「无锁路径 vs 全局锁回退」最直观的抓手。
