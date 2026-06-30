# Workspace 与 crate 布局总览

## 1. 本讲目标

在《u1-l1》里我们建立了「Zenoh 是什么、仓库有哪六大块」的直觉，在《u1-l2》里我们用 `cargo run --example` 把示例跑了起来。本讲要回答的是一个更结构化的问题：**这些目录里的几十个 crate 到底谁依赖谁、谁对用户稳定、谁只在内部使用？**

读完本讲，你应当能够：

- 看懂根 `Cargo.toml` 的 `workspace.members` 列表，并说出每个 crate 的职责（包括本次新增的内部 crate `zenoh-uring`）。
- 画出 `io（link/transport）→ zenoh（api/net）→ zenohd/plugins` 的依赖方向，理解为什么是「从底向上」堆叠。
- 区分「对用户稳定的 crate（`zenoh`、`zenoh-ext`）」与「内部实现 crate（`commons/*`、`io/*`）」。
- 理解 `transport_tcp`、`transport_quic`、`shared-memory`、`unstable`、`uring` 这类 **feature 开关** 如何沿依赖链层层下传、按需启用。

> 本讲相对上一版的更新点：Zenoh 在 `5dd1a2f7`（"Support io_uring"）这一提交里新增了内部 crate `zenoh-uring`，并引入了一条贯穿 `zenoh → zenoh-transport → zenoh-link` 的 `uring` feature，用来在 Linux 上启用基于 io_uring 的零拷贝接收路径。本讲会把它纳入 crate 地图与 feature 转发链中讲解（运行时接收路径的细节留到《u9-l5 io_uring 接收路径》）。

本讲是后续所有「内部架构」讲义的地图：当我们后面讲传输层、路由层、协议编解码时，你会反复回到这张依赖图来定位文件。

## 2. 前置知识

### 2.1 什么是 Cargo Workspace

一个 Rust 项目可以有多个 crate（多个 `Cargo.toml`）。当你有一组「共享版本号、互相依赖、一起编译」的 crate 时，用 **workspace** 把它们组织起来。workspace 有一个根 `Cargo.toml`，它不产出一个库/二进制，而是：

- 用 `[workspace] members = [...]` 列出所有子 crate。
- 用 `[workspace.package]` 声明共享的包元信息（版本、Rust 版本、license），子 crate 用 `{ workspace = true }` 继承。
- 用 `[workspace.dependencies]` 声明共享的依赖版本，子 crate 用 `{ workspace = true }` 引用，避免到处重复写版本号。

> 小贴士：把版本写在 workspace 顶部，是为了保证「所有内部 crate 用同一个版本号、互相严格锁定」。Zenoh 全仓的内部 crate 都锁定在同一个 `1.9.0`（根 `Cargo.toml` 里写成 `version = "=1.9.0"`，等号表示「恰好这个版本」）。

### 2.2 依赖方向意味着什么

在 Rust 里，**被依赖的 crate 不能反向依赖它的依赖者**，否则会形成循环。所以一个大型项目天然分层：

- **底层 crate**：只依赖外部库（如 `serde`），不依赖任何「自家兄弟」。
- **中层 crate**：依赖底层自家 crate + 外部库。
- **顶层 crate**：把中下层组合起来，对外暴露 API。

本讲的核心工作，就是把 Zenoh 的 crate 按「底→中→顶」分层画出来。

### 2.3 feature 是什么

一个 crate 可以在 `[features]` 里声明一组「可选编译特性」，比如 `transport_tcp`。feature 可以：

- 开启/关闭某些依赖（用 `optional = true` 标记的可选依赖）。
- 开启/关闭某些代码路径（用 `#[cfg(feature = "...")]` 门控）。
- **转发到下游 crate**：在自己的 feature 定义里写 `zenoh-transport/transport_tcp`，就等于「当我启用 `transport_tcp` 时，也请下游 `zenoh-transport` 启用它」。

这种「层层转发」是理解 Zenoh feature 系统的关键，第 4.3 节会专门讲，并在 4.3.6 用本次新增的 `uring` feature 做一次完整演示。

## 3. 本讲源码地图

本讲主要阅读配置类文件，它们描述了「结构」而非「行为」：

| 文件 | 作用 |
|------|------|
| `Cargo.toml`（根） | workspace 的总入口：列出所有成员、共享版本、共享依赖（含新增的 `zenoh-uring`、`io-uring`）。 |
| `README.md` | 用人话描述仓库六大块（zenoh / zenoh-ext / zenohd / plugins / commons / examples）的职责与稳定边界。 |
| `zenohd/README.md` | `zenohd` 路由器二进制的命令行参数、插件加载方式，以及它和 `zenoh` crate 的关系。 |
| `zenoh/Cargo.toml` | 主 crate 的 feature 定义与依赖列表，是「feature 转发」的中心枢纽。 |
| `commons/zenoh-uring/Cargo.toml` | 本次新增的 io_uring 内部 crate，确认它属于 commons 地基、且依赖被 Linux target 门控。 |
| `zenohd/Cargo.toml`、`io/zenoh-transport/Cargo.toml`、`io/zenoh-link/Cargo.toml`、`io/zenoh-link-commons/Cargo.toml`、若干 `commons/*/Cargo.toml` | 用来确认依赖箭头的方向与 `uring` feature 的转发目标。 |

## 4. 核心概念与源码讲解

### 4.1 workspace 配置：members、package 与 resolver

#### 4.1.1 概念说明

根 `Cargo.toml` 是整个 Zenoh 仓库的「总目录页」。它做三件事：

1. **声明 workspace**：用 `members = [...]` 列出所有参与编译的子 crate。
2. **共享包元信息**：用 `[workspace.package]` 集中管理版本号、Rust 最低版本、license。
3. **共享依赖**：用 `[workspace.dependencies]` 集中管理所有外部和内部依赖的版本。

`members` 列表本身就是一张「仓库里到底有哪些 crate」的权威清单。Zenoh 把成员按目录前缀分成了几个直觉分组：`commons/*`、`io/*`、`plugins/*`，以及顶层的 `zenoh`、`zenoh-ext`、`zenohd`、`examples`。

#### 4.1.2 核心流程

当一个用户执行 `cargo build` 时，cargo 的处理流程大致是：

1. 读取根 `Cargo.toml`，识别这是一个 workspace。
2. 解析 `members` 列表，找到每个子 crate 的 `Cargo.toml`。
3. 收集所有子 crate 的依赖，结合 `[workspace.dependencies]` 统一版本。
4. `resolver = "2"` 告诉 cargo 用新版依赖解析器（更准确地按 feature 解析，避免不必要的 feature 被合并进来）。
5. 构建出依赖图，从底层开始编译。

`resolver = "2"` 对 Zenoh 这种「feature 多、可选依赖多」的项目尤其重要：旧解析器可能把不同 target 的 feature 合并，新解析器按 target 分别处理——这对本次新增的 `zenoh-uring`（只在 Linux 上有实际依赖）尤其关键。

#### 4.1.3 源码精读

先看 workspace 的成员清单——这就是仓库里全部 crate 的权威列表：

[Cargo.toml:L22-L65](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/Cargo.toml#L22-L65) —— `[workspace] members = [...]`。按目录前缀可以读出 Zenoh 的六大块：

- `commons/zenoh-*`（第 23–40 行）：内部地基，共 **18 个**，如 `zenoh-buffers`、`zenoh-codec`、`zenoh-config`、`zenoh-keyexpr`、`zenoh-protocol`、`zenoh-runtime`、`zenoh-shm`、`zenoh-stats`、`zenoh-sync`、`zenoh-task`、`zenoh-util` 等，以及**本次新增的 `zenoh-uring`（第 39 行）**。
- `examples`（第 41 行）：示例 crate（我们在《u1-l2》里见过）。
- `io/zenoh-link*`（第 42–53 行）：链路层，包含抽象 `zenoh-link`、`zenoh-link-commons`，以及各具体链路 `zenoh-link-tcp/udp/tls/quic/ws/...`。
- `io/zenoh-transport`（第 54 行）：传输层。
- `plugins/*`（第 55–60 行）：插件，如 `zenoh-plugin-rest`、`zenoh-plugin-storage-manager`、`zenoh-plugin-trait`、`zenoh-backend-traits`、示例插件。
- `zenoh`、`zenoh-ext`、`zenoh-ext/examples`、`zenohd`（第 61–64 行）：顶层的主实现、扩展库与路由器二进制。

> 注意第 15–21 行还有一个 `exclude = [...]`：它把一些**独立的子 workspace**（如 `ci/nostd-check`、`commons/zenoh-codec/fuzz`）排除在外，因为它们各自有自己的 `[workspace]`，不能并入主 workspace。

再看共享的包元信息：

[Cargo.toml:L82-L83](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/Cargo.toml#L82-L83) —— `rust-version = "1.75.0"` 与 `version = "1.9.0"`。这两行被所有子 crate 用 `{ workspace = true }` 继承，从而保证「全仓统一 Rust 最低版本 1.75、统一 crate 版本 1.9.0」。我们在《u1-l2》里提到的「最低 Rust 1.75」就来自这里。新增的 `zenoh-uring` 同样继承这两行。

最后是共享依赖区。这里有两类条目：外部依赖（`serde`、`tokio`、`clap` 等）和**自家内部 crate**。本次提交在这里新增了两个外部依赖：`atomic-queue`（[Cargo.toml:L99](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/Cargo.toml#L99)，供 uring 缓冲回收的无锁队列用）和 `io-uring`（[Cargo.toml:L126](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/Cargo.toml#L126)，Linux io_uring 的 Rust 绑定）。

[Cargo.toml:L229-L264](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/Cargo.toml#L229-L264) —— 自家 crate 的 workspace 依赖声明。关键点：每条都用 `path = "..."` 指向本地目录，且版本写成 `=1.9.0`（等号锁定）。例如：

- `zenoh = { version = "=1.9.0", path = "zenoh", default-features = false }`（第 229 行）
- `zenoh-codec = { version = "=1.9.0", path = "commons/zenoh-codec" }`（第 231 行）
- `zenoh-transport = { version = "=1.9.0", path = "io/zenoh-transport", default-features = false }`（第 261 行）
- **`zenoh-uring = { version = "=1.9.0", path = "commons/zenoh-uring" }`（第 262 行，本次新增）**

`default-features = false` 是一个值得记住的设计：很多自家 crate（尤其是被 `zenoh` 重新组合的那些）在 workspace 层就把默认特性关掉，让上层 crate（`zenoh` 自己）用 feature 重新「精选」要启用什么，避免「大家都开一套默认特性」造成编译臃肿。注意 `zenoh-uring` 本身**没有**写 `default-features = false`——它没有默认特性（只有一个可选的 `uring_trace` 调试特性），是否被编译完全由上层 `zenoh-transport` 的 `uring` feature 决定（见 4.3.6）。

#### 4.1.4 代码实践

**实践目标**：把 `members` 列表变成一张「分组表」，建立 crate 目录的肌肉记忆。

**操作步骤**：

1. 打开根 `Cargo.toml`，定位到 `members = [`（第 22 行）。
2. 把每一行按目录前缀归入 6 组：`commons/`、`io/`、`plugins/`、`examples`、顶层（`zenoh`/`zenoh-ext`）、`zenohd`。
3. 数一下各组分别有多少个成员。

**需要观察的现象**：你会发现 `commons/` 最多（地基最厚，**18 个**，含本次新增的 `zenoh-uring`），`io/zenoh-links/` 下每种链路一个 crate（tcp/udp/tls/quic/quic_datagram/serial/unixpipe/unixsock_stream/vsock/ws 共 10 种），`plugins/` 有 6 个。

**预期结果**：得到一张类似下面的分组计数表（请自己数后核对）：

| 分组 | 大致成员数 | 性质 |
|------|-----------|------|
| `commons/*` | 18 | 内部地基，不稳定（含新增 `zenoh-uring`） |
| `io/zenoh-link*` | 12 | 链路层，内部 |
| `io/zenoh-transport` | 1 | 传输层，内部 |
| `plugins/*` | 6 | 插件，内部（除通过 zenohd 间接使用外） |
| `examples` | 1 | 示例，不发布 |
| `zenoh` / `zenoh-ext`(+examples) | 3 | **对用户稳定** |
| `zenohd` | 1 | 路由器二进制 |

#### 4.1.5 小练习与答案

**练习 1**：为什么根 `Cargo.toml` 里有一段 `exclude = [...]`？如果不排除 `commons/zenoh-codec/fuzz` 会怎样？

**参考答案**：因为 `commons/zenoh-codec/fuzz` 本身是一个独立的 cargo workspace（fuzz target 通常自带 `[workspace]`）。一个目录不能同时属于两个 workspace，若不排除，cargo 会报「workspace 互相嵌套」的错误。`exclude` 把这些独立 workspace 从主 workspace 中摘出去，让它们单独编译。

**练习 2**：子 crate 的 `Cargo.toml` 里经常看到 `version = { workspace = true }`，这句话具体等价于什么？以新增的 `zenoh-uring` 为例。

**参考答案**：它等价于「继承根 `[workspace.package]` 里定义的 `version`（即 `1.9.0`）」。好处是改版本只需改根 `Cargo.toml` 一处，所有子 crate 同步更新，保证全仓版本一致。`commons/zenoh-uring/Cargo.toml` 第 24 行写的正是 `version = { workspace = true }`，因此它一加入 workspace 就自动锁到 `1.9.0`。

---

### 4.2 workspace 依赖与 crate 分层关系

#### 4.2.1 概念说明

有了成员列表，下一个问题是：**这些 crate 谁依赖谁？** 读每个 crate 的 `[dependencies]` 就能画出依赖箭头。Zenoh 的 crate 可以清楚地分成四层（从底到顶）：

```
┌─────────────────────────────────────────────────────────┐
│ 第 4 层（顶层产物）                                       │
│   zenohd   zenoh-ext   plugins/*   examples             │
├─────────────────────────────────────────────────────────┤
│ 第 3 层（公开 API / 主实现）                              │
│   zenoh  （Session + Runtime + 路由 + 门面）              │
├─────────────────────────────────────────────────────────┤
│ 第 2 层（IO 层）                                          │
│   zenoh-transport  →  zenoh-link  →  zenoh-link-{tcp,…}  │
│                       zenoh-link-commons                 │
├─────────────────────────────────────────────────────────┤
│ 第 1 层（commons 地基）                                   │
│   zenoh-protocol  zenoh-codec  zenoh-config  zenoh-keyexpr│
│   zenoh-buffers  zenoh-result  zenoh-core  zenoh-runtime │
│   zenoh-sync  zenoh-task  zenoh-shm  zenoh-stats         │
│   zenoh-uring（io_uring 接收路径，依赖 buffers/core/…）   │
└─────────────────────────────────────────────────────────┘
```

依赖方向是**严格自顶向下**：顶层依赖中层，中层依赖底层；底层绝不会反过来 `use` 顶层。这是 Rust 防止循环依赖的硬约束，也是我们定位「某个功能实现在哪一层」的指南针。

> 关于 `zenoh-uring` 的位置：它的依赖只有 `zenoh-buffers`、`zenoh-core`、`zenoh-result`、`zenoh-runtime` 等更底层的自家 crate（外加 `io-uring`、`libc`、`nix` 等外部库），所以它稳稳落在**第 1 层 commons 地基**里，与 `zenoh-shm`、`zenoh-sync` 同层。它**不**依赖任何 IO 层 crate，因此可以被 `zenoh-transport`（第 2 层）安全地向上引用（见 4.2.3）。

README 把「稳定边界」说得很清楚：

[README.md:L45-L48](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/README.md#L45-L48) —— commons 小节原话：「These crates are not intended to be imported directly, and their public APIs can be changed at any time. Stable APIs are provided by `zenoh` and `zenoh-ext` only.」也就是说：**只有 `zenoh` 和 `zenoh-ext` 对外稳定**；`commons/*`（含 `zenoh-uring`）、`io/*` 都是内部实现，随时会改。

#### 4.2.2 核心流程

要确认一条依赖箭头，只需打开「上层 crate」的 `Cargo.toml`，看它的 `[dependencies]` 里有没有「下层 crate」。例如要确认 `zenoh-transport → zenoh-uring`，就看 `io/zenoh-transport/Cargo.toml` 里有没有 `zenoh-uring`。

依赖链的形成遵循一个朴素的递归规则：

```
顶层 crate 编译 → 需要它依赖的中层 crate
中层 crate 编译 → 需要它依赖的底层 crate
底层 crate 编译 → 只需要外部库（serde、tokio、io-uring…）
```

因此，**底层 crate 必须最先编译完成**。这也解释了为什么底层 crate（如 `zenoh-buffers`、`zenoh-result`）几乎只依赖外部库——它们是整座大厦的地基，不能有循环风险。

#### 4.2.3 源码精读

我们自顶向下确认几个关键箭头（含本次新增的 `zenoh-uring`）。

**(a) `zenohd → zenoh`**：路由器二进制依赖主 crate。

[zenohd/Cargo.toml:L39-L44](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/zenohd/Cargo.toml#L39-L44) —— `zenohd` 的依赖里 `zenoh = { workspace = true, default-features = false, features = ["internal", "plugins", "runtime_plugins", "unstable"] }`。这说明 `zenohd` 几乎只是「`zenoh` 运行时 + 一个插件管理器 + 命令行参数解析」的薄壳（`zenohd/README.md` 原话：「`zenohd` is the Zenoh runtime with a plugin manager」）。注意它显式打开了 `internal`、`unstable` 等 feature——这些是普通应用默认关闭、但路由器必须打开的内部开关。

**(b) `zenoh → zenoh-transport`**：主实现依赖传输层。

[zenoh/Cargo.toml:L130](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/zenoh/Cargo.toml#L130) —— `zenoh-transport = { workspace = true }`。同一文件第 119 行还有 `zenoh-link = { workspace = true }`、第 120 行 `zenoh-link-commons`，说明 `zenoh` 直接持有传输层与链路层句柄。

**(c) `zenoh-transport → zenoh-link / zenoh-codec / zenoh-config / zenoh-uring`**：传输层依赖链路层、编解码、配置，以及（开启 uring 时）新增的 `zenoh-uring`。

[io/zenoh-transport/Cargo.toml:L84](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/Cargo.toml#L84) —— `zenoh-link = { workspace = true }`。同一依赖块（第 79–94 行）还列出 `zenoh-codec`（第 80 行）、`zenoh-config`（第 81 行）、`zenoh-protocol`（第 86 行）、`zenoh-runtime`（第 88 行）、`zenoh-task`（第 92 行），以及**本次新增的 `zenoh-uring = { workspace = true, optional = true }`（第 93 行）**。这正说明传输层是「把字节链路 + 编解码 + 配置 + 异步运行时组合起来」的中层；而 `zenoh-uring` 被声明为 `optional`，只有当启用 `uring` feature（见 4.3.6）时才会被拉进编译。

**(d) `zenoh-link → zenoh-link-commons / 具体链路`**：链路抽象层依赖各具体链路实现。

[io/zenoh-link/Cargo.toml:L31](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link/Cargo.toml#L31) —— `transport_tcp = ["zenoh-config/transport_tcp", "zenoh-link-tcp"]`。这里 `zenoh-link-tcp` 是一个 `optional` 依赖（见第 61 行 `zenoh-link-tcp = { workspace = true, optional = true }`），只有当启用 `transport_tcp` feature 时才被拉进来。`zenoh-link` 自己则固定依赖 `zenoh-link-commons`（第 57 行）和 `zenoh-protocol`（第 68 行）。

**(e) commons 地基**：最底层的协议/编解码/配置 crate，以及本次新增的 io_uring crate。

[commons/zenoh-protocol/Cargo.toml:L42-L50](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-protocol/Cargo.toml#L42-L50) —— `zenoh-protocol` 只依赖 `zenoh-buffers`、`zenoh-keyexpr`、`zenoh-macros`、`zenoh-result` 这些更底层的自家 crate（外加 `serde`、`uhlc`、`rand`）。注意它的 `description = "Internal crate for zenoh."`——明确标注自己是内部 crate。

[commons/zenoh-uring/Cargo.toml:L30-L42](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-uring/Cargo.toml#L30-L42) —— 本次新增的 `zenoh-uring`。两个要点：① 它的 `description = "Internal crate for zenoh."`（第 17 行），与其它 commons crate 一样是内部 crate；② 它的全部依赖被包在 `[target.'cfg(target_os = "linux")'.dependencies]` 里（第 30 行起），即**只在 Linux target 上才有实际依赖**（`io-uring`、`libc`、`nix`、`atomic-queue` 等），在非 Linux 平台上这个 crate 编译为「空壳」，从而保证跨平台可编译。它依赖的自家 crate 是 `zenoh-buffers`、`zenoh-core`、`zenoh-result`、`zenoh-runtime`（第 39–42 行），全部是更底层的地基——所以它属于第 1 层。

[commons/zenoh-codec/Cargo.toml:L44-L46](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-codec/Cargo.toml#L44-L46) —— `zenoh-codec` 依赖 `zenoh-buffers` 和 `zenoh-protocol`（外加可选的 `zenoh-shm`）。所以编解码层站在「缓冲区 + 协议模型」之上。

把这些箭头串起来，得到一条完整的自顶向下依赖链（含 uring 分支）：

```
zenohd ──► zenoh ──► zenoh-transport ──► zenoh-link ──► zenoh-link-tcp
                        │                     │
                        ├──► zenoh-codec ──► zenoh-protocol ──► zenoh-keyexpr
                        ├──► zenoh-config ──► zenoh-protocol
                        ├──► zenoh-uring ──► zenoh-buffers / zenoh-runtime …（仅 uring feature + Linux）
                        └──► zenoh-runtime / zenoh-task / zenoh-sync …
                                              （都落在 commons 地基上）
```

> 稳定性标注：这条链里**只有 `zenoh`（和它的伙伴 `zenoh-ext`）对用户稳定**；`zenoh-transport`、`zenoh-link`、`zenoh-codec`、`zenoh-protocol`、`zenoh-config`、`zenoh-keyexpr`、`zenoh-runtime`、`zenoh-uring` 等全部是 `commons`/`io` 内部 crate，写应用时不应直接依赖，但读源码理解原理时它们是主角（后续单元会逐层深入）。

#### 4.2.4 代码实践

**实践目标**：动手画出本讲的「综合依赖草图」，把 `zenoh-codec`、`zenoh-protocol`、`zenoh-keyexpr`、`zenoh-config`、`zenoh-link`、`zenoh-transport`、`zenohd`、`zenoh-uring` 之间的箭头标出来，并标注哪些是 commons 内部 crate、哪些对外稳定。

**操作步骤**：

1. 打开下列 8 个 `Cargo.toml`，分别看它们的 `[dependencies]`：
   - `zenohd/Cargo.toml`（看是否依赖 `zenoh`）
   - `zenoh/Cargo.toml`（看是否依赖 `zenoh-transport`、`zenoh-link`）
   - `io/zenoh-transport/Cargo.toml`（看是否依赖 `zenoh-link`、`zenoh-codec`、`zenoh-config`、`zenoh-uring`）
   - `io/zenoh-link/Cargo.toml`（看是否依赖 `zenoh-link-commons`、`zenoh-protocol`）
   - `commons/zenoh-codec/Cargo.toml`（看是否依赖 `zenoh-protocol`、`zenoh-buffers`）
   - `commons/zenoh-config/Cargo.toml`（看是否依赖 `zenoh-protocol`、`zenoh-keyexpr`）
   - `commons/zenoh-protocol/Cargo.toml`（看是否依赖 `zenoh-keyexpr`、`zenoh-buffers`）
   - `commons/zenoh-uring/Cargo.toml`（看是否依赖 `zenoh-buffers`、`zenoh-runtime` 等地基）
2. 用文字或简单符号（`A ──► B` 表示 A 依赖 B）画出依赖图。
3. 给每个 crate 标注：**对外稳定**（`zenoh`、`zenoh-ext`）还是 **commons 内部**（其余几个，含 `zenoh-uring`）。

**需要观察的现象**：你应该能确认以下结论——`zenohd` 依赖 `zenoh`；`zenoh` 依赖 `zenoh-transport`/`zenoh-link`；`zenoh-transport` 依赖 `zenoh-link`/`zenoh-codec`/`zenoh-config`，并在开启 uring 时额外依赖 `zenoh-uring`；`zenoh-link` 依赖 `zenoh-link-commons`；`zenoh-codec` 与 `zenoh-config` 都依赖 `zenoh-protocol`；`zenoh-protocol` 依赖 `zenoh-keyexpr`；`zenoh-uring` 只依赖地基层的 `zenoh-buffers`/`zenoh-runtime` 等。

**预期结果**：得到一张与本节 4.2.3 末尾相同的依赖草图，并且明确「只有 `zenoh`/`zenoh-ext` 稳定，其余（含 `zenoh-uring`）都是 commons/io 内部 crate」。这是后续内部架构单元反复要用到的地图。

#### 4.2.5 小练习与答案

**练习 1**：如果有人想在 `zenoh-protocol`（底层）里调用 `zenoh-transport`（中层）的功能，为什么不可能？

**参考答案**：因为依赖只能「从上向下」，`zenoh-transport` 已经依赖 `zenoh-protocol`；若 `zenoh-protocol` 反过来依赖 `zenoh-transport`，就形成循环依赖，cargo 会直接报错拒绝编译。这也倒逼 Zenoh 把「通用、抽象」的东西（协议消息结构、key expression）放在底层，把「具体、组合」的东西（传输实现）放在上层。

**练习 2**：`zenoh-config` 同时出现在 `io/zenoh-transport` 和 `io/zenoh-link` 的依赖里。这说明配置 crate 处于什么位置？`zenoh-uring` 为什么不能反过来依赖 `zenoh-transport`？

**参考答案**：`zenoh-config` 属于 commons 地基层，被多个 IO 层 crate 共享。它依赖 `zenoh-protocol`、`zenoh-keyexpr` 等更底层 crate，但不依赖任何 IO 层 crate，所以可以被 `zenoh-link`、`zenoh-transport`、`zenoh` 等上层安全地复用，作为「全仓统一的配置模型」。`zenoh-uring` 同理——它位于地基层，依赖 `zenoh-buffers`/`zenoh-runtime` 等；而 `zenoh-transport`（第 2 层）反过来依赖 `zenoh-uring`（第 1 层）。若 `zenoh-uring` 反向依赖 `zenoh-transport`，就会形成 `transport ─► uring ─► transport` 的循环，cargo 会拒绝编译。

---

### 4.3 Feature 开关：按需启用传输与特性

#### 4.3.1 概念说明

Zenoh 支持很多种链路（tcp/udp/tls/quic/ws/serial/…）、很多可选能力（共享内存、统计、压缩、多种认证，以及本次新增的 Linux io_uring 接收路径）。如果全部默认编译进来，二进制会很大、编译很慢，还会引入不必要的系统依赖。所以 Zenoh 用 **feature** 把它们做成「按需启用」的开关。

核心思路是**层层转发**：用户在自己的 `Cargo.toml` 里只面对最顶层的 `zenoh` crate 的 feature（例如 `transport_tcp`、`shared-memory`、`uring`），而 `zenoh` 会把这个 feature 转发给 `zenoh-transport`，`zenoh-transport` 再转发给 `zenoh-link`，`zenoh-link` 再拉进具体的 `zenoh-link-tcp`。最终只有真正需要的链路 crate 被编译。

这样做的好处：

- 用户只记一套 feature 名字（在 `zenoh` 上），不用知道下层 crate 结构。
- 不需要的链路/能力（比如嵌入式设备不需要 TLS、非 Linux 不需要 io_uring）完全不参与编译。

#### 4.3.2 核心流程

以「启用 TCP 传输」为例，feature 的转发链是：

```
用户 Cargo.toml:  features = ["transport_tcp"]   （作用在 zenoh 上）
        │
        ▼
zenoh/Cargo.toml:        transport_tcp = ["zenoh-config/transport_tcp",
                                           "zenoh-transport/transport_tcp"]
        │
        ▼
zenoh-transport/Cargo.toml: transport_tcp = ["zenoh-config/transport_tcp",
                                              "zenoh-link/transport_tcp"]
        │
        ▼
zenoh-link/Cargo.toml:    transport_tcp = ["zenoh-config/transport_tcp",
                                            "zenoh-link-tcp"]   ← 拉进具体实现 crate
```

可以看到 feature 名字 `transport_tcp` 在三层都叫同一个名字，但每层的「转发目标」不同。最底层 `zenoh-link` 终于把它落到了一个真实依赖 `zenoh-link-tcp` 上，这个 crate 才是 TCP 链路的真正实现。

类似地，`shared-memory`、`unstable`、`stats`、`auth_usrpwd`、`auth_pubkey`、`transport_compression`、以及本次新增的 `uring`，都是按同样的「层层转发」机制工作（4.3.6 会专门拆解 `uring`）。

#### 4.3.3 源码精读

**(a) `zenoh` 的 feature 定义——用户面对的入口**：

[zenoh/Cargo.toml:L34-L46](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/zenoh/Cargo.toml#L34-L46) —— `zenoh` 的 `default` feature 列表。默认会启用一长串 `transport_*`、`auth_*`、`transport_compression`、`transport_multilink`。这意味着默认情况下，普通用户开箱即用就支持 tcp/udp/tls/quic/ws/unixsock 等主流链路。注意：`uring` **不在** default 里——io_uring 接收路径需要用户显式开启（Linux 专属优化）。

[zenoh/Cargo.toml:L72](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/zenoh/Cargo.toml#L72) —— `transport_tcp = ["zenoh-config/transport_tcp", "zenoh-transport/transport_tcp"]`。这是「转发」的关键一行：启用 `transport_tcp` 时，同时把 `transport_tcp` 这个 feature 打开到下游 `zenoh-config` 和 `zenoh-transport`。

[zenoh/Cargo.toml:L55-L60](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/zenoh/Cargo.toml#L55-L60) —— `shared-memory = [...]`。它一次性把共享内存 feature 下发到 `zenoh-buffers`、`zenoh-protocol`、`zenoh-shm`、`zenoh-transport` 四个 crate（注意 `zenoh-shm` 本身在 `zenoh` 里是 `optional` 依赖，第 126 行，只有开 feature 才会拉进来）。

[zenoh/Cargo.toml:L79-L84](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/zenoh/Cargo.toml#L79-L84) —— `unstable = [...]`。`unstable` 是「不稳定 API」的总开关，它会打开 `zenoh-config`、`zenoh-keyexpr`、`zenoh-protocol`、`zenoh-transport` 各自的 `unstable` feature。我们在《u1-l1》《u1-l2》里提到的「不稳定 API 受 feature 门控」就来自这里。

[zenoh/Cargo.toml:L47-L51](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/zenoh/Cargo.toml#L47-L51) —— `internal = [...]`。`internal` 是更深的「内部 API」开关，普通应用几乎不用；但路由器 `zenohd` 必须打开它（见下面）。

**(b) `zenoh-transport` 的转发——中转站**：

[io/zenoh-transport/Cargo.toml:L46](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/Cargo.toml#L46) —— `transport_tcp = ["zenoh-config/transport_tcp", "zenoh-link/transport_tcp"]`。可以看到同样的模式：再往下转发给 `zenoh-config` 和 `zenoh-link`。

**(c) `zenoh-link` 的落地——拉进具体实现**：

[io/zenoh-link/Cargo.toml:L31](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link/Cargo.toml#L31) —— `transport_tcp = ["zenoh-config/transport_tcp", "zenoh-link-tcp"]`。这一层终于把 `zenoh-link-tcp` 这个真实的可选依赖激活（它在 `zenoh-link/Cargo.toml` 第 61 行被声明为 `optional = true`）。

**(d) `zenohd` 反过来强制打开内部 feature**：

[zenohd/Cargo.toml:L29](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/zenohd/Cargo.toml#L29) —— `default = ["zenoh/default"]`。`zenohd` 的默认 feature 直接继承 `zenoh` 的默认 feature，确保路由器自带所有主流链路。再结合第 39–44 行的 `features = ["internal", "plugins", "runtime_plugins", "unstable"]`，路由器额外打开了插件与内部 API 能力。

> 一句话总结：feature 在 `zenoh` 这一层是「面向用户的开关」，在 `zenoh-transport`/`zenoh-link` 这几层是「转发管道」，最终在 `zenoh-link-*` 这一层变成「真实的可选依赖」。这套设计让用户只面对一套名字，却能在编译期精确控制要包含哪些底层实现。

#### 4.3.4 代码实践

**实践目标**：亲手跟踪一次 feature 转发链，验证「层层下发」机制。

**操作步骤**：

1. 打开 `zenoh/Cargo.toml` 第 72 行，确认 `transport_tcp` 转发给 `zenoh-config` 和 `zenoh-transport`。
2. 跟着跳到 `io/zenoh-transport/Cargo.toml` 第 46 行，确认它又转发给 `zenoh-config` 和 `zenoh-link`。
3. 再跳到 `io/zenoh-link/Cargo.toml` 第 31 行，确认它最终激活了 `zenoh-link-tcp`（一个 `optional` 依赖，见同文件第 61 行）。
4. 试着换一条链路重复这个过程：跟踪 `transport_ws`（websocket）从 `zenoh` → `zenoh-transport` → `zenoh-link` → `zenoh-link-ws`。
5. （可选，待本地验证）用一个最小项目 `cargo add zenoh --no-default-features --features transport_tcp`，然后 `cargo build`，观察 `target/` 下是否只编译了 `zenoh-link-tcp` 而**没有**编译 `zenoh-link-ws`、`zenoh-link-udp` 等其他链路 crate。

**需要观察的现象**：在步骤 5 中，关闭默认特性后只开 `transport_tcp`，应该只有 TCP 相关的链路 crate 参与编译，其它链路被排除，二进制更小、编译更快。

**预期结果**：你将直观看到 feature 系统的「按需编译」效果，并能用一句话解释「为什么用户只在 `zenoh` 上设 feature，底层却能正确响应」。

**说明**：步骤 5 的具体编译输出依赖本地环境与 Rust 工具链版本，若无法稳定复现，记为「待本地验证」即可；前三步的源码跟踪是确定的。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `zenoh` 的 `transport_tcp` feature 要同时转发给 `zenoh-config/transport_tcp` 和 `zenoh-transport/transport_tcp`，而不是只转发给传输层？

**参考答案**：因为「TCP」不仅影响传输层实现，还影响**配置**——`zenoh-config` 需要知道哪些 locator 协议（`tcp/`、`udp/`…）是合法的、能被解析的。把 `transport_tcp` 也下发到 `zenoh-config`，是为了让配置层在编译期就根据是否启用 TCP 来决定是否接受 `tcp/` 端点。这是「同一个 feature 名字、跨多个 crate 协同生效」的典型用法。

**练习 2**：`shared-memory` feature 在 `zenoh/Cargo.toml` 里同时激活了 `zenoh-shm`（一个 `optional` 依赖）和多个下游 crate 的 `shared-memory` feature。请说明这两类动作的区别。

**参考答案**：激活 `zenoh-shm` 是「**引入一个原本不编译的可选 crate**」（它依赖 `optional = true`）；而 `zenoh-protocol/shared-memory` 等是「**在已经编译的 crate 内部打开一组代码路径**」（用 `#[cfg(feature = "shared-memory")]` 门控的代码）。前者改变「编译哪些 crate」，后者改变「某个 crate 编译进哪些代码」。共享内存需要两者配合：既要 SHM 缓冲区 crate，又要协议/传输层识别 SHM 类型的 payload。

**练习 3**：`zenohd` 为什么要显式打开 `unstable` 和 `internal` 这两个 feature，而普通应用默认不开？

**参考答案**：路由器作为基础设施，需要使用大量「不稳定 / 内部」API（如直接操控 Runtime、插件加载器、admin space），这些 API 不对普通应用承诺稳定。普通应用不开这些 feature，可以避免误用未来会变的内部接口；而 `zenohd` 与 `zenoh` 同版本同步发布，可以安全地使用内部 API。

#### 4.3.6 最小模块：`uring` feature 的完整转发链（本次新增）

##### 概念说明

`5dd1a2f7`（"Support io_uring"）这一提交新增了一个贯穿三层的 `uring` feature，用来在 Linux 上启用基于 io_uring 的零拷贝接收路径。它和 `transport_tcp` 一样走「层层转发」，但有一个重要区别：**它在中间层 `zenoh-transport` 不仅转发 feature，还顺手拉进了一个新的 `optional` 依赖 `zenoh-uring`**——这就是 4.2.3 里看到的 `zenoh-uring = { workspace = true, optional = true }`。换句话说，`uring` feature 同时做了 4.3.5 练习 2 里说的两类动作：打开下游代码路径 + 引入一个可选 crate。

##### 核心流程

`uring` feature 的转发链（自顶向下）：

```
zenoh/Cargo.toml:           uring = ["zenoh-transport/uring"]
                                  │
                                  ▼
zenoh-transport/Cargo.toml: uring = ["zenoh-link/uring", "zenoh-uring"]   ← ① 转发 ② 拉进 optional 依赖
                                  │
                                  ▼
zenoh-link/Cargo.toml:      uring = ["zenoh-link-commons/uring",           ← 抽象层打标记
                                     "zenoh-link-{tcp,udp,tls,quic,…}/uring",  ← 每条具体链路都打 uring 标记
                                     ... 共 10 条具体链路]
                                  │
                                  ▼
zenoh-link-commons/Cargo.toml: uring = []    ← 最底层只是个「空标记」feature，靠 #[cfg(feature="uring")] 门控代码
```

注意「层层聚合」的形态：`zenoh-link` 这一层的 `uring` 不是只转发给一个 crate，而是**一次性把 `zenoh-link-commons` 和全部 10 条具体链路的 `uring` 标记都点亮**——因为 io_uring 是一条「跨所有链路」的接收路径优化，需要每条链路都参与（提供 fd、适配缓冲）。这与 `transport_tcp`（只点亮一条链路）的形态不同。

##### 源码精读

**(a) `zenoh` 顶层入口**：

[zenoh/Cargo.toml:L85](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/zenoh/Cargo.toml#L85) —— `uring = ["zenoh-transport/uring"]`。用户只需在 `zenoh` 上开 `uring`，剩下全靠转发。注意它**只**转发给 `zenoh-transport`，不像 `transport_tcp` 那样同时转发给 `zenoh-config`——因为 io_uring 是传输/链路层的接收路径优化，不涉及 locator 协议是否合法的配置问题。

**(b) `zenoh-transport` 中转：转发 + 拉进可选 crate**：

[io/zenoh-transport/Cargo.toml:L54](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/Cargo.toml#L54) —— `uring = ["zenoh-link/uring", "zenoh-uring"]`。这一行干了两件事：① 把 `uring` 继续下发给 `zenoh-link`；② 激活同名可选依赖 `zenoh-uring`（它在第 93 行声明为 `optional = true`）。后者正是「把 `zenoh-uring` 这个 commons crate 拉进编译」的开关——不开 `uring` 时 `zenoh-uring` 根本不参与编译。

**(c) `zenoh-link` 聚合所有链路**：

[io/zenoh-link/Cargo.toml:L41-L53](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link/Cargo.toml#L41-L53) —— `uring = [...]` 一次性聚合了 `zenoh-link-commons/uring`（第 42 行，抽象层）以及 `zenoh-link-quic/uring`、`zenoh-link-quic_datagram/uring`、`zenoh-link-serial/uring`、`zenoh-link-tcp/uring`、`zenoh-link-tls/uring`、`zenoh-link-udp/uring`、`zenoh-link-unixpipe/uring`、`zenoh-link-unixsock_stream/uring`、`zenoh-link-vsock/uring`、`zenoh-link-ws/uring`（第 43–52 行，全部 10 条具体链路）。这把「io_uring 能力」广播到了每一条链路 crate。

**(d) `zenoh-link-commons` 最底层：空标记 feature**：

[io/zenoh-link-commons/Cargo.toml:L45](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link-commons/Cargo.toml#L45) —— `uring = []`。这是一个**空 feature**：它不引入任何依赖，只作为一个「编译期标记」，供代码里 `#[cfg(feature = "uring")]` 判断「是否要为 io_uring 准备 get_fd 等能力」。这是 feature 系统的第三种用法（前两种是「引入可选依赖」「转发到下游」）：**纯粹做条件编译开关**。

> 一句话总结 `uring` 与 `transport_tcp` 的差异：`transport_tcp` 是「点亮一条链路」，`uring` 是「给所有链路 + 一个新 commons crate 统一打上 io_uring 标记」。至于运行时哪些链路真的会走 io_uring 接收路径、哪些会回退到 tokio（例如未连接 UDP、TLS），属于传输层运行时行为，留到《u9-l5 io_uring 接收路径》详讲。

##### 代码实践

**实践目标**：对照源码，说出 `uring` feature 在 `zenoh-link` 与 `zenoh-transport` 中分别聚合/转发了哪些子特性，并解释为什么 `zenoh-uring` 在 `zenoh-transport` 里是 `optional` 依赖。

**操作步骤**：

1. 打开 `io/zenoh-link/Cargo.toml` 第 41–53 行，数一下 `uring` 聚合了多少个下游 feature（应是 1 个抽象层 `zenoh-link-commons/uring` + 10 个具体链路 `*/uring`）。
2. 打开 `io/zenoh-transport/Cargo.toml` 第 54 行，确认 `uring` 转发给 `zenoh-link/uring` 并激活 `zenoh-uring`；再看第 93 行确认 `zenoh-uring` 是 `optional = true`。
3. 打开 `io/zenoh-link-commons/Cargo.toml` 第 45 行，确认 `uring = []` 是空标记。
4. （可选，待本地验证）在 Linux 上用 `cargo build -p zenoh --features uring` 编译，再用 `cargo tree -p zenoh -e features -i zenoh-uring`（或 `cargo tree -p zenoh-transport -i zenoh-uring`）观察 `zenoh-uring` 是否被拉进依赖图；不带 `--features uring` 时它应缺席。

**需要观察的现象**：步骤 4 中，开启 `uring` 后 `zenoh-uring` 出现在依赖树里；不开启时它完全不在编译图中——这印证了「`optional` 依赖 + feature 激活」的按需编译效果。

**预期结果**：你能用一句话回答——「`zenoh-link` 的 `uring` 聚合了 `zenoh-link-commons/uring` 加全部 10 条具体链路的 `*/uring`；`zenoh-transport` 的 `uring` 则转发给 `zenoh-link/uring` 并把 `optional` 的 `zenoh-uring` 拉进编译」。`zenoh-uring` 设为 `optional`，是为了让非 Linux、或不需要 io_uring 的用户完全不为此付出编译/二进制代价。

**说明**：步骤 4 依赖 Linux 环境与 `cargo tree` 子命令版本，若本地不可用记为「待本地验证」；前 3 步的源码阅读是确定的。

##### 小练习与答案

**练习 1**：为什么 `zenoh` 顶层的 `uring = ["zenoh-transport/uring"]` 不像 `transport_tcp` 那样同时转发给 `zenoh-config/uring`？

**参考答案**：因为 io_uring 是传输/链路层的**接收路径**优化，它改变的是「收到字节后用什么异步模型处理」，而不是「哪些 locator 协议名合法」。后者属于配置层（`zenoh-config`）的职责，TCP/UDP 等才需要让配置层知道；而 io_uring 对 locator 协议串（`tcp/`、`udp/`…）完全透明，所以不需要下发到 `zenoh-config`。

**练习 2**：`zenoh-link-commons` 的 `uring = []` 是一个空 feature。既然它什么都不做，为什么还要声明？

**参考答案**：它是一个**编译期标记**。代码里会用 `#[cfg(feature = "uring")]` 来门控「为 io_uring 准备的能力」（例如让 unicast 链路暴露文件描述符 `get_fd`、准备适配 uring 缓冲的代码路径）。声明这个空 feature，就是给 `#[cfg]` 提供一个可判断的开关；cargo 的 feature 解析也要求「被引用的 feature 必须在某处声明」，所以即使为空也必须显式写出。

**练习 3**：如果用户在非 Linux 平台（例如 macOS）上开启 `zenoh` 的 `uring` feature，会发生什么？

**参考答案**：feature 仍然会被解析、`zenoh-uring` 仍会被拉进编译图，但由于 `zenoh-uring` 的实际依赖都被 `[target.'cfg(target_os = "linux")'.dependencies]` 门控（见 4.2.3 的 `commons/zenoh-uring/Cargo.toml`），在非 Linux 上 `io-uring`/`libc`/`nix` 等不会被拉入，`zenoh-uring` 实际编译为一个「空壳」。运行时传输层会检测到 io_uring 不可用而回退到 tokio 接收路径（具体回退逻辑见《u9-l5》）。这正是「编译期 feature + target 门控 + 运行时回退」三层配合，保证跨平台可编译且不崩溃。

---

## 5. 综合实践

把本讲三个模块串起来，完成一份「**Zenoh crate 全景图**」文档（纯文字即可）：

1. **成员清单**：从根 `Cargo.toml` 的 `members` 抄出全部 crate，按 `commons/`、`io/`、`plugins/`、顶层分组，并标出本次新增的 `commons/zenoh-uring`。
2. **依赖草图**：至少画出下面这些箭头，并保证方向是「上层 → 下层」：
   - `zenohd ──► zenoh`
   - `zenoh ──► zenoh-transport ──► zenoh-link ──► zenoh-link-tcp`
   - `zenoh-transport ──► zenoh-codec ──► zenoh-protocol ──► zenoh-keyexpr`
   - `zenoh-transport ──► zenoh-config ──► zenoh-protocol`
   - `zenoh-transport ──► zenoh-uring ──► zenoh-buffers / zenoh-runtime`（仅 `uring` feature + Linux）
   - `zenoh ──► zenoh-runtime / zenoh-task / zenoh-sync`（commons 地基）
3. **稳定性标注**：用两种颜色/符号区分「对外稳定（`zenoh`、`zenoh-ext`）」与「commons/io 内部 crate（其余，含 `zenoh-uring`）」。
4. **feature 转发链**：任选一条并写出完整路径（参考 4.3.4 的步骤）：
   - 经典链路：`transport_tcp` 或 `shared-memory`，从 `zenoh` 一路下发到最底层具体 crate/可选依赖；
   - 本次新增链路：`uring`，说明它在 `zenoh-link` 聚合了哪些子特性、在 `zenoh-transport` 又激活了哪个 `optional` 依赖（参考 4.3.6）。

完成后，你应当拥有两张图：一张「依赖层次图」、一张「feature 转发图」。这两张图是后续所有内部架构讲义（u7–u12）的导航地图——以后每讲到一个子系统，你都能在这张图上找到它所在的层。例如讲到《u9-l5 io_uring 接收路径》时，你会立刻知道：它的核心 crate `zenoh-uring` 在 commons 第 1 层，由 `zenoh-transport` 的 `uring` feature 拉起，并通过 `zenoh-link` 的 `uring` 把能力广播到每条链路。

## 6. 本讲小结

- Zenoh 是一个标准的 cargo **workspace**，根 `Cargo.toml` 用 `members` 列出全部约 42 个子 crate，并用 `[workspace.package]`/`[workspace.dependencies]` 统一管理版本（`1.9.0`）与 Rust 最低版本（`1.75.0`）。
- 仓库分六大块：`commons/*`（内部地基，**18 个**，含本次新增的 `zenoh-uring`）、`io/*`（链路层 + 传输层）、`plugins/*`（插件）、`zenoh`（主实现 + 公开 API）、`zenoh-ext`（扩展库）、`zenohd`（路由器二进制），外加 `examples`。
- crate 严格**分层**：`zenohd/plugins/examples`（顶层）→ `zenoh`（API/主实现）→ `zenoh-transport`/`zenoh-link`（IO 层）→ `zenoh-protocol`/`zenoh-codec`/`zenoh-config`/`zenoh-keyexpr`/`zenoh-buffers`/`zenoh-uring`（commons 地基）。依赖只能从上向下，杜绝循环。
- **稳定边界**：只有 `zenoh` 与 `zenoh-ext` 对外提供稳定 API；所有 `commons/*`（含 `zenoh-uring`）、`io/*` 都是内部 crate（多数 `description` 写着 `"Internal crate for zenoh."`），写应用不应直接依赖。
- **feature 系统**采用层层转发：用户只面对 `zenoh` 上的 feature（如 `transport_tcp`、`shared-memory`、`unstable`、本次新增的 `uring`），它沿 `zenoh → zenoh-transport → zenoh-link → zenoh-link-*` 下发，最终落到真实的可选依赖上，实现「按需编译」。
- **`uring` feature（本次新增）**：`zenoh` 顶层只转发给 `zenoh-transport/uring`；`zenoh-transport` 的 `uring` 再转发给 `zenoh-link/uring` 并把 `optional` 的 `zenoh-uring` 拉进编译；`zenoh-link` 的 `uring` 则一次性聚合 `zenoh-link-commons/uring` 加全部 10 条具体链路的 `*/uring`，把 io_uring 能力广播到每条链路。`zenoh-uring` 的实际依赖被 `target_os = "linux"` 门控，非 Linux 平台编译为空壳。
- `zenohd` 作为路由器，在依赖 `zenoh` 时显式打开 `internal`、`plugins`、`runtime_plugins`、`unstable` 等 feature，以使用普通应用默认关闭的内部能力。

## 7. 下一步学习建议

本讲建立的是「**横向地图**」（谁和谁并列、谁依赖谁）。接下来建议：

- 进入**第 2 单元**，从「**纵向使用**」角度学习：先看《u2-l1 打开一个 Session》，理解 `zenoh::open` 如何把这张图里的 `zenoh-runtime`/`zenoh-transport` 等内部 crate 组装成一个用户手中的 `Session`。
- 如果你想更早接触公开 API 的内部模块布局，可以先读《u1-l4 公开 API 地图：lib.rs 模块导览》，它承接本讲的「`zenoh` 是门面」结论，展开门面后面 re-export 了哪些类型。
- 当后续进入**第 7–10 单元（内部架构）**时，请随时回到本讲的依赖草图定位文件——例如讲传输层就盯 `io/zenoh-transport`，讲协议编解码就盯 `commons/zenoh-codec`，讲路由就回到 `zenoh` crate 内部的 `net/routing` 模块。
- 对 io_uring 感兴趣的读者，可以直接跳到《u9-l5 io_uring 接收路径：零拷贝异步读》，它会基于本讲建立的「`zenoh-uring` 在 commons 第 1 层、由 `uring` feature 拉起」的认知，深入讲解 `Reader`/arena/window、`rx_task` 在 uring 与 tokio 之间的分派与回退。
