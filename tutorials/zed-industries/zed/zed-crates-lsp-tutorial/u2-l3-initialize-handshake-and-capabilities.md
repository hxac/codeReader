# initialize 握手与客户端能力声明

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 LSP 规定的启动握手顺序：`Initialize` 请求 → 服务器返回 `InitializeResult` → 客户端补发 `Initialized` 通知，以及「为什么必须是两步」。
2. 读懂 `LanguageServer::initialize` 的源码：它如何发送请求、如何用响应里的 `server_info` 回填 `version` 与 `process_name`、如何保存服务器能力。
3. 解读 `default_initialize_params` 构造的那份庞大「自我介绍信」—— Zed 作为客户端声明了哪些能力（completion resolve、semantic tokens delta、pull diagnostics、动态注册等），以及 `pull_diagnostics`、`augments_syntax_tokens` 两个开关参数如何控制其中的分支。
4. 理解 `capabilities` 字段为什么用 `RwLock<ServerCapabilities>` 存储，`capabilities()` 与 `update_capabilities()` 各自的用途与真实调用方。
5. 认识 `SEMANTIC_TOKEN_TYPES` / `SEMANTIC_TOKEN_MODIFIERS` 两个常量在语义高亮「图例」机制中的角色。

## 2. 前置知识

### 2.1 LSP 的启动握手：先谈判，再开工

上一讲（u2-l2）结束时，`LanguageServer::new_internal` 已经把子进程拉起来、三路 IO 任务也已就位。但此时客户端和服务器之间还**一句话都没说过**。LSP 规定，在正式工作之前必须完成一次「谈判」：

1. 客户端发送 **`initialize` 请求**（有 id，需要响应）。参数 `InitializeParams` 是一封自我介绍信：我是谁（`client_info`）、我在哪个目录工作（`root_uri` / `workspace_folders`）、**我支持哪些功能**（`capabilities`）。
2. 服务器返回 **`InitializeResult`**，其中 `capabilities: ServerCapabilities` 是服务器的回信：**我能提供哪些功能**（补全？悬停？格式化？）。
3. 客户端收到响应后，补发一条 **`initialized` 通知**（无 id，不需要响应），表示「谈判结束，开始干活」。在此之前，双方不得交换任何其他消息。

为什么分成两步而不是一步？因为服务器在返回能力清单**之前**可能需要根据客户端能力做初始化（比如客户端声明支持 UTF-16，服务器就按 UTF-16 准备索引）；而服务器返回清单**之后**、收到 `initialized` 之前，也可能还要做一次「看到客户端能力之后」的内部准备。两步握手给了双方各自完成初始化的机会。

「能力协商」（capability negotiation）是 LSP 的核心设计：**双方都只使用对方声明过的功能**。客户端在 `InitializeParams` 里声明「我会什么」，服务器在 `InitializeResult` 里声明「我会什么」，之后所有的功能调用都以这两份清单为准。

### 2.2 客户端能力 vs 服务器能力

| | 客户端能力 `ClientCapabilities` | 服务器能力 `ServerCapabilities` |
|---|---|---|
| 谁写的 | 客户端（Zed），随 `initialize` 请求发出 | 服务器，随 `initialize` 响应返回 |
| 在本 crate 哪里构造 | `default_initialize_params`（本讲 4.2） | 服务器进程自己决定，本 crate 只是**接收并保存** |
| 描述什么 | 客户端**能理解/处理**哪些特性（如「补全项支持 snippet」「支持 semantic tokens 增量请求」） | 服务器**能提供**哪些特性（如 `hover_provider: true`） |
| 存在哪 | 发出去就完了，不保存 | `LanguageServer.capabilities: RwLock<ServerCapabilities>`（本讲 4.4） |

### 2.3 Rust 前置知识

- **`RwLock`（读写锁）**：同一时刻允许多个读者**或**一个写者。适合「读多写少」的数据。这里用的是 `parking_lot::RwLock`（同步锁，临界区极短、绝不在 `.await` 上跨持锁）。
- **内部可变性**：`LanguageServer` 在初始化完成后以 `Arc<LanguageServer>` 形式共享，外部拿不到 `&mut self`；要修改 `capabilities` 就必须把可变性「藏」进字段内部——这就是 `RwLock` 的作用。
- **`bool::then_some`**：`pull_diagnostics.then_some(DiagnosticWorkspaceClientCapabilities { .. })` 在 `false` 时得到 `None`（该能力字段**整个不出现在 JSON 里**），`true` 时得到 `Some(..)`。这是本讲反复出现的惯用法。

### 2.4 与上一讲的衔接

u2-l2 介绍了 `LanguageServer` 结构体和三路 IO 任务；本讲用到它的这几个字段（定义见 [src/lsp.rs:L115-L143](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L115-L143)）：`version`（L121）、`process_name`（L122）、`capabilities`（L124）、`configuration`（L128）。发送请求用的 `request::<T>` 与发送通知用的 `notify::<T>` 的内部机制分别在 u3-l2 与 u3-l1 详解，本讲先把它们当作「发送并等待响应」「发送即忘」的两个黑盒使用。

## 3. 本讲源码地图

本讲只涉及一个文件，但跨度很大：

| 位置 | 内容 | 本讲角色 |
|---|---|---|
| [src/lsp.rs:L3-L4](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L3-L4) | `pub use lsp_types::request::*` 与 `pub use lsp_types::*` | `request::Initialize`、`notification::Initialized`、各种 `*ClientCapabilities` 类型都来自这里的整体再导出（u1-l1 已讲） |
| [src/lsp.rs:L380-L424](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L380-L424) | `SEMANTIC_TOKEN_TYPES` / `SEMANTIC_TOKEN_MODIFIERS` | 最小模块 4.3 |
| [src/lsp.rs:L778-L1073](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L778-L1073) | `default_initialize_params` | 最小模块 4.2 |
| [src/lsp.rs:L1079-L1108](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1079-L1108) | `LanguageServer::initialize` | 最小模块 4.1 |
| [src/lsp.rs:L1366-L1382](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1366-L1382) | `capabilities()` / `update_capabilities()` | 最小模块 4.4 |
| [src/lsp.rs:L1836-L1937](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1836-L1937) | `FakeLanguageServer`（test-support） | 本讲所有实践的假服务器；注意它**自动注册了 `Initialize` 应答器**（L1907-L1922） |
| [src/lsp.rs:L2408-L2452](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2408-L2452) | `test_default_initialize_params` | 实践样板 |

仓库外的交叉引用（只需知道存在，不必精读）：`crates/project/src/lsp_store/dynamic_registration.rs` 是 `update_capabilities` 的真实调用方（本讲 4.4）。

## 4. 核心概念与源码讲解

### 4.1 LanguageServer::initialize：一次完整的握手

#### 4.1.1 概念说明

`initialize` 是 `LanguageServer` 生命周期里「从进程就绪到可以工作」的那一步。它做四件事：

1. 把构造好的 `InitializeParams`（通常来自 4.2 的 `default_initialize_params`）作为 **`initialize` 请求**发给服务器，并等待响应；
2. 用响应 `InitializeResult.server_info` 回填自身的 `version` 与 `process_name`；
3. 把响应里的 `ServerCapabilities` 存入 `capabilities` 字段；
4. 补发 **`initialized` 通知**，完成协议规定的两步握手。

有两个签名细节值得先注意：

- 它接收 **`mut self`（按值拿走整个 `LanguageServer`）**，返回 `Task<Result<Arc<Self>>>`。这意味着握手期间调用方**无法触碰**这个服务器——类型系统保证了「在谈判结束前不会有其他消息插队」。成功后才以 `Arc<Self>` 的共享句柄还给调用方。
- 参数 `configuration: Arc<DidChangeConfigurationParams>` 是稍后要发给服务器的配置内容，这里先存进 `configuration` 字段（其文档注释说明：保存它是为了在「语言服务器日志面板」里展示发给服务器的配置，见 [src/lsp.rs:L125-L128](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L125-L128)）。

#### 4.1.2 核心流程

```text
调用方（project/language crate）
   │  server.initialize(params, configuration, timeout, cx)
   ▼
cx.background_spawn ────────────────────────────────────── 在后台任务中：
   │
   ├─ ① request::<request::Initialize>(params, timeout)
   │       发送 "initialize" 请求，等待响应（超时由 timeout 控制）
   │       .into_response() 把 ConnectionResult 转成 Result<InitializeResult>
   │       .with_context(...) 失败时附上 "initializing server {name}, id {id}"
   │
   ├─ ② if let Some(info) = response.server_info
   │       self.version     = info.version            // Option<String> → Option<SharedString>
   │       self.process_name = info.name              // String → Arc<str>
   │
   ├─ ③ self.capabilities = RwLock::new(response.capabilities)   // 保存服务器能力
   ├─ ④ self.configuration = configuration
   │
   └─ ⑤ notify::<notification::Initialized>(InitializedParams {}) // 补发 initialized
          返回 Ok(Arc::new(self))
```

时序上，①的请求走 `outbound_tx` 通道进 stdin，响应从 stdout 经 u1-l3 的分帧读取、按 `RequestId` 匹配到 `response_handlers` 后唤醒等待中的 future；⑤的通知同样经序列化通道发往 stdin。这两条底层链路分别在 u3-l2、u3-l1 精读。

#### 4.1.3 源码精读

握手本体，逻辑与上面的流程图逐行对应：

[src/lsp.rs:L1079-L1108](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1079-L1108) —— `initialize` 的全部实现：签名声明了「按值接收 `mut self`、返回 `Task<Result<Arc<Self>>>`」；函数体把整段握手 `cx.background_spawn` 到后台执行；先发 `Initialize` 请求并把 `ConnectionResult` 经 `into_response()` 折叠成 `Result`，失败时用 `with_context` 附加服务器名与 id，方便日志定位是哪个服务器初始化失败。

```rust
pub fn initialize(
    mut self,
    params: InitializeParams,
    configuration: Arc<DidChangeConfigurationParams>,
    timeout: Duration,
    cx: &App,
) -> Task<Result<Arc<Self>>> {
    cx.background_spawn(async move {
        let response = self
            .request::<request::Initialize>(params, timeout)
            .await
            .into_response()
            .with_context(|| {
                format!("initializing server {}, id {}", self.name(), self.server_id())
            })?;
        if let Some(info) = response.server_info {
            self.version = info.version.map(SharedString::from);
            self.process_name = info.name.into();
        }
        self.capabilities = RwLock::new(response.capabilities);
        self.configuration = configuration;

        self.notify::<notification::Initialized>(InitializedParams {})?;
        Ok(Arc::new(self))
    })
}
```

被回填的三个字段定义在结构体里（[src/lsp.rs:L115-L143](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L115-L143)，这段列出 `LanguageServer` 的全部字段，本讲关注 `version: Option<SharedString>`、`process_name: Arc<str>`、`capabilities: RwLock<ServerCapabilities>`、`configuration: Arc<DidChangeConfigurationParams>`）。

几个细节：

- **`server_info` 回填是「尽力而为」的**：`if let Some(info)` 说明服务器可以不回传 `ServerInfo`；不回传时 `version`/`process_name` 保持 `LanguageServer::new` 时设置的初值（进程二进制的名字）。回传时 `info.name` 会**覆盖** `process_name`——注意区分两个字段：`name`（L120）是 Zed 内部对语言服务器的注册名（如 `rust-analyzer`），`process_name` 是服务器自报的家门（`ServerInfo.name`，通常也是可执行名但以服务器为准）。
- **`version` 的读取口**是 [src/lsp.rs:L1333-L1335](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1333-L1335) 的 `version()`；`process_name` 的读取口是 [src/lsp.rs:L1361-L1363](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1361-L1363)。（对 gopls 版本串的特殊解析 `readable_version` 留到 u4-l4。）
- **`?` 与 `notify` 的组合**：`notify::<notification::Initialized>(...)?` 在服务器已关闭、通知发不出去时让整个初始化以 `Err` 收场——`initialized` 发不出去意味着握手没有完成，调用方应当知道。
- **`Arc::new(self)`** 是这趟旅程的终点：从此 `LanguageServer` 以 `Arc` 共享，任何 `&self` 方法都能调用，而修改 `capabilities` 只能靠内部可变性（4.4）。

顺带一提测试侧的关键事实：`FakeLanguageServer::new` 在构造时**自动注册了 `Initialize` 与 `Shutdown` 两个应答器**（[src/lsp.rs:L1907-L1924](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1907-L1924)）——`Initialize` 应答器返回 `InitializeResult { capabilities（构造时传入的那份）, server_info: Some(ServerInfo { name, ..Default::default() }) }`。所以在测试里对 fake 调 `initialize` 无需任何额外准备就能成功；`ServerInfo` 只填了 `name`、`version` 为 `None`，这正好可以用来观察 4.1.1 中「`server_info` 回填」的行为。

#### 4.1.4 代码实践

**实践目标**：亲手完成一次握手，验证 `initialize` 对 `capabilities`、`process_name`、`version` 三个字段的写入效果，并在 fake 一侧确认 `initialized` 通知确实补发了。

**操作步骤**（示例代码，仿照 [src/lsp.rs:L2408-L2452](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2408-L2452) 的 `test_default_initialize_params` 与 [src/lsp.rs:L2135-L2149](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2135-L2149) 的写法，可加到 `src/lsp.rs` 末尾 `tests` 模块中本地运行，验证后删除）：

```rust
#[gpui::test]
async fn test_initialize_handshake(cx: &mut TestAppContext) {
    cx.update(|cx| release_channel::init(semver::Version::new(0, 0, 0), cx));
    let (server, mut fake) = FakeLanguageServer::new(
        LanguageServerId(0),
        LanguageServerBinary {
            path: "path/to/language-server".into(),
            arguments: vec![],
            env: None,
        },
        "test-lsp".to_string(),
        LanguageServer::full_capabilities(), // 服务器能力清单：document_highlight_provider 等为 true
        &mut cx.to_async(),
    );

    let server = cx
        .update(|cx| {
            let params = server.default_initialize_params(false, false, cx);
            server.initialize(
                params,
                DidChangeConfigurationParams::default().into(),
                DEFAULT_LSP_REQUEST_TIMEOUT,
                cx,
            )
        })
        .await
        .unwrap();

    // initialize 之后：服务器能力已保存，server_info 已回填
    assert_eq!(
        server.capabilities().document_highlight_provider,
        Some(OneOf::Left(true))
    );
    assert_eq!(server.process_name(), "test-lsp"); // ServerInfo.name 覆盖了 process_name
    assert_eq!(server.version(), None); // fake 的 ServerInfo::default() 没填 version

    // fake 一侧确认 initialized 通知已送达
    let _ = fake.receive_notification::<notification::Initialized>().await;
}
```

**需要观察的现象**：

- `initialize(...).await.unwrap()` 返回的是 `Arc<LanguageServer>`，此后的 `server` 是共享句柄（原 `LanguageServer` 值已被 move 进任务）。
- `full_capabilities()`（[src/lsp.rs:L1941-L1953](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1941-L1953)，test-support 辅助函数）里声明的 `document_highlight_provider` 原样出现在 `server.capabilities()` 里——证明响应中的 `ServerCapabilities` 被完整保存。
- `process_name()` 是 `"test-lsp"`（`ServerInfo.name` 回填）而非二进制路径名。

**预期结果**：断言全部通过；若把 `full_capabilities()` 换成 `Default::default()`（全 `None` 能力），第一条断言应改为 `assert!(server.capabilities().document_highlight_provider.is_none())` 依然通过。运行方式：`cargo test -p lsp test_initialize_handshake`。

**待本地验证**：以上测试的编译与运行结果需在 Zed workspace 内实际执行确认。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `self.notify::<notification::Initialized>(..)?` 这一行删掉，协议上会发生什么问题？

**答案**：`initialized` 通知是 LSP 规定的握手收尾信号。不发它，规范服务器会拒绝处理后续绝大多数请求/通知（规范要求服务器在收到 `initialized` 之前不得发送除 `window/logMessage` 等少数消息之外的任何东西，客户端也不应发送其他消息）。客户端一侧虽然看起来「能用」，但真实服务器很可能不响应任何功能请求。测试里的 fake 不强制这一点，所以单测不会失败——这正是「协议正确性无法只靠 fake 验证」的一个例子。

**练习 2**：为什么 `initialize` 的签名是 `fn initialize(mut self, ..) -> Task<Result<Arc<Self>>>`，而不是 `fn initialize(&mut self, ..)`？

**答案**：按值接收 `self` 并在成功后以 `Arc` 返回，有两层效果。其一，握手期间（`Task` 尚未完成）调用方手里没有可用的句柄，类型系统阻止了「在谈判结束前发送其他消息」。其二，返回 `Arc<Self>` 明确了此后服务器的共享形态——多处以 `Arc<LanguageServer>` 存储、跨线程传递，`&mut self` 在这种形态下拿不到，后续对 `capabilities` 的修改只能走 `RwLock` 内部可变性（见 4.4）。

**练习 3**：`initialize` 失败时（例如服务器直接退出），错误信息里能看出是哪个服务器失败吗？

**答案**：能。`with_context`（[src/lsp.rs:L1091-L1097](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1091-L1097)）把 `format!("initializing server {}, id {}", self.name(), self.server_id())` 附在错误链上，`anyhow` 的 `{:#}` 打印会连带这条上下文。另外结合 u2-l1 讲过的 `stderr_capture`，真实进程场景下启动失败的 stderr 也会拼进用户可见的错误。

### 4.2 default_initialize_params：Zed 的能力声明清单

#### 4.2.1 概念说明

`default_initialize_params` 是 Zed 那封「自我介绍信」的默认底稿：构造一份几乎完整的 `InitializeParams`，包括 `ClientCapabilities`。它有两个布尔开关参数：

- **`pull_diagnostics`**：是否声明**拉取式诊断**（`workspace/diagnostics` + `textDocument/diagnostic`）。LSP 有两套诊断体系：传统的「推送」（服务器主动发 `textDocument/publishDiagnostics` 通知）和 3.17 引入的「拉取」（客户端按需请求）。服务器只有看到客户端声明了拉取能力才会启用它，所以这个开关直接决定两个能力字段是否出现在 JSON 里。
- **`augments_syntax_tokens`**：写入 `textDocument.semanticTokens.augmentsSyntaxTokens`，告诉服务器 Zed 的语义高亮应当**增强**（而非替代）内置的 TreeSitter 语法高亮。

调用方（语言适配器所在的 crate）可以拿这份底稿再按语言微调，随后交给 `initialize`。

#### 4.2.2 核心流程

函数体按 `InitializeParams` 的字段自上而下构造，可以分成六块：

```text
default_initialize_params(pull_diagnostics, augments_syntax_tokens, cx)
 ├─ A. 工作区：workspace_folders 字段 ← 共享状态 BTreeSet<Uri>（或退化为 root_uri 单文件夹）
 ├─ B. 进程身份：process_id、root_path、root_uri
 ├─ C. capabilities.general      —— 位置编码声明 UTF-16
 ├─ D. capabilities.workspace    —— 工作区级能力（configuration、动态注册、inlayHint 刷新、
 │                                  诊断刷新 ← pull_diagnostics 开关 ①、语义 token 刷新…）
 ├─ E. capabilities.textDocument —— 文档级能力（definition/codeAction/completion/rename/hover/
 │                                  inlayHint/semanticTokens ← 图例+delta、publishDiagnostics、
 │                                  formatting/signatureHelp/synchronization/foldingRange/
 │                                  diagnostic ← pull_diagnostics 开关 ② …）
 ├─ F. capabilities.window + experimental —— 进度条、showMessage、Zed 私有扩展
 └─ G. client_info ← release_channel 全局（"Zed" + 版本号）；trace/locale 置空
```

`ClientCapabilities` 的五个区段（general / workspace / textDocument / window / experimental）与 LSP 规范一一对应；几乎每个能力都是 `Option`，`None` 即「该字段不序列化进 JSON」——这也是 `then_some` 惯用法频繁出现的原因。

#### 4.2.3 源码精读

函数签名与工作区推导：

[src/lsp.rs:L778-L794](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L778-L794) —— `default_initialize_params` 的签名（三个参数：两个能力开关 + `cx`）以及开头对 `workspace_folders` 的推导：若构造时传入了共享的 `BTreeSet<Uri>` 就逐个转成 `WorkspaceFolder`，否则退化为只含 `root_uri` 的单文件夹列表。每个 `Uri` 到 `WorkspaceFolder` 的命名规则（`workspace_folder_for_uri`）在 u3-l5 精读。

进程身份三件套：

[src/lsp.rs:L796-L806](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L796-L806) —— 构造 `InitializeParams` 的开头：`process_id` 填 Zed 自己的 pid；`root_path` 与 `root_uri` 都从 `self.root_uri` 推导（`root_path` 是把 URI 转回文件路径字符串）。注意 `#[allow(deprecated)]`：`root_path` 在 LSP 3.x 已被 `root_uri` 取代，但为了兼容老服务器仍然发送——这是「弃用但必须继续填」的典型场景。

`workspace` 区段（节选关键项，完整清单见 [src/lsp.rs:L812-L862](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L812-L862)）：

| 能力字段 | 值 | 含义 |
|---|---|---|
| `configuration` | `Some(true)` | 客户端支持 `workspace/configuration` 拉取配置 |
| `didChangeWatchedFiles` | 动态注册 + 相对路径 | 文件监听可由服务器动态注册 |
| `workspaceFolders` | `Some(true)` | 支持多工作区文件夹及增删通知（u3-l5） |
| `inlayHint.refreshSupport` | `Some(true)` | 服务器可发 `workspace/inlayHint/refresh` 让客户端重拉 |
| `diagnostics` | `pull_diagnostics.then_some { refresh_support: true }` | **开关 ①**：拉取式工作区诊断（含刷新） |
| `semanticTokens.refreshSupport` | `Some(true)` | 语义高亮可整库刷新 |

[src/lsp.rs:L830-L834](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L830-L834) —— `workspace.diagnostics` 分支：`pull_diagnostics.then_some(DiagnosticWorkspaceClientCapabilities { refresh_support: Some(true) })`。`false` 时整体为 `None`（JSON 里不出现），`true` 时声明「支持拉取式诊断且可刷新」。

`textDocument` 区段同样庞大（[src/lsp.rs:L863-L1047](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L863-L1047)），挑四个最有教学价值的片段：

**其一，completion 的延迟解析**：

[src/lsp.rs:L892-L936](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L892-L936) —— `completion` 能力声明：`snippet_support`、`documentation_format`（Markdown 优先）、`completion_list.item_defaults`（批量补全列表的公共字段省略）等；最重要的是 L895-L904 的 `resolve_support.properties`——列出了哪些字段允许服务器在 `completionItem/resolve` 二段式请求中延迟填充。注意 L901-L902 那行注释：

```rust
// NB: Do not have this resolved, otherwise Zed becomes slow to complete things
// "textEdit".to_string(),
```

`textEdit` 被**故意注释掉**：不把它加进 resolve 列表，服务器就必须在首段响应里直接给出文本编辑，避免每次补全都多一轮回程。这是「能力声明直接影响交互性能」的活例子。

**其二，semantic tokens 的 delta 请求与图例**：

[src/lsp.rs:L961-L974](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L961-L974) —— `semanticTokens` 能力：`requests.full = Some(SemanticTokensFullOptions::Delta { delta: Some(true) })` 声明「全量语义高亮支持**增量**」（服务器可只回变化部分）；`token_types` / `token_modifiers` 直接引用 4.3 的两个常量（L967-L968）；`augments_syntax_tokens: Some(augments_syntax_tokens)` 是**开关 ②**（该字段为 Zed 维护的 lsp-types fork 提供的扩展，不在官方 3.17 规范中）。

**其三，推送式诊断的解析能力**：

[src/lsp.rs:L975-L983](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L975-L983) —— `publishDiagnostics` 能力：声明客户端能处理 `relatedInformation`（关联诊断）、`version_support`（诊断带文档版本号，过期诊断可丢弃）、`data_support`、`tagSupport`（UNNECESSARY/DEPRECATED 灰显与删除线）、`code_description_support`。注意这与上面的拉取式诊断**并不互斥**：这是对「服务器推来的通知」的解析能力声明。

**其四，拉取式文档诊断**：

[src/lsp.rs:L1020-L1023](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1020-L1023) —— `textDocument.diagnostic` 分支：`pull_diagnostics.then_some(DiagnosticClientCapabilities { dynamic_registration: Some(true), related_document_support: Some(true) })`，**开关 ①** 的第二个落点。与 L830-L834 成对出现：一个让客户端能按文档拉诊断，一个让服务器能要求整库刷新。

收尾的三个区段：

[src/lsp.rs:L1048-L1070](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1048-L1070) —— `experimental` 声明两个 Zed 私有扩展：`serverStatusNotification`（rust-analyzer 的 `experimental/serverStatus`）与 `localDocs`（本地文档检索）；`window` 声明 `work_done_progress`（支持进度条通知，见 u4-l3 的 fake 进度模拟）与带附加属性的 `showMessage`；最后 `client_info` 从 `release_channel` 全局读出客户端名（如 "Zed"）与版本号——这也是本讲实践里测试要先调 `release_channel::init` 的原因（未初始化时 `try_global` 返回 `None`，`client_info` 就是 `None`，测试断言会不稳定）。`trace`、`locale` 显式置 `None`，其余字段用 `..InitializeParams::default()` 兜底。

#### 4.2.4 代码实践

**实践目标**：对照现有测试 `test_default_initialize_params`（[src/lsp.rs:L2408-L2452](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2408-L2452)，它只验证 root_path/root_uri/workspace_folders），补上 `pull_diagnostics` 开关的两个断言点。

**操作步骤**（示例代码，加到 `src/lsp.rs` 的 `tests` 模块，本地验证后删除）：

```rust
#[gpui::test]
async fn test_pull_diagnostics_capability_toggle(cx: &mut TestAppContext) {
    cx.update(|cx| release_channel::init(semver::Version::new(0, 0, 0), cx));
    let (server, _fake) = FakeLanguageServer::new(
        LanguageServerId(0),
        LanguageServerBinary {
            path: "path/to/language-server".into(),
            arguments: vec![],
            env: None,
        },
        "test-lsp".to_string(),
        Default::default(),
        &mut cx.to_async(),
    );

    // 开关关闭：两个拉取诊断能力字段都应为 None（不出现在 JSON 中）
    let params = cx.update(|cx| server.default_initialize_params(false, false, cx));
    assert!(params
        .capabilities
        .workspace
        .and_then(|workspace| workspace.diagnostics)
        .is_none());
    assert!(params
        .capabilities
        .text_document
        .and_then(|text_document| text_document.diagnostic)
        .is_none());

    // 开关打开：workspace.diagnostics 开启刷新，textDocument.diagnostic 声明相关文档支持
    let params = cx.update(|cx| server.default_initialize_params(true, false, cx));
    let workspace_diagnostics = params
        .capabilities
        .workspace
        .and_then(|workspace| workspace.diagnostics)
        .expect("workspace.diagnostics should be set");
    assert_eq!(workspace_diagnostics.refresh_support, Some(true));
    let text_document_diagnostic = params
        .capabilities
        .text_document
        .and_then(|text_document| text_document.diagnostic)
        .expect("textDocument.diagnostic should be set");
    assert_eq!(text_document_diagnostic.related_document_support, Some(true));
}
```

**需要观察的现象**：同一个方法、只差一个布尔参数，`capabilities.workspace.diagnostics` 与 `capabilities.text_document.diagnostic` 两处从 `None` 变为 `Some(..)`；其余能力字段两次完全一致。

**预期结果**：断言通过。运行：`cargo test -p lsp test_pull_diagnostics_capability_toggle`。若想进一步确认「`None` 意味着 JSON 里没有该键」，可以用 `serde_json::to_value(&params.capabilities)` 把 `ClientCapabilities` 序列化成 `Value`，再断言序列化结果里查不到 `diagnostics` 相关键（服务器收到的就是这份 JSON）。

**待本地验证**：以上测试需在 Zed workspace 内实际运行确认。

#### 4.2.5 小练习与答案

**练习 1**：`pull_diagnostics` 为什么用 `bool::then_some` 控制字段有无，而不是固定写出 `refresh_support: Some(pull_diagnostics)`？

**答案**：语义完全不同。`then_some` 在 `false` 时让整个字段为 `None`、**不出现在 JSON 里**——服务器读到「没有这个键」就当作客户端不支持拉取诊断。若固定写出 `refresh_support: Some(false)`，字段存在但值为 `false`，语义变成「支持拉取诊断、但不支持刷新」——服务器可能因此启用拉取模式却发不了刷新请求。LSP 里「能力缺失」与「能力存在但关闭」是两件事，`None` 与 `Some(false)` 必须区分。

**练习 2**：`augments_syntax_tokens` 是官方 LSP 3.17 规范里的字段吗？Zed 为什么需要它？

**答案**：不是官方字段，是 Zed 维护的 lsp-types fork 增加的扩展（u1-l1 讲过 lsp-types 是 Zed fork 并锁定 rev）。它的作用是告诉服务器：Zed 已经用 TreeSitter 做了语法高亮，服务器的语义 token 应当**叠加增强**这份高亮，而不是当作唯一着色来源。语言适配器按语言特点决定传 `true` 还是 `false`。

**练习 3**：`default_initialize_params` 里 `process_id` 填的是谁的 pid？服务器拿它有什么用？

**答案**：填 `std::process::id()`（[src/lsp.rs:L798](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L798)），即 Zed（客户端）自己的 pid，不是语言服务器子进程的 pid。服务器可用它识别父进程、在客户端退出时做清理（部分服务器会监视父进程存活）。子进程自己的 pid 则通过 `LanguageServer::process_id()`（[src/lsp.rs:L1396-L1398](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1396-L1398)）从 `Child` 读取。

### 4.3 SEMANTIC_TOKEN_TYPES / SEMANTIC_TOKEN_MODIFIERS：语义高亮的图例

#### 4.3.1 概念说明

语义高亮（semantic highlighting）的传输有个带宽难题：如果每个 token 都传字符串 `"keyword"`、`"variable"`，报文会非常臃肿。LSP 的解法是**图例（legend）+ 索引**：

1. 握手时客户端在 `textDocument.semanticTokens.tokenTypes` / `tokenModifiers` 里声明一份**有序清单**（图例）；
2. 服务器的全量/增量响应里，每个 token 的类型只是一个**整数索引**，修饰符是一个**位掩码**（第 i 位对应图例第 i 个修饰符）；
3. 客户端拿索引回自己的图例里查出名字，再映射到主题颜色。

因此图例必须在握手时**随能力一起声明**——这正是 4.2 中 [src/lsp.rs:L967-L968](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L967-L968) 把这两个常量填进 `semanticTokens` 能力的原因。图例必须与客户端实际的渲染代码保持一致：图例里没有的类型，索引过来就查不到。

#### 4.3.2 核心流程

```text
客户端                                        服务器
  │ tokenTypes:   [namespace, class, ..., lifetime]   │
  │ tokenModifiers:[declaration, ..., constant]        │
  │ ───────────── initialize 请求 ────────────────▶    │
  │        （图例已随能力送达，双方有了同一本字典）        │
  │                                                   │
  │ ◀───── textDocument/semanticTokens/full 响应 ───── │
  │   data: [deltaLine, deltaStartChar, length,        │
  │           tokenType=索引, tokenModifiers=位掩码]    │
  ▼
 索引 12 → SEMANTIC_TOKEN_TYPES[12] = "function"
 掩码 0b11 → [DECLARATION, DEFINITION]
```

#### 4.3.3 源码精读

[src/lsp.rs:L376-L409](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L376-L409) —— `SEMANTIC_TOKEN_TYPES` 常量：开头 L376-L379 的注释给出两个权威出处（VSCode 语义高亮文档与 LSP 3.17 规范），随后按固定顺序列出全部 token 类型。前 23 项是规范/文档标准项（`NAMESPACE` 到 `MODIFIER`），其中有两处偏离值得注意：L396 的 `SemanticTokenType::new("label")` 注明「不在规范里、但在 VSCode 文档里」；L403 的 `MODIFIER` 注明「只在规范里、不在文档里」——规范与文档不一致是真实存在的，注释把坑标了出来。L404-L408 是语言特定项：C# 的 `EVENT` 与 Rust 的 `lifetime`（通过 `SemanticTokenType::new("lifetime")` 动态构造，标准枚举没有它）。**顺序即语义**：服务器发的索引就是按这个数组下标解释的，所以这个数组一经发布就不能重排，只能尾部追加。

[src/lsp.rs:L410-L424](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L410-L424) —— `SEMANTIC_TOKEN_MODIFIERS` 常量：10 个标准修饰符（`DECLARATION` 到 `DEFAULT_LIBRARY`）加上 Rust 特有的 `constant`（L423）。修饰符在协议里以位掩码传输：第 i 个修饰符对应整数的第 i 位，因此它的顺序同样不可变动。

两个常量的消费点就是 4.2 的 [src/lsp.rs:L967-L968](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L967-L968)：`token_types: SEMANTIC_TOKEN_TYPES.to_vec()`、`token_modifiers: SEMANTIC_TOKEN_MODIFIERS.to_vec()`——每次构造 `InitializeParams` 都整份复制进能力声明。

#### 4.3.4 代码实践

**实践目标**：验证图例内容与顺序，体会「索引 → 图例」的映射。

**操作步骤**（示例代码）：

```rust
#[test]
fn semantic_token_legend() {
    // 图例是 pub 常量，测试外也能直接引用
    assert_eq!(SEMANTIC_TOKEN_TYPES.len(), 25);
    assert_eq!(SEMANTIC_TOKEN_MODIFIERS.len(), 11);

    // 顺序即语义：前若干位是标准项
    assert_eq!(SEMANTIC_TOKEN_TYPES[0], SemanticTokenType::NAMESPACE);
    assert_eq!(SEMANTIC_TOKEN_TYPES[12], SemanticTokenType::FUNCTION);
    // 语言特定项通过 new 动态构造，也能用相等性比较
    assert!(SEMANTIC_TOKEN_TYPES.contains(&SemanticTokenType::new("lifetime")));
    assert!(SEMANTIC_TOKEN_MODIFIERS.contains(&SemanticTokenModifier::new("constant")));
}
```

把断言里的下标与 [src/lsp.rs:L380-L409](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L380-L409) 的数组逐项对照：`NAMESPACE` 是第 0 项、`FUNCTION` 是第 12 项（从 0 数：NAMESPACE 0、CLASS 1、ENUM 2、INTERFACE 3、STRUCT 4、TYPE_PARAMETER 5、TYPE 6、PARAMETER 7、VARIABLE 8、PROPERTY 9、ENUM_MEMBER 10、DECORATOR 11、FUNCTION 12）。

**需要观察的现象**：`SEMANTIC_TOKEN_TYPES[12]` 恰为 `FUNCTION`——若某服务器的 token 流里 `tokenType = 12`，Zed 就把它渲染为函数。

**预期结果**：`cargo test -p lsp semantic_token_legend` 通过。若你对数组下标的推算没把握，可以先 `println!("{}", SEMANTIC_TOKEN_TYPES.iter().position(|t| *t == SemanticTokenType::FUNCTION).unwrap())` 打印确认——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：服务器发来 `tokenType = 24`，按图例这是什么？为什么 Zed 能在没有语言上下文的情况下解释它？

**答案**：按 [src/lsp.rs:L380-L409](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L380-L409) 的顺序数到第 24 项（0 起）是 `EVENT`（倒数第二项，其后是 `lifetime`）。能直接解释，是因为图例在握手时**随客户端能力声明发给了服务器**，服务器承诺只用这张图例里的索引；双方共享同一本字典，索引才有确定含义。

**练习 2**：如果未来要新增一个 token 类型，应该插到数组中间还是追加到尾部？为什么？

**答案**：只能**尾部追加**。图例的顺序就是索引的编码：已有服务器可能按旧图例发 `12` 表示 `FUNCTION`，一旦中间插入新项，`12` 的含义就变了，旧服务器的语义高亮会整体错位。这种「公开常量数组的顺序是不可变协议」是维护图例类数据的核心约束。

**练习 3**：`tokenModifiers` 为什么用位掩码而不用索引数组？

**答案**：一个 token 可以同时带多个修饰符（既是声明又是只读）。位掩码用整数的第 i 位表示「是否带第 i 个修饰符」，一个数字就能表达任意组合，且定长编码对增量传输友好（每个 token 固定 5 个数字：行差、列差、长度、类型索引、修饰符掩码）。图例长度决定了掩码的位宽，这也是修饰符数组同样不能重排的原因。

### 4.4 capabilities 的存储：RwLock 与 update_capabilities

#### 4.4.1 概念说明

握手拿到的 `ServerCapabilities` 不是用完即弃的——之后每一次功能调用前，调用方都要查「服务器到底支不支持」。于是它被存进 `LanguageServer.capabilities` 字段，并配了一对读写接口：

- `capabilities() -> ServerCapabilities`：**读**，返回整份能力的克隆（快照）；
- `update_capabilities(impl FnOnce(&mut ServerCapabilities))`：**写**，用闭包就地修改。

为什么存 `RwLock` 而不是普通字段？三个理由叠加：

1. **拿不到 `&mut self`**：4.1 讲过，初始化完成后 `LanguageServer` 以 `Arc` 共享，全仓库只有共享引用，修改必须靠内部可变性；
2. **读多写少**：功能检查（「服务器支持格式化吗？」）发生在几乎每个编辑操作里，是高频读；而写只发生在两个时刻——`initialize` 一次，以及运行时**动态注册**；
3. **并发读安全**：`RwLock` 允许多个读者并发进入，互不阻塞。

「动态注册」是 `update_capabilities` 存在的真正原因：LSP 允许服务器在会话中途发 `client/registerCapability` / `unregisterCapability` 请求，动态开关某些功能（比如用户改配置后，rust-analyzer 注销掉 inlay hint）。此时服务器**不会重发** `ServerCapabilities`——这份清单自握手后就是客户端本地维护的视图，服务器只发增量指令，客户端要自己把本地清单改掉。

#### 4.4.2 核心流程

```text
initialize 响应
   │ self.capabilities = RwLock::new(response.capabilities)     ← 唯一的整份写入
   ▼
┌──────────────── capabilities: RwLock<ServerCapabilities> ────────────────┐
│                                                                          │
│  capabilities() ──read().clone()──▶ 调用方拿到快照                        │
│     （editor/project/language 各处功能检查）                              │
│                                                                          │
│  update_capabilities(|caps| ..) ──write()──▶ 闭包就地修改                │
│     （initialize 后由动态注册逻辑调用）                                    │
└──────────────────────────────────────────────────────────────────────────┘
```

#### 4.4.3 源码精读

字段定义：

[src/lsp.rs:L124](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L124) —— `capabilities: RwLock<ServerCapabilities>`（parking_lot 的 `RwLock`，L17 导入），是结构体里唯一的非 `Mutex`、非 `Arc<Mutex>` 可变字段：handler 表们用 `Arc<Mutex<HashMap>>` 是为了配合 `Subscription` 的弱引用摘除机制（u4-l2），而 `capabilities` 是单一值、生命周期与服务器的 `Arc` 完全一致，用 `RwLock` 足够。

读接口：

[src/lsp.rs:L1366-L1368](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1366-L1368) —— `capabilities()`：`self.capabilities.read().clone()`，读锁只持有到 `clone` 结束，调用方拿到的是当时的能力快照，之后别人怎么改都不影响手里这份。

写接口：

[src/lsp.rs:L1380-L1382](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1380-L1382) —— `update_capabilities(update: impl FnOnce(&mut ServerCapabilities))`：取写锁、把 `&mut ServerCapabilities` 交给闭包。用闭包而非「先 clone 出来改完再写回」，避免了两次拷贝，也把锁的临界期限制在闭包执行期间。

真实调用方（本 crate 之外，证明这不是摆设接口）：`crates/project/src/lsp_store/dynamic_registration.rs` 在处理服务器的动态注册/注销请求时调用它，例如 [crates/project/src/lsp_store/dynamic_registration.rs:L128](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store/dynamic_registration.rs#L128) 的 `server.update_capabilities(|capabilities| *capability_of(capabilities) = active);`——服务器注销某能力时，把本地视图中对应位关掉，后续功能检查立即生效。这正是 4.4.1 所述「服务器能力清单由客户端本地维护」的落地处。

相邻的复合接口：

[src/lsp.rs:L1372-L1377](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1372-L1377) —— `adapter_server_capabilities()`：把 `capabilities()` 与 `code_action_kinds()`（语言适配器侧已知的补充信息）打包成 `AdapterServerCapabilities` 返回，供上层做更完整的功能判断。

#### 4.4.4 代码实践

**实践目标**：完整走一遍「读 → 改 → 读回」，验证 `RwLock` 内部可变性的效果——全程只有 `&self`/`Arc`，没有任何 `&mut`。

**操作步骤**（示例代码，紧接 4.1.4 的握手测试继续写）：

```rust
    // server 此时是 Arc<LanguageServer>，没有 &mut 可用
    let before = server.capabilities();
    assert_eq!(before.document_highlight_provider, Some(OneOf::Left(true))); // full_capabilities 声明过

    // 服务器运行时「注销」了 document highlight：本地视图关掉这一位
    server.update_capabilities(|capabilities| {
        capabilities.document_highlight_provider = None;
    });

    // 读回验证：快照已更新，且 before 这份旧快照不受影响
    let after = server.capabilities();
    assert!(after.document_highlight_provider.is_none());
    assert_eq!(before.document_highlight_provider, Some(OneOf::Left(true)));
```

**需要观察的现象**：三段断言分别对应「初值来自 initialize 响应」「update_capabilities 就地生效」「旧快照不可变」三个性质。整个过程只用了共享引用——这就是 `RwLock` 存在的意义。

**预期结果**：断言通过。可并入 4.1.4 的测试一起运行（`cargo test -p lsp test_initialize_handshake`）。

**待本地验证**：编译与断言结果需在 Zed workspace 内实际运行确认。

#### 4.4.5 小练习与答案

**练习 1**：`capabilities()` 为什么返回克隆而不是 `Arc<ServerCapabilities>` 或守卫引用？

**答案**：返回守卫（`RwLockReadGuard`）会把锁的生命周期泄漏给调用方，调用方不小心把守卫存起来或在上面跨 `.await`，就会阻塞所有写者甚至死锁；返回 `Arc` 则要求字段本身存 `Arc<RwLock<..>>`，且更新时要么整份替换（`update_capabilities` 的闭包式就地修改就做不到了）。克隆一份 `ServerCapabilities` 的代价可以接受（结构体主要是 `Option` 字段集合），换来的是调用方拿到独立快照、锁立刻释放——正确性优先，符合本仓库的编码准则。

**练习 2**：服务器通过 `client/unregisterCapability` 注销了 inlay hint 功能后，Zed 是怎么「知道」这件事的？服务器会重新发一遍 `ServerCapabilities` 吗？

**答案**：不会重发。`ServerCapabilities` 只在 `initialize` 响应里传输一次；此后它就是客户端本地维护的视图。服务器只发「注册/注销了哪些 method」的增量指令，客户端（`crates/project/src/lsp_store/dynamic_registration.rs`）收到后调用 `update_capabilities` 把本地视图中对应的 provider 位打开或关掉，后续功能检查（`capabilities().inlay_hint_provider` 等）自然反映最新状态。

**练习 3**：`LanguageServer` 里 handler 表用 `Arc<Mutex<HashMap<..>>>`，而 `capabilities` 用 `RwLock<ServerCapabilities>`，为什么存储形式不同？

**答案**：两者的共享需求不同。handler 表需要被 `Subscription` 以 `Arc::downgrade` 弱引用（`Subscription::drop` 时升级并摘除自己，u4-l2 详解），所以必须有 `Arc`；而 `capabilities` 的写者就是服务器自身逻辑、不需要弱引用，只读多写少，`RwLock` 即可。选哪种容器由「谁持有它、生命周期如何」决定，而不是随便挑一个锁。

## 5. 综合实践

把本讲三个知识点串成一个端到端小任务：**用 FakeLanguageServer 完成一次「能力协商 → 握手 → 动态改能力 → 优雅退出」的完整会话**。

在 `src/lsp.rs` 的 `tests` 模块中新增（示例代码）：

```rust
#[gpui::test]
async fn test_capability_negotiation_session(cx: &mut TestAppContext) {
    cx.update(|cx| release_channel::init(semver::Version::new(0, 0, 0), cx));

    // 服务器一侧：声明支持 document highlight，其余默认
    let (server, mut fake) = FakeLanguageServer::new(
        LanguageServerId(0),
        LanguageServerBinary {
            path: "path/to/language-server".into(),
            arguments: vec![],
            env: None,
        },
        "test-lsp".to_string(),
        LanguageServer::full_capabilities(),
        &mut cx.to_async(),
    );

    // 客户端一侧：打开 pull diagnostics 开关构造自我介绍信
    let server = cx
        .update(|cx| {
            let params = server.default_initialize_params(true, false, cx);
            server.initialize(
                params,
                DidChangeConfigurationParams::default().into(),
                DEFAULT_LSP_REQUEST_TIMEOUT,
                cx,
            )
        })
        .await
        .unwrap();

    // ① 握手结果：服务器能力已入库，server_info 已回填
    assert_eq!(
        server.capabilities().document_highlight_provider,
        Some(OneOf::Left(true))
    );
    assert_eq!(server.process_name(), "test-lsp");

    // ② initialized 通知已按协议补发到服务器一侧
    let _ = fake.receive_notification::<notification::Initialized>().await;

    // ③ 运行时动态注销 document highlight，本地视图同步更新
    server.update_capabilities(|capabilities| {
        capabilities.document_highlight_provider = None;
    });
    assert!(server.capabilities().document_highlight_provider.is_none());

    // ④ 优雅退出（为下一讲的 shutdown 流程做铺垫）
    drop(server);
    cx.run_until_parked();
    fake.receive_notification::<notification::Exit>().await;
}
```

完成后依次核对四个检查点，它们分别对应本讲的四个最小模块：

| 检查点 | 验证的知识 |
|---|---|
| ① `capabilities()` 反映 fake 声明的能力 | initialize 保存 `ServerCapabilities`（4.1） |
| ② fake 收到 `initialized` | 两步握手的第二拍（4.1） |
| ③ `update_capabilities` 生效 | RwLock 内部可变性（4.4） |
| ④ fake 收到 `Exit` | Drop 自动善后（u2-l4 预告） |

再把 `default_initialize_params(true, ..)` 换成 `false`，用 4.2.4 的断言确认拉取诊断能力从信里消失，观察同一方法受开关驱动的分支。运行：`cargo test -p lsp test_capability_negotiation_session`。

**待本地验证**：以上代码需在 Zed workspace 内实际编译运行确认。

## 6. 本讲小结

- LSP 启动握手是两步：`initialize` 请求（携带 `InitializeParams`，含客户端能力）→ `InitializeResult`（含 `ServerCapabilities`）→ 补发 `initialized` 通知；握手完成前不得交换其他消息。
- `LanguageServer::initialize` 按值接收 `self`、返回 `Task<Result<Arc<Self>>>`，用类型系统锁住「谈判期间不许插队」；成功后用 `server_info` 回填 `version`/`process_name`，把 `ServerCapabilities` 存入 `RwLock` 字段。
- `default_initialize_params` 是 Zed 的能力底稿，分 general/workspace/textDocument/window/experimental 五个区段；`pull_diagnostics` 通过 `then_some` 控制拉取诊断字段**是否出现在 JSON 里**（缺失 ≠ 关闭），`augments_syntax_tokens` 是 lsp-types fork 的扩展字段。
- 能力声明有真实性能后果：completion 的 `resolve_support` 里 `textEdit` 被故意注释掉，避免补全变慢。
- `SEMANTIC_TOKEN_TYPES`/`SEMANTIC_TOKEN_MODIFIERS` 是语义高亮的图例，索引与位掩码都按它的顺序解释，因此只能尾部追加、不可重排。
- `capabilities` 用 `RwLock` 是因为初始化后只有共享引用（内部可变性）且读多写少；`update_capabilities` 的真实驱动力是运行时动态注册/注销——服务器能力清单本质上是客户端本地维护的视图。

## 7. 下一步学习建议

本讲之后，握手已完成、能力已就位，自然的下一讲是 **u2-l4（shutdown 关闭流程与 Drop 自动善后）**：看 `Shutdown` 请求、`SERVER_SHUTDOWN_TIMEOUT` 竞争、`Exit` 通知与 barrier 收尾如何为会话画上句号——综合实践的第 ④ 步已经预演了它的出口。

之后进入第三单元 RPC 核心：**u3-l1** 讲透本讲反复当黑盒用的 `notify` 与 `NotificationSerializer`（为什么发送的是序列化闭包）；**u3-l2** 展开 `request::<Initialize>` 背后的完整请求机制（id 分配、handler 表、oneshot）；**u3-l5** 则讲 `register_buffer`/workspace folders 这些构建在 `notify` 之上的便捷 API（本讲 4.2.3 里 `workspace_folder_for_uri` 的命名推导留到那里）。

对能力协商本身感兴趣的读者，可以对照 LSP 3.17 规范的 `InitializeParams`/`ClientCapabilities` 章节通读 [src/lsp.rs:L807-L1060](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L807-L1060)，逐个字段问一句「这个能力关掉会少什么功能」，并到 `crates/project/src/lsp_store/dynamic_registration.rs` 看动态注册如何驱动 `update_capabilities`。
