# 后端启动与 Tauri 应用装配

## 1. 本讲目标

本讲承接上一讲「前端启动流程（main.tsx → App.tsx）」，把视角切到 **Rust 后端**，回答一个问题：

> 用户双击 CC Switch 图标后，Rust 这一侧到底做了什么，才让前端能够正常拉起？

学完本讲，你应当能够：

1. 读懂 `main.rs` 入口里的平台特殊处理，并追踪到 `cc_switch_lib::run()`。
2. 说出 `tauri::Builder` 的三段式装配：**插件链 → setup 钩子 → 命令处理器**。
3. 按**执行顺序**列出 setup 钩子里至少六个关键初始化步骤（数据库版本预检、数据库初始化、迁移、种子、MCP 导入……）。
4. 说清本版本（edeee25f）新增的「数据库版本过新」预检：它在 `Database::init()` 之前如何拦截、如何写 `InitErrorPayload`、如何强制显示主窗口并提前返回。
5. 说出全局状态 `AppState` 的三个字段，以及它如何被各命令共享。

本讲只建立「后端启动主线」的地图，不深入单个子系统的实现细节——那些是后续 U2～U9 各单元的主题。

> **本次更新（update）说明**：edeee25f 提交给后端启动链路带来了一条新的「可恢复错误」分支——当磁盘上的数据库版本比当前应用支持的版本更新时，应用不再闪退或反复弹无效重试框，而是在 setup 里提前返回 `Ok(())`、强制显示主窗口，由前端渲染「升级应用」恢复界面。本讲据此新增 **4.4 数据库版本预检与恢复早退** 这一最小模块，并刷新全部永久链接的 HEAD 与行号。

## 2. 前置知识

本讲需要你已建立以下认知（来自 u1-l1 ～ u1-l4）：

- **技术栈**：CC Switch 是 Tauri 2 应用，前端 React，后端 Rust，两者通过 **Tauri IPC**（`invoke` 调命令 / `listen` 收事件）通信。
- **分层架构**：后端是 `Commands → Services → DAO → Database` 的分层结构。
- **SSOT**：可同步数据集中存在 SQLite 数据库 `~/.cc-switch/cc-switch.db`。
- **前端 fail-fast**：上一讲讲过，前端 `main.tsx` 的 `bootstrap()` 会先调 `invoke("get_init_error")` 询问后端「你启动时有没有出错」。本讲就要解释：**后端是如何决定要不要把错误塞进 `get_init_error` 的**——尤其是 edeee25f 新增的 `kind === "db_version_too_new"` 分支，前端据此渲染 `DatabaseUpgrade` 恢复界面而非直接退出。

再补充几个本讲会用到的 Tauri 概念，对不熟悉桌面开发的读者先做铺垫：

| 术语 | 通俗解释 |
| --- | --- |
| `tauri::Builder` | 组装一个 Tauri 应用的「流水线 builder」。你往上面挂插件、挂 setup 钩子、注册命令，最后 `.build()` 出一个可运行的应用。 |
| 插件（plugin） | 提供一组能力的模块，例如「对话框」「自动更新」「单实例」。挂上即生效。 |
| setup 钩子 | 一个在应用「即将启动、窗口还未显示」时执行的闭包，几乎所有「启动时要一次性初始化」的逻辑都写在这里。它返回 `Result<(), Box<dyn Error>>`，可以**提前 `return Ok(())`** 跳过后续步骤。 |
| `app.manage(state)` | 把一个 Rust 对象注册进 Tauri 的「托管状态池」，之后命令函数可以通过 `tauri::State<'_, AppState>` 取到同一个实例。 |
| `invoke_handler` | 声明「前端可以 `invoke` 哪些 Rust 函数」的白名单。没在这里注册的 `#[tauri::command]`，前端调不到。 |

## 3. 本讲源码地图

本讲涉及五个关键文件，各自在后端启动链路中的角色如下：

| 文件 | 行数级 | 角色 |
| --- | --- | --- |
| [src-tauri/src/main.rs](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/main.rs) | 极短（22 行） | 进程入口：平台环境变量修正，然后转交 `cc_switch_lib::run()`。 |
| [src-tauri/src/lib.rs](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs) | 巨大（2000+ 行） | 真正的装配现场：`run()` 函数、Builder 插件链、setup 钩子、命令注册、退出清理全在这里。 |
| [src-tauri/src/store.rs](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/store.rs) | 极短（23 行） | 定义全局状态 `AppState`（db / proxy_service / usage_cache）。 |
| [src-tauri/src/init_status.rs](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/init_status.rs) | 短（约 125 行） | 启动期「一次性状态」的静态仓库：初始化错误（含本版本新增的 `kind`/`db_version`/`supported_version`）、迁移结果等，供前端拉取。 |
| [src-tauri/src/database/mod.rs](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/database/mod.rs) | 中（200+ 行入口部分） | `Database::init()`、`SCHEMA_VERSION` 常量，以及本版本新增的版本预检 `stored_user_version_exceeds_supported()`。 |

一句话记住分工：**`main.rs` 是门，`lib.rs::run()` 是装配车间，`store.rs` 造全局状态，`init_status.rs` 是给前端递话的信箱，`database/mod.rs` 是被预检/初始化的数据底座。**

## 4. 核心概念与源码讲解

本讲拆成五个最小模块，正好对应启动链路的五个阶段：

- **4.1 main.rs 入口** —— 进程怎么进来的。
- **4.2 Builder 与插件注册** —— 装配流水线怎么搭起来的。
- **4.3 setup 钩子初始化序列** —— 启动时按顺序做了哪些事（本讲最长）。
- **4.4 数据库版本预检与恢复早退** —— 本版本（edeee25f）新增的可恢复错误分支。
- **4.5 AppState 全局状态** —— 启动产物如何被后续命令共享。

### 4.1 main.rs 入口

#### 4.1.1 概念说明

每个 Rust 程序都从 `fn main()` 开始。Tauri 应用的 `main.rs` 通常**很薄**——它只做两件事：

1. 处理几个必须在进程最早期、甚至建窗口之前就设定的**平台级环境**。
2. 把控制权交给真正的库入口 `cc_switch_lib::run()`。

之所以把核心逻辑放进 `lib.rs` 而非 `main.rs`，是因为 Tauri 的移动端入口（`#[tauri::mobile_entry_point]`）需要复用同一段装配代码，`main.rs` 只服务于桌面端。

#### 4.1.2 核心流程

```text
进程启动
   │
   ├─（release 模式，Windows）隐藏多余控制台窗口
   │
   ├─（Linux）设置 WebKit 环境变量，规避渲染/合成 bug
   │
   └─ cc_switch_lib::run()   ←── 全部逻辑在这里
```

#### 4.1.3 源码精读

文件开头这行是「防误删」标记，作用是 release 模式下不在 Windows 上弹出黑色控制台窗口：

[src-tauri/src/main.rs:1-2](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/main.rs#L1-L2) —— `windows_subsystem = "windows"` 属性，注释明说 DO NOT REMOVE。

接着是 Linux 专用的 WebKit 环境变量修正。Tauri 在 Linux 上用 WebKitGTK 渲染界面，部分系统会出现白屏/黑屏或 Wayland 下点击无响应，这里在进程最早期把两个开关关掉：

[src-tauri/src/main.rs:8-19](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/main.rs#L8-L19) —— 设置 `WEBKIT_DISABLE_DMABUF_RENDERER` 与 `WEBKIT_DISABLE_COMPOSITING_MODE`，且都用 `is_err()` 判断「仅在用户没自己设过时才设」，避免覆盖用户自定义。

最后，所有平台殊途同归，调用库入口：

[src-tauri/src/main.rs:21](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/main.rs#L21) —— `cc_switch_lib::run();`。注意这里的 `cc_switch_lib` 对应 `Cargo.toml` 里的 `[lib] name = "cc_switch_lib"`（即 `src/lib.rs`）。

> 小知识：`#[cfg(target_os = "linux")]` 这类「条件编译」是 Rust 的特性，编译器在目标平台不是 Linux 时会**直接删掉**这段代码，所以 Windows/macOS 的产物里不会有 WebKitGTK 相关调用。

#### 4.1.4 代码实践

**实践目标**：确认入口的薄与厚分工。

1. 打开 `src-tauri/src/main.rs`，确认它只有 22 行，且唯一一次「干活」的调用是 `cc_switch_lib::run()`。
2. 打开 `src-tauri/Cargo.toml`，找到 `[lib]` 段，确认 `name = "cc_switch_lib"`，从而理解 `main.rs` 里的 `cc_switch_lib::` 前缀从何而来。
3. **需要观察的现象**：`main.rs` 里没有任何业务逻辑、没有数据库、没有命令注册——这些都得去 `lib.rs` 找。

预期结果：你能用一句话解释「为什么 main.rs 这么短」。无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：如果删掉 `main.rs` 第 2 行的 `windows_subsystem` 属性，在 Windows release 版本会发生什么？

> **答案**：应用启动时会额外弹出一个黑色控制台窗口跟着主窗口一起出现。注释明确标注 `DO NOT REMOVE!!` 就是防止这个。

**练习 2**：为什么 Linux 的 WebKit 修正要放在 `main.rs` 最早期，而不是放进 setup 钩子？

> **答案**：环境变量必须在 WebKitGTK 初始化（建 webview）之前设定才生效；setup 钩子执行时窗口/webview 通常已经在初始化，太晚了。

### 4.2 Builder 与插件注册

#### 4.2.1 概念说明

`tauri::Builder::default()` 是一个**链式装配器**：你不断调用 `.plugin(...)`、`.setup(...)`、`.invoke_handler(...)` 往上面「挂」东西，最后 `.build()` 生成可运行的应用。CC Switch 的 `run()` 函数主体就是这条链。

装配分三大段，**顺序很关键**：

1. **插件链**（`.plugin(...)`）：声明应用具备哪些能力。
2. **setup 钩子**（`.setup(|app| { ... })`）：应用将起未起时的一次性初始化（下一节详讲）。
3. **命令处理器**（`.invoke_handler(generate_handler![...])`）：向前端开放的 RPC 白名单。

本模块只讲第 1、3 段与整体框架，第 2 段留给 4.3～4.4。

#### 4.2.2 核心流程

```text
run()
 ├─ panic_hook::setup_panic_hook()      ← 第一件事：装崩溃日志兜底
 │
 ├─ Builder::default()
 │    ├─ .plugin(single_instance)       ← 只允许跑一个实例
 │    ├─ .plugin(deep_link)             ← ccswitch:// 协议
 │    ├─ .on_window_event(...)          ← 拦截「关闭」（含 DB 恢复模式的特殊处理）
 │    ├─ .plugin(process / dialog / opener / store / window_state)
 │    └─ .setup(|app| { ... 200+ 行 ... })   ← 4.3 / 4.4 详讲
 │
 ├─ .invoke_handler(generate_handler![ 150+ 个命令 ])
 │
 ├─ builder.build(generate_context!())  ← 生成 App
 └─ app.run(|app_handle, event| { ... }) ← 事件循环 + 退出清理
```

#### 4.2.3 源码精读

`run()` 的开头第一件事不是建 Builder，而是先装「崩溃日志兜底」——即使后续代码 panic，也能把堆栈写进 `~/.cc-switch/crash.log`，避免「闪退且无任何线索」：

[src-tauri/src/lib.rs:219-224](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L219-L224) —— `pub fn run()` 带 `#[cfg_attr(mobile, tauri::mobile_entry_point)]`（移动端复用此入口），第一行就是 `panic_hook::setup_panic_hook();`，然后才 `let mut builder = tauri::Builder::default();`。

接着是**单实例插件**。CC Switch 只允许同时运行一个实例：第二次启动时不会开新窗口，而是把已存在的实例唤醒到前台，并处理可能携带的 Deep Link URL：

[src-tauri/src/lib.rs:226-265](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L226-L265) —— `tauri_plugin_single_instance::init(...)` 的回调里：遍历命令行参数找 `ccswitch://`、退出轻量模式、把主窗口 unminimize/show/set_focus。

> 注意：这里用 `#[cfg(any(target_os = "macos", target_os = "windows", target_os = "linux"))]` 包裹，意味着**只在桌面三平台**启用单实例。

然后是一连串 `.plugin(...)` 与 `.on_window_event(...)`，用一条链式调用挂上去。其中窗口关闭事件的处理因本次更新增加了一个分支：

[src-tauri/src/lib.rs:271-301](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L271-L301) —— `.on_window_event(...)` 拦截 `CloseRequested`。**新增逻辑（[lib.rs:273-281](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L273-L281)）**：先读 `init_status::get_init_error()`，若 `kind == "db_version_too_new"`，说明此刻处于「数据库恢复界面」——此时托盘还没被创建（见 4.4，预检提前返回后整个平台集成段被跳过），关闭窗口若走「最小化到托盘」会让应用隐身后台无法唤回，因此直接 `prevent_close` + `exit(0)` 真正退出。其余情况再按 `minimize_to_tray_on_close` 设置决定最小化还是退出。

随后挂载常规插件链：

[src-tauri/src/lib.rs:302-310](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L302-L310) —— 依次 `tauri_plugin_process` / `dialog` / `opener` / `store` / `window_state`（进程、对话框、打开外链、键值存储、记忆窗口位置）。

> 这里有一个容易忽略的细节：**Updater 和日志插件没有在这条链上**，而是放进了 setup 钩子里动态注册（见 4.3），因为它们依赖 app config 目录，要先确定路径才能初始化。

链的最后一环是 `.setup(|app| { ... })`，它就是 4.3 的主角。

setup 之后是 `.invoke_handler(...)`——这是「前端能调哪些后端命令」的白名单。注意它是一个**宏** `tauri::generate_handler![...]`，里面列出所有要暴露的命令函数：

[src-tauri/src/lib.rs:1184-1185](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L1184-L1185) —— `.invoke_handler(tauri::generate_handler![ commands::get_providers, ... ])`，列表长达 150+ 项（从 `get_providers` 一直到 `is_lightweight_mode`，结束于 [lib.rs:1499-1500](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L1499-L1500)）。任何一个 `#[tauri::command]` 函数，如果没出现在这个列表里，前端 `invoke` 会直接报「找不到命令」。

> **本次更新新增命令**：`commands::check_app_update_available`（注册在 [lib.rs:1229](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L1229)）。它专供「数据库恢复界面」调用——前端用它在升级流程里探测「是否有新版本可装」，从而决定展示「立即升级」按钮还是「已是最新但仍不兼容」的提示（详见 [commands/settings.rs:275-285](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/commands/settings.rs#L275-L285)）。

链装配完毕，`.build()` 生成应用并进入事件循环：

[src-tauri/src/lib.rs:1501-1505](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L1501-L1505) —— `builder.build(tauri::generate_context!())` 生成 `app`，随后 `app.run(|app_handle, event| { ... })` 启动事件循环。`generate_context!()` 会在编译期读取 `tauri.conf.json` 把配置烘焙进二进制。

#### 4.2.4 代码实践

**实践目标**：把「插件链 → setup → 命令白名单 → build」这条骨架亲手数一遍。

1. 在 `lib.rs` 搜索 `.plugin(`，数一下链上直接挂了几个插件（提示：single_instance、deep_link、process、dialog、opener、store、window_state）。
2. 跳到 `invoke_handler` 那一行（约 1184 行），随便挑三个命令名（如 `get_providers`、`switch_provider`、`get_init_error`），确认它们都来自 `commands::`；再定位新增的 `check_app_update_available`（约 1229 行）。
3. **需要观察的现象**：Updater 与 log 插件**不在**这条 `.plugin(` 链上；窗口关闭处理里有针对 `db_version_too_new` 的特殊分支。

预期结果：你能画出 run() 的四段式骨架。本实践为「源码阅读型」，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Updater 插件不在 `.plugin(...)` 链上，而要放进 setup 钩子？

> **答案**：因为 Updater 需要读取 app config 目录里的配置/签名，而 config 目录的覆盖路径（`app_config_dir_override`）要等 setup 开始时刷新后才确定，所以只能动态注册。

**练习 2**：如果某天你新增了一个 `#[tauri::command] fn foo()`，但忘了加进 `generate_handler!`，会怎样？

> **答案**：编译能过，但前端 `invoke("foo")` 运行时会失败（命令未注册）。`invoke_handler` 是运行期白名单，不是编译期检查。

### 4.3 setup 钩子初始化序列

#### 4.3.1 概念说明

`.setup(|app| { ... })` 是启动链路里**最重**的一段：它是一个闭包，签名要求返回 `Result<(), Box<dyn Error>>`，在应用「即将启动、窗口尚未显示」时同步执行。CC Switch 几乎所有「启动时只做一次」的初始化都集中在这里——从建数据库、跑迁移、种子官方预设，到导入 MCP/Prompts、建托盘、拉起云同步 worker，最后决定窗口显不显示。

理解 setup 的关键不是记住每一行，而是抓住它的**设计纪律**：

1. **早期确定路径**：先刷新 config 目录覆盖、装 panic hook 的目录、注册日志，确保后续所有「读路径/写日志」都指向正确位置。
2. **失败可恢复**：数据库初始化和旧配置迁移都包了「弹原生对话框 → 用户选重试/退出」的循环，而不是直接 panic；本版本还新增了「版本过新 → 走应用内升级」的第三条恢复路径（见 4.4）。
3. **按表独立导入**：Skills / Providers / MCP / Prompts 各自检查「表是否为空」，空才导入，互不影响。
4. **重活扔进异步任务**：crash 恢复、周期备份、会话用量同步等耗时项，用 `tauri::async_runtime::spawn` 放后台，不阻塞窗口显示。

#### 4.3.2 核心流程

setup 闭包内大致执行顺序（编号与源码注释里的「1./1.1/1.5/1.6/2./3./4.」基本对应）。注意 **(b′) 预检分支会提前 `return Ok(())`**，一旦命中就跳过其后所有步骤：

```text
setup(|app|)
 │
 ├─ 0. 基础设施
 │    ├─ rustls 加密 provider
 │    ├─ 刷新 app_config_dir 覆盖
 │    ├─ panic_hook 记录 config 目录
 │    ├─ (Windows) 设置 AppUserModelID
 │    ├─ (桌面) 动态注册 Updater 插件
 │    ├─ 初始化日志（删旧日志 + 单文件 + 轮转）
 │    └─ usage_events::init(注入 AppHandle)
 │
 ├─ 1. 数据库与迁移
 │    ├─ (b) 检测 config.json 是否需要迁移到 SQLite（失败可重试/退出）
 │    ├─ (b′) 【本版本新增】版本预检：DB 版本过新 → set_init_error + 强制显示窗口 + return Ok(())  ← 命中即跳过下方一切
 │    ├─ (c) Database::init()（失败可重试/退出）
 │    └─ (d) 执行 migrate_from_json + 归档旧文件为 .migrated
 │
 ├─ AppState::new(db)  ←── 造出全局状态（见 4.5）
 │
 ├─ 2. 各类数据按表独立导入 / 种子
 │    ├─ 默认 Skills 仓库；Skills SSOT 迁移
 │    ├─ 导入 live 配置 + seed 官方 provider 预设
 │    ├─ Codex 历史 / 模板桶迁移（后台 spawn_blocking）
 │    ├─ OpenCode / OpenClaw / Hermes live providers 导入
 │    ├─ OMO / OMO Slim 导入
 │    ├─ MCP 服务器导入（表空时，从 5 个工具回填）
 │    └─ Prompts 导入（表空时，从 6 个工具回填）
 │
 ├─ 3. 平台集成
 │    ├─ 注册 Deep Link scheme + on_open_url 回调
 │    └─ 创建系统托盘（菜单 + 图标 + 事件）
 │
 ├─ 4. 后台 worker 与托管状态
 │    ├─ 启动 WebDAV / S3 自动同步 worker
 │    ├─ app.manage(app_state)  ←── 状态入池
 │    ├─ 从 DB 加载日志级别
 │    ├─ SkillService / CopilotAuthManager / CodexOAuthManager 入池
 │    └─ 初始化全局出站 HTTP 代理客户端
 │
 ├─ 5. 异步恢复任务（spawn）
 │    ├─ crash 后恢复 Live 配置
 │    ├─ 恢复代理接管状态
 │    ├─ 周期备份定时器（24h）
 │    └─ 会话用量同步（启动一次 + 每 60s）
 │
 └─ 6. 显示窗口
      ├─ (Linux) 禁用 WebKit 硬件加速
      └─ 按 silent_startup 设置：隐藏 or 显示主窗口
```

> **关键观察**：步骤 (b′) 一旦命中「数据库版本过新」，setup 会立即 `return Ok(())`，于是步骤 2～5（含托盘创建、`app.manage`）全部被跳过——这就是为什么 4.2 里窗口关闭处理要为 `db_version_too_new` 单独判断「此时没有托盘」。这条提前返回的细节在 4.4 专讲。

#### 4.3.3 源码精读

**(a) 早期基础设施**：rustls、config 目录、日志。

[src-tauri/src/lib.rs:312-371](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L312-L371) —— setup 开头（`.setup` 在 [lib.rs:311](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L311)）：装 rustls provider、`app_store::refresh_app_config_dir_override`、`panic_hook::init_app_config_dir`、(Windows) `set_windows_app_user_model_id`；随后在 `#[cfg(desktop)]` 块里动态注册 Updater 插件（失败仅 warn 不中断）；再初始化 `tauri_plugin_log`（启动时删除旧 `cc-switch.log` 实现「单文件覆盖」、1GB 轮转）；最后 `usage_events::init(app.handle().clone())` 注入 AppHandle。

> 这里就解释了「为什么日志插件不在链上」：它需要 `log_dir`，而 `log_dir` 来自 `panic_hook::get_log_dir()`，后者依赖刚刚刷新过的 config 目录。

**(b) 旧配置迁移检测与加载验证**：

[src-tauri/src/lib.rs:374-409](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L374-L409) —— 先判断 `config.json`（旧）与 `cc-switch.db`（新）谁存在。若「无 db 有 json」需要迁移，**先**在创建数据库之前验证 `config.json` 能否加载——这样用户若选退出，数据库文件还没被建，下次能干净重试。加载用 `loop` 包裹，失败时弹 `show_migration_error_dialog`，用户选「重试」则继续循环、选「退出」则 `std::process::exit(1)`。

**(b′) 数据库版本预检（本版本新增）**：详见 4.4，此处仅指出位置与「命中即提前返回」的影响。

**(c) 数据库初始化（可重试循环）**：

[src-tauri/src/lib.rs:445-460](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L445-L460) —— `Database::init()` 用 `loop` 包裹，失败时弹 `show_database_init_error_dialog`，用户选「重试」则继续循环、选「退出」则 `std::process::exit(1)`。

**(d) 执行迁移并归档旧文件**：

[src-tauri/src/lib.rs:463-484](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L463-L484) —— `db.migrate_from_json(&config)` 成功后调 `init_status::set_migration_success()`（供前端弹 Toast），并把 `config.json` 重命名为 `config.json.migrated`（**重命名而非删除**，便于用户恢复）。

**(e) 造出全局状态并接管 AppHandle**：

[src-tauri/src/lib.rs:486-489](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L486-L489) —— `let app_state = AppState::new(db);` 然后给 `proxy_service` 注入 `app.handle()`，这样代理做故障转移时能直接更新 UI。

**(f) 按表独立导入 / 种子**（练习要你列的就是这一大段）：

- Skills：[src-tauri/src/lib.rs:495-543](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L495-L543) —— 初始化默认 Skill 仓库；若 `skills_ssot_migration_pending` 标志为真，自动从各应用目录迁移进 SSOT。
- Provider 导入 + 种子：[src-tauri/src/lib.rs:545-600](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L545-L600) —— 遍历非 additive 模式的 `AppType`，把用户手改的 live 配置导成 default provider；随后 `init_default_official_providers()` 追加官方预设。注释点明「先 import 后 seed」是有意为之，配合回填机制保护用户原配置。
- Codex 历史迁移：[src-tauri/src/lib.rs:602-662](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L602-L662) —— 用 `spawn_blocking` 丢到阻塞线程池，避免拖慢启动。
- OpenCode/OpenClaw/Hermes live 导入：[src-tauri/src/lib.rs:679-699](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L679-L699) —— additive 模式导入本身幂等，每次启动都跑也安全。
- MCP 导入（表空触发）：[src-tauri/src/lib.rs:752-795](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L752-L795) —— `is_mcp_table_empty()` 为真时，分别从 Claude/Codex/Gemini/OpenCode/Hermes 回填。
- Prompts 导入（表空触发）：[src-tauri/src/lib.rs:798-820](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L798-L820) —— 遍历 6 个 `AppType`，调 `PromptService::import_from_file_on_first_launch`。

**(g) 平台集成（Deep Link + 托盘）**：

[src-tauri/src/lib.rs:829-891](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L829-L891) —— 注册 `ccswitch://` scheme（Linux/Windows debug 需显式 `register_all`），并 `app.deep_link().on_open_url(...)` 注册回调。

[src-tauri/src/lib.rs:893-939](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L893-L939) —— `TrayIconBuilder::with_id(TRAY_ID)` 构建托盘：tooltip、`on_tray_icon_event`（鼠标进入/点击时后台刷新用量缓存）、菜单、`on_menu_event`，最后 `.build(app)`。macOS 用模板图标适配深浅色，其他平台用默认窗口图标。

**(h) 后台 worker + 状态入池**：

[src-tauri/src/lib.rs:940-949](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L940-L949) —— 启动 `webdav_auto_sync::start_worker` 和 `s3_auto_sync::start_worker`，然后 `app.manage(app_state)` 把全局状态注入 Tauri 状态池（此后命令可用 `tauri::State` 取到）。

[src-tauri/src/lib.rs:964-1021](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L964-L1021) —— 把 `SkillService`、`CopilotAuthManager`、`CodexOAuthManager` 分别 `app.manage(...)`；初始化全局出站 HTTP 代理客户端（失败则清掉无效配置并回退直连）。

**(i) 异步恢复任务**（不阻塞窗口）：

[src-tauri/src/lib.rs:1023-1134](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L1023-L1134) —— `tauri::async_runtime::spawn(async move { ... })`：crash 后恢复 Live 配置、`restore_proxy_state_on_startup`（按 `proxy_config.enabled` 恢复代理接管）、`periodic_backup_if_needed`（启动一次）、24h 周期备份定时器、会话用量同步（启动一次 + 每 60s 一次，覆盖 Claude/Codex/Gemini/OpenCode）。

**(j) 显示窗口**：

[src-tauri/src/lib.rs:1136-1182](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L1136-L1182) —— (Linux) 禁用 WebKitGTK 硬件加速；读 `settings.silent_startup`：为真则 `window.hide()`（静默启动到托盘），否则 `window.show()`。最后 `Ok(())` 收尾，setup 闭包返回成功。

> 这就闭环了上一讲前端的 fail-fast：**只有 setup 整段成功返回 `Ok(())`，窗口才会显示，前端才会被渲染**；若中途致命错误走到 `std::process::exit(1)`，进程直接结束；而 4.4 的「版本过新」是第三种结局——同样 `return Ok(())` 让窗口显示，但前端会因 `get_init_error` 命中 `db_version_too_new` 而渲染恢复界面，而非正常 App。

#### 4.3.4 代码实践

**实践目标**：在 setup 钩子里按顺序找出至少 6 个初始化步骤（本讲义规格要求的实践任务之一）。

操作步骤：

1. 打开 `src-tauri/src/lib.rs`，定位 `.setup(|app| {`（[lib.rs:311](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L311)）。
2. 自上而下阅读，按出现顺序记录以下步骤及其所在行号区间：
   - ① **初始化日志**（删除旧日志、配置轮转）—— [lib.rs:332-365](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L332-L365)；
   - ② **【新增】数据库版本预检**（版本过新则提前返回）—— [lib.rs:419-443](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L419-L443)；
   - ③ **数据库初始化 `Database::init()`（含失败重试循环）** —— [lib.rs:445-460](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L445-L460)；
   - ④ **执行 `migrate_from_json` 并归档旧文件** —— [lib.rs:463-484](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L463-L484)；
   - ⑤ **种子官方 provider 预设 `init_default_official_providers`** —— [lib.rs:594-600](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L594-L600)；
   - ⑥ **MCP 导入（表空时从多工具回填）** —— [lib.rs:752-795](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L752-L795)；
   - ⑦ **Prompts 导入（表空时回填）** —— [lib.rs:798-820](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L798-L820)；
   - ⑧（加分）**创建系统托盘** —— [lib.rs:893-939](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L893-L939)；
   - ⑨（加分）**后台 spawn 的 crash 恢复 + 会话用量同步** —— [lib.rs:1023-1134](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L1023-L1134)。
3. **需要观察的现象**：注意每一步几乎都用 `match` + `log::warn!`/`log::info!` 兜底，**单步失败不会让整个 setup 崩溃**；只有数据库初始化这类「硬依赖」才走 exit 路径；而②预检命中时走的是「提前 `Ok(())`」这第三种路径。
4. 预期结果：你能画出 setup 的时序图，并指出「哪几步失败会直接退出进程、哪几步失败仅记录日志继续、哪一步会提前返回进入恢复界面」。

本实践为源码阅读型，无需运行命令。如果你本地已能 `pnpm dev` 启动，可额外观察 `~/.cc-switch/logs/cc-switch.log` 里这些 `✓` / `○` / `✗` 前缀的日志，它们就是 setup 里逐条打印的（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `config.json` 的迁移验证要放在 `Database::init()` **之前**？

> **答案**：这样如果旧配置加载失败、用户选择退出，数据库文件还没被创建，下次启动仍能干净地重试迁移；若先建库再迁移，失败时库已经存在，下次逻辑会以为「不需要迁移」。

**练习 2**：MCP 和 Prompts 的导入都包了 `is_xxx_table_empty()` 判断。如果删掉这个判断会怎样？

> **答案**：每次启动都会把 live 配置重新导入一遍，可能覆盖用户在数据库里做的修改，或产生重复记录。表空判断是「只在首次/干净状态导入一次」的幂等保护。

**练习 3**：crash 恢复、周期备份、会话用量同步为什么用 `async_runtime::spawn` 而不是直接写在 setup 同步流程里？

> **答案**：它们是耗时/定时任务，若同步执行会阻塞 setup 返回，导致窗口迟迟不显示；放进后台任务既不阻塞窗口，又能异步完成 I/O。

### 4.4 数据库版本预检与恢复早退（本版本新增）

#### 4.4.1 概念说明

本模块专门讲 edeee25f 提交带来的新机制。要理解它，先看一个真实场景：

> 用户用新版 CC Switch（比如支持数据库 schema v11）建过数据库，之后**回退**到旧版应用（只支持到 v9）。旧版应用若照常执行 `Database::init()`，会在读不懂的新版表结构上跑 `DROP/ALTER` 等 DDL，可能把数据库写坏。

旧设计下，这种情况要么表现为「闪退无提示」，要么反复弹出数据库初始化失败的「重试/退出」对话框——但**重试永远不会成功**（版本不匹配是客观事实），用户陷入死循环。

新设计的思路是：**在真正动数据库之前，先只读地探一下版本**，如果发现「磁盘上的 `user_version` 比当前应用支持的 `SCHEMA_VERSION` 还新」，就不要去 `init`、不要写任何 DDL，而是：

1. 把这个错误以结构化的 `InitErrorPayload`（带 `kind="db_version_too_new"`）写进 `init_status`。
2. 强制显示主窗口。
3. `return Ok(())` 提前结束 setup——既不让进程退出（用户还能看到界面），也不再触碰数据库。

随后前端 `bootstrap()` 调 `get_init_error` 拿到 `kind === "db_version_too_new"`，就渲染 `DatabaseUpgrade` 恢复界面：检查是否有新版本可升级 → 有则引导安装更新、重启 → 重启后版本匹配，正常进入 App；若已是最新版仍不兼容，则提示「可能由第三方/更高版本客户端创建」，不再让用户反复尝试。

> **与 4.3 其它失败路径的区别**：数据库初始化失败（DDL 跑挂在可读结构上）走「重试/退出」原生对话框；版本过新是「客观不可重试」，走「应用内升级」恢复界面。后者是**友好且可自助**的。

#### 4.4.2 核心流程

```text
Database::init() 之前
   │
   ▼
stored_user_version_exceeds_supported(db_path)
   │
   ├─ 返回 Ok(None)      → 版本正常或库不存在 → 继续正常 Database::init() 流程
   ├─ 返回 Err(e)        → 预检本身失败（如无法打开） → 仅 warn，继续正常流程（不阻断）
   └─ 返回 Ok(Some(v))   → 版本过新（v > SCHEMA_VERSION）
        │
        ├─ set_init_error(InitErrorPayload {
        │     path, error(中文提示),
        │     kind: "db_version_too_new",
        │     db_version: v,
        │     supported_version: SCHEMA_VERSION,
        │  })
        ├─ window.show() + window.set_focus()   ← 主窗口默认 visible:false，必须强制显示
        └─ return Ok(())                         ← 提前结束 setup（跳过 init/迁移/种子/托盘/manage）

（前端侧）
   get_init_error → kind==="db_version_too_new" → 渲染 DatabaseUpgrade
        │
        ├─ check_app_update_available() → 有新版 → 安装更新并重启
        └─ 无新版 → 提示「已是最新但仍不兼容（可能第三方创建）」
```

预检判定的核心是一行非常简洁的比较：磁盘版本 `v` 必须严格大于应用支持的 `SCHEMA_VERSION`，才算「过新」：

\[ \text{tooNew}(v) \iff v > \text{SCHEMA\_VERSION} \]

当前 `SCHEMA_VERSION = 11`（见 [database/mod.rs:52](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/database/mod.rs#L52)）。`v == SCHEMA_VERSION`（恰好匹配）或 `v < SCHEMA_VERSION`（旧库需要向上迁移）都不算过新，走正常的 `Database::init()`。

#### 4.4.3 源码精读

先看预检的「探针」函数。它**只打开连接、只读 `user_version`、不执行任何 DDL**，所以对数据库零副作用：

[src-tauri/src/database/mod.rs:167-176](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/database/mod.rs#L167-L176) —— `Database::stored_user_version_exceeds_supported(db_path)`：若文件不存在直接 `Ok(None)`；否则 `Connection::open` 后调 `Self::get_user_version(&conn)`（定义在 [schema.rs:2430](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/database/schema.rs#L2430)，本质是 `PRAGMA user_version`），返回 `Ok((version > SCHEMA_VERSION).then_some(version))`。

> 注意它返回的是 `Option<i32>`：`None` 表示「不需要特殊处理」（版本正常 / 库不存在 / 预检内部用 `then_some` 在比较为假时返回 None），`Some(v)` 才表示「确实过新」。这种「只关心异常态」的返回风格，让 setup 侧的 `match` 非常清爽。

再看 setup 侧的调用与早退。这段紧接在「旧配置迁移验证」之后、`Database::init()` 之前：

[src-tauri/src/lib.rs:419-443](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L419-L443) —— `match Database::stored_user_version_exceeds_supported(&db_path)`：
- `Ok(Some(version))` → `log::warn!` 记录、`init_status::set_init_error(InitErrorPayload { path, error, kind: Some("db_version_too_new"), db_version: Some(version), supported_version: Some(SCHEMA_VERSION) })`；接着 `app.get_webview_window("main")` 后 `window.show()` + `window.set_focus()`；最后 **`return Ok(())`**——setup 提前成功返回。
- `Ok(None)` → 版本正常，`{}` 空操作，落入下方正常 `Database::init()`。
- `Err(e)` → 预检自身失败（如权限不足打不开），仅 `log::warn!` 后继续正常流程——**宁可走老路径，也不要因为预检失败而误伤正常启动**。

源码注释明确写出这条纪律：「预检必须先于任何 schema 写操作（`create_tables` 内含 DROP/ALTER 等 DDL），避免旧应用对读不懂的更新版 DB 落写」。

接着看承载这条错误的信箱结构。`InitErrorPayload` 在本版本新增了三个字段：

[src-tauri/src/init_status.rs:5-19](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/init_status.rs#L5-L19) —— `pub struct InitErrorPayload`。除了原有的 `path` / `error`，新增：

| 字段（[init_status.rs:11,14,18](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/init_status.rs#L11-L18)） | 类型 | 在 `db_version_too_new` 场景的用途 |
| --- | --- | --- |
| `kind` | `Option<String>` | 设为 `Some("db_version_too_new")`。前端就是靠它区分「该渲染恢复界面」还是「该弹配置损坏框后退出」。其余错误路径不填（`None`）。 |
| `db_version` | `Option<i32>` | 磁盘上数据库的实际 `user_version`（如 11）。用于在恢复界面里向用户展示「检测到数据库版本 v11」。 |
| `supported_version` | `Option<i32>` | 当前应用支持的 `SCHEMA_VERSION`（如 9）。让前端能比较两者、也能在「升级到最新后 db_version 仍 > supported_version」时判断「可能是第三方客户端创建」。 |

三个新字段都带 `#[serde(skip_serializing_if = "Option::is_none")]`，意味着普通配置损坏场景序列化出去的 JSON 仍只有 `path`+`error`，**前后兼容、不破坏旧前端解析**。

> 顺带一提：本版本同时移除了 `set_init_error` 上原有的 `#[allow(dead_code)]`（见 [init_status.rs:27](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/init_status.rs#L27)）——因为这个函数从「以前没人调用」变成了「预检路径真正在用」。

最后看恢复界面探测更新用的命令（前端 `DatabaseUpgrade` 调它）：

[src-tauri/src/commands/settings.rs:275-285](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/commands/settings.rs#L275-L285) —— `check_app_update_available(app)`：用 `app.updater_builder().build()?.check().await` 查一次更新，返回 `Ok(Some(version))`（有新版）或 `Ok(None)`（已是最新）。前端据此决定展示「立即升级」还是「已是最新仍不兼容」的提示。它已注册进 `invoke_handler`（[lib.rs:1229](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L1229)）。

#### 4.4.4 代码实践

**实践目标**：把「预检 → 写信箱 → 强制显示窗口 → 提前返回」这条新增链路走通，并说清三个新字段的用途（本讲义规格要求的实践任务之一）。

操作步骤：

1. 打开 [database/mod.rs:167-176](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/database/mod.rs#L167-L176)，确认 `stored_user_version_exceeds_supported` **不调用** `init`/`create_tables`/任何写操作，只 `open` + `get_user_version` + 比较。
2. 打开 [lib.rs:419-443](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L419-L443)，确认 `Ok(Some(version))` 分支里依次做了：`set_init_error(...)` → `window.show()` → `window.set_focus()` → `return Ok(())`，且这条 `return` 之后**没有** `Database::init()`、没有 `app.manage`、没有建托盘。
3. 打开 [init_status.rs:5-19](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/init_status.rs#L5-L19)，对照本模块 4.4.3 的三字段表格，用自己的话写出 `kind` / `db_version` / `supported_version` 各自给前端恢复界面提供什么信息。
4. 打开 [commands/settings.rs:275-285](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/commands/settings.rs#L275-L285)，理解恢复界面靠它判断「升级能否解决问题」。
5. **需要观察的现象**：预检命中时，主窗口被显示，但 `~/.cc-switch/logs/cc-switch.log` 里**不会**出现 `Database::init` 之后的 `✓` 系列日志（因为提前返回了）；只会有一条 `数据库版本过新（v{version}）` 的 `warn`。
6. 预期结果：你能向别人讲清「为什么版本过新时数据库不会被写坏，且用户看到的是恢复界面而非闪退」。

如果你本地有不同版本的 CC Switch，可尝试复现：先用高版本启动生成新版 DB，再用低版本启动，观察是否进入 `DatabaseUpgrade` 界面（待本地验证；切勿在生产配置目录上实验，建议用 `app_config_dir` 覆盖指向临时目录）。

#### 4.4.5 小练习与答案

**练习 1**：为什么预检命中后要 `return Ok(())` 而不是 `return Err(...)`？

> **答案**：返回 `Err` 会让 Tauri 认为 setup 失败、应用无法启动（典型表现是进程退出或弹通用错误）。这里的目标恰恰相反——**让应用正常起来**，只是用一个特殊界面替换主界面，所以必须返回 `Ok(())`，把「错误」通过 `init_status` 这条旁路通道交给前端自己决定怎么展示。

**练习 2**：预检函数返回 `Err` 时为什么只 `warn` 后继续正常流程，而不是也进入恢复界面？

> **答案**：`Err` 表示「预检自身出错」（如文件权限打不开），并不能断定「版本过新」。如果据此进入恢复界面，会把无关的 I/O 故障误判成版本问题，反而阻断正常启动。所以设计上「预检失败 = 放弃预检」，退回老的 `Database::init()` 流程（它有自己的重试/退出对话框兜底）。

**练习 3**：三个新字段都加了 `skip_serializing_if = "Option::is_none"`，有什么好处？

> **答案**：保证普通配置损坏场景（`kind/db_version/supported_version` 全为 `None`）序列化出的 JSON 与旧版完全一致（只有 `path`+`error`），不会破坏旧前端或旧版本的前端解析逻辑——这是一次向后兼容的增量扩展。

### 4.5 AppState 全局状态

#### 4.5.1 概念说明

setup 的一大产物是 **`AppState`**——一个被放进 Tauri 状态池、供所有命令共享的「全局应用状态」。它把启动时建好的核心资源（数据库连接、代理服务、用量缓存）打包成一个对象，命令函数通过 `tauri::State<'_, AppState>` 就能拿到**同一个实例**，无需自己再建一遍。

`AppState` 的字段就是 CC Switch 后端的「三大支柱」：数据（db）、代理（proxy_service）、用量（usage_cache）。几乎所有命令的底层都要经由这三者之一。

另一个常被忽略但关键的伙伴是 `init_status.rs`——它不是 `AppState` 的一部分，而是一组**进程级静态变量**，专门存「启动期一次性、需要告知前端」的状态（初始化错误、迁移结果）。前端 `get_init_error` / `get_migration_result` 拉的就是它；4.4 的「版本过新」错误也是经它传递的。

#### 4.5.2 核心流程

```text
Database::init()  ──►  Arc<Database>
                          │
                          ▼
                   AppState::new(db)
                   ├─ db: Arc<Database>            （克隆 Arc，共享同一连接）
                   ├─ proxy_service: ProxyService::new(db.clone())
                   └─ usage_cache: Arc<UsageCache::new()>
                          │
                          ▼
                   app.manage(app_state)   ←── 注入 Tauri 状态池
                          │
        ┌─────────────────┴──────────────────┐
        ▼                                     ▼
  命令 A: tauri::State<AppState>      命令 B: tauri::State<AppState>
  （取到同一个 db / proxy_service）   （取到同一个实例）

   注：若 4.4 预检命中提前返回，则 AppState 永不创建、app.manage 永不执行，
       此时的「全局状态」只有 init_status 里的 InitErrorPayload。
```

#### 4.5.3 源码精读

`AppState` 的定义极简，只有三个字段：

[src-tauri/src/store.rs:5-10](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/store.rs#L5-L10) —— `pub struct AppState { pub db: Arc<Database>, pub proxy_service: ProxyService, pub usage_cache: Arc<UsageCache> }`。

| 字段 | 类型 | 作用 |
| --- | --- | --- |
| `db` | `Arc<Database>` | SQLite 数据库句柄（SSOT）。`Arc` 让多个服务/命令共享同一份，内部用 `Mutex` 保证并发写入安全（详见 U2）。 |
| `proxy_service` | `ProxyService` | 本地代理服务（U7 主角），持有 `db` 克隆，负责启停代理、热切换、故障转移。 |
| `usage_cache` | `Arc<UsageCache>` | 用量统计的内存缓存，避免每次查仪表盘都打数据库。 |

构造函数把数据库 `Arc` 分发给各服务：

[src-tauri/src/store.rs:12-22](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/store.rs#L12-L22) —— `AppState::new(db)`：用 `db.clone()`（仅增 `Arc` 引用计数，不复制连接）构造 `ProxyService`，新建一个空 `UsageCache`。注意 `ProxyService` 没有包 `Arc`，因为它在 `lib.rs` 里还另有 `set_app_handle` 等方法，且整体由 `AppState` 的所有权托管。

回到 setup，状态入池就一行：

[src-tauri/src/lib.rs:949](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/lib.rs#L949) —— `app.manage(app_state);`。此后任何注册在 `invoke_handler` 里的命令，参数里写 `state: tauri::State<'_, AppState>`，Tauri 会自动注入同一个 `AppState`。

> 提醒：`AppState::new` 与 `app.manage` 都位于 4.4 预检的**下方**。若预检命中提前返回，这两步都不会执行——此时前端恢复界面拿不到 `AppState`，只能用 `init_status` 的静态状态 + `check_app_update_available` 这类「不依赖 AppState」的命令工作。

现在看 `init_status.rs`——前端的「信箱」。它用 `OnceLock<RwLock<...>>` 存进程级一次性状态：

[src-tauri/src/init_status.rs:5-19](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/init_status.rs#L5-L19) —— `InitErrorPayload`（本版本新增 `kind`/`db_version`/`supported_version` 三字段，含义见 4.4.3），配合 [init_status.rs:27-36](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/init_status.rs#L27-L36) 的 `set_init_error` / `get_init_error`。注意 `get_init_error` 是**可重复读**（每次返回克隆），用于前端反复查询——恢复界面可能多次重新拉取。

[src-tauri/src/init_status.rs:42-63](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/init_status.rs#L42-L63) —— 迁移成功状态用「取走即消费」语义：`take_migration_success()` 只返回一次 `true`，之后返回 `false`，避免前端重复弹 Toast。Skills 迁移结果（[init_status.rs:69-104](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/init_status.rs#L69-L104)）同理用 `take_skills_migration_result()`。

最后看命令侧如何把这封信递给前端：

[src-tauri/src/commands/misc.rs:81-97](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/commands/misc.rs#L81-L97) —— `get_init_error`（[misc.rs:81-83](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/commands/misc.rs#L81-L83)）直接转发 `init_status::get_init_error()`；`get_migration_result` 转发 `take_migration_success()`；`get_skills_migration_result` 转发 `take_skills_migration_result()`。这正是上一讲前端 `bootstrap()` 调的 `invoke("get_init_error")` 的后端落点——而 4.4 的版本过新错误正是经此传到前端、触发 `DatabaseUpgrade` 的。

> 设计对照：`AppState` 存「运行期长期共享的资源」（db/proxy/cache），走 `app.manage` + `tauri::State`；`init_status` 存「启动期一次性、只读给前端」的信号，走静态全局 + 专门命令。两者各司其职，不要混用。本版本给 `init_status` 增加的 `db_version_too_new` 错误，是「后者」的一次典型扩展。

#### 4.5.4 代码实践

**实践目标**：确认 AppState 的三字段，并追踪一次「前端查 → 后端命令 → 全局状态」的链路（本讲义规格要求「指出 AppState 包含的三个字段」）。

1. 打开 [src-tauri/src/store.rs:5-10](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/store.rs#L5-L10)，列出 `AppState` 的三个字段（db / proxy_service / usage_cache）——这正是规格要求的「指出 AppState 包含的三个字段」。
2. 在 `src-tauri/src/commands/` 下任选一个命令（如 `get_providers` 或 `get_proxy_status`），看它的参数里是否有 `tauri::State<'_, AppState>`，确认它正是通过这个拿到 `db` / `proxy_service`。
3. 打开 [init_status.rs:5-19](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/init_status.rs#L5-L19) 与 [commands/misc.rs:81-83](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/commands/misc.rs#L81-L83)，确认 `get_init_error` 命令就是把 `init_status` 的静态状态透传给前端，并理解三个新字段如何在 `db_version_too_new` 时被消费。
4. **需要观察的现象**：`AppState` 的三个字段都不是「每次命令新建」，而是 setup 期间建好、之后全局复用；而 `init_status` 的错误是预检阶段就写好的，**早于** `AppState` 创建。

预期结果：你能说清「`AppState` 与 `init_status` 的分工」，以及「为什么版本过新时只有 `init_status` 在工作、`AppState` 缺席」。本实践为源码阅读型。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `db` 用 `Arc<Database>` 而不是直接 `Database`？

> **答案**：`ProxyService` 在 `AppState::new` 里也拿了 `db.clone()`，需要多处共享同一个数据库句柄。`Arc` 提供共享所有权的引用计数；真正的并发安全由 `Database` 内部的 `Mutex<Connection>` 保证（U2 详讲）。

**练习 2**：`get_init_error`（可重复读）和 `get_migration_result`（取走即消费）语义不同，为什么要区分？

> **答案**：初始化错误是「持续状态」，前端可能多次查询（如恢复界面刷新、页面重载）；迁移成功提示是「一次性事件」，只应弹一次 Toast，消费后置为 false 避免重复打扰用户。

**练习 3**：如果我新增一个后端服务 `FooService`，想在命令里用 `tauri::State` 取到它，需要做哪两步？

> **答案**：① 在 setup 里构造它并 `app.manage(foo_service)`（或包进 `AppState`）；② 若它要被前端调用，还得在 `invoke_handler` 的 `generate_handler!` 列表里注册相应命令。只 `manage` 不注册命令，前端调不到；只注册命令不 `manage`，命令里 `tauri::State` 取不到。

## 5. 综合实践

把本讲五个模块串起来，完成一个「启动链路追踪」任务：

**任务**：假设有用户报告「CC Switch 启动后白屏，但进程没退出」，请你借助本讲建立的后端地图，定位排查方向。

步骤：

1. **入口层（4.1）**：确认是桌面三平台之一，排除 Linux WebKit 已被 `main.rs` 修正的已知坑。
2. **基础设施层（4.3 a）**：去 `~/.cc-switch/logs/cc-switch.log`（启动时被清空重写）看 setup 最早期日志。若日志文件根本没生成，说明 config 目录/日志初始化阶段就挂了，重点查 `app_config_dir` 覆盖与权限。
3. **数据库预检层（4.4）**：若日志里有一条 `数据库版本过新（v{version}）` 的 `warn`，且其后**没有** `Database::init` 成功的 `✓`，说明命中了恢复早退——此时白屏/卡在恢复界面是预期内的。排查方向是「用户的 DB 是否由更高版本/第三方客户端创建」，并确认前端 `DatabaseUpgrade` 是否正常渲染（这条线索也对应上一讲前端的 `db_version_too_new` 分支）。
4. **数据库层（4.3 c）**：若日志里有 `Failed to init database` 且后续没有 `✓`，但**不是**版本过新，说明数据库初始化失败——此时应有原生对话框弹出（`show_database_init_error_dialog`）。用户没看到对话框却白屏，说明问题在数据库之后。
5. **状态与命令层（4.5）**：若日志显示 setup 已 `Ok(())`、窗口已 `window.show()`，但前端白屏，那问题大概率在**前端**或 **IPC**：检查前端 `bootstrap()` 调 `get_init_error` 时，后端命令是否在 `invoke_handler` 白名单里（4.2），以及 `init_status` 是否被意外写入了错误。
6. **产出**：写一份「白屏排查决策树」，每一步标注对应的源码行号区间与本讲的模块编号，特别要把「版本过新早退」单列为一个独立分支。

这个练习强迫你把「入口 → 装配 → setup 序列 → 版本预检 → 全局状态」五层与真实故障现象对应起来，是检验你是否真正读懂启动链路的试金石。

## 6. 本讲小结

- `main.rs` 极薄：只做平台早期环境修正（Windows 隐藏控制台、Linux 修 WebKit），随后转交 `cc_switch_lib::run()`。
- `run()` 是四段式骨架：`panic_hook` → `Builder` 插件链（single_instance/deep_link/store/window_state 等，含针对 `db_version_too_new` 的窗口关闭特殊处理）→ `setup` 钩子 → `invoke_handler` 命令白名单（本版本新增 `check_app_update_available`）→ `build` + `app.run`。
- setup 钩子是启动最重的一段，遵循「先路径后业务、失败可重试、按表独立导入、重活异步化」纪律；关键步骤包括日志初始化、**数据库版本预检**、数据库初始化与迁移、provider 种子、MCP/Prompts 导入、托盘创建、后台 worker。
- **本版本新增「数据库版本预检与恢复早退」**：`Database::init()` 之前用 `stored_user_version_exceeds_supported` 只读探版本，命中「过新」即写 `InitErrorPayload(kind=db_version_too_new)`、强制显示窗口、`return Ok(())` 提前结束——既不写坏数据库，也让前端渲染应用内升级恢复界面。
- `app.manage(app_state)` 把 `AppState`（db / proxy_service / usage_cache）注入状态池，命令经 `tauri::State` 共享同一实例；预检命中时此步被跳过。
- `init_status.rs` 用静态全局存「启动期一次性信号」，本版本给 `InitErrorPayload` 新增 `kind`/`db_version`/`supported_version` 三字段（向后兼容），由 `get_init_error` 透传给前端，与 `AppState` 的运行期共享资源分工明确。

## 7. 下一步学习建议

本讲建立了后端启动主线，接下来建议：

1. **进入 U2「数据存储与 SSOT 机制」**：本讲反复出现的 `Database::init()`、`Arc<Database>`、各表导入、`SCHEMA_VERSION` 与 `get_user_version`，其内部实现就在 U2——你会看到 SQLite 表结构、`Mutex<Connection>` 并发安全、DAO 分层与从 `config.json` 到数据库的迁移细节（`src-tauri/src/database/`）。
2. **想先看启动产物的去向**：可跳读 `src-tauri/src/commands/mod.rs` 与 `src-tauri/src/commands/misc.rs`，确认 `get_init_error` / `get_providers` 等命令如何取用 `AppState`；以及 `commands/settings.rs` 里的 `check_app_update_available` 如何被恢复界面复用。
3. **想看托盘与 Deep Link 的实现**：它们在本讲只点到「注册」，完整逻辑分别在 `src-tauri/src/tray.rs` 和 `src-tauri/src/deeplink/`，对应 U9 平台集成单元。
4. **想看「数据库版本过新」的前端侧**：回到上一讲（u1-l4）阅读 `DatabaseUpgrade.tsx` 的 `phase` 状态机，把它与本讲 4.4 的后端预检一一对应。

建议下一讲直接学习 **u2-l1 SQLite 数据库与表结构**，把本讲「数据库初始化与版本预检」这一步彻底拆开。
