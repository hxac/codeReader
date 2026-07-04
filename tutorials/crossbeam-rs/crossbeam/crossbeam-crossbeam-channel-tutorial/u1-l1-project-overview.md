# 项目总览与运行方式

## 1. 本讲目标

本讲是整本学习手册的起点。读完本讲，你应当能够：

- 说清楚 `crossbeam-channel` 这个 crate 是做什么的，它和标准库 `std::sync::mpsc` 是什么关系。
- 看懂仓库的目录结构，能用一句话描述 `src/` 下每个子模块（`channel` / `context` / `counter` / `err` / `flavors` / `select` / `utils` / `waker`）的职责。
- 掌握 `cargo build` / `cargo test` / `cargo bench` 的运行方式。
- 找到库入口 `src/lib.rs`，并完整列出它对外 `pub use` 导出的所有类型、函数与宏。

本讲不要求你已经懂通道的内部实现，只需要你会一点 Rust 基础语法（知道 `struct`、`enum`、`trait`、`use` 是什么即可）。

## 2. 前置知识

在开始之前，先用最朴素的语言建立两个直觉。

**什么是「通道（channel）」？**

通道是线程之间传递消息的「管道」。一头有人往里塞消息（发送方 Sender），另一头有人从中取消息（接收方 Receiver）。它是并发编程里「不要通过共享内存来通信，而要通过通信来共享内存」这一思想的核心工具。

**什么是 mpsc / mpmc？**

- `mpsc`：multi-producer single-consumer，多个发送方、单个接收方。Rust 标准库 `std::sync::mpsc` 就是这种。
- `mpmc`：multi-producer multi-consumer，多个发送方、多个接收方。`crossbeam-channel` 提供的就是 mpmc——接收端也可以克隆出多份，多个线程一起取消息。

**什么是 `no_std`？**

Rust 程序默认链接标准库 `std`。加上 `#![no_std]` 后，代码就不再依赖完整标准库，只能用更底层的 `core`（和可选的 `alloc`）。这能让 crate 在嵌入式、内核等没有操作系统的环境里被使用。本讲后面会看到，`crossbeam-channel` 顶部就声明了 `#![no_std]`，但目前还需要 `std` 才能真正编译运行。

## 3. 本讲源码地图

本讲涉及的关键文件如下表：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目的「名片」：定位、亮点、用法、许可证、最低 Rust 版本。 |
| `Cargo.toml` | crate 的元信息与构建配置：版本号、MSRV、feature 开关、依赖。 |
| `CHANGELOG.md` | 版本演进记录，能帮你理解这个 crate 修过哪些 bug、加了哪些能力。 |
| `src/lib.rs` | 库的入口文件：crate 文档、`#![no_std]`、模块声明、对外 `pub use` 导出。 |
| `src/flavors/mod.rs` | 「六种通道风味（flavor）」的模块汇总声明。 |

> 说明：`crossbeam-channel` 是 `crossbeam-rs/crossbeam` 这个 monorepo 里的一个子 crate，本手册的视角始终落在 `crossbeam-channel/` 这个目录内部。

## 4. 核心概念与源码讲解

### 4.1 项目定位：crossbeam-channel 是什么

#### 4.1.1 概念说明

`crossbeam-channel` 是一个为多生产者多消费者（mpmc）消息传递而设计的通道库。它定位为标准库 `std::sync::mpsc` 的「功能更多、性能更好」的替代品。它的核心能力包括：

- `Sender` 和 `Receiver` 都可以克隆并在多个线程间共享。
- 两种主通道：`bounded`（有界）和 `unbounded`（无界）。
- 三种特殊只读通道：`after`、`tick`、`never`。
- `select!` 宏可以同时等待多个通道操作。
- `Select` 结构可以对「运行时动态构建」的操作列表做选择。
- 通道内部极少使用锁，以追求最大性能。

#### 4.1.2 核心流程

从用户视角看，整个 crate 的使用流程极简：

1. 用 `unbounded()` 或 `bounded(cap)` 创建一个通道，得到 `(Sender, Receiver)`。
2. 发送方调用 `send(msg)`，接收方调用 `recv()`。
3. 需要的话，对 `Sender` / `Receiver` 调用 `.clone()` 在线程间共享。
4. 当不再需要时 `drop` 掉所有发送方或接收方，通道「断开（disconnected）」。

上面这些是「用法」，本讲只让你建立宏观印象；具体 API 的细节在后续讲义 `u1-l2`、`u1-l3` 中展开。

#### 4.1.3 源码精读

项目名片写在 README 开头：

[README.md:15-16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/README.md#L15-L16)

> 这两句说明了 crate 的本质：提供 mpmc 通道，是 `std::sync::mpsc` 的替代品。

紧接着 README 列出了它的「亮点（highlights）」：

[README.md:18-25](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/README.md#L18-L25)

> 这 6 个 bullet 基本对应了后续整本手册要逐个深入的主题：克隆共享、bounded/unbounded、after/tick/never、select! 宏、Select 动态 API、少锁高性能设计。

README 还声明了对 Rust 版本的要求（最低支持 1.74）：

[README.md:47-51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/README.md#L47-L51)

#### 4.1.4 代码实践

**实践目标**：把 `crossbeam-channel` 当作「用户」用起来，建立第一手感性认识。

操作步骤：

1. 在你自己的某个 Rust 项目里，向 `Cargo.toml` 加入依赖：
   ```toml
   [dependencies]
   crossbeam-channel = "0.5"
   ```
2. 在 `main.rs` 里写一个最小示例（**示例代码**，非项目原有）：
   ```rust
   use crossbeam_channel::unbounded;

   fn main() {
       let (s, r) = unbounded();
       s.send("Hello, world!").unwrap();
       assert_eq!(r.recv(), Ok("Hello, world!"));
       println!("ok");
   }
   ```
3. 运行 `cargo run`。

需要观察的现象：程序打印 `ok` 且不 panic。

预期结果：`send` 把消息塞进通道，`recv` 立刻取到同一条消息。如果你在 `recv` 之前再 `drop(s)`，这条消息仍能被收到——这是后续会讲的「断开后剩余消息仍可接收」语义。

> 本示例是「示例代码」，不是仓库里现有的文件；仓库里同款最小示例出现在 `src/lib.rs` 顶部的 crate 文档注释中。

#### 4.1.5 小练习与答案

**练习 1**：`crossbeam-channel` 和 `std::sync::mpsc` 在「接收方数量」上的最大区别是什么？

**参考答案**：标准库 `mpsc` 名字里就表明是 single-consumer（虽然新版也有 `mpsc::Receiver::clone` 之类的限制性扩展），而 `crossbeam-channel` 是原生 mpmc——`Receiver` 可以自由 `clone`，多个线程能同时从同一个通道取消息。

**练习 2**：README 列出的「亮点」里，哪一条最直接关系到「同时等待多个通道」？

**参考答案**：`select!` 宏（以及它底层的 `Select`）——它允许你在一个语句里同时挂多个 `recv` / `send`，谁先就绪就执行谁。

---

### 4.2 构建配置 Cargo.toml：版本、MSRV、feature 与依赖

#### 4.2.1 概念说明

Rust crate 的「身份证」和「构建说明书」就是 `Cargo.toml`。对 `crossbeam-channel` 而言，读懂这个文件能回答四个问题：

- 这个 crate 叫什么、版本多少、最低需要哪个 Rust？（元信息）
- 它有没有「可选功能（feature）」？（编译开关）
- 它依赖哪些别的 crate？（运行依赖）
- 它在测试/基准时额外需要哪些 crate？（开发依赖）

#### 4.2.2 核心流程

`Cargo.toml` 的组织遵循 Cargo 惯例：

1. `[package]` 段：声明 `name` / `version` / `edition` / `rust-version` / `license` 等。
2. `[features]` 段：定义可选 feature。`default` 表示默认开启的集合。
3. `[dependencies]` 段：发布时必须带的运行依赖。
4. `[dev-dependencies]` 段：只在测试、示例、基准里才用到的依赖。

#### 4.2.3 源码精读

版本、Rust 版本要求与 edition：

[Cargo.toml:7-10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/Cargo.toml#L7-L10)

> 当前版本是 `0.5.15`，edition 2021，最低支持 Rust 版本（MSRV）为 `1.74`。

feature 开关是本文件的重点：

[Cargo.toml:26-33](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/Cargo.toml#L26-L33)

> 注意三件事：默认开启 `std`；`std` 会连带要求底层依赖 `crossbeam-utils` 也开 `std`；注释明确写了「禁用 `std` 暂不支持（not supported yet）」。这解释了为什么 `src/lib.rs` 里几乎所有模块都被 `#[cfg(feature = "std")]` 门控——不开 `std` 的话现在还编译不出可用东西。

唯一的运行依赖：

[Cargo.toml:35-36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/Cargo.toml#L35-L36)

> `crossbeam-channel` 只依赖同仓库的 `crossbeam-utils 0.8.18`（开启 `atomic` feature、关闭默认 feature）。这是 monorepo 内用 `path = "../crossbeam-utils"` 做的本地路径依赖。

开发依赖（仅测试/bench 用）：

[Cargo.toml:38-41](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/Cargo.toml#L38-L41)

> `fastrand`（随机数，多用于并发测试打乱）、`rustversion`（按编译器版本条件编译测试）、`signal-hook`（信号处理测试）。它们不会进你最终发布的应用。

#### 4.2.4 代码实践

**实践目标**：验证 MSRV 和 feature 门控的真实效果。

操作步骤：

1. 在仓库 `crossbeam-channel/` 目录下运行 `cargo build`，确认默认（带 `std`）能编译通过。
2. 运行 `cargo build --no-default-features`，尝试关掉默认的 `std`。
3. 观察编译输出。

需要观察的现象：第 1 步成功；第 2 步会因「禁用 std 尚不支持」而出错或缺少关键实现。

预期结果：默认构建正常；关闭 `std` 后无法正常编译出完整通道。这正是 Cargo.toml 注释里那句「NOTE: Disabling `std` feature is not supported yet」的含义。若本地环境不允许完整编译，请标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `std` feature 要写成 `std = ["crossbeam-utils/std"]`？

**参考答案**：因为 `crossbeam-channel` 开启 `std` 时，它依赖的 `crossbeam-utils` 也必须开 `std`（否则 utils 那边走的是 `no_std` 路径，二者对不上）。Cargo 的 feature 传递语法就是把「我开 std」自动转发为「依赖也开 std」。

**练习 2**：`fastrand` 是运行依赖还是开发依赖？普通用户 `cargo build` 自己项目时会下载它吗？

**参考答案**：它是 `[dev-dependencies]`，仅测试/bench 用。普通用户把 `crossbeam-channel` 当依赖引入并 `cargo build` 时，不会拉取 `fastrand`。

---

### 4.3 库入口 src/lib.rs：no_std、模块声明与目录结构

#### 4.3.1 概念说明

`src/lib.rs` 是整个 crate 的「根」。它做三件事：

1. 写 crate 级别的文档注释（就是 docs.rs 上展示的那段说明）。
2. 声明 crate 由哪些子模块组成（`mod xxx;`）。
3. 决定对外暴露哪些东西（`pub use ...`）。

理解了它，你就拿到了一张「项目地图」。

#### 4.3.2 核心流程

`lib.rs` 的组织顺序是：

1. 顶部一段长长的 `//!` 文档注释，用「Hello, world!」和若干分类小节介绍用法。
2. crate 级属性：`#![no_std]`、文档测试属性、一组 `#![warn(...)]` 的 lint。
3. `extern crate alloc;` / `extern crate std;`（在 `std` feature 下）。
4. 一连串 `#[cfg(feature = "std")] mod xxx;` 声明子模块。
5. 一个 `#[doc(hidden)] pub mod internal` 模块，专门给 `select!` 宏用。
6. 最后一个大 `pub use crate::{ ... }`，把对外 API 一次性导出。

#### 4.3.3 源码精读

crate 顶部的定位说明：

[src/lib.rs:1-3](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L1-L3)

> 这段和 README 呼应：再次点明它是 `std::sync::mpsc` 的替代品。

`no_std` 声明与 lint 配置：

[src/lib.rs:328-339](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L328-L339)

> 重点：`#![no_std]` 让 crate 默认不依赖标准库；`#![warn(unsafe_op_in_unsafe_fn, ...)]` 等提示作者在并发 unsafe 代码上非常谨慎。

引入 `alloc` 与 `std`：

[src/lib.rs:341-344](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L341-L344)

> 在 `std` feature 下显式引入 `alloc` 和 `std`——这是 `no_std` crate 里常见的写法，把对标准库的依赖变成「可选项」。

子模块声明（这是「src 模块地图」的源头）：

[src/lib.rs:346-366](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L346-L366)

> 这一段就是每个子模块的「出生证明」。结合每个文件的实际内容，可以把它们的职责归纳成下表（这是本讲要求你掌握的核心知识）：

| 模块 | 文件 | 一句话职责 |
| --- | --- | --- |
| `channel` | `src/channel.rs` | 公共「类型壳」：定义对外暴露的 `Sender` / `Receiver`，把方法调用按 flavor 分发到具体实现。 |
| `context` | `src/context.rs` | 线程阻塞/唤醒的「线程本地上下文」，记录一次 select 操作的状态。 |
| `counter` | `src/counter.rs` | 引用计数：管理多个 `Sender` / `Receiver` 共享同一通道，并在最后一个引用释放时销毁。 |
| `err` | `src/err.rs` | 全部对外错误类型（`SendError` / `TrySendError` / `RecvError` 等）。 |
| `flavors` | `src/flavors/` | 六种通道「风味」的具体实现（array/list/zero/at/tick/never）。 |
| `select` | `src/select.rs` | `Select` 动态 API 与 select 的核心调度算法（`SelectHandle` trait）。 |
| `select_macro` | `src/select_macro.rs` | `select!` / `select_biased!` 宏的定义与展开。 |
| `utils` | `src/utils.rs` | 内部工具：非毒 `Mutex`、随机数与 shuffle、`sleep_until`。 |
| `waker` | `src/waker.rs` | 阻塞者队列：登记谁在等、谁来唤醒、断开时如何通知所有人。 |
| `alloc_helper` | `src/alloc_helper.rs` | `no_std` 友好的分配器封装（实为指向 `crossbeam-utils` 的符号链接）。 |

> 说明：`alloc_helper.rs` 在仓库里是一个软链接，指向 `../../crossbeam-utils/src/alloc_helper.rs`，复用上游同名实现。

`flavors/mod.rs` 则把六种风味列得很清楚：

[src/flavors/mod.rs:1-17](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/mod.rs#L1-L17)

> 六种 flavor 分别是：`at`（定时投递一次）、`array`（有界环形缓冲）、`list`（无界链表）、`never`（永不投递）、`tick`（周期投递）、`zero`（零容量会合）。这是后续进阶层讲义的主线。

`internal` 隐藏模块：

[src/lib.rs:368-375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L368-L375)

> 这个模块用 `#[doc(hidden)]` 标记，文档里看不到，但它是 `select!` 宏展开后实际调用的「后门」：导出了 `SelectHandle`、`select`、`try_select`、`select_timeout`、`receiver_addr`、`sender_addr`。普通用户不应直接使用它。

#### 4.3.4 代码实践

**实践目标**：把「src 模块地图」落到真实文件上，而不是死记表格。

操作步骤：

1. 在仓库 `crossbeam-channel/` 下，逐个打开 `src/` 下的文件，只看每个文件最顶部的 `//!` 注释（Rust 习惯把模块用途写在文件开头）。
2. 对照上面的职责表，确认每个文件的自我描述与表格一致。
3. 特别地，打开 `src/flavors/mod.rs`，确认它列出的六种 flavor 名称与上表一致。

需要观察的现象：每个 `.rs` 文件顶部 `//!` 注释里的一句话，与本讲给的「一句话职责」高度吻合。

预期结果：你能不看本讲义，凭文件头部注释复述出至少 6 个模块的职责。

> 提示：`channel.rs` 文件较大（约 5 万字节），不必通读，只看顶部注释和它里面 `pub struct Sender` / `pub struct Receiver` 的定义即可。

#### 4.3.5 小练习与答案

**练习 1**：为什么几乎所有 `mod` 声明都带 `#[cfg(feature = "std")]`？

**参考答案**：因为 crate 顶部声明了 `#![no_std]`，而这些模块（通道、阻塞唤醒、select 等）依赖线程、`Mutex`、定时器等只在 `std` 下才有的能力。所以它们被「`std` feature」门控；不开 `std` 时这些模块根本不参与编译（这也正是「禁用 std 尚不支持」的体现）。

**练习 2**：`internal` 模块为什么要 `#[doc(hidden)]`？

**参考答案**：它内部的 `select` / `try_select` / `SelectHandle` 等是给 `select!` 宏展开后调用的「实现细节」，签名和行为可能随版本变化。用 `#[doc(hidden)]` 让它在 docs.rs 上不显眼，避免普通用户直接依赖，但宏仍能通过完整路径访问。

---

### 4.4 公共 API 全景：类型、函数与宏

#### 4.4.1 概念说明

一个 crate 真正「对外承诺」的东西，就是它 `pub use` 出去的类型、函数和宏。`lib.rs` 末尾那一段 `pub use crate::{ ... }` 就是 `crossbeam-channel` 的完整公共 API 清单。读懂它，你就知道「这个库一共能给我什么」。

#### 4.4.2 核心流程

`pub use` 把分散在不同内部模块里的公开项，重新汇聚到 crate 根，分三类来源：

1. 来自 `channel` 模块：核心类型与构造函数。
2. 来自 `err` 模块：全部错误类型。
3. 来自 `select` 模块：动态选择 API。

此外，`select!` 和 `select_biased!` 两个宏通过 `#[macro_export]` 单独导出到 crate 根。

#### 4.4.3 源码精读

最关键的对外导出：

[src/lib.rs:377-387](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L377-L387)

> 这一段把对外 API 一次性列清。可拆成三组理解：

**第一组——来自 `channel` 模块（类型 + 构造函数）：**

- 类型：`Sender`、`Receiver`、`Iter`、`TryIter`、`IntoIter`（后三个是接收端的迭代器）。
- 构造函数：`unbounded()`、`bounded(cap)`、`after(dur)`、`at(instant)`、`tick(dur)`、`never()`。

**第二组——来自 `err` 模块（错误类型）：**

- `SendError`、`TrySendError`、`SendTimeoutError`
- `RecvError`、`TryRecvError`、`RecvTimeoutError`
- `ReadyTimeoutError`、`TryReadyError`
- `SelectTimeoutError`、`TrySelectError`

**第三组——来自 `select` 模块：**

- `Select`（动态选择器）、`SelectedOperation`（选中后必须完成的对象）。

宏的导出在另一个文件里，通过 `#[macro_export]` 完成：

[src/select_macro.rs:1135-1157](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L1135-L1157)

> 这里定义并导出了用户最常用的两个宏 `select!` 与 `select_biased!`。它们都会展开成对 `internal` 模块的调用（详见进阶层 `u2-l9` 与专家层 `u3-l3`）。

> 提示：`#[macro_export]` 会把宏放到 crate 根路径，所以用户写 `use crossbeam_channel::{select, select_biased};` 就能直接用。

#### 4.4.4 代码实践

**实践目标**：自己动手列出 crate 的完整对外 API，而不是只看本讲的归纳。

操作步骤：

1. 打开 `src/lib.rs`，定位到最后一个 `pub use crate::{ ... }` 块。
2. 在一张纸上分三列写下：来自 `channel`、来自 `err`、来自 `select` 的所有名字。
3. 再打开 `src/select_macro.rs`，搜索 `macro_export`，把用户可见的宏（排除 `crossbeam_channel_internal!` 这个内部宏）补进列表。
4. 把第 3 步的列表与 docs.rs 上的 `crossbeam-channel` 文档首页对照。

需要观察的现象：你自己整理出的列表，与 docs.rs 首页「Re-exports」「Macros」两节一致。

预期结果：得到一张含 2 个核心类型、6 个构造函数、10 个错误类型、2 个 select 类型、2 个对外宏的完整清单（具体数量以你实际数到的为准）。

#### 4.4.5 小练习与答案

**练习 1**：用户想「等两个通道谁先来消息」，最直接该用哪个宏？它对应的动态 API 类型又是什么？

**参考答案**：用 `select!` 宏最直接；它对应的动态 API 类型是 `Select`（配合 `SelectedOperation`）。`select!` 本质上就是 `Select` 的便捷封装。

**练习 2**：`Iter` / `TryIter` / `IntoIter` 三个迭代器类型分别由什么产生？

**参考答案**：`Iter` 由 `Receiver::iter()` 产生（会阻塞地一条条收，直到断开）；`TryIter` 由 `Receiver::try_iter()` 产生（非阻塞地把当前已有的全收走）；`IntoIter` 由「消费 `Receiver`」（`for msg in r` 或 `r.into_iter()`）产生。

---

### 4.5 构建与测试运行方式

#### 4.5.1 概念说明

「能跑起来」是学习一个项目的第一步。`crossbeam-channel` 是一个普通 Rust crate，所以它的构建与测试完全走 Cargo 标准流程。此外它还自带了两套基准：仓库根的 `benches/`（criterion 微基准）和 `benchmarks/`（与 Go/flume/std 等多实现对比的程序级基准）。

#### 4.5.2 核心流程

日常学习常用的命令如下（均为「示例命令」，非项目脚本里写死的步骤）：

1. `cargo build`：编译库。
2. `cargo test`：跑 `tests/` 目录与代码里的 `#[test]` / 文档测试。
3. `cargo bench`：跑 `benches/` 下的 criterion 基准（需要 nightly 才能用 `#![feature(test)]` 时则参考各 bench 文件头部说明）。
4. `cd benchmarks && ./run.sh`：跑程序级对比基准（依赖 Rust + Go + Bash + Python）。

#### 4.5.3 源码精读

criterion 基准文件位置在 `benches/`：

[benches/crossbeam.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benches/crossbeam.rs)

> 这是 crate 自带的微基准入口，用 criterion 框架测量各种通道操作的单线程/多线程耗时。

程序级对比基准的运行脚本：

[benchmarks/run.sh:1-12](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/run.sh#L1-L12)

> 脚本依次 `cargo run --release --bin <实现>` 跑 crossbeam-channel、futures-channel、mpsc、flume，再用 `go run go.go` 跑 Go 版本，最后用 `plot.py` 画图对比。

六种基准场景的定义在 benchmarks 的 README：

[benchmarks/README.md:3-9](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/README.md#L3-L9)

> 六个场景：`seq`（单线程自发自收）、`spsc`（一发一收）、`mpsc`（多发一收）、`mpmc`（多发多收）、`select_rx`（select 多路接收）、`select_both`（select 多路收发）。默认 `N = 5_000_000`、`T = 4`。

#### 4.5.4 代码实践

**实践目标**：把项目真正跑起来，并解读测试输出。

操作步骤：

1. 进入 `crossbeam-channel/` 目录，运行 `cargo build`。
2. 运行 `cargo test`，等待全部用例跑完。
3. （可选）进入 `benchmarks/` 目录，确认本机已装 Rust/Go/Bash/Python 后执行 `./run.sh`；若缺少 Go，至少 `cargo run --release --bin crossbeam-channel` 能跑出一份 `crossbeam-channel.txt`。

需要观察的现象：

- `cargo test` 会编译并运行大量用例（`tests/` 下有 `array.rs`、`list.rs`、`zero.rs`、`golang.rs`、`mpsc.rs`、`select.rs` 等十多个文件），最后给出 `test result: ok. ...`。
- 部分并发用例可能因机器负载偶发较慢，但应全部通过。

预期结果：`cargo build` 成功；`cargo test` 全绿。如果因网络/工具链拉不到依赖或机器环境受限无法运行，请明确标注「待本地验证」并记录失败原因。

> 提示：不要假装已经跑过。如果你确实跑通了，可以把 `cargo test` 最后那行 `test result:` 贴进自己的学习笔记，作为「环境可用」的证据。

#### 4.5.5 小练习与答案

**练习 1**：`benches/` 和 `benchmarks/` 两套基准的定位有何不同？

**参考答案**：`benches/` 是 criterion 微基准，关注单 crate 内各种操作的精细耗时；`benchmarks/` 是程序级对比基准，把 `crossbeam-channel` 和 Go、flume、futures-channel、std mpsc 等不同实现放在同一组场景下横向比较，并生成对比图。

**练习 2**：`seq` 场景和 `spsc` 场景的区别是什么？

**参考答案**：`seq` 是同一个线程先发 N 条再收 N 条（纯顺序，无跨线程）；`spsc` 是一个线程发 N 条、另一个线程收 N 条（单生产者单消费者，跨线程）。后者才是衡量通道并发开销的场景。

## 5. 综合实践

把本讲的「地图」和「运行」串起来，完成下面这个小任务：

1. **跑通**：在 `crossbeam-channel/` 下执行 `cargo build` 与 `cargo test`，确认环境可用。
2. **画地图**：用一句话写出 `src/` 下每个子模块（`channel` / `context` / `counter` / `err` / `flavors` / `select` / `select_macro` / `utils` / `waker` / `alloc_helper`）的职责，并标注「它的职责声明在文件第几行的 `//!` 注释」。
3. **列 API**：从 `src/lib.rs` 最后的 `pub use` 与 `src/select_macro.rs` 的 `#[macro_export]` 中，整理出 crate 对外暴露的全部「类型 / 函数 / 宏」清单，按「来自 channel / 来自 err / 来自 select / 宏」四类分组。
4. **自检**：把你整理的清单与 `cargo doc --open` 生成的文档首页对照，看看有没有遗漏或多余。

完成上述四步后，你就拥有了一份属于自己的 `crossbeam-channel` 「项目地图」，后续每一篇讲义都可以在这张地图上找到对应位置。

## 6. 本讲小结

- `crossbeam-channel` 是一个 mpmc 通道库，定位为 `std::sync::mpsc` 的功能更强、性能更好的替代品，当前版本 `0.5.15`，MSRV 为 Rust `1.74`。
- crate 顶部声明 `#![no_std]`，但通过 `std` feature 门控几乎所有模块，「禁用 std 暂不支持」。
- `src/` 由 `channel / context / counter / err / flavors / select / select_macro / utils / waker / alloc_helper` 等模块组成，其中 `flavors/` 包含 array/list/zero/at/tick/never 六种通道实现。
- 对外 API 通过 `src/lib.rs` 末尾的 `pub use` 集中导出：核心类型（`Sender`/`Receiver` 等）、6 个构造函数、10 个错误类型、`Select`/`SelectedOperation`，以及通过 `#[macro_export]` 导出的 `select!` / `select_biased!` 宏。
- 还有一个 `#[doc(hidden)]` 的 `internal` 模块，是 `select!` 宏展开后调用的「后门」，普通用户不应直接使用。
- 构建与测试走 Cargo 标准流程（`cargo build` / `cargo test`），基准分 `benches/`（微基准）与 `benchmarks/`（多实现对比）两套。

## 7. 下一步学习建议

本讲让你「认识项目」。下一讲 `u1-l2 第一个通道：unbounded 与 bounded` 将带你亲手写出第一段通道代码，理解 `send` / `recv` 的阻塞行为与零容量会合语义。

建议你同时：

- 通读 `src/lib.rs` 顶部那段 `//!` 文档注释（它本身就是一份极好的入门教程）。
- 翻一翻 `CHANGELOG.md`，看看 0.5.x 系列都修过哪些 bug（比如 0.5.15 修复了无界通道的双重释放），这会帮你理解为什么后续讲义要花那么多篇幅讲 `list` flavor 的内存安全。
- 准备好一个能跑 `cargo test` 的本地环境，因为后续几乎每篇讲义都带有「动手实践」。
