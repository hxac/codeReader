# BUS/RT 是什么：项目定位与技术栈

## 1. 本讲目标

学完本讲后，你应该能够：

- 用一句话说清楚 BUS/RT 是什么、它解决什么问题。
- 分清 BUS/RT 支持的三种通信模式（点对点、一对多、发布订阅）和四种传输通道（线程内异步通道、Unix socket、TCP、WebSocket）。
- 读懂 `Cargo.toml` 里的 `[features]` 表，明白 `broker` / `ipc` / `rpc` 这些 feature 各自打开了哪些模块、引入了哪些第三方依赖。
- 知道 BUS/RT 在 EVA ICS v4 中扮演「核心总线」的角色，以及它为什么用 Rust + Tokio 来写。

本讲是整套学习手册的第一篇，不要求你预先懂 Rust 的任何高级特性，但会涉及一些工程概念（feature、异步运行时、IPC）。这些概念会在第 2 节用通俗的话先解释一遍。

## 2. 前置知识

在开始前，下面几个名词最好先有个印象，看不懂也没关系，后面结合源码会再讲。

- **进程间通信（IPC, Inter-Process Communication）**：同一台机器或网络上的不同程序之间互相传消息的机制。常见的 IPC 方式有管道、共享内存、Unix socket、TCP 等。BUS/RT 就是一个专门管「谁把什么消息发给谁」的中间人。
- **消息总线 / 代理（broker）**：一个常驻运行、负责转发消息的程序。所有客户端都连到它，由它根据地址或主题把消息分发出去。你可以把它想象成邮局：发件人把信交给邮局，邮局按地址送到收件人手里。
- **Rust + Tokio**：Rust 是一门强调安全和性能的系统编程语言；Tokio 是 Rust 生态里最主流的异步运行时（async runtime），负责调度大量并发任务。BUS/RT 用它们来做到「又快又能同时处理海量连接」。
- **Cargo feature**：Rust 的包管理器 Cargo 允许在编译时用 `--features xxx` 开关可选功能。同一个 `busrt` crate，开不同 feature 会编译出完全不同的能力。理解 feature 是读 BUS/RT 源码的钥匙。
- **IPC 通道（channel）**：消息实际走的「物理管道」，比如一个本地 Unix socket 文件，或一个 TCP 端口。注意区分「通道（channel，物理传输）」和「通信模式（pattern，逻辑语义）」——前者是「怎么传」，后者是「传给谁」。

## 3. 本讲源码地图

本讲只看两个文件，它们是认识整个项目的「门面」：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `README.md` | 项目对外说明，描述 BUS/RT 是什么、能做什么、性能如何 | 定位、通信模式、通道、绑定、实时性 |
| `Cargo.toml` | Cargo 的配置文件，定义依赖、编译 feature、两个二进制 | `[features]` 表、`rpc`/`broker`/`ipc` 引入的依赖、`busrtd` 与 `busrt` 两个二进制 |

此外，为了让「feature → 模块」的映射有据可查，我们会顺带引用一点 `src/lib.rs` 里模块的条件编译声明（仅作印证，深入讲解留给后面的讲义）。

永久链接基址（本讲所有链接都基于当前 HEAD）：

```
https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/
```

## 4. 核心概念与源码讲解

### 4.1 项目说明：BUS/RT 是什么

#### 4.1.1 概念说明

BUS/RT 是一个**用 Rust + Tokio 写的进程间通信（IPC）消息代理（broker）**。

「消息代理」这个词可能有点抽象，我们用一个生活类比来理解：

- 想象一栋写字楼，里面有几十家公司（进程），它们之间要互相送快递。
- 如果每两家公司都自己约好怎么送（点对点直连），连线数量会爆炸（N 家公司要 N×(N-1)/2 条线）。
- 于是大楼里设了一个**总服务台（broker）**：所有公司都只和服务台对接，把快递交给服务台，由服务台按「收件人」或「楼层主题」分发。

BUS/RT 就是这个「总服务台」。它有两个关键定位：

1. **既可嵌入，也可独立运行**。你可以把它当成一个库（crate），直接在自己的 Rust 程序里 `use busrt`，让程序内部的多个线程/任务互相通信；也可以把它编译成一个独立的可执行文件 `busrtd`，作为一个常驻服务跑起来，给本机或网络上所有客户端用。
2. **为「高负载」和「超低延迟实时」同时优化**。这两件事通常是矛盾的（高吞吐要批处理，低延迟要即时发送），BUS/RT 用了一套缓冲 + 实时刷新的机制来兼顾，这套机制会在后面进阶讲义里细讲。

设计灵感上，BUS/RT 借鉴了三个业界知名项目：

- **NATS**：一个轻量级、高性能的消息系统，强调「主题（subject）+ 发布订阅」模型。
- **ZeroMQ**：一个强调「极低延迟、无 broker 也可用」的消息库。
- **Nanomsg**：ZeroMQ 的精神续作，提供多种通信模式（模式 / scalability protocols）。

读源码时你会看到这些灵感的影子：比如 NATS 式的「主题通配符订阅」（`#` 匹配所有子主题），ZeroMQ 式的「多种通信模式」，以及 Nanomsg 式的「把模式作为一等公民」。

它还是 **EVA ICS v4 的核心总线**。EVA ICS 是一个工业物联网（IIoT）/企业自动化平台，部署在电厂、工厂、城市基础设施里，大型部署会有上百万个传感器和受控设备。这意味着 BUS/RT 是为「真实工业场景、海量设备」设计的，不是玩具项目。

#### 4.1.2 核心流程

从「上帝视角」看，一个 BUS/RT 系统由三类角色组成：

```
   ┌──────────┐  send/publish   ┌──────────┐   deliver   ┌──────────┐
   │ 客户端 A  │ ──────────────▶ │  broker  │ ──────────▶│ 客户端 B  │
   │ (Rust/   │                 │ (busrtd  │            │ (Rust/   │
   │  Py/JS)  │ ◀────────────── │  或嵌入) │ ◀──────────│  Py/JS)  │
   └──────────┘     reply       └──────────┘   subscribe └──────────┘
                                         │
                                         ▼
                                  ┌──────────────────┐
                                  │ 记录: 谁连着、订阅 │
                                  │ 了什么、权限如何  │
                                  └──────────────────┘
```

逻辑流程：

1. broker 先启动（独立 `busrtd`，或嵌入到某个程序里）。
2. 客户端通过某种通道（Unix socket / TCP / WebSocket，或线程内异步通道）连上 broker，并注册一个名字。
3. 客户端按三种模式之一发消息：
   - **点对点（one-to-one）**：指定收件人名字，broker 投递给那个客户端。
   - **一对多广播（one-to-many / broadcast）**：发给所有连着的客户端。
   - **发布订阅（pub/sub）**：发到一个「主题」，所有订阅了该主题（可用通配符）的客户端都收到。
4. broker 根据内部维护的「连接表 / 订阅表 / 权限表」决定把消息送给谁。

注意：**通道（channel）决定消息走哪种物理管道，模式（pattern）决定消息按什么逻辑语义分发**，两者是正交的。这是后面所有讲义的基础认知。

#### 4.1.3 源码精读

先看 README 里对 BUS/RT 的「自我介绍」：

> [README.md:14-24](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/README.md#L14-L24) —— 这段是 BUS/RT 的官方定位：Rust 原生 IPC broker，用 Rust/Tokio 写成，灵感来自 NATS/ZeroMQ/Nanomsg；为高负载和超低延迟实时场景优化；既可嵌入也可独立运行；是 EVA ICS v4 的核心总线。

紧接着 README 列出了支持的通信模式与通道：

> [README.md:26-39](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/README.md#L26-L39) —— 这里明确写出三种通信模式（one-to-one、one-to-many、pub/sub）和四种通道（线程间异步通道、Unix socket、TCP、WebSocket）。注意 WebSocket 只在 broker 和 async client 端可用。

关于多语言绑定（非 Rust 也能用）：

> [README.md:41-48](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/README.md#L41-L48) —— BUS/RT 还提供 Python（同步）、Python（异步）、JavaScript（Node.js）、Dart 的绑定。跨语言之所以可行，是因为所有客户端走的是同一套二进制协议（后面讲义会精读这套协议）。

关于实时安全（real-time safety），README 专门有一节：

> [README.md:50-54](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/README.md#L50-L54) —— 启用 `rt` feature 后，内部互斥锁会换成 `parking_lot_rt`（一个去掉自旋锁的 `parking_lot` 分支），从而对实时场景安全。本讲只需记住「有这个开关」，细节在进阶讲义。

README 还附了一组基准测试数据，证明「又快又实时」不是空话：

> [README.md:60-77](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/README.md#L60-L77) —— 在 i7-7700HQ、4 worker、8 客户端、100 字节载荷的本地 Unix socket 场景下，纯 `send.qos.no` 可达约 \(2{,}748{,}870\) 次/秒，`send+recv.qos.no` 约 \(1{,}667{,}131\) 次/秒。这些数字说明它面向的是高性能场景。

> **小提示（术语）**：表里的 `qos.no` / `qos.processed` 指服务质量等级（QoS）。`No` 表示「发了就不管」，`Processed` 表示「要等对方确认处理」。QoS 是后续讲义的核心概念，这里先留个印象。

#### 4.1.4 代码实践

**实践目标**：用你自己的话，把 BUS/RT 讲清楚——这是检验「真的看懂了 README」的最快方式。

**操作步骤**：

1. 打开 [README.md](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/README.md)，重点读 *What is BUS/RT*、*Inter-process communication*、*Real-time safety* 三节。
2. 准备一个文本文件 `my-notes.md`（写在你自己的笔记目录，不要写进 `busrt-tutorial/`），用**一段话**回答下面四个问题：
   - BUS/RT 是什么？用一句不超过 30 字的话概括。
   - 它支持哪三种通信模式？各适合什么场景？
   - 它支持哪四种通道？哪一种是「只有 Rust 内嵌才用」「哪一种是 broker 和 async client 限定」？
   - 它在 EVA ICS v4 里扮演什么角色？

**需要观察的现象**：

- 你应该能不查资料就写出「one-to-one / one-to-many / pub-sub」和「线程内异步通道 / Unix socket / TCP / WebSocket」。
- 你应该注意到 WebSocket 那一行有个括号限定，TCP 那一行写明了「Linux/BSD/Windows」，而 Unix socket 写的是「Linux/BSD」——这暗示了平台差异。

**预期结果**：你写出的段落里应包含「Rust 原生 IPC broker」「嵌入式 + 独立服务两种用法」「EVA ICS v4 核心总线」这几个关键词。如果漏掉了「既可嵌入又可独立运行」，说明还要再读一遍 *What is BUS/RT* 那一段。

> 本实践不需要运行任何命令，属于「源码阅读型实践」。验证方法是把你写的段落讲给一个完全没听过 BUS/RT 的人，看 ta 能不能听懂。

#### 4.1.5 小练习与答案

**练习 1**：BUS/RT 借鉴了哪三个项目？它们分别贡献了什么灵感？

> **参考答案**：NATS（主题 + 发布订阅模型）、ZeroMQ（极低延迟、无 broker 也可用的消息库）、Nanomsg（把多种「通信模式」作为一等公民的 scalability protocols）。见 [README.md:16-18](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/README.md#L16-L18)。

**练习 2**：为什么说 BUS/RT「既可嵌入又可独立运行」？这两者分别对应什么？

> **参考答案**：「嵌入」指把 `busrt` 作为库 `use` 进自己的 Rust 程序，用线程内异步通道通信，不需要单独跑服务；「独立运行」指编译出 `busrtd` 二进制，作为一个常驻进程监听 Unix socket / TCP / WebSocket，供本机或网络上的多个客户端连接。见 [README.md:21-23](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/README.md#L21-L23)。

**练习 3**：「通道」和「通信模式」有什么区别？各举一个例子。

> **参考答案**：「通道」是消息走的物理管道，如 Unix socket、TCP；「通信模式」是消息分发的逻辑语义，如发布订阅（pub/sub）。前者回答「怎么传」，后者回答「传给谁」。见 [README.md:28-39](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/README.md#L28-L39)。

---

### 4.2 Cargo.toml：feature 开关与依赖体系

#### 4.2.1 概念说明

BUS/RT 是一个**高度模块化**的库：不同用户需要的部分完全不同。

- 一个只想「连上现成 broker 发消息」的客户端，根本不需要 broker 代码。
- 一个只想「嵌入 broker 做线程间通信」的程序，不需要 TCP/WebSocket 网络栈。
- 一个要在实时系统里跑的人，需要换掉默认的互斥锁实现。

如果把这些全打包给所有用户，二进制会很大、编译很慢。Rust 的解决办法是 **Cargo feature**：在 `Cargo.toml` 里声明一组可选开关，每个开关对应一批模块和依赖；用户在编译时用 `--features xxx` 按需打开。

所以读 BUS/RT 源码的第一步，不是看 `src/` 里的代码，而是看 `Cargo.toml` 的 `[features]` 表——它告诉你「这个项目到底由哪几块骨头拼起来的」。

`Cargo.toml` 还定义了两个二进制（`[[bin]]`）：

- **`busrtd`**：独立的服务端程序，对应 `src/server.rs`，需要 `server` feature。
- **`busrt`**：命令行客户端/调试工具，对应 `src/cli.rs`，需要 `cli` feature。

这两者都建立在核心库之上，feature 决定了它们能否被编译出来。

#### 4.2.2 核心流程

feature 的工作流程可以这样理解：

```
   cargo build --features "ipc rpc"
            │
            ▼
   ┌─────────────────────────────────────┐
   │ Cargo 读取 [features] 表             │
   │  ipc  → 打开 ipc 相关模块 + 依赖     │
   │  rpc  → 打开 rpc  相关模块 + 依赖     │
   └─────────────────────────────────────┘
            │
            ▼
   ┌─────────────────────────────────────┐
   │ rustc 按 #[cfg(feature = "...")]     │
   │ 条件编译，只编译被打开的代码          │
   └─────────────────────────────────────┘
            │
            ▼
   只含所需能力的 busrt 库 / 二进制
```

关键机制：feature 之间会**互相依赖**。例如 `broker-rpc = ["broker", "rpc", ...]`，意思是「打开 `broker-rpc` 会顺带打开 `broker` 和 `rpc`」。这种传递关系让用户只需要说一个高层目标（「我要带 RPC 的 broker」），Cargo 自动展开成一组底层 feature。

而在源码层面，每个模块都用 `#[cfg(feature = "...")]` 守卫，只有对应 feature 打开时才编译。我们在 `src/lib.rs` 里能直接看到这种映射。

#### 4.2.3 源码精读

先看包基本信息：

> [Cargo.toml:1-11](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L1-L11) —— 包名 `busrt`，版本 0.5.5，edition 2021，许可证 Apache-2.0，描述是「Local and network IPC bus」（本地与网络 IPC 总线），关键词 `bus / rt / ipc / pubsub`。这几行就回答了「这是什么、叫什么、什么协议」。

接下来是全文最关键的一块——`[features]` 表：

> [Cargo.toml:66-90](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L66-L90) —— 这里定义了全部 feature。本讲重点关注其中几个：
> - `rpc = ["dep:log", "dep:serde", "dep:async-trait", "dep:serde-value", "dep:parking_lot", "dep:regex", "dep:tokio-task-pool", "dep:tokio"]` —— RPC 层，需要 serde 做序列化、tokio 做异步、tokio-task-pool 做任务调度、regex 做方法名匹配。
> - `broker = ["dep:log", "submap/digest", "dep:async-trait", "dep:unix-named-pipe", "dep:nix", "dep:ipnetwork", "dep:triggered", "dep:parking_lot", "dep:tokio", "dep:rustls", ...WebSocket/TLS 一堆...]` —— broker 核心，需要 `submap/digest`（主题/订阅映射匹配）、`unix-named-pipe`（fifo 通道）、`ipnetwork`（AAA 主机白名单）、`triggered`（优雅关闭信号）、`rustls` + 一整套 WebSocket/TLS 依赖（网络传输）。
> - `ipc = ["dep:log", "dep:async-trait", "dep:parking_lot", "dep:tokio", "dep:rustls", ...WebSocket/TLS 一堆...]` —— IPC 客户端，依赖基本是 broker 网络栈的子集（客户端也要能连 TCP/WebSocket/TLS）。
> - `full = ["rpc", "ipc", "broker", "broker-rpc", "ipc-sync"]` —— 一键打开几乎所有异步能力（注释里那行 `#default = ["full"]` 说明默认并**不**开启 full，用户必须显式选）。

两个二进制的定义：

> [Cargo.toml:96-104](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L96-L104) —— `busrtd`（`src/server.rs`，需要 `server` feature）是独立服务端；`busrt`（`src/cli.rs`，需要 `cli` feature）是命令行工具。两个二进制各自由不同 feature 门控，互不依赖。

最后，印证一下「feature → 源码模块」的映射。打开 `src/lib.rs` 的模块声明区：

> [src/lib.rs:502-523](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L502-L523) —— 这里能直接看到对应关系：
> - `borrow`、`common`：**始终编译**（无 feature 守卫）。
> - `broker` 模块 ← `broker` feature。
> - `ipc` 模块 ← `ipc` feature。
> - `rpc` 模块 ← `rpc` 或 `rpc-sync` feature。
> - `sync` 模块（同步客户端）← `ipc-sync` 或 `rpc-sync`。
> - `cursors` 模块 ← `cursors` feature。
> - `client`（统一 AsyncClient trait）← `rpc`/`broker`/`ipc` 任一。
> - `comm`（缓冲写入器）← `broker` 或 `ipc`。
>
> 这张表是后面所有讲义的「导航图」：想读哪块，就开哪个 feature。

#### 4.2.4 代码实践

**实践目标**：亲手验证 feature 的「开关效应」，并整理出 `rpc` / `broker` / `ipc` 三个 feature 各自引入的关键依赖——这正是练习任务要求的产出。

**操作步骤**：

1. 打开 [Cargo.toml 的 [features] 段](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L66-L90)。
2. 在笔记里画一张表，把 `rpc`、`broker`、`ipc` 三个 feature 各自**最关键**的 2~3 个依赖挑出来，并写一句话说明「为什么需要它」。参考答案见本节练习，但请先自己填。

   参考填法（仅作对照）：
   | feature | 关键依赖 | 为什么需要 |
   | --- | --- | --- |
   | `rpc` | `serde` / `rmp-serde`（后者经 `broker-rpc` 引入） | 把 RPC 调用参数序列化成 msgpack |
   | `rpc` | `tokio-task-pool` | 用任务池并发执行 RPC 处理器 |
   | `broker` | `submap/digest` | 主题/订阅的通配符匹配（`#` `+`） |
   | `broker` | `ipnetwork` + `unix-named-pipe` | AAA 主机白名单 + fifo 命令通道 |
   | `broker` | `rustls` + WebSocket 系 | 安全的 TCP/WebSocket 传输 |
   | `ipc` | `tokio` + `async-trait` | 异步客户端 + AsyncClient trait |
   | `ipc` | `rustls` + WebSocket 系 | 客户端也要连 TCP/WebSocket/TLS |

3. （可选，需本地有 Rust 工具链）验证 feature 门控：
   ```bash
   # 只编译 rpc，不应该出现 broker/ipc 相关代码被链接
   cargo build --no-default-features --features rpc
   # 再加上 ipc
   cargo build --no-default-features --features "ipc rpc"
   ```
   想编译 `busrtd` 服务端：
   ```bash
   cargo build --release --features server
   ```
   想编译 `busrt` 命令行工具：
   ```bash
   cargo build --release --features cli
   ```

**需要观察的现象**：

- 步骤 2 的表里，你应该发现 `broker` 和 `ipc` 的网络依赖高度重叠（都含 `rustls`、`tungstenite`、`ws_stream_tungstenite` 等），因为「服务端和客户端说的是同一种网络协议」。
- 步骤 3 里，`cargo build --features server` 会拉入 `broker-rpc`（因为 `server` 依赖 `broker-rpc`，见 [Cargo.toml:67-68](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L67-L68)），这体现了 feature 的传递性。

**预期结果**：

- 表格能清晰区分「rpc 管 RPC 调用层」「broker 管服务端核心」「ipc 管客户端」。
- 编译命令能成功（前提是网络能拉取依赖）。若本地无 Rust 工具链或离线，**待本地验证**——至少能口头说清「开哪个 feature 编哪个模块」。

#### 4.2.5 小练习与答案

**练习 1**：`broker-rpc` 这个 feature 和 `broker`、`rpc` 是什么关系？为什么要单独设一个 `broker-rpc`？

> **参考答案**：`broker-rpc = ["broker", "rpc", "dep:rmp-serde"]`，它同时打开 `broker` 和 `rpc`，并额外引入 `rmp-serde`（msgpack 序列化）。单独设它是因为「带 RPC 能力的 broker」是一个常见组合，让用户一句 `--features broker-rpc` 即可，而不必每次写 `broker rpc rmp-serde`。见 [Cargo.toml:73](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L73)。

**练习 2**：如果只想写一个「连接现成 broker、发发布订阅消息」的轻量客户端，应该开哪些 feature？为什么不需要 `broker`？

> **参考答案**：开 `ipc`（客户端传输与帧编解码）和 `rpc`（如果要用 RPC）。不需要 `broker`，因为客户端不自己当代理，broker 代码（订阅表、AAA、服务端监听）对客户端是多余的，关掉能大幅减小二进制和编译时间。见 [Cargo.toml:74-76](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L74-L76) 与 [src/lib.rs:509-514](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L509-L514)。

**练习 3**：`full` feature 包含了哪些 feature？为什么默认不启用它？

> **参考答案**：`full = ["rpc", "ipc", "broker", "broker-rpc", "ipc-sync"]`（注意不含 `cli`/`server`/`cursors`/`sync` 的 rpc 部分）。默认不启用（`#default = ["full"]` 被注释）是为了让用户按需编译、避免拉入用不到的重型依赖（如 WebSocket/TLS 栈）。见 [Cargo.toml:84-90](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L84-L90)。

## 5. 综合实践

**任务**：把本讲的两个最小模块串起来，做一次「读说明 → 选 feature → 看模块」的完整闭环。

1. 读 [README.md](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/README.md)，确定你想用 BUS/RT 做的三件事之一：
   - 场景 A：写一个**嵌入 broker** 的 Rust 程序，让它内部多个任务互发消息。
   - 场景 B：写一个**客户端**，连上别人起的 `busrtd`，发发布订阅消息。
   - 场景 C：**起一个独立服务端** `busrtd`，给本机几个客户端用。
2. 针对 you 选的场景，在 [Cargo.toml [features]](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L66-L90) 里挑出**最小**的 feature 组合，并写出对应的 `cargo build` 命令。参考：
   - 场景 A → `broker`（线程间异步通道是 broker 内嵌模式，见 README 第 36 行）。
   - 场景 B → `ipc`（+ `rpc` 如需 RPC）。
   - 场景 C → `server`（它会传递引入 `broker-rpc`）。
3. 对照 [src/lib.rs:502-523](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L502-L523)，列出你选的 feature 组合会**实际编译出哪些 `pub mod`**。例如场景 A 应包含 `broker`、`comm`、`borrow`、`common`、`client`。
4. 用一段话总结：「我选这个 feature 组合的原因是……，它对应的源码模块有……，被排除的是……。」

**验收标准**：你能不查表说出「场景 → feature → 模块」的对应关系，且能解释「为什么不需要把 `full` 全开」。这就是本讲想建立的整体认知地图。

> 若本地暂无 Rust 工具链，第 2 步的 `cargo build` 命令标注为「待本地验证」，但 1/3/4 步的阅读与归纳完全可以完成。

## 6. 本讲小结

- BUS/RT 是一个用 Rust + Tokio 写的**进程间通信（IPC）消息代理**，灵感来自 NATS / ZeroMQ / Nanomsg，是 EVA ICS v4 的核心总线。
- 它支持三种通信模式：**点对点（one-to-one）、广播（one-to-many）、发布订阅（pub/sub）**。
- 它支持四种通道：**线程内异步通道（仅 Rust 内嵌）、Unix socket、TCP、WebSocket（仅 broker 与 async client）**。
- 它「既可嵌入 Rust 程序，也可作为独立服务 `busrtd` 运行」，并通过 `rt` feature 提供实时安全（无自旋锁）。
- 项目是**高度模块化**的：`Cargo.toml` 的 `[features]` 表是理解整个项目的钥匙，`rpc` / `broker` / `ipc` 等开关各自打开不同模块和依赖。
- 两个二进制 `busrtd`（服务端，`server` feature）和 `busrt`（CLI，`cli` feature）都建立在核心库之上。

## 7. 下一步学习建议

本讲建立了「BUS/RT 是什么 + feature 怎么开」的整体印象。接下来建议：

1. **先动手跑起来**：进入第 2 讲 *从零构建与运行*，亲手编译 `busrtd` 并用 `busrt` CLI 连上去，获得第一手体感。
2. **建立源码导航**：进入第 3 讲 *源码地图：目录结构与 feature→模块映射*，把本讲提到的 `src/lib.rs` 模块声明表完整过一遍，为后续精读源码铺路。
3. **不要急着看 broker 源码**：在跑通示例、看懂模块地图之前，直接读 `broker.rs` 会很容易迷路。按大纲顺序循序渐进更高效。
