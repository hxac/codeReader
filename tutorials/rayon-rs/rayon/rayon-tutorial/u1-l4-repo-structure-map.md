# 仓库结构与代码地图（u1-l4）

## 1. 本讲目标

读完本讲，你应该能够：

1. 画出 rayon 仓库的**三层分层图**：`rayon-demo` → `rayon` → `rayon-core`，并说出每一层的职责。
2. 说出 `rayon` crate 中 `src/` 下各目录（`iter`、`slice`、`collections`、`str` 等）各自负责什么。
3. 说出 `rayon-core/src/` 下各模块（`join`、`job.rs`、`registry.rs`、`scope`、`sleep` 等）各自负责什么。
4. 在源码中**亲手定位**三大核心 API 的定义位置：`ParallelIterator`、`join`、`ThreadPool`。
5. 读懂两个 crate 入口文件（`src/lib.rs` 与 `rayon-core/src/lib.rs`）中的 `pub use` 再导出语句，明白「你在 prelude 里用的名字到底从哪来」。

这一讲不讲解任何算法细节，它的唯一任务是：**为后续所有讲义建立一张可以反复对照的地图**。以后每讲深入某个模块时，你都能在这张图上找到它的位置。

## 2. 前置知识

本讲只需要两个前置认知（分别在 u1-l1 和 u1-l2 建立）：

- **Rayon 分三层**：`rayon` 是上层并行迭代器库，`rayon-core` 是底层调度内核，`rayon-demo` 是演示程序。本讲把这句话展开成精确的文件级地图。
- **Cargo workspace**：一个仓库里可以装多个 crate，根 `Cargo.toml` 用 `[workspace]` 段声明成员，成员之间用 `path` 依赖互相引用。

再补充两个本讲会反复用到的 Rust 概念，供不熟悉的读者对照：

| 概念 | 通俗解释 |
|---|---|
| `mod foo;` | 声明一个子模块，编译器会去找 `foo.rs` 或 `foo/mod.rs` 文件 |
| `pub use a::b;` | **再导出**（re-export）：把 `a` 模块里的 `b` 挂到当前模块的公开路径上，外界就可以通过当前模块访问它 |
| `pub(super)` | 只对父模块可见的可见性，比 `pub` 更窄，常用于 crate 内部实现细节 |
| `#[cfg(test)]` | 只在测试编译（`cargo test`）时才存在的代码，普通构建会整段跳过 |

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|---|---|
| [Cargo.toml](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/Cargo.toml#L37-L39) | 根包 `rayon` 的清单，同时声明 workspace 成员 |
| [rayon-core/Cargo.toml](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/Cargo.toml#L1-L7) | 调度内核包清单，`links` 保证全局唯一 |
| [rayon-demo/Cargo.toml](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/Cargo.toml#L8-L9) | 演示程序清单，path 依赖根包 `rayon` |
| [src/lib.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L84-L118) | `rayon` crate 入口：模块声明 + 从 rayon-core 再导出 |
| [src/iter/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L359) | 并行迭代器两大 trait 的定义处，全仓库最大的文件 |
| [src/prelude.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/prelude.rs#L5-L17) | `use rayon::prelude::*` 实际引入的 13 个 trait |
| [rayon-core/src/lib.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L69-L92) | `rayon-core` crate 入口：内核模块声明 + 对外再导出 |
| [rayon-core/src/join/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L93) | `join` 函数定义处 |
| [rayon-core/src/thread_pool/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L46) | `ThreadPool` 结构体定义处 |
| [rayon-demo/src/main.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/main.rs#L73-L92) | demo 的 CLI 入口与命令分发 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**4.1 三层分层图**、**4.2 src 目录导览**、**4.3 rayon-core 目录导览**。

### 4.1 三层分层图

#### 4.1.1 概念说明

rayon 仓库是一个「倒挂」的三层结构：**使用者在最上面，内核在最下面**：

```
┌─────────────────────────────────────────────────────┐
│  rayon-demo（演示/基准程序，publish = false）        │
│  fibonacci、matmul、nbody、quicksort、mergesort...   │
└──────────────────────┬──────────────────────────────┘
                       │ 依赖（rayon = { path = "../" }）
┌──────────────────────▼──────────────────────────────┐
│  rayon（上层库：并行迭代器 + 各数据源适配）          │
│  src/iter、src/slice、src/collections、src/str...    │
└──────────────────────┬──────────────────────────────┘
                       │ 依赖（rayon-core，path + version 双约束）
┌──────────────────────▼──────────────────────────────┐
│  rayon-core（调度内核：线程池 + 工作窃取）           │
│  join、scope、spawn、broadcast、registry、job、sleep │
└─────────────────────────────────────────────────────┘
```

三层职责一句话概括：

- **rayon-core**：回答「任务怎么被调度执行」。只有线程池、任务对象、工作窃取、睡眠唤醒，**完全不知道迭代器是什么**。
- **rayon**：回答「数据怎么被切分给任务」。并行迭代器、各种数据源（切片/字符串/集合）的适配，全部建立在 rayon-core 的 `join` 之上。
- **rayon-demo**：回答「这套东西性能如何」。一组可运行的基准程序。

为什么要拆成两个 crate？因为 `rayon-core` 通过 `links` 声明强制整个构建产物里**只能存在一份**（否则会出现两个各自为政的全局线程池），拆开后便于单独锁版本；同时 `rayon` 可以快速迭代上层 API 而不动内核。

#### 4.1.2 核心流程

依赖方向是严格单向的，可以用三条规则描述：

1. `rayon-demo` → `rayon`（path 依赖），不直接依赖 `rayon-core`。
2. `rayon` → `rayon-core`（path + version 双约束），运行时还依赖 `either`。
3. `rayon-core` 不依赖仓库内任何其他 crate，只依赖外部的 `crossbeam-deque`、`crossbeam-utils`。

读代码时的推论：**想理解调度，往下走（rayon-core）；想理解迭代器，留在上面（rayon）；想看用法示例，去 rayon-demo**。任何跨层的调用只会是「上层调下层」，绝不会反向。

#### 4.1.3 源码精读

**证据一：workspace 成员声明。** 根 `Cargo.toml` 在文件末尾声明了两个成员，依赖方向由各成员自己的清单决定：

[Cargo.toml:L37-L39](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/Cargo.toml#L37-L39) —— 声明 workspace 包含 `rayon-demo` 与 `rayon-core` 两个成员（根包 `rayon` 自身隐式是成员）。

[Cargo.toml:L26-L29](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/Cargo.toml#L26-L29) —— `rayon` 对 `rayon-core` 的依赖写成 `{ version = "1.13.0", path = "rayon-core" }`：本地开发走 path，发布到 crates.io 后走版本号。

[rayon-demo/Cargo.toml:L8-L9](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/Cargo.toml#L8-L9) —— `rayon-demo` 只依赖 `rayon = { path = "../" }`，印证「demo 不碰 core」。

[rayon-core/Cargo.toml:L1-L7](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/Cargo.toml#L1-L7) —— `links = "rayon-core"` 配合 build.rs，是「全局只允许一份内核」的机制来源（u1-l2 已讲过构建细节）。

**证据二：rayon-demo 的入口分发。** demo 的 `main.rs` 用一个 `match` 把命令行第一个参数分发到各子模块：

[rayon-demo/src/main.rs:L73-L92](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/main.rs#L73-L92) —— `main` 函数按 `matmul`/`mergesort`/`nbody`/`quicksort`/`sieve`/`tsp`/`life`/`noop` 八个名字分发，其余情况打印用法并退出。

[rayon-demo/src/main.rs:L6-L14](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/main.rs#L6-L14) —— 八个**可直接运行**的 demo 模块声明（`cpu_time` 是辅助模块）。

[rayon-demo/src/main.rs:L18-L37](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/main.rs#L18-L37) —— 十个 `#[cfg(test)]` 模块（factorial、fibonacci 等）：它们只在 `cargo bench`/`cargo test` 时编译，命令行运行只会打印用法——这正是 u1-l2 里「fibonacci 跑不起来」的根源。

**证据三：rayon 入口对内核的「转贴」。** 上层 crate 的入口几乎不含逻辑，它做的只是声明自己的模块、再把内核的公共 API 原样搬出来：

[src/lib.rs:L107-L118](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L107-L118) —— 共 12 条 `pub use rayon_core::...`，把 `join`、`spawn`、`scope`、`ThreadPool` 等全部再导出。**这就是为什么你写 `rayon::join` 而不是 `rayon_core::join`**——官方推荐始终从 `rayon` 使用这些 API（rayon-core 自己的文档也这么说）。

#### 4.1.4 代码实践

**实践：手工绘制三层依赖图。**

1. **实践目标**：不看本讲插图，凭源码证据画出三个 crate 的依赖箭头图，并为每条箭头标注源码依据（文件 + 行号）。
2. **操作步骤**：
   - 打开三个 `Cargo.toml`（根目录、`rayon-core/`、`rayon-demo/`），找到所有 `[dependencies]` 段。
   - 在纸上画三个方框，凡出现 `path = "..."` 的依赖就画一条箭头，旁边写「Cargo.toml 第 N 行」。
   - 再打开 [src/lib.rs:L107-L118](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L107-L118)，在 `rayon → rayon-core` 的箭头旁补注「12 条 pub use 再导出」。
3. **需要观察的现象**：三个 Cargo.toml 里**找不到任何一条反向依赖**（rayon-core 不依赖 rayon，rayon 不依赖 rayon-demo）。
4. **预期结果**：得到一张与 4.1.1 插图同构的图，且每条边都有行号级证据。这张图建议保留，后续每学一个模块就在图上补一个叶子节点。

（本实践为源码阅读型，无需运行命令。）

#### 4.1.5 小练习与答案

**练习 1**：既然 `rayon-demo` 不依赖 `rayon-core`，那 demo 里能使用 `join` 吗？

<details><summary>参考答案</summary>

能。demo 依赖 `rayon`，而 `rayon` 通过 [src/lib.rs:L117](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L117) 的 `pub use rayon_core::{join, join_context};` 把 `join` 再导出了，所以 `rayon::join` 直接可用。传递依赖 + 再导出让 demo 无需直连内核。
</details>

**练习 2**：为什么 `rayon-core` 要用 `links` 强制全局唯一，而 `rayon` 不需要？

<details><summary>参考答案</summary>

`rayon-core` 内部持有**全局线程池**（一个进程级单例，见 4.3 的 `THE_REGISTRY`）。若两份 `rayon-core` 共存，就会出现两个互不知情的全局池，线程数翻倍、任务互不窃取。`rayon` 只是上层适配，无全局状态，共存无害。相关说明见 [rayon-core/src/lib.rs:L39-L55](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L39-L55) 的文档。
</details>

### 4.2 src 目录导览：rayon 上层 crate

#### 4.2.1 概念说明

`rayon` crate 的 `src/` 目录遵循一条设计原则，其入口文档写得很清楚：**模块结构镜像标准库**——`std` 有 `option`，rayon 就有 `src/option.rs`；`std` 有 `collections`，rayon 就有 `src/collections/`。每个模块的使命是「给对应的标准库类型装上并行迭代能力」。

除此之外还有三个特殊角色：

- `src/iter/`：唯一不对应任何 std 模块的大模块，是并行迭代器**本身的定义地**（trait + 几十个适配器）。
- `src/delegate.rs`：一个内部宏工具箱，用宏消除适配器之间大量重复的转发代码。
- `src/prelude.rs`：把散落各处的 trait 汇总成一个导入入口。

#### 4.2.2 核心流程

`rayon` 上层的一条数据流可以概括为：

```
数据源模块（slice/str/collections/...）
        │  实现 IntoParallelIterator，产出并行迭代器
        ▼
ParallelIterator / IndexedParallelIterator（src/iter/mod.rs 定义）
        │  .map().filter()... 链式套适配器（src/iter/ 下每个文件一个）
        ▼
消费者触发执行（sum/collect/for_each...）
        │  借助 src/iter/plumbing 的 Producer/Consumer 协议
        ▼
最终落到 rayon-core 的 join 上执行
```

目录 → 职责速查表：

| 路径 | 职责 | 代表性定义 |
|---|---|---|
| `src/iter/` | 迭代器 trait + 适配器 + plumbing 协议 | `ParallelIterator`（L359）、`IndexedParallelIterator`（L2449） |
| `src/slice/` | 切片并行：视图、排序 | `ParallelSlice`（L31）、`sort.rs` 并行归并排序 |
| `src/collections/` | 八大 std 集合的并行适配 | `hash_map.rs`、`btree_map.rs` 等 |
| `src/str.rs` / `src/string.rs` | 字符串并行处理 | `ParallelString`（L59） |
| `src/range.rs` / `range_inclusive.rs` / `array.rs` / `option.rs` / `result.rs` / `vec.rs` | 各数据源 | 与文件名同名的类型 |
| `src/delegate.rs` | 委托宏（内部） | `delegate_iterator!`（L11） |
| `src/split_producer.rs` / `src/math.rs` / `src/par_either.rs` | 内部工具 | 按值切分、数学辅助、Either 支持 |

#### 4.2.3 源码精读

**入口的模块声明分区。** [src/lib.rs:L84-L103](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L84-L103) 把 `src/` 下的文件分成四组声明，每组性质不同：

- [src/lib.rs:L84-L87](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L84-L87) —— 内部基础设施：`#[macro_use] mod delegate;`（宏要先声明才能被后续模块使用）与 `mod split_producer;`。
- [src/lib.rs:L89-L100](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L89-L100) —— **公开**的数据源模块（`pub mod array; ... pub mod vec;`），镜像 std 的那一批。
- [src/lib.rs:L102-L103](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L102-L103) —— 私有工具模块（`mod math; mod par_either;`）。

**ParallelIterator 的家。** 全仓库最重要的一行定义：

[src/iter/mod.rs:L359](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L359) —— `pub trait ParallelIterator: Sized + Send`，所有并行迭代器的根 trait，`map`/`filter`/`sum` 等几十个方法都在它的提供方法里。

[src/iter/mod.rs:L2449](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2449) —— `pub trait IndexedParallelIterator: ParallelIterator`，加上「已知长度、可随机切分」能力后才能用 `zip`/`enumerate`（u2-l1 展开）。

[src/iter/mod.rs:L107-L163](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L107-L163) —— 私有适配器模块清单：**一个文件一个适配器**（`mod map; mod filter; mod zip;`……共 50 余个）。文件名即适配器名，这是 `src/iter/` 最重要的浏览规律。

[src/iter/mod.rs:L165-L212](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L165-L212) —— 与上一条对应的 `pub use self::{...}`：把这些私有模块里的**公开类型**（`Map`、`Filter`、`Zip`…）挂到 `iter` 模块路径上，供需要写出具体类型的人使用。

[src/iter/mod.rs:L91](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L91) —— `pub mod plumbing;`：Producer/Consumer 协议所在，单元四整讲都围绕它（配套的 [src/iter/plumbing/README.md](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md) 是官方设计说明）。

**prelude 的真实成分。** [src/prelude.rs:L5-L17](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/prelude.rs#L5-L17) 共 13 条 `pub use`：10 个来自 `crate::iter`（`ParallelIterator`、`IndexedParallelIterator`、`IntoParallelIterator` 等），2 个来自 `crate::slice`，1 个来自 `crate::str`。u1-l3 说「prelude 引入 13 个 trait」，账就在这里对上。

**切片与字符串的扩展 trait。** 数据源模块不为每个方法单开文件，而是用 trait 把方法「贴」到 std 类型上：

- [src/slice/mod.rs:L31](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L31) / [L222](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L222) —— `ParallelSlice` 与 `ParallelSliceMut`：给 `&[T]`/`&mut [T]` 贴上 `par_chunks`、`par_sort` 等方法。
- [src/str.rs:L59](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/str.rs#L59) —— `ParallelString`：给 `&str` 贴上 `par_split`、`par_lines` 等方法。

#### 4.2.4 代码实践

**实践：给 `src/lib.rs` 的每条再导出标注来源。**

1. **实践目标**：验证「`rayon` 里凡是任务调度类 API，全部来自 `rayon-core`」这一论断，并给每条 `pub use` 找到它在内核中的原始定义文件。
2. **操作步骤**：
   - 读 [src/lib.rs:L107-L118](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L107-L118)，抄下 12 条 `pub use rayon_core::...`。
   - 对照下表（答案已给出，你要做的是**逐条在源码里核实**再导出符号在 rayon-core 侧的出处，即 4.3.3 将读到的 [rayon-core/src/lib.rs:L83-L92](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L83-L92)）：

| `rayon` 侧再导出（src/lib.rs 行号） | rayon-core 中的原始来源 |
|---|---|
| `FnContext`（L107） | lib.rs 内定义（L836） |
| `ThreadBuilder`（L108） | `registry` 模块 |
| `ThreadPool`、`ThreadPoolBuildError`、`ThreadPoolBuilder`（L109-L111） | `thread_pool` 模块 / lib.rs 内定义 |
| `BroadcastContext`、`broadcast`、`spawn_broadcast`（L112） | `broadcast` 模块 |
| `Scope`、`in_place_scope`、`scope`（L113） | `scope` 模块 |
| `ScopeFifo`、`in_place_scope_fifo`、`scope_fifo`（L114） | `scope` 模块 |
| `Yield`、`yield_local`、`yield_now`（L115） | `thread_pool` 模块 |
| `current_num_threads`、`current_thread_index`、`max_num_threads`（L116） | lib.rs 函数 / `thread_pool` 模块 / `sleep` 计数上限 |
| `join`、`join_context`（L117） | `join` 模块 |
| `spawn`、`spawn_fifo`（L118） | `spawn` 模块 |

3. **需要观察的现象**：上表右侧没有一个条目来自 `src/iter`、`src/slice` 等上层模块——调度 API 与迭代器 API 完全两套来源。
4. **预期结果**：在自己的笔记里产出这张「符号搬运表」。之后在文档里看到 `rayon::scope`，你就能立刻反应出它真正的定义在 `rayon-core/src/scope/mod.rs`。

（本实践为源码阅读型；表中「L836」等行号可在本地用 `grep -n "pub struct FnContext" rayon-core/src/lib.rs` 复核。）

#### 4.2.5 小练习与答案

**练习 1**：`rayon::Map` 这个类型（`map` 适配器返回的类型）的模块路径是什么？它是 `pub mod` 直接暴露的吗？

<details><summary>参考答案</summary>

路径是 `rayon::iter::Map`。`mod map;` 本身是私有的（[src/iter/mod.rs:L134](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L134)），但通过 [src/iter/mod.rs:L187](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L187) 的 `pub use self::{... map::Map ...}` 挂到了公开路径上。这正是 mod.rs:L96-L105 注释所说的「私有模块 + 定点再导出」模式。
</details>

**练习 2**：我想找 `flat_map` 适配器的实现代码，应该打开哪个文件？为什么不用搜索也能推断出来？

<details><summary>参考答案</summary>

`src/iter/flat_map.rs`。因为 `src/iter/` 的组织规律是「一个适配器一个同名文件」，且 [src/iter/mod.rs:L120](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L120) 确有 `mod flat_map;`。
</details>

**练习 3**：`src/collections/hash_map.rs` 大概率是怎么给 `HashMap` 提供并行迭代的？说出你的猜测和依据。

<details><summary>参考答案</summary>

猜测：不重写生产逻辑，而是委托给内部已有的并行生产者（或 `par_iter` 产出键值引用再适配）。依据是 u1-l1/u1-l2 的结论——rayon 上层大量使用委托复用；精确答案在 u8-l3「集合的委托实现模式」中用 `src/delegate.rs` 的宏展开验证。
</details>

### 4.3 rayon-core 目录导览：调度内核

#### 4.3.1 概念说明

`rayon-core` 只有约 15 个源文件，却承担了全部运行时职责。它对迭代器一无所知，只认识一个概念：**「任务」（Job）**。上层把切分好的工作打包成闭包丢进来，内核负责让池里的线程把这些闭包跑完，并且在空闲时互相「偷」活干（工作窃取）。

模块按功能分四组：

| 分组 | 模块 | 职责 |
|---|---|---|
| **任务原语** | `join/`、`scope/`、`spawn/`、`broadcast/` | 四种「把闭包交出去」的方式（对应 u1-l1 的四类 API 中间三层） |
| **任务表示** | `job.rs`、`latch.rs`、`unwind.rs` | 闭包如何装箱成任务、如何通知完成、panic 如何安全传递 |
| **线程与调度** | `registry.rs`、`thread_pool/`、`sleep/` | 线程注册表、对外池对象、空闲睡眠/唤醒协议 |
| **测试** | `test.rs`、各模块的 `test.rs` | 内核自测 |

#### 4.3.2 核心流程

一次最简单的 `rayon::join(a, b)` 在内核里走过的路径（后续 u5 各讲逐站展开）：

```
join(oper_a, oper_b)            join/mod.rs
   │ 把两个闭包包装成 Job（栈上或堆上）
   │ oper_b 入队到本地 deque，随后先执行 oper_a
   ▼
Job / JobRef                    job.rs —— 任务的统一表示
   │ 执行完毕后通过 Latch「点亮」通知等待者
   ▼
Latch 家族                      latch.rs —— 自旋/阻塞两种等待策略
   │ 线程从哪来？—— Registry 管理的线程池
   ▼
Registry                        registry.rs —— 全局单例 + 每线程主循环
   │ 主循环：取本地任务 → 失败则尝试窃取 → 都没有则
   ▼
sleep 模块                      sleep/ —— 进入休眠等条件变量唤醒
```

#### 4.3.3 源码精读

**入口的模块声明与再导出。**

[rayon-core/src/lib.rs:L69-L78](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L69-L78) —— 10 个内部模块声明，与 4.3.1 的表格一一对应：`broadcast`、`job`、`join`、`latch`、`registry`、`scope`、`sleep`、`spawn`、`thread_pool`、`unwind`。注意其中 6 个是**目录模块**（含 `mod.rs` 与 `test.rs`），4 个是单文件（`job.rs`、`latch.rs`、`registry.rs`、`unwind.rs`）——有配套测试的复杂模块才会长成目录。

[rayon-core/src/lib.rs:L83-L92](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L83-L92) —— 10 条 `pub use self::...`，即内核的全部公共出口。读这张表就能反推出：`ThreadPool` 与 `yield_now` 一族来自 `thread_pool` 模块，`join` 来自 `join` 模块，`ThreadBuilder` 竟来自 `registry` 模块（因为线程的创建与命名由注册表负责）。

**三大 API 的精确定位（本讲硬指标）。**

1. `join`：[rayon-core/src/join/mod.rs:L93](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L93) —— `pub fn join<A, B, RA, RB>(oper_a: A, oper_b: B) -> (RA, RB)`；带上下文版本 [join_context](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L115) 紧随其后。
2. `ThreadPool`：[rayon-core/src/thread_pool/mod.rs:L46](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L46) —— `pub struct ThreadPool`，其 `install`/`join`/`spawn` 等方法都在同文件 `impl ThreadPool`（L50 起）。
3. `ParallelIterator`：不在内核，而在上层 [src/iter/mod.rs:L359](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L359)——这一条特意列出，因为它是最容易找错层的代表。

**配置与全局状态的落点。**

[rayon-core/src/lib.rs:L165-L197](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L165-L197) —— `ThreadPoolBuilder` 结构体定义在 lib.rs 而非 thread_pool 模块：线程数、线程名、栈大小、spawn_handler 等 10 个字段全在此。u7-l1 的链式配置方法都是它的 `impl`。

[rayon-core/src/lib.rs:L131-L133](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L131-L133) —— `current_num_threads()` 转调 `Registry::current_num_threads()`，是「lib.rs 只做门面、实活在 registry」的典型。

[rayon-core/src/registry.rs:L126](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L126) —— `pub(super) struct Registry`：注意可见性只有 `pub(super)`（对本 crate 可见），外界永远拿不到这个类型，只能通过 `ThreadPool` 间接使用。

[rayon-core/src/registry.rs:L154-L160](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L154-L160) —— `static mut THE_REGISTRY: Option<Arc<Registry>>` 与 `global_registry()`：全局线程池单例的存放处（u5-l3 展开）。

**两份「藏在外围的文档」。** 内核目录里有两个不写代码却极重要的文件：[rayon-core/src/sleep/README.md](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/README.md)（睡眠/唤醒状态机的官方说明，u5-l5 的教材）与 [src/iter/plumbing/README.md](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md)（Producer/Consumer 设计说明，u4-l1 的教材，其中开篇的 Pull/Push 双模式图示值得现在就翻一眼）。

#### 4.3.4 代码实践

**实践：三大 API 定位 + 内核导出表核实。**

1. **实践目标**：不看本讲，独立用工具定位 `ParallelIterator`、`join`、`ThreadPool` 的定义行号，并核实 4.2.4 表格中 rayon-core 侧的每一条来源。
2. **操作步骤**（在仓库根目录执行）：

   ```bash
   # 1. 定位三大 API 的定义处
   grep -n "pub trait ParallelIterator" src/iter/mod.rs
   grep -n "pub fn join" rayon-core/src/join/mod.rs
   grep -n "pub struct ThreadPool" rayon-core/src/thread_pool/mod.rs

   # 2. 列出内核全部对外导出
   grep -n "pub use" rayon-core/src/lib.rs

   # 3. 数一数内核模块清单
   grep -n "^mod " rayon-core/src/lib.rs
   ```

3. **需要观察的现象**：
   - 第 1 组命令应分别命中 `359`、`93`、`46` 三个行号；
   - 第 2 组命令恰好 10 条，全部以 `pub use self::` 开头；
   - 第 3 组命令列出 10 个模块 + 2 个测试模块（`compile_fail`、`test`）。
4. **预期结果**：输出与本讲给出的行号完全一致。若不一致（例如未来版本行号漂移），以你本地 grep 的结果为准，并回头修正自己笔记里的链接行号。
5. 本组命令均为只读 grep，可直接运行验证（如在你环境中未安装 grep 等工具则属「待本地验证」）。

#### 4.3.5 小练习与答案

**练习 1**：`rayon_core::ThreadBuilder` 为什么定义在 `registry.rs` 而不是 `thread_pool/mod.rs`？

<details><summary>参考答案</summary>

从再导出表 [rayon-core/src/lib.rs:L85](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L85) 可见出处是 `self::registry`。`ThreadBuilder` 描述「一个将要被启动的工作线程」（名字、栈大小），而启动线程、跑主循环的是 Registry；`ThreadPool` 只是对外持有的句柄。谁使用谁定义，是内核分文件的惯例。
</details>

**练习 2**：`job.rs`、`latch.rs`、`registry.rs`、`unwind.rs` 是单文件，而 `join`、`scope`、`spawn`、`broadcast`、`thread_pool`、`sleep` 是目录。推测这条划分线是什么？

<details><summary>参考答案</summary>

观察目录内容可发现每个目录都含 `mod.rs + test.rs`：**带独立测试文件的模块长成目录**。这与是否核心无关（job/latch 同样核心），只反映代码组织习惯——练习 3 的 grep（`grep -n "^mod " rayon-core/src/lib.rs` 会列出 `mod test;` 与 `mod compile_fail;`）能印证这一点。
</details>

**练习 3**：在 `rayon-core` 里 grep `ParallelIterator`，预计能找到几处？为什么？

<details><summary>参考答案</summary>

预计 0 处（忽略注释）。内核只认「闭包任务」，迭代器概念完全住在 `rayon` 上层；若在内核见到该词，只可能是文档注释里的说明性引用。这条「找不到」本身就是分层干净的证据。
</details>

## 5. 综合实践

**任务：制作你自己的「rayon 全景地图」页。**

把本讲三个实践合并成一页可长期维护的笔记（Markdown 或纸笔均可）：

1. **画三层依赖图**（4.1.4）：三个方框、两条 `path` 依赖箭头，每条箭头标注 Cargo.toml 行号；在 `rayon → rayon-core` 箭头旁注明「12 条 pub use 再导出（src/lib.rs L107-L118）」。
2. **给 `rayon` 层标叶子**：在 `rayon` 方框内列出 `src/` 的 8 个公开数据源模块（iter、slice、collections、str、string、range、range_inclusive、option/result/array/vec 可合并为「std 镜像组」），并标出 `ParallelIterator`（src/iter/mod.rs L359）的位置。
3. **给 `rayon-core` 层标四组模块**：任务原语（join/scope/spawn/broadcast）、任务表示（job/latch/unwind）、线程与调度（registry/thread_pool/sleep），在 `join`、`ThreadPool` 处标注行号（join/mod.rs L93、thread_pool/mod.rs L46）。
4. **画一条穿越线**：从 `input.par_iter().map(...).sum()` 出发，画一条线穿过 `src/iter` → plumbing → `rayon-core::join` → `registry.rs` 主循环，表示一次并行计算的完整路径。这条线上的每个站点就是单元二到单元五的课程目录——**本图的穿越线就是本手册的学习路线**。

验收标准：不看讲义，能对着自己的图回答「`rayon::join` 的定义在哪个文件第几行」「`src/iter/flat_map.rs` 为什么存在」「为什么 demo 不能直接依赖 rayon-core 的问题不存在」。

## 6. 本讲小结

- 仓库是**严格单向的三层结构**：`rayon-demo` → `rayon` → `rayon-core`，依赖全部由各 Cargo.toml 的 `path` 声明，可在行号级核实。
- `rayon` 上层**镜像 std 的模块结构**（option/collections/str...），外加两个特殊角色：`src/iter/`（迭代器定义 + 「一适配器一文件」）与 `src/delegate.rs`（委托宏）。
- `rayon` 入口的 12 条 `pub use rayon_core::...` 把调度 API 原样搬给用户，所以日常只需依赖 `rayon` 一个 crate。
- `rayon-core` 只有约 15 个文件，按「任务原语 / 任务表示 / 线程与调度」分组；`ThreadPoolBuilder` 定义在 lib.rs，`Registry` 是 `pub(super)` 的进程单例，外界只见 `ThreadPool`。
- 三大 API 定位：`ParallelIterator` → src/iter/mod.rs:359；`join` → rayon-core/src/join/mod.rs:93；`ThreadPool` → rayon-core/src/thread_pool/mod.rs:46。
- 两份藏在外围的设计文档（`sleep/README.md`、`iter/plumbing/README.md`）分别是单元五和单元四的官方教材，现在知道位置即可。

## 7. 下一步学习建议

本讲是地图篇，到此入门单元的「环境 + 用法 + 地图」三件套已齐（u1-l1～u1-l4）。接下来两条路：

- **推荐主线**：进入单元二第 1 讲《ParallelIterator 与 IndexedParallelIterator》（u2-l1），带着本讲的地图去读 `src/iter/mod.rs` 的两大 trait——你已经知道它们在 L359 与 L2449。
- **支线（偏爱底层者）**：若对调度更感兴趣，可直接跳到 u5-l1《join：最小的并行原语》从 `rayon-core/src/join/mod.rs:93` 开始，之后再回头补单元二；不过官方推荐顺序仍是先上层后内核，因为 plumbing（单元四）是连接两层的桥。

无论走哪条路，遇到陌生文件时先回到本讲的地图上找它的位置——这张图会陪你看完整个手册。
