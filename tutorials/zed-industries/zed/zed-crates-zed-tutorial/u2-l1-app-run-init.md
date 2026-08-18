# app.run：全局 init 序列

## 1. 本讲目标

学完本讲，你应该能够：

- 把 `app.run` 闭包里近百个 `init` 调用整理成一张**分层时序图**（基础设施 → 服务 → UI 框架 → 面板 → 收尾装配）。
- 解释**为什么 init 顺序不能随意调换**：能指出「谁依赖谁的全局状态」，并能举出至少三个调换后会崩溃或行为异常的具体例子。
- 掌握 `cx.set_global` / `Xxx::set_global` 这类**全局状态注册**的清单与时机。
- 理解 `cx.observe_global::<SettingsStore>` 这类**配置观察者注册**如何把「设置变化」扩散到整个应用。
- 学会从一个 `init` 调用出发，追进对应 crate 的入口函数（本讲以 `settings::init` 与 `workspace::init` 为示范）。

## 2. 前置知识

### 2.1 GPUI 的全局状态（Global）

Zed 基于 GPUI 框架。GPUI 提供一个「应用级全局仓库」：任何类型只要实现 `impl Global for T {}`（空的标记 trait），就能用 `cx.set_global(value)` 放进去、用 `cx.global::<T>()` 读出来。它相当于一个**类型即键**的全局 HashMap。

- `cx.set_global(v)`：写入，后写覆盖先写。
- `cx.global::<T>()`：读取，**若从未写入会直接 panic**——这是理解「init 顺序不能乱」的关键：读取方必须排在写入方之后。
- `cx.try_global::<T>()`：安全版本，不存在时返回 `None`。

### 2.2 `xxx::init(cx)` 惯例

Zed 仓库的通用惯例：每个 crate 提供一个（或几个）无返回值的 `pub fn init(cx: &mut App)`（或带额外参数的变体），负责：

1. `set_global` 注册本 crate 的全局单例（注册表、仓库、配置存储等）；
2. 用 `cx.on_action` / `cx.observe_new` 等挂回调；
3. 加载内嵌资源（默认设置、默认主题、字体等）。

`crates/zed/src/main.rs` 的 `app.run` 闭包就是**按依赖顺序把几十个 crate 的 `init` 串起来**的总装线。本讲的核心就是读懂这条总装线。

### 2.3 观察者（observe_global）与订阅（subscribe）

- `cx.observe_global::<T>(callback).detach()`：当类型 `T` 的全局被 `set_global` 更新（或 `notify_global`）时触发回调。配置热加载全靠它。
- `cx.subscribe(&entity, callback).detach()`：订阅某个实体发出的事件。
- 返回的 `Subscription` 被 drop 时回调自动注销；应用级生命周期 的观察者通常直接 `.detach()` 常驻。

### 2.4 与前一讲的衔接

u1-l4 讲过 `main()` 前半部分：clap 参数解析、`init_paths()` 建目录、zlog 日志、单实例检查。本讲从 `app.run(...)` 这一行开始（[src/main.rs:478](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L478)），即「进程级准备全部完成，进入应用装配阶段」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/main.rs` | 本讲主战场：`app.run` 闭包（约 478-999 行）是完整的 init 总装线 |
| `src/zed.rs` | 提供 `zed::init`、`watch_settings_files`、`handle_keymap_file_changes` 等被 main 调用的装配函数 |
| `crates/settings/src/settings.rs` | `settings::init` 入口：创建 `SettingsStore` 并设为全局 |
| `crates/settings/src/settings_store.rs` | `SettingsStore` 本体：`impl Global`、`new`、文件监听 |
| `crates/workspace/src/workspace.rs` | `workspace::init` 入口：工作区 action 注册与子组件 init |

## 4. 核心概念与源码讲解

### 4.1 模块一：init 序列分层

#### 4.1.1 概念说明

`app.run` 闭包里有近百个调用，逐行背下来既不可能也无必要。正确的方法是把它们看成一条**分层流水线**：下层提供能力，上层消费能力。可以把依赖关系想象成一张有向无环图（DAG）——箭头从「被依赖方」指向「使用方」，分层就是这张图的拓扑排序结果：

\[ \text{第 } n \text{ 层的可执行性} = \text{其引用的第 } 1..n-1 \text{ 层全局均已注册} \]

粗略地可分为七层（行号以当前 HEAD 为准）：

| 层 | 大致行号 | 内容 | 代表调用 |
| --- | --- | --- | --- |
| L0 run 前备料 | 345-476 | 在 `app.run` **之前**创建、以闭包捕获传入 | `app_db`、`system_id`/`installation_id`/`session` 三个后台任务、`open_listener`、`fs`、crash handler、keymap watcher |
| L1 进程基础设施 | 479-499 | 数据库、菜单/action 骨架、版本、**设置系统**、日志设置、配置文件监听 | `cx.set_global(app_db)`、`release_channel::init`、`gpui_tokio::init`、`settings::init` |
| L2 网络与系统能力 | 501-527 | HTTP 客户端、文件系统、git 托管注册表、打开监听器、扩展代理、collab 客户端 | `cx.set_http_client`、`<dyn Fs>::set_global`、`Client::production` |
| L3 语言与存储服务 | 528-598 | 语言注册表、Node 运行时、用户/工作区存储、项目、调试器、feature flags | `LanguageRegistry::new`、`NodeRuntime::new`、`languages::init`、`project::Project::init` |
| L4 遥测与 AppState | 599-654 | 遥测启动、首启事件、会话、**AppState 全局枢纽** | `telemetry.start`、`AppSession::new`、`AppState::set_global` |
| L5 主题与 UI 框架 | 656-736 | 自动更新、可靠性、扩展宿主、主题、命令面板、AI、**编辑器与工作区** | `theme_settings::init`、`load_embedded_fonts`、`editor::init`、`workspace::init` |
| L6 面板与功能 UI | 738-787 | 约 40 个面向用户的功能模块 | `file_finder`、`project_panel`、`search`、`vim`、`terminal_view`、`git_ui`… |
| L7 收尾装配与启动 | 789-998 | 配置观察者、菜单、崩溃句柄、工作区装配、激活、恢复、打开循环 | `observe_global`、`cx.set_menus`、`initialize_workspace`、`cx.activate(true)` |

#### 4.1.2 核心流程

用伪代码描述这条流水线的骨架：

```text
app.run(cx):
    # L1 基础设施：先立规矩（设置系统最先可用）
    set_global(app_db)
    settings::init(cx)                 # <- SettingsStore 成为全局
    watch_settings_files / keymap 监听  # 配置文件开始被监听

    # L2 网络与系统：读设置、建连接
    http = ReqwestClient(proxy_url)     # proxy_url 来自设置！
    set_http_client(http)
    set_global(Fs / GitHostingProviderRegistry / OpenListener)
    client = Client::production(cx)     # collab 连接，复用 http client

    # L3 语言与存储
    languages = LanguageRegistry + 下载目录
    node_runtime = NodeRuntime(client.http_client, shell_env, rx)  # rx 来自设置观察者
    languages::init / UserStore / WorkspaceStore / language_extension::init
    Client::set_global(client)
    zed::init / project::Project::init / client::init

    # L4 遥测与全局枢纽
    block_on(system_id / installation_id / session)  # 等 L0 的后台任务
    telemetry.start(...)
    app_state = AppState { languages, client, user_store, fs, ... }
    AppState::set_global(app_state)     # <- 此后一切 "AppState::global(cx)" 才合法

    # L5 主题与 UI 框架
    theme_settings::init / load_embedded_fonts
    editor::init / workspace::init      # 编辑器先于工作区

    # L6 面板 UI：都挂在 workspace 之上
    file_finder / project_panel / search / vim / terminal_view / ...

    # L7 收尾
    observe_global::<SettingsStore>(...)  # 外观/文本渲染/服务器地址联动
    set_menus(app_menus(cx))
    initialize_workspace(app_state, cx)
    cx.activate(true)
    启动会话恢复 + open_rx 打开循环
```

为什么顺序不能乱？三个最有说服力的例子（下节源码逐一印证）：

1. `settings::init`（L1）必须先于一切 `XxxSettings::get_global`。例如 L2 里构造 HTTP 客户端要读 `ProxySettings::get_global(cx)`，若把 `settings::init` 挪到它之后，程序在启动时 panic。
2. `Client::production`（L2）依赖 HTTP client 已 `set_http_client`；随后几十个模块（`auto_update`、`language_models`、`web_search_providers`…）都拿着 `app_state.client` 或其 http client 初始化，因此 `AppState::set_global`（L4）必须先于它们。
3. `workspace::init`（L5，735 行）先于全部面板 init（L6，738 行起）：面板要往工作区的 action 体系上注册自己，顺序颠倒则注册无枝可依。

#### 4.1.3 源码精读

**入口：`app.run` 闭包开始**。[src/main.rs:478-499](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L478-L499) 中，闭包第一行把 `app_db`（L0 在 345 行创建的 `db::AppDatabase`）设为全局，随后 `trusted_worktrees::init`、`menu::init()`、`zed_actions::init()`（无参，仅注册 action 定义）依次执行，再到 `release_channel::init`、`gpui_tokio::init`、`settings::init(cx)`、`zlog_settings::init(cx)`、`zed::watch_settings_files`、`handle_keymap_file_changes`——L1 到此完成，**设置系统在此刻起可用**。

**L2：读设置、建 HTTP、注册系统能力**。[src/main.rs:501-527](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L501-L527)：先拼 user agent（用到 `AppVersion::global`，说明 `release_channel::init` 必须已完成），再 `ProxySettings::get_global(cx).proxy_url()`（用到设置系统），构造 `ReqwestClient` 并 `cx.set_http_client(Arc::new(http))`。紧接着 `<dyn Fs>::set_global`、`GitHostingProviderRegistry::set_global`、`OpenListener::set_global`、`extension::init`、`Client::production(cx)` 与 `cx.set_http_client(client.http_client())`——注意 collab 客户端会**替换**通用 HTTP 客户端为自己的。

**L3-L4：从语言服务到 AppState 枢纽**。[src/main.rs:528-565](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L528-L565)：`LanguageRegistry::new` 并指定语言服务器下载目录；`UserStore`/`WorkspaceStore` 两个实体在此创建；`language_extension::init` 用闭包 `LspAccess::ViaWorkspaces` 把「扩展的 LSP 请求」桥接到工作区存储；`Client::set_global`、`zed::init`、`project::Project::init`、`client::init` 随后。

[src/main.rs:595-654](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L595-L654) 是 L4 的核心：三个 `block_on` 等待 L0 备好的 `system_id`/`installation_id`/`session` 后台任务（在 346-356 行 spawn），随后 `telemetry.start(...)`、发出 `App First Opened` 系列事件、`AppSession::new`，最后构造 `AppState` 结构体并 `AppState::set_global(app_state.clone(), cx)`——这是整条总装线的**枢纽时刻**：`AppState` 汇集了 `languages`、`client`、`user_store`、`fs`、`build_window_options`、`workspace_store`、`node_runtime`、`session` 八个句柄（详见下一讲 u2-l2）。

**L5-L6：UI 框架与面板**。[src/main.rs:727-736](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L727-L736)：`load_embedded_fonts(cx)` 加载内嵌字体，然后 `editor::init(cx)`、`image_viewer::init`、`diagnostics::init`、`audio::init`、**`workspace::init(app_state.clone(), cx)`**、`ui_prompt::init`。到 [src/main.rs:738-787](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L738-L787) 是密集的 L6：`go_to_line`、`file_finder`、`project_panel`、`search`、`vim`、`terminal_view`、各 selector、`git_ui`、`settings_ui` 等，Windows 下还有条件编译的 `etw_tracing::init`。

**L7：收尾装配**。[src/main.rs:850-873](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L850-L873)：`app_menus(cx)` 构造菜单并 `cx.set_menus`；crash handler 完成后 `cx.set_global(CrashHandler(...))`；`initialize_workspace(app_state.clone(), cx)`（u4-l2 的主题）装配工作区观察者；最后 `cx.activate(true)` 让应用真正出现在用户面前。

#### 4.1.4 代码实践

**实践一：把 init 序列整理成分层时序图**（本讲指定实践任务的前半部分）。

1. **实践目标**：亲手把近百个 init 调用归入七层，形成一张可长期维护的「启动地图」。
2. **操作步骤**：
   - 打开 [src/main.rs:478-999](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L478-L999)，从 478 行向下逐段读。
   - 建一张 Markdown 表（或 Mermaid 图），列为：`行号 | 调用 | 所属层 | 它读取了谁的全局 | 它注册了什么全局`。
   - 遇到不确定归属的调用，问自己两个问题：它调用了 `Xxx::get_global`/`global(cx)` 吗（说明依赖 X 先 init）？它内部 `set_global` 吗（说明别人依赖它先 init）？
3. **需要观察的现象**：你会发现 `set_global` 调用集中在 L1-L4，而 L6 的面板 init 几乎只做 action 注册与观察者挂载，不再注册全局——这正是分层的证据。
4. **预期结果**：得到一张与 4.1.1 表格同构但更细的图，且每个 `set_global` 的写入行号都早于它的所有读取行号。

**实践二：追进 `settings::init` 与 `workspace::init`**（本讲指定实践任务的后半部分）。

1. **实践目标**：掌握「从 main.rs 的一个 init 调用跳进对应 crate 入口」的源码追踪方法。
2. **操作步骤**：
   - 在编辑器中把光标放在 [src/main.rs:496](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L496) 的 `settings::init` 上，用「Go to Definition」（默认 `gd` 或 F12）跳转，应落在 [crates/settings/src/settings.rs:127-131](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings.rs#L127-L131)：`SettingsStore::new(cx, &default_settings())` → `cx.set_global(settings)` → `SettingsStore::observe_active_settings_profile_name(cx).detach()`。其中 `default_settings()` 用 `RustEmbed` 内嵌的 `assets/settings/default.json` 作为默认值（[crates/settings/src/settings.rs:133-135](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings.rs#L133-L135)）；`impl Global for SettingsStore` 在 [crates/settings/src/settings_store.rs:245](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L245)。
   - 同样跳转 [src/main.rs:735](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L735) 的 `workspace::init`，落在 [crates/workspace/src/workspace.rs:784-826](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/workspace.rs#L784-L826)：先 `component::init()`、`theme_preview::init`、`toast_layer::init`、`history_manager::init`，再连续 `cx.on_action` 注册 `CloseWindow`/`Reload`/`Open`/`OpenFiles` 四个 workspace 级 action。
3. **需要观察的现象**：两个入口函数都非常短（5 行与 43 行），且都遵循「创建/获取全局 + 挂回调」的模式；`workspace::init` 中 `Open` action 的处理还读取了 `WorkspaceSettings::get_global(cx).default_open_behavior`——再次印证它必须排在 `settings::init` 之后。
4. **预期结果**：你能不看资料说出 `settings::init` 做的三件事，以及 `workspace::init` 注册了哪四个 action。

#### 4.1.5 小练习与答案

**练习 1**：如果把 [src/main.rs:507](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L507) 的 `ProxySettings::get_global(cx)` 移到 `settings::init(cx)` 之前会发生什么？

**答案**：`get_global` 内部调用 `cx.global::<SettingsStore>()`，而全局尚未注册，GPUI 会直接 panic（类似 "global was not set"）。这就是「L1 设置系统必须最先 init」的机械原因：所有 `*Settings::get_global` 都是类型化全局读取。

**练习 2**：`app.run` 闭包中共出现三次 `cx.foreground_executor().block_on`（[src/main.rs:595-597](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L595-L597)），它们在等什么？为什么可以放心 block？

**答案**：在等 L0 备料阶段（346-356 行）用 `app.background_executor().spawn` 启动的三个后台任务：`system_id`、`installation_id`、`Session::new`。它们是纯后台 I/O（读/写 KVP 存储），不依赖主线程状态，所以在前台 `block_on` 不会死锁；拿到的值随即喂给 `telemetry.start` 与 `AppSession::new`。

**练习 3**：`Client::production(cx)`（[src/main.rs:526](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L526)）之后又调了一次 `cx.set_http_client(client.http_client())`（527 行）。为什么不沿用 514 行设置的通用 HTTP 客户端？

**答案**：514 行先设置了一个基于 `ReqwestClient` 的通用 HTTP 客户端，供早期需要网络的初始化使用；`Client::production` 创建 collab 客户端后，用它**自己的** http client 覆盖全局——后续所有走全局 HTTP 的模块（扩展下载、语言服务器下载等）就会与 collab 客户端共享连接配置（含服务器地址）。这是「后写覆盖先写」的 `set_global` 语义在 HTTP client 上的体现。

### 4.2 模块二：全局状态注册（set_global）

#### 4.2.1 概念说明

「全局状态注册」是 init 序列的骨架。`app.run` 闭包里 `set_global` 的每一次出现，都是在往 GPUI 全局仓库放入一个**后续大量代码要读取的句柄**。可以把它们分成三类：

1. **数据与服务单例**：`AppDatabase`、`SettingsStore`、`Fs`、`Client`、`AppState`、`CrashHandler`。
2. **注册表（registry）**：`GitHostingProviderRegistry`、`OpenListener`——各处代码向其注册/查询。
3. **回调容器**：`workspace::PaneSearchBarCallbacks`——不是数据，而是把函数指针放进全局供跨 crate 调用。

理解每个全局「何时写、谁在读」，就理解了 init 顺序的全部理由。

#### 4.2.2 核心流程

`app.run` 闭包内的 `set_global` 时间线（按行号）：

```text
479  cx.set_global(app_db)                          # AppDatabase：工作区数据库
494  AppCommitSha::set_global(app_commit_sha)       # 提交 SHA（仅存在时）
514  cx.set_http_client(Arc::new(http))             # 通用 HTTP 客户端
516  <dyn Fs>::set_global(fs)                       # 真实文件系统
518  GitHostingProviderRegistry::set_global(...)    # git 托管提供方注册表
521  OpenListener::set_global(cx, open_listener)    # 打开请求信箱
527  cx.set_http_client(client.http_client())       # 用 collab 客户端的 http 覆盖
584  Client::set_global(client)                     # collab 客户端本体
654  AppState::set_global(app_state)                # ★ 全局枢纽
750  cx.set_global(workspace::PaneSearchBarCallbacks { ... })  # 回调容器
857/863 cx.set_global(CrashHandler(...))            # 崩溃句柄（异步就绪后）
```

另有**藏在被调 init 内部**的注册，例如 `settings::init` 里的 `cx.set_global(settings)`（[crates/settings/src/settings.rs:129](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings.rs#L129)）与 `SettingsStore::new` 内部的 `cx.set_global::<DefaultSemanticTokenRules>`（[crates/settings/src/settings_store.rs:300](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings/src/settings_store.rs#L300)）。追踪时不要只看 main.rs 表面。

#### 4.2.3 源码精读

**AppState：枢纽全局**。[src/main.rs:644-654](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L644-L654)：`AppState` 结构体在此组装八个字段（`languages`、`client`、`user_store`、`fs`、`build_window_options`、`workspace_store`、`node_runtime`、`session`）后立即 `set_global`。此后 L5-L7 的大量 init 通过参数传递或 `AppState::global(cx)` 获取它——例如 u1-l4 提过的 `app.on_reopen` 回调里就用了 `AppState::try_global(cx)`（[src/main.rs:466](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L466)）。

**回调容器型全局**。[src/main.rs:750-758](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L750-L758)：`workspace::PaneSearchBarCallbacks` 不是状态而是两个函数——`setup_search_bar`（创建 `search::BufferSearchBar` 并挂到工具栏）与 `wrap_div_with_search_actions`。zed crate 在此把 search crate 的能力「注入」给 workspace crate，避免 workspace 反向依赖 search。这是用全局做**控制反转**的典型手法。

**异步就绪的全局**。[src/main.rs:853-869](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L853-L869)：crash handler 是 L0 在后台 spawn 的任务，此处先 `poll_once` 试取；若已就绪直接 `set_global(CrashHandler(...))`，否则 spawn 一个任务等待完成后补注册。说明全局注册不总发生在 init 直线上，也可以异步补齐——但读取方必须用 `try_global`（如 606-619 行订阅 `user_store` 时对 `CrashHandler` 的取用）。

#### 4.2.4 代码实践

1. **实践目标**：制作一份「全局状态注册清单」，作为日后排查启动问题与阅读其他 init 的索引。
2. **操作步骤**：
   - 对 4.2.2 列出的每个全局，在仓库里用文本搜索统计读取点：例如搜 `AppState::global` 与 `AppState::try_global`，统计出现在多少个文件；再搜 `Client::global`。
   - 对 `workspace::PaneSearchBarCallbacks`，搜索它在 workspace crate 内的读取位置，确认「zed 写入、workspace 读取」的方向。
3. **需要观察的现象**：`AppState` 的读取点应远多于写入点（写入仅 654 行一处）；`CrashHandler` 的读取都用 `try_global`，与其「异步补注册」的特性吻合。
4. **预期结果**：清单中每一行都有「写入行号 / 读取文件数 / 读取方式（global vs try_global）」三列，且能回答「哪个全局的读取方式最特殊、为什么」。
5. 搜索命令的精确计数因仓库演进会变化，具体数字**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`AppState::set_global` 传的是 `Arc<AppState>`，而后续很多 init 又通过**参数**接收 `&AppState`（如 `copilot_ui::init(&app_state, cx)`）。既然有全局，为什么还要传参？

**答案**：两者并存各有用途——参数传递让依赖显式、便于测试时注入；全局（`AppState::global(cx)`）方便在无法传参的回调深处（如 `app.on_reopen`）取用。Zed 的惯例是 init 尽量走参数，回调深处才退回全局。

**练习 2**：为什么读取 `CrashHandler` 用 `cx.try_global`（[src/main.rs:610](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L610)），而读取 `SettingsStore` 可以直接 `get_global`？

**答案**：`SettingsStore` 在 L1 同步注册，读取点必然在其后，直接 `global` 不会失败；`CrashHandler` 依赖后台任务完成、可能尚未注册（且在 Dev 通道 `should_install_crash_handler` 为假时根本不会有），必须用 `try_global` 优雅降级。

### 4.3 模块三：配置观察者注册

#### 4.3.1 概念说明

前两个模块解决「启动时装配」，本模块解决「**运行时热更新**」。Zed 的配置（settings.json、keymap.json、主题）改动后无需重启即生效，其机制是三段式：

```text
文件变化（fs watcher）
    → SettingsStore 重新解析并 set_global（触发 observe_global 回调）
    → 各模块的观察者读取新值，调整自身行为
```

`app.run` 闭包及紧邻处注册了多个观察者，负责把「设置变了」这一事件翻译成具体动作。它们大多在 L7（789 行起）统一注册，个别（如 Node 路径）在 L3 提前注册，因为 `NodeRuntime` 需要在创建时就接到设置的「当前值流」。

#### 4.3.2 核心流程

配置观察者的三条注册线：

1. **Node 运行时设置流（L3，531-558 行）**：`watch::channel(None)` 建通道 → `observe_global::<SettingsStore>` 每次设置变化都把最新 `NodeBinaryOptions`（由 `node.ignore_system_version`、`node.path`、`node.npm_path` 拼出）`tx.send` 进通道 → `NodeRuntime::new(.., rx)` 持有接收端，随时读到最新选项。
2. **设置→外观/网络联动（L7，789-823 行）**：一个大观察者同时处理三件事——为所有窗口应用 `window_background_appearance`、按 `text_rendering_mode` 调用 `cx.set_text_rendering_mode`、当 collab `server_url` 变化时 `http.set_base_url` 并在已连接时触发 `client.reconnect`。
3. **主题→语言高亮联动（L7，824-831 行）**：先立即 `languages.set_theme(cx.theme().clone())` 一次，再 `observe_global::<GlobalTheme>` 订阅后续主题变化。

此外还有两个不在 main.rs 闭包内、但同属「配置观察者」的装配函数（都在 zed.rs）：`watch_settings_files`（文件级监听入口，由 main 498 行调用）与 `handle_keymap_file_changes`（keymap 监听，由 main 499 行调用）。

#### 4.3.3 源码精读

**Node 设置流**。[src/main.rs:531-558](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L531-L558)：`cx.observe_global::<SettingsStore>(move |cx| { ... tx.send(Some(options)).log_err(); }).detach()` 把每次设置变化转换为 `NodeBinaryOptions` 并发送；`NodeRuntime::new(client.http_client(), Some(shell_env_loaded_rx), rx)` 用接收端构造运行时。注意 `ui::on_new_scrollbars::<SettingsStore>(cx)`（556 行）也是一种观察者注册——为设置的 `ui_font_size` 变化重建滚动条样式（细节属 u5-l1 范围）。

**设置→外观/网络联动**。[src/main.rs:789-823](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L789-L823)：单个回调里遍历 `cx.windows()` 逐个 `window.update` 设置背景外观；然后映射 `TextRenderingMode` 三分支（PlatformDefault/Subpixel/Grayscale）；最后比较 `http.base_url()` 与新的 `server_url`，不一致则切换并按连接状态重连。一个观察者管三件事，是因为它们都只关心「设置变了」这一信号。

**主题→语言联动与主题装载**。[src/main.rs:824-848](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L824-L848)：`app_state.languages.set_theme(...)` 立即生效一次，`observe_global::<GlobalTheme>` 保持后续同步；随后 `load_user_themes_in_background` 与 `watch_themes` 启动主题目录的后台装载与监听，`watch_languages` 仅在 `debug_assertions` 下编译——开发模式下保存 `.scm` 查询文件即可热重载语法高亮（详见 u5-l3）。

**文件级监听入口**。[src/zed.rs:2190-2209](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L2190-L2209)：`watch_settings_files` 先 `MigrationNotification::set_global`（迁移提示的实体全局），再 `SettingsStore::update_global` 调用 store 的 `watch_settings_files`，回调里对每次解析结果调用 `notify_settings_errors`（错误分级通知）并向 `MigrationNotification` 发 `MigrationEvent::ContentChanged` 事件。

**keymap 监听**。[src/zed.rs:2211-2235](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L2211-L2235)：`handle_keymap_file_changes` 同时监听「keymap 文件内容」（`user_keymap_file_rx`）与「设置中的 base keymap / vim / helix / disable_ai」（`observe_global::<SettingsStore>`），任一变化都会触发 `reload_keymaps`（完整逻辑在 u5-l2 展开）。

#### 4.3.4 代码实践

1. **实践目标**：亲眼验证「设置变化 → 观察者触发」链路，并定位触发点行号。
2. **操作步骤**：
   - 本地 `cargo run`（仓库根目录）启动 Zed，打开 `~/.config/zed/settings.json`（Linux）。
   - 把 `\"ui_font_size\": 16` 改为 `\"ui_font_size\": 20` 保存。
   - 对照 4.3.3 的三处源码，思考这次变化依次触发了哪些观察者（文件 watcher → SettingsStore 重解析 → `observe_global::<SettingsStore>` 各回调；字体大小则由编辑器/GPUI 层的设置消费）。
   - 再把 `\"proxy\"` 一节加上一个非法 URL 保存，观察 `notify_settings_errors` 路径产生的界面提示。
3. **需要观察的现象**：界面字号无需重启立即变化；非法设置保存后出现错误通知，且不影响其他合法字段继续生效。
4. **预期结果**：能把「看到的现象」与「531-558 / 789-823 / zed.rs 2190-2209」三段代码一一对应。
5. 若本地无法编译运行（平台或环境限制），改为纯源码追踪并标注**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Node 设置观察者（531 行）必须注册在 `NodeRuntime::new`（558 行）**之前**，而外观观察者（789 行）可以放在 init 序列末尾？

**答案**：`NodeRuntime` 通过 `rx`（watch channel 接收端）获取设置，通道必须先建立并被观察者持有发送端，运行时创建时才能拿到；且该观察者只服务 NodeRuntime 一个消费者，随其就近放置。外观观察者消费的是「已存在的窗口列表」，只要在首个窗口创建前注册即可，放末尾不影响正确性。前者是「构造期依赖」，后者是「运行期响应」。

**练习 2**：设置变化时 `observe_global::<SettingsStore>` 的多个回调都会执行。它们之间有顺序保证吗？从 [src/main.rs:532](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L532) 与 [src/main.rs:789](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L789) 两处注册推断。

**答案**：回调按注册顺序执行（先 532 行的 Node 选项回调，后 789 行的外观回调），这是 GPUI 观察者列表的天然语义。但**不应**依赖这个顺序写代码——两个回调消费的设置互不相关，正确性不建立在执行次序上；若存在依赖，应合并到同一回调或改用显式数据流。

## 5. 综合实践

**任务：为 init 总装线写一份「依赖审计报告」并验证一个真实依赖。**

1. 通读 [src/main.rs:478-999](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L478-L999)，产出两张表：
   - 表 A（分层表）：每个调用归入 4.1.1 的 L1-L7，并标注它**读取**的全局与**注册**的全局。
   - 表 B（依赖边表）：形如 `settings::init (496) → ProxySettings::get_global (507)`、`Client::production (526) → set_http_client (514)`，至少列出 10 条边。
2. 从表 B 中挑一条**你认为最可疑**的边（即「如果调换会出错」的边），做一个思想实验并验证：
   - 思想实验：写出调换后第一个会失败的调用与失败方式（panic 于哪个 `global`，或行为如何劣化）。
   - 验证：在本地拉出分支，把这两行的顺序真的对调，`cargo check -p zed` 看编译期是否报错；若编译通过则 `cargo run` 观察运行期表现。**实验后务必还原**（`git checkout -- crates/zed/src/main.rs`），不要提交。
3. 追进两个入口函数（若尚未完成 4.1.4 实践二）：`settings::init` 与 `workspace::init`，各用三句话总结其行为，附在报告末尾。
4. 交付物：`zed-crates-zed-tutorial/` 之外的个人笔记中保存表 A、表 B 与实验记录（本讲义目录只存放讲义本身）。

预期成果：你将得到一份属于自己的启动地图，后续阅读 u2-l2（AppState）、u4（窗口装配）、u5（配置热加载）时，随时可以回到这张地图定位上下文。

## 6. 本讲小结

- `app.run` 闭包是一条**分层流水线**：基础设施（设置最先）→ 网络与系统能力 → 语言与存储服务 → 遥测与 AppState → 主题与 UI 框架（editor、workspace）→ 面板 UI → 收尾装配（菜单、观察者、激活、会话恢复）。
- init 顺序的机械约束来自 GPUI 全局仓库：**读取 `cx.global::<T>()` 必须排在 `set_global` 之后**，否则 panic；`try_global` 用于可能缺席的全局（如异步注册的 `CrashHandler`）。
- `AppState::set_global`（654 行）是总装线的枢纽时刻，汇集八个核心句柄；`workspace::PaneSearchBarCallbacks` 展示了「全局存放回调」的控制反转手法。
- 配置热加载三段式：fs watcher → `SettingsStore` 重解析并触发 `observe_global::<SettingsStore>` → 各观察者响应；Node 设置走 watch channel 流，外观/网络/主题走注册在末尾的观察者。
- 追踪方法：对任一 `xxx::init`，先问「它读了谁的全局、注册了什么全局、挂了哪些回调」，再跳进对应 crate 的入口函数（如 `settings::init` 五行、`workspace::init` 四个 action 注册）。

## 7. 下一步学习建议

- **下一讲 u2-l2（AppState、身份标识与遥测）**：本讲把 `AppState::set_global` 当作黑点，下一讲拆开它——`system_id`/`installation_id` 的读取与 legacy 迁移、`AppSession` 与 `Session` 的关系、首启遥测事件的判定分支。
- **u2-l3（会话恢复）**：继续沿本讲 L7 末尾的 `restore_or_create_workspace`（923-948 行）深入。
- **延伸阅读源码**：`crates/settings/src/settings_store.rs` 的 `watch_settings_files` 与 `set_global_settings`（本讲只用了入口），为 u5-l1 做准备；`crates/workspace/src/workspace.rs` 的 `init` 之后的 `Workspace` 结构体定义，为单元 4 做准备。
