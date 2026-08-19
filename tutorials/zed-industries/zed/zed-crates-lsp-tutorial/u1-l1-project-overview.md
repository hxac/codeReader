# 项目定位与 crate 全貌（u1-l1）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `lsp` crate 在 Zed workspace 中的位置与角色——它是 Zed 与语言服务器（Language Server）进程通信的基础库。
2. 理解 `pub use lsp_types::*;` 整体再导出模式：为什么下游 crate 只依赖 `lsp` 一个 crate，就能使用全部 LSP 协议类型。
3. 读懂 `crates/lsp/Cargo.toml` 中的 `[lib] path`、`test-support` feature 与关键依赖的作用。
4. 说清 `src/lsp.rs` 与 `src/input_handler.rs` 两个源码文件的职责划分，为后续讲义建立「源码地图」。

本讲不涉及任何运行时行为（启动进程、收发消息都在后续讲义），只解决一个问题：**这个 crate 是什么、由哪些文件组成、类型从哪里来**。

## 2. 前置知识

### 2.1 什么是 LSP（Language Server Protocol）

写代码时的自动补全、跳转定义、诊断报错，如果每种语言都要在编辑器里重新实现一遍，成本极高。LSP 是微软提出的一套标准协议，把「语言智能」从编辑器中剥离出去：

- **语言服务器（Language Server）**：一个独立进程（例如 `rust-analyzer`、`pyright`），负责分析代码。
- **编辑器（客户端）**：例如 Zed，把用户的操作（打开文件、修改文本）通过协议发给服务器，再把结果（补全列表、诊断）展示给用户。
- **通信方式**：LSP 基于 **JSON-RPC 2.0**，最常见的传输方式是 **stdio**——客户端把 JSON 消息写进子进程的 stdin，从子进程的 stdout 读回消息。每条消息前有一个 `Content-Length: N` 头，标明 JSON 正文的字节数（这叫「分帧」，第 u1-l3 讲会精读）。

### 2.2 什么是 lsp-types

[`lsp-types`](https://github.com/gluon-lang/lsp-types) 是社区维护的 Rust crate，把 LSP 规范中的每个类型（`Position`、`Uri`、`ServerCapabilities`……）都定义成了带 serde 派生的结构体。**注意**：Zed 并不直接用官方发布版，而是使用自己的 fork（原因见 4.1.3）。

### 2.3 Rust 的 Cargo workspace 与再导出

- **workspace**：一个仓库里放多个 crate，共享依赖版本与构建缓存。Zed 仓库有上百个 crate。
- **`pub use` 再导出（re-export）**：一个 crate 可以把依赖 crate 的公开项「转发」出去。下游只需依赖转发者，无需直接依赖原始 crate。`pub use lsp_types::*;` 是整体（glob）再导出，把 `lsp_types` 的全部公开项变成 `lsp` crate 命名空间的一部分。
- **glob 导入与本地定义的优先级**：Rust 规定，模块内显式定义的名字**优先于** glob 导入进来的同名名字。这一点在 4.2.3 会看到真实案例（`RequestId`）。

## 3. 本讲源码地图

整个 `lsp` crate 只有两个源码文件，加上两个清单文件：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `crates/lsp/Cargo.toml` | 44 | crate 清单：库名、lib 路径、feature、依赖 |
| `crates/lsp/src/lsp.rs` | 2453 | **crate 根**（库入口）。声明子模块、再导出 lsp-types、定义 `LanguageServer` 运行时与全部 RPC 机制；文件尾部是约 350 行的 `mod tests` |
| `crates/lsp/src/input_handler.rs` | 213 | **私有子模块**（`mod input_handler;`，非 `pub`）。负责从服务器 stdout 字节流中按 `Content-Length` 分帧读出一条条 JSON-RPC 消息 |
| 仓库根 `Cargo.toml` | — | workspace 配置：成员列表、`lsp` 的 path 依赖、`lsp-types` fork 的 git 地址 |

> 提示：Zed 遵循「不使用 `mod.rs`、用描述性文件名做 lib 路径」的约定，所以库入口叫 `src/lsp.rs` 而不是 `src/lib.rs`（这正是 `Cargo.toml` 里 `[lib] path = "src/lsp.rs"` 那一行的来源）。

## 4. 核心概念与源码讲解

### 4.1 lsp crate 在 Zed workspace 中的定位

#### 4.1.1 概念说明

Zed 是一个多语言编辑器，但「语言支持」被刻意分了层：

- `lsp` crate：**只管协议与进程通信**——怎么启动服务器进程、怎么按 JSON-RPC 收发消息、怎么处理超时和取消。它不关心「补全菜单长什么样」，甚至不关心「这是哪种语言」。
- `language`、`project`、`languages` 等 crate：在 `lsp` 之上构建编辑器侧的语言模型（buffer、快照、诊断聚合）。
- `editor`、`copilot`、`prettier`、`extension` 等 crate：把语言能力接进 UI 或扩展系统。

一个快速的事实可以说明它的基础地位：workspace 中有 20 多个 crate 声明了 `lsp.workspace = true`，包括 `project`、`language`、`editor`、`copilot`、`prettier`、`extension`、`diagnostics`、`search` 等。`lsp` 是它们共同的协议底座。

#### 4.1.2 核心流程

从构建系统视角看，`lsp` crate 的「定位」由三层配置固定下来：

1. workspace 根 `Cargo.toml` 把 `crates/lsp` 列入 `members`，workspace 才会构建它。
2. 根 `Cargo.toml` 的 `[workspace.dependencies]` 声明 `lsp = { path = "crates/lsp" }`，让其他 crate 用 `lsp.workspace = true` 统一引用这一个副本。
3. `crates/lsp/Cargo.toml` 自己声明库名为 `lsp`、入口为 `src/lsp.rs`，并给出依赖与 feature。

依赖方向的示意：

```
editor ─┐
project ├─→ language ─→ lsp ─→ lsp-types (zed fork, 协议类型)
copilot ─┘             │
                       ├─→ gpui (执行器/Task/AsyncApp)
                       └─→ util  (子进程封装、超时工具、脱敏)
```

#### 4.1.3 源码精读

先看 crate 自己的清单：

[Cargo.toml:L1-L16](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/Cargo.toml#L1-L16)
这段定义了包名 `lsp`、版本、`[lints] workspace = true`（沿用 workspace 统一的 lint 配置），以及两处关键配置：`[lib]` 与 `[features]`。

[Cargo.toml:L11-L13](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/Cargo.toml#L11-L13)
`path = "src/lsp.rs"` 把库入口从默认的 `src/lib.rs` 改为 `src/lsp.rs`（描述性命名，与 Zed 的仓库规范一致）；`doctest = false` 表示 `cargo test` 不运行本 crate 文档注释里的代码示例——本 crate 的文档示例很少，关掉可以节省测试时间。

[Cargo.toml:L15-L16](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/Cargo.toml#L15-L16)
定义唯一的 feature：`test-support = ["async-pipe"]`。打开它会启用可选依赖 `async-pipe`（内存管道），并解锁 `FakeLanguageServer`——一个不需要真实进程的假语言服务器，供本 crate 与下游 crate 写测试（详见 u4-l3）。

[Cargo.toml:L18-L35](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/Cargo.toml#L18-L35)
运行时依赖。几个值得记的：`lsp-types`（协议类型来源）、`gpui`（Zed 自研 UI/异步框架，这里只用它的执行器与 `Task`）、`async-channel`（有界/无界 channel）、`postage`（提供 barrier 原语，用于优雅关闭）、`util`（子进程启动与 `ConnectionResult` 等）、`parking_lot`（同步锁）、`serde`/`serde_json`（序列化）。

[Cargo.toml:L37-L44](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/Cargo.toml#L37-L44)
开发依赖（只在测试时生效）：`ctor`（在测试进程启动时自动初始化 zlog 日志）、带 `test-support` feature 的 `gpui` 与 `util`、`semver`、`zlog`。注意 `async-pipe` 同时出现在普通依赖（optional）与 dev-dependencies 里——测试代码无条件需要它，而对外只在 `test-support` feature 下暴露。

再看 workspace 根的两处：

[Cargo.toml:L132](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/Cargo.toml#L132)
`"crates/lsp",` 出现在 workspace `members` 列表中——没有这一行，`cargo build -p lsp` 根本找不到这个包。

[Cargo.toml:L398](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/Cargo.toml#L398)
`lsp = { path = "crates/lsp" }` 在 `[workspace.dependencies]` 中，是所有下游 `lsp.workspace = true` 的唯一出处，保证全仓库只有一份 `lsp`。

[Cargo.toml:L678](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/Cargo.toml#L678)
`lsp-types` 指向 Zed 自己 fork 的 git 仓库并锁定 rev `f4dfa89a`。这就是 4.2 要讲的「再导出」必须由 `lsp` 统一把关的原因之一：协议类型的版本被固定在 workspace 级，下游不可能引入另一份不一致的 `lsp-types`。

#### 4.1.4 代码实践

**实践目标**：在本地把 `lsp` crate 单独编译通过，并观察它的直接依赖与文档结构。

**操作步骤**：

1. 克隆 Zed 仓库后，在仓库根目录运行：

   ```bash
   cargo check -p lsp
   ```

   `-p lsp`（package 选择器）让你只编译这一个 crate，而不必先编译整个编辑器（workspace 的 `default-members` 只有 `crates/zed`，所以必须显式 `-p`）。
2. 生成并浏览本 crate 的 API 文档：

   ```bash
   cargo doc -p lsp --no-deps
   ```

   然后打开 `target/doc/lsp/index.html`。
3. 查看直接依赖树：

   ```bash
   cargo tree -p lsp --depth 1
   ```

**需要观察的现象**：

- `cargo check` 能独立通过，说明 `lsp` 是自包含的库，不依赖编辑器 UI 代码。
- `cargo doc` 生成的文档里能看到大量并非本 crate 编写的类型（如 `Position`、`ServerCapabilities`）——它们来自再导出（见 4.2）。
- `cargo tree --depth 1` 列出的直接依赖应与 [Cargo.toml:L18-L35](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/Cargo.toml#L18-L35) 一一对应。

**预期结果**：三条命令均成功；依赖树第一层包含 `lsp-types v* (https://github.com/zed-industries/lsp-types?rev=f4dfa89a...)` 这样的 git 依赖。具体输出**待本地验证**（本讲义编写环境未执行构建）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `prettier`、`copilot` 这类与「语言服务器」表面无关的 crate 也依赖 `lsp`？

<details><summary>参考答案</summary>

因为它们本质上都是「通过 stdio 上的 JSON-RPC 与一个子进程通信」的客户端：Copilot 的 agent 进程、Prettier 的格式化服务进程，都复用了 `lsp` 提供的进程启动、消息分帧、请求-响应与超时机制。`lsp` 管的是「协议运行时」，不限于传统意义的服务器。
</details>

**练习 2**：如果删掉根 `Cargo.toml` 中的 `lsp = { path = "crates/lsp" }`，会发生什么？

<details><summary>参考答案</summary>

所有写着 `lsp.workspace = true` 的下游 crate 会解析失败——`workspace = true` 的含义就是「从 `[workspace.dependencies]` 取这个依赖」。而 `members` 里的 `"crates/lsp"` 只决定 workspace 是否构建它，两者缺一不可。
</details>

### 4.2 src/lsp.rs 文件头部：再导出与类型一览

#### 4.2.1 概念说明

`src/lsp.rs` 的头四行是理解整个 crate 使用方式的钥匙：

- 第 1 行声明私有子模块 `input_handler`；
- 第 3、4 行把 `lsp_types` 的公开项**整体再导出**。

这是一种**门面（facade）模式**：`lsp` crate 站在 `lsp-types` 前面，对下游承诺「你需要的所有协议类型，从我这取」。好处有两个：

1. **版本一致**：`lsp-types` 是 git fork + 锁定 rev（见 4.1.3），由 workspace 统一管理，下游不会各自引入不兼容版本。
2. **API 收敛**：`lsp` 在协议类型之上补充了 `LanguageServer`、`LanguageServerName` 等运行时类型，下游一套 `use lsp::...` 就能同时拿到「数据类型」和「行为类型」。

同时要建立一个重要意识：`lsp` 命名空间里的类型分**两个来源**——大部分是 `lsp_types` 的再导出，少数是 `lsp.rs` 本地定义（`RequestId`、`LanguageServerId`、`LanguageServerName`、`LanguageServerBinary` 等）。读代码时要能区分。

#### 4.2.2 核心流程

自上而下读 `lsp.rs` 头部，布局依次是：

```
第 1 行    mod input_handler;          ← 声明私有子模块（stdout 读取器）
第 3-4 行  pub use lsp_types::...      ← 协议类型整体再导出
第 6-42 行 use ...                     ← 引入 futures/gpui/serde/util 等外部依赖
第 44-58 行 常量                        ← JSON-RPC 版本、分帧头、超时默认值
第 60 行起  函数与类型                   ← workspace_folder_for_uri、handler 表类型、
                                          LanguageServerBinary、LanguageServer ...
第 2095 行起 mod tests                  ← 全部测试（FakeLanguageServer 就在这层）
```

一条判断类型来源的经验规则：**在 [src/lsp.rs:L3-L4](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L3-L4) 之后、crate 内又显式 `struct/enum` 定义的同名类型，会遮蔽再导出进来的同名类型**（Rust 中显式定义优先于 glob 导入）。

#### 4.2.3 源码精读

[lsp.rs:L1-L4](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1-L4)
这四行是 crate 的「目录页」：`mod input_handler;` 把 `src/input_handler.rs` 纳入编译（没有 `pub`，外部不可见）；`pub use lsp_types::request::*;` 把 request 模块里的请求标记 trait（`request::Initialize`、`request::Hover` 这类）平铺到 crate 根，于是 `lsp::request::Hover` 与扁平路径两种写法都可用；`pub use lsp_types::*;` 再整体导出全部协议类型——`Uri`、`Position`、`ServerCapabilities`、`notification` 模块等都经由此进入 `lsp` 命名空间。

[lsp.rs:L44-L58](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L44-L58)
头部常量区：`JSON_RPC_VERSION = "2.0"` 与 `CONTENT_LEN_HEADER = "Content-Length: "` 是协议层的两个字符串（后者供 input_handler 分帧用）；`DEFAULT_LSP_REQUEST_TIMEOUT_SECS = 120`、`SERVER_SHUTDOWN_TIMEOUT = 5s` 是两条重要的时间预算（请求默认 120 秒、关闭服务器最多等 5 秒，后续讲义会用到）。

[lsp.rs:L92-L110](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L92-L110)
`LanguageServerBinary` 描述「如何启动一个服务器」：可执行文件路径、参数、环境变量——它既可以是独立二进制，也可以是「运行时 + 脚本」组合（如 `node some-server.js --stdio`）。这是本地定义、而非再导出的类型。

[lsp.rs:L114-L143](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L114-L143)
`LanguageServer` 结构体——本 crate 的主角。字段大致分四组：进程身份（`server_id`、`name`、`binary`、`server: Arc<Mutex<Option<Child>>>`）、出站通道（`outbound_tx`、`notification_tx`）、入站分发（`notification_handlers`、`response_handlers`、`io_handlers`、`pending_respond_tasks`）、协商状态（`capabilities`、`workspace_folders`、`root_uri`）。本讲只需混个眼熟，每个字段在 u2/u3 讲义中都会展开。

[lsp.rs:L145-L199](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L145-L199)
另一组本地类型：`LanguageServerId`（运行中服务器的数字标识，`#[repr(transparent)]` 包着 `usize`）与 `LanguageServerName`（包装 `SharedString` 的服务器名，实现了 `Display`/`AsRef<str>`，带 `#[serde(transparent)]`）。注意 `LanguageServerId::from_proto`/`to_proto` 这类方法名暗示它还会跨进程/协议边界使用。

[lsp.rs:L228-L233](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L228-L233)
`RequestId` 是「本地定义遮蔽再导出」的活例子：`lsp_types` 自己也有 `RequestId`，但 `lsp.rs` 在 glob 再导出之后又显式定义了一个 `#[serde(untagged)] enum RequestId { Int(i32), Str(String) }`（带 `Hash`/`Eq`，便于做 handler 表的键）。按 Rust 的优先级规则，crate 内与下游看到的 `lsp::RequestId` 都是这一个本地版本。

[lsp.rs:L2095-L2103](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2095-L2103)
文件尾部 `mod tests` 的开头：`use super::*;` 引入上层全部内容，`#[ctor::ctor(unsafe)]` 在测试二进制加载时自动调用 `zlog::init_test()` 初始化日志。本 crate 的所有实践都以这个模块为样板。

#### 4.2.4 代码实践

**实践目标**：亲手验证「不直接依赖 lsp-types 也能使用全部协议类型」，并区分再导出类型与本地类型。

**操作步骤**：

1. 打开 [crates/lsp/src/lsp.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2095) 的 `mod tests`，在任意位置临时添加下面的测试（**示例代码**，非项目原有代码；练习完成后请用 `git checkout -- crates/lsp/src/lsp.rs` 还原，不要提交）：

   ```rust
   #[test]
   fn test_reexported_and_local_types() {
       // Uri、Position 来自 lsp_types 的整体再导出
       let uri = Uri::from_str("file:///tmp/main.rs").unwrap();
       assert_eq!(uri.as_str(), "file:///tmp/main.rs");

       let position = Position { line: 3, character: 7 };
       assert_eq!(position.line, 3);
       assert_eq!(position.character, 7);

       // LanguageServerName 是 lsp.rs 本地定义的类型（见 L171）
       let name = LanguageServerName::from_proto("rust-analyzer".to_string());
       assert_eq!(name.as_ref(), "rust-analyzer");
       assert_eq!(name.to_string(), "rust-analyzer");
   }
   ```

   说明：`from_str` 需要 `use std::str::FromStr;`——tests 模块在第 2098 行已经导入，直接可用；普通 `#[test]`（而非 `#[gpui::test]`）就够了，因为这个测试不涉及 gpui 上下文，与 [lsp.rs:L2367-L2368](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2367-L2368) 的既有测试风格一致。
2. 在仓库根运行：

   ```bash
   cargo test -p lsp test_reexported_and_local_types
   ```

3. 观察编译器行为的小实验：把 `Uri::from_str` 改成 `lsp_types::Uri::from_str` 再编译一次。

**需要观察的现象**：

- 测试编译通过且断言全部成立——`Uri`、`Position` 无需任何额外 `use` 即可使用（glob 再导出生效）。
- 第 3 步会得到「`lsp_types` 未被声明为 crate 或成员」之类的错误：`lsp.rs` 自己并没有 `use lsp_types` 之外的直接引用权限？——实际原因值得玩味：`lsp_types` 是本 crate 的依赖，`cargo` 层面可用，但该路径写法能否编译取决于 2018 版后的外部 crate 路径规则，此处预期会正常解析。**待本地验证**，请以编译器实际输出为准。

**预期结果**：测试通过；你在下游项目中如果也这样做（依赖 `lsp` 而不依赖 `lsp-types`），同样能直接使用 `lsp::Uri` 等类型。运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`pub use lsp_types::request::*;`（第 3 行）和 `pub use lsp_types::*;`（第 4 行）各自解决什么问题？为什么两行都要写？

<details><summary>参考答案</summary>

第 4 行整体再导出让 `lsp::Position`、`lsp::Uri`、`lsp::notification::ShowMessage` 这类路径可用，但它只引入 `request` **模块本身**，不会把模块内容平铺到根上。第 3 行额外把 `request` 模块内的请求标记 trait 平铺，于是 `request::Initialize` 这类 trait 既可以用模块路径 `lsp::request::Initialize` 引用，也可以扁平引用。两行配合，下游写法更灵活。
</details>

**练习 2**：`lsp::LanguageServerName` 和 `lsp::Uri` 分别来自哪里？如何快速验证？

<details><summary>参考答案</summary>

`LanguageServerName` 是 `lsp.rs` 本地定义（L171），`Uri` 来自 `lsp_types` 再导出。验证方法：在 [src/lsp.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L171) 里能搜到 `pub struct LanguageServerName` 的定义体，而 `Uri` 在本 crate 内没有任何定义，只能来自第 4 行的 glob 再导出。
</details>

**练习 3**：为什么 `RequestId` 要在 `lsp.rs` 里重新定义，而不是直接用 `lsp_types` 的版本？

<details><summary>参考答案</summary>

从定义体 [lsp.rs:L228-L233](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L228-L233) 可以看出它派生了 `Eq`、`PartialEq`、`Hash`——本 crate 要用它做 `response_handlers: HashMap<RequestId, ...>` 的键（L131），需要稳定可哈希的形态；`untagged` serde 则让它既能与 JSON 里的数字 id 又能与字符串 id 互转。这是 Zed fork 之外、按自身需求定制的协议细节。
</details>

### 4.3 src/input_handler.rs：子模块声明与职责划分

#### 4.3.1 概念说明

`src/input_handler.rs` 是被 `mod input_handler;`（`lsp.rs` 第 1 行）引入的**私有**子模块。它只负责一件事：把「服务器 stdout 上的一串字节」变成「一条条已解析的消息」。

为什么要单独拆一个文件？

- **关注点分离**：分帧（找 `Content-Length` 头、按长度读正文）是纯 IO 解析逻辑，与 `LanguageServer` 的 RPC 状态管理（handler 表、超时、取消）性质不同。
- **可测试性**：分帧逻辑只依赖 `AsyncRead` 抽象，不依赖真实进程，可以喂数据直接测（见 u1-l3 的实践）。

它暴露给 crate 根的唯一类型是 `LspStdoutHandler`，crate 内部通过 `crate::{...}` 路径反向引用根模块的类型——这种「根模块定义共享类型、子模块专注算法」的结构在小型 crate 中很常见。

#### 4.3.2 核心流程

子模块与根模块的协作关系：

```
src/lsp.rs（crate 根）                     src/input_handler.rs（私有子模块）
────────────────────────                  ─────────────────────────────
mod input_handler;                 ──▶     整个文件被纳入编译
定义 RequestId / AnyResponse /
        NotificationOrRequest /
        IoKind / IoHandler /
        CONTENT_LEN_HEADER ...     ◀──     use crate::{ AnyResponse, CONTENT_LEN_HEADER,
                                               IoHandler, IoKind, NotificationOrRequest,
                                               RequestId, ResponseHandler };
                                        （LspStdoutHandler 在此实现，
                                         stdout 字节 → 消息的分帧读取）
```

后续（u2-l2 详讲）：`LanguageServer::new_internal` 会把服务器进程的 stdout 交给 `LspStdoutHandler::new`，后者启动一个后台任务循环读帧，把「通知/请求」推进有界 channel，把「响应」直接派发给根模块的 `response_handlers` 表。

#### 4.3.3 源码精读

[lsp.rs:L1](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1)
`mod input_handler;`——没有 `pub`，所以 `LspStdoutHandler` 对下游 crate 不可见，纯属内部实现细节。

[input_handler.rs:L15-L18](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L15-L18)
子模块用 `use crate::{...}` 从根模块借来 7 个名字。这份清单就是两个文件的「接口面」：`AnyResponse`、`NotificationOrRequest`（消息形状）、`RequestId`（消息 id）、`ResponseHandler`（响应回调类型）、`IoHandler`/`IoKind`（IO 观测钩子）、`CONTENT_LEN_HEADER`（分帧头前缀）。

[input_handler.rs:L22-L27](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L22-L27)
`INCOMING_MESSAGE_QUEUE_CAPACITY = 128`，`pub(crate)` 可见。注释解释了它的用途：限制后台读取器与前台分发器之间缓冲的消息数，队列满时读取器停止读 stdout，让 OS 管道对服务器产生**背压**（backpressure），避免前台卡住时内存被无界撑爆。这是 u4-l1 的主角，本讲先记住「有界队列」这个设计即可。

[input_handler.rs:L29-L33](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L29-L33)
`LspStdoutHandler` 只有两个 `pub(super)` 字段：`loop_handle`（读取循环的任务句柄）与 `incoming_messages`（装解析后消息的 channel 接收端）。`pub(super)` 意味着只有 crate 根（`lsp.rs`）能访问——再次印证这是内部实现。

[input_handler.rs:L35-L50](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L35-L50)
`read_headers` 函数：增量读取直到缓冲区以 `\r\n\r\n`（`HEADER_DELIMITER`，第 20 行）结尾，即 HTTP 风格头部结束符。读不到任何字节（返回 0）则报错「cannot read LSP message headers」。具体逐行精读放在 u1-l3。

#### 4.3.4 代码实践

**实践目标**：通过「源码阅读型实践」建立两个文件之间的接口地图，为后续所有讲义定位代码做准备。

**操作步骤**：

1. 打开 [input_handler.rs:L15-L18](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L15-L18) 的 `use crate::{...}` 清单。
2. 对清单中的每个名字，在 `src/lsp.rs` 中找到它的定义位置，填入下表（前三行已给出答案作为示范）：

   | 名字 | 在 src/lsp.rs 中的定义处 | 一句话作用 |
   | --- | --- | --- |
   | `CONTENT_LEN_HEADER` | L45 | `"Content-Length: "` 常量 |
   | `IoKind` | L86-L90 | stdin/stdout/stderr 三种流的标签枚举 |
   | `RequestId` | L230-L233 | 消息 id（Int/Str 二态） |
   | `AnyResponse` | 待填写 | |
   | `ResponseHandler` | 待填写 | |
   | `IoHandler` | 待填写 | |
   | `NotificationOrRequest` | 待填写 | |

3. 用一张手绘或文本图把关系画出来：`LanguageServer`（根模块）→ 持有 stdout → 交给 `LspStdoutHandler`（子模块）→ 产出的消息回流到根模块的 handler 表。

**需要观察的现象**：所有 7 个名字都能在 `lsp.rs` 中找到定义（其中部分是 `pub`、部分是 crate 私有）；`input_handler.rs` 自身除 `LspStdoutHandler` 与 `read_headers` 外几乎不定义共享类型。

**预期结果**：得到一张完整的「根模块类型 ↔ 子模块使用」对照表。本实践为纯阅读，无需运行，答案可直接在源码中核对（定义行号见上表与 4.3.3）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `mod input_handler;` 不加 `pub`？加了会有什么问题？

<details><summary>参考答案</summary>

分帧读取是内部实现细节，下游不需要也不应该依赖。若声明为 `pub mod`，`LspStdoutHandler` 等类型就会进入公共 API，今后任何改动（哪怕只是字段调整）都可能破坏下游编译，违背「小而稳的公共接口」原则。
</details>

**练习 2**：`input_handler.rs` 里的 `use crate::{...}` 与 `use lsp_types::...` 有何不同？

<details><summary>参考答案</summary>

前者引用的是**本 crate 根模块**中定义（或再导出）的名字——`crate::` 路径始终指向当前 crate 的根；后者引用外部 crate。注意 `crate::RequestId` 解析到的是 `lsp.rs` L230 的本地定义，而不是 `lsp_types` 的同名类型（显式定义优先于根模块里的 glob 再导出）。
</details>

**练习 3**：`LspStdoutHandler` 的两个字段为什么用 `pub(super)` 而不是 `pub(crate)` 或私有？

<details><summary>参考答案</summary>

子模块只被根模块直接使用，`pub(super)` 把可见性精确限制为「父模块可见」，是最小授权；`pub(crate)` 会放宽到全 crate（当前 crate 更小，实际等价，但语义上更松），私有字段则根模块无法初始化/读取它们。可见性收得越紧，将来重构的自由度越大。
</details>

## 5. 综合实践

**任务：为 `lsp` crate 制作一张「身份卡」。** 把本讲三个模块的知识串成一份可保存的笔记（建议放在你自己的笔记库，不要写进仓库），包含四部分：

1. **定位**：三句话说明 `lsp` 在 Zed 分层中的位置（协议运行时 / 被谁依赖 / 不负责什么），并列出你用 `grep -rl "lsp.workspace = true" crates/*/Cargo.toml` 找到的 5 个下游 crate。
2. **构建面**：记录 `cargo check -p lsp`、`cargo doc -p lsp --no-deps`、`cargo tree -p lsp --depth 1` 三条命令的关键输出（git fork 的 `lsp-types`、feature 列表）。
3. **类型来源表**：从 `cargo doc` 打开的文档或源码中挑 10 个类型，标注「lsp-types 再导出」还是「lsp.rs 本地定义」并给出定义行号（如 `RequestId` → L230、`LanguageServerBinary` → L95、`Uri` → 再导出）。
4. **文件地图**：抄录 4.3.4 的对照表与模块关系图，并注明两个文件的行数（2453 / 213）。

完成标准：仅凭这张身份卡，不看本讲义也能回答「`lsp::Position` 从哪来」「`input_handler` 模块给谁用」「`test-support` feature 打开了什么」。构建与测试类输出**待本地验证**。

## 6. 本讲小结

- `lsp` 是 Zed workspace 中的语言服务器通信基础库（workspace 成员见根 [Cargo.toml:L132](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/Cargo.toml#L132)），被 `project`、`language`、`editor`、`copilot`、`prettier` 等 20 多个 crate 依赖。
- 整个 crate 只有 `src/lsp.rs`（2453 行，库入口）与 `src/input_handler.rs`（213 行，私有分帧子模块）两个源文件；`[lib] path = "src/lsp.rs"` 与 `doctest = false` 定义在 [Cargo.toml:L11-L13](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/Cargo.toml#L11-L13)。
- `pub use lsp_types::request::*; pub use lsp_types::*;`（[lsp.rs:L3-L4](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L3-L4)）让下游只依赖 `lsp` 即可使用全部协议类型，且 `lsp-types` 是锁定 rev 的 Zed fork。
- `lsp` 命名空间是「再导出 + 本地定义」的混合体：`Uri`/`Position` 来自 lsp-types，`LanguageServerName`/`LanguageServerId`/`LanguageServerBinary`/`RequestId` 等是本地定义；本地显式定义会遮蔽 glob 再导出的同名类型。
- `test-support` feature（启用 `async-pipe` 并解锁 `FakeLanguageServer`）与 dev-dependencies 中的 `ctor`/`zlog` 共同构成本 crate 的测试基建。
- `input_handler.rs` 通过 `use crate::{...}` 借用根模块的 7 个类型，专注「stdout 字节流 → 消息」的分帧，其 `INCOMING_MESSAGE_QUEUE_CAPACITY = 128` 的有界队列是后续背压主题的伏笔。

## 7. 下一步学习建议

下一讲（u1-l2「JSON-RPC 消息模型与 serde 设计」）将深入 `lsp.rs` 中部的消息结构体：`Request`、`Notification`、`AnyResponse`、`Response`/`LspResult` 与 `NotificationOrRequest`，重点理解 `serde(untagged)`、`skip_serializing_if` 和基于 `RawValue` 的借用式反序列化。

在进入下一讲前，建议先通读 [src/lsp.rs:L225-L330](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L225-L330) 这一百来行消息定义，并浏览 [lsp.rs:L2340-L2365](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2340-L2365) 的三个 id 反序列化测试——它们是下一讲实践任务的直接样板。
