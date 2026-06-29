# 项目总览：UDT 协议与 tokio-udt 定位

## 1. 本讲目标

本讲是整个学习手册的第一篇，目标是让你在不动手写任何协议代码的前提下，先建立三个直觉：

1. **UDT 是什么、为什么存在**——它想替代 TCP 的哪些痛点。
2. **tokio-udt 是什么**——它在 Rust / Tokio 生态里的定位、技术栈、版本与许可证。
3. **它对外提供哪些能力**——通过 README 的两个最小示例，先认识 `UdtListener` 和 `UdtConnection` 这两个入口。

学完本讲，你应该能用自己的话回答：「tokio-udt 解决了什么问题、依赖哪些关键 crate、当前是什么版本和 license」。后续每一篇讲义都会在这张「地图」上往深里走。

## 2. 前置知识

本讲面向零基础读者，但有几条概念最好先有个模糊印象，读起来会更顺：

- **TCP**：我们熟悉的「可靠传输」协议，自带重传、拥塞控制、按序到达。它在广域网高速链路上效率不高、且容易和其他连接抢占带宽（fairness 问题）。
- **UDP**：轻量、不可靠、无连接的传输协议，只有一个「尽力而为」的数据报服务，没有任何可靠性保证。
- **Rust 与 Tokio**：Rust 是一门系统级语言；Tokio 是 Rust 生态里事实标准的异步运行时，提供 `async/await`、`AsyncRead`/`AsyncWrite`、定时器、网络等异步原语。
- **crate 与 Cargo.toml**：Rust 用 Cargo 做包管理，一个 crate（库）的元信息和依赖都写在 `Cargo.toml` 里。

> 不熟悉其中某些概念也没关系，本讲会在用到时用通俗语言补充解释。本讲**不要求**你已经读过任何项目源码。

## 3. 本讲源码地图

本讲只看「门面」级别的三个文件，它们决定了你对项目的第一印象：

| 文件 | 作用 | 本讲解读重点 |
| --- | --- | --- |
| `README.md` | 面向用户的说明：UDT 是什么、两个最小用法示例 | 协议背景 + 入口 API 示例 |
| `Cargo.toml` | crate 的元信息与依赖清单 | 版本、license、技术栈 |
| `src/lib.rs` | crate 的根模块，包含顶层文档与模块声明 | 顶层文档、`pub use` 导出边界 |

> 这三个文件加起来不到 100 行有效内容，却是理解整个 crate 的「目录页」。后面所有讲义都会反复回到它们。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：UDT 协议背景、Cargo.toml 依赖与元信息、lib.rs 顶层文档。

### 4.1 UDT 协议背景

#### 4.1.1 概念说明

**UDT（UDP-based Data Transfer Protocol）** 是一个构建在 UDP 之上的高性能数据传输协议。它的核心动机可以一句话概括：

> 在高带宽、高延迟的广域网（WAN）上，TCP 既「不够快」（效率问题），又「不够公平」（fairness 问题）。UDT 用 UDP 作为底层数据通道，自己在上层实现可靠传输与拥塞控制，从而专门服务数据密集型应用。

具体来说，UDT 同时提供两类服务：

- **可靠的数据流（reliable data streaming）**：像 TCP 一样保证数据按序、不丢、不重。
- **消息服务（messaging）**：可以按「一条完整消息」的边界来发送和接收。

为什么要「基于 UDP」而不是直接改 TCP？因为 TCP 的拥塞控制算法被固化在操作系统内核里，应用层很难替换；而 UDT 在 UDP 之上自己实现可靠性与拥塞控制，就可以用更适合高速广域网的算法，并且可以更公平地与 TCP 流共存。

#### 4.1.2 核心流程

从「应用层数据」到「网络字节」的概貌可以画成下面这条链（本讲只建立直觉，细节留到后续讲义）：

```text
应用层 (read/write)
   │  tokio-udt 在中间做：
   ├─ 1. 把字节流切成 UDT 数据包（受 MSS 限制）
   ├─ 2. 可靠性：序号 + 接收方 ACK + 丢包 NAK + 重传
   ├─ 3. 拥塞控制：慢启动 / AIMD / 速率调节，控制「何时发、发多快」
   └─ 4. 最终通过一个普通 UDP socket 收发字节
网络层 (UDP/IP)
```

你可以把它理解成：「UDP 提供了一条不可靠的管道，UDT 在管道两端加上一层『可靠性 + 拥塞控制』的引擎，让上层用起来像 TCP，但更快、更可控。」

#### 4.1.3 源码精读

项目在 `README.md` 开头就用一段话说明了 UDT 的定位，这正是本讲的核心依据：

[README.md:13-22](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/README.md#L13-L22) —— README 的「What is UDT?」一节，用 6 行话说清了 UDT 的目标、设计动机、与 UDP/TCP 的关系，以及它同时提供 streaming 与 messaging 两类服务。

其中最关键的两句可以直接对照[README.md:15-18](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/README.md#L15-L18)：UDT 是为「数据密集型应用在高速广域网」设计，目的是「克服 TCP 的效率与公平性问题」，并且「built on top of UDP」。

想深入了解协议本身，README 给出了两个权威入口：

- 协议官网与文档：[README.md:20](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/README.md#L20) 指向 `https://udt.sourceforge.io/`。
- 参考的 C++ 实现：[README.md:22](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/README.md#L22) 指向 `https://github.com/eminence/udt`。

> 也就是说，tokio-udt 本质上是「用 Rust + Tokio 重新实现一遍 UDT 协议」。本系列讲义后续遇到协议细节（包格式、握手、拥塞控制）时，都可以回到上面两个参考资料做对照。

同样的定位描述也写在 crate 的顶层文档里，措辞几乎一致：[src/lib.rs:2-7](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L2-L7)。

#### 4.1.4 代码实践

> **实践目标**：用阅读 + 检索的方式，确认 UDT 的设计动机，并建立「tokio-udt = UDT 的 Rust 实现」这个判断。

操作步骤：

1. 打开 [README.md:13-22](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/README.md#L13-L22)，用中文复述 UDT 解决的两个问题（效率、公平）。
2. 访问 README 第 20 行给出的 `https://udt.sourceforge.io/`，浏览协议概览（**待本地验证**：该外部网站是否可访问取决于你的网络环境）。
3. 在本仓库中用检索工具搜索 `udt.sourceforge.io` 这类字样，确认项目里是否还有别处引用协议规范。

需要观察的现象 / 预期结果：

- 你应该能从 README 原文中找到「high speed wide area networks」「efficiency and fairness problems of TCP」「built on top of UDP」这三个关键短语。
- 复述时能区分「UDT 协议（规范）」与「tokio-udt（某个具体实现）」是两个不同概念。

#### 4.1.5 小练习与答案

**练习 1**：UDT 为什么选择「基于 UDP」而不是「改进 TCP」？

> **参考答案**：TCP 的可靠性与拥塞控制固化在操作系统内核中，应用层难以替换其算法；而 UDT 在 UDP 这条「不可靠管道」之上自行实现可靠性与拥塞控制，既能采用更适合高速广域网的算法，也便于应用层定制，同时能与现有 TCP 流更公平地共存。

**练习 2**：UDT 同时提供哪两类服务？

> **参考答案**：可靠的数据流（reliable data streaming）与消息服务（messaging）。前者像 TCP 一样按序可靠传输，后者支持按消息边界收发。

### 4.2 Cargo.toml 依赖与元信息

#### 4.2.1 概念说明

`Cargo.toml` 是这个 crate 的「身份证 + 采购清单」：上面写着它的名字、版本、许可证、Rust edition，以及它依赖了哪些外部 crate。对一个库的使用者来说，这是判断「能不能用、要不要用、依赖重不重」的第一依据。

#### 4.2.2 核心流程

阅读一个 `Cargo.toml` 时，建议按这个顺序看：

```text
1. [package]          → 名字、版本、edition、license、描述、仓库地址
2. [dependencies]     → 通用依赖（所有平台都要用）
3. [target...'linux'] → 平台专用依赖（只有特定平台编译）
4. [dev-dependencies] → 只在测试/文档时用
```

tokio-udt 正好四种都齐了，是个很标准的范本。

#### 4.2.3 源码精读

先看包的元信息：[Cargo.toml:1-11](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L1-L11)。关键事实逐条对应：

- 名字 `tokio-udt`、版本 `0.1.0-alpha.8`（[Cargo.toml:3](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L3)）——注意是 **alpha** 版，意味着 API 仍可能变动。
- Rust `edition = "2021"`（[Cargo.toml:4](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L4)）。
- 许可证 `AGPL-3.0`（[Cargo.toml:5](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L5)）——这是一个 copyleft 较强的许可证，集成前需留意合规要求。
- 关键字 `udt / udt4 / networking / transport / protocol`（[Cargo.toml:10](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L10)），用于 crates.io 检索。

> ⚠️ 一个值得注意的细节：[Cargo.toml:9](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L9) 的 `repository` 字段指向 `https://github.com/amatissart/tokio-udt`，而本学习手册所在的组织是 `Distributed-EPFL/tokio-udt`。这说明该字段可能是早期作者的个人仓库地址、尚未更新。这是真实存在于源码中的事实，**待后续讲义或项目维护者确认**是否有意为之。

再看通用依赖清单：[Cargo.toml:14-21](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L14-L21)。这些依赖大致勾勒出整个技术栈（每个 crate 的具体用途会在后续讲义逐一验证）：

| 依赖 | 版本 | 在本项目里大致承担的角色 |
| --- | --- | --- |
| `tokio` | `1.*`（[L16](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L16)） | 异步运行时，开启了 `macros/net/io-util/sync/time/rt-multi-thread` 多个 feature |
| `rand` | `0.8`（[L15](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L15)） | 随机数（如握手 cookie 的随机盐、拥塞控制的随机化） |
| `sha2` | `0.10.2`（[L17](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L17)） | SHA-256，用于握手阶段的 SYN cookie 计算 |
| `once_cell` | `1.12`（[L18](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L18)） | 提供进程级全局单例（UDT 引擎实例） |
| `socket2` | `0.4.4`（[L19](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L19)） | 底层 socket 配置（缓冲区大小、`SO_REUSEPORT` 等） |
| `nix` | `0.24.2`（[L20](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L20)） | Unix 系统调用封装（Linux 下的批量收发系统调用） |
| `bytes` | `1.1`（[L21](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L21)） | 高效的字节缓冲区管理 |

平台专用依赖：[Cargo.toml:23-24](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L23-L24) 仅在 `cfg(target_os="linux")` 下引入 `tokio-timerfd`，用于 Linux 上的高精度定时器（这是后续「平台快路径」讲义的重点）。

开发依赖：[Cargo.toml:26-27](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L26-L27) 引入 `doc-comment`，作用是把 `README.md` 当作 doctest 跑一遍——后面 `lib.rs` 里会用到它。

#### 4.2.4 代码实践

> **实践目标**：把 `Cargo.toml` 的元信息和依赖，整理成一份你能随时查阅的「事实清单」。

操作步骤：

1. 打开 [Cargo.toml:1-11](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L1-L11)，记录 crate 名、版本、edition、license、关键字。
2. 在仓库根目录执行 `cargo doc --open`（**待本地验证**：需要可联网拉取依赖）。这会编译并打开库文档，你会看到顶层文档里就是 `lib.rs` 第 1–67 行的内容。
3. 对照 [Cargo.toml:14-27](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L14-L27)，把 8 个依赖按「通用 / Linux 专用 / 仅测试」分成三类。

需要观察的现象 / 预期结果：

- `cargo doc --open` 成功后，浏览器里应能看到 `UdtListener`、`UdtConnection`、`UdtConfiguration`、`RateControl`、`SeqNumber` 这几个公共类型（它们由 `lib.rs` 的 `pub use` 导出，见 4.3）。
- 如果你不在 Linux 上，`tokio-timerfd` 不会被编译——这解释了为什么它单独放在 `cfg(target_os="linux")` 下。

#### 4.2.5 小练习与答案

**练习 1**：tokio-udt 当前是什么版本和许可证？这两个信息对一个想集成它的项目分别意味着什么？

> **参考答案**：版本 `0.1.0-alpha.8`，许可证 `AGPL-3.0`。alpha 版意味着 API 尚未稳定、可能在不通知的情况下变动；AGPL-3.0 是强 copyleft 许可证，通过网络提供服务也要开放源码，集成前必须做合规评估。

**练习 2**：为什么 `tokio-timerfd` 要单独用 `[target.'cfg(target_os="linux")'.dependencies]` 声明，而不是放在普通 `[dependencies]` 里？

> **参考答案**：因为它只在 Linux 上可用（依赖 Linux 的 `timerfd` 内核机制）。放在目标条件依赖里，可以确保在 macOS / Windows 等非 Linux 平台编译时不会去拉取它，从而保证跨平台可编译。

### 4.3 lib.rs 顶层文档

#### 4.3.1 概念说明

`src/lib.rs` 是整个 crate 的根模块。它做了三件事：

1. 用一段顶层文档（`/*! ... */`）介绍库并给出用法示例——这段文档就是 `cargo doc` 生成的首页。
2. 用一连串 `mod xxx;` 声明 crate 内部的所有子模块。
3. 用 `pub use ...` 把少数几个类型「重新导出」到 crate 根，作为对外的公共 API。

对学习者来说，`lib.rs` 就是整个项目的「目录页 + 公共接口边界」。

#### 4.3.2 核心流程

读 `lib.rs` 的顺序与读 `Cargo.toml` 类似，但要特别关注「公开 vs 内部」这条线：

```text
顶层文档 (//!)     → 给用户看的介绍 + 示例
mod xxx;           → 内部模块（默认是私有的，外部用不到）
pub use xxx::Yyy;  → 真正对外暴露的公共 API
```

只有出现在 `pub use` 里的类型，才是这个 crate 的「对外承诺」。其余 `mod` 声明的模块是实现细节，使用者既看不到也不应依赖。

#### 4.3.3 源码精读

顶层文档开头复述了 UDT 的定位：[src/lib.rs:2-7](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L2-L7)。随后给出了和 README 完全一致的两个示例：

- 服务端示例：[src/lib.rs:10-47](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L10-L47)，核心是 `UdtListener::bind(...)` 之后循环 `listener.accept().await`。
- 客户端示例：[src/lib.rs:49-66](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L49-L66)，核心是 `UdtConnection::connect(...)` 之后 `connection.write_all(...)`。

接着是模块声明区：[src/lib.rs:68-84](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L68-L84)。这里一口气声明了 17 个子模块（`ack_window`、`common`、`configuration`、`connection`、`control_packet`、`data_packet`、`flow`、`listener`、`loss_list`、`multiplexer`、`packet`、`queue`、`rate_control`、`seq_number`、`socket`、`state`、`udt`）。光看名字就能猜出整个项目的骨架——这些模块后续会被组织成「数据通路 / 可靠性 / 拥塞控制 / 连接生命周期」等主题逐篇拆解（见本讲的「下一步学习建议」与本系列大纲）。

> 注意：这些 `mod` 都是默认私有（没有 `pub`），说明它们是内部实现。对使用者来说，唯一重要的是下面这段导出。

公共 API 边界：[src/lib.rs:86-90](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L86-L90) 用 `pub use` 暴露了 5 个类型：

| 导出类型 | 来自模块 | 一句话角色 |
| --- | --- | --- |
| `UdtConfiguration` | `configuration` | 配置项（MSS、缓冲大小、复用、linger 等） |
| `UdtConnection` | `connection` | 面向连接的读写句柄，实现 `AsyncRead`/`AsyncWrite` |
| `UdtListener` | `listener` | 服务端监听器，`bind` + `accept` |
| `RateControl` | `rate_control` | 拥塞控制（可读指标，如发送周期、拥塞窗口） |
| `SeqNumber` | `seq_number` | UDT 的循环序列号类型 |

最后，[src/lib.rs:92-93](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L92-L93) 用 `doc_comment::doctest!("../README.md")` 把 README 也纳入 doctest——这正是 [Cargo.toml:27](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L27) 那个 `doc-comment` 开发依赖的用途，保证 README 里的示例代码永远是可编译的。

#### 4.3.4 代码实践

> **实践目标**：把「对外公共 API」和「内部实现模块」这条边界亲手分清楚。

操作步骤：

1. 打开 [src/lib.rs:68-90](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L68-L90)。
2. 把 17 个 `mod`（内部实现）和 5 个 `pub use`（公共 API）分别抄成两张清单。
3. 思考：README 示例里用到了哪些类型？它们是否都出现在 `pub use` 清单里？

需要观察的现象 / 预期结果：

- README 的 listener 示例只用到 `UdtListener`，client 示例只用到 `UdtConnection`（见 [README.md:32](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/README.md#L32) 与 [README.md:67](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/README.md#L67) 的 `use` 语句），二者都在 `pub use` 清单中——这是一致且自洽的。
- 其余 16 个内部模块（如 `packet`、`flow`、`rate_control`）虽然在 crate 里，但用户代码 `use tokio_udt::...` 时无法直接引用它们。

#### 4.3.5 小练习与答案

**练习 1**：crate 对外暴露了哪 5 个类型？其中哪两个是 README 示例里真正用到的？

> **参考答案**：`UdtConfiguration`、`UdtConnection`、`UdtListener`、`RateControl`、`SeqNumber` 共 5 个。README 示例真正用到的是 `UdtListener`（服务端）和 `UdtConnection`（客户端）。

**练习 2**：[src/lib.rs:92-93](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L92-L93) 的 `doc_comment::doctest!("../README.md")` 起什么作用？为什么这是个好习惯？

> **参考答案**：它把 README 中的代码块当作 doctest 来编译运行，确保文档示例永远能通过编译、不会随代码演进而腐化。这是个好习惯，因为 README 通常是用户接触项目的第一份代码，保证它可编译能极大降低上手成本。

## 5. 综合实践

把本讲的三个模块串起来，完成下面这个「项目速览」小任务：

1. **协议层**：阅读 [README.md:13-22](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/README.md#L13-L22)，用一句话写出 UDT 想替代 TCP 的哪两类问题。
2. **工程层**：阅读 [Cargo.toml:1-27](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L1-L27)，填一张「事实卡片」：版本、edition、license、通用依赖数、是否有 Linux 专用依赖。
3. **接口层**：阅读 [src/lib.rs:86-90](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L86-L90)，列出公共 API，并标注 README 示例用到了其中哪两个。
4. **验证**：执行 `cargo doc --open`（**待本地验证**），确认首页内容来自 `lib.rs` 顶层文档，且能在文档里看到这 5 个公共类型。

最终产出：一张不超过 10 行的「项目速览卡」。它将成为你后续阅读所有源码讲义时的速查表。

## 6. 本讲小结

- UDT 是构建在 UDP 之上的高性能可靠传输协议，目标是克服 TCP 在高速广域网上的**效率**与**公平性**问题，同时提供 streaming 与 messaging 两类服务。
- tokio-udt 是 UDT 协议的 **Rust + Tokio** 实现，当前版本 `0.1.0-alpha.8`、edition 2021、许可证 **AGPL-3.0**（强 copyleft，集成需评估合规）。
- 技术栈以 `tokio`（异步运行时）为核心，辅以 `socket2`/`nix`（底层 socket 与系统调用）、`bytes`（缓冲）、`sha2`（握手 cookie）、`once_cell`（全局单例）、`rand`，并在 Linux 上额外引入 `tokio-timerfd` 做高精度定时。
- crate 的对外公共 API 只有 5 个类型：`UdtConfiguration`、`UdtConnection`、`UdtListener`、`RateControl`、`SeqNumber`；README 示例只用到 `UdtListener` 与 `UdtConnection`。
- `lib.rs` 声明了 17 个内部子模块，它们勾勒出整个项目的骨架，是后续讲义逐层拆解的目录。
- `lib.rs` 用 `doc_comment` 把 README 当作 doctest，保证文档示例永远可编译。

## 7. 下一步学习建议

本讲只看了「门面」，还没真正跑过任何 UDT 流量。建议按以下顺序继续：

1. **下一篇 `u1-l2`（构建运行与示例）**：亲手用 `cargo run` 跑通仓库自带的 `udt_sender` / `udt_receiver` 两个二进制，观察吞吐与拥塞窗口的实时变化——把「纸面上的 UDT」变成「能跑的 UDT」。
2. **`u1-l3`（目录结构与模块地图）**：以本讲看到的 17 个 `mod` 为线索，画出完整模块树，为后续逐模块深入做好准备。
3. **`u1-l4`（公共 API 全貌与配置）**：细化本讲列出的 5 个公共类型，特别是 `UdtConfiguration` 的关键字段。

如果你想现在就跳进协议细节，也可以先去 [README.md:20](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/README.md#L20) 给出的 UDT 官方文档浏览协议概览，再回到本系列讲义对照源码——但更推荐先跑通示例（`u1-l2`），建立「它真的能传数据」的体感。
