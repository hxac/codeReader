# markdown_preview 是什么：crate 定位、目录结构与构建

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `markdown_preview` 这个 crate 在 Zed 编辑器中的职责边界——它做什么、不做什么。
2. 说出 crate 内三个源码文件（`markdown_preview.rs`、`markdown_preview_settings.rs`、`markdown_preview_view.rs`）各自的作用与大小差异。
3. 看懂 `Cargo.toml` 中 `[lib] path` 的声明方式，并把 `[dependencies]` 里的每个依赖按「UI 框架层 / 编辑器层 / 平台层 / 其他工具」分组。
4. 独立编译这个 crate（`cargo build -p markdown_preview`），并知道在 Zed 源码里是哪一行把它挂载进应用启动流程的。
5. 在 Zed 中打开一个 Markdown 文件并触发预览面板。

## 2. 前置知识

本讲是整套手册的第一篇，不假设你读过 Zed 的其他部分，但需要一点基础概念：

- **crate**：Rust 的编译单元，粗略类比「一个独立库/包」。Zed 是一个巨大的 Cargo 工作区（workspace），根目录的 `Cargo.toml` 管理着上百个 crate，本讲的 `markdown_preview` 是其中之一。
- **Markdown**：一种用纯文本写格式文档的标记语言，比如 `# 标题`、`- 列表项`。预览面板就是把这份纯文本渲染成带样式的页面。
- **gpui**：Zed 自研的 UI 框架，提供窗口、元素树、实体（Entity）、异步任务等能力。你不需要现在就懂它，只要知道「画界面靠它」即可。
- **workspace（Zed 里的概念）**：Zed 中一个打开的项目窗口，管理标签页（item）、面板（pane）等。注意它和 Cargo 工作区是同名词，本文用「Cargo 工作区」和「Workspace crate」区分。
- **动作（Action）**：Zed 把用户操作抽象成 Action，键盘快捷键绑定到 Action 上，例如 `markdown::OpenPreview`。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [crates/markdown_preview/Cargo.toml](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/Cargo.toml#L1-L41) | 41 | crate 清单：声明 lib 根路径、feature 与依赖列表 |
| [crates/markdown_preview/src/markdown_preview.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview.rs#L1-L49) | 49 | lib 根：声明子模块、定义滚动类 Action、提供 `init(cx)` 入口 |
| [crates/markdown_preview/src/markdown_preview_settings.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_settings.rs#L1-L22) | 22 | 预览设置：`MarkdownPreviewSettings`（内容最大宽度） |
| [crates/markdown_preview/src/markdown_preview_view.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L1-L3733) | 3733 | 绝对主角：视图结构、打开链路、编辑器同步、渲染、搜索、持久化与测试 |
| [crates/zed/src/main.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/zed/src/main.rs#L781-L781) | — | Zed 应用入口，第 781 行调用 `markdown_preview::init(cx)` |
| [assets/keymaps/default-macos.json](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/assets/keymaps/default-macos.json#L640-L645) | — | 默认键位表，含 `markdown::OpenPreview` 的快捷键 |
| [assets/settings/default.json](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/assets/settings/default.json#L119-L128) | — | 默认设置，含 `markdown_preview` 设置块 |

一个直观的印象：**这个 crate 97% 的代码都在一个文件里**（3733 / 3804 行）。所以「读懂 markdown_preview」基本上等于「读懂 `markdown_preview_view.rs`」，后面十几篇讲义都会围绕它展开。

## 4. 核心概念与源码讲解

### 4.1 crate 定位：Zed 内置的 Markdown 预览面板

#### 4.1.1 概念说明

`markdown_preview` 实现的是 Zed 编辑器里「边写边看」的 Markdown 预览面板：你在编辑器里写 `.md` 文件，旁边一个标签页实时渲染出标题、列表、代码块、图片和复选框。

要理解它的职责边界，最好先说清楚它**不做什么**：

- 它**不做 Markdown 解析**。解析（把纯文本变成结构化节点树）由兄弟 crate `markdown` 完成，本 crate 只是持有解析结果的实体并订阅它。
- 它**不做通用面板框架**。标签页、分栏、恢复会话这些由 `workspace` crate 提供，本 crate 实现它要求的接口（`Item`、`SerializableItem` 等）。
- 它**不做代码编辑**。预览里点击复选框要写回源文件时，它调用 `editor` crate 的编辑 API。

它做的是「胶水 + 面板」：把编辑器里的 buffer 内容喂给 `markdown` 解析，把解析结果渲染成 UI，再把预览里的交互（点击链接、勾选复选框、滚动）翻译回编辑器动作。

谁在用它？除了 Zed 主程序，还有三个消费方（用 grep `markdown_preview::` 可验证）：

- [crates/zed/src/main.rs:781](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/zed/src/main.rs#L781-L781) —— 应用启动时调用 `init`；
- [crates/project_panel/src/project_panel.rs:32](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/project_panel/src/project_panel.rs#L32-L32) —— 项目面板右键「预览」入口；
- [crates/auto_update_ui/src/auto_update_ui.rs:12](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/auto_update_ui/src/auto_update_ui.rs#L12-L12) —— 更新说明弹窗复用了渲染好的预览视图（`MarkdownPreviewMode`、`MarkdownPreviewView`）。

#### 4.1.2 核心流程

从用户视角看，预览的生命周期是：

```text
Zed 启动
  └─ crates/zed/src/main.rs: main() → app.run(...) 闭包
       └─ markdown_preview::init(cx)              ← 本 crate 的挂载点
            ├─ 注册 MarkdownPreviewView 为可序列化条目（会话恢复用）
            └─ 观察每个新 Workspace，注册动作处理器
用户打开 .md 文件并按 cmd-shift-v (ctrl-shift-v)
  └─ 触发 markdown::OpenPreview 动作
       └─ 创建 MarkdownPreviewView，作为标签页加入 Pane
            ├─ 订阅编辑器事件 → 防抖后把全文喂给 markdown 解析
            ├─ 渲染：MarkdownElement 元素树
            └─ 交互：点击链接/复选框/滚动 → 回写编辑器
```

本讲只需要记住第一层（启动挂载）和最后一层的轮廓；中间的「订阅、防抖、渲染」是单元二的内容。

#### 4.1.3 源码精读

先看挂载点。`init(cx)` 是这个 crate 对外的唯一入口，整段只有 10 行：

[crates/markdown_preview/src/markdown_preview.rs:39-49](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview.rs#L39-L49) —— `init` 先把视图注册为可序列化条目（这样关闭重开 Zed 后预览标签能恢复），再用 `cx.observe_new` 监听每一个新创建的 `Workspace`，给它注册预览相关的动作处理器；`.detach()` 让这个观察任务常驻不被取消。

```rust
pub fn init(cx: &mut App) {
    workspace::register_serializable_item::<MarkdownPreviewView>(cx);

    cx.observe_new(|workspace: &mut Workspace, window, cx| {
        let Some(window) = window else {
            return;
        };
        markdown_preview_view::MarkdownPreviewView::register(workspace, window, cx);
    })
    .detach();
}
```

这 10 行在 Zed 主程序里被调用的位置（`app.run` 启动闭包中、一长串 `xxx::init(cx)` 之一）：

[crates/zed/src/main.rs:781-781](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/zed/src/main.rs#L781-L781) —— `fn main()` 里第 485 行的 `app.run(move |cx| {...}` 启动闭包内，`markdown_preview::init(cx);` 与 `vim::init`、`terminal_view::init` 等并排，这是 Zed「每个功能 crate 一个 init」约定的标准样式。

触发预览的快捷键定义在默认键位表中：

[assets/keymaps/default-macos.json:640-645](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/assets/keymaps/default-macos.json#L640-L645) —— 在「编辑器 + 文件扩展名为 md」这个上下文里，`cmd-shift-v` 打开预览、`cmd-k v` 在侧边打开预览（Linux/Windows 对应 `ctrl-shift-v` / `ctrl-k v`，见 [assets/keymaps/default-linux.json:605-606](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/assets/keymaps/default-linux.json#L605-L606)）。

```json
"context": "Editor && extension == md",
"use_key_equivalents": true,
"bindings": {
  "cmd-k v": "markdown::OpenPreviewToTheSide",
  "cmd-shift-v": "markdown::OpenPreview"
}
```

#### 4.1.4 代码实践

1. **实践目标**：亲手把预览跑起来，并确认 `init` 的挂载位置。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -n "markdown_preview::init" crates/zed/src/main.rs`，确认输出指向第 781 行。
   - 用 `grep -rn "markdown_preview::" crates --include=*.rs` 列出所有消费方（应为 main.rs、project_panel、auto_update_ui 和 vim 的测试辅助共 5 处左右）。
   - 若要在完整 Zed 中体验：按仓库根 README 构建并运行 Zed（`cargo run`，首次编译较久），打开任意 `.md` 文件，按 `ctrl-shift-v`（macOS 为 `cmd-shift-v`）。
3. **需要观察的现象**：编辑器旁边出现一个新的标签页，内容是渲染后的 Markdown；标题带 "Preview" 字样。
4. **预期结果**：预览能实时跟随输入刷新。grep 结果应与本讲 4.1.3 列出的消费方一致。
5. 完整构建 Zed 耗时较长，若只做源码学习可跳过运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `markdown_preview` 自己不实现 Markdown 解析，而是依赖 `markdown` crate？
**答案**：解析逻辑被多个场景复用（编辑器悬浮文档、聊天面板、更新说明、本预览），抽出独立 crate 避免重复实现；本 crate 专注于「把解析结果呈现为工作区标签页并与编辑器双向联动」这一层职责。

**练习 2**：`init(cx)` 里 `.detach()` 如果去掉会发生什么？
**答案**：`cx.observe_new` 返回一个订阅句柄/任务，函数返回时它被 drop，对新建 Workspace 的监听随之失效，后续窗口里将无法触发预览动作。`detach()` 让它脱离所有权约束、常驻生效。

**练习 3**：`init` 为什么用 `cx.observe_new` 而不是直接在当前窗口注册动作？
**答案**：Zed 支持多窗口，每个窗口各有自己的 `Workspace` 实体。`observe_new` 保证之后新建的每个 Workspace 都会被挂上动作处理器，而不仅限于 init 时已存在的那个。

### 4.2 三个源码文件的分工与 lib 根声明

#### 4.2.1 概念说明

这个 crate 只有两个子模块，加上 lib 根共三个文件：

- `markdown_preview.rs`（lib 根，49 行）：声明模块、定义 Action、暴露 `init`。
- `markdown_preview_settings.rs`（22 行）：预览的设置项，目前只有「内容最大宽度」。
- `markdown_preview_view.rs`（3733 行）：视图本体，含约一半篇幅的集成测试。

值得注意的一点：Zed 的编码规范（见仓库 CLAUDE.md）要求不用 `mod.rs`，并且新建 crate 时用 `[lib] path` 指定一个与 crate 同名的文件作为 lib 根，而不是默认的 `src/lib.rs`。这个 crate 就是标准示例：lib 根是 `src/markdown_preview.rs`。

#### 4.2.2 核心流程

lib 根的组织逻辑：

```text
markdown_preview.rs (lib 根)
  ├─ pub mod markdown_preview_settings   ← 设置子模块
  ├─ pub mod markdown_preview_view       ← 视图子模块
  ├─ pub use zed_actions::preview::markdown::{OpenPreview, ...}  ← 动作从 zed_actions 重导出
  ├─ actions!(markdown, [ ScrollPageUp, ..., CloseAndReturnToEditor ])  ← 本地定义的滚动/开关动作
  └─ pub fn init(cx)                     ← 启动入口
```

#### 4.2.3 源码精读

lib 根的模块声明与重导出：

[crates/markdown_preview/src/markdown_preview.rs:4-9](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview.rs#L4-L9) —— 声明两个公开子模块；第 7 行把两个「打开预览」动作从 `zed_actions` crate 重导出。这样分离的原因是：动作类型需要在别的 crate（如 `zed_actions` 汇总处、project_panel）被引用，定义在公共动作 crate 里可以避免循环依赖；`markdown_preview` 再 re-export 一份，外部 `use markdown_preview::OpenPreview` 也能通过。

```rust
pub mod markdown_preview_settings;
pub mod markdown_preview_view;

pub use zed_actions::preview::markdown::{OpenPreview, OpenPreviewToTheSide};
```

本地定义的动作族（滚动 + 开关）：

[crates/markdown_preview/src/markdown_preview.rs:11-37](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview.rs#L11-L37) —— `actions!` 宏在 `markdown` 命名空间下定义 10 个动作：8 个滚动类（页滚、行滚、按元素滚、滚到顶/底，其中页滚两个带 `deprecated_aliases` 兼容旧名）、1 个 `OpenFollowingPreview`（跟随模式预览）、1 个 `CloseAndReturnToEditor`（关闭预览并把焦点还给编辑器）。宏里的 doc 注释会展示给用户。

```rust
actions!(
    markdown,
    [
        /// Scrolls up by one page in the markdown preview.
        #[action(deprecated_aliases = ["markdown::MovePageUp"])]
        ScrollPageUp,
        // ... ScrollPageDown / ScrollUp / ScrollDown / ScrollUpByItem / ScrollDownByItem
        // ... ScrollToTop / ScrollToBottom / OpenFollowingPreview / CloseAndReturnToEditor
    ]
);
```

设置模块的全部内容：

[crates/markdown_preview/src/markdown_preview_settings.rs:5-22](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_settings.rs#L5-L22) —— `MarkdownPreviewSettings` 只有一个字段 `max_width: Option<Pixels>`。`from_settings` 把 `settings.json` 里的原始 JSON 转成强类型：当 `limit_content_width` 为 `false`（或未设即默认 `true`）时把 `max_width` 置为 `None`，表示内容通栏渲染。`RegisterSetting` derive 会自动把该设置注册进全局设置注册表。

```rust
#[derive(Clone, Copy, Debug, Default, RegisterSetting)]
pub struct MarkdownPreviewSettings {
    pub max_width: Option<Pixels>,
}

impl Settings for MarkdownPreviewSettings {
    fn from_settings(content: &settings::SettingsContent) -> Self {
        let content = content.markdown_preview.clone().unwrap_or_default();
        let max_width = if content.limit_content_width.unwrap_or(true) {
            content.max_width.map(IntoGpui::into_gpui)
        } else {
            None
        };
        Self { max_width }
    }
}
```

与之对应的默认值在 [assets/settings/default.json:119-128](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/assets/settings/default.json#L119-L128) —— `limit_content_width: true`、`max_width: 800`，即默认把预览内容约束在 800 像素宽并水平居中。

视图模块的规模与开头（先睹为快，细节留给单元二）：

[crates/markdown_preview/src/markdown_preview_view.rs:56-74](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L56-L74) —— 文件开头是约 55 行 `use` 导入（横跨 editor/gpui/markdown/project/theme/ui/workspace 等十余个 crate，这是「胶水层」最直观的证据），然后是防抖常量 `REPARSE_DEBOUNCE = 200ms` 和核心结构体 `MarkdownPreviewView`，其字段包括 workspace 弱引用、活动编辑器状态、markdown 实体、滚动句柄、图片缓存、悬停 URL、模式（Default/Follow）等。

```rust
const REPARSE_DEBOUNCE: Duration = Duration::from_millis(200);

pub struct MarkdownPreviewView {
    workspace: WeakEntity<Workspace>,
    active_editor: Option<EditorState>,
    focus_handle: FocusHandle,
    markdown: Entity<Markdown>,
    // ... scroll_handle / image_cache / base_directory / pending_update_task
    // ... hovered_url / mode / markdown_parse_pending
}
```

[crates/markdown_preview/src/markdown_preview_view.rs:76-82](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L76-L82) —— 两种预览模式的定义：`Default` 固定绑定创建时的那个编辑器；`Follow` 跟随当前活动编辑器切换内容。这个枚举还带 `to_db`/`from_db` 方法用于 SQLite 持久化（第 84-98 行）。

#### 4.2.4 代码实践

1. **实践目标**：用行数统计和阅读建立对三个文件的「体感」，并理解 lib 根的非默认声明。
2. **操作步骤**：
   - 执行 `wc -l crates/markdown_preview/src/*.rs`，核对三个文件约为 49 / 22 / 3733 行。
   - 打开 `Cargo.toml`，找到 `[lib]` 段（第 11-12 行），确认 `path = "src/markdown_preview.rs"`。
   - 在 `markdown_preview_view.rs` 里搜索 `mod tests`，跳到测试区起点，感受「生产代码 + 测试」在同一文件的分布。
3. **需要观察的现象**：`markdown_preview_view.rs` 中 `mod tests` 之前约有 1800+ 行生产代码、之后是测试。
4. **预期结果**：能口头说出「改设置去 `markdown_preview_settings.rs`，改行为去 `markdown_preview_view.rs`，加动作去 lib 根」。
5. `mod tests` 的精确分界行号请以本地搜索结果为准（待确认）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `OpenPreview` 定义在 `zed_actions` 而不是本 crate？
**答案**：动作类型要被 `zed_actions` 汇总供键位系统、project_panel、vim 等引用；若定义在本 crate，这些消费方就得依赖 `markdown_preview`，容易形成不合理的依赖方向甚至循环依赖。本 crate 通过 `pub use` 重导出保持使用便利。

**练习 2**：`MarkdownPreviewSettings` 为什么把 `limit_content_width` 和 `max_width` 两个 JSON 字段折叠成一个 `Option<Pixels>`？
**答案**：消费方（渲染层）只关心「有没有宽度限制」这一个事实。把布尔与数值的组合在设置解析层就归一化成 `Option`，下游不需要再写 `if limit && let Some(w)` 的双重判断，减少分支与出错面。

**练习 3**：如果让你给预览加一个「行高倍数」设置，要改哪些文件？
**答案**：在 `assets/settings/default.json` 的 `markdown_preview` 块加默认值；在 `markdown_preview_settings.rs` 的结构体和 `from_settings` 中加字段与解析；再在 `markdown_preview_view.rs` 渲染处消费它（设置的具体解析细节在 u1-l3 详讲）。

### 4.3 Cargo.toml 依赖清单：UI 框架层、编辑器层与平台层

#### 4.3.1 概念说明

`cargo build -p markdown_preview` 能否独立编译？能——它是 Cargo 工作区里的普通成员，只依赖兄弟 crate 的库代码。根 [Cargo.toml:401](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/Cargo.toml#L401-L401) 用 `markdown_preview = { path = "crates/markdown_preview" }` 把它登记为 workspace 依赖，因此各 crate 引用它时统一写 `markdown_preview.workspace = true`。

把这 16 个运行时依赖按角色分组，是理解整个 crate 架构最快的方式：

| 分层 | 依赖 | 在本 crate 里的用途 |
| --- | --- | --- |
| **UI 框架层** | `gpui`、`ui`、`theme`、`theme_settings` | 窗口/实体/元素树（gpui）、按钮与滚动条等组件（ui）、主题与主题中的字体设置 |
| **解析层** | `markdown` | Markdown 解析与 `MarkdownElement` 渲染元素，本 crate 的内容来源 |
| **编辑器层** | `editor`、`language` | 源编辑器实体与编辑 API、buffer 与语言注册表 |
| **平台层（workspace）** | `workspace`、`project`、`db`、`zed_actions` | 标签页/面板/序列化接口（workspace）、文件与 worktree（project）、SQLite 持久化（db）、公共动作定义（zed_actions） |
| **工具层** | `anyhow`、`log`、`settings`、`urlencoding`、`util` | 错误处理、日志、设置框架、URL 编码（图片/链接路径）、通用工具（相对路径、带行列号路径等） |

#### 4.3.2 核心流程

依赖分层可以用一张「洋葱图」表达，本 crate 位于最外圈，把内圈能力组合起来：

```text
            ┌──────────────────────────────────────────┐
            │  markdown_preview（本 crate，胶水+面板）   │
            │  ┌─────────┐ ┌─────────┐ ┌────────────┐  │
            │  │ 编辑器层 │ │ 解析层   │ │ 工具层      │  │
            │  │ editor   │ │ markdown│ │ anyhow/log │  │
            │  │ language │ │         │ │ settings…  │  │
            │  └────┬────┘ └────┬────┘ └────────────┘  │
            │       ┌──────────┴───────────┐           │
            │       │ 平台层：workspace     │           │
            │       │ project / db          │           │
            │       └──────────┬───────────┘           │
            │       ┌──────────┴───────────┐           │
            │       │ UI 框架层：gpui       │           │
            │       │ ui / theme            │           │
            │       └──────────────────────┘           │
            └──────────────────────────────────────────┘
```

分层不是 Cargo 强制的，只是阅读约定：**越靠内越通用，越靠外越贴近用户**。数据流则是横向的——编辑器层产出文本，解析层产出结构，UI 框架层画出来，平台层负责把它挂进窗口和会话。

#### 4.3.3 源码精读

lib 根的非默认声明（对应 CLAUDE.md 的规范）：

[crates/markdown_preview/Cargo.toml:11-15](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/Cargo.toml#L11-L15) —— `[lib] path = "src/markdown_preview.rs"` 指定与 crate 同名的文件作为库根（而非默认 `src/lib.rs`）；`test-support` feature 仅为测试开启额外入口，生产构建不带。

```toml
[lib]
path = "src/markdown_preview.rs"

[features]
test-support = []
```

完整的运行时依赖表：

[crates/markdown_preview/Cargo.toml:17-33](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/Cargo.toml#L17-L33) —— 16 个依赖全部通过 `workspace = true` 继承工作区统一版本，逐行对照 4.3.1 的表格即可画出依赖草图。

```toml
[dependencies]
anyhow.workspace = true
db.workspace = true
editor.workspace = true
gpui.workspace = true
language.workspace = true
log.workspace = true
markdown.workspace = true
project.workspace = true
settings.workspace = true
theme.workspace = true
theme_settings.workspace = true
ui.workspace = true
urlencoding.workspace = true
util.workspace = true
workspace.workspace = true
zed_actions.workspace = true
```

开发依赖（只有跑测试才编译）：

[crates/markdown_preview/Cargo.toml:35-41](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/Cargo.toml#L35-L41) —— `fs`（临时目录造测试文件）、`serde_json`、`buffer_diff`，以及为 `editor`/`gpui`/`workspace` 打开 `test-support` feature。这解释了为什么 `cargo build -p markdown_preview` 比 `cargo test -p markdown_preview` 依赖面小得多。

在工作区中的登记：

[Cargo.toml:401-401](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/Cargo.toml#L401-L401)（仓库根）—— `markdown_preview = { path = "crates/markdown_preview" }`，同时根 Cargo.toml 第 135 行把它列进 workspace members。

#### 4.3.4 代码实践

1. **实践目标**：验证独立可编译，并亲手产出一份依赖分组草图。
2. **操作步骤**：
   - 在仓库根目录执行 `cargo build -p markdown_preview`（首次会编译依赖，耗时较长）。
   - 打开 `crates/markdown_preview/Cargo.toml` 的 `[dependencies]`，按 4.3.1 表格给每一行标注层级（可抄进自己的笔记）。
   - 执行 `cargo tree -p markdown_preview --depth 1`，把输出与你的草图对照。
   - 用 `grep -n "markdown_preview" crates/*/Cargo.toml` 找出所有声明依赖本 crate 的 crate（应为 zed、vim、auto_update_ui、project_panel 四个）。
3. **需要观察的现象**：`cargo build` 最终输出 `Compiling markdown_preview vX.Y.Z` 且无错误；`cargo tree --depth 1` 列出的直接依赖与 Cargo.toml 一一对应。
4. **预期结果**：得到一张五层依赖草图，并能回答「为什么 `db` 是平台层——因为预览标签的会话持久化直接用它写 SQLite」。
5. 本讲写作环境未实际执行 `cargo build`（编译耗时），请在本地验证（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：所有依赖都写 `xxx.workspace = true` 而不是带版本号，这样做的好处是什么？
**答案**：版本与来源由根 Cargo.toml 的 `[workspace.dependencies]` 统一管理，整个仓库只有一处版本号，避免不同 crate 间版本漂移；路径依赖也只需声明一次。

**练习 2**：`editor`、`gpui`、`workspace` 同时出现在 `[dependencies]` 和 `[dev-dependencies]`（后者带 `test-support` feature），为什么不冲突？
**答案**：同一个 crate 的 dev-dependencies 会与其普通 dependencies 合并，feature 取并集。测试构建时 `test-support` 生效，普通构建时不受影响——这是 Zed 给 crate 挂测试钩子的标准做法。

**练习 3**：如果预览想支持「导出 HTML」功能，可能需要新增哪类依赖？
**答案**：视实现而定——渲染仍可复用 `markdown`/`gpui` 既有产物；只有当需要新的序列化或文件写出能力时才考虑新增依赖，且应优先看 `util`/`project` 是否已提供。这个练习没有标准答案，目的是养成「先查兄弟 crate 再引新依赖」的习惯。

## 5. 综合实践

把本讲三块知识串成一张「crate 身份卡」，完成以下步骤并记录结果：

1. **跑通编译**：仓库根目录执行 `cargo build -p markdown_preview`，记录编译耗时。
2. **画依赖草图**：对照 `Cargo.toml` 第 17-33 行与 `cargo tree -p markdown_preview --depth 1` 的输出，画出 4.3.2 的洋葱图，并在每层写下「本 crate 用它做什么」的一句话（提示：可从 `markdown_preview_view.rs` 开头第 1-54 行的 `use` 语句反查用途，例如第 21 行 `use markdown::{...}` 对应解析层、第 40-46 行 `use workspace::...` 对应平台层）。
3. **定位挂载点**：用 `grep -n "markdown_preview::init" crates/zed/src/main.rs` 确认第 781 行，读它上下文 20 行，感受「一 crate 一 init」的启动样式。
4. **触发预览**（可选，需完整构建 Zed）：打开 `.md` 文件按 `ctrl-shift-v`，确认预览标签出现。
5. **产出**：一张包含「文件分工表（3 个文件）+ 依赖分层图（5 层）+ 挂载点行号」的笔记，这是后续所有讲义的检索底图。

## 6. 本讲小结

- `markdown_preview` 是 Zed 内置的 Markdown 预览面板 crate，定位是「胶水 + 面板」：解析交给 `markdown`，标签页/会话交给 `workspace`，编辑回写交给 `editor`。
- crate 只有两个子模块 + 一个 lib 根，97% 代码集中在 `markdown_preview_view.rs`（3733 行，约一半是测试）。
- lib 根用 `[lib] path = "src/markdown_preview.rs"` 声明（非默认的 `src/lib.rs`），这是 Zed 的编码规范；`init(cx)` 是唯一对外入口，做「注册可序列化条目 + 观察新 Workspace」两件事。
- 设置极简：`MarkdownPreviewSettings` 只有 `max_width: Option<Pixels>`，由 `limit_content_width` + `max_width` 两个 JSON 字段归一化而来，默认 800px 居中。
- 16 个运行时依赖可分五层：UI 框架层（gpui/ui/theme）、解析层（markdown）、编辑器层（editor/language）、平台层（workspace/project/db/zed_actions）、工具层（anyhow/log/settings/urlencoding/util）。
- 消费方有四个：zed 主程序（init）、project_panel（右键预览）、auto_update_ui（更新弹窗复用视图）、vim（测试辅助）。

## 7. 下一步学习建议

下一篇 **u1-l2《入口与动作注册：init、actions! 宏与快捷键》**将深入本讲留下的两个钩子：`init` 里 `register_serializable_item` 与 `MarkdownPreviewView::register` 的完整动作注册流程，以及 `actions!` 宏定义的 10 个动作如何被键位系统匹配。

在进入下一篇前，建议先自己浏览：

- [crates/markdown_preview/src/markdown_preview.rs:117](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L117-L117) 起的 `MarkdownPreviewView::register`——看它注册了哪些 `register_action`；
- [assets/keymaps/default-macos.json:1432-1445](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/assets/keymaps/default-macos.json#L1432-L1445) 的 `MarkdownPreview` 上下文——预览聚焦时的全部默认键位。
