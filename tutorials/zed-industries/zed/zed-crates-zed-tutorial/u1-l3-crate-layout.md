# u1-l3 目录结构与模块树

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 `crates/zed` 的完整模块树，并准确说出哪些模块只在 macOS / Windows / 开启 `visual-tests` feature 时才参与编译。
2. 说出 `zed.rs`（7830 行）和 `open_listener.rs`（3022 行）两个大文件各自内部的「函数分区」，拿到任何一个功能点（比如「打开设置文件」）都能直接跳到对应的行号区间。
3. 掌握一套「功能 → 文件」的速查方法，后续阅读任何一讲时都能先把源码定位到手。

本讲不讲解任何函数的实现细节——那是个后续讲义的任务。本讲只做一件事：**给你画一张这个 crate 的地图**。

## 2. 前置知识

### 2.1 Rust 模块系统：文件与目录的配对规则

Rust 中一个模块可以由「两个部分」组成：

- `src/zed.rs`：模块 `zed` 的根文件。
- `src/zed/` 目录：存放 `zed` 模块的子模块文件。

`src/zed.rs` 里写 `mod open_listener;`，编译器就会去找 `src/zed/open_listener.rs`。**`zed.rs` 与 `zed/` 目录是同一个模块的两半**，不是两个独立模块。`reliability.rs` 与 `reliability/` 目录同理。

### 2.2 二进制目标不等于模块

`Cargo.toml` 里的 `[[bin]]` 段声明的是「二进制目标」，每个 `[[bin]]` 有自己独立的 crate 根文件，**互相不在对方的模块树里**。所以 `src/visual_test_runner.rs` 虽然躺在 `src/` 下，却不会被 `main.rs` 的模块树包含——它自己就是一个独立程序的入口。

### 2.3 条件编译（`#[cfg(...)]`）

`#[cfg(target_os = "macos")]` 写在 `mod` 声明上面时，表示「只有编译目标是 macOS 时，这个模块才存在」。这让同一份代码可以在不同平台编译出不同的模块集合，而不需要维护多个仓库。

### 2.4 承接前两讲

- u1-l1 告诉我们：这个 crate 是总装型二进制，自己几乎不写业务逻辑，职责是「把各功能 crate 装配成可运行的编辑器」。
- u1-l2 告诉我们：`build.rs` 在构建期向 `OUT_DIR` 写入图标等产物，源码用 `include_bytes!` 消费。

本讲把视角从「构建」切到「源码组织」：装配逻辑具体摊在哪些文件里、每个文件多大、内部分几块。

## 3. 本讲源码地图

先用 `wc -l` 实测的行数建立体量直觉（数值为当前 HEAD 实测）：

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `src/main.rs` | 2022 | 主二进制入口：多模式分发、init 序列、会话恢复 |
| `src/zed.rs` | 7830 | 窗口/工作区/面板/action 的装配大厅（约 4900 行是测试） |
| `src/zed/open_listener.rs` | 3022 | zed:// URL 解析、CLI IPC、打开行为（约 1900 行是测试） |
| `src/visual_test_runner.rs` | 3595 | 第二个二进制 `zed_visual_test_runner`，独立入口 |
| `src/reliability.rs` | 528 | 卡顿检测、内存监控、崩溃上报的启动器 |
| `src/reliability/hang_detection.rs` | 123 | 卡顿检测主循环 |
| `src/reliability/hang_detection/` | 189 + 67 + 301 | 落盘 / 任务栈采样 / 遥测三个子模块 |
| `src/zed/quick_action_bar.rs` | 823 | 编辑器右上角工具栏 |
| `src/zed/app_menus.rs` | 325 | 应用菜单栏构建 |
| `src/zed/telemetry_log.rs` | 625 | 遥测日志视图（标准 Item 范例） |
| `src/zed/edit_prediction_registry.rs` | 466 | 补全提供方注册 |
| `src/zed/visual_tests.rs` | 551 | macOS 专属可视化测试 |
| `src/zed/migrate.rs` | 326 | 设置/键位迁移提示 |
| `src/zed/move_to_applications.rs` | 320 | macOS「移动到 Applications」提示 |
| `src/zed/windows_only_instance.rs` | 224 | Windows 单实例 |
| `src/zed/mac_only_instance.rs` | 151 | macOS 单实例 |
| `src/zed/open_url_modal.rs` | 116 | 「打开 URL」模态框 |
| `src/zed/remote_debug.rs` | 53 | 远程调试辅助 |

一个反直觉的事实：**两个最大的文件里约 63% 的行数是测试**（`zed.rs` 的测试从第 2916 行开始，`open_listener.rs` 的从第 1126 行开始）。所以真正要读的「正文」只有：`zed.rs` 约 2900 行、`open_listener.rs` 约 1100 行、`main.rs` 约 2000 行——地图并不吓人。

## 4. 核心概念与源码讲解

### 4.1 模块树与条件编译

#### 4.1.1 概念说明

这个 crate 的模块树很浅，只有两层：

- 第一层：`main.rs`（crate 根）声明 `reliability` 和 `zed` 两个模块。
- 第二层：`zed.rs` 与 `reliability.rs` 各自声明自己的子模块，其中一部分带平台条件。

为什么要用条件编译而不是把平台代码拆成三个 crate？因为单实例检查这类逻辑与平台强绑定但体量很小（151 / 224 行），拆 crate 的维护成本远高于一个 `#[cfg]` 属性。这是「小体量平台差异就地用 cfg」的典型取舍。

#### 4.1.2 核心流程

完整的模块树如下（⌘ 表示 macOS、⊞ 表示 Windows、🧪 表示需要 `visual-tests` feature）：

```text
zed (crate)
├── [[bin]] zed                      → src/main.rs
│   ├── mod zed                      → src/zed.rs
│   │   ├── app_menus
│   │   ├── edit_prediction_registry
│   │   ├── mac_only_instance        ⌘  仅 macOS
│   │   ├── migrate
│   │   ├── move_to_applications     ⌘  仅 macOS
│   │   ├── open_listener
│   │   ├── open_url_modal
│   │   ├── quick_action_bar
│   │   │   ├── preview
│   │   │   └── repl_menu
│   │   ├── remote_debug
│   │   ├── telemetry_log
│   │   ├── visual_tests             ⌘🧪 仅 macOS 且开启 visual-tests
│   │   └── windows_only_instance    ⊞  仅 Windows
│   └── mod reliability              → src/reliability.rs
│       └── hang_detection
│           ├── logging
│           ├── task_traces
│           └── telemetry
└── [[bin]] zed_visual_test_runner   → src/visual_test_runner.rs（独立入口，不在模块树中）
```

在 Linux 上编译时，`mac_only_instance`、`move_to_applications`、`visual_tests`、`windows_only_instance` 四个模块根本不存在；`visual_tests` 还额外要求编译时带上 `visual-tests` feature。

#### 4.1.3 源码精读

**第一站：crate 根只声明两个模块。** [src/main.rs:L4-L5](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L4-L5) 中 `mod reliability;` 与 `mod zed;` 是整个模块树的起点。`main.rs` 自己则承担入口职责——`fn main()` 定义在 [src/main.rs:L200](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L200)。

**第二站：两个二进制目标。** [Cargo.toml:L56-L63](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/Cargo.toml#L56-L63) 声明了 `zed`（指向 `src/main.rs`，无条件构建）与 `zed_visual_test_runner`（指向 `src/visual_test_runner.rs`，`required-features = ["visual-tests"]`）。这解释了为什么 `visual_test_runner.rs` 有 3595 行却不出现在 `main.rs` 的 mod 声明里。

**第三站：zed 的子模块清单。** [src/zed.rs:L1-L16](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L1-L16) 是整个 crate 最值得背下来的 16 行——12 个子模块的可见性与平台条件全在这里：

- `#[cfg(target_os = "macos")]` 罩住 `mac_only_instance` 与 `move_to_applications`；
- `#[cfg(target_os = "windows")]` 罩住 `windows_only_instance`；
- `#[cfg(all(target_os = "macos", feature = "visual-tests"))]` 罩住 `visual_tests`；
- 其余模块无条件编译。

**第四站：reliability 的子模块。** [src/reliability.rs:L26](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/reliability.rs#L26) 只声明一个 `mod hang_detection;`，而 [src/reliability/hang_detection.rs:L10-L12](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/reliability/hang_detection.rs#L10-L12) 再声明 `logging`、`task_traces`、`telemetry` 三个子模块——这是模块树里唯一的三层链。

**第五站：装配关系。** 模块树描述「静态结构」，还要知道「谁调用谁」：`main.rs` 在启动序列里调用 [zed::init（src/zed.rs:L194）](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L194) 与 [reliability::init（src/reliability.rs:L28）](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/reliability.rs#L28)，调用点分别在 [src/main.rs:L586](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L586) 和 [src/main.rs:L659](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L659)。也就是说：`main.rs` 是「启动器」，`zed.rs` 与 `reliability.rs` 是「被启动的装配间」。

#### 4.1.4 代码实践

**实践目标**：亲手生成一份属于你自己的模块清单，验证上面的模块树，并观察条件编译在你当前平台上的实际效果。

**操作步骤**：

1. 在 `crates/zed` 目录下执行：

   ```bash
   # 1. 列出所有源码文件及行数
   find src -name "*.rs" | xargs wc -l | sort -n

   # 2. 找出所有模块声明及其紧邻的 cfg 条件
   grep -rn -B1 "^mod \|^pub mod \|^pub(crate) mod " src --include="*.rs"
   ```

2. 把第 2 步的输出与 4.1.2 的模块树对照，标出你当前平台（Linux）上**不会**编译的模块。
3. 执行 `cargo tree -p zed -e no-dev 2>/dev/null | head -5` 确认 crate 本身能被解析（这一步只验证 Cargo.toml，与模块树无关，做个体感练习即可）。

**需要观察的现象**：

- `grep -B1` 的输出里，`mac_only_instance`、`move_to_applications`、`windows_only_instance`、`visual_tests` 的上一行都是 `#[cfg(...)]`。
- `src/zed/visual_tests.rs` 文件在磁盘上始终存在（Linux 上也能 `ls` 到），但它不参与 Linux 构建——**文件存在 ≠ 模块存在**。

**预期结果**：你得到一份与 4.1.2 一致的模块树，并且确认 Linux 构建时模块树会「瘦身」掉 4 个模块。

（本实践的命令均为只读操作，可安全运行；若你的环境没有安装 Rust 工具链，第 3 步可跳过，标注「待本地验证」即可。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `src/visual_test_runner.rs` 不需要出现在 `main.rs` 的 `mod` 声明中？

**答案**：因为它不是 `main.rs` 这个 crate 根的模块，而是 Cargo.toml 中第二个 `[[bin]]` 目标的独立 crate 根。两个二进制目标各自从自己的根文件开始构建模块树，互不可见。若在 `main.rs` 里也写 `mod visual_test_runner;`，反而会把 3595 行代码编进主程序。

**练习 2**：如果要在 Linux 上引用 `windows_only_instance` 模块里的函数，会发生什么？

**答案**：编译错误。`#[cfg(target_os = "windows")]` 使该模块在 Linux 构建中不存在，任何对它的路径引用（如 `crate::zed::windows_only_instance::xxx`）都无法解析。正确做法是在调用点同样加平台条件，或用条件编译提供替代实现。

**练习 3**：`mod hang_detection;` 写在 `reliability.rs` 而不是 `main.rs` 里，这决定了它的完整路径是什么？

**答案**：`crate::reliability::hang_detection`。模块路径由声明位置决定：`main.rs` 声明 `reliability`，`reliability.rs` 声明 `hang_detection`，它的三个子模块则分别是 `crate::reliability::hang_detection::logging` 等。

### 4.2 大文件的函数分区

#### 4.2.1 概念说明

`zed.rs` 和 `open_listener.rs` 合计占掉这个 crate 正文代码的约三分之二。读这种文件最忌讳从第 1 行顺读到最后一行——正确做法是先建一张「函数名 → 行号区间 → 职责」的分区表，然后按需跳转。本节直接把这张表建好，后续每一讲都会反复引用它。

为什么这两个文件会这么大还不拆分？回忆 CLAUDE.md 的编码守则：「Prefer implementing functionality in existing files unless it is a new logical component」——这个仓库的文化就是优先在现有文件里扩展，只有出现全新逻辑组件时才建新文件。所以「大文件 + 清晰分区」是这个 crate 的常态而非坏味道。

#### 4.2.2 核心流程

`zed.rs` 正文（L1–L2915）可以粗分为四个功能区：

```text
zed.rs（7830 行）
├── L1–L121    导入与模块声明区：12 个子模块 + 大量外部 crate 导入
├── L122–L192  action 定义区：actions! 宏批量定义 zed / dev 命名空间的 action
├── L194–L2915 装配逻辑区（按启动时序排列，见下表）
└── L2916–L7830 测试区：#[cfg(test)] mod tests
```

`open_listener.rs` 正文（L1–L1125）天然分成「数据结构与解析」和「连接与打开」两半：

```text
open_listener.rs（3022 行）
├── L1–L35     导入区
├── L36–L380   请求模型区：OpenRequest / OpenRequestKind / parse 系列
├── L381–L464  通道与 IPC 区：OpenListener / listen_for_cli_connections / connect_to_cli
├── L465–L834  行为决策区：open_paths_with_positions / handle_cli_connection / resolve_open_behavior / open_options_for_*
├── L835–L1125 执行区：open_workspaces / open_local_workspace / derive_paths_with_position
└── L1126–L3022 测试区：#[cfg(test)] mod tests
```

#### 4.2.3 源码精读

**zed.rs 装配逻辑区的关键锚点**（行号均为当前 HEAD 实测）：

| 行号 | 函数 | 职责 | 后续讲义 |
| --- | --- | --- | --- |
| [L194](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L194) | `init` | zed 模块总入口：注册平台 action、绑定窗口关闭回调 | u2-l1 |
| [L361](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L361) | `build_window_options` | 组装窗口选项（标题栏、图标、最小尺寸） | u4-l1 |
| [L430](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L430) | `initialize_workspace` | 观察新 Workspace，挂接关闭确认与装配流程 | u4-l2 |
| [L779](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L779) | `initialize_panels` | 并行加载六大面板 | u4-l3 |
| [L912](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L912) | `register_actions` | 注册 workspace 级 action（约 550 行的大函数） | u4-l5 |
| [L1468](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L1468) | `initialize_pane` | PaneAdded 事件驱动的工具栏装配 | u4-l4 |
| [L1996](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L1996) | `notify_settings_errors` | 设置解析错误的分级通知 | u5-l1 |
| [L2190](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L2190) | `watch_settings_files` | 监听 settings.json 变更 | u5-l1 |
| [L2211](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L2211) | `handle_keymap_file_changes` | keymap 监听→解析→重载 | u5-l2 |
| [L2420](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L2420) | `load_default_keymap` | 多 keymap 合成 | u5-l2 |
| [L2772](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L2772) | `open_settings_file` | 打开用户设置文件 | u5-l5 |
| [L2826](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L2826) | `eager_load_active_theme_and_icon_theme` | 预加载当前主题 | u5-l3 |

注意函数排列大致沿「启动时序」展开：`init` → 窗口选项 → 工作区观察 → 面板 → action → 工具栏，然后是运行期的设置/keymap/主题热加载。**文件顺序就是阅读顺序**，这是读装配型代码的一个便利。

**action 定义区的一个条件编译细节**：[src/zed.rs:L158-L192](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L158-L192) 有两个 `actions!(dev, ...)` 块，第二个被 `#[cfg(debug_assertions)]` 罩住——`ShowWorkspaceError` 这个调试用 action 只在 debug 构建中存在。这说明条件编译不只用于平台，也用于构建类型。

**同名函数多版本按平台选择**：`initialize_file_watcher` 有两个定义——[src/zed.rs:L667-L669](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L667-L669) 是 Linux/FreeBSD 版（检查 inotify），紧随其后的 Windows 版检查 ReadDirectoryChangesW。调用点只有一处（`initialize_workspace` 内），编译器按目标平台自动选择版本。

**open_listener.rs 的关键锚点**：

| 行号 | 条目 | 职责 | 后续讲义 |
| --- | --- | --- | --- |
| [L37](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L37) | `struct OpenRequest` | 打开请求的统一数据模型 | u3-l1 |
| [L49](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L49) | `enum OpenRequestKind` | 请求类型枚举（CLI 连接、agent、git clone 等） | u3-l1 |
| [L135](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L135) | `OpenRequest::parse` | URL → OpenRequest 的解析入口 | u3-l1 |
| [L217–L285](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L217-L285) | `parse_file_path` 等 6 个辅助 | 各种子 URL 的分项解析 | u3-l1 |
| [L324](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L324) | `parse_ssh_url` | SCP 风格 ssh 地址规范化 | u3-l2 |
| [L381](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L381) | `struct OpenListener` | 全局通道生产端 | u3-l5 |
| [L410](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L410) | `listen_for_cli_connections` | Linux unix socket 监听 | u3-l5 |
| [L465](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L465) | `open_paths_with_positions` | 打开一批带位置信息的路径 | u3-l4 |
| [L576](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L576) | `handle_cli_connection` | CLI IPC 连接处理 | u3-l3 |
| [L689](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L689) | `resolve_open_behavior` | 首次使用时的交互式打开行为提示 | u3-l3 |
| [L791](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L791) | `open_options_for_behavior` | OpenBehavior → OpenOptions 映射 | u3-l4 |
| [L835](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L835) | `open_workspaces` | 打开工作区总分发（本地/远程分支） | u3-l4 |
| [L952](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L952) | `open_local_workspace` | 本地工作区打开实现 | u3-l4 |
| [L1089](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L1089) | `derive_paths_with_position` | 路径 → PathWithPosition 推导 | u6-l5 |

**两个大文件的收尾都是测试**：[src/zed.rs:L2916-L2917](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L2916-L2917) 和 [src/zed/open_listener.rs:L1126-L1127](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L1126-L1127) 都是 `#[cfg(test)] mod tests {`。读正文时可以直接忽略其后所有内容；反过来，u6-l5 会专门钻进这两个测试区。

#### 4.2.4 代码实践

**实践目标**：用一条命令自动生成两个大文件的分区清单雏形，并补全「职责」列，形成你自己的阅读地图。

**操作步骤**：

1. 生成 `zed.rs` 的顶层条目清单（只看正文区，排除测试）：

   ```bash
   head -2915 src/zed.rs | grep -nE "^(pub |pub\(crate\) )?(async )?fn |^impl |^actions!|^#\[cfg\(test\)\]"
   ```

2. 对 `open_listener.rs` 做同样的事：

   ```bash
   head -1125 src/zed/open_listener.rs | grep -nE "^(pub |pub\(crate\) )?(async )?fn |^impl |^struct |^enum "
   ```

3. 把输出粘贴进表格工具（或纯文本表），为每个条目补一列「职责」——不确定的先写「待确认」，读到对应讲义时回填。

**需要观察的现象**：

- 步骤 1 的输出里，`fn` 定义在 L176 到 L2826 之间分布不均：L912–L1468 之间几乎只有 `register_actions` 一个函数（它占了约 550 行）。
- 步骤 2 的输出里，L125 的 `impl OpenRequest` 块内部还会嵌套若干 `parse_*` 辅助函数，它们以 4 空格缩进出现——如果想把嵌套函数也列出来，把正则开头的 `^` 去掉并加上缩进匹配即可。

**预期结果**：得到两张「函数名 → 行号 → 职责」表，与 4.2.3 的锚点表互相印证；后续每读一讲，都可以直接在这张表上定位入口函数。

（命令为只读操作，可安全运行。）

#### 4.2.5 小练习与答案

**练习 1**：`zed.rs` 有 7830 行，其中测试占多少？怎么用一条命令验证？

**答案**：约 4914 行（从 L2916 的 `#[cfg(test)]` 到文件末尾）。验证：`sed -n '2916,7830p' src/zed.rs | wc -l` 得 4915 行（含起始行）。这说明读正文时用编辑器折叠 L2916 之后的内容即可把文件「缩短」近三分之二。

**练习 2**：我想找「Zed 收到 zed://git/clone URL 后在哪里解析」，应该跳到 `open_listener.rs` 的哪一段？

**答案**：先跳 [OpenRequest::parse（L135）](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L135) 看分发结构，再进入 [parse_git_clone_url（L243 附近）](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L243-L262)——按分区表，URL 解析逻辑都在 L36–L380 的「请求模型区」。

**练习 3**：`register_actions` 一个函数约 550 行，为什么这个仓库仍然不把它拆文件？

**答案**：仓库编码守则（CLAUDE.md）明确「Prefer implementing functionality in existing files unless it is a new logical component」。action 注册本质上是同一逻辑组件（把 action 绑定到 workspace）的清单式罗列，拆出去反而增加文件跳转成本。读它时应按「action 名 → 处理逻辑」逐条扫读，而不是顺序通读。

### 4.3 文件职责速查

#### 4.3.1 概念说明

有了模块树和分区表，最后一层地图是「反向索引」：**从功能反查文件**。真实读码场景往往是「我知道现象，不知道代码在哪」，这时需要按关键词直接命中文件。本节把本 crate 最常被查询的功能点整理成速查表，并示范两种通用定位手法。

#### 4.3.2 核心流程

两种定位手法：

1. **从 action 名入手**：界面上几乎所有可触发的行为都是 action（u4-l5 详讲）。在 `src/zed.rs` 的 actions! 区或各面板 crate 里 `grep` action 名，再 `grep` 它的 `on_action` 注册点。
2. **从数据类型入手**：想知道某个流程，先找它的核心结构体（如 `OpenRequest`），再看谁构造它、谁消费它。

速查表（功能 → 位置 → 后续讲义）：

| 我想找…… | 去这里 | 讲义 |
| --- | --- | --- |
| 程序入口、命令行参数 | `src/main.rs` L200 `fn main`、L1688 `struct Args` | u1-l4 |
| 全局 init 顺序（settings、theme、workspace 等） | `src/main.rs` 的 `app.run` 闭包（L586 调 `zed::init`、L659 调 `reliability::init`） | u2-l1 |
| 打开请求的分发（所有 zed:// 的汇合点） | `src/main.rs` L1002 `handle_open_request` | u2-l4 |
| zed:// URL 解析 | `src/zed/open_listener.rs` L135 `OpenRequest::parse` | u3-l1 |
| ssh:// 远程打开 | `src/zed/open_listener.rs` L324 `parse_ssh_url` | u3-l2 |
| 单实例检查 | `mac_only_instance.rs` / `windows_only_instance.rs` / `open_listener.rs` L410 | u3-l5 |
| 窗口外观（标题栏、图标） | `src/zed.rs` L361 `build_window_options` | u4-l1 |
| 面板加载 | `src/zed.rs` L779 `initialize_panels` | u4-l3 |
| 菜单栏 | `src/zed/app_menus.rs` | u5-l4 |
| 设置文件打开/热加载 | `src/zed.rs` L2190 `watch_settings_files`、L2772 `open_settings_file` | u5-l1 / u5-l5 |
| keymap 加载与迁移 | `src/zed.rs` L2211、`src/zed/migrate.rs` | u5-l2 |
| 卡顿检测、崩溃上报 | `src/reliability.rs` L28 `init` 及 `hang_detection/` | u6-l1 / u6-l2 |
| 测试基础设施 | `src/zed.rs` L2916 起的 `mod tests` | u6-l5 |

#### 4.3.3 源码精读

**main.rs 里的关键定位点**：`handle_open_request` 定义在 [src/main.rs:L1002](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1002)，它是「启动器」与「打开协议层」之间的桥——`open_listener.rs` 顶部第一行就 `use crate::handle_open_request;`（见 [src/zed/open_listener.rs:L1](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/open_listener.rs#L1)），解析完成后交回 `main.rs` 分发。这条 `use` 是判断两个文件关系最快的线索。

**main.rs 自己的分区**（L200 之后按职责排列）：`fn main`（L200）占约 800 行完成启动；`handle_open_request`（L1002）处理打开请求；`authenticate`/`system_id`/`installation_id`（L1367–L1396 起）处理身份与登录；`init_paths`（L1656）创建启动目录；`struct Args`（L1688）是 clap 参数定义；尾部 L1827–L1960 是字体/主题/语言加载辅助；L1963 `dump_all_gpui_actions` 与 L2007 `check_for_conpty_dll` 是两个调试/平台辅助。

**小文件也各就各位**：`quick_action_bar.rs` 在 [L1-L2](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed/quick_action_bar.rs#L1-L2) 声明 `mod preview;` 与 `mod repl_menu;` 两个子模块，分别处理图片预览按钮和 REPL 菜单——查工具栏按钮行为时记得进这两个文件。

#### 4.3.4 代码实践

**实践目标**：完成三个「功能定位」小任务，验证速查表并训练 grep 定位手感。

**操作步骤**：

1. **定位「打开设置文件」action**：

   ```bash
   grep -n "OpenDefaultSettings\|OpenProjectSettingsFile\|open_settings_file" src/zed.rs | head
   ```

   预期命中三类位置：L134/L136 附近的 `actions!(zed, ...)` 定义（action 名）、L308 附近的 `on_action` 注册（处理逻辑）、L2772 的 `open_settings_file`（真正干活的函数）。

2. **定位单实例检查在 macOS 上的入口**：

   ```bash
   grep -n "ensure_only_instance" src/zed/mac_only_instance.rs src/main.rs
   ```

   预期命中 `mac_only_instance.rs` L88 的 `pub fn ensure_only_instance()` 与 `main.rs` L377 的调用点（调用点处于 `#[cfg(target_os = "macos")]` 块中，Linux 构建时该调用不存在）。

3. **定位「状态栏」装配**：已知状态栏 item 注册在 `initialize_workspace` 内部：

   ```bash
   sed -n '430,660p' src/zed.rs | grep -n "add_item\|status_bar"
   ```

   预期看到若干 `add_item` 调用，左右两侧 item 各占一段。

**需要观察的现象**：

- 三个任务都不需要通读任何文件，每条命令输出都在 20 行以内。
- 任务 1 里 action 定义（`actions!` 宏）与处理函数（`on_action` 或直接调用）是分开的两处——这正是 u4-l5 要展开的主题。

**预期结果**：三次定位全部命中速查表给出的位置；若某次 miss，先检查 grep 的大小写与命名习惯（这个仓库用 snake_case 函数 + PascalCase action）。

（命令均为只读操作。任务 2 展开实现属于 u3-l5 的内容，本讲只需确认「定义在专属模块、调用在 main.rs 平台条件块中」这一结构。）

#### 4.3.5 小练习与答案

**练习 1**：界面上点了一个菜单项，想找它对应的代码，第一步做什么？

**答案**：先在命令面板或 keymap 里确认 action 名（PascalCase，如 `OpenProjectSettingsFile`），然后 `grep -rn "OpenProjectSettingsFile" src/` 依次找到三处：`actions!` 定义（src/zed.rs L136 附近）、`on_action` 注册（通常在 `register_actions` 或面板代码里）、菜单绑定（`app_menus.rs`）。

**练习 2**：`use crate::handle_open_request;` 出现在 `open_listener.rs` 第一行，这说明两个文件是什么关系？

**答案**：`open_listener`（子模块）解析完请求后回调 `main.rs`（crate 根）里的 `handle_open_request` 做实际分发。数据流向是：URL/socket → `open_listener` 解析 → `main.rs::handle_open_request` 分发 → 打开工作区。这是「协议解析」与「业务分发」的分离。

**练习 3**：为什么速查表里「单实例检查」要列三个位置？

**答案**：因为三个平台用三种机制：macOS 用 `mac_only_instance`、Windows 用 `windows_only_instance`、Linux 用 `open_listener.rs` 里的 unix socket 监听（`listen_for_cli_connections`，L410）。条件编译决定了你当前平台只会用到其中之一，u3-l5 会统一讲解。

## 5. 综合实践

**任务：产出你的《crates/zed 源码地图》文档。**

把本讲三张图合并成一份可以长期维护的地图文件（放在你自己的笔记里，不要写进仓库），必须包含：

1. **完整模块树**：照 4.1.2 的格式手画一遍，用你自己的命令输出验证，并标注四个平台条件模块与一个 feature 条件模块（`visual_tests`）。
2. **zed.rs 分区清单**：用 4.2.4 步骤 1 的命令生成，保留「函数名 → 行号区间 → 职责」三列，职责列至少填 8 行，允许暂写「待确认」。
3. **open_listener.rs 分区清单**：同上。
4. **三条跨文件边**：用一句话描述 `main.rs → zed.rs`（谁调用 `zed::init`）、`main.rs → reliability.rs`、`open_listener.rs → main.rs`（`use crate::handle_open_request`）三条调用/依赖边。

验收标准：拿一个你好奇的功能（例如「Zed 是怎么打开 settings.json 的」），只用这份地图在 1 分钟内跳到正确的文件与行号区间。做到这一点，本讲的目标就达成了。

注意：所有行号锚定在当前 HEAD（`a7d74150`）。zed 仓库迭代很快，隔一段时间后应重新运行本讲的 grep 命令刷新行号——这也是为什么地图要用命令生成而不是手抄。

## 6. 本讲小结

- `crates/zed` 的模块树极浅：`main.rs` 只声明 `zed` 与 `reliability` 两个模块；`zed.rs` 再声明 12 个子模块，其中 4 个带平台条件、1 个还要求 `visual-tests` feature。
- `src/visual_test_runner.rs` 是第二个 `[[bin]]` 的独立入口，不属于 `main.rs` 的模块树——文件存在不等于模块存在。
- 两大文件 `zed.rs`（7830 行）与 `open_listener.rs`（3022 行）各有约 63% 是测试代码，正文分别约 2900 行与 1100 行；正文内部按启动时序/请求处理阶段自然分区，先建分区表再按需跳读。
- `main.rs` 是启动器：L586 调 `zed::init`、L659 调 `reliability::init`、L1002 的 `handle_open_request` 接收 `open_listener` 解析出的请求——这三条边是 crate 内部最重要的依赖关系。
- 定位功能的两把钥匙：action 名（界面行为）与核心结构体名（数据流），配合速查表可以直接命中文件与行号区间。

## 7. 下一步学习建议

地图已经有了，下一步沿「执行顺序」走：下一讲 **u1-l4 main() 入口：多模式与启动前置** 将逐段阅读 [src/main.rs:L200](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L200) 起的 `fn main()`——沙箱启动器、clap 参数解析、`init_paths` 目录创建与日志初始化。

在进入下一讲之前，建议先自己通读一遍 `src/main.rs` 的 L200–L260（`main` 的前 60 行），对照本讲速查表确认你能说出每一行属于哪个功能区；有兴趣的读者也可以提前浏览 `src/zed/open_listener.rs` L36–L83 的 `OpenRequest`/`OpenRequestKind` 定义，为单元 3 的 URL 协议解析建立数据模型直觉。
