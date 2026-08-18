# editor crate 是什么：定位、构建与模块全景

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `editor` crate 在 Zed 整体架构中的位置：它是所有文本输入元素和完整代码编辑器的实现，仓库中有 53 个 crate 直接依赖它（本讲用 grep 实测过，包括 `vim`、`search`、`terminal_view`、主程序 `zed` 等）。
- 看懂 `Cargo.toml` 中 `[lib] path = "src/editor.rs"` 与 `[features] test-support` 这两段各自的作用。
- 对照 `src/editor.rs` 顶部约 60 行的模块声明（`mod xxx;`）和 `pub use` 导出面，在脑子里建立起这个 70 个源文件、数万行代码的 crate 的"全景地图"。
- 完成一次代码实践：把全部子模块按职责分类，并用 `cargo build -p editor` 验证这个 crate 可以独立编译。

## 2. 前置知识

本讲是整套手册的第一讲，不假设你了解 Zed 内部实现，但需要一点 Rust 基础概念。不熟悉的的话，先看下面的通俗解释：

- **crate**：Rust 的编译单元，类似"一个独立的库或程序"。一个 crate 由一个 `Cargo.toml` 描述，里面声明名字、依赖和编译开关。
- **workspace**：多个 crate 放在一起统一管理。Zed 的仓库根目录有一个总的 `Cargo.toml`，`crates/` 目录下每个子目录是一个 crate。依赖写作 `gpui.workspace = true`，意思是"版本和来源以 workspace 根的定义为准"。
- **库根（lib root）**：crate 对外编译成库时，Rust 默认从 `src/lib.rs` 开始读代码。但可以在 `Cargo.toml` 里用 `[lib] path = "..."` 改成别的文件——Zed 的约定是改成一个描述性名字，所以本 crate 的库根是 `src/editor.rs` 而不是 `src/lib.rs`。
- **feature**：编译期开关。`[features]` 段定义的 feature 打开后，会激活对应的可选依赖和代码里 `#[cfg(feature = "...")]` 标注的部分。本 crate 只有一个 feature：`test-support`。
- **模块声明**：Rust 中 `mod foo;` 表示"存在一个子模块 foo，它的代码在 `src/foo.rs`（或 `src/foo/` 目录下的文件）"。库根文件里的一串 `mod` 声明，就构成了这个 crate 的"目录页"。
- **GPUI**：Zed 自研的 UI 框架，同时提供状态管理和并发原语。本讲只需要知道"编辑器是画在 GPUI 上的"即可，细节后面几讲再说。

另外说明阅读方式：本讲义引用源码时都会给出 GitHub 永久链接（固定到当前 HEAD `a7d74150`），点击可以直接跳到对应行。所有行号都已对照源码核实。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `crates/editor/Cargo.toml` | 141 | crate 的"身份证与配货单"：名字、库根路径、feature 开关、全部依赖 |
| `crates/editor/src/editor.rs` | 12624 | 库根文件：crate 级文档、约 52 个子模块声明、`pub use` 导出面，以及 `Editor`（第 921 行起）等核心类型的定义 |

为了建立规模感，再看三个只在本讲"路过"的大文件（后续单元会深入）：

| 文件 | 行数 | 作用（一句话） |
| --- | --- | --- |
| `crates/editor/src/element.rs` | 12523 | 所有渲染发生的地方 |
| `crates/editor/src/display_map.rs` | 4364 | 把文本切分成逻辑块、建立坐标映射 |
| `crates/editor/src/editor_tests.rs` | 43366 | 测试文件（比两个大源文件加起来还长） |

整个 `src/` 目录共有 70 个 `.rs` 文件，其中一部分是 `display_map/`、`element/`、`git/`、`inlays/`、`scroll/`、`test/` 这类子目录里的文件。

## 4. 核心概念与源码讲解

本讲的两个最小模块：**Cargo.toml 的 `[lib]` 与 `[features]` 段**、**editor.rs 顶部模块声明与 `pub use` 导出面**。

### 4.1 Cargo.toml：editor crate 的身份证与配货单

#### 4.1.1 概念说明

打开任何 Rust crate，第一件事应该读 `Cargo.toml`。它回答三个问题：

1. 这个 crate 叫什么、以什么方式被编译？（`[package]` 和 `[lib]` 段）
2. 它依赖谁？（`[dependencies]` 段）
3. 它有哪些编译期开关？（`[features]` 段）

对 `editor` 来说，这三段信息尤其重要，因为它的依赖列表直接暴露了它的"社交关系"：它既依赖 `gpui`（UI 框架）、`text`/`rope`/`multi_buffer`（文本数据结构），又依赖 `language`/`lsp`/`project`（语言智能），还依赖 `git`/`buffer_diff`（版本控制集成）——这正是后面单元划分的伏笔。

#### 4.1.2 核心流程

`cargo build -p editor` 时发生的事情：

1. cargo 读取 workspace 根 `Cargo.toml`，解析 `editor` 的依赖版本（全部来自 workspace 统一配置）。
2. 根据 `[lib] path = "src/editor.rs"` 找到库根，从它开始递归编译所有 `mod` 声明的模块。
3. 默认情况下 `test-support` feature 是关闭的，可选依赖（`proptest`、`tree-sitter-*` 等）不会被编译。
4. 当某个下游 crate 运行测试时，它的 dev-dependencies 会以 `features = ["test-support"]` 的方式依赖 `editor`，于是这里的 `test-support` feature 被打开，连带激活其声明的所有可选依赖。

#### 4.1.3 源码精读

**库根声明**：

```toml
[lib]
path = "src/editor.rs"
doctest = false
```

这段代码声明：库根是 [Cargo.toml:L11-L13](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/Cargo.toml#L11-L13)，即整个 crate 从 `src/editor.rs` 开始编译；同时关闭 doctest——`cargo test` 不会把文档注释里的代码块当作测试来编译运行，这是 Zed 仓库里 crate 的常见配置。用描述性文件名代替 `lib.rs` 也是 Zed 的明确约定（见仓库 `CLAUDE.md` 中"新建 crate 时优先用 `[lib] path` 指定库根"的要求），好处是文件名即模块名，编辑器标签页里一眼可辨。

**唯一的 feature：test-support**：

```toml
[features]
test-support = [
    "text/test-support",
    "language/test-support",
    "gpui/test-support",
    "multi_buffer/test-support",
    "project/test-support",
    "theme/test-support",
    "util/test-support",
    "workspace/test-support",
    "tree-sitter-c",
    "tree-sitter-rust",
    "tree-sitter-typescript",
    "tree-sitter-html",
    "proptest",
    "unindent",
]
```

这是 [Cargo.toml:L15-L31](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/Cargo.toml#L15-L31)。`"text/test-support"` 这种写法表示"打开本 crate 的 test-support 时，同时打开依赖 `text` 的 test-support feature"——测试工具是层层向下开启的。而 `tree-sitter-c`、`tree-sitter-rust` 等是可选依赖（在依赖表里标了 `optional = true`）：测试折叠、语法高亮这类功能需要真实的语法解析器，所以只在测试构建时引入，正常发行版不背这个包袱。

**依赖表（节选关键行）**，见 [Cargo.toml:L33-L107](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/Cargo.toml#L33-L107)。按用途分组理解：

| 分组 | 代表依赖（所在行） | 用途 |
| --- | --- | --- |
| UI 与基础 | `gpui`（L54）、`ui`（L98）、`theme`（L86） | 界面绘制与主题 |
| 文本数据 | `text`（L84）、`rope`（L74）、`multi_buffer`（L64）、`sum_tree`（L81） | 文本存储、多缓冲、区间树 |
| 语言智能 | `language`（L58）、`lsp`（L61）、`project`（L68）、`snippet`（L80） | 语法、语言服务器、工程数据 |
| git 与 diff | `git`（L53）、`buffer_diff`（L45） | blame、hunk 对比 |
| 集成 | `workspace`（L105）、`settings`（L78）、`db`（L44） | 工作区条目、设置、持久化 |

**开发依赖**：[Cargo.toml:L109-L141](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/Cargo.toml#L109-L141)。注意这里 `gpui`、`project` 等再次出现但带上了 `features = ["test-support"]`，还额外引入了更多语法树（`tree-sitter-go`、`tree-sitter-md` 等）——测试比运行时需要更丰富的语言环境。

#### 4.1.4 代码实践

**实践目标**：验证 `editor` 可以脱离主程序单独编译，并对它的编译体量建立直觉。

**操作步骤**：

1. 在仓库根目录执行 `cargo build -p editor`（`-p` 指定只构建这个包）。
2. 用 `time cargo build -p editor` 记录耗时；第二次执行会命中缓存几乎瞬间完成，可以体会增量构建。
3. 想更快的话用 `cargo check -p editor`（只做类型检查不生成产物）。
4. （选做）用 `cargo tree -p editor --depth 1` 观察它的直接依赖列表，与 4.1.3 的分组表对照。

**需要观察的现象**：命令能成功结束，说明 `editor` 不依赖 `zed` 主程序——依赖方向是主程序依赖它，而不是反过来。

**预期结果**：编译成功，无错误输出。具体耗时取决于机器，**待本地验证**（本讲义编写时未在此环境执行完整构建）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `test-support` feature 里要列出 `tree-sitter-c`、`tree-sitter-rust` 这些语法解析器？

参考答案：`editor` 的很多功能（折叠范围计算、语义高亮、自动缩进等）依赖真实语法树才能测试。这些解析器体积不小且只在测试中需要，所以声明为可选依赖、由 `test-support` feature 统一开启，发行版构建不会包含它们。

**练习 2**：`[dependencies]` 和 `[dev-dependencies]` 里都出现了 `project`，两者有什么区别？

参考答案：`[dependencies]` 里的 `project.workspace = true`（L68）是运行时依赖，正式构建就会编译；`[dev-dependencies]` 里的 `project = { workspace = true, features = ["test-support"] }`（L117）只在编译测试/示例时生效，并且额外打开了它的 test-support feature，让测试能拿到假项目、假语言服务器等工具。

**练习 3**：如果把 `[lib] path = "src/editor.rs"` 删掉，会发生什么？

参考答案：cargo 会去找默认的 `src/lib.rs`，而这个文件不存在，构建直接报错。这也反向说明了"库根文件名是约定出来的"——改成 `src/editor.rs` 只是让文件名更有辨识度。

### 4.2 src/editor.rs：库根、crate 文档与模块全景

#### 4.2.1 概念说明

`src/editor.rs` 是这个 crate 的库根，但它远不止一个"目录页"：

- 顶部约 12 行是 **crate 级文档注释**（`//!` 开头），是官方对这个 crate 最权威的一段自述；
- 接下来是 **52 个子模块声明**，构成整个 crate 的模块树；
- 然后是一大片 **`pub use` 导出面**——决定外部 crate 能看到哪些类型，是理解"editor 向外界提供什么服务"的直接材料；
- 文件后半部分（第 521 行的 `EditorStyle`、第 921 行的 `Editor` 结构体等）才是核心类型本身的定义，那属于下一单元的内容。

读一个大 crate 的库根文件，是快速建立全局认知的最高性价比做法。

#### 4.2.2 核心流程

Rust 的模块查找规则（结合 Zed 的约定）：

1. 库根里写 `mod foo;`，编译器去找 `src/foo.rs`。
2. `src/foo.rs` 里还可以继续声明 `mod bar;`，对应 `src/foo/bar.rs`。Zed 的 `CLAUDE.md` 明确禁止 `mod.rs` 文件，所以你会看到 `display_map.rs` 与 `display_map/` 目录并存的形态：`display_map.rs` 是模块本体，`display_map/tab_map.rs` 是它的子模块。
3. 可见性三档：`pub mod`（外部可用）、`mod`（仅 crate 内部可用）、`#[cfg(test)] mod`（只在测试构建中存在）。

#### 4.2.3 源码精读

**crate 自述文档**，见 [src/editor.rs:L2-L13](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L2-L13)：

```rust
//! This is the place where everything editor-related is stored (data-wise) and displayed (ui-wise).
//! The main point of interest in this crate is [`Editor`] type, which is used in every other Zed part as a user input element.
//! It comes in different flavors: single line, multiline and a fixed height one.
//!
//! Editor contains of multiple large submodules:
//! * [`element`] — the place where all rendering happens
//! * [`display_map`] - chunks up text in the editor into the logical blocks, establishes coordinates and mapping between each of them.
//!
//! If you're looking to improve Vim mode, you should check out Vim crate that wraps Editor and overrides its behavior.
```

这段话信息量很大，翻译并提炼如下：

- `Editor` 类型是整个 crate 的主角，**Zed 里所有需要用户输入文本的地方都在用它**——不只 是代码编辑器，还包括单行输入框、多行输入框、固定高度输入框三种形态。
- 官方点名两个最大的子模块：`element`（渲染）和 `display_map`（把文本切块并建立坐标映射）——这正是本手册第 3、5 单元的主角。
- 最后一句直接指路：想做 Vim 模式，去看包装了 `Editor` 的 `vim` crate。这透露了 `editor` 的定位——它被设计成"可被包装、可被复用"的底座。

**第一段模块声明**，见 [src/editor.rs:L14-L47](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L14-L47)：

```rust
pub mod actions;
pub mod blink_manager;
mod bracket_colorization;
mod clangd_ext;
pub mod code_context_menus;
mod code_lens;
pub mod display_map;
mod document_colors;
mod document_links;
mod document_symbols;
mod editor_settings;
mod element;
mod fold;
mod folding_ranges;
mod git;
mod highlight_matching_bracket;
pub mod hover_links;
pub mod hover_popover;
mod indent_guides;
mod inlays;
pub mod items;
mod jsx_tag_auto_close;
mod linked_editing_ranges;
mod lsp_ext;
mod mouse_context_menu;
pub mod movement;
mod persistence;
mod runnables;
mod rust_analyzer_ext;
pub mod scroll;
mod selections_collection;
pub mod semantic_tokens;
mod split;
pub mod split_editor_view;
```

注意哪些是 `pub`：`movement`、`scroll`、`display_map`、`items` 等是对外公开的（`vim` crate 大量复用 `movement`），而 `element`、`git` 反而是私有的——它们只通过下一层 `pub use` 选择性导出。

**测试模块与内部模块**，见 [src/editor.rs:L49-L72](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L49-L72)：

```rust
mod bookmarks;
#[cfg(test)]
mod code_completion_tests;
#[cfg(test)]
mod edit_prediction_tests;
#[cfg(test)]
mod editor_block_comment_tests;
#[cfg(test)]
mod editor_tests;
mod signature_help;
#[cfg(any(test, feature = "test-support"))]
pub mod test;

mod clipboard;
mod code_actions;
mod completions;
mod config;
mod diagnostics;
mod edit_prediction;
mod input;
mod markdown_actions;
mod navigation;
mod rewrap;
mod selection;
```

这里出现了两种条件编译：`#[cfg(test)]` 只在 `cargo test` 编译本 crate 时生效；而 `pub mod test` 用的是 `#[cfg(any(test, feature = "test-support"))]`——当**别的 crate** 开着 test-support 依赖我们时也能用到 `editor::test` 里的测试工具（`EditorTestContext` 就住在 `src/test/` 目录里，第 8 单元会专门讲）。这正是 4.1 节那个 feature 存在的直接原因。

**导出面（节选）**，完整范围是 [src/editor.rs:L74-L131](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L74-L131)。几处值得精读：

```rust
pub(crate) use actions::*;
pub use clipboard::ClipboardSelection;
```

这两行形成了鲜明对比（[src/editor.rs:L74-L75](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L74-L75)）：动作类型只对 crate 内部全开放（`pub(crate)`），而剪贴板选区类型对外导出。

```rust
pub use display_map::{
    ChunkRenderer, ChunkRendererContext, DisplayPoint, FoldPlaceholder, HighlightKey,
    NavigationOverlayKey, SemanticTokenHighlight,
};
```

私有模块 `display_map` 通过这条 `pub use`（[src/editor.rs:L84-L87](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L84-L87)）把 `DisplayPoint`（显示坐标点）等类型暴露出去——外部 crate 不需要知道 display_map 内部有多少层。

```rust
pub use multi_buffer::{
    Anchor, AnchorRangeExt, BufferOffset, ExcerptRange, MBTextSummary, MultiBuffer,
    MultiBufferOffset, MultiBufferOffsetUtf16, MultiBufferSnapshot, PathKey, RowInfo, ToOffset,
    ToPoint,
};
```

这是一个重要的架构信号：[src/editor.rs:L124-L128](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L124-L128) 把依赖 crate `multi_buffer` 的核心类型**原样转发**出来。于是 `vim`、`search` 等下游只需要 `use editor::...` 一条路径就能拿到 `Anchor`、`MultiBuffer`——`editor` 实际上充当了整个"编辑器技术栈"的门面（facade）。

```rust
pub use element::{
    CursorLayout, EditorElement, HighlightedRange, HighlightedRangeLine, PointForPosition,
    file_status_label_color, render_breadcrumb_text,
};
```

渲染模块 `element` 同样只选择性导出（[src/editor.rs:L105-L108](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L105-L108)）：`terminal_view` 复用了这里的 `CursorLayout` 来画终端光标。类似的还有 git 相关导出（[src/editor.rs:L109-L117](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L109-L117)，`BlameRenderer`、`DiffHunkDelegate` 等）和 diff 视图导出（[src/editor.rs:L129](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L129)，`SplittableEditor`、`ToggleSplitDiff`）。

最后提醒一点：`editor.rs` 并非只有声明。`EditorStyle` 定义在 [src/editor.rs:L521](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L521)，主角 `Editor` 结构体从 [src/editor.rs:L921](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L921) 开始——那是下一单元的开场。

#### 4.2.4 代码实践

**实践目标**：把 52 个子模块按职责归类，形成一张属于你自己的模块地图，并用独立编译验证认知没有跑偏。

**操作步骤**：

1. 打开 [src/editor.rs:L14-L72](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L14-L72)，逐行阅读模块声明。
2. 在你自己的笔记（或本地分支的一个注释文件）里，把每个模块归入下面五类。下面是**参考分类**（示例答案，允许有自己的判断，关键是能说出理由）：

   ```text
   // 示例代码：模块分类清单（节选 + 归类理由）
   // —— 数据与坐标 ——
   // display_map        坐标变换分层管线（tab/wrap/fold/inlay/block）
   // selections_collection / selection   多光标选区数据与历史
   // movement           光标移动的纯函数计算（vim crate 复用）
   // navigation / bookmarks / clipboard  跳转历史、书签、剪贴板
   // fold / folding_ranges               折叠状态与折叠范围来源
   //
   // —— 渲染 ——
   // element(+element/header.rs, element/mouse.rs)  全部渲染与鼠标事件
   // blink_manager       光标闪烁节奏
   // indent_guides / bracket_colorization / highlight_matching_bracket
   // mouse_context_menu / split_editor_view / scroll
   //
   // —— 语言智能（LSP / tree-sitter）——
   // completions / code_actions / code_context_menus   补全与代码操作
   // hover_popover / hover_links / document_links     悬浮与链接
   // semantic_tokens / document_colors / document_symbols
   // inlays(+inlays/inlay_hints.rs) / diagnostics / signature_help
   // code_lens / folding_ranges / linked_editing_ranges / jsx_tag_auto_close
   // lsp_ext / clangd_ext / rust_analyzer_ext          针对特定语言服务器的扩展
   // edit_prediction / markdown_actions / rewrap / runnables / tasks*
   //
   // —— git 与 diff ——
   // git(+git/blame.rs)  diff hunk、blame、hunk 操作
   // split               并排 diff 视图（SplittableEditor）
   //
   // —— 测试 ——
   // test(+test/editor_test_context.rs 等)  公共测试工具（test-support feature）
   // editor_tests / code_completion_tests / edit_prediction_tests / editor_block_comment_tests
   //
   // —— 编辑器核心与集成（补充组）——
   // editor.rs 本体（Editor 结构体 L921 起）
   // actions / input / config / editor_settings / items / persistence
   ```

   注：`tasks.rs` 目前未在库根的 `mod` 声明中出现，其接入方式**待确认**，分类时可以先跳过。
3. 执行 `time cargo build -p editor`，确认编译通过并记录耗时。
4. 抽查两三个归类：用编辑器搜索功能在对应文件里验证你的猜测（例如 `inlays.rs` 里是否真的在处理 LSP inlay）。

**需要观察的现象**：分类过程中你会发现不少模块跨类别（比如 `fold` 既改坐标又影响渲染），这很正常——分类是为了建立记忆锚点，不是严格边界。

**预期结果**：得到一份五类清单和一个成功的构建结果。构建耗时**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`pub mod movement` 和 `mod element` 的可见性差异，分别服务了什么需求？

参考答案：`movement` 公开是因为 `vim` crate 需要复用这些光标移动纯函数来实现 vim 风格的移动命令；`element` 私有是因为外部不需要（也不应该）直接依赖渲染 internals，外界只通过 `pub use element::{CursorLayout, EditorElement, ...}` 这条受控通道拿走必要的类型。

**练习 2**：`#[cfg(test)] mod editor_tests` 和 `#[cfg(any(test, feature = "test-support"))] pub mod test` 的使用场景有何不同？

参考答案：前者只在本 crate 自己跑 `cargo test` 时编译，是纯粹的内部测试；后者在本 crate 测试**或**下游 crate 开启 test-support 时都可用——`EditorTestContext` 这类测试基建需要被 `vim`、`search` 等下游 crate 的测试复用，所以必须对外公开且由 feature 控制。

**练习 3**：为什么 `search` crate 写 `use editor::{Editor, EditorElement, EditorStyle, ...}` 时，`EditorStyle` 能从 `editor` 命名空间找到？

参考答案：`EditorStyle` 直接定义在库根文件 [src/editor.rs:L521](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L521)，而库根里的公开类型天然位于 crate 命名空间顶层，不需要额外 `pub use`。

## 5. 综合实践

**任务：用证据回答"谁在用 editor 的什么"。**

本讲反复强调 editor 是被 53 个 crate 复用的底座，现在轮到你亲手验证。操作步骤：

1. 在仓库根目录执行下面的 grep，统计依赖方数量（结果应为 53 个 Cargo.toml，其中一部分仅在 dev-dependencies 中使用）：

   ```bash
   grep -rl '^editor\.workspace = true\|^editor = { workspace = true' crates/*/Cargo.toml | wc -l
   ```

2. 再看三个典型依赖方各自 import 了什么：

   ```bash
   grep -rn '^use editor::' crates/vim/src | head -5
   grep -rn '^use editor::' crates/search/src | head -5
   grep -rn '^use editor::' crates/terminal_view/src | head -5
   ```

3. 把结果整理成"crate → 用到的导出 → 对应 editor 里哪个模块"的三列表。

**参考答案**（编写本讲义时已实际执行过上述 grep，结果真实）：

| 依赖方 | 实际导入（示例） | 来源模块 |
| --- | --- | --- |
| vim | `Editor`、`SelectionEffects`、`display_map::ToDisplayPoint`、`ClipboardSelection`（[crates/vim/src/indent.rs:L3-L4](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/vim/src/indent.rs#L3-L4)） | editor.rs 本体、actions、display_map、clipboard |
| search | `Editor`、`EditorElement`、`EditorStyle`、`MultiBufferOffset`、`EditorSettings`、`SearchSettings`（[crates/search/src/search_bar.rs:L1](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/search/src/search_bar.rs#L1)） | editor.rs 本体、element、multi_buffer 转发、editor_settings |
| terminal_view | `CursorLayout`、`EditorSettings`、`HighlightedRange`（[crates/terminal_view/src/terminal_element.rs:L1](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/terminal_view/src/terminal_element.rs#L1)） | element、editor_settings |

可以清楚看到复用的层次：vim 深入到坐标与动作层，search 把 editor 当输入框和设置来源用，terminal_view 只借用渲染零件。一个 crate 被三种完全不同的方式消费，正是"底座"定位的证据。

## 6. 本讲小结

- `editor` 是 Zed 的编辑器底座：既实现完整代码编辑器，也提供单行/多行/固定高度三种输入框形态，仓库内 53 个 crate 直接依赖它（实测）。
- `Cargo.toml` 里 `[lib] path = "src/editor.rs"` 遵循 Zed "库根用描述性文件名"的约定；唯一的 feature `test-support` 是层层向下开启的测试基建开关，还会拉起 tree-sitter 语法解析器等仅在测试中需要的可选依赖。
- `src/editor.rs` 顶部的 crate 文档点名了两个最大的子模块：`element`（全部渲染）和 `display_map`（文本切块与坐标映射）。
- 库根共声明约 52 个子模块，`pub` 与 `mod` 的取舍、以及大片 `pub use`（尤其是 `multi_buffer` 类型的转发导出）共同构成了 editor 对外的受控接口面。
- 按"数据与坐标 / 渲染 / 语言智能 / git 与 diff / 测试"五类给模块分组，是后续单元之前最好的脑内地图。

## 7. 下一步学习建议

- 下一讲（u1-l2《构建与测试》）将真正跑通这个 crate 的第一个测试：`cargo test -p editor test_undo_redo_with_selection_restoration`，并认识 `test-support` feature 在测试运行中的具体作用。
- 再下一讲（u1-l3《模块地图》）会更系统地验证模块间的依赖方向（例如用 grep 确认 `element` 如何引用 `display_map`）。
- 如果你已经迫不及待想看主角，可以直接跳读 [src/editor.rs:L921](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L921) 开始的 `Editor` 结构体定义，但建议按单元顺序推进——第 2 单元会逐字段拆解它。
