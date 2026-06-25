# 构建、Feature Gate 与示例运行

## 1. 本讲目标

本讲承接 [u1-l1 项目概览](./u1-l1-project-overview.md)，把视角从「interprocess 是什么」推进到「怎么把它编译出来、怎么按需启用功能、怎么把仓库自带的示例跑起来」。

学完后你应当能够：

- 用 `cargo` 把 interprocess 编译成库，并理解 `Cargo.toml` 里包元数据（edition、MSRV、`autoexamples`/`autotests`）的含义。
- 准确说出 `async`、`tokio`、`doc_cfg` 三个 feature 各自的作用，以及 `tokio` 为什么会自动连带启用 `async`。
- 看懂 `Cargo.toml` 中 `cfg(windows)` / `cfg(unix)` 这类 target 特定依赖，以及 `[dev-dependencies]` 为示例和测试额外提供了什么。
- 独立运行 `examples/` 目录里的同步与异步 local socket 示例对，并解释示例代码里「先收后发」的顺序为什么能避免死锁。
- 区分写在 `Cargo.toml` 的 `[lints]`（全 crate 生效）和写在 `src/lib.rs` 的 `#![warn]`（仅库本体生效）两套 lint 配置。

## 2. 前置知识

在开始前，你需要大致了解以下概念。如果某一项完全陌生，建议先补一下再来。

- **Cargo 与 `Cargo.toml`**：Rust 官方的包管理器。`Cargo.toml` 描述一个 crate 的元数据、依赖和功能开关；`cargo build` / `cargo run` 是最基本的命令。
- **feature（功能特性）**：Cargo 里用 `[features]` 段声明的可选编译开关。下游可以通过 `--features xxx` 启用，从而条件编译出不同的代码。Rust 源码里对应的 `#[cfg(feature = "xxx")]` 就是根据它来决定某段代码是否参与编译。
- **edition（版本）**：Rust 的语言版本，例如 `2015` / `2018` / `2021`。它控制一些语法和默认行为，与编译器版本是两回事。
- **MSRV（Minimum Supported Rust Version，最低支持的 Rust 版本）**：项目保证能编译通过的最老的 Rust 编译器版本。
- **target 特定依赖**：可以用 `[target.'cfg(条件)'.dependencies]` 让某个依赖只在特定平台（如 Windows 或 Unix）才被引入。
- **local socket**：interprocess 自造的跨平台本地通信抽象（Windows 上由 named pipe 实现、Unix 上由 Unix domain socket 实现）。这是 [u1-l1](./u1-l1-project-overview.md) 已建立的核心认知，本讲只用到它的同步/异步示例，不再重复其原理。

> 提示：本讲不要求你已经理解 local socket 的内部实现，只需要能照着示例把程序跑起来。源码层面的深入剖析留到第二、三单元。

## 3. 本讲源码地图

本讲围绕「构建配置」和「可运行示例」两部分展开，涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `Cargo.toml` | 整个 crate 的总配置：包元数据、feature、依赖、lint、示例与二进制的显式声明。本讲最重要的单一文件。 |
| `examples/local_socket/sync/listener.rs` | 同步 local socket **服务端**示例（回显一行后回复）。 |
| `examples/local_socket/sync/stream.rs` | 同步 local socket **客户端**示例（先发一行再收回复）。 |
| `examples/local_socket/tokio/listener.rs` | 异步（Tokio）服务端示例，演示示例代码内部如何用 `#[cfg(feature = "tokio")]` 做门控。 |
| `examples/local_socket/tokio/stream.rs` | 异步客户端示例，同样带 feature 门控。 |
| `src/lib.rs` | crate 根，本讲只关注其中 `doc_cfg` 与 lint 的两处配置，以对照 `Cargo.toml`。 |

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：包元数据与 lint 配置 → feature 与依赖链 → target 特定依赖与 dev-dependencies → 同步示例的声明与运行 → 异步示例与 tokio 门控。

### 4.1 包元数据、构建与 lint 配置

#### 4.1.1 概念说明

`Cargo.toml` 最前面的 `[package]` 段告诉 Cargo「这是个什么包」。对学习者来说，最值得关注的几个字段是：

- `edition = "2021"`：使用 Rust 2021 语言版本。
- `rust-version = "1.75"`：MSRV 是 1.75。也就是说，只要你的 Rust 编译器不低于 1.75，就应当能编译。
- `autotests = false` 和 `autoexamples = false`：**关闭自动发现**。默认情况下，Cargo 会自动把 `tests/` 和 `examples/` 目录里的每个文件当作测试/示例来编译。interprocess 把这两个开关都关掉，改为在 `Cargo.toml` 里**逐个显式声明**（见 `[[example]]` 段）。这样做的好处是可以精确命名示例、给示例设置不同的 `path`，也避免目录里那些「辅助文件」（比如示例共用的 `side_a.rs` / `side_b.rs`）被误当成独立示例编译。

此外，interprocess 还附带一个独立的二进制 `inspect-platform`，它通过 `[[bin]]` 段声明（这个二进制的作用留到 [u9-l4 平台探测](./u9-l4-platform-check-extension.md) 讲，这里只需知道它存在）。

#### 4.1.2 核心流程

构建 interprocess 这个库本身（不是示例）的流程是：

1. Cargo 读取 `Cargo.toml` 的 `[package]` 与 `[dependencies]`，确定要拉取哪些依赖。
2. Cargo 根据**当前编译目标**（Windows 还是 Unix）挑选 `cfg` 匹配的 target 特定依赖。
3. 根据**启用的 feature**（默认一个都不开）决定哪些可选依赖和 `#[cfg(feature)]` 代码参与编译。
4. 编译 `src/lib.rs` 起的库本体，以及（如果命令是 `cargo run --example` / `cargo test`）编译显式声明的示例与测试。

一个关键细节：interprocess 的 lint 分成两套，作用范围不同——这点很容易被初学者忽略。

- 写在 `Cargo.toml` 的 `[lints.rust]` / `[lints.clippy]` 是**全 crate 生效**的，库、示例、测试都受约束。
- 写在 `src/lib.rs` 顶部的 `#![warn(...)]` 只对**库本体**生效，不影响示例和测试。

#### 4.1.3 源码精读

包元数据与关闭自动发现的配置在 [Cargo.toml:1-16](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L1-L16)，其中 `autotests = false` / `autoexamples = false` 是后面「示例必须显式声明」的根源。

附带二进制 `inspect-platform` 的声明在 [Cargo.toml:109-112](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L109-L112)。

全 crate 生效的 lint 在 [Cargo.toml:81-98](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L81-L98)，其中最值得关注的是 `unsafe_op_in_unsafe_fn = "forbid"`（在 unsafe 函数里仍必须显式写 `unsafe` 块，违者直接拒绝编译），以及一组关于类型转换可移植性的 clippy 规则。

只对库本体生效的 `#![warn(...)]` 在 [src/lib.rs:3-10](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L3-L10)，第 3 行的注释明确解释了作者**特意**把这套更严格的 lint 放在 `lib.rs` 而不是 `Cargo.toml` 的原因——「如果放进 Cargo.toml，它就会连示例一起覆盖」。

```toml
# Cargo.toml 片段（节选）
[package]
name         = "interprocess"
version      = "2.4.2"
edition      = "2021"
rust-version = "1.75"
autotests = false
autoexamples = false
```

#### 4.1.4 代码实践

**实践目标**：确认你的工具链满足 MSRV，并能干净地编译出库本体。

**操作步骤**：

1. 查看本机 Rust 版本：`rustc --version`。
2. 进入仓库根目录，编译库（不启用任何 feature）：`cargo build`。

**需要观察的现象**：

- `rustc --version` 输出的版本号应当 ≥ `1.75.0`。
- `cargo build` 应当成功，并在 `target/debug/` 下产生 `libinterprocess.rlib` 之类产物；因为默认 feature 为空、没有启用 tokio，所以编译过程中**不会**拉取 tokio。

**预期结果**：编译通过，且依赖列表里不含 tokio（这一点会在 4.2、4.3 进一步验证）。如果本机 Rust 版本低于 1.75，则会因 MSRV 不满足而报错——请先用 rustup 升级。

#### 4.1.5 小练习与答案

**练习 1**：为什么 interprocess 要把 `autoexamples` 设为 `false`？

> **参考答案**：为了让示例**逐个显式声明**在 `[[example]]` 段里，从而精确控制每个示例的名字和源码路径，也避免 `examples/` 下那些被其他示例 `include!` 进去的辅助文件（如 `side_a.rs` / `side_b.rs`）被 Cargo 当成独立示例重复编译。

**练习 2**：`[lints.clippy]` 里 `unsafe_op_in_unsafe_fn = "forbid"`（写在 `Cargo.toml`）和 `src/lib.rs` 里的 `#![warn(...)]`，作用范围有什么区别？

> **参考答案**：前者是全 crate 生效（库、示例、测试都受限）；后者只对库本体（`src/`）生效，不影响 `examples/` 和 `tests/`。注释 [src/lib.rs:3](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L3) 正是为此而写。

### 4.2 Feature Gate 与依赖链

#### 4.2.1 概念说明

interprocess 一共声明了三个 feature，且**默认全部关闭**（`default = []`）：

| feature | 作用 | 是否默认 |
| --- | --- | --- |
| `async` | 引入 `futures-core`，提供异步 trait 所需的最基础依赖。 | 关 |
| `tokio` | 启用各原语的 Tokio 异步变体；**会自动连带启用 `async`**。 | 关 |
| `doc_cfg` | 启用 nightly 的 `doc_cfg`，让 `cargo doc` 在平台专属 API 旁标注 `cfg(...)` 徽章。 | 关 |

最关键的关系是：`tokio` **蕴含** `async`。也就是说，启用 `tokio` 时不需要再单独加 `async`，Cargo 会自动把它一起打开。这是因为 `tokio` 的声明里把 `async` 列为它依赖的 feature。

`doc_cfg` 比较特殊：它启用的是 nightly 编译器的 `feature(doc_cfg)`，主要给 docs.rs 构建文档时用，普通用户构建通常不需要碰。

#### 4.2.2 核心流程

feature 之间的蕴含关系可以表示成一条有向链（`⇒` 读作「连带启用」）：

\[
\text{tokio} \;\Rightarrow\; \text{async} \;\Rightarrow\; \text{futures-core}
\]

也就是说：

1. 你执行 `cargo build --features tokio`。
2. Cargo 发现 `tokio` 依赖 `async`，于是也启用 `async`。
3. `async` 又把可选依赖 `futures-core` 拉进来。
4. 与此同时，`tokio` 这个可选依赖本身也被启用，于是 `#[cfg(feature = "tokio")]` 的所有异步类型（如 `local_socket::tokio::Stream`）参与编译。

反过来，如果你只 `cargo build`（不带 feature），上面整条链都不会触发，编译出来的库**完全不含** tokio 与 futures-core。这正是 interprocess 把这些大依赖放在默认关闭的 feature 后面的目的：让不需要异步的用户零负担。

#### 4.2.3 源码精读

feature 声明与蕴含关系在 [Cargo.toml:25-29](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L25-L29)，关键一行是 `tokio = ["dep:tokio", "async"]`——它说明启用 `tokio` 会同时启用名为 `tokio` 的可选依赖（`dep:tokio` 语法）以及 `async` feature。

对应的可选依赖在 [Cargo.toml:31-41](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L31-L41)：`tokio` 与 `futures-core` 都标了 `optional = true`，未启用 feature 时它们不会被编译。

`doc_cfg` 的作用点在 [src/lib.rs:2](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L2)，它用 `cfg_attr` 仅在启用 `doc_cfg` 时才打开 nightly 的 `feature(doc_cfg)`；随后在 [src/lib.rs:33-40](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L33-L40) 给 `os::unix` / `os::windows` 模块挂上 `doc(cfg(...))` 徽章，这样文档读者一眼就能看出某模块只在特定平台存在。

```toml
# Cargo.toml 的 [features] 段
[features]
default = []
async = ["futures-core"]
tokio = ["dep:tokio", "async"]
doc_cfg = []
```

#### 4.2.4 代码实践

**实践目标**：亲手验证「启用 `tokio` 会连带启用 `async`，并拉入 tokio / futures-core」。

**操作步骤**：

1. 先不带 feature 看依赖：`cargo tree`（或 `cargo tree -e features`）。
2. 再带 feature 看：`cargo tree --features tokio`。

**需要观察的现象**：

- 第 1 步的依赖树里**没有** `tokio` 和 `futures-core`。
- 第 2 步的依赖树里**出现**了 `tokio` 和 `futures-core`。

**预期结果**：两次输出对比能直观看到 feature 如何改变依赖图。若想进一步确认 `async` 被连带启用，可执行 `cargo tree -e features --features tokio`，在 features 视图里能看到 `tokio` feature 指向 `async`。运行结果以你本机输出为准。

#### 4.2.5 小练习与答案

**练习 1**：如果你只想用同步 API，需要启用 `tokio` feature 吗？

> **参考答案**：不需要。同步 API（`local_socket::Stream`、`ListenerOptions::create_sync` 等）不依赖 tokio；只有用到 `local_socket::tokio` 等异步变体时才需要 `--features tokio`。

**练习 2**：为什么 `tokio = ["dep:tokio", "async"]` 里要写 `dep:tokio` 而不是直接写 `tokio`？

> **参考答案**：在 Cargo 里，当可选依赖与 feature 同名时（这里依赖叫 `tokio`、feature 也叫 `tokio`），需要用 `dep:` 前缀来明确「我指的是那个**依赖**」，避免歧义。加上 `async` 表示这个 feature 同时连带启用 `async` feature。

### 4.3 target 特定依赖与 dev-dependencies

#### 4.3.1 概念说明

不同平台需要的系统调用封装库不一样。interprocess 用 `cfg` 把平台依赖隔离开：

- **Windows（`cfg(windows)`）** 需要 `windows-sys`（Windows 系统 API 绑定）、`recvmsg`、`widestring`，以及一份平台专属的 `tokio` 配置。
- **Unix（`cfg(unix)`）** 需要 `libc`（C 库绑定，用于 `mkfifo`、Unix domain socket 等）。

注意一个容易被忽略的点：`tokio` 在 `Cargo.toml` 里被声明了**两次**——一次在普通 `[dependencies]`（line 33-40），一次在 `[target.'cfg(windows)'.dependencies]`（line 56-62），两份的 feature 集合**不同**。Cargo 会按「平台特异的覆盖更通用」的方式合并：在 Windows 上取并集，在 Unix 上只用通用的那份。这正是 named pipe 在 Windows 下的异步实现需要额外 tokio feature（如 `fs`）而 Unix 不需要的体现。

此外，`[dev-dependencies]` 只在编译示例、测试和 benchmark 时生效，不会被下游用户拉入。interprocess 的 dev-dependencies 里又声明了一份 `tokio`，这次带上了 `rt-multi-thread`——这是 `#[tokio::main]` 默认使用多线程运行时所必需的 feature，仅供示例/测试使用。

#### 4.3.2 核心流程

依赖的选取流程：

1. Cargo 先取通用 `[dependencies]`。
2. 再叠加当前平台匹配的 `[target.'cfg(...)'.dependencies]`（同名依赖合并 feature）。
3. 若正在编译示例/测试，再叠加 `[dev-dependencies]`（同名依赖再次合并 feature）。

举例：在 Windows 上用 `--features tokio` 编译一个 tokio 示例时，tokio 最终的 feature 集合 ≈ 通用声明 ∪ Windows 声明 ∪ dev-dependencies 声明。所以示例能拿到 `rt-multi-thread`，而普通库用户不会。

#### 4.3.3 源码精读

Windows 平台依赖在 [Cargo.toml:43-64](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L43-L64)，可以看到 `windows-sys` 启用了一长串 `Win32_*` feature（文件系统、安全、管道、线程等），这正是 named pipe 与安全描述符实现所需的底层 API。

Unix 平台依赖在 [Cargo.toml:66-67](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L66-L67)，只有 `libc`。

dev-dependencies 在 [Cargo.toml:69-79](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L69-L79)，其中的 `tokio` 带有 `rt-multi-thread`，还有 `color-eyre`（用于测试/示例里更友好的错误与 panic 报告；注意 local_socket 示例目前用的是普通的 `std::io::Result`，并未直接调用 color-eyre）。

```toml
# Unix 平台只依赖 libc
[target.'cfg(unix)'.dependencies]
libc = { version = "0.2.137", features = ["extra_traits"] }
```

#### 4.3.4 代码实践

**实践目标**：看清「你当前平台」实际拉入了哪些平台依赖。

**操作步骤**：

1. 在 Linux/macOS 上执行 `cargo tree --features tokio`；在 Windows 上执行同样命令。
2. 若有条件，对比两个平台的输出。

**需要观察的现象**：

- Unix 平台：依赖树里出现 `libc`，**没有** `windows-sys` / `recvmsg` / `widestring`。
- Windows 平台：依赖树里出现 `windows-sys`、`recvmsg`、`widestring`，**没有** `libc`。
- 两边都出现 `tokio`（因为带了 `--features tokio`），但 feature 子集可能不同。

**预期结果**：直观验证 `cfg` target 依赖的互斥选取。若你只有一个平台，观察单一平台输出即可，另一边的结论标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `[dev-dependencies]` 里的 `tokio` 要带 `rt-multi-thread`，而 `[dependencies]` 里的不带？

> **参考答案**：示例使用 `#[tokio::main]`，它默认创建多线程运行时，需要 `rt-multi-thread` feature。但这是示例/测试的需求，不是库本身的需求；把它放进 dev-dependencies，就不会让下游库用户被迫启用多线程运行时，保持库的依赖最小化。

**练习 2**：`color-eyre` 会成为 interprocess 下游用户的依赖吗？

> **参考答案**：不会。`[dev-dependencies]` 只在编译本 crate 的示例/测试/benchmark 时生效，不会传播给把 interprocess 当依赖的下游项目。

### 4.4 同步示例的显式声明与运行

#### 4.4.1 概念说明

因为 `autoexamples = false`，仓库里每个可运行示例都必须在 `Cargo.toml` 里用 `[[example]]` 显式声明，给它一个**示例名**和一个**源码路径**。运行时用的就是示例名，而不是文件名。例如 `examples/local_socket/sync/listener.rs` 对应的示例名是 `local_socket_sync_server`，所以运行命令是 `cargo run --example local_socket_sync_server`。

local socket 的同步示例是一对回显程序：

- **服务端** `local_socket_sync_server`：监听一个名称，接受连接，**先读一行再回写一行**。
- **客户端** `local_socket_sync_client`：连接服务端，**先写一行再读回复**。

这里有一个贯穿后续所有流式 IPC 的关键设计：**半双工的收发顺序**。同步 local socket 在单线程里不能同时收和发，如果客户端和服务端都「先发后收」，两边都会卡在写缓冲等对方读走，形成死锁。所以示例刻意安排成「客户端先发、服务端先收」，让数据有地方可去。

#### 4.4.2 核心流程

同步回显的完整时序：

1. 服务端用 `ListenerOptions::new().name(name).create_sync()` 创建监听器，进入 `incoming()` 主循环等待连接。
2. 客户端用 `Stream::connect(name)` 连接（若服务端未启动会**立即失败**，见代码注释）。
3. 连接建立后：
   - 客户端 `write_all(b"Hello from client!\n")` 发出一行。
   - 服务端 `read_line` 读到这一行。
   - 服务端 `get_mut().write_all(b"Hello from server!\n")` 回写一行。
   - 客户端 `read_line` 读到回复。
4. 双方各自打印收到的内容，连接 `drop` 释放。

#### 4.4.3 源码精读

所有示例的显式声明集中在 [Cargo.toml:114-149](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L114-L149)。其中 local socket 同步对的声明是：

```toml
[[example]]
name = "local_socket_sync_server"
path = "examples/local_socket/sync/listener.rs"
[[example]]
name = "local_socket_sync_client"
path = "examples/local_socket/sync/stream.rs"
```

服务端核心在 [examples/local_socket/sync/listener.rs:12](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs#L12) 用构建器创建监听器；[examples/local_socket/sync/listener.rs:41-45](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs#L41-L45) 用 `incoming()` 产生连接流并包成 `BufReader`；[examples/local_socket/sync/listener.rs:51](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs#L51) 与 [examples/local_socket/sync/listener.rs:56](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs#L56) 体现「先收后发」，并用 `get_mut()` 绕过 `BufReader` 拿到底层流来写（因为 `BufReader` 不透传 `Write`）。死锁规避的理由写在 [examples/local_socket/sync/listener.rs:46-50](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs#L46-L50) 的注释里。

客户端核心在 [examples/local_socket/sync/stream.rs:18](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/stream.rs#L18) 连接，[examples/local_socket/sync/stream.rs:21](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/stream.rs#L21) 先发、[examples/local_socket/sync/stream.rs:27](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/stream.rs#L27) 后收。客户端在 [examples/local_socket/sync/stream.rs:9-13](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/stream.rs#L9-L13) 用 `GenericNamespaced::is_supported()` 选择用「命名空间名」还是「文件路径名」——名称系统的细节留到 [u2-l4 名称系统](./u2-l4-name-system.md)，这里只需知道它构造出一个跨平台的 `Name`。

> 关于 `to_ns_name` / `to_fs_name` / `GenericNamespaced` / `GenericFilePath`：它们属于 local socket 的「名称系统」，本讲只把它当作「构造一个可移植的通信名称」来用，不展开。

#### 4.4.4 代码实践

**实践目标**：在两个终端跑通同步回显示例对，亲眼看到一次跨进程通信。这是本讲的**核心实践**。

**操作步骤**：

1. 终端 A（先启动服务端）：
   ```bash
   cargo run --example local_socket_sync_server
   ```
2. 等终端 A 打印出 `Server running at example.sock` 后，终端 B（再启动客户端）：
   ```bash
   cargo run --example local_socket_sync_client
   ```
3. 若想反过来验证「服务端未启动时客户端立即失败」，可以先单独跑客户端，观察它直接报错退出。

**需要观察的现象**：

- 服务端先打印 `Server running at example.sock`，随后阻塞等待连接。
- 客户端运行后，服务端打印 `Client answered: Hello from client!`，客户端打印 `Server answered: Hello from server!`。
- 先单独跑客户端时，客户端应因连接失败而立即报错（而不是挂起等待）。

**预期结果**：服务端与客户端各打印出对方发来的一行，完成一次回显。若地址被占用（上一次服务端异常退出留下的「僵尸 socket」），服务端会报 `AddrInUse`——这正是示例里 [examples/local_socket/sync/listener.rs:13-33](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs#L13-L33) 处理的情况，可删除残留的 `example.sock` 文件后重试。

> 注：示例的 `main` 体内有些 `//{` 和 `}//` 形式的注释标记，它们是用于文档测试（doctest）截取代码片段的标记，不影响运行逻辑，阅读时可忽略。

#### 4.4.5 小练习与答案

**练习 1**：如果把服务端改成「先写后读」、客户端也「先写后读」，会发生什么？

> **参考答案**：很可能死锁。同步流在单线程里收发不能同时进行，两边都先 `write_all` 会因为对端没在读、写缓冲塞满而互相阻塞。示例刻意做成「一端先发、另一端先收」正是为了避免这一点。

**练习 2**：为什么服务端写回时要用 `conn.get_mut().write_all(...)` 而不是直接 `conn.write_all(...)`？

> **参考答案**：`conn` 是 `BufReader<Stream>`，它实现了 `Read` 但**不透传** `Write`。要写数据必须先用 `get_mut()` 拿到被包裹的底层 `Stream`，再对它调用 `write_all`。

### 4.5 异步示例与 tokio feature 门控

#### 4.5.1 概念说明

local socket 的 Tokio 异步示例与服务端/客户端结构对应，但有两个新特点：

1. 它们依赖 `local_socket::tokio` 模块，因此**必须启用 `tokio` feature** 才能编译出真正的逻辑。
2. 为了让示例在「没启用 tokio」时也能编译（只是打印一句提示），示例代码内部用了一个**双 `main` 门控模式**：用 `#[cfg(not(feature = "tokio"))]` 提供一个「占位 main」打印错误，再用 `#[cfg(feature = "tokio")]` + `#[tokio::main]` 提供真正的异步 `main`。

异步版还顺便展示了流式 IPC 在异步下的一个优势：用 `try_join!` 让「读」和「写」**真正并发**执行，从而不必像同步版那样精心安排收发顺序——这正好呼应了 4.4 讲的死锁问题。

#### 4.5.2 核心流程

异步门控的编译期分支：

1. 若 `--features tokio` 未启用：编译占位 `main`，运行时打印 `This example is not available when the Tokio feature is disabled.`。
2. 若启用了 `tokio`：编译 `#[tokio::main] async fn main`，其中用 `create_tokio()` 创建异步监听器、`Stream::connect(name).await` 异步连接、`try_join!(send, recv)` 并发收发。

#### 4.5.3 源码精读

门控模式在异步服务端 [examples/local_socket/tokio/listener.rs:2-8](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/tokio/listener.rs#L2-L8)：第 2-5 行是「未启用 tokio」的占位 main，第 6-8 行是「启用 tokio」的异步 main。真正创建异步监听器在 [examples/local_socket/tokio/listener.rs:44](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/tokio/listener.rs#L44) 的 `create_tokio()`。

异步客户端的门控在 [examples/local_socket/tokio/stream.rs:2-6](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/tokio/stream.rs#L2-L6)，连接在 [examples/local_socket/tokio/stream.rs:29](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/tokio/stream.rs#L29) 的 `Stream::connect(name).await`，并发收发在 [examples/local_socket/tokio/stream.rs:40-43](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/tokio/stream.rs#L40-L43) 的 `try_join!(send, recv)`。

```rust
// 异步示例里的「双 main」门控（节选自 listener.rs）
#[cfg(not(feature = "tokio"))]
fn main() {
    eprintln!("This example is not available when the Tokio feature is disabled.");
}
#[cfg(feature = "tokio")]
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> { /* ... */ }
```

#### 4.5.4 代码实践

**实践目标**：用 `--features tokio` 跑通异步示例对，并验证「忘记加 feature」时的行为。

**操作步骤**：

1. 终端 A：`cargo run --features tokio --example local_socket_tokio_server`
2. 终端 B：`cargo run --features tokio --example local_socket_tokio_client`
3. （对照）故意不加 feature 跑一次：`cargo run --example local_socket_tokio_server`，观察占位 main 的输出。

**需要观察的现象**：

- 第 1、2 步：与服务端/客户端异步通信，输出与同步版类似的回显内容。
- 第 3 步：程序不报编译错误，但运行时打印 `This example is not available when the Tokio feature is disabled.` 然后正常退出。

**预期结果**：带 `--features tokio` 时完成异步回显；不带时走占位分支并提示。这正是「示例代码内部 feature 门控」的效果——它保证示例在两种 feature 配置下都能编译，只是行为不同。

#### 4.5.5 小练习与答案

**练习 1**：为什么异步客户端可以用 `try_join!(send, recv)` 同时收发，而同步客户端必须先发后收？

> **参考答案**：异步运行时（Tokio）能在 `await` 点之间切换任务，让「读」和「写」两个 future 并发推进；而同步代码单线程下收发不能同时进行，必须靠顺序安排避免互相等死。

**练习 2**：如果不启用 `tokio` feature，`local_socket::tokio::Stream` 这个类型还存在吗？

> **参考答案**：不存在。整个 `local_socket::tokio` 模块都由 `#[cfg(feature = "tokio")]` 门控，未启用时根本不参与编译。示例里的「双 main」门控就是为了在这样的配置下也能给出一个可编译、可运行的占位程序。

## 5. 综合实践

把本讲的三条主线（构建、feature、示例）串起来做一次综合验证：

1. **构建对比**：分别执行 `cargo build` 与 `cargo build --features tokio`，用 `cargo tree` 记录两次的依赖差异，确认同步构建不含 tokio、异步构建才拉入 tokio 与 futures-core。
2. **平台依赖观察**：在你当前平台执行 `cargo tree --features tokio`，记录它拉入的是 `libc`（Unix）还是 `windows-sys` 系列（Windows），并据此判断你机器属于哪一类后端。
3. **同步回显跑通**：按 4.4.4 的步骤在两个终端跑通 `local_socket_sync_server` / `local_socket_sync_client`，截下双方打印。
4. **异步对照**：加 `--features tokio` 跑通 `local_socket_tokio_server` / `local_socket_tokio_client`，再故意「忘记」加 feature 跑一次，验证占位 main 的提示。
5. **小结**：用一段话说明「为什么同步示例不需要任何 feature、而异步示例必须 `--features tokio`」，并结合 4.1 的 lint 配置说明为什么这套严格 lint 不会影响你正在运行的示例。

完成后，你应当能向别人解释：interprocess 默认编译出的是一个**不含异步运行时**的精简库，只有在显式启用 `tokio` 后才会「长出」异步能力与相应依赖。

## 6. 本讲小结

- interprocess 的 `Cargo.toml` 把 `autoexamples` / `autotests` 关掉，改为用 `[[example]]` 显式声明每个示例，运行时用**示例名**（如 `local_socket_sync_server`）而非文件名。
- 三个 feature 默认全关；`tokio` 蕴含 `async`（`tokio ⇒ async ⇒ futures-core`），`doc_cfg` 仅影响文档构建。
- 依赖按 `cfg(windows)` / `cfg(unix)` 互斥选取：Windows 用 `windows-sys` 等，Unix 用 `libc`；`tokio` 在通用依赖与 Windows target 依赖里各声明一次、feature 集合不同。
- `[dev-dependencies]` 只对示例/测试生效，其中的 `tokio` 带 `rt-multi-thread` 以支撑示例的 `#[tokio::main]`，不会传染给下游。
- lint 分两套：`Cargo.toml` 的 `[lints]` 全 crate 生效，`src/lib.rs` 的 `#![warn]` 仅库本体生效。
- 同步 local socket 示例刻意「一端先发、一端先收」以规避单线程收发死锁；异步示例则用 `try_join!` 并发收发，并用「双 main」门控在未启用 tokio 时给出可编译的占位程序。

## 7. 下一步学习建议

- 想真正读懂示例里 `ListenerOptions`、`Stream::connect`、`incoming()` 这些 API 的来龙去脉，请进入第二单元，先读 [u2-l1 Local Socket 的设计哲学](./u2-l1-local-socket-philosophy.md)。
- 对示例中 `GenericNamespaced` / `to_ns_name` / `to_fs_name` 这套名称构造感到好奇，可直接跳到 [u2-l4 名称系统：Name 与 NameType](./u2-l4-name-system.md)。
- 想系统地把同步 local socket API 用熟（监听器选项、连接选项、读写与拆分），进入第三单元，从 [u3-l1 ListenerOptions 与服务端创建](./u3-l1-listener-options.md) 开始。
- 若你更关心 unnamed pipe 或 named pipe 的示例，可对照本讲方法自行运行 `unnamed_pipe_sync` / `named_pipe_sync_server` 等示例（清单见 [Cargo.toml:114-149](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L114-L149)），对应原理分别在第五、四单元讲解。
