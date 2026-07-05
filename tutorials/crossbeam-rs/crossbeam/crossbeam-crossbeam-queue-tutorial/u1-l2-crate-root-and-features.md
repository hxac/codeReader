# 目录结构与 crate 入口：lib.rs、feature 与条件编译

## 1. 本讲目标

上一篇我们认识了 `crossbeam-queue` 的定位（有界 `ArrayQueue` 与无界 `SegQueue` 两个 MPMC 无锁队列）。本讲把视角落到「工程层面」：当我们写下 `use crossbeam_queue::ArrayQueue;` 时，编译器到底从哪里把这个类型找出来？为什么这个 crate 既能跑在标准的 `std` 程序里，也能跑在 `no_std + alloc` 的嵌入式/内核环境里？

学完本讲你应该能够：

1. 说出 `crossbeam-queue` 的目录布局，以及 `src/` 下每个模块的职责。
2. 解释 `lib.rs` 作为 crate 根（crate root）的作用，以及 `//!` 文档注释的位置。
3. 说明 `#![no_std]`、`extern crate alloc/std`、`feature = "std"/"alloc"` 三者如何配合，控制一个模块是否被编译。
4. 解释 `target_has_atomic = "ptr"` 守卫的含义，以及为什么它和 `feature = "alloc"` 一起决定了 `ArrayQueue`/`SegQueue` 是否被导出。
5. 用 `cargo` 在三种 feature 配置下构建本 crate，并解释每一种的结果。

## 2. 前置知识

在进入源码之前，先用通俗语言建立几个 Rust 工程概念。如果你已经熟悉，可以快速跳过。

- **crate 与 crate root**：Rust 的一个编译单元叫一个 crate。库型 crate 必须有一个「根文件」，里面可以写模块声明（`mod xxx;`）、导出（`pub use ...`）和 crate 级属性（`#![...]`）。在 `crossbeam-queue` 里，这个根文件就是 `src/lib.rs`。
- **属性 `#![...]` 与 `#[...]`**：带感叹号 `#![...]` 的属性作用于「整个 crate / 当前模块」，称为 inner attribute；不带感叹号的 `#[...]` 作用于「紧随其后的那个项」。本讲里 `#![no_std]`、`#![warn(...)]` 都是 crate 级的。
- **feature（特性）**：Cargo 的 feature 是一组编译期开关，写在 `Cargo.toml` 的 `[features]` 表里。代码里用 `#[cfg(feature = "xxx")]` 来判断某个 feature 是否被启用，从而决定一段代码要不要参与编译。
- **`std` 与 `alloc` 与 `core`**：Rust 标准库分三层。`core` 是最底层、不依赖操作系统的（连堆分配都没有）；`alloc` 在 `core` 之上，提供 `Box`、`Vec` 等需要全局分配器的类型；`std` 在 `alloc` 之上，再加上文件、线程、网络等操作系统相关功能。`#![no_std]` 的意思是「不自动引入 `std`」，但可以手动 `extern crate alloc;` 或 `extern crate std;` 来按需引入。
- **原子操作（atomic）**：`AtomicUsize`、`AtomicPtr` 这类类型提供可在多线程间安全读写的变量，是构建无锁数据结构的基础。不是所有 CPU 平台都支持指针宽度（`usize` 宽度）的原子操作，Rust 用 `target_has_atomic = "ptr"` 来判断当前目标是否支持。
- **`extern crate` 语句**：在旧版 Rust（2015 edition）里，引用外部 crate 必须显式写 `extern crate xxx;`。从 2018 edition 起，`Cargo.toml` 里列出的依赖会自动引入，但 `#![no_std]` crate 里想用 `alloc` 或 `std`（它们不是普通依赖，而是和标准库绑定的）时，仍需要显式 `extern crate alloc;` / `extern crate std;`。

## 3. 本讲源码地图

整个 crate 非常精简，全部跟踪在 git 里的文件如下：

| 文件 | 作用 | 本讲是否精读 |
| --- | --- | --- |
| `src/lib.rs` | crate 根：文档注释、`#![no_std]`、feature/原子守卫、模块声明与 `pub use` 导出 | ✅ 本讲核心 |
| `Cargo.toml` | 包元数据与 `[features]`（`default`/`std`/`alloc`）、依赖 | ✅ 本讲核心 |
| `src/array_queue.rs` | `ArrayQueue`（有界 MPMC 队列）的实现 | 后续 u2 精读，本讲只看它「何时被编译」 |
| `src/seg_queue.rs` | `SegQueue`（无界分段 MPMC 队列）的实现 | 后续 u3 精读，本讲只看它「何时被编译」 |
| `src/alloc_helper.rs` | 内部用的分配器封装（`allocate` / `allocate_zeroed` / `deallocate`），仅 `seg_queue` 使用 | u4-l5 精读 |
| `tests/array_queue.rs` | `ArrayQueue` 的集成测试 | u1-l3 / u4-l4 使用 |
| `tests/seg_queue.rs` | `SegQueue` 的集成测试 | u1-l3 / u4-l4 使用 |
| `README.md` / `CHANGELOG.md` | 用户文档与版本变更记录 | u1-l1 已介绍 |

本讲主要读两个文件：`src/lib.rs`（35 行）和 `Cargo.toml`（features 段）。它们共同回答一个问题——**「这个 crate 在不同编译配置下，到底会产出什么」**。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：crate 根与文档注释、`no_std` 与 `extern crate`、feature 开关、`target_has_atomic` 守卫与 `pub use` 导出。它们环环相扣，共同决定了最终导出。

### 4.1 crate 根 `lib.rs`：文档注释与 crate 级属性

#### 4.1.1 概念说明

一个库型 crate 的「入口」就是它的 crate 根文件。对 `crossbeam-queue` 而言，根文件是 `src/lib.rs`。它本身几乎不包含业务逻辑（真正的队列算法在 `array_queue.rs` 和 `seg_queue.rs` 里），它只做三件事：

1. 用 `//!` 写一段 crate 级的文档注释（会渲染成 docs.rs 上的首页说明）。
2. 用 `#![...]` 设置 crate 级属性（`#![no_std]`、文档测试选项、lint 警告）。
3. 用 `mod` + `pub use` 把内部模块组装成对外的公共 API。

这种「根文件只做装配、算法放在子模块」的写法是 Rust 库的常见组织方式，便于读者一眼看清「这个 crate 对外暴露了什么」。

#### 4.1.2 核心流程

当编译器处理 `src/lib.rs` 时，顺序大致是：

1. 解析顶部 `//! ...` 文档注释，作为整个 crate 的文档首页。
2. 应用各条 `#![...]` crate 级属性（如 `#![no_std]` 改变预导入、`#![warn(...)]` 打开一组 lint）。
3. 根据每条 `mod xxx;` 上方的 `#[cfg(...)]`，决定该模块是否参与编译。
4. 根据最末 `pub use ...` 上方的 `#[cfg(...)]`，决定是否对外导出 `ArrayQueue`/`SegQueue`。

3、4 两步是本讲后半部分的重点（feature 与原子守卫）。

#### 4.1.3 源码精读

先看 crate 根的文档注释，它直接说明了本 crate 提供的两个类型：

[src/lib.rs:1-6](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L1-L6) —— crate 级 `//!` 文档注释，介绍 `ArrayQueue`（有界、构造时一次性分配）与 `SegQueue`（无界、按需分段分配）。

```rust
//! Concurrent queues.
//!
//! This crate provides concurrent queues that can be shared among threads:
//!
//! * [`ArrayQueue`], a bounded MPMC queue that allocates a fixed-capacity buffer on construction.
//! * [`SegQueue`], an unbounded MPMC queue that allocates small buffers, segments, on demand.
```

注意两点：第一，`//!`（两个斜杠加感叹号）是「inner doc comment」，作用于它所在的模块/crate，所以它成了 docs.rs 首页的文字；相对地，`///` 是「outer doc comment」，作用于紧跟其后的那个项（函数、结构体等）。第二，文档里的 ``[`ArrayQueue`] `` 是一个 intra-doc 链接，rustdoc 会自动把它指向本 crate 导出的 `ArrayQueue` 类型——前提是它真的被 `pub use` 导出了（见 4.4）。

接下来是两条 crate 级属性：

[src/lib.rs:9-19](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L9-L19) —— 文档测试的默认配置与一组 lint 警告。

```rust
#![doc(test(
    no_crate_inject,
    attr(allow(dead_code, unused_assignments, unused_variables))
))]
#![warn(
    missing_docs,
    unsafe_op_in_unsafe_fn,
    clippy::alloc_instead_of_core,
    clippy::std_instead_of_alloc,
    clippy::std_instead_of_core
)]
```

简单解释这两条属性，它们都服务于「`no_std` 友好 + 文档严格」这个目标：

- `#![doc(test(...))]`：rustdoc 会把文档注释里的代码块当作测试来跑。`no_crate_inject` 表示「在文档测试里不要自动 `extern crate` 本 crate」；`attr(allow(...))` 让文档示例里可以写出「看起来没用」的变量而不报警告（示例代码常常只演示用法，不真正消费结果）。
- `#![warn(...)]`：把以下问题升级为警告。
  - `missing_docs`：公开项没有文档注释就警告——所以本 crate 的每个 `pub` 项都有 `///`。
  - `unsafe_op_in_unsafe_fn`：即便在 `unsafe fn` 内部，做 unsafe 操作也要显式写 `unsafe { }` 块（这是当代 Rust 推荐的写法，后续读源码时会反复看到）。
  - `clippy::alloc_instead_of_core` / `clippy::std_instead_of_alloc` / `clippy::std_instead_of_core`：能用更底层的 `core`/`alloc` 就不要用 `std`，逼着代码保持 `no_std` 可移植性（例如优先 `core::mem::MaybeUninit` 而不是 `std::mem::MaybeUninit`）。

#### 4.1.4 代码实践

**目标**：直观感受 crate 根文档注释如何变成「对外文档」。

**步骤**：

1. 在本 crate 目录下运行 `cargo doc --no-deps --open`。
2. 在浏览器里打开生成的文档首页。

**需要观察的现象**：首页正文正是 `src/lib.rs` 第 1–6 行那段 `//!` 注释；并且在类型列表里能看到 `ArrayQueue` 和 `SegQueue`。

**预期结果**：首页文字与源码注释一致；两个类型作为「Re-exports」或「Structs」出现。如果你关掉 `alloc` feature（见综合实践），重新 `cargo doc --no-deps`，这两个类型会消失——这就直观验证了「文档里的 intra-doc 链接依赖 `pub use` 是否真的生效」。

> 如果无法本地运行 `cargo doc`，可改为在 <https://docs.rs/crossbeam-queue> 上对照阅读，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：把 ``[`ArrayQueue`] `` 这种写法叫什么？它和普通 Markdown 链接 `[文字](url)` 有什么区别？

**答案**：它叫 intra-doc link（文档内链接）。普通 Markdown 链接需要你手写完整 URL；intra-doc link 只写目标项的名字，rustdoc 会在编译期解析它指向哪个类型/函数，并在重构改名时自动跟着更新，链接失效时会直接报编译错误。

**练习 2**：`//!` 和 `///` 有什么区别？如果把 `src/lib.rs` 顶部的 `//!` 误写成 `///`，会发生什么？

**答案**：`//!` 是 inner doc，作用于「当前模块/crate」本身；`///` 是 outer doc，作用于「下一个项」。如果误写成 `///`，rustc 会发现这条文档注释后面没有紧跟任何「项」（下一个 token 是 `#![no_std]` 属性），从而报「expected item after doc comment」之类的错误。

**练习 3**：`#![warn(missing_docs)]` 和 `#![deny(missing_docs)]` 的区别是什么？

**答案**：`warn` 把「缺文档」变成**警告**（warning），编译仍能通过；`deny` 把它变成**错误**（error），编译会失败。本 crate 用 `warn`，所以缺文档不会让构建直接挂掉，但会在 CI 里被看到。

---

### 4.2 `no_std` 与 `extern crate`：按需引入 `alloc` / `std`

#### 4.2.1 概念说明

默认情况下，Rust 程序会自动链接 `std`（标准库），并且自动 `extern crate std;`，所以你直接写 `use std::sync::atomic::AtomicUsize;` 就能用。但 `crossbeam-queue` 想同时支持「有操作系统」和「裸机/内核（no_std）」两种环境，于是它在根文件里写了 `#![no_std]`，**主动放弃**默认的 `std` 预导入，转而手动声明「我现在到底需要 `alloc` 还是 `std`」。

关键在于：这种「需要才引入」不是无条件的，而是受 feature 控制——只有当用户启用了 `alloc`（或更上层的 `std`）时，才 `extern crate alloc;`。这就是为什么同一个源码树能在三种配置下编译出不同的结果。

#### 4.2.2 核心流程

`#![no_std]` 之后，预导入里只剩 `core`（不再有 `std`）。随后：

1. 若 `alloc` feature 开启 **且** 当前目标支持指针原子 → `extern crate alloc;`，于是 `use alloc::boxed::Box;` 这类写法可用。
2. 若 `std` feature 开启 → `extern crate std;`，于是 `use std::...` 可用（本 crate 内部其实几乎不直接用 `std`，更多是为了让依赖 `crossbeam-utils` 也能用上 `std`，见 4.3）。

两条 `extern crate` 各自带自己的 `#[cfg(...)]`，彼此独立。

#### 4.2.3 源码精读

`#![no_std]` 只有这一行，但它是整个「可移植」故事的起点：

[src/lib.rs:8](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L8) —— 声明本 crate 不依赖默认的 `std` 预导入。

```rust
#![no_std]
```

紧接着是两条条件化的 `extern crate`：

[src/lib.rs:21-24](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L21-L24) —— 在 `alloc`+原子守卫下引入 `alloc`；在 `std` 下引入 `std`。

```rust
#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]
extern crate alloc;
#[cfg(feature = "std")]
extern crate std;
```

注意两个细节：

- `alloc` 的引入条件是 `all(feature = "alloc", target_has_atomic = "ptr")`，**两个条件取交集**（`all`）。`std` 的引入条件只有 `feature = "std"`。为什么 `std` 不需要再判断 `target_has_atomic`？因为只要能用 `std`，就一定在一个「正常」的有操作系统、有原子的环境里，`std` 本身就隐含了这些能力，不需要单独守卫。
- 因为 `extern crate alloc;` 受 `feature = "alloc"` 控制，所以子模块里 `use alloc::boxed::Box;`（见 `array_queue.rs` 第 6 行、`seg_queue.rs` 第 1 行）也只有 `alloc` 开启时才合法——这和下面 4.4 里模块守卫是一致的，整条链路是自洽的。

可以对照看子模块如何使用 `alloc`：

[src/array_queue.rs:6](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L6) —— `ArrayQueue` 用 `alloc::boxed::Box` 在堆上分配缓冲区。

```rust
use alloc::boxed::Box;
```

这正是 `#![no_std]` crate 的典型写法：用 `alloc::boxed::Box` 而不是 `std::boxed::Box`，二者其实是同一个类型，但前者在 `no_std + alloc` 下也能编译。

#### 4.2.4 代码实践

**目标**：理解「关掉 `alloc` 会破坏哪些 `use`」。

**步骤**：

1. 在 `src/array_queue.rs` 第 6 行 `use alloc::boxed::Box;` 处停留，问自己：如果 `alloc` feature 没开，这一行会怎样？
2. 在脑中（不要真改源码）推演：`extern crate alloc;` 缺失 → `alloc` crate 不在作用域 → `use alloc::boxed::Box;` 报「unresolved import」。

**需要观察的现象**：这是一个「源码阅读型实践」——不需要运行，只需确认「模块顶部的 `use alloc::...` 与 `lib.rs` 里的 `#[cfg(feature="alloc")] extern crate alloc;` 是同生同灭的」。

**预期结果**：你能解释为什么 4.4 节里 `mod array_queue;` 上方必须也挂 `#[cfg(all(feature="alloc", target_has_atomic="ptr"))]`——否则在 `alloc` 关闭时，模块体里的 `use alloc::...` 会直接编译失败。

#### 4.2.5 小练习与答案

**练习 1**：`#![no_std]` 之后，预导入里还有哪个 crate 一定可用？

**答案**：`core`。`#![no_std]` 只是把默认的 `std` 预导入换成了 `core` 预导入，所以 `use core::mem::MaybeUninit;` 这种写法在任何配置下都成立（这也是为什么子模块里大量用 `core::...`）。

**练习 2**：为什么 `extern crate std;` 的守卫里没有 `target_has_atomic = "ptr"`？

**答案**：因为 `feature = "std"` 已经隐含了「运行在带操作系统、带原子的常规平台上」。`std` 本身就要求这些能力，没必要再单独判断；而 `alloc` 可以脱离 `std` 用于某些只支持部分原子操作的嵌入式目标，所以需要额外用 `target_has_atomic` 把「没有指针原子」的冷门平台排除掉。

**练习 3**：在 2018 edition 之后，普通依赖（如 `crossbeam-utils`）不需要写 `extern crate`，为什么 `alloc`/`std` 还要写？

**答案**：`alloc` 和 `std` 不是普通的 `Cargo.toml` 依赖，而是和工具链绑定的「标准库组件」。自动引入机制只处理 `Cargo.toml` 里 `[dependencies]` 的 crate；`alloc`/`std` 是否可用由 `#![no_std]` 和 feature 决定，所以需要显式 `extern crate` 来把它们引入作用域。

---

### 4.3 feature 开关：`Cargo.toml` 里 `std` / `alloc` 的依赖关系

#### 4.3.1 概念说明

Feature 的定义不在 Rust 代码里，而在 `Cargo.toml` 的 `[features]` 表里。`crossbeam-queue` 一共只有两个有意义的 feature：`std` 和 `alloc`，并且 `std` 默认开启。它们之间存在一条「依赖链」：开 `std` 会自动带上 `alloc`，还会把依赖 `crossbeam-utils` 的 `std` 也打开。

理解这条依赖链，是理解「为什么 `--no-default-features --features alloc` 也能编译、而什么都不开就不行」的关键。

#### 4.3.2 核心流程

`[features]` 表的语义：

- `default = ["std"]`：不加任何 `--features` 参数时，默认启用 `std`。
- `std = ["alloc", "crossbeam-utils/std"]`：启用 `std` 这个 feature 时，会**连带**启用本 crate 的 `alloc`，以及依赖项 `crossbeam-utils` 的 `std` feature。
- `alloc = []`：`alloc` 是一个「空」feature——它不连带启用别的 feature，但它本身作为一个「名字」被 `lib.rs` 里的 `#[cfg(feature = "alloc")]` 用来做条件编译。

用一条依赖图表示：

```
default ──► std ──► alloc            (本 crate 内部)
                ╰──► crossbeam-utils/std   (依赖项的 feature)
```

所以打开 `std` 等价于「`alloc` + `crossbeam-utils/std` + `std` 三个名字都为真」。

#### 4.3.3 源码精读

features 定义在 manifest 里：

[Cargo.toml:26-37](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/Cargo.toml#L26-L37) —— `[features]` 段：`default`/`std`/`alloc` 三个特性及它们的依赖关系。

```toml
[features]
default = ["std"]

# Enable to use APIs that require `std`.
# This is enabled by default.
std = ["alloc", "crossbeam-utils/std"]

# Enable to use APIs that require `alloc`.
# This is enabled by default and also enabled if the `std` feature is enabled.
#
# NOTE: Disabling both `std` *and* `alloc` features is not supported yet.
alloc = []
```

特别注意那条注释 `NOTE: Disabling both std *and* alloc features is not supported yet.`——它的意思是：**两个都不开**的配置「不被支持」。注意「不被支持」≠「编译报错」：从 4.4 节会看到，两个都不开时，所有 `mod` 和 `pub use` 都会被 cfg 掉，crate 会编译成一个「空壳」（没有任何导出），能通过编译但没有任何功能。综合实践里我们会亲手验证这一点。

再看依赖项是怎么和 feature 联动的：

[Cargo.toml:39-40](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/Cargo.toml#L39-L40) —— 对 `crossbeam-utils` 的依赖，关闭了它的默认 feature。

```toml
[dependencies]
crossbeam-utils = { version = "0.8.18", path = "../crossbeam-utils", default-features = false }
```

`default-features = false` 表示：默认**不**把 `crossbeam-utils` 的 `default` feature（也就是它的 `std`）打开。这样本 crate 在 `no_std + alloc` 模式下，`crossbeam-utils` 也能跟着保持 `no_std`。只有当本 crate 的 `std` feature 打开时，上面 `std = [..., "crossbeam-utils/std"]` 才会把依赖项的 `std` 顺带打开。这是一种「feature 透传」的常见写法，保证两个 crate 的 `std`/`no_std` 状态始终一致。

补充一个元信息点，说明本 crate 对外宣传的「可移植性」边界：

[Cargo.toml:14-16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/Cargo.toml#L14-L16) —— `description` 与 `keywords`/`categories`，其中 `categories` 含 `no-std`，表明这是个面向 `no_std` 生态的 crate。

```toml
description = "Concurrent queues"
keywords = ["queue", "mpmc", "lock-free", "producer", "consumer"]
categories = ["concurrency", "data-structures", "no-std"]
```

#### 4.3.4 代码实践

**目标**：把 feature 依赖关系画清楚。

**步骤**：

1. 读上面的 `[features]` 段，在纸上画出三个圆圈 `std`、`alloc`、`crossbeam-utils/std`。
2. 用箭头标出「开谁会顺带开谁」：`std → alloc`，`std → crossbeam-utils/std`。
3. 标出 `default → std`。

**需要观察的现象/预期结果**：你应当得到一张有向图，从图上能直接读出：「只开 `alloc`」时，`std` 为假、`crossbeam-utils/std` 为假、`alloc` 为真；「什么都不开」时三者全为假。这张图就是综合实践中三种配置的预测依据。

#### 4.3.5 小练习与答案

**练习 1**：`alloc = []` 方括号里是空的，这代表什么？

**答案**：代表 `alloc` 是一个「纯开关」feature，启用它不会连带启用任何其他 feature；它只是让 `#[cfg(feature = "alloc")]` 这个条件为真。空列表是合法且常见的写法。

**练习 2**：如果用户写 `--no-default-features --features std`，最终哪些 feature 名字为真？

**答案**：`std` 为真，并且根据 `std = ["alloc", "crossbeam-utils/std"]`，`alloc` 和 `crossbeam-utils/std` 也为真。也就是说，这和默认配置（`default`）的效果几乎一样，只是 `default` 这个名字本身不为真（但它只是个聚合入口，没有代码用 `#[cfg(feature = "default")]`，所以无影响）。

**练习 3**：为什么对 `crossbeam-utils` 要写 `default-features = false`？

**答案**：为了让本 crate 在 `no_std + alloc` 模式下，不被依赖项的默认 `std` feature「污染」。如果这里不关掉默认 feature，那么即使用户指定 `--no-default-features --features alloc`，`crossbeam-utils` 仍会带上 `std`，导致整个依赖树偷偷变成 `std` 模式，违背 `no_std` 的初衷。配合 `std = [..., "crossbeam-utils/std"]`，就把「本 crate 是否用 std」和「依赖是否用 std」绑在了同一个开关上。

---

### 4.4 `target_has_atomic` 守卫与 `pub use` 导出

#### 4.4.1 概念说明

到目前为止，我们只讨论了 feature 这一个条件。但本 crate 还有一个**与目标平台相关**的条件：`target_has_atomic = "ptr"`。它表示「当前编译目标是否支持指针宽度（通常是 `usize` 宽度）的原子操作」。无锁队列底层依赖 `AtomicUsize`、`AtomicPtr`，如果目标 CPU 不支持这种原子操作，整个算法就无从谈起。

于是 `lib.rs` 把 **feature 条件** 和 **平台条件** 用 `all(...)` 组合起来，作为每个模块、每条导出的守卫。这一节是本讲的高潮：它把前面所有概念串起来，解释了「为什么 `ArrayQueue`/`SegQueue` 有时存在、有时不存在」。

#### 4.4.2 核心流程

模块声明与导出全部挂在同一个组合守卫下：

```
#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]
mod alloc_helper;

#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]
mod array_queue;

#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]
mod seg_queue;

#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]
pub use crate::{array_queue::ArrayQueue, seg_queue::SegQueue};
```

含义：只有当 **`alloc` feature 开启** 且 **目标支持指针原子** 时，三个内部模块才会被编译，`ArrayQueue`/`SegQueue` 才会被导出。否则这些类型在公共 API 里根本不存在。

用真值表总结（假设目标是常见的 `x86_64`，`target_has_atomic = "ptr"` 为真）：

| 配置 | `feature="alloc"` | 模块编译? | 导出? | 结果 |
| --- | --- | --- | --- | --- |
| 默认（`std`） | 真（被 `std` 带上） | 是 | 是 | 两个队列可用 |
| `--no-default-features --features alloc` | 真 | 是 | 是 | 两个队列可用（`no_std + alloc`） |
| `--no-default-features` | 假 | 否 | 否 | 空壳 crate，无导出 |

如果在「不支持指针原子」的冷门目标上（即使 `alloc` 开着），第二列的平台条件为假，结果同样是空壳。

#### 4.4.3 源码精读

模块声明部分，三个 `mod` 共享同一个守卫：

[src/lib.rs:26-31](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L26-L31) —— 三个内部模块都受 `alloc`+原子守卫保护。

```rust
#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]
mod alloc_helper;
#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]
mod array_queue;
#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]
mod seg_queue;
```

最后是对外导出，`pub use` 把两个子模块里的类型「提升」到 crate 根，让用户能写 `crossbeam_queue::ArrayQueue`：

[src/lib.rs:33-34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L33-L34) —— 在同一守卫下，把 `ArrayQueue` 与 `SegQueue` 导出为 crate 的公共 API。

```rust
#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]
pub use crate::{array_queue::ArrayQueue, seg_queue::SegQueue};
```

把 4.2、4.3、4.4 串起来看，整个 crate 的「编译产物」是由这一行 `[features]` 和这一组 `#[cfg(...)]` 共同决定的：

- `mod xxx;` 守卫保证「模块体内部的 `use alloc::...`」不会在 `alloc` 关闭时报错（因为整个模块都不编译）。
- `pub use` 守卫保证「文档注释里的 ``[`ArrayQueue`] `` 链接」不会在导出缺失时变成死链（因为整段都不会被处理）。
- `target_has_atomic = "ptr"` 保证「模块体内部的 `AtomicUsize`/`AtomicPtr` 用法」不会在无原子平台上编译失败。

这是一种非常工整的「条件编译闭环」：每一个平台/feature 相关的代码片段，都有对应的 cfg 守卫兜底。

#### 4.4.4 代码实践

**目标**：亲手验证「关掉 `alloc` 后，`ArrayQueue` 不再可导入」。

**步骤**（不改源码，只用临时小程序验证；可在 `/tmp` 下另建一个依赖本 crate 的项目，或直接看本 crate 的 `cargo doc`）：

1. 运行 `cargo doc --no-deps --no-default-features`，查看生成的文档。
2. 对比 `cargo doc --no-deps --features alloc`（注意要先 `--no-default-features` 再加 `alloc`，或直接默认）的结果。

**需要观察的现象**：`--no-default-features` 下生成的文档里，`ArrayQueue`/`SegQueue` 都消失了（因为 `pub use` 被 cfg 掉）；带 `alloc` 时它们出现。

**预期结果**：与真值表一致。如果无法运行 `cargo doc`，标注「待本地验证」，并直接在 docs.rs 上用「Feature flags」下拉切换观察（docs.rs 支持 `?features=alloc` 之类的参数）。

#### 4.4.5 小练习与答案

**练习 1**：`#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]` 里的 `all` 能不能换成 `any`？会带来什么后果？

**答案**：不能。`all` 表示「两个条件同时成立才编译」，`any` 表示「任一成立就编译」。换成 `any` 后，即便 `alloc` 没开、只要平台支持原子，就会去编译 `mod array_queue;`，而模块体里有 `use alloc::boxed::Box;`，此时 `alloc` 未引入，会直接编译失败。所以这里必须是 `all`。

**练习 2**：`pub use crate::{array_queue::ArrayQueue, seg_queue::SegQueue};` 这行如果不加 `#[cfg(...)]`，在 `--no-default-features` 下会发生什么？

**答案**：因为 `mod array_queue;` 在 `alloc` 关闭时不编译，`array_queue` 这个模块路径根本不存在，于是 `pub use crate::array_queue::ArrayQueue` 会报「unresolved module」之类的编译错误。所以 `pub use` 必须和 `mod` 共享同一个 cfg 守卫，二者缺一不可。

**练习 3**：`target_has_atomic = "ptr"` 中的 `"ptr"` 指的是哪种整数宽度的原子？

**答案**：它指「指针宽度」的原子，也就是和 `usize` 同宽（在 64 位平台上是 64 位，32 位平台上是 32 位）。本 crate 用 `AtomicUsize`（保存 head/tail/stamp）和 `AtomicPtr`（保存块指针），它们都要求平台支持指针宽度的原子操作，所以用 `"ptr"` 作为守卫。

---

## 5. 综合实践

本讲的核心动手任务：用 `cargo` 在三种 feature 配置下构建本 crate，记录哪些能编译、导出什么，并解释原因。这是把第 4 节全部知识串起来的一步。

### 实践目标

验证「feature 开关 + `target_has_atomic` 守卫」如何决定 crate 的最终产物，亲手看到「`--no-default-features` 会得到一个空壳 crate」。

### 操作步骤

> 在 `crossbeam-queue` 目录下执行。本 crate 的测试和构建都是只读于源码的（不会修改你的源文件），可以放心运行。

**配置 A：默认（`std`）**

```bash
cargo build
cargo build --release
```

**配置 B：`no_std + alloc`**

```bash
cargo build --no-default-features --features alloc
```

**配置 C：什么都不开**

```bash
cargo build --no-default-features
```

为了更直观地看到「导出了什么」，建议额外跑：

```bash
cargo doc --no-deps                                   # 默认配置的文档
cargo doc --no-deps --no-default-features --features alloc
cargo doc --no-deps --no-default-features
```

并在浏览器里对比三次生成的文档首页是否有 `ArrayQueue`/`SegQueue`。

### 需要观察的现象与预期结果

| 配置 | 预期能否编译 | 预期是否导出 `ArrayQueue`/`SegQueue` | 原因 |
| --- | --- | --- | --- |
| A：默认（`std`） | ✅ 能 | ✅ 是 | `std` 带上 `alloc`，平台有原子 → 守卫通过 |
| B：`--features alloc` | ✅ 能 | ✅ 是 | `alloc` 为真，平台有原子 → 守卫通过（`no_std + alloc` 模式） |
| C：`--no-default-features` | ✅ 能（编译成空壳） | ❌ 否 | `alloc` 为假 → 所有 `mod`/`pub use` 被 cfg 掉，crate 无导出 |

关键解释：

- 配置 A、B 都能正常编译并导出两个队列，区别只是 A 链接了 `std`（并顺带打开 `crossbeam-utils/std`），B 是纯 `no_std + alloc`。
- 配置 C 能通过编译（一个没有任何 `pub` 项的 `no_std` crate 是合法的），但**什么也不导出**——这正是 `Cargo.toml` 里那句 `Disabling both std *and* alloc features is not supported yet` 的含义：不是「编译失败」，而是「没有功能」。如果你想真的使用队列，就必须至少开 `alloc`。

> 关于配置 C 的精确编译器输出（是否有 warning、空 crate 的提示文案），**待本地验证**：不同 rustc 版本可能给出不同的提示文字，但「能编译、无导出」这一结论由源码里的 cfg 守卫唯一确定。

### 进阶观察（可选）

如果你想进一步确认 `target_has_atomic` 的作用，可以尝试为一个「没有指针原子」的目标交叉编译（这通常需要特定工具链，例如某些 AVR/MSP430 目标）。预期：即便开了 `alloc`，模块也会因 `target_has_atomic = "ptr"` 为假而被 cfg 掉。此项需要对应目标的工具链，**待本地验证**。

## 6. 本讲小结

- `crossbeam-queue` 的源码极其精简：`src/` 下只有 `lib.rs`（crate 根）、`array_queue.rs`、`seg_queue.rs`、`alloc_helper.rs` 四个文件，外加 `tests/` 两个集成测试。
- `src/lib.rs` 几乎不含算法，只做三件事：写 crate 级 `//!` 文档、设置 `#![no_std]` 等属性、用 `mod` + `pub use` 组装公共 API。
- `#![no_std]` 主动放弃默认的 `std` 预导入，改用条件化的 `extern crate alloc;` / `extern crate std;` 按需引入，使 crate 同时支持 `std` 与 `no_std + alloc`。
- `[features]` 里 `default = ["std"]`，`std = ["alloc", "crossbeam-utils/std"]`：开 `std` 会连带开 `alloc` 和依赖项的 `std`，形成一条干净的 feature 依赖链。
- 每个模块和 `pub use` 都挂在 `#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]` 下：`alloc` 保证堆分配可用，`target_has_atomic = "ptr"` 保证无锁算法所需的指针原子可用。
- 三种典型配置：默认（`std`）与 `--features alloc` 都能导出两个队列；`--no-default-features` 会编译成一个**无导出的空壳 crate**（「不被支持」指无功能，而非编译失败）。

## 7. 下一步学习建议

到这里，你已经能从工程层面「看懂」这个 crate 的骨架：知道它由哪些文件组成、在不同配置下编译出什么。下一步建议：

1. **u1-l3 快速上手**：实际把 `crossbeam-queue` 加进一个新项目的依赖，跑通 `cargo test`，并写第一个 `push`/`pop` 小程序，把「能编译」变成「能用」。
2. 之后进入第二单元，从 **u2-l1 ArrayQueue 的数据结构** 开始精读 `src/array_queue.rs`，看 `stamp`/`lap`/`Slot` 模型如何落地成无锁有界队列。
3. 如果你对本讲的 `target_has_atomic`、`alloc_helper` 这类底层话题更感兴趣，可以先跳到 **u4-l5 no_std / alloc 支持与自定义全局分配器**，那里会精读 `src/alloc_helper.rs`。

阅读源码时，建议始终把 `src/lib.rs` 的 cfg 守卫放在心里：每当你疑惑「这段代码什么时候生效」，回到本讲那张真值表对照即可。
