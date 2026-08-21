# 项目定位与整体结构：sidebar crate 是什么

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `sidebar` crate 在 Zed 编辑器中的角色——它是「多项目 Agent 线程与终端侧边栏」的实现，而不是传统意义上的文件树面板。
2. 说出本 crate 三个源码文件（`sidebar.rs`、`sidebar_tests.rs`、`thread_switcher.rs`）各自的职责和体量。
3. 从 `Cargo.toml` 的依赖列表推断出 sidebar 与 `agent_ui`、`workspace`、`project`、`git_ui_core` 等兄弟 crate 之间的边界。
4. 在 `zed` crate（应用外壳）中找到 `Sidebar` 的创建点，并解释 `MultiWorkspace::register_sidebar` 如何把侧边栏挂进多工作区容器。

## 2. 前置知识

本讲假设你第一次接触 Zed 代码库，但需要以下基础概念：

- **crate（Rust 包）**：Rust 的编译单元。Zed 是一个 Cargo workspace，`crates/` 目录下每个子目录都是一个 crate，`zed` crate 是最终的应用外壳（二进制入口），其余大多是库 crate。
- **GPUI**：Zed 自研的 UI 框架，同时提供状态管理与并发原语。核心概念：
  - **Entity（实体）**：`Entity<T>` 是对类型 `T` 的共享句柄，通过 `cx.new(|cx| ...)` 创建。
  - **Render（渲染）**：实现 `Render` trait 的实体称为「视图」，其 `render` 方法把状态变成一棵元素树。
  - **Context（上下文）**：`Context<T>` 是更新 `Entity<T>` 时拿到的环境句柄，可用来注册订阅（`cx.subscribe`）、观察（`cx.observe`）和派发异步任务（`cx.spawn`）。
- **Agent（智能体）**：Zed 内置的 AI 编程助手。「线程（thread）」是一次与 Agent 的对话，「终端线程（terminal thread / agent terminal）」是 Agent 会话中派生的终端任务。
- **Workspace（工作区）与 MultiWorkspace（多工作区）**：Zed 支持在一个窗口里同时打开多个项目（multi-project / multi-root），`MultiWorkspace` 是容纳多个 `Workspace` 的窗口级容器，也是侧边栏的直接宿主。

如果你对 GPUI 的 Entity / Context 还不熟，不必担心——本讲只用到「创建实体、注册订阅」这一层皮毛，后续讲义会逐步深入。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [crates/sidebar/Cargo.toml](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/Cargo.toml) | 82 | crate 清单：声明库名、依赖（含 dev-dependencies） |
| [crates/sidebar/src/sidebar.rs](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs) | 8208 | 库根（见下方说明），包含 `Sidebar` 实体、数据模型、重建管线、渲染与全部交互逻辑 |
| [crates/sidebar/src/sidebar_tests.rs](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs) | 15111 | 测试模块，仅 `#[cfg(test)]` 时编译，体量约为实现代码的两倍 |
| [crates/sidebar/src/thread_switcher.rs](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/thread_switcher.rs) | 421 | ctrl-tab 风格的「线程切换器」模态组件，自包含 |
| [crates/zed/src/zed.rs](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/zed/src/zed.rs) | — | 应用外壳：`Sidebar` 在这里被创建并注册（还有 `dump_workspace_info` 调试动作的注册点） |
| [crates/workspace/src/multi_workspace.rs](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/workspace/src/multi_workspace.rs) | — | 多工作区容器：定义 `Sidebar` 契约 trait 与 `register_sidebar` |

一个值得注意的细节：本 crate **没有** `src/lib.rs`，库根直接由 Cargo.toml 指定为 `src/sidebar.rs`：

> [Cargo.toml:L11-L12](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/Cargo.toml#L11-L12) —— `[lib] path = "src/sidebar.rs"`，这是 Zed 仓库的命名惯例（用描述性文件名代替 `lib.rs`）。`sidebar.rs` 第一行 `mod thread_switcher;` 声明子模块，`#[cfg(test)] mod sidebar_tests;` 在测试构建时挂载测试文件（见 [sidebar.rs:L1](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1) 与 [sidebar.rs:L83-L84](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L83-L84)）。

## 4. 核心概念与源码讲解

### 4.1 crate 定位与三大源文件分工

#### 4.1.1 概念说明

打开 Zed 并启用多项目模式后，窗口左侧那条可以搜索、点选、折叠的「线程列表」，就是本 crate 渲染出来的。它同时承载四类内容：

1. **项目分组头（ProjectHeader）**——按项目（project group）聚合的分组行，可折叠、可弹出菜单；
2. **Agent 线程行（Thread）**——每个会话一行，显示标题、运行状态、diff 统计等；
3. **终端线程行（Terminal）**——Agent 派生的终端任务；
4. **辅助 UI**——顶部搜索过滤器、底部栏、归档视图入口、导入横幅、ctrl-tab 切换器。

要特别注意它与「项目面板（project panel，文件树）」的区别：sidebar crate 不显示文件树，它显示的是 **Agent 线程与终端**，且列表范围是**窗口内所有项目**（这正是它挂在 `MultiWorkspace` 而不是单个 `Workspace` 上的原因）。

#### 4.1.2 核心流程

本 crate 的运行主线可以概括为一句话：

```text
订阅外部状态（工作区/项目/元数据存储/Agent 面板）
        │  任一事件到来
        ▼
schedule_update_entries（合并去抖）
        ▼
update_entries → rebuild_contents（从「当前世界状态」全量重推导列表）
        ▼
ListState 增量应用 + cx.notify 触发重渲染
        ▼
render()（flexbox 元素树 → gpui list 虚拟列表）
```

`sidebar.rs` 顶部的 struct 文档注释明确写出了这条架构约束：

> [sidebar.rs:L730-L733](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L730-L733) —— 「侧边栏在每次变化时通过 `update_entries` → `rebuild_contents` 从零重推导整个条目列表。不要添加增量或跨事件协调状态——凡是能从当前世界状态算出来的，都在 rebuild 里算。」这段注释是理解整个 crate 的钥匙，后续第三单元会专门展开。

三个源文件的分工：

| 文件 | 定位 | 一句话概括 |
| --- | --- | --- |
| `sidebar.rs` | 库根 + 主实现 | `Sidebar` 实体：订阅、重建管线、数据模型、渲染、键盘交互、归档、序列化，全部在此 |
| `thread_switcher.rs` | 子模块 | 独立的模态视图 `ThreadSwitcher`：条目模型、焦点管理、修饰键释放即确认 |
| `sidebar_tests.rs` | 测试 | 数十个 gpui 集成测试，覆盖列表行为、键盘导航、归档级联、序列化往返等 |

#### 4.1.3 源码精读

**库根与动作声明。** [sidebar.rs:L1](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1) 声明 `mod thread_switcher;`；[sidebar.rs:L83-L84](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L83-L84) 在且仅在测试构建中挂载 `sidebar_tests` 模块。

**本 crate 自定义的动作（action）。** GPUI 中「动作」是键盘/菜单事件的目标，用 `gpui::actions!` 宏声明：

- [sidebar.rs:L86-L94](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L86-L94) —— 在 `agents_sidebar` 命名空间下声明 `NewThreadInGroup`（在当前分组新建线程）与 `ToggleThreadHistory`（在线程列表与历史归档之间切换）。
- [sidebar.rs:L96-L102](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L96-L102) —— 在 `dev` 命名空间下声明 `DumpWorkspaceInfo`（把多工作区状态倾倒进一个缓冲区，用于调试）。

**宽度常量。** [sidebar.rs:L104-L106](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L104-L106) 定义默认宽 300px、最小 200px、最大 800px——序列化恢复时宽度会被钳制在这个区间（见 4.4.3）。

**切换器子模块。** [sidebar.rs:L78-L81](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L78-L81) 从 `crate::thread_switcher` 引入 `ThreadSwitcher` 及其条目/事件类型；`thread_switcher.rs` 内部又反向引用父模块的 `ThreadEntryWorkspace`（[thread_switcher.rs:L15](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/thread_switcher.rs#L15)），两个文件形成双向协作，但切换器的 UI 逻辑完全自包含。

#### 4.1.4 代码实践

**实践目标**：用纯观察的方式确认三个文件的体量与分工，建立「读哪里」的直觉。

**操作步骤**：

1. 在 Zed 仓库根目录执行 `wc -l crates/sidebar/src/*.rs`，记录三个文件的行数。
2. 用编辑器打开 `sidebar.rs`，只看目录式导航（outline），数一数有多少个 `fn render_*`、多少个 `fn test_*`（后者应为 0——测试都在 `sidebar_tests.rs`）。
3. 打开 `thread_switcher.rs`，确认它只有约 421 行，并能独立读完。

**需要观察的现象**：`sidebar_tests.rs` 的行数明显大于 `sidebar.rs`（约 15111 对 8208 行）；`sidebar.rs` 中搜不到 `#[gpui::test]`。

**预期结果**：三个文件行数与上表一致；`#[gpui::test]` 只出现在 `sidebar_tests.rs` 中。本实践只是本地观察，无「待本地验证」风险项。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `sidebar_tests.rs` 不出现在 `Cargo.toml` 的任何 target 里，却能被编译？

答案：它通过库根 [sidebar.rs:L83-L84](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L83-L84) 的 `#[cfg(test)] mod sidebar_tests;` 挂进模块树，只在 `cargo test` 时编译；Cargo 会自动把 `<模块名>.rs` 作为同名模块的文件来源。

**练习 2**：`DumpWorkspaceInfo` 动作属于哪个命名空间？它面向什么用户？

答案：`dev` 命名空间（[sidebar.rs:L96-L102](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L96-L102)），面向开发者调试多工作区状态，普通用户日常不会触发。

### 4.2 Cargo.toml 依赖与 crate 边界

#### 4.2.1 概念说明

Zed 是个巨大的 Cargo workspace：顶层 `Cargo.toml` 用 `[workspace.dependencies]` 统一声明版本，各 crate 再以 `xxx.workspace = true` 引用。读一个 crate 的 `Cargo.toml`，基本就能画出它在架构中的位置——**依赖方向就是分层方向**。

sidebar crate 处在一个「上层 UI、中层编排」的位置：它自己不实现 Agent 会话（那是 `agent` / `agent_ui` 的事），不管项目文件扫描（`project`），也不管窗口与面板骨架（`workspace`）；它把这些能力组合成一条侧边栏。

#### 4.2.2 核心流程

依赖按用途可以分成六组（见下方源码精读中的表格）。数据大致这样流动：

```text
workspace(project) ──项目/工作树事件──┐
agent_ui(元数据存储、AgentPanel) ─────┤
                                      ▼
                          sidebar（订阅 + 全量重建 + 渲染）
                                      ▲
git / git_ui_core(分支信息) ──────────┤
gpui / ui / theme(绘制原语) ─────────┘
```

注意依赖是**单向**的：sidebar 依赖 `agent_ui`、`workspace`，反之 `zed` 依赖 sidebar（[crates/zed/Cargo.toml:L201](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/zed/Cargo.toml#L201) 的 `sidebar.workspace = true`）。这保证了库 crate 之间不循环引用。

#### 4.2.3 源码精读

**依赖声明区**：[Cargo.toml:L17-L53](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/Cargo.toml#L17-L53) 列出全部正式依赖；[Cargo.toml:L55-L82](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/Cargo.toml#L55-L82) 是带 `test-support` 特性的 dev-dependencies（测试专用，比如 `clock`、`db`、`pretty_assertions`）。

结合 `sidebar.rs` 顶部的 use 区段（[sidebar.rs:L3-L81](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L3-L81)），关键依赖的用途如下：

| 依赖（组） | Cargo.toml 行 | 在 sidebar.rs 中的真实使用点（示例） | 边界含义 |
| --- | --- | --- | --- |
| `agent_ui` | L24 | `AgentPanel`、`ThreadId`、`ThreadMetadataStore`、`ThreadsArchiveView`（[sidebar.rs:L8-L23](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L8-L23)） | Agent 面板 UI、线程元数据存储与归档视图都由 agent_ui 提供，sidebar 只做列表编排 |
| `agent` / `acp_thread` / `agent-client-protocol` / `agent_settings` | L21-L22, L18, L22 | `ThreadStore`、`ThreadStatus`、`acp::SessionId`（[sidebar.rs:L3-L6](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L3-L6)） | 会话数据模型与 ACP 协议类型；sidebar 不实现协议，只消费类型 |
| `workspace` | L51 | `MultiWorkspace`、`ProjectGroupKey`、`Sidebar` 契约 trait、`Open`（[sidebar.rs:L65-L70](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L65-L70)） | 窗口级容器与侧边栏契约（见 4.4）；workspace 不知道 sidebar 的存在 |
| `project` | L38 | `ProjectEvent`、`WorktreeId`、`AgentId`（[sidebar.rs:L42](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L42)） | 工作树增删与路径变化事件是列表重建的触发源之一 |
| `gpui` / `ui` / `theme` / `theme_settings` / `platform_title_bar` | L31, L48, L46-L47, L37 | `Entity`、`ListState`、`ThreadItem`、`ActiveTheme`、`CLIENT_SIDE_DECORATION_ROUNDING`（[sidebar.rs:L30-L35](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L30-L35)、[sidebar.rs:L56-L61](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L56-L61)） | 绘制与交互原语；`ThreadItem` 行组件直接来自 ui crate |
| `git` / `git_ui_core` | L30, L52 | `RemoteBranchName`、`worktree_create_targets`（[sidebar.rs:L72](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L72)） | 分组菜单里「新建 worktree」走 git_ui_core 的服务，sidebar 不直接做 git I/O |
| `menu` / `zed_actions` | L35, L53 | `Confirm`、`SelectNext` 等通用列表动作；`CreateWorktree`、`ToggleThreadSwitcher`（[sidebar.rs:L38-L40](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L38-L40)、[sidebar.rs:L72-L76](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L72-L76)） | 键盘动作复用全局动作定义，避免各写一份 |
| `editor` | L27 | 行内搜索过滤器与重命名输入框都是 `Editor` 实体（[sidebar.rs:L26](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L26)） | 文本输入复用编辑器组件 |
| `feature_flags` | L28 | `AgentThreadWorktreeLabelFlag`（[sidebar.rs:L27-L29](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L27-L29)） | 行上是否显示 worktree/branch 标签由远程特性开关控制 |

（其余如 `anyhow`、`chrono`、`serde`、`itertools`、`unicode-segmentation`、`util` 等为通用工具依赖。）

**一个特性开关细节**：[Cargo.toml:L24](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/Cargo.toml#L24) 写的是 `agent_ui = { workspace = true, features = ["audio"] }`——正式依赖里给 agent_ui 追加开了 `audio` 特性（线程完成提示音），而 dev-dependencies（L58）则开 `test-support`。这说明 crate 边界不止「谁依赖谁」，还包括「以什么特性依赖」。

#### 4.2.4 代码实践

**实践目标**：用 `cargo tree` 验证 Cargo.toml 阅读得出的依赖清单。

**操作步骤**：

1. 本机克隆 Zed 仓库并进入仓库根目录。
2. 执行 `cargo tree -p sidebar --depth 1`，把输出的直接依赖列表抄下来。
3. 与 [Cargo.toml:L17-L53](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/Cargo.toml#L17-L53) 逐项对照，确认一一对应。
4. 任选其中 3 个 zed 内部 crate（建议 `agent_ui`、`workspace`、`git_ui_core`），在 [sidebar.rs:L3-L81](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L3-L81) 的 use 区段为每个依赖找到至少一处真实使用点（形如上表第三列），写一份「依赖 → use 行 → 用途」对照表。

**需要观察的现象**：`cargo tree` 输出的第一层依赖应恰好是 Cargo.toml 中 `[dependencies]` 列出的那些（外部 crate 与内部 crate 混排，按字母序）。

**预期结果**：对照表能覆盖全部 zed 内部依赖，且每条 use 都能在后续代码中找到调用点。`cargo tree` 的具体输出**待本地验证**（本讲义编写环境未运行该命令）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 sidebar 的依赖里有 `editor`，却没有 `language`（编辑器的语法高亮依赖）？

答案：sidebar 只把 `Editor` 当单行输入框用（过滤搜索、行内重命名，见 [sidebar.rs:L806-L811](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L806-L811)），不需要语法能力；`language` 只出现在 dev-dependencies 且带 `test-support`（[Cargo.toml:L61](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/Cargo.toml#L61)），供测试环境使用。依赖最小化是 Zed workspace 的普遍风格。

**练习 2**：如果要在侧边栏里直接读写数据库里的线程记录，应该依赖哪个 crate？现在的代码是怎么避免这层依赖的？

答案：直接依赖应是数据库层（如 `db`）；当前代码避免了这个依赖——线程/终端的持久化元数据通过 `agent_ui` 的 `ThreadMetadataStore` 与 `TerminalThreadMetadataStore` 全局存储访问（[sidebar.rs:L8-L13](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L8-L13)），sidebar 只 observe 它们的变化（见 4.3.3），把数据访问留在 agent_ui 一侧。

### 4.3 Sidebar 实体的诞生：Sidebar::new

#### 4.3.1 概念说明

`Sidebar` 是一个 GPUI 实体（`Entity<Sidebar>`）：结构体保存状态，`Sidebar::new` 在创建实体的同时注册全部「事件 → 反应」 wiring。读懂构造函数，等于拿到这个 crate 的「接线图」。

`Sidebar::new` 的签名值得先看一眼（[sidebar.rs:L795-L799](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L795-L799)）：

```rust
pub fn new(
    multi_workspace: Entity<MultiWorkspace>,
    window: &mut Window,
    cx: &mut Context<Self>,
) -> Self
```

三个参数透露了它的定位：宿主是 `MultiWorkspace`（不是单个 `Workspace`），需要窗口（创建子编辑器），需要自身的 `Context`（注册订阅）。

#### 4.3.2 核心流程

`Sidebar::new` 按顺序做四件事：

```text
1. 焦点与特性开关接线
   ├── cx.focus_handle() + on_focus_in(focus_in)          # 焦点进入时清理选中态
   └── AgentThreadWorktreeLabelFlag::watch(cx)            # 特性开关变化可触发刷新
2. 创建两个子编辑器实体
   ├── filter_editor       # 顶部搜索框（占位文案 "Search threads…"）
   └── thread_rename_editor# 行内重命名输入框
3. 注册构造期订阅/观察
   ├── subscribe_in(multi_workspace)     # ActiveWorkspaceChanged / WorkspaceAdded / ...
   ├── subscribe(filter_editor)          # BufferEdited → 触发刷新
   ├── subscribe_in(thread_rename_editor)# 重命名编辑事件
   ├── observe(ThreadMetadataStore)      # 线程元数据全局存储
   └── observe(TerminalThreadMetadataStore)
4. defer_in：延迟到窗口就绪后
   ├── 对当前每个已存在的 workspace 调 subscribe_to_workspace
   └── schedule_update_entries(false)    # 第一次全量重建
```

之后返回 `Self { ... }`，把约 30 个字段填默认值（[sidebar.rs:L889-L923](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L889-L923)）。

#### 4.3.3 源码精读

**状态字段全景（选读）。** [sidebar.rs:L734-L792](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L734-L792) 定义了 `Sidebar` 的全部状态，几个代表性字段：

- `multi_workspace: WeakEntity<MultiWorkspace>`（[L735](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L735)）——持有**弱**引用，避免容器与面板互相强持有导致实体永不释放；
- `list_state: ListState`（[L740](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L740)）——gpui 虚拟列表的滚动/测量状态；
- `selection: Option<usize>`（[L742-L745](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L742-L745)）——键盘焦点下标，注释特别强调「不等于 active 条目」；
- `thread_last_accessed`（[L757-L761](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L757-L761)）——只由显式用户动作更新，用于切换器的 MRU 排序；
- `update_task: Option<Task<()>>`（[L782](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L782)）——挂起的刷新任务，用于合并重复刷新。

**焦点接线。** [sidebar.rs:L800-L802](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L800-L802) 创建焦点句柄并注册 `on_focus_in` 回调到 `Self::focus_in`（`.detach()` 让订阅独立于返回值存活）。

**搜索框与重命名框。** [sidebar.rs:L806-L811](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L806-L811) 创建两个单行 `Editor` 实体，搜索框带占位文案 `Search threads…`。

**宿主事件订阅。** [sidebar.rs:L813-L832](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L813-L832) 订阅 `MultiWorkspaceEvent`：活跃工作区变化时同步 active 条目并刷新；新增工作区时调用 `subscribe_to_workspace` 为它补订事件；工作区移除或分组变化时刷新。这就是「侧边栏跟随窗口内所有项目」的实现起点。

**搜索框事件。** [sidebar.rs:L834-L843](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L834-L843) 监听 `BufferEdited`：非空查询会先 `take()` 掉键盘选中态，再以 `select_first_after_update = true` 调度刷新。

**两个元数据存储的观察。** [sidebar.rs:L854-L865](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L854-L865) `cx.observe` 全局 `ThreadMetadataStore` 与 `TerminalThreadMetadataStore`——数据库里的线程标题、路径等任何变化都会触发一次列表刷新。这是 sidebar 与持久层的唯一耦合点。

**延迟初始化。** [sidebar.rs:L878-L887](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L878-L887) 先把宿主降级为 `WeakEntity`，再 `cx.defer_in(window, ...)`：等本轮更新结束、实体完全就绪后，遍历宿主**当前已有**的每个 workspace 逐个订阅，并做第一次 `schedule_update_entries(false)`。之所以延迟，是因为构造函数里 `Self` 尚未返回，此时去读自身实体容易造成重入借用；之所以用弱引用，是防止闭包意外延长宿主生命周期。

#### 4.3.4 代码实践

**实践目标**：把 `Sidebar::new` 的「事件源 → 回调 → 副作用」整理成一张接线表（这是读懂本 crate 的第一步，也是 u1-l3 讲义的预演）。

**操作步骤**：

1. 通读 [sidebar.rs:L795-L924](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L795-L924)。
2. 建一张三列表格：事件源（实体）| API（subscribe/observe/defer_in）| 副作用（调用了哪些方法）。
3. 单独标出哪些订阅在构造时注册、哪些推迟到 `defer_in` 中（提示：`subscribe_to_workspace` 在两处被调用——`WorkspaceAdded` 事件与 defer 块）。
4. （可选，本地练习）在 `defer_in` 闭包末尾临时加一行 `log::info!("sidebar deferred init");`，运行任一测试观察日志输出，确认延迟块被执行，然后**还原改动**。

**需要观察的现象**：构造期注册的订阅至少有 5 组（multi_workspace、filter_editor、thread_rename_editor、两个元数据存储）；`defer_in` 块会对每个已存在 workspace 各注册一次 `subscribe_to_workspace`。

**预期结果**：接线表能覆盖上述全部事件源。第 4 步的日志输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`Sidebar` 为什么持有 `WeakEntity<MultiWorkspace>` 而不是 `Entity<MultiWorkspace>`？

答案：`MultiWorkspace`（宿主）通过 `register_sidebar` 强持有侧边栏实体（见 4.4.3 的 `sidebar: Option<Box<dyn SidebarHandle>>` 字段）；若 sidebar 再强持有宿主，两者互相强引用，实体引用计数永不归零，窗口关闭后内存泄漏。弱引用 + 用时 `upgrade()`（如 [sidebar.rs:L930-L939](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L930-L939) 的 `is_group_collapsed`）是 GPUI 的标准做法。

**练习 2**：搜索框为什么在查询非空时要先 `this.selection.take()`？

答案：输入过滤后可见行集合立刻收窄，原先的键盘选中下标可能指向已被过滤掉的行甚至越界；先清空选中，再靠 `select_first_after_update = true` 在刷新完成后自动选中第一个匹配行（[sidebar.rs:L834-L843](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L834-L843)）。

**练习 3**：`Sidebar::new` 里创建的两个 `Editor` 实体，分别在什么交互场景下被用户看到？

答案：`filter_editor` 是常驻顶部的搜索框（占位文案 `Search threads…`）；`thread_rename_editor` 平时不可见，只在行内重命名某个线程时出现在那一行里（后续 u5-l4 讲义专门讲它的状态机）。

### 4.4 装配点：zed.rs 中的创建与 register_sidebar

#### 4.4.1 概念说明

到目前为止我们只知道 sidebar 是个库 crate；它变成屏幕上一条侧边栏，靠的是 `zed` crate（应用外壳）在合适的时机 `cx.new(|cx| Sidebar::new(...))`，再交给 `MultiWorkspace::register_sidebar` 挂载。

这里有一条重要的**契约边界**：`workspace` crate 定义了一个泛型 trait `Sidebar`（在 sidebar.rs 中以别名 `WorkspaceSidebar` 引入，[sidebar.rs:L68](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L68)），要求实现 `Focusable + Render + EventEmitter<SidebarEvent>`。也就是说：**workspace 不知道具体实现是谁**，zed 负责把实现「注射」进去。这样一来 workspace ↔ sidebar 互不依赖，装配只发生在最上层的 zed crate。

#### 4.4.2 核心流程

装配链路（每个窗口一次）：

```text
zed::initialize_workspace(app_state, cx)                    # zed.rs:430
  └── cx.observe_new(|multi_workspace: &mut MultiWorkspace, window, cx| ...)   # zed.rs:444
        └── cx.defer(move |cx| {                             # zed.rs:538，等实体就绪
              window_handle.update(cx, |_, window, cx| {
                let sidebar = cx.new(|cx| Sidebar::new(multi_workspace_handle, window, cx));  # zed.rs:542
                multi_workspace_handle.update(cx, |mw, cx| {
                  mw.register_sidebar(sidebar, cx);          # zed.rs:544
                });
              });
            })
```

`register_sidebar`（multi_workspace.rs）内部做三件事：observe 侧边栏（通知宿主重渲染）、订阅 `SidebarEvent::SerializeNeeded`（持久化时机）、把 `Entity<Sidebar>` 装箱为 `Box<dyn SidebarHandle>` 存进自身字段。

#### 4.4.3 源码精读

**观察每个新 MultiWorkspace。** [zed.rs:L444-L447](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/zed/src/zed.rs#L444-L447) 位于 `initialize_workspace`（[zed.rs:L430](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/zed/src/zed.rs#L430)）内：每当有新的 `MultiWorkspace` 实体创建（即新窗口），就执行装配；没有 window 的（headless 测试）直接返回。

**创建并注册。** [zed.rs:L538-L548](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/zed/src/zed.rs#L538-L548) 是本讲的落点：先 `cx.new` 创建 `Sidebar` 实体，再 `multi_workspace_handle.update` 调用 `register_sidebar`。注意整体包在 `cx.defer` 里——等 MultiWorkspace 自身的构造与订阅完成后再挂侧边栏。此外 [zed.rs:L79](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/zed/src/zed.rs#L79) 的 `use sidebar::Sidebar;` 正是 zed crate 依赖 sidebar 的体现（声明见 [crates/zed/Cargo.toml:L201](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/zed/Cargo.toml#L201)）。

**宿主侧的容器字段。** [multi_workspace.rs:L306-L324](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/workspace/src/multi_workspace.rs#L306-L324) 定义 `MultiWorkspace`，其中 [L317](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/workspace/src/multi_workspace.rs#L317) 的 `sidebar: Option<Box<dyn SidebarHandle>>` 是侧边栏的存放处——类型擦除的 trait 对象，workspace 侧只认接口。

**register_sidebar 本体。** [multi_workspace.rs:L393-L405](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/workspace/src/multi_workspace.rs#L393-L405)：

```rust
pub fn register_sidebar<T: Sidebar>(&mut self, sidebar: Entity<T>, cx: &mut Context<Self>) {
    self._subscriptions.push(cx.observe(&sidebar, |_this, _, cx| { cx.notify(); }));
    self._subscriptions.push(cx.subscribe(&sidebar, |this, _, event, cx| match event {
        SidebarEvent::SerializeNeeded => { this.serialize(cx); }
    }));
    self.sidebar = Some(Box::new(sidebar));
}
```

三行分别是：侧边栏任何变化 → 宿主重渲染；侧边栏发出 `SerializeNeeded` → 宿主把窗口状态写盘；装箱存储。订阅被 push 进 `_subscriptions`，随宿主存活。

**契约 trait。** [multi_workspace.rs:L122-L161](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/workspace/src/multi_workspace.rs#L122-L161) 定义 trait `Sidebar`：必备 `width`/`set_width`/`has_notifications`/`side`，可选 `prepare_for_focus`、`toggle_thread_switcher`、`cycle_project`、`cycle_thread`、`serialized_state`/`restore_serialized_state` 等（均带默认实现）。

**sidebar crate 侧的实现。** [sidebar.rs:L7677-L7750](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L7677-L7750) 是 `impl WorkspaceSidebar for Sidebar`：例如 `set_width` 会把宽度钳制在 `[MIN_WIDTH, MAX_WIDTH]`（[L7682-L7685](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L7682-L7685)），`serialized_state` 把宽度与当前视图打包成 JSON（[L7721-L7730](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L7721-L7730)）。紧随其后的 [L7752](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L7752) 声明 `EventEmitter<workspace::SidebarEvent>`、[L7754-L7758](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L7754-L7758) 实现 `Focusable`、[L7760](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L7760) 起 `Render`——三者共同满足契约 trait 的超 trait 要求。

**另一个装配点：调试动作。** [zed.rs:L1427](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/zed/src/zed.rs#L1427) 在 `register_actions`（[zed.rs:L912](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/zed/src/zed.rs#L912)）里把 `sidebar::dump_workspace_info` 注册为 workspace 动作——这是「函数式动作」注册的例子，也说明 zed crate 除了创建实体外还负责动作接线。

#### 4.4.4 代码实践

**实践目标**：亲手沿装配链路走一遍，确认「新窗口 → 侧边栏出现」这条路径上的每个函数。

**操作步骤**：

1. 打开 [zed.rs:L538-L548](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/zed/src/zed.rs#L538-L548)，从内向外跳转：`Sidebar::new` → `register_sidebar` → 外层的 `observe_new` → `initialize_workspace`。
2. 在仓库根目录执行 `git grep -n "sidebar::" crates/zed/src/zed.rs`，列出 zed crate 对 sidebar 的全部引用（应能看到 `use sidebar::Sidebar`、`cx.new(...)`、`register_action(sidebar::dump_workspace_info)` 等）。
3. 打开 [multi_workspace.rs:L393-L405](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/workspace/src/multi_workspace.rs#L393-L405)，确认 `register_sidebar` 存进 `self.sidebar` 的值类型是 `Box<dyn SidebarHandle>`。
4. 回答检验问题（见「预期结果」）。

**需要观察的现象**：zed.rs 中 sidebar 相关引用集中在少数几处；`register_sidebar` 的两笔订阅都进 `_subscriptions`。

**预期结果**：能不假思索地说出——**是谁**（zed crate 的 `observe_new(MultiWorkspace)` 回调）**在什么时机**（新窗口实体创建后、defer 到窗口就绪）**用什么**（`cx.new(Sidebar::new)` + `MultiWorkspace::register_sidebar`）把侧边栏挂进窗口。若步骤 2 的 grep 输出与本讲描述不符（例如行号漂移），以你本地代码为准并记录差异。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `register_sidebar` 的参数是泛型 `Entity<T: Sidebar>`，而 `MultiWorkspace` 的字段却是 `Box<dyn SidebarHandle>`？

答案：注册时编译期类型已知，用泛型零开销；存储时宿主只关心统一接口（宽度、焦点、序列化等），用 trait 对象擦除类型后放进一个 `Option` 字段即可，无需为每种侧边栏实现写专门字段。配套的 `impl<T: Sidebar> SidebarHandle for Entity<T>`（[multi_workspace.rs:L192](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/workspace/src/multi_workspace.rs#L192)）完成了从泛型到 trait 对象的桥接。

**练习 2**：`SidebarEvent::SerializeNeeded` 是谁发出、谁消费、干什么用？

答案：sidebar 在状态需要持久化时经 `cx.emit` 发出（`serialize` 方法，[sidebar.rs:L926-L928](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L926-L928)）；`register_sidebar` 注册的订阅消费它并调用 `MultiWorkspace::serialize` 写盘（[multi_workspace.rs:L398-L403](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/workspace/src/multi_workspace.rs#L398-L403)）。这让 sidebar 不必知道持久化细节。

**练习 3**：如果把 `Sidebar::new` 的调用从 `cx.defer` 中移出来直接执行，可能会遇到什么问题？

答案：`observe_new` 回调执行时 `MultiWorkspace` 自身仍在构造流程中，`window_handle.update` 与对宿主的二次 `update` 可能与进行中的借用冲突；defer 把装配推迟到当前更新周期结束后，保证宿主完全就绪。这正是 GPUI 中常见的「构造期不重入」模式（与 4.3 中 `Sidebar::new` 自己的 `defer_in` 同理）。

## 5. 综合实践

**任务**：为 sidebar crate 编写一份《依赖-用途对照表》并验证装配链路。

1. **依赖清单（命令验证）**：本机克隆 Zed 仓库后，在仓库根目录运行：

   ```bash
   cargo tree -p sidebar --depth 1
   ```

   抄下输出的全部直接依赖，与 [Cargo.toml:L17-L53](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/Cargo.toml#L17-L53) 逐项核对。（输出**待本地验证**。）

2. **使用点定位（源码阅读）**：对下列每个 zed 内部依赖——`agent_ui`、`agent`、`acp_thread`、`workspace`、`project`、`git`、`git_ui_core`、`ui`、`menu`、`zed_actions`、`editor`、`feature_flags`、`recent_projects`、`remote`、`action_log`——在 [sidebar.rs:L3-L81](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L3-L81) 的 use 区段找到对应 import，并顺着符号在文件后文找到至少一处真实调用，填入下表（示例已填两行）：

   | 依赖 | use 位置 | 真实使用点（函数/行为） | 一句话用途 |
   | --- | --- | --- | --- |
   | `workspace` | sidebar.rs:L65-L70 | `Sidebar::new` 参数 `Entity<MultiWorkspace>`（L796） | 多工作区宿主与契约 trait |
   | `menu` | sidebar.rs:L38-L40 | `render` 中 `.on_action(cx.listener(Self::confirm))`（L7784） | 通用列表键盘动作 |
   | `project` | … | … | … |

3. **装配链路复述**：合上讲义，用 5 句话以内写出「从打开一个新窗口，到侧边栏实体被挂载」的完整链路（提示：`initialize_workspace` → `observe_new` → `defer` → `cx.new` → `register_sidebar`），并标注每步的文件与行号。

4. **自检**：如果你能在不看答案的情况下回答 4.4.5 的三个练习，本讲目标即达成。

## 6. 本讲小结

- `sidebar` crate 是 Zed 的**多项目 Agent 线程/终端侧边栏**：列表范围覆盖窗口内所有项目分组，因此挂在 `MultiWorkspace` 而非单个 `Workspace` 上。
- 三个源文件分工明确：`sidebar.rs`（8208 行，库根+主实现）、`sidebar_tests.rs`（15111 行，仅测试编译）、`thread_switcher.rs`（421 行，自包含模态）。
- 架构核心约束写在 [sidebar.rs:L730-L733](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L730-L733)：每次变化都从当前世界状态**全量重推导**列表，禁止增量协调状态。
- 依赖方向 = 分层方向：sidebar 组合 `agent_ui`（面板与元数据存储）、`workspace`（容器与契约）、`project`/`git*`（事件与 git 服务）、`gpui`/`ui`（绘制），自身不做数据持久化与 git I/O。
- 装配发生在 zed crate：新窗口的 `MultiWorkspace` 触发 `observe_new` 回调，`defer` 后 `cx.new(Sidebar::new)` 并 `register_sidebar` 装箱为 `Box<dyn SidebarHandle>` 存进宿主。
- `Sidebar::new` 是全 crate 的接线图：构造期注册 5 组订阅/观察，`defer_in` 中补订已有工作区并做首次全量刷新。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：学习如何构建、运行与测试这个 crate（`cargo test -p sidebar`、`#[gpui::test]` 的形态），让代码真正跑起来。
- **随后（u1-l3）**：逐字段精读 `Sidebar` 结构体与 `Sidebar::new` 的全部订阅，把本讲 4.3 的接线表补全。
- **源码预读建议**：带着「全量重推导」这条约束去读 [sidebar.rs:L1974-L1993](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1974-L1993) 附近的 `schedule_update_entries` 与 `update_entries`，为第三单元的重建管线做铺垫。
- 如果对 GPUI 基础不熟，可先浏览仓库内 `crates/gpui` 的文档注释与 Zed 官方文档中关于 Entity/Context/Render 的章节。
