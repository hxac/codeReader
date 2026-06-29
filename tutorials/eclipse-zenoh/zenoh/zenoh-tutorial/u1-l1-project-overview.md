# Zenoh 是什么：项目定位与仓库结构

## 1. 本讲目标

本讲是整个 Zenoh 学习手册的**第一站**，不写一行复杂代码，只解决三件事：

1. 搞清楚 Zenoh 到底是什么、它想解决什么问题、为什么会有这个项目。
2. 在脑子里建立这张仓库地图：根目录下 `zenoh`、`zenoh-ext`、`zenohd`、`plugins`、`commons`、`io`、`examples` 这些目录各自负责什么、谁依赖谁、哪些是给用户用的「稳定 API」、哪些是「内部实现」。
3. 读懂公开 API 的总入口 `zenoh/src/lib.rs` 的模块文档，知道 Zenoh 有哪些顶层概念（Session、Pub/Sub、Query/Reply、Key Expression 等），为后续每一讲的学习打好「目录索引」。

学完本讲，你应当能用自己的话向同事解释「Zenoh 是干什么的」，并且能在仓库里迅速找到任何功能对应的源码位置。

---

## 2. 前置知识

本讲面向零基础读者，但下面几个概念会让你读得更顺：

- **Pub/Sub（发布/订阅）**：一种通信模式。发布者（Publisher）把消息发到一个「主题」，订阅者（Subscriber）订阅自己感兴趣的「主题」，两者不需要直接认识对方。常见的例子有 MQTT、Kafka。你订过 RSS、关注过某位作者的专栏，本质上就是 Pub/Sub。
- **Query/Reply（查询/应答）**：另一种通信模式，类似 HTTP 的「请求—响应」。提问方发出查询，提供数据的一方返回应答。
- **Cargo 与 Rust workspace**：Cargo 是 Rust 的包管理器/构建工具。一个 workspace（工作空间）可以包含多个互相协作的 crate（Rust 的编译单元，类似「子项目」）。本仓库就是一个 workspace，里面有几十个 crate。
- **`lib.rs`**：Rust 库 crate 的「根文件」，相当于这本书的目录页。`pub mod xxx;` 表示对外暴露一个名为 `xxx` 的模块。

> 不熟悉 Rust 也完全没关系：本讲重点在「读文档和目录结构」，几乎不涉及 Rust 语法细节。遇到看不懂的语法，先跳过，抓住「这段代码在描述什么概念」即可。

---

## 3. 本讲源码地图

本讲只看三个文件，它们正好对应三个最小模块：

| 文件 | 作用 | 本讲用来回答的问题 |
| --- | --- | --- |
| [README.md](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/README.md) | 项目门面，介绍 Zenoh 是什么、怎么构建、仓库结构 | Zenoh 是什么？仓库有哪些部分？ |
| [Cargo.toml](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/Cargo.toml) | workspace 的总配置，列出所有成员 crate | workspace 里到底有哪些 crate？谁依赖谁？ |
| [zenoh/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs) | 公开 API 的根文件与模块文档 | Zenoh 对外提供了哪些概念和模块？ |

记住一句话：**README 告诉你「是什么」，Cargo.toml 告诉你「有哪些零件」，lib.rs 告诉你「用户能调用什么」。**

---

## 4. 核心概念与源码讲解

### 4.1 项目说明：Zenoh 是什么

#### 4.1.1 概念说明

打开 Zenoh 官网和 README，你会反复看到一句口号：

> **Zero Overhead Pub/Sub, Store/Query and Compute.**

把它拆开理解：

- **Zero Overhead（零开销）**：Zenoh 追求极低的延迟和极高的吞吐，目标是「比主流栈快得多」。
- **Pub/Sub（发布/订阅）**：实时流动的数据（data in motion），例如传感器每秒上报的温度。
- **Store/Query（存储/查询）**：静止在某个地方的数据（data at rest），例如数据库里某个 key 的历史值。
- **Compute（计算）**：把计算搬到数据所在的地方，而不是把数据搬到计算所在的地方。

Zenoh 最核心的卖点，是把上面这三件事**统一**进同一个协议栈。README 开头用一句话点明了这一点：

[README.md:L13-L15](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/README.md#L13-L15) —— 这里定义了 Zenoh 的定位：把传统的 pub/sub 与地理分布式存储、查询、计算优雅地融合在一起。

换句话说，传统架构里你可能需要「一个消息队列 + 一个数据库 + 一个 RPC 框架」三套系统；Zenoh 想用一套 API、一套网络协议把它们都包了。

Zenoh 之所以能做到，关键在于它自己定义了一套网络协议（Zenoh 协议），而本仓库就是这套协议的** Rust 参考实现**。其他语言（Python、C、Kotlin、Java…）大多是绑定到这个 Rust 实现上的。

#### 4.1.2 核心流程

从一个使用者的视角，Zenoh 的世界可以归纳成三层关系：

```text
┌─────────────────────────────────────────────┐
│  应用代码（用户写）                          │
│   open(config)  →  得到一个 Session          │
└──────────────────┬──────────────────────────┘
                   │ Session 上声明各种实体
                   ▼
┌─────────────────────────────────────────────┐
│  两大通信范式                                │
│   ① Pub/Sub：Publisher ──▶ Subscriber        │
│   ② Query/Reply：Querier ──▶ Queryable       │
└──────────────────┬──────────────────────────┘
                   │ 底层走 Zenoh 协议
                   ▼
┌─────────────────────────────────────────────┐
│  Zenoh 网络（节点可组成 mesh/star/clique）   │
└─────────────────────────────────────────────┘
```

- 最上面：用户调用 `zenoh::open(config)` 打开一个**会话（Session）**。
- 中间：在 Session 上声明「发布者、订阅者、查询器、可查询者」等实体，使用两种通信范式之一。
- 最下面：这些实体通过 Zenoh 协议在网络里互相找对方、传数据。

节点之间可以组成**任意拓扑**（mesh 网状、star 星型、clique 全互联），由配置里的 `mode` 参数决定本节点扮演什么角色（路由器 router / 对等节点 peer / 客户端 client）。

#### 4.1.3 源码精读

我们直接看 `lib.rs` 顶部的模块文档，这段是理解 Zenoh 全貌最浓缩的一段：

[zenoh/src/lib.rs:L17-L20](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L17-L20) —— Zenoh 官方的一句话自我介绍，与 README 一致：统一 data in motion / at rest / computation。

紧接着是一个叫 `Components and concepts`（组件与概念）的小节，它是整篇文档的目录。其中最关键的一句点出 Zenoh 支持**两种通信范式**：

[zenoh/src/lib.rs:L37-L38](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L37-L38) —— 明确 Zenoh 支持两种通信范式：publish/subscribe 与 query/reply；进行通信的实体（publisher、subscriber、querier、queryable）都由 Session 对象声明。

再往上一点，关于 Session 与拓扑角色：

[zenoh/src/lib.rs:L26-L35](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L26-L35) —— 说明 Session 是 API 的根元素，由 `open` 函数创建并接收一个 `config`；Zenoh 允许节点组成任意拓扑，配置里的 `mode` 决定角色。

把这两段合起来，你就掌握了 Zenoh 的「骨架」：**Session 是根，两种范式是肉，config 里的 mode 决定骨架长什么样。**

#### 4.1.4 代码实践

> 本节是**源码阅读型实践**（不要求运行），目标：吃透「两大通信范式 + Session」。

1. **实践目标**：用自己的话把 Zenoh 的「三大组件」（Session / Pub-Sub / Query-Reply）讲清楚，并各举一个真实场景。
2. **操作步骤**：
   - 用浏览器或本地编辑器打开 [README.md](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/README.md) 和 [zenoh/src/lib.rs 的 Components and concepts 小节](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L22-L72)。
   - 逐字阅读 `Session`、`Publish/Subscribe`、`Query/Reply`、`Key Expressions`、`Data representation`、`Other components` 这几个小标题下的说明。
   - 在笔记里写一段**不少于 5 行**的中文总结，要求：
     - 解释什么是 Session；
     - 解释 Pub/Sub 适合什么场景（自己举一个例子，例如「多辆车的 GPS 实时上报」）；
     - 解释 Query/Reply 适合什么场景（自己举一个例子，例如「查询某辆车最近一小时的轨迹」）。
3. **需要观察的现象**：你会发现 README 和 lib.rs 描述的是同一套概念，只是 lib.rs 更偏 API 视角。两者对照阅读能加深印象。
4. **预期结果**：产出一段总结文字。建议你自己写完后再对照下面「小练习」的参考答案，看理解是否到位。

> 说明：本实践不涉及运行命令，所以**没有「运行结果」**，重点在阅读与归纳。本手册后续每一讲都会有真正可运行的实践。

#### 4.1.5 小练习与答案

**练习 1**：README 说 Zenoh 统一了「data in motion」和「data at rest」，这两个词分别对应哪两种通信范式？

> **参考答案**：data in motion（流动的数据）对应 Pub/Sub（实时发布订阅）；data at rest（静止的数据）对应 Query/Reply（向存储/可查询者查询历史或当前值）。

**练习 2**：为什么 Zenoh 不需要你显式「发现」别的节点也能发布/订阅？（提示：看 lib.rs 里 `scouting` 小节的说明）

> **参考答案**：因为 Zenoh 协议本身会处理路由和匹配。lib.rs 在 `scouting` 小节明确指出：仅仅为了发布、订阅或查询数据，**没有必要**显式去发现别的节点。scouting（发现）是一个「锦上添花」的功能，不是必须的。

---

### 4.2 workspace 成员：仓库里到底有哪些零件

#### 4.2.1 概念说明

Zenoh 是个大项目，单靠一个 crate 装不下。它采用 Rust workspace 的方式，把实现拆成几十个小 crate，每个 crate 只管一件事。理解这些 crate 的分层，是后续阅读任何源码的前提。

README 的「Structure of the Repository」一节把顶层目录分成 6 类，这是最重要的一张表：

[README.md:L21-L52](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/README.md#L21-L52) —— 仓库结构总览，列出 zenoh / zenoh-ext / zenohd / plugins / commons / examples 六大组成部分。

其中要特别记住一条**边界**：

[README.md:L45-L48](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/README.md#L45-L48) —— `commons` 是 `zenoh` 内部使用的 crate，**不打算被直接 import**，其公开 API 随时可能变；只有 `zenoh` 和 `zenoh-ext` 提供**稳定 API**。

也就是说：作为普通用户，你只应该依赖 `zenoh`（必要时加 `zenoh-ext`）。`commons` 下的那些 crate（`zenoh-protocol`、`zenoh-codec`、`zenoh-transport` 等）是实现细节，写应用时不该直接用，但**读源码理解原理时**它们才是主角。

#### 4.2.2 核心流程

把 README 的六大目录和「稳定 vs 内部」的边界合起来，可以得到这张分层图：

```text
用户应用
   │  只依赖稳定 API
   ▼
┌──────────────┐   ┌──────────────┐
│   zenoh      │   │  zenoh-ext   │   ← 稳定 API（公开）
│ (协议参考实现)│   │(高级 pub/sub │
│              │   │  + 序列化)   │
└──────┬───────┘   └──────┬───────┘
       │ 内部调用          │
       ▼                   ▼
┌──────────────────────────────────┐
│ commons/* (协议/编解码/缓冲/...)  │  ← 内部实现（非稳定）
│ io/*     (link 链路 + transport)  │
└──────────────────────────────────┘
       ▲ 配套基础设施
       │
┌──────┴───────┐   ┌──────────────┐
│   zenohd     │   │   plugins    │   ← 路由器二进制 + 插件
│ (路由器守护) │   │(rest/storage │
│              │   │  /example...)│
└──────────────┘   └──────────────┘

examples：官方示例（既是文档也是测试工具）
```

要点：

- **纵向是依赖方向**：上层依赖下层。`zenoh` 依赖 `commons` 和 `io`，而 `zenohd` 又依赖 `zenoh`。
- **横向是职责划分**：`zenoh`/`zenoh-ext` 是「给应用的库」，`zenohd`/`plugins` 是「给运维/二次开发的组件」，`examples` 是「教学 + 工具」。
- **`commons` 和 `io` 是地基**：协议怎么定义、消息怎么编码、字节怎么传输，都在这两层。

#### 4.2.3 源码精读

具体有哪些 crate？答案在根 `Cargo.toml` 的 workspace `members` 列表里：

[Cargo.toml:L14-L64](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/Cargo.toml#L14-L64) —— workspace 配置，`members` 数组列出了全部要编译的 crate，`exclude` 列出了不参与正常构建的子目录（多是一些 CI 专用的检查项目）。

我们按目录把 `members` 归类（不必背，对照上表理解即可）：

| 目录前缀 | 代表 crate | 一句话职责 |
| --- | --- | --- |
| `commons/` | `zenoh-protocol`、`zenoh-codec`、`zenoh-config`、`zenoh-keyexpr`、`zenoh-buffers`、`zenoh-runtime`、`zenoh-shm`、`zenoh-stats` 等 | 协议定义、编解码、配置、key 表达式、缓冲区、异步运行时、共享内存、统计等「地基」 |
| `io/` | `zenoh-link`、`zenoh-link-commons`、`zenoh-link-tcp/udp/tls/quic/ws/...`、`zenoh-transport` | 各种物理链路（TCP/UDP/TLS/QUIC/WebSocket…）与传输层 |
| `plugins/` | `zenoh-plugin-trait`、`zenoh-plugin-rest`、`zenoh-plugin-storage-manager`、`zenoh-backend-traits`、`zenoh-backend-example`、`zenoh-plugin-example` | 插件机制与各种插件（REST、存储管理器、存储后端示例、示例插件） |
| （顶层） | `zenoh`、`zenoh-ext`、`zenoh-ext/examples`、`zenohd`、`examples` | 协议主实现、扩展库、路由器、示例 |

再看 `workspace.package`，它定义了所有 crate 共享的元信息：

[Cargo.toml:L67-L82](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/Cargo.toml#L67-L82) —— 共享的包元信息，注意 `version = "1.9.0"`（整组 crate 统一版本）、`rust-version = "1.75.0"`（最低 Rust 版本）、`edition = "2021"`。

> 小知识：所有内部 crate 都用**同一个版本号**（这里是 1.9.0），并且在 `[workspace.dependencies]` 里互相用 `version = "=1.9.0"` 这种带等号的写法锁定，保证一组 crate 严格匹配、不会乱用旧版。这条规则在 [Cargo.toml:L226-L260](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/Cargo.toml#L226-L260) 的依赖声明里能看到，例如 `zenoh = { version = "=1.9.0", path = "zenoh", ... }`。

#### 4.2.4 代码实践

1. **实践目标**：亲手在源码里确认「六大目录」与 workspace members 的对应关系，建立一张可查的对照表。
2. **操作步骤**：
   - 打开 [Cargo.toml 的 members 列表](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/Cargo.toml#L22-L64)。
   - 打开 [README 的仓库结构小节](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/README.md#L21-L52)。
   - 在你的笔记里画一张三列表格：`crate 名` | `所在顶层目录` | `是否稳定 API`。至少填入 10 行，例如：`zenoh-protocol` | `commons` | `否（内部）`；`zenoh-link-tcp` | `io` | `否（内部）`；`zenoh` | 顶层 | `是（稳定）`。
3. **需要观察的现象**：你会发现 `members` 里的每一项都能归到 README 描述的六大目录之一；`commons` 和 `io` 下的 crate 数量最多。
4. **预期结果**：得到一张「crate → 目录 → 稳定性」的速查表。以后看任何源码路径（例如 `commons/zenoh-protocol/...`），你都能立刻判断它属于哪一层、是否是稳定 API。

#### 4.2.5 小练习与答案

**练习 1**：`zenoh-transport` 属于哪个顶层目录？它是不是稳定 API？

> **参考答案**：它属于 `io/` 目录（路径是 `io/zenoh-transport`）。它是 `commons/io` 层的内部 crate，**不是**稳定 API，普通应用不该直接依赖它（应由 `zenoh` 内部使用）。

**练习 2**：如果我想给 Zenoh 写一个把数据存到 MySQL 的存储后端，我应该看哪个目录的示例？

> **参考答案**：看 `plugins/` 目录，参考 `zenoh-backend-example`（一个示例存储后端）和 `zenoh-backend-traits`（定义后端要实现的接口 trait）。这些会在「存储与后端」那一讲详细讲。

---

### 4.3 顶层模块文档：lib.rs 里的公开 API 全景

#### 4.3.1 概念说明

知道了仓库有哪些零件，接下来要回答：**作为用户，我到底能调用什么？** 答案全部写在 `zenoh/src/lib.rs` 里。

`lib.rs` 是 `zenoh` 这个 crate 的根文件，它通过一系列 `pub mod xxx { ... }` 把内部模块「重新导出」成对用户友好的公开模块。可以这样理解：

- 内部代码散落在 `zenoh/src/api/`、`zenoh/src/net/` 等目录里（实现细节，乱且多）。
- `lib.rs` 像一个「门面（facade）」，把它们整理成干净的、有文档的公开模块，例如 `zenoh::pubsub`、`zenoh::query`、`zenoh::config`。

所以读 `lib.rs` 的模块文档，就等于读了一遍「Zenoh 用户手册的目录」。

另外，`lib.rs` 里还有一个贯穿全局的设计——**builder 模式**和三个基础 trait，先建立印象：

[zenoh/src/lib.rs:L275](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L275) —— 重新导出 `Resolvable`、`Resolve`、`Wait` 三个 trait，它们是 Zenoh builder 模式的根基（构造器最终要「resolve」成真实对象，异步用 `await`、同步用 `wait()`）。

最顶层的几个入口函数/类型则在这里：

[zenoh/src/lib.rs:L283-L288](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L283-L288) —— 重新导出 `Config`、`scout`、`open`、`Session`，这四个是用户最先接触的入口：用 `Config` 配置，用 `open` 打开会话得到 `Session`，用 `scout` 做节点发现。

#### 4.3.2 核心流程

`lib.rs` 把公开 API 组织成下面这些模块，建议你把它们当成「功能抽屉」来记：

| 模块（`zenoh::xxx`） | 关键类型/函数 | 解决什么问题 |
| --- | --- | --- |
| `session` | `open`、`Session` | 打开会话，是所有操作的根 |
| `config` | `Config`、`WhatAmI` | 配置会话，选择角色（router/peer/client） |
| `key_expr` | `KeyExpr`、`OwnedKeyExpr` | 用路径 + 通配符表达「地址」（如 `robot/sensor/*`） |
| `pubsub` | `Publisher`、`Subscriber` | 发布/订阅 |
| `query` | `Queryable`、`Querier`、`Query`、`Reply` | 查询/应答 |
| `sample` | `Sample`、`SampleKind` | 收到的「数据单元」，含 payload 与元数据 |
| `bytes` | `ZBytes`、`Encoding` | 负载的原始字节容器与编码 |
| `handlers` | `FifoChannel`、`RingChannel` | 如何从 subscriber/query 取数据（通道 vs 回调） |
| `qos` | `Reliability`、`CongestionControl`、`Priority` | 服务质量配置 |
| `scouting` | `scout`、`Scout` | 发现网络中的 Zenoh 节点 |
| `liveliness` | `LivelinessToken` | 感知节点/资源的上线、下线 |
| `matching` | `MatchingListener` | 让发布者知道「有没有人在订阅」，按需发送省带宽 |
| `time` | `Timestamp` | 基于混合逻辑时钟的时间戳 |

每个模块对应本手册后面的某一讲或几讲。本讲只要知道「有这些抽屉」就够了。

另外要留意一个机制：**feature 门控**。`lib.rs` 里有大量 `#[zenoh_macros::unstable]` 和 `#[zenoh_macros::internal]` 标注，表示对应的 API 需要开启特定 feature 才能用，并且可能随时变化。模块文档的「Features」小节列出了全部 feature：

[zenoh/src/lib.rs:L157-L210](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L157-L210) —— 列出 crate 暴露的所有 feature（`shared-memory`、`stats`、各种 `transport_*`、`unstable`、`internal` 等），以及默认开启的 feature 列表。

#### 4.3.3 源码精读

我们在 `lib.rs` 里逐个看几个最关键的模块声明，建立「类型 → 模块」的映射。注意行号对应的是 `pub mod xxx {` 出现的位置。

**Session 模块**：

[zenoh/src/lib.rs:L382-L416](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L382-L416) —— `pub mod session`，导出 `open`、`Session`、`SessionClosedError` 等。文档强调 Session 是可克隆的（每个 clone 是指向内部对象的 `Arc`，克隆廉价），关闭 Session 会关闭其上所有实体。

**Pub/Sub 模块**：

[zenoh/src/lib.rs:L550-L562](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L550-L562) —— `pub mod pubsub`，导出 `Publisher`、`Subscriber` 及其 builder。文档说明有两种发布语义：产生一个值序列，或更新某个 key 关联的单个值（后者需要 `delete` 操作来表示「该 key 不再有值」）。

**Query/Reply 模块**：

[zenoh/src/lib.rs:L645-L667](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L645-L667) —— `pub mod query`，导出 `Queryable`、`Querier`、`Query`、`Reply`、`Selector` 等。文档点出查询参数走 `Selector` 语法（类似 URL：`key?name=value;...`）。

**Config 模块**：

[zenoh/src/lib.rs:L1011-L1017](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L1011-L1017) —— `pub mod config`，导出 `Config`、`WhatAmI`、`ZenohId`、`EndPoint` 等。文档说明可用 `Config::from_file` 加载 json5/yaml 文件，也可用 `insert_json5`/`get_json` 读写单个配置项。

> 这四段代码合起来，正好覆盖了本练习任务里的「三大组件」：Session（session 模块）、Pub-Sub（pubsub 模块）、Query-Reply（query 模块），而它们都靠 config 模块来配置。

#### 4.3.4 代码实践

1. **实践目标**：在 `lib.rs` 里完成一次「寻宝」，亲手确认每个公开模块导出了哪些类型，而不是死记硬背。
2. **操作步骤**：
   - 打开 [zenoh/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs)。
   - 找到下面五个模块声明所在的行：`pub mod session`、`pub mod pubsub`、`pub mod query`、`pub mod bytes`、`pub mod handlers`。
   - 针对每个模块，找出它通过 `pub use ...` 导出的 **2 个**核心类型（例如 session 模块导出 `open` 和 `Session`；bytes 模块导出 `ZBytes` 和 `Encoding`）。
   - 把结果整理成一张「模块 → 类型」对照表。
3. **需要观察的现象**：你会看到很多类型带有 `#[zenoh_macros::unstable]` 或 `#[zenoh_macros::internal]` 标注——这说明它们需要开 feature 才能用，普通使用应优先选没有标注的那些。
4. **预期结果**：得到一张类似下表的成品：

   | 模块 | 导出的 2 个核心类型 |
   | --- | --- |
   | `session` | `open`（函数）、`Session` |
   | `pubsub` | `Publisher`、`Subscriber` |
   | `query` | `Queryable`、`Query` |
   | `bytes` | `ZBytes`、`Encoding` |
   | `handlers` | `FifoChannel`、`RingChannel` |

> 说明：上表是**示例答案**，你实际找出的类型可能更多，能列出即可。这张表会在后续每一讲被反复用到。

#### 4.3.5 小练习与答案

**练习 1**：我想把一段字符串当作消息体发布出去，应该用 `lib.rs` 里哪个模块的类型？

> **参考答案**：用 `bytes` 模块的 `ZBytes`。`ZBytes` 实现了 `From<&str>`/`From<String>`（文档里有 `let zbytes = zenoh::bytes::ZBytes::from("Hello, world!");` 的例子），所以可以直接把字符串转成 `ZBytes` 再发布。

**练习 2**：`#[zenoh_macros::internal]` 标注的类型，和 `#[zenoh_macros::unstable]` 标注的类型，区别是什么？

> **参考答案**：看 [zenoh/src/lib.rs:L201-L204](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L201-L204) 的说明：`unstable` API 是「可能被稳定下来」的（未来有机会转正），而 `internal` 是「因为暴露实现细节而不稳定」，本就不打算给用户用，多为方便其他语言 binding 而存在。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个**总览任务**（纯阅读 + 整理，不写代码）：

**任务：制作一份「Zenoh 一页速查卡」**

要求在一张纸（或一个 markdown 文件）上完成以下四块内容：

1. **一句话定位**：用你自己的话写 Zenoh 是什么（参考 4.1）。
2. **仓库分层图**：照着 4.2.2 的分层图，画出 `用户应用 → zenoh/zenoh-ext → commons/io → zenohd/plugins` 的依赖箭头，并标出「稳定 API」与「内部实现」的分界线。
3. **三大组件表**：列出 Session、Pub-Sub、Query-Reply 三个组件，每个写：它对应 `lib.rs` 的哪个模块、一句话作用、一个应用场景。
4. **下一步入口**：写下 `zenoh::open(config)`、`Session`、`Config` 这三个名字，并注明它们在 [lib.rs:L283-L288](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L283-L288) 被重新导出——这是下一讲「打开一个 Session」的起点。

**自检标准**：如果一位完全没接触过 Zenoh 的同事，仅凭你这张速查卡就能说出「Zenoh 是干什么的、仓库怎么分、公开 API 有哪些大模块」，那么本讲你就过关了。

---

## 6. 本讲小结

- Zenoh 是一套**统一**了 Pub/Sub（data in motion）、Store/Query（data at rest）和 Compute 的网络协议栈，主打「零开销」的高效通信，本仓库是它的 Rust 参考实现。
- 仓库分六大块：`zenoh`（主实现）、`zenoh-ext`（扩展：高级 pub/sub + 序列化）、`zenohd`（路由器二进制）、`plugins`（插件）、`commons`+`io`（内部地基）、`examples`（示例/工具）。
- **稳定边界**很重要：只有 `zenoh` 和 `zenoh-ext` 提供稳定 API；`commons/*` 和 `io/*` 是内部实现，API 随时可能变，写应用不该直接依赖，但读源码理解原理时它们是主角。
- workspace 里所有 crate 共享同一个版本号（当前 1.9.0）和最低 Rust 版本（1.75.0），互相用 `=版本` 严格锁定。
- `zenoh/src/lib.rs` 是公开 API 的「门面」，通过 `pub mod` 把内部模块整理成 `session`、`config`、`key_expr`、`pubsub`、`query`、`bytes`、`handlers`、`qos`、`scouting`、`liveliness`、`matching`、`time` 等模块，本手册后续每讲对应其中一两个。
- Zenoh 大量使用 builder 模式，根基是 `Resolvable`/`Resolve`/`Wait` 三个 trait；很多 API 被 `unstable`/`internal` feature 门控，需按需开启。

---

## 7. 下一步学习建议

本讲建立了全局地图，但你还没真正「跑起来」任何一个 Zenoh 程序。建议按这个顺序继续：

1. **先动手**：进入第 1 单元第 2 讲《构建、运行与第一个示例》（`u1-l2`），用 `cargo run --example z_pub` 和 `z_sub` 跑通一次真实的发布订阅，获得第一手体感。
2. **再补地图**：第 1 单元第 3 讲《Workspace 与 crate 布局总览》（`u1-l3`）会带你在 `Cargo.toml` 里把 crate 之间的依赖箭头画清楚，是本讲 4.2 的深入版。
3. **深入 API 入口**：第 1 单元第 4 讲《公开 API 地图》（`u1-l4`）会逐模块精读 `lib.rs`，是本讲 4.3 的展开。
4. **进入核心概念**：之后进入第 2 单元，从 `zenoh::open` 和 `Session` 开始真正写代码（`u2-l1`）。

> 阅读建议：从下一讲起，每篇都会引用真实源码并给出永久链接。遇到想深入了解的概念（例如传输层、协议编解码），可以随时跳到目录里 `io/`、`commons/zenoh-protocol`、`commons/zenoh-codec` 下对应的源码先「随便翻翻」，不必一次看懂——建立熟悉感本身就是学习的一部分。
