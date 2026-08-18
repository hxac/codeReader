# main() 入口：多模式与启动前置

## 1. 本讲目标

本讲聚焦 `src/main.rs` 中 `main()` 函数的**前半部分**——从进程入口到 `app.run()` 之前的那一段"启动前置"代码。读完本讲，你应该能够：

1. 理解 `zed` 这一个二进制如何根据命令行参数，分别以 askpass 助手、崩溃处理服务器、环境打印、action 清单导出、系统规格打印、正常编辑器等多种"人格"运行。
2. 掌握 `init_paths()` 会创建哪些目录、失败时错误如何被聚类、以及 `files_not_created_on_launch` 如何用一个最小 GPUI 窗口向用户兜底。
3. 理解 zlog 日志初始化的分流逻辑：stdout 与日志文件两条路径，以及 `ZED_FORCE_CLI_MODE` 的作用。
4. 理解单实例检查在 `main()` 中的位置、Dev 通道为何跳过它、以及三大平台的实现差异。

承接上一讲（u1-l3）：我们已经知道 `main.rs` 是 crate 根、只声明 `zed` 与 `reliability` 两个模块。本讲正式走进 `main()` 函数体，看这个"启动器"在把控制权交给 `zed::init` 与 `app.run` 之前都做了什么。

## 2. 前置知识

- **clap 与 derive 宏**：Rust 最流行的命令行解析库。给结构体标上 `#[derive(Parser)]`，clap 就能自动把 `argv` 解析成结构体字段——字段名对应参数名，文档注释变成 `--help` 文本。
- **多模式二进制**：类似 busybox 的思路——分发一个可执行文件，根据 argv[1] 或某个标志切换成完全不同的程序。Zed 用同一个 `zed` 二进制同时充当编辑器、askpass 助手、崩溃处理器等，省去额外的小可执行文件。
- **pty 与 `is_terminal()`**：pty（伪终端）指命令行交互环境。`std::io::IsTerminal` 可以判断 stdout 是否连着终端——这决定了日志该往屏幕写还是往文件写。
- **XDG Base Directory 规范**：Linux 桌面应用的目录约定：配置放 `~/.config/<app>`，数据放 `~/.local/share/<app>`，缓存放 `~/.cache/<app>`。Zed 的 `paths` crate 按此规范（加上 macOS/Windows 各自约定）解析目录。
- **`OnceLock` / `LazyLock`**：标准库的延迟初始化容器——第一次访问时执行初始化，之后所有访问拿到同一个值。`paths` crate 用它把"目录路径解析"做成进程级单例。
- **单实例（single instance）**：桌面应用的常见约束——同一时间只允许一个主进程，后来者把"打开请求"转发给先来的进程。三平台手段各不相同：Linux 用 unix socket 抢占、macOS 查进程列表、Windows 用命名对象。
- **`io::ErrorKind`**：`std::io::Error` 的分类枚举（如 `PermissionDenied`、`NotFound`）。本讲会看到它被当作 HashMap 的键，用来给失败目录聚类。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `src/main.rs` | 本讲主战场。`main()` 前半（约 L200–L420）是启动前置阶段；`init_paths`、`files_not_created_on_launch`、`Args` 结构体、`dump_all_gpui_actions` 等辅助函数分布在文件后半 |
| `src/zed/open_listener.rs` | 单实例检查的平台实现之一：Linux/FreeBSD 的 unix datagram socket 监听（`listen_for_cli_connections`） |
| `src/zed/mac_only_instance.rs`、`src/zed/windows_only_instance.rs` | macOS / Windows 的单实例实现，本讲只提及其调用位置，深入留到 u3-l5 |

涉及但不在本讲展开的依赖 crate（按名认识即可）：`paths`（目录解析）、`sandbox`（Linux 沙箱启动器）、`askpass`（凭证询问助手）、`crashes`（崩溃处理）、`zlog`（日志框架）、`util`（root 防护等杂项工具）、`release_channel`（发布通道，u1-l1 已讲）。

## 4. 核心概念与源码讲解

### 4.1 CLI 参数与特殊模式

#### 4.1.1 概念说明

`zed` 不是"只有一个用途"的二进制。同一份代码，根据命令行参数可以进入以下互斥的运行模式之一：

| 模式 | 触发方式 | 做什么 | 返回点 |
| --- | --- | --- | --- |
| Linux 沙箱启动器 | 由 bwrap/WSL 以特定方式再执行 | 校验 bind 与受限网络桥接，不返回 | 在参数解析**之前**分流 |
| askpass 助手 | `zed --askpass <socket>`（非 Windows，隐藏参数） | 充当 nc/netcat 替身：从 stdin 读 prompt，通过 Unix socket 转发给主进程 | L218 `return` |
| 崩溃处理服务器 | `zed --crash-handler <socket>`（隐藏参数） | 以独立进程通过 socket 通信，录制 minidump | L224 `return` |
| ETW 追踪器 | `zed --record-etw-trace ...`（仅 Windows，隐藏参数） | 录制 ETW 跟踪，需要管理员权限 | L248 `return` |
| 环境打印 | `zed --printenv`（隐藏参数） | 把全部环境变量以 JSON 打到 stdout | L263 `return` |
| action 清单导出 | `zed --dump-all-actions`（隐藏参数） | 导出所有已注册 GPUI action 的 JSON 清单 | L268 `return` |
| 系统规格打印 | `zed --system-specs` | 打印版本、OS、架构等系统规格 | L320 `return` |
| 正常编辑器 | （默认） | 走完启动主链路，进入 `app.run` | 不返回 |

标注 `hide = true` 的参数不会出现在 `--help` 里——它们是 Zed 主进程与 zed CLI/内部组件之间的**私有协议**，不是给用户手敲的。

#### 4.1.2 核心流程

`main()` 前半的执行顺序（每一步都可能提前终止进程）：

```text
记录 STARTUP_TIME
    │
    ▼
sandbox::run_sandbox_launcher_if_invoked()   ← 必须在参数解析之前！
    │
    ▼
(unix) util::prevent_root_execution()        ← root 直接拒绝
    │
    ▼
Args::parse()                                ← clap 解析参数
    │
    ▼
--askpass?      ── 是 ──▶ askpass::main(socket); return
    │ 否
    ▼
--crash-handler? ── 是 ──▶ crashes::crash_server(...); return
    │ 否
    ▼
(Windows) --record-etw-trace? ── 是 ──▶ 录制 ETW; return
    │ 否
    ▼
--printenv?     ── 是 ──▶ util::shell_env::print_env(); return
--dump-all-actions? ── 是 ──▶ dump_all_gpui_actions(); return
    │ 否
    ▼
--user-data-dir? ── 是 ──▶ paths::set_custom_data_dir(dir)
    │
    ▼
init_paths() ── 有失败 ──▶ files_not_created_on_launch(errors); return
    │ 全部成功
    ▼
zlog::init() + stdout/文件分流 + ztracing::init()
    │
    ▼
计算 AppVersion / AppCommitSha
    │
    ▼
--system-specs? ── 是 ──▶ 打印系统规格; return
    │ 否
    ▼
初始化 rayon 线程池、记录启动日志
    │
    ▼
build_application()、数据库、身份 ID、OpenListener
    │
    ▼
单实例检查 ── 失败 ──▶ println!("zed is already running"); return
    │ 通过
    ▼
崩溃处理器、文件系统、配置监听……（进入 app.run 的前置装配）
```

两个顺序要点：

1. **沙箱启动器在 `Args::parse()` 之前**。源码注释解释了原因：被沙箱包裹的命令的参数会"原样追加"在 zed 自己的参数之后，若先做 clap 解析，这些参数会被误认成 zed 的参数。
2. **`--system-specs` 在 `zlog::init()` 之后**、而 `--printenv` 在其之前。因为打印系统规格需要先构造 `AppVersion`（依赖编译期注入的 `ZED_COMMIT_SHA` 等环境变量），并且希望诊断信息本身能进日志；而 printenv 要求"环境原样输出"，越早执行越干净。

#### 4.1.3 源码精读

先看函数开头的四道闸门——计时、沙箱、root 防护、参数解析：

[src/main.rs:L200-L212](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L200-L212) — `main()` 开始：`STARTUP_TIME` 用 `OnceLock` 记录启动时刻（供后续测量启动耗时）；随后调用沙箱启动器（若本进程是被 bwrap/WSL 以启动器身份再执行的，函数内部直接运行对应逻辑且**不返回**）；unix 平台上 `prevent_root_execution` 检查 euid 是否为 root，是则打印错误并以码 1 退出（`ZED_ALLOW_ROOT=true` 可豁免——在容器里以 root 调试 Zed 时会用到）；最后 `Args::parse()` 完成 clap 解析。

接着是两个"隐藏模式"：

[src/main.rs:L214-L225](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L214-L225) — `--askpass <socket>`：非 Windows 下，把 zed 切换成 askpass 助手——从 stdin 读到底，然后把内容写往指定 Unix socket（相当于用自身替代 netcat 依赖），随后 `return`。`--crash-handler <socket>`：切换成 minidump 崩溃处理服务器，日志目录传 `paths::logs_dir()`，同样 `return`。两者都不需要数据目录，因此排在 `init_paths()` 之前。

[src/main.rs:L227-L249](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L227-L249) — 仅 Windows：`--record-etw-trace` 模式，要求同时给出 `--etw-output` 与 `--etw-socket`，缺一则报错退出；随后调用 `etw_tracing::record_etw_trace` 录制跟踪并 `return`。这是 Windows 上做堆分析的诊断通道。

[src/main.rs:L260-L269](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L260-L269) — `--printenv` 与 `--dump-all-actions`：前者调用 `util::shell_env::print_env()`，把 `std::env::vars()` 收进 `HashMap` 后以 pretty JSON 打到 stdout（见 `crates/util/src/shell_env.rs` 的 `print_env`）；后者调用本文件的 `dump_all_gpui_actions()`。两者执行后立即 `return`，全程不触碰数据目录。

[src/main.rs:L271-L274](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L271-L274) — `--user-data-dir <DIR>`：设置自定义数据目录。注意这行在 `init_paths()` **之前**——它改变了 `paths` crate 中 `CUSTOM_DATA_DIR` 的取值，从而让后面创建的所有目录（config、extensions、database……）都落到新位置。

`Args` 结构体是全部 CLI 契约的单一事实来源：

[src/main.rs:L1686-L1793](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1686-L1793) — clap derive 定义。值得注意的点：
- `paths_or_urls: Vec<String>` 是位置参数（不需要 `--` 前缀），支持 `path:line:row` 定位语法，也接受 `file://`、`zed://` 等 URL——u3-l1 会深入它的解析。
- `--diff` 用 `ArgAction::Append` + `num_args = 2`，可多次给出文件对。
- `--system-specs` 的文档注释说明了存在意义：当 Zed 崩到起不来、无法用界面里的"复制系统规格"时，还能从命令行拿到规格附进 issue。
- 带 `#[cfg(target_os = "windows")]` / `#[cfg(not(target_os = "windows"))]` 的字段是平台专属参数（wsl、foreground、dock-action、askpass、ETW 组）。
- 大量 `hide = true`：askpass、crash-handler、printenv、dump-all-actions、ETW 组、foreground、dock-action 都是内部协议参数。

`--dump-all-actions` 的实现：

[src/main.rs:L1963-L2004](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1963-L2004) — `dump_all_gpui_actions()`：遍历 `gpui::generate_list_of_all_registered_actions()`，为每个 action 构造 `ActionDef`（name、命令面板里的人类可读名、JSON schema、废弃别名、废弃信息、文档），按 name 排序后连同 schema 定义一起，以 `{ "actions": [...], "schema_definitions": ... }` 的 pretty JSON 写到 stdout。这个清单正是 Zed 文档站 action 列表的数据来源之一。

#### 4.1.4 代码实践

**实践：体验三个"即打印即退出"的特殊模式**

1. **实践目标**：验证 zed 二进制的多模式行为，确认这三个模式不创建窗口、不依赖数据目录即完成输出。
2. **操作步骤**（需要本地已构建或已安装 zed；仓库根 `cargo run --` 形式亦可，如 `cargo run -p zed -- --printenv`）：
   - `zed --printenv | head -30`
   - `zed --system-specs`
   - `zed --dump-all-actions | head -60`（机器上若有 jq，可 `zed --dump-all-actions | jq '.actions | length'` 数一数 action 总数）
3. **需要观察的现象**：
   - `--printenv` 输出一段 pretty JSON，键为环境变量名；
   - `--system-specs` 以 `Zed System Specs (from CLI):` 开头，随后是多行规格文本；
   - `--dump-all-actions` 输出含 `actions` 与 `schema_definitions` 两个顶层键的 JSON，`actions` 数组已按字母序排列；
   - 三条命令都只打印、不开窗口、立即退出。
4. **预期结果**：输出形态与上面一致。其中 system-specs 的具体字段集合与构建通道（dev/stable/preview）相关。
5. 以上命令本讲义编写环境未实际执行，**待本地验证**。

另一个零成本的替代实践（不需要构建）：给 `Args` 结构体的某个字段（如 `system_specs`）临时改一句文档注释，重新 `cargo run -p zed -- --help`，观察帮助文本随之变化，验证"文档注释即 `--help` 文案"。看完记得还原，不要提交。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `sandbox::run_sandbox_launcher_if_invoked()` 必须放在 `Args::parse()` 之前调用？

**答案**：源码注释（L203-L206）写明：被沙箱包裹的命令的参数会**原样追加**在 zed 的参数之后。若先做 clap 解析，这些属于被包裹命令的参数会被误解释为 zed 自己的参数，导致解析失败或行为错乱。所以必须先检测"本进程是不是启动器身份"，是则进入启动器模式且不再返回。

**练习 2**：`--askpass` 模式下的 zed 扮演什么角色？为什么 Zed 要内置这个模式？

**答案**：它扮演 nc/netcat 的替身：从 stdin 读取全部内容，连接指定 Unix socket 并把内容写过去（见 `crates/askpass/src/askpass.rs` 的 `main`）。SSH/Git 需要密码认证时会设置 `SSH_ASKPASS` 指向这个入口。内置它是为了去掉对系统 netcat 的外部依赖。

**练习 3**：`--printenv` 与 `--system-specs` 都是"打印诊断信息后退出"，为什么后者排在 `init_paths()` 和 `zlog::init()` 之后？

**答案**：`--system-specs` 需要先构造 `AppVersion`（L306-L309，依赖编译期注入的 `ZED_BUILD_ID`/`ZED_COMMIT_SHA`），并且其输出内容（版本、通道）要进入 `SystemSpecs::new_stateless`；放在日志初始化之后也意味着这次诊断本身可被日志记录。而 `--printenv` 追求的是尽早原样输出环境，且完全不依赖任何 Zed 基础设施，所以放在最前面。

### 4.2 init_paths 与目录创建

#### 4.2.1 概念说明

Zed 启动时假设一组目录"已经可用"：设置文件要写进 config 目录、扩展与语言服务器会下载到数据目录、数据库与日志各有归属。`init_paths()` 的职责是在一切开始之前用 `std::fs::create_dir_all` 把这 8 个目录创建到位。

它的返回值设计很讲究：不是 `Result`，而是 `HashMap<io::ErrorKind, Vec<&Path>>`——**按错误类别聚类**的失败清单。比如 3 个目录都因权限被拒、2 个目录因磁盘满失败，用户会看到两条按类别归并的提示，而不是 5 条零散报错。而处理这个清单的 `files_not_created_on_launch` 兜底函数，则展示了"启动失败也要给用户一个图形提示"的完整链条。

#### 4.2.2 核心流程

```text
init_paths():
    待创建目录 = [config, extensions, languages, debug_adapters,
                  database, logs, temp, hang_traces]
    errors = {}
    for path in 待创建目录:
        create_dir_all(path) 失败 → errors[错误类别].push(path)
    return errors

main() 中:
    errors 非空 → files_not_created_on_launch(errors) → return
    errors 为空 → 继续启动
```

8 个目录在 `paths` crate 中的定义与 Linux 下的默认落点（`data_dir()` = `~/.local/share/zed`，受 Flatpak 变量与 `--user-data-dir` 影响）：

| `paths` 函数 | 默认位置（Linux） | 用途 |
| --- | --- | --- |
| `config_dir()` | `~/.config/zed` | settings.json、keymap.json |
| `extensions_dir()` | `~/.local/share/zed/extensions` | 已安装扩展 |
| `languages_dir()` | `~/.local/share/zed/languages` | 内置语言的语言服务器下载 |
| `debug_adapters_dir()` | `~/.local/share/zed/debug_adapters` | 内置调试适配器下载 |
| `database_dir()` | `~/.local/share/zed/db` | 本地键值数据库 |
| `logs_dir()` | `~/.local/share/zed/logs` | 日志目录 |
| `temp_dir()` | `~/.cache/zed` | 临时文件（macOS/Windows 用各自 cache 目录） |
| `hang_traces_dir()` | `~/.local/share/zed/hang_traces` | 卡顿痕迹（u6-l2 深入） |

#### 4.2.3 源码精读

`main()` 中的调用点：

[src/main.rs:L287-L291](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L287-L291) — 调用 `init_paths()`，只要返回的 `file_errors` 非空，就把错误清单交给 `files_not_created_on_launch` 并直接 `return`：目录建不起来，编辑器根本不该继续启动。

`init_paths` 本体：

[src/main.rs:L1656-L1674](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1656-L1674) — 把 8 个 `paths` 函数返回的 `&'static Path` 放进数组，用 `fold` 逐个 `create_dir_all`；失败时以 `e.kind()`（`io::ErrorKind`）为键、失败路径列表为值累积。返回 `&'static Path` 是可行的，因为 `paths` crate 用 `OnceLock` 把路径做成了进程级静态值。

兜底函数：

[src/main.rs:L95-L150](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L95-L150) — `files_not_created_on_launch`：先把 `HashMap` 展平成人类可读的错误详情——单个路径时逐个列出，多个路径合并展示；unix 上若错误类别是 `PermissionDenied`，还会追加一段建议：用 `chown`/`chmod` 修正目录权限（附具体命令示例）。随后 `eprintln` 一份，再**另起一个最小 GPUI 应用**（`build_application()` + `with_quit_mode(QuitMode::Explicit)`）开一个空窗口，弹出 `PromptLevel::Critical` 的确认框，用户点掉唯一的 "Exit" 按钮后进程退出。也就是说：即便主启动失败，也尽力用 GUI 告知用户原因，而不是无声崩溃。

若连窗口都开不出来，还有最后一层：

[src/main.rs:L156-L197](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L156-L197) — `fail_to_open_window`：非 Linux/FreeBSD 平台直接 `eprintln` 后 `process::exit(1)`；Linux/FreeBSD 上则尝试通过桌面门户（ashpd 的 `NotificationProxy`）发一条高优先级系统通知（id 为 `dev.zed.Oops`，指向 zed.dev/docs/linux 排错文档），发完再退出。这是"图形界面不可用时退化为桌面通知"的分层降级。

#### 4.2.4 代码实践

**实践：亲手触发一次目录创建失败，观察兜底链**

1. **实践目标**：理解 `init_paths` 的失败路径与 `files_not_created_on_launch` 的兜底行为。
2. **操作步骤**（Linux 示例，macOS 思路相同）：
   - `mkdir -p /tmp/zed-readonly && chmod 500 /tmp/zed-readonly`
   - 以普通用户运行 `zed --user-data-dir /tmp/zed-readonly/zed-data`（提示：`--user-data-dir` 在 L271-L274 被 `set_custom_data_dir` 记录，config/数据/日志等目录都会重定向到该前缀下）；
   - 若无 GUI 环境，可改在另一个终端先 `chmod 500 ~/.config` 前备份权限，运行后恢复（`chmod 700 ~/.config`）——注意不要对 HOME 下其他目录做破坏性操作。
3. **需要观察的现象**：stderr 出现 `Zed failed to launch: ...`；错误详情里出现 `PermissionDenied when creating directory ...` 且附带 `chown`/`chmod` 建议；随后弹出一个只有 "Exit" 按钮的严重级别提示窗（GUI 可用时）。
4. **预期结果**：`init_paths` 返回的多个失败被按 `PermissionDenied` 归并为一条类别级提示；点击 Exit 后进程以非零状态退出；`~/.local/share/zed` 等真实数据目录不受影响。
5. 本讲义编写环境未实际执行，**待本地验证**。若在容器/CI 中以 root 运行，注意会先被 `prevent_root_execution` 拦截，需设 `ZED_ALLOW_ROOT=true` 才能继续。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `init_paths` 返回 `HashMap<io::ErrorKind, Vec<&Path>>` 而不是 `anyhow::Result`？

**答案**：因为要向用户呈现的是"按错误类别归并"的聚合信息（几类错误、每类涉及哪些目录），`Result` 只能携带第一个失败，会丢失后续目录的失败信息。聚类结构也让 `files_not_created_on_launch` 能对 `PermissionDenied` 单独附加权限修复建议。

**练习 2**：`--user-data-dir` 为什么必须生效于 `init_paths()` 之前？

**答案**：它通过 `paths::set_custom_data_dir` 修改 `paths` crate 的 `CUSTOM_DATA_DIR`，而 `config_dir()`/`data_dir()` 等都以它为最高优先级来源。`init_paths` 创建的正是这些函数返回的路径，所以覆盖动作必须在创建动作之前完成，否则创建的仍是默认位置。

**练习 3**：`files_not_created_on_launch` 为什么要新起一个 `Application` 而不是复用主启动流程？

**答案**：此刻主流程尚未 `app.run`，主应用对象还不存在；而错误提示需要一个窗口来渲染。于是这个函数自建最小 `Application`、开一个空窗口、弹 Critical 提示、等待用户确认后退出——用最小的代价保证"启动失败也有图形反馈"。若开窗本身失败，再降级到 `fail_to_open_window` 的 eprintln/桌面通知路径。

### 4.3 zlog 日志初始化

#### 4.3.1 概念说明

`zlog` 是 Zed 自己的日志外观（实现 `log::Log` trait），支持多输出目标与按 crate 过滤。`main()` 在 `init_paths()` 之后立即初始化它，并做一个关键分流：

- stdout 是终端（用户在命令行里直接跑 `zed`）→ 日志打到 stdout，方便实时看；
- stdout 不是终端（从桌面图标、Dock、systemd 启动）→ 日志写进 `Zed.log` 文件，上一份被轮转为 `Zed.log.old`。

分流判据是 `stdout_is_a_pty()`，它同时叠加了 `ZED_FORCE_CLI_MODE` 环境变量的否决权——zed CLI 在拉起主进程时会设置该变量，强制日志走文件，避免把日志混进 CLI 自己的 stdout。

#### 4.3.2 核心流程

```text
zlog::init()                    ← 安装全局 logger（log::set_logger），设置最高日志级别
    │
    ▼
stdout_is_a_pty()?
    │ 是                                    │ 否
    ▼                                       ▼
zlog::init_output_stdout()        zlog::init_output_file(log_file(), old_log_file())
    │                                       │ 失败（打不开日志文件）
    │                                       ▼
    │                          eprintln 提示后退化 init_output_stdout()
    ▼
ztracing::init()                 ← 初始化 tracing 桥接
```

#### 4.3.3 源码精读

[src/main.rs:L293-L304](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L293-L304) — `zlog::init()` 安装 logger（其内部调用 `log::set_logger(&ZLOG)` 并把级别开到最大，见 `crates/zlog/src/zlog.rs`）；随后按 `stdout_is_a_pty()` 分流：终端走 stdout，否则 `init_output_file` 打开 `paths::log_file()`（`logs_dir/Zed.log`）并把 `old_log_file()`（`Zed.log.old`）作为轮转目标；文件打不开时 eprintln 一条提示并退回 stdout——日志初始化本身也不允许把进程搞挂。最后 `ztracing::init()` 桥接 tracing 生态。

判据函数与 CLI 否决权：

[src/main.rs:L1682-L1684](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1682-L1684) — `stdout_is_a_pty()` = "没有 `ZED_FORCE_CLI_MODE` 环境变量" 且 "stdout 是终端"。两个条件缺一不可。

[src/main.rs:L1676-L1680](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1676-L1680) — `FORCE_CLI_MODE` 是 `LazyLock` 静态：首次读取时检查环境变量 `ZED_FORCE_CLI_MODE`（常量定义在 `crates/cli/src/cli.rs`），随后**立即把它从进程环境中移除**——这样 Zed 主进程后续派生的子进程（语言服务器等）不会继承这个只属于父子协议的变量。读取一次、全局缓存。

顺带一提，`stdout_is_a_pty()` 在后面还会被第二次使用（L443）：非终端环境下，Zed 会后台执行一次登录 shell 环境加载（`load_login_shell_environment`），保证从桌面启动时也能拿到用户 shell 的 PATH 等变量。同一个判据驱动两处行为，是"启动环境探测"的复用。

#### 4.3.4 代码实践

**实践：验证日志的双通道分流**

1. **实践目标**：亲眼确认"终端启动→stdout 日志、非终端启动→文件日志"的分流行。
2. **操作步骤**：
   - 在终端直接运行 `cargo run -p zed`（或已安装的 `zed`），观察终端里滚动的 `========== starting zed version ... ==========` 日志行；
   - 重定向再跑一次：`cargo run -p zed > /tmp/zed-out.log 2>&1 &`，随后 `head -5 ~/.local/share/zed/logs/Zed.log`（macOS 为 `~/Library/Logs/Zed/Zed.log`）；
   - 变体：`ZED_FORCE_CLI_MODE= cargo run -p zed` 在终端里运行，观察日志是否改走文件。
3. **需要观察的现象**：第一次日志出现在终端；第二次 `~/.local/share/zed/logs/Zed.log` 更新且开头是版本横幅，`/tmp/zed-out.log` 里基本没有日志；变体实验里即便 stdout 是终端，日志也进了文件。
4. **预期结果**：与 `stdout_is_a_pty()` 的布尔逻辑完全对应（`!FORCE_CLI_MODE && is_terminal()`）。
5. **待本地验证**。另外可对照观察 `logs` 目录里是否存在 `Zed.log.old`，验证轮转文件的存在。

#### 4.3.5 小练习与答案

**练习 1**：zed CLI 启动 Zed 时设置 `ZED_FORCE_CLI_MODE`，为什么 `FORCE_CLI_MODE` 读取后要把它从环境中删掉？

**答案**：这个变量是 CLI 与主进程之间的单次信号，只该影响主进程自己的日志分流决策。Zed 主进程会派生大量子进程（语言服务器、扩展进程等），若不删除，子进程会错误继承该变量，可能影响它们自身基于环境的判断。读取一次后 `remove_var`，把影响面限制在当前进程。

**练习 2**：日志文件初始化失败时进程会退出吗？

**答案**：不会。L298-L302 对 `init_output_file` 的失败只是 `eprintln` 一条 "Could not open log file ... Defaulting to stdout"，然后退回 `init_output_stdout()`。日志是可降级的附属能力，不应阻断编辑器启动——这与 `init_paths` 失败即退出的"硬依赖"形成对比。

### 4.4 单实例检查的位置与平台差异

#### 4.4.1 概念说明

桌面编辑器通常要求"全机一个主实例"：再次启动时不应开出第二个孤立进程，而应把打开请求转给已有实例。`main()` 在完成日志初始化、版本计算、rayon 线程池、`Application` 构造之后（L359-L383）执行单实例检查——位置考究：要晚于基础设施就绪（socket 路径依赖 `paths`，日志要能记录），又要早于任何窗口创建与崩溃处理器安装。

两个跳过条件：`ZED_STATELESS` 环境变量（无状态运行，供特殊部署/测试），或当前是 **Dev 通道**——这正是 u1-l1 讲过的结论"Dev 通道跳过单实例检查"在源码中的落点。开发者本机可以同时跑多个从源码构建的 zed 而互不干扰，原因就在这里。

三平台手段：

| 平台 | 机制 | 失败含义 |
| --- | --- | --- |
| Linux/FreeBSD | 抢占 unix datagram socket `data_dir/zed-<channel>.sock` | 绑定失败 = 已有实例 |
| macOS | `ensure_only_instance()` 遍历进程列表 | 非 `Yes` = 已有实例 |
| Windows | `handle_single_instance(open_listener, &args)` | 返回 false = 已有实例 |

#### 4.4.2 核心流程

```text
ZED_STATELESS 或 ReleaseChannel::Dev?
    │ 是 → failed = false（跳过检查）
    │ 否
    ▼
按平台三选一：
    Linux/FreeBSD: listen_for_cli_connections(listener).is_err()
    Windows:       !windows_only_instance::handle_single_instance(listener, &args)
    macOS:         ensure_only_instance() != IsOnlyInstance::Yes
    │
    ▼
failed?
    │ 是 → println!("zed is already running"); return
    │ 否 → 继续安装崩溃处理器、装配 fs/配置监听……
```

Linux 路径的巧妙处：`listen_for_cli_connections` 绑定的 socket **本身就是**后续 CLI 转发打开请求的通道。抢占成功 = 你是主实例 + 你拿到了收信信箱；绑定失败 = 已有实例占着信箱，你就该把请求发给它然后退出。

#### 4.4.3 源码精读

[src/main.rs:L359-L383](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L359-L383) — `main()` 中的单实例检查块：`if *zed_env_vars::ZED_STATELESS || *release_channel::RELEASE_CHANNEL == ReleaseChannel::Dev` 直接视为通过；否则按平台分支取相反语义的布尔值。任一平台判定"已有实例"，就打印一行 `zed is already running` 并 `return`（注意是温和退出，不是报错）。

[src/zed/open_listener.rs:L409-L432](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L409-L432) — Linux/FreeBSD 实现 `listen_for_cli_connections`：socket 路径为 `data_dir().join(format!("zed-{}.sock", RELEASE_CHANNEL_NAME))`——**通道名编进文件名**，所以同一机器上 dev/stable/preview 三个通道各有各的 socket、互不冲突（对应 u1-l1 的"四通道共存"设计）。绑定前先探测：若 connect 该路径返回 `ConnectionRefused`，说明上次的主进程已死但 socket 文件残留，于是先删文件再绑定。绑定成功后开一个后台线程循环 `recv`，把收到的字节串包装成只含一个 URL 的 `RawOpenRequest` 塞进 `opener`（即 `OpenListener`）。macOS 与 Windows 的实现分别在 `src/zed/mac_only_instance.rs` 与 `src/zed/windows_only_instance.rs`，细节留给 u3-l5。

#### 4.4.4 代码实践

**实践：找到并"戳一下"单实例 socket**

1. **实践目标**：把"单实例 = 抢占 socket"从源码结论变成可观察的事实。
2. **操作步骤**（Linux）：
   - 注意前提：从源码 `cargo run` 的 debug 构建是 Dev 通道，**会跳过检查**。要观察 socket，需要用官方安装的 zed，或在 debug 构建时设 `ZED_RELEASE_CHANNEL=stable cargo run -p zed`（u1-l1 讲过的环境变量覆盖，仅 debug 构建生效）；
   - 启动后 `ls -la ~/.local/share/zed/`，应看到 `zed-stable.sock`（名字随通道变化）；
   - 安装 socat 后发送一个打开请求：`echo -n 'file:///etc/hostname' | socat - UNIX-SENDTO:$HOME/.local/share/zed/zed-stable.sock`，观察已有 Zed 实例是否打开了对应文件；
   - 保持实例运行，再启动一次同通道 zed，观察终端输出 `zed is already running`。
3. **需要观察的现象**：socket 文件存在且随通道名变化；datagram 发送后已有实例打开目标文件；第二实例温和退出。
4. **预期结果**：与 L410-L432 的源码行为一致——socket 上收到的字符串被当成 URL 塞进 `OpenListener`。
5. **待本地验证**（需要 GUI 环境与 socat；若环境不支持，可退化为只做 socket 文件的观察）。

#### 4.4.5 小练习与答案

**练习 1**：为什么单实例检查放在 `zlog::init()` 与 `Application` 构造之后、而不是 `main()` 最前面？

**答案**：一方面它依赖基础设施：socket 路径来自 `paths::data_dir()`（需要目录已就位），检查过程也希望有日志可查；另一方面它必须早于窗口创建和 `crashes::init` 等重资源的初始化——第二实例越早退出越省资源。位置是"依赖已就绪、代价未付出"的平衡点。

**练习 2**：Linux 实现里为什么绑定前要尝试 `connect` 一次并在 `ConnectionRefused` 时删除 socket 文件？

**答案**：unix socket 文件在进程崩溃/被杀后不会自动删除。若上次实例异常退出，文件残留但无人监听，直接 `bind` 会报 `AddrInUse`，让 Zed 永远无法启动。先用 `connect` 探活：`ConnectionRefused` 证明文件是"死"的，删掉再绑定即可自愈；若 connect 成功则说明活实例还在，bind 自然失败，正确走"已有实例"分支。

**练习 3**：开发者同时调试三个不同通道的 Zed，会互相干扰单实例吗？

**答案**：不会。socket 文件名包含 `RELEASE_CHANNEL_NAME`（`zed-dev.sock`、`zed-stable.sock`、`zed-preview.sock` 各自独立）；且 Dev 通道本身直接跳过检查。通道隔离是文件名级的，这与 u1-l1 讲的"四通道靠不同 bundle identifier 共存"是同一思想在 IPC 层的体现。

## 5. 综合实践

**综合任务：绘制"启动前置阶段"的完整决策流程图，并采集三种模式的实证**

1. 通读 [src/main.rs:L200-L420](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L200-L420)，用你熟悉的工具（纸笔、mermaid、draw.io 均可）画出从进程入口到 `app.run` 之前的完整流程图，要求：
   - 标出**每一个提前 `return` 的出口**（至少 7 个：sandbox、askpass、crash-handler、ETW、printenv、dump-all-actions、system-specs，加上 init_paths 失败与单实例失败）；
   - 在 `init_paths` 分支旁注明 8 个待创建目录及 Linux 默认路径；
   - 在 zlog 分支旁注明两条输出通道及判据 `!ZED_FORCE_CLI_MODE && is_terminal()`。
2. 实证部分（本地完成，全部标注实际结果或"待本地验证"）：
   - 采集 `--printenv`、`--system-specs`、`--dump-all-actions` 三条命令的输出各前若干行，贴进笔记；
   - 用 `--dump-all-actions | jq '.actions | length'` 记录 action 总数（本讲义编写时未运行，数值待本地验证）；
   - 触发一次目录创建失败（见 4.2.4），记录错误提示文案与 GUI 兜底形态。
3. 自查问题：如果把你新画的流程图里 `--user-data-dir` 的处理挪到 `init_paths()` 之后，会发生什么？（提示：目录会建在旧位置，而运行时读写却指向新位置。）

## 6. 本讲小结

- `zed` 是**多模式二进制**：askpass 助手、崩溃处理服务器、ETW 追踪器、printenv/dump-all-actions/system-specs 诊断模式共用同一份代码，靠 `Args::parse()` 前后的顺序化 `return` 分流；`hide = true` 的参数是主进程与 CLI 的私有协议。
- 启动顺序高度敏感：沙箱启动器必须先于参数解析；`--user-data-dir` 必须先于 `init_paths()`；`--system-specs` 晚于版本计算与日志初始化。
- `init_paths()` 用 `create_dir_all` 就位 8 个目录，失败按 `io::ErrorKind` 聚类；`files_not_created_on_launch` 以"新起最小 GPUI 应用弹 Critical 提示"兜底，开窗失败再降级为 eprintln/桌面通知（`fail_to_open_window`）。
- 日志初始化是可降级的软依赖：pty 分流 stdout/`Zed.log` 文件（轮转为 `.old`），文件打不开退回 stdout；`ZED_FORCE_CLI_MODE` 由 zed CLI 设置、读取后立即从环境中移除。
- 单实例检查位于基础设施就绪之后、重资源初始化之前；`ZED_STATELESS` 与 Dev 通道跳过；Linux 用通道命名的 unix datagram socket（兼作后续打开请求的信箱），macOS 查进程列表，Windows 用命名对象。

## 7. 下一步学习建议

下一讲 **u2-l1《app.run：全局 init 序列》**将越过本讲的边界，进入 `app.run` 闭包：数十个 `init` 调用如何分层完成全局状态注册与配置观察者挂接。建议预习时先浏览 [src/main.rs:L478-L560](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L478-L560)，数一数出现了多少个 `init`，试着按"基础设施 → 服务 → UI 框架"给它们粗分层。若对单实例与打开请求的转发链路更感兴趣，可以提前跳读 `src/zed/open_listener.rs` 的 `OpenRequest::parse`（u3-l1 的主题），再回头补 u2 的主链路。
