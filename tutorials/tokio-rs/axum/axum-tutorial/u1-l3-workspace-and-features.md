# 工作区结构、crate 划分与 feature 配置

## 1. 本讲目标

前两讲（u1-l1、u1-l2）我们都在「用」axum：知道它是 hyper 之上的一层、知道 `axum::serve` 需要哪些 feature 才存在、也跑起来了 `hello-world`。但如果你打开 axum 的仓库根目录，会发现它**不是一个 crate，而是四个**：`axum`、`axum-core`、`axum-extra`、`axum-macros`。它们为什么这样切分？为什么你在 `Cargo.toml` 里写一行 `axum = "0.8"` 之后，`Json`、`Form`、`Query` 这些类型会「默认就有」，而 `WebSocketUpgrade`、`Multipart` 却要额外开 feature？

本讲就从「项目工程组织」的角度把这些问题讲透。读完本讲，你应当能够：

- 看懂 axum 根目录 `Cargo.toml` 的 **workspace** 配置，说清「一个仓库管多个 crate」带来的好处。
- 说清 `axum`、`axum-core`、`axum-extra`、`axum-macros` 四个 crate 各自的职责，以及**为什么写库的人应当优先依赖 `axum-core`**。
- 读懂 `axum/Cargo.toml` 的 `[features]` 段，掌握 `default`、`http1`/`http2`、`json`、`form`、`query`、`tokio`、`ws` 等 feature 如何「按需裁剪编译产物」。
- 用 `--no-default-features --features ...` 在本地实际验证「关掉某个 feature 后某个类型就消失」。

本讲覆盖三个最小模块：**workspace 配置**、**crate 划分**、**feature flags**。它们回答的是同一个工程问题——「如何让一个功能丰富、却又轻量的库，在不同场景下只编译你真正需要的那部分」。

## 2. 前置知识

本讲会用到一些 Cargo 的概念，先用最通俗的方式建立直觉（如果你写过 Rust 项目，可以快速浏览）。

- **crate（包）**：Rust 里一个可独立编译、可被发布到 crates.io 的单元。一个项目可以由很多个 crate 组成，彼此通过依赖关系组合。你可以把它理解成「一个 `.so`/`.dll` 动态库」的 Rust 版本。
- **workspace（工作区）**：把多个相关的 crate 放在**同一个仓库**里统一管理。好处是：它们可以**共享同一份 `Cargo.lock`、同一个 `target/` 编译缓存、同一套 lint 规则**，还能用相对路径 `path = "../axum-core"` 互相引用，方便本地一起开发。
- **Cargo feature（特性开关）**：在 `Cargo.toml` 的 `[features]` 段声明的一组布尔开关。开启一个 feature 可以：①引入新的可选依赖（optional dependency），②打开某些依赖自己的子特性，③在源码里用 `#[cfg(feature = "xxx")]` 控制某段代码「编不编进来」。它的本质是「条件编译 + 条件依赖」。
- **`#[cfg(...)]`**：Rust 的条件编译属性。`#[cfg(feature = "json")]` 意思是「只有当 `json` 这个 feature 被开启时，紧跟的这行/这个 mod 才会被编译」。这是 feature 能「裁剪代码」的根本机制。

> **承接前两讲**：u1-l1 已经提到「`http1`/`http2` 决定 `axum::serve` 是否存在，`json` 决定 `Json` 是否可用」。本讲就把这句话背后的 **`Cargo.toml` 声明** 和 **源码里的 `#[cfg]`** 完整展开给你看，并解释仓库为什么要拆成四个 crate。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `Cargo.toml`（仓库根） | 定义 **workspace**：列出成员 crate、统一 `rust-version` 与 lint 规则。 |
| `axum/Cargo.toml` | `axum` 主 crate 的依赖与 `[features]`，是本讲「feature 裁剪」的主样本。 |
| `axum/src/lib.rs` | `axum` crate 的顶层文档与公开导出，能看到大量 `#[cfg(feature = ...)]` 如何「按 feature 决定导出什么」。 |
| `axum-core/Cargo.toml` | `axum-core` 的极简依赖与 features，体现「核心 crate 尽量少依赖」。 |
| `axum-core/src/lib.rs` | `axum-core` 顶层文档，明确建议「库作者优先依赖它」。 |
| `examples/hello-world/Cargo.toml` | 最小示例的依赖声明，是本讲代码实践的起点。 |

> 顺带一提：仓库根目录下还有 `axum-extra/` 和 `axum-macros/`，它们也通过 `axum-*` 通配被纳入 workspace，本讲在「crate 划分」一节会读它们的 `Cargo.toml`。

## 4. 核心概念与源码讲解

### 4.1 Cargo workspace：一个仓库管多个 crate

#### 4.1.1 概念说明

axum 不是一个单 crate 项目，而是一个 **workspace**：在一个 git 仓库里，同时维护四个互相依赖的 crate（`axum`、`axum-core`、`axum-extra`、`axum-macros`）。为什么不拆成四个独立仓库？因为它们**关系紧密、需要同步演进**——比如 `axum` 升级时往往要顺带改 `axum-core` 和 `axum-macros`。放在一个 workspace 里，可以用相对路径互相引用，改完一处就能一起编译、一起测试，不必等某个 crate 先发版到 crates.io。

对**使用者**来说，workspace 是「隐形」的：你只需要在 `Cargo.toml` 里写 `axum = "0.8"`，Cargo 会自动把它依赖的 `axum-core` 等拉进来。workspace 影响的是**项目维护者**的开发体验。

#### 4.1.2 核心流程

一个 workspace 的运作流程可以概括为：

1. 根 `Cargo.toml` 用 `[workspace]` 段声明「这个仓库是工作区」，并用 `members` 列出所有成员 crate。
2. 成员 crate 各自有自己的 `[package]`，是一个个独立可发布的 crate。
3. workspace 级别的配置（如统一的 `rust-version`、统一的 lint 规则）写在 `[workspace.package]` / `[workspace.lints]` 里，成员 crate 用 `{ workspace = true }` 继承。
4. 成员之间用 `path = "../xxx"` 互相引用，本地联调零成本。
5. 共享同一个 `Cargo.lock` 和 `target/` 目录，避免重复编译依赖。

#### 4.1.3 源码精读

先看根 `Cargo.toml` 的 workspace 声明：

- [Cargo.toml:1-L3](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/Cargo.toml#L1-L3) — `[workspace]` 段，`members = ["axum", "axum-*"]`。这里的 `"axum-*"` 是一个**通配模式**，表示「任何名字以 `axum-` 开头的子目录都自动算成员」。所以 `axum-core`、`axum-extra`、`axum-macros` 三个目录即使不逐个列出，也会被纳入 workspace。

> 注意：根目录下其实还有 `examples/` 和 `contrib/` 两个目录，但它们不匹配 `axum-*` 模式，所以不是 workspace 成员（`examples/` 里的示例各自独立、`publish = false`）。

接着看 workspace 级别如何统一配置：

- [Cargo.toml:5-L6](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/Cargo.toml#L5-L6) — `[workspace.package]` 段，`rust-version = "1.80"`。这是一个被所有成员继承的「最低 Rust 版本」声明。
- [Cargo.toml:8-L17](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/Cargo.toml#L8-L17) — `[workspace.lints]` 段，统一配置 lint 规则，例如 `unsafe_code = "forbid"`（禁止 unsafe）、`missing_docs = "warn"`（公共项必须有文档）。

成员 crate 如何「继承」这些配置？以 `axum` 为例：

- [axum/Cargo.toml:7-L7](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L7-L7) — `rust-version = { workspace = true }`，表示「我用 workspace 里定义的那个 `1.80`」。
- [axum/Cargo.toml:248-L249](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L248-L249) — `[lints] workspace = true`，继承上面那套统一的 lint 规则。这样四个 crate 的 lint 标准永远一致，改一处即可。

#### 4.1.4 代码实践

**实践目标**：亲手确认 axum 是一个多成员 workspace。

**操作步骤**：

1. 在仓库根目录运行 `cargo metadata --no-deps --format-version 1 > /tmp/meta.json`（只读元数据，不下载依赖）。
2. 在生成的 JSON 里搜索 `"members"` 字段，或在终端直接运行 `ls -d axum*`。
3. 对照上面的源码链接，确认 `members = ["axum", "axum-*"]` 实际展开后包含了哪些目录。

**需要观察的现象**：`members` 列表里应包含 `axum`、`axum-core`、`axum-extra`、`axum-macros` 四项，且**不包含** `examples`、`contrib`。

**预期结果**：四个成员 crate，符合 `"axum-*"` 通配的语义。

#### 4.1.5 小练习与答案

**练习 1**：如果维护者新增了一个 `axum-test-utils/` 目录，它会被自动纳入 workspace 吗？

**参考答案**：会。因为 `members` 里的 `"axum-*"` 通配会匹配任何 `axum-` 开头的目录，无需手动改 `Cargo.toml`。

**练习 2**：为什么 `examples/hello-world` 不算 workspace 成员？

**参考答案**：它的目录名是 `examples/hello-world`，既不是 `axum` 也不匹配 `axum-*` 模式；示例项目通常通过 `publish = false` 单独管理（见 [examples/hello-world/Cargo.toml:5-L5](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/examples/hello-world/Cargo.toml#L5-L5)）。

### 4.2 四个 crate 的职责划分

#### 4.2.1 概念说明

把 axum 拆成四个 crate，核心动机是**分层 + 稳定性**。不同 crate 的「变更频率」和「依赖范围」差别很大：

| crate | 一句话职责 | 依赖规模 | 变更频率 |
| --- | --- | --- | --- |
| `axum-core` | 最核心的类型与 trait（`FromRequest`、`IntoResponse`、`Body` 等） | 极小 | 最低，最稳定 |
| `axum` | 面向终端用户的完整框架（`Router`、`serve`、各种提取器/响应） | 较大 | 随需求演进 |
| `axum-macros` | 过程宏（`debug_handler`、`#[derive(FromRequest)]` 等） | proc-macro 专用 | 随语法糖演进 |
| `axum-extra` | 可选的「额外工具」（`CookieJar`、`TypedPath`、`Protobuf` 等） | 按需 | 较慢 |

**关键直觉**：越往下越稳定、依赖越少；越往上越丰富、依赖越多。`axum-core` 故意只依赖 `http`、`http-body`、`tower-layer`、`tower-service` 这些「业界 1.0 稳定」的基础 crate，于是它的 API 极少发生破坏性变更。

#### 4.2.2 核心流程

这四个 crate 的依赖关系是一条链：

```text
axum-macros ──(被 axum / axum-core / axum-extra 在 test 时依赖)
axum-core   ──(被 axum、axum-extra 依赖)        ← 最底层核心
axum        ──(终端用户依赖它)                  ← 主框架
axum-extra  ──(依赖 axum-core，可选增强)         ← 额外工具
```

终端用户只需要在 `Cargo.toml` 写 `axum = "0.8"`，Cargo 会自动把 `axum-core`（必需）拉进来；`axum-macros` 和 `axum-extra` 是按需的。

#### 4.2.3 源码精读

**先看 `axum-core`——最稳定的那一层。**

- [axum-core/Cargo.toml:3-L3](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/Cargo.toml#L3-L3) — `description = "Core types and traits for axum"`，定位就是「核心类型与 trait」。
- [axum-core/Cargo.toml:37-L47](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/Cargo.toml#L37-L47) — 看它的 `[dependencies]`：只有 `bytes`、`futures-core`、`http`、`http-body`、`http-body-util`、`mime`、`pin-project-lite`、`sync_wrapper`、`tower-layer`、`tower-service`。**没有任何 serde、tokio、hyper**——这就是它能保持极小、极稳定的原因。
- [axum-core/src/lib.rs:26-L28](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/lib.rs#L26-L28) — 它只导出三个公开模块：`body`、`extract`、`response`。也就是 `Body`、`FromRequest`/`IntoResponse` 这一层最基础的抽象。

正因为 `axum-core` 稳定，官方文档里有一条给库作者的明确建议：

- [axum/src/lib.rs:392-L397](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L392-L397) — 「如果你要给 axum 提供 `FromRequest` / `IntoResponse` 实现，应尽量依赖 `axum-core` 而不是 `axum`，因为它更少发生破坏性变更。」这条建议也写在 [axum-core/src/lib.rs:1-L9](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/lib.rs#L1-L9) 的顶部。

**再看 `axum` 主框架如何依赖 `axum-core`：**

- [axum/Cargo.toml:111-L111](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L111-L111) — `axum-core = { path = "../axum-core", version = "0.5.6" }`。注意它**同时**写了 `path`（本地开发用相对路径）和 `version`（发布到 crates.io 时用版本号）。`axum-core` 是**必需依赖**（没有 `optional = true`），因为它太基础了。

- [axum/src/lib.rs:523-L524](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L523-L524) — `axum` 直接把 `axum-core` 的几个类型 re-export 出来：`pub use axum_core::{BoxError, Error, RequestExt, RequestPartsExt};`。所以你用 `axum` 时，拿到的其实是 `axum-core` 里的东西。

**再看另外两个 crate 的定位：**

- [axum-macros/Cargo.toml:2-L2](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-macros/Cargo.toml#L2-L2) — `description = "Macros for axum"`。它是一个 **proc-macro crate**（过程宏只能在专门的 crate 里定义，且不能同时导出普通 item），负责 `debug_handler`、`#[derive(FromRequest)]` 等编译期代码生成。它对 `axum` 是**可选依赖**：
- [axum/Cargo.toml:131-L131](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L131-L131) — `axum-macros = { path = "../axum-macros", version = "0.5.1", optional = true }`，只有开启 `macros` feature 才会被拉入。
- [axum-extra/Cargo.toml:2-L3](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-extra/Cargo.toml#L2-L3) — `description = "Extra utilities for axum"`。它提供 `CookieJar`、`TypedPath`、`Protobuf` 等非核心的增强能力，依赖 `axum-core`（在 u10 会专门讲）。

#### 4.2.4 代码实践

**实践目标**：通过源码确认「`axum-core` 比 `axum` 依赖少得多」这一稳定性直觉。

**操作步骤**：

1. 打开 [axum-core/Cargo.toml:37-L47](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/Cargo.toml#L37-L47)，数一下 `[dependencies]` 里有多少项。
2. 打开 [axum/Cargo.toml:110-L144](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L110-L144)，对比 `axum` 的 `[dependencies]` 有多少项、是否包含 `serde_json`、`tokio`、`hyper` 这些「重量级」依赖。
3. 验证结论：`axum-core` 的依赖里**没有** `serde_json`、`tokio`、`hyper`，而 `axum` 有（只是有些标了 `optional = true`）。

**需要观察的现象**：`axum-core` 依赖列表很短且都是基础 IO/类型 crate；`axum` 依赖列表长很多。

**预期结果**：这就是「核心 crate 稳定、主框架丰富」的工程体现——想写第三方提取器的人依赖 `axum-core`，就不会被 `serde_json`、`tokio` 等牵着走。

#### 4.2.5 小练习与答案

**练习 1**：你在写一个第三方库，想给某个自定义类型实现 `FromRequestParts` 和 `IntoResponse`。你该依赖 `axum` 还是 `axum-core`？为什么？

**参考答案**：优先 `axum-core`。因为这两个 trait 就定义在 `axum-core` 里（见 [axum-core/src/lib.rs:26-L28](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/lib.rs#L26-L28)），且 `axum-core` 依赖更少、API 更稳定（[axum/src/lib.rs:392-L397](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L392-L397) 的官方建议）。

**练习 2**：为什么 `axum-macros` 必须是一个独立的 crate，而不能直接把宏写在 `axum` 里？

**参考答案**：Rust 规定**过程宏（`proc-macro = true`）必须放在独立的 crate 里**，这种 crate 不能再导出普通函数/类型。所以宏逻辑必须拆到 `axum-macros`，再由 `axum` 通过 `pub use axum_macros::{...}` 转发。

### 4.3 Cargo feature：按需裁剪能力

#### 4.3.1 概念说明

这是本讲最实用的一节。axum 提供了 HTTP/1、HTTP/2、JSON、WebSocket、文件上传（multipart）等一大堆能力，但**绝大多数应用用不到全部**。如果把所有功能都无条件编译进去，每个 axum 应用都会背上 `tokio-tungstenite`（WebSocket）、`multer`（multipart）这些沉重依赖。

Cargo feature 解决的就是这个问题：你可以在 `Cargo.toml` 里声明一组开关，**只有开启的 feature 对应的依赖和代码才会被编译**。这样「只用 HTTP/1 + JSON」的应用就不会编译 WebSocket 相关代码，产物更小、编译更快。

一个 feature 通常会同时做三件事：

1. 引入一个或多个 **optional dependency**（可选依赖）。
2. 打开这些依赖自身的某些子特性。
3. 在源码里通过 `#[cfg(feature = "xxx")]` 让对应的模块/类型「生效」。

#### 4.3.2 核心流程

**feature 的「布尔组合」原理**

一个 feature 是一组条件的布尔表达式。最典型的例子是 `serve` 模块的 gate——它需要 `tokio` **并且**（`http1` **或** `http2`）同时满足：

\[
\text{serve 存在} \iff \text{tokio} \land (\text{http1} \lor \text{http2})
\]

对应的源码是 `#[cfg(all(feature = "tokio", any(feature = "http1", feature = "http2")))]`。它的真值表如下：

| `tokio` | `http1` | `http2` | `serve` 是否存在 |
| --- | --- | --- | --- |
| 否 | 任意 | 任意 | 否（没有运行时就没法跑服务器） |
| 是 | 是 | 否 | 是 |
| 是 | 否 | 是 | 是 |
| 是 | 是 | 是 | 是 |
| 是 | 否 | 否 | **否**（有运行时但没有任何 HTTP 协议实现） |

> 这解释了 u1-l2 实践里「必须同时开 `tokio` 和 `http1`/`http2`」的原因——`serve` 在源码层就是这么要求的。

**`dep:` 前缀的原理（进阶）**

在 `axum/Cargo.toml` 里你会看到 `json = ["dep:serde_json", "dep:serde_path_to_error"]` 这种写法。这里的 `dep:` 前缀是 Cargo 在 resolver v2（也就是本仓库 `resolver = "2"`，见 [Cargo.toml:3-L3](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/Cargo.toml#L3-L3)）下引入的语法。它的作用是：**让可选依赖「不会因为同名 feature 被意外开启」**。如果不写 `dep:`，Cargo 会认为「开启 `json`」隐含「开启一个叫 `serde_json` 的 feature」并自动激活该依赖，容易造成依赖被意外拉入。加上 `dep:` 后，依赖只有在被 `dep:` 显式提及时才会启用，行为更可控。

#### 4.3.3 源码精读

**先看 `default` 默认开了哪些 feature：**

- [axum/Cargo.toml:40-L51](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L40-L51) — `default = ["form", "http1", "json", "matched-path", "original-uri", "query", "tokio", "tower-log", "tracing"]`。所以你写 `axum = "0.8"` 不加任何修饰时，这 9 个 feature 默认全开——这正是 `Json`、`Form`、`Query`、`axum::serve`「开箱即用」的原因。注意：`http2`、`ws`、`multipart`、`macros` **不在** default 里，需要手动开。

**再看几个具体 feature 如何「带依赖进来」：**

- [axum/Cargo.toml:61-L61](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L61-L61) — `json = ["dep:serde_json", "dep:serde_path_to_error"]`：开启 `json` 才会引入 `serde_json`。
- [axum/Cargo.toml:59-L60](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L59-L60) — `http1`/`http2` 分别开启 hyper 的对应协议支持。
- [axum/Cargo.toml:72-L78](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L72-L78) — `tokio` feature 引入 `hyper-util` 和 `tokio`，并打开 `tokio/rt`、`tower/make` 等子特性。
- [axum/Cargo.toml:81-L88](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L81-L88) — `ws`（WebSocket）引入 `tokio-tungstenite`、`sha1`、`base64` 等一串依赖，所以默认不开。

**最关键的一步：源码里 `#[cfg]` 如何让类型「凭 feature 出现/消失」。** 以 `Json` 为例，它在一个被 gate 的 `mod` 里：

- [axum/src/lib.rs:486-L489](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L486-L489) — `#[cfg(feature = "form")] mod form;` 和 `#[cfg(feature = "json")] mod json;`。整个 `json` 模块只有在 `json` feature 开启时才被编译。
- [axum/src/lib.rs:513-L515](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L513-L515) — `#[cfg(feature = "json")] pub use self::json::Json;`。`Json` 的 re-export 同样被 gate。**这两层 gate 叠加，就彻底决定了「关掉 `json` feature，`axum::Json` 这个类型就从源码里整个消失」。**

`axum::serve` 的 gate 印证了 4.3.2 的布尔公式：

- [axum/src/lib.rs:500-L501](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L500-L501) — `#[cfg(all(feature = "tokio", any(feature = "http1", feature = "http2")))] pub mod serve;`，`serve` 模块的定义。
- [axum/src/lib.rs:529-L531](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L529-L531) — `#[cfg(all(feature = "tokio", any(feature = "http1", feature = "http2")))] pub use self::serve::serve;`，`serve` 函数的导出。

提取器也遵循同样的 gate 模式：

- [axum/src/extract/mod.rs:67-L72](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/mod.rs#L67-L72) — `#[cfg(feature = "query")] mod query;` + `pub use self::query::Query;`。关掉 `query` feature，`Query` 提取器就没了。
- [axum/src/extract/mod.rs:49-L51](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/mod.rs#L49-L51) — `#[cfg(feature = "form")] pub use crate::form::Form;`。关掉 `form` feature，`Form` 提取器就没了。

最后，`axum` 的顶层文档里有一张完整的 feature 总览表，可作为速查：

- [axum/src/lib.rs:420-L441](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L420-L441) — 列出每个 feature 的名字、说明、是否默认开启（带 ✔ 的为默认 feature）。例如 `http2`、`ws`、`multipart`、`macros` 没有 ✔，说明它们非默认。

#### 4.3.4 代码实践

**实践目标**：用源码阅读的方式，确认「关掉某个 feature，对应类型/模块就消失」。

**操作步骤**：

1. 打开 [axum/src/lib.rs:513-L515](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L513-L515)，确认 `Json` 的导出挂在 `#[cfg(feature = "json")]` 上。
2. 打开 [axum/src/lib.rs:529-L531](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L529-L531)，确认 `serve` 的导出挂在 `#[cfg(all(feature = "tokio", any(feature = "http1", feature = "http2")))]` 上。
3. 自己推断：如果用 `--no-default-features --features tokio`（只开 `tokio`、不开任何 http 协议），`axum::serve` 还能用吗？

**需要观察的现象**：仅从源码就能看出，`serve` 需要 `tokio` **且**（`http1` 或 `http2`），缺一不可。

**预期结果**：只开 `tokio` 时 `serve` 不存在，编译会报 `cannot find function serve` 之类的错误。这是一个纯源码阅读型实践，无需运行即可得出确定结论。

#### 4.3.5 小练习与答案

**练习 1**：写一个 `Cargo.toml`，让 axum 只支持 HTTP/2 + JSON，不要 HTTP/1、不要 WebSocket。

**参考答案**：
```toml
[dependencies]
axum = { version = "0.8", default-features = false, features = ["http2", "json", "tokio"] }
```
注意必须同时保留 `tokio`，否则 `serve` 因布尔公式不成立而消失。

**练习 2**：为什么把 `axum` 设为 `default-features = false` 后，连 `axum::serve` 都可能消失？

**参考答案**：因为 `default` 里包含 `tokio` 和 `http1`（[axum/Cargo.toml:40-L51](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L40-L51)）。关掉 default 后，`serve` 的两个前提条件都不满足了，必须手动补 `tokio` +（`http1`/`http2`）才能恢复。

**练习 3**：`ws` feature 为什么默认不开？

**参考答案**：它会拉入 `tokio-tungstenite`、`sha1`、`base64` 等额外依赖（[axum/Cargo.toml:81-L88](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L81-L88)）。不是所有应用都需要 WebSocket，默认关闭可以减少编译时间和产物体积——这正是 feature 裁剪的意义。

## 5. 综合实践

本实践把三个模块串起来：在本地新建一个 axum 项目，**用两种 feature 组合分别编译**，对比差异，并亲手触发「关掉 feature 导致类型消失」的编译错误。这是贯穿本讲的核心任务。

**实践目标**：直观感受 feature 对「依赖、编译时间、可用类型」的影响。

**操作步骤**：

1. 新建项目（任选其一）：
   - 方式 A（用官方示例当模板）：把 `examples/hello-world/` 整个目录复制到一个独立目录，它的依赖是 `axum = { path = "../../axum" }`（见 [examples/hello-world/Cargo.toml:7-L9](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/examples/hello-world/Cargo.toml#L7-L9)）。复制后把 `path` 改成线上版本 `axum = "0.8"`。
   - 方式 B（全新项目）：`cargo new my-axum && cd my-axum && cargo add axum --features http1,tokio && cargo add tokio --features full`。

2. **第一次编译（默认 feature 基线）**：在项目根目录执行 `cargo clean && cargo build --release 2>&1 | tail -5`，记录编译耗时和最后产物大小。
   - 预期：默认开 9 个 feature（含 `json`/`form`/`query`/`tracing` 等），会编译 `serde_json`、`serde_html_form`、`tracing` 等依赖。
   - **具体编译时间与产物大小：待本地验证**（取决于机器，但默认组合必然比下面的精简组合编译更多依赖）。

3. **给项目加一段用到 `Json` 的 handler**，例如：
   ```rust
   use axum::{routing::post, Router, Json};
   use serde_json::Value;

   async fn echo(Json(v): Json<Value>) -> Json<Value> { Json(v) }

   // 在 main 里：.route("/echo", post(echo))
   ```
   先确认默认 feature 下它能编译通过。

4. **第二次编译（精简 feature）**：把 `Cargo.toml` 里 axum 改成
   ```toml
   axum = { version = "0.8", default-features = false, features = ["http1", "tokio"] }
   ```
   然后 `cargo clean && cargo build --release`。
   - **预期现象**：编译会**失败**，错误形如 `cannot find type Json in module axum` 或 `unresolved import axum::Json`。
   - 同时 `cargo build` 不再编译 `serde_json`、`serde_html_form` 等依赖，编译依赖列表明显变短。

5. **解释原因**：对照源码 [axum/src/lib.rs:488-L489](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L488-L489)（`#[cfg(feature = "json")] mod json;`）和 [axum/src/lib.rs:513-L515](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L513-L515)（`#[cfg(feature = "json")] pub use self::json::Json;`），说明：精简组合里没有 `json` feature，所以整个 `json` 模块和 `Json` 的导出都没有被编译进来——这就是 `Json` 不可用的根本原因。
   - 进一步推论：在这个精简组合下，`Form`（[axum/src/extract/mod.rs:49-L51](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/mod.rs#L49-L51)）、`Query`（[axum/src/extract/mod.rs:67-L72](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/mod.rs#L67-L72)）同样不可用；但 `axum::serve` 仍然可用（`tokio` + `http1` 满足 [axum/src/lib.rs:529-L531](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L529-L531) 的 gate）。

6. （可选）**恢复并对比编译时间**：把第 3 步的 `echo` handler 注释掉，让精简组合也能编译通过，再次 `cargo clean && cargo build --release`，与第 2 步的默认组合对比编译耗时和产物大小。
   - 预期方向：精简组合编译更快、产物更小（因为少编译了 `serde_json`、`serde_html_form`、`serde_path_to_error`、`tracing` 等依赖）。
   - **具体差值：待本地验证**。

**需要观察的现象**：
- 默认组合：`Json` 可用，编译依赖较多。
- 精简组合：`Json` 不可用（编译报错），编译依赖明显变少。

**预期结果**：你应当能用一句话总结——「feature 就是一组条件编译开关，关掉 `json` feature 等于把 `json` 模块从源码里整段删掉，所以 `axum::Json` 不复存在」。

> 说明：本实践涉及实际下载依赖与编译，具体耗时/产物体积因机器而异，相关数字标注为「待本地验证」；但「关掉 feature → 类型消失」这一结论由源码中的 `#[cfg]` 直接决定，是确定无疑的。

## 6. 本讲小结

- axum 是一个 **workspace**，根 `Cargo.toml` 用 `members = ["axum", "axum-*"]`（[Cargo.toml:1-L3](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/Cargo.toml#L1-L3)）把四个 crate 统一管理，共享 `Cargo.lock`、`target/`、lint 规则。
- 四个 crate 分层清晰：`axum-core`（最稳定的核心 trait）→ `axum`（完整框架）→ `axum-extra`（可选增强）、`axum-macros`（过程宏）。库作者应优先依赖 `axum-core`（[axum/src/lib.rs:392-L397](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L392-L397)）。
- `default` feature 默认开 9 项（[axum/Cargo.toml:40-L51](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L40-L51)），所以 `Json`/`Form`/`Query`/`serve` 开箱即用；`http2`/`ws`/`multipart`/`macros` 默认不开。
- feature 通过 `#[cfg(feature = "xxx")]` 真正「裁剪代码」，例如 `Json` 被 `#[cfg(feature = "json")]` 双层 gate（[axum/src/lib.rs:488-L489](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L488-L489)、[axum/src/lib.rs:513-L515](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L513-L515)），关掉 feature 类型即消失。
- `axum::serve` 的存在性是一个布尔公式 `tokio ∧ (http1 ∨ http2)`（[axum/src/lib.rs:529-L531](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L529-L531)），缺任一项都会让 `serve` 不可用。
- `dep:` 前缀（resolver v2）让可选依赖不会被同名 feature 意外开启，行为更可控。

## 7. 下一步学习建议

到这里，你已经从「工程组织」层面看懂了 axum 的仓库结构、crate 分层和 feature 裁剪机制。接下来的 u1-l4「Handler 与 Router 初体验」会重新回到**代码**层面，正式建立 `Router<S>` 与 `Handler` trait 的心智模型，把本讲「为什么某些类型默认可用」和前两讲「serve 怎么跑起来」串成一条完整的「请求 → 路由 → handler」链路。

建议继续阅读的源码：

- [axum/src/lib.rs:399-L413](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L399-L413) — 「Required dependencies」一节，说明终端用户至少需要 `axum` + `tokio` + `tower` 三件套。
- [axum/src/handler/mod.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs) — 为 u1-l4 预习 `Handler` trait 的定义。
- [axum/src/docs/handlers_intro.md](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/handlers_intro.md) — 官方对 handler 的入门说明，u1-l4 会用到。
