# Workspace 与 crate 布局总览

## 1. 本讲目标

在《u1-l1》里我们建立了「Zenoh 是什么、仓库有哪六大块」的直觉，在《u1-l2》里我们用 `cargo run --example` 把示例跑了起来。本讲要回答的是一个更结构化的问题：**这些目录里的几十个 crate 到底谁依赖谁、谁对用户稳定、谁只在内部使用？**

读完本讲，你应当能够：

- 看懂根 `Cargo.toml` 的 `workspace.members` 列表，并说出每个 crate 的职责。
- 画出 `io（link/transport）→ zenoh（api/net）→ zenohd/plugins` 的依赖方向，理解为什么是「从底向上」堆叠。
- 区分「对用户稳定的 crate（`zenoh`、`zenoh-ext`）」与「内部实现 crate（`commons/*`、`io/*`）」。
- 理解 `transport_tcp`、`transport_quic`、`shared-memory`、`unstable` 这类 **feature 开关** 如何沿依赖链层层下传、按需启用。

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

这种「层层转发」是理解 Zenoh feature 系统的关键，第 4.3 节会专门讲。

## 3. 本讲源码地图

本讲主要阅读配置类文件，它们描述了「结构」而非「行为」：

| 文件 | 作用 |
|------|------|
| `Cargo.toml`（根） | workspace 的总入口：列出所有成员、共享版本、共享依赖。 |
| `README.md` | 用人话描述仓库六大块（zenoh / zenoh-ext / zenohd / plugins / commons / examples）的职责与稳定边界。 |
| `zenohd/README.md` | `zenohd` 路由器二进制的命令行参数、插件加载方式，以及它和 `zenoh` crate 的关系。 |
| `zenoh/Cargo.toml` | 主 crate 的 feature 定义与依赖列表，是「feature 转发」的中心枢纽。 |
| `zenohd/Cargo.toml`、`io/zenoh-transport/Cargo.toml`、`io/zenoh-link/Cargo.toml`、若干 `commons/*/Cargo.toml` | 用来确认依赖箭头的方向（谁依赖谁）。 |

## 4. 核心概念与源码讲解

### 4.1 workspace 配置：members、package 与 resolver

#### 4.1.1 概念说明

根 `Cargo.toml` 是整个 Zenoh 仓库的「总目录页」。它做三件事：

1. **声明 workspace**：用 `members = [...]` 列出所有参与编译的子 crate 目录。
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

`resolver = "2"` 对 Zenoh 这种「feature 多、可选依赖多」的项目尤其重要：旧解析器可能把不同 target 的 feature 合并，新解析器按 target 分别处理。

#### 4.1.3 源码精读

先看 workspace 的成员清单——这就是仓库里全部 crate 的权威列表：

[Cargo.toml:L22-L64](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/Cargo.toml#L22-L64) —— `[workspace] members = [...]`。按目录前缀可以读出 Zenoh 的六大块：

- `commons/zenoh-*`（第 23–39 行）：内部地基，如 `zenoh-buffers`、`zenoh-codec`、`zenoh-config`、`zenoh-keyexpr`、`zenoh-protocol`、`zenoh-runtime`、`zenoh-shm`、`zenoh-stats`、`zenoh-sync`、`zenoh-task`、`zenoh-util` 等。
- `examples`（第 40 行）：示例 crate（我们在《u1-l2》里见过）。
- `io/zenoh-link*`（第 41–52 行）：链路层，包含抽象 `zenoh-link`、`zenoh-link-commons`，以及各具体链路 `zenoh-link-tcp/udp/tls/quic/ws/...`。
- `io/zenoh-transport`（第 53 行）：传输层。
- `plugins/*`（第 54–59 行）：插件，如 `zenoh-plugin-rest`、`zenoh-plugin-storage-manager`、`zenoh-plugin-trait`、`zenoh-backend-traits`、示例插件。
- `zenoh`、`zenoh-ext`、`zenoh-ext/examples`、`zenohd`（第 60–63 行）：顶层的主实现、扩展库与路由器二进制。

> 注意第 15–21 行还有一个 `exclude = [...]`：它把一些**独立的子 workspace**（如 `ci/nostd-check`、`commons/zenoh-codec/fuzz`）排除在外，因为它们各自有自己的 `[workspace]`，不能并入主 workspace。

再看共享的包元信息：

[Cargo.toml:L81-L82](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/Cargo.toml#L81-L82) —— `rust-version = "1.75.0"` 与 `version = "1.9.0"`。这两行被所有子 crate 用 `{ workspace = true }` 继承，从而保证「全仓统一 Rust 最低版本 1.75、统一 crate 版本 1.9.0」。我们在《u1-l2》里提到的「最低 Rust 1.75」就来自这里。

最后是共享依赖区。这里有两类条目：外部依赖（`serde`、`tokio`、`clap` 等）和**自家内部 crate**：

[Cargo.toml:L226-L260](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/Cargo.toml#L226-L260) —— 自家 crate 的 workspace 依赖声明。关键点：每条都用 `path = "..."` 指向本地目录，且版本写成 `=1.9.0`（等号锁定）。例如：

- `zenoh = { version = "=1.9.0", path = "zenoh", default-features = false }`（第 226 行）
- `zenoh-codec = { version = "=1.9.0", path = "commons/zenoh-codec" }`（第 228 行）
- `zenoh-transport = { version = "=1.9.0", path = "io/zenoh-transport", default-features = false }`（第 258 行）

`default-features = false` 是一个值得记住的设计：很多自家 crate（尤其是被 `zenoh` 重新组合的那些）在 workspace 层就把默认特性关掉，让上层 crate（`zenoh` 自己）用 feature 重新「精选」要启用什么，避免「大家都开一套默认特性」造成编译臃肿。

#### 4.1.4 代码实践

**实践目标**：把 `members` 列表变成一张「分组表」，建立 crate 目录的肌肉记忆。

**操作步骤**：

1. 打开根 `Cargo.toml`，定位到 `members = [`（第 22 行）。
2. 把每一行按目录前缀归入 6 组：`commons/`、`io/`、`plugins/`、`examples`、顶层（`zenoh`/`zenoh-ext`）、`zenohd`。
3. 数一下各组分别有多少个成员。

**需要观察的现象**：你会发现 `commons/` 最多（地基最厚），`io/zenoh-links/` 下每种链路一个 crate（tcp/udp/tls/quic/quic_datagram/serial/unixpipe/unixsock_stream/vsock/ws 共 10 种），`plugins/` 有 6 个。

**预期结果**：得到一张类似下面的分组计数表（请自己数后核对）：

| 分组 | 大致成员数 | 性质 |
|------|-----------|------|
| `commons/*` | 约 17 | 内部地基，不稳定 |
| `io/zenoh-link*` | 12 | 链路层，内部 |
| `io/zenoh-transport` | 1 | 传输层，内部 |
| `plugins/*` | 6 | 插件，内部（除通过 zenohd 间接使用外） |
| `examples` | 1 | 示例，不发布 |
| `zenoh` / `zenoh-ext`(+examples) | 3 | **对用户稳定** |
| `zenohd` | 1 | 路由器二进制 |

#### 4.1.5 小练习与答案

**练习 1**：为什么根 `Cargo.toml` 里有一段 `exclude = [...]`？如果不排除 `commons/zenoh-codec/fuzz` 会怎样？

**参考答案**：因为 `commons/zenoh-codec/fuzz` 本身是一个独立的 cargo workspace（fuzz target 通常自带 `[workspace]`）。一个目录不能同时属于两个 workspace，若不排除，cargo 会报「workspace 互相嵌套」的错误。`exclude` 把这些独立 workspace 从主 workspace 中摘出去，让它们单独编译。

**练习 2**：子 crate 的 `Cargo.toml` 里经常看到 `version = { workspace = true }`，这句话具体等价于什么？

**参考答案**：它等价于「继承根 `[workspace.package]` 里定义的 `version`（即 `1.9.0`）」。好处是改版本只需改根 `Cargo.toml` 一处，所有子 crate 同步更新，保证全仓版本一致。

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
│   zenoh-sync  zenoh-task  zenoh-shm  zenoh-stats  …      │
└─────────────────────────────────────────────────────────┘
```

依赖方向是**严格自顶向下**：顶层依赖中层，中层依赖底层；底层绝不会反过来 `use` 顶层。这是 Rust 防止循环依赖的硬约束，也是我们定位「某个功能实现在哪一层」的指南针。

README 把「稳定边界」说得很清楚：

[README.md:L21-L52](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/README.md#L21-L52) —— 仓库结构说明。关键两段：`commons`「These crates are not intended to be imported directly, and their public APIs can be changed at any time. Stable APIs are provided by `zenoh` and `zenoh-ext` only.」也就是说：**只有 `zenoh` 和 `zenoh-ext` 对外稳定**；`commons/*`、`io/*` 都是内部实现，随时会改。

#### 4.2.2 核心流程

要确认一条依赖箭头，只需打开「上层 crate」的 `Cargo.toml`，看它的 `[dependencies]` 里有没有「下层 crate」。例如要确认 `zenoh → zenoh-transport`，就看 `zenoh/Cargo.toml` 里有没有 `zenoh-transport`。

依赖链的形成遵循一个朴素的递归规则：

```
顶层 crate 编译 → 需要它依赖的中层 crate
中层 crate 编译 → 需要它依赖的底层 crate
底层 crate 编译 → 只需要外部库（serde、tokio…）
```

因此，**底层 crate 必须最先编译完成**。这也解释了为什么底层 crate（如 `zenoh-buffers`、`zenoh-result`）几乎只依赖外部库——它们是整座大厦的地基，不能有循环风险。

#### 4.2.3 源码精读

我们自顶向下确认四个关键箭头。

**(a) `zenohd → zenoh`**：路由器二进制依赖主 crate。

[zenohd/Cargo.toml:L39-L44](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/Cargo.toml#L39-L44) —— `zenohd` 的依赖里 `zenoh = { workspace = true, default-features = false, features = ["internal", "plugins", "runtime_plugins", "unstable"] }`。这说明 `zenohd` 几乎只是「`zenoh` 运行时 + 一个插件管理器 + 命令行参数解析」的薄壳（`zenohd/README.md` 第 23 行原话：「`zenohd` is the Zenoh runtime with a plugin manager」）。注意它显式打开了 `internal`、`unstable` 等 feature——这些是普通应用默认关闭、但路由器必须打开的内部开关。

**(b) `zenoh → zenoh-transport`**：主实现依赖传输层。

[zenoh/Cargo.toml:L129](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/Cargo.toml#L129) —— `zenoh-transport = { workspace = true }`。同一文件第 118 行还有 `zenoh-link = { workspace = true }`、第 119 行 `zenoh-link-commons`，说明 `zenoh` 直接持有传输层与链路层句柄。

**(c) `zenoh-transport → zenoh-link / zenoh-codec / zenoh-config`**：传输层依赖链路层与编解码、配置。

[io/zenoh-transport/Cargo.toml:L83](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/Cargo.toml#L83) —— `zenoh-link = { workspace = true }`。同一依赖块（第 78–92 行）还列出 `zenoh-codec`（第 79 行）、`zenoh-config`（第 80 行）、`zenoh-protocol`（第 85 行）、`zenoh-runtime`（第 87 行）、`zenoh-task`（第 91 行）等。这正说明传输层是「把字节链路 + 编解码 + 配置 + 异步运行时组合起来」的中层。

**(d) `zenoh-link → zenoh-link-commons / 具体链路`**：链路抽象层依赖各具体链路实现。

[io/zenoh-link/Cargo.toml:L31](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link/Cargo.toml#L31) —— `transport_tcp = ["zenoh-config/transport_tcp", "zenoh-link-tcp"]`。这里 `zenoh-link-tcp` 是一个 `optional` 依赖（见第 48 行 `zenoh-link-tcp = { workspace = true, optional = true }`），只有当启用 `transport_tcp` feature 时才被拉进来。`zenoh-link` 自己则固定依赖 `zenoh-link-commons`（第 44 行）和 `zenoh-protocol`（第 55 行）。

**(e) commons 地基**：最底层的协议/编解码/配置 crate。

[commons/zenoh-protocol/Cargo.toml:L42-L50](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/Cargo.toml#L42-L50) —— `zenoh-protocol` 只依赖 `zenoh-buffers`、`zenoh-keyexpr`、`zenoh-macros`、`zenoh-result` 这些更底层的自家 crate（外加 `serde`、`uhlc`、`rand`）。注意它的 `description = "Internal crate for zenoh."`（第 17 行）——明确标注自己是内部 crate。

[commons/zenoh-codec/Cargo.toml:L44-L46](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-codec/Cargo.toml#L44-L46) —— `zenoh-codec` 依赖 `zenoh-buffers` 和 `zenoh-protocol`（外加可选的 `zenoh-shm`）。所以编解码层站在「缓冲区 + 协议模型」之上。

把这些箭头串起来，得到一条完整的自顶向下依赖链：

```
zenohd ──► zenoh ──► zenoh-transport ──► zenoh-link ──► zenoh-link-tcp
                        │                     │
                        ├──► zenoh-codec ──► zenoh-protocol ──► zenoh-keyexpr
                        ├──► zenoh-config ──► zenoh-protocol
                        └──► zenoh-runtime / zenoh-task / zenoh-sync …
                                              （都落在 commons 地基上）
```

> 稳定性标注：这条链里**只有 `zenoh`（和它的伙伴 `zenoh-ext`）对用户稳定**；`zenoh-transport`、`zenoh-link`、`zenoh-codec`、`zenoh-protocol`、`zenoh-config`、`zenoh-keyexpr`、`zenoh-runtime` 等全部是 `commons`/`io` 内部 crate，写应用时不应直接依赖，但读源码理解原理时它们是主角（后续单元会逐层深入）。

#### 4.2.4 代码实践

**实践目标**：动手画出本讲的「综合依赖草图」，把 `zenoh-codec`、`zenoh-protocol`、`zenoh-keyexpr`、`zenoh-config`、`zenoh-link`、`zenoh-transport`、`zenohd` 之间的箭头标出来，并标注哪些是 commons 内部 crate、哪些对外稳定。

**操作步骤**：

1. 打开下列 7 个 `Cargo.toml`，分别看它们的 `[dependencies]`：
   - `zenohd/Cargo.toml`（看是否依赖 `zenoh`）
   - `zenoh/Cargo.toml`（看是否依赖 `zenoh-transport`、`zenoh-link`）
   - `io/zenoh-transport/Cargo.toml`（看是否依赖 `zenoh-link`、`zenoh-codec`、`zenoh-config`）
   - `io/zenoh-link/Cargo.toml`（看是否依赖 `zenoh-link-commons`、`zenoh-protocol`）
   - `commons/zenoh-codec/Cargo.toml`（看是否依赖 `zenoh-protocol`、`zenoh-buffers`）
   - `commons/zenoh-config/Cargo.toml`（看是否依赖 `zenoh-protocol`、`zenoh-keyexpr`）
   - `commons/zenoh-protocol/Cargo.toml`（看是否依赖 `zenoh-keyexpr`、`zenoh-buffers`）
2. 用文字或简单符号（`A ──► B` 表示 A 依赖 B）画出依赖图。
3. 给每个 crate 标注：**对外稳定**（`zenoh`、`zenoh-ext`）还是 **commons 内部**（其余几个）。

**需要观察的现象**：你应该能确认以下结论——`zenohd` 依赖 `zenoh`；`zenoh` 依赖 `zenoh-transport`/`zenoh-link`；`zenoh-transport` 依赖 `zenoh-link`/`zenoh-codec`/`zenoh-config`；`zenoh-link` 依赖 `zenoh-link-commons`；`zenoh-codec` 与 `zenoh-config` 都依赖 `zenoh-protocol`；`zenoh-protocol` 依赖 `zenoh-keyexpr`。

**预期结果**：得到一张与本节 4.2.3 末尾相同的依赖草图，并且明确「只有 `zenoh`/`zenoh-ext` 稳定，其余都是 commons/io 内部 crate」。这是后续内部架构单元反复要用到的地图。

#### 4.2.5 小练习与答案

**练习 1**：如果有人想在 `zenoh-protocol`（底层）里调用 `zenoh-transport`（中层）的功能，为什么不可能？

**参考答案**：因为依赖只能「从上向下」，`zenoh-transport` 已经依赖 `zenoh-protocol`；若 `zenoh-protocol` 反过来依赖 `zenoh-transport`，就形成循环依赖，cargo 会直接报错拒绝编译。这也倒逼 Zenoh 把「通用、抽象」的东西（协议消息结构、key expression）放在底层，把「具体、组合」的东西（传输实现）放在上层。

**练习 2**：`zenoh-config` 同时出现在 `io/zenoh-transport` 和 `io/zenoh-link` 的依赖里。这说明配置 crate 处于什么位置？

**参考答案**：`zenoh-config` 属于 commons 地基层，被多个 IO 层 crate 共享。它依赖 `zenoh-protocol`、`zenoh-keyexpr` 等更底层 crate，但不依赖任何 IO 层 crate，所以可以被 `zenoh-link`、`zenoh-transport`、`zenoh` 等上层安全地复用，作为「全仓统一的配置模型」。

---

### 4.3 Feature 开关：按需启用传输与特性

#### 4.3.1 概念说明

Zenoh 支持很多种链路（tcp/udp/tls/quic/ws/serial/…）、很多可选能力（共享内存、统计、压缩、多种认证）。如果全部默认编译进来，二进制会很大、编译很慢，还会引入不必要的系统依赖。所以 Zenoh 用 **feature** 把它们做成「按需启用」的开关。

核心思路是**层层转发**：用户在自己的 `Cargo.toml` 里只面对最顶层的 `zenoh` crate 的 feature（例如 `transport_tcp`、`shared-memory`），而 `zenoh` 会把这个 feature 转发给 `zenoh-transport`，`zenoh-transport` 再转发给 `zenoh-link`，`zenoh-link` 再拉进具体的 `zenoh-link-tcp`。最终只有真正需要的链路 crate 被编译。

这样做的好处：

- 用户只记一套 feature 名字（在 `zenoh` 上），不用知道下层 crate 结构。
- 不需要的链路（比如嵌入式设备不需要 TLS）完全不参与编译。

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

类似地，`shared-memory`、`unstable`、`stats`、`auth_usrpwd`、`auth_pubkey`、`transport_compression` 等都是按同样的「层层转发」机制工作。

#### 4.3.3 源码精读

**(a) `zenoh` 的 feature 定义——用户面对的入口**：

[zenoh/Cargo.toml:L31-L46](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/Cargo.toml#L31-L46) —— `zenoh` 的 `default` feature 列表。默认会启用一长串 `transport_*`、`auth_*`、`transport_compression`、`transport_multilink`。这意味着默认情况下，普通用户开箱即用就支持 tcp/udp/tls/quic/ws/unixsock 等主流链路。

[zenoh/Cargo.toml:L72](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/Cargo.toml#L72) —— `transport_tcp = ["zenoh-config/transport_tcp", "zenoh-transport/transport_tcp"]`。这是「转发」的关键一行：启用 `transport_tcp` 时，同时把 `transport_tcp` 这个 feature 打开到下游 `zenoh-config` 和 `zenoh-transport`。

[zenoh/Cargo.toml:L55-L60](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/Cargo.toml#L55-L60) —— `shared-memory = [...]`。它一次性把共享内存 feature 下发到 `zenoh-buffers`、`zenoh-protocol`、`zenoh-shm`、`zenoh-transport` 四个 crate（注意 `zenoh-shm` 本身在 `zenoh` 里是 `optional` 依赖，第 125 行，只有开 feature 才会拉进来）。

[zenoh/Cargo.toml:L79-L84](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/Cargo.toml#L79-L84) —— `unstable = [...]`。`unstable` 是「不稳定 API」的总开关，它会打开 `zenoh-config`、`zenoh-keyexpr`、`zenoh-protocol`、`zenoh-transport` 各自的 `unstable` feature。我们在《u1-l1》《u1-l2》里提到的「不稳定 API 受 feature 门控」就来自这里。

[zenoh/Cargo.toml:L47-L51](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/Cargo.toml#L47-L51) —— `internal = [...]`。`internal` 是更深的「内部 API」开关，普通应用几乎不用；但路由器 `zenohd` 必须打开它（见下面）。

**(b) `zenoh-transport` 的转发——中转站**：

[io/zenoh-transport/Cargo.toml:L46](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/Cargo.toml#L46) —— `transport_tcp = ["zenoh-config/transport_tcp", "zenoh-link/transport_tcp"]`。可以看到同样的模式：再往下转发给 `zenoh-config` 和 `zenoh-link`。

**(c) `zenoh-link` 的落地——拉进具体实现**：

[io/zenoh-link/Cargo.toml:L31](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link/Cargo.toml#L31) —— `transport_tcp = ["zenoh-config/transport_tcp", "zenoh-link-tcp"]`。这一层终于把 `zenoh-link-tcp` 这个真实的可选依赖激活（它在 `zenoh-link/Cargo.toml` 第 48 行被声明为 `optional = true`）。

**(d) `zenohd` 反过来强制打开内部 feature**：

[zenohd/Cargo.toml:L29](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/Cargo.toml#L29) —— `default = ["zenoh/default"]`。`zenohd` 的默认 feature 直接继承 `zenoh` 的默认 feature，确保路由器自带所有主流链路。再结合第 39–44 行的 `features = ["internal", "plugins", "runtime_plugins", "unstable"]`，路由器额外打开了插件与内部 API 能力。

> 一句话总结：feature 在 `zenoh` 这一层是「面向用户的开关」，在 `zenoh-transport`/`zenoh-link` 这几层是「转发管道」，最终在 `zenoh-link-*` 这一层变成「真实的可选依赖」。这套设计让用户只面对一套名字，却能在编译期精确控制要包含哪些底层实现。

#### 4.3.4 代码实践

**实践目标**：亲手跟踪一次 feature 转发链，验证「层层下发」机制。

**操作步骤**：

1. 打开 `zenoh/Cargo.toml` 第 72 行，确认 `transport_tcp` 转发给 `zenoh-config` 和 `zenoh-transport`。
2. 跟着跳到 `io/zenoh-transport/Cargo.toml` 第 46 行，确认它又转发给 `zenoh-config` 和 `zenoh-link`。
3. 再跳到 `io/zenoh-link/Cargo.toml` 第 31 行，确认它最终激活了 `zenoh-link-tcp`（一个 `optional` 依赖，见同文件第 48 行）。
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

---

## 5. 综合实践

把本讲三个模块串起来，完成一份「**Zenoh crate 全景图**」文档（纯文字即可）：

1. **成员清单**：从根 `Cargo.toml` 的 `members` 抄出全部 crate，按 `commons/`、`io/`、`plugins/`、顶层分组。
2. **依赖草图**：至少画出下面这些箭头，并保证方向是「上层 → 下层」：
   - `zenohd ──► zenoh`
   - `zenoh ──► zenoh-transport ──► zenoh-link ──► zenoh-link-tcp`
   - `zenoh-transport ──► zenoh-codec ──► zenoh-protocol ──► zenoh-keyexpr`
   - `zenoh-transport ──► zenoh-config ──► zenoh-protocol`
   - `zenoh ──► zenoh-runtime / zenoh-task / zenoh-sync`（commons 地基）
3. **稳定性标注**：用两种颜色/符号区分「对外稳定（`zenoh`、`zenoh-ext`）」与「commons/io 内部 crate（其余）」。
4. **feature 转发链**：任选 `transport_tcp` 或 `shared-memory`，写出它从 `zenoh` 一路下发到最底层具体 crate/可选依赖的完整路径（参考 4.3.4 的步骤）。

完成后，你应当拥有两张图：一张「依赖层次图」、一张「feature 转发图」。这两张图是后续所有内部架构讲义（u7–u12）的导航地图——以后每讲到一个子系统，你都能在这张图上找到它所在的层。

## 6. 本讲小结

- Zenoh 是一个标准的 cargo **workspace**，根 `Cargo.toml` 用 `members` 列出全部约 40 个子 crate，并用 `[workspace.package]`/`[workspace.dependencies]` 统一管理版本（`1.9.0`）与 Rust 最低版本（`1.75.0`）。
- 仓库分六大块：`commons/*`（内部地基）、`io/*`（链路层 + 传输层）、`plugins/*`（插件）、`zenoh`（主实现 + 公开 API）、`zenoh-ext`（扩展库）、`zenohd`（路由器二进制），外加 `examples`。
- crate 严格**分层**：`zenohd/plugins/examples`（顶层）→ `zenoh`（API/主实现）→ `zenoh-transport`/`zenoh-link`（IO 层）→ `zenoh-protocol`/`zenoh-codec`/`zenoh-config`/`zenoh-keyexpr`/`zenoh-buffers`（commons 地基）。依赖只能从上向下，杜绝循环。
- **稳定边界**：只有 `zenoh` 与 `zenoh-ext` 对外提供稳定 API；所有 `commons/*`、`io/*` 都是内部 crate（多数 `description` 写着 `"Internal crate for zenoh."`），写应用不应直接依赖。
- **feature 系统**采用层层转发：用户只面对 `zenoh` 上的 feature（如 `transport_tcp`、`shared-memory`、`unstable`），它沿 `zenoh → zenoh-transport → zenoh-link → zenoh-link-*` 下发，最终落到真实的可选依赖上，实现「按需编译」。
- `zenohd` 作为路由器，在依赖 `zenoh` 时显式打开 `internal`、`plugins`、`runtime_plugins`、`unstable` 等 feature，以使用普通应用默认关闭的内部能力。

## 7. 下一步学习建议

本讲建立的是「**横向地图**」（谁和谁并列、谁依赖谁）。接下来建议：

- 进入**第 2 单元**，从「**纵向使用**」角度学习：先看《u2-l1 打开一个 Session》，理解 `zenoh::open` 如何把这张图里的 `zenoh-runtime`/`zenoh-transport` 等内部 crate 组装成一个用户手中的 `Session`。
- 如果你想更早接触公开 API 的内部模块布局，可以先读《u1-l4 公开 API 地图：lib.rs 模块导览》，它承接本讲的「`zenoh` 是门面」结论，展开门面后面 re-export 了哪些类型。
- 当后续进入**第 7–10 单元（内部架构）**时，请随时回到本讲的依赖草图定位文件——例如讲传输层就盯 `io/zenoh-transport`，讲协议编解码就盯 `commons/zenoh-codec`，讲路由就回到 `zenoh` crate 内部的 `net/routing` 模块。
