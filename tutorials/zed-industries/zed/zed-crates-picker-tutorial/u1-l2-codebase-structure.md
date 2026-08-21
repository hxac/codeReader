# 目录结构与模块地图：从 lib 根到每个子模块

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 picker crate 的库根为什么是 `src/picker.rs` 而不是 `src/lib.rs`（对应 `Cargo.toml` 中 `[lib] path` 的配置），以及 `test-support` feature 的作用。
2. 逐一说出 crate 内 11 个源码文件（10 个模块 + 1 个嵌套子模块）各自的职责。
3. 理解「私有模块 + crate 根 `pub use` 再导出」的门面（facade）设计：哪些类型对外可见、以什么名字可见。
4. 画出模块之间的依赖方向图（谁 `use` 谁），并理解同一 crate 内模块互相引用是合法的。

本讲承接上一讲（u1-l1）：上一讲已经建立了「框架管交互与外观，委托管数据」的心智模型，认识了 `Picker<D>` 与 `PickerDelegate` 的分工、`DEFAULT_MODAL_WIDTH`（34rem）/`DEFAULT_MODAL_MAX_HEIGHT`（24rem）两个常量，以及 `name()` 作为 `pickers_v2` 持久化键的用途。本讲把镜头拉远，看整个 crate 的「文件地图」。

## 2. 前置知识

### 2.1 Rust 模块系统速览

如果你对 Rust 模块还不太熟，只需要先掌握这几条：

- **`mod foo;`**：声明一个子模块，编译器会去找 `src/foo.rs` 或 `src/foo/mod.rs`（Zed 规范禁止 `mod.rs`，见仓库根 `CLAUDE.md`）。声明时不加 `pub`，这个模块就是**私有**的——只有本 crate 内部能访问它的路径。
- **`pub mod foo;`**：公开模块，外部 crate 可以写 `picker::foo::某类型` 来使用它。
- **`pub use bar::Baz;`**：再导出。把 `bar` 模块里的 `Baz` 提升到当前位置的命名空间，外部使用者就能写 `picker::Baz`，而不必知道它实际定义在哪个子模块里。
- **`pub(crate)`**：对整个 crate 可见、但对外部 crate 不可见。比「私有」宽、比 `pub` 窄。

一个容易困惑的点：**同一 crate 内的两个模块可以互相引用**。例如 picker crate 里 `picker.rs` 再导出了 `footer::PickerAction`，而 `footer.rs` 又反过来 `use crate::Picker`——这完全合法，因为整个 crate 是一次性编译的整体，模块间的「互相引用」不构成循环依赖问题。只有 **crate 与 crate 之间** 才要求依赖关系无环。

### 2.2 lib.rs 约定与 `[lib] path` 覆盖

Cargo 默认把 `src/lib.rs` 当作库 crate 的根。但可以在 `Cargo.toml` 里用 `[lib] path = "..."` 覆盖这个约定，指向任意文件。Zed 仓库规范（根 `CLAUDE.md`）明确要求新 crate 这样做，用「描述性命名」的文件（如 `picker.rs`、`main.rs`）替代千篇一律的 `lib.rs`。

### 2.3 门面模式（Facade）

「把实现拆进一堆私有子模块，再在 crate 根统一 `pub use`」是一种常见的设计：外部使用者只需要认识 crate 根这一个入口，内部文件怎么拆分、怎么重命名都不会影响外部调用方。阅读大型 Rust crate 时，**先看库根的 `mod` 声明和 `pub use`，就能立刻分清「对外 API」和「内部实现」**——这是本讲反复使用的技巧。

## 3. 本讲源码地图

picker crate 没有自己的 README，全部说明都写在代码的 doc 注释里。src 目录下共 10 个模块文件（外加 1 个嵌套子模块），合计约 4490 行：

| 文件 | 行数 | 职责 | 模块可见性 |
|---|---|---|---|
| `src/picker.rs` | 1999 | 库根：`Picker<D>` 结构体、`PickerDelegate` trait、构造函数、交互逻辑、内嵌测试 | （库根本身） |
| `src/head.rs` | 84 | 头部：可搜索的编辑器或不可见的空头部 | `mod head`（私有） |
| `src/preview.rs` | 139 | 预览：`PreviewBackend` trait、`Preview`、`Layout`、`Update` 等数据类型 | `mod preview`（私有） |
| `src/shape.rs` | 678 | 几何：相对尺寸类型、`Shape`、`SizeBounds`、clamp 约束计算 | `mod shape`（私有） |
| `src/persistence.rs` | 145 | 持久化：基于 db KVP 的形状/布局存取 | `mod persistence`（私有） |
| `src/footer.rs` | 226 | 底栏：`PickerAction` 与默认 footer 渲染 | `mod footer`（私有） |
| `src/render.rs` | 453 | 渲染入口：`impl Render for Picker<D>` 与各布局渲染函数 | `mod render`（私有） |
| `src/render/window_controls.rs` | 503 | 拖拽调整：`Side` trait、`ResizeDrag`、resize 手柄 | `render` 内的 `pub mod`，因 `render` 私有而实际仅 crate 内可见 |
| `src/popover_menu.rs` | 102 | 把整个 Picker 装进 `ui::PopoverMenu` 的包装器 | `pub mod popover_menu` |
| `src/parts.rs` | 27 | 多个 picker 复用的小组件（`project_scan_indicator`） | `pub mod parts` |
| `src/highlighted_match_with_paths.rs` | 134 | 带路径的「高亮匹配行」展示组件 | `pub mod highlighted_match_with_paths` |

本讲精读的关键文件是 `Cargo.toml` 与 `src/picker.rs`，其余模块只看「头部声明与被引用方式」，不深入算法（那是后续单元的事）。

## 4. 核心概念与源码讲解

### 4.1 库根 picker.rs：Cargo.toml 的 [lib] 配置

#### 4.1.1 概念说明

一个 crate 由两部分描述：`Cargo.toml`（怎么编译、依赖谁）和库根文件（从哪个文件开始组织代码）。picker 的库根不是默认的 `src/lib.rs`，而是 `src/picker.rs`，这是在 `Cargo.toml` 的 `[lib]` 段显式指定的。

同时，`Cargo.toml` 还声明了一个空的 feature：`test-support = []`。空 feature 不开启任何条件编译代码，它的作用是**一个开关**——配合源码里的 `#[cfg(any(test, feature = "test-support"))]`，让「只在测试里需要的公开 API」在正式构建中不存在，而依赖 picker 的其他 crate（如 file_finder）可以在自己的 dev-dependencies 里打开这个开关，在它们的测试中使用这些 API。

#### 4.1.2 核心流程

Cargo 定位库根的决策过程：

1. 读取 `Cargo.toml`。
2. 若有 `[lib] path = "..."`，直接用该文件作为 crate 根；否则回退到默认 `src/lib.rs`。
3. 编译 crate 根时，遇到 `mod xxx;` 就沿着路径系统找到 `src/xxx.rs`。

`test-support` 的生效过程：

1. 正常 `cargo build`：feature 关闭，`#[cfg(any(test, feature = "test-support"))]` 标注的代码**不参与编译**。
2. `cargo test -p picker`：`test` cfg 为真，这些代码对 crate 自己的测试可见。
3. 其他 crate（如 file_finder）在自己的 `[dev-dependencies]` 里写 `picker = { workspace = true, features = ["test-support"] }`：feature 为真，这些代码对**该 crate 的测试**可见。实际验证过，仓库里 `file_finder` 和 `recent_projects` 两个 crate 正是这么做的。

#### 4.1.3 源码精读

库根路径配置与 feature 声明（[Cargo.toml:L11-L16](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/Cargo.toml#L11-L16)）：

```toml
[lib]
path = "src/picker.rs"
doctest = false

[features]
test-support = []
```

这段配置做了三件事：把库根指定为 `src/picker.rs`；`doctest = false` 关闭文档测试（UI 组件的示例依赖 GPUI 运行环境，无法在 doctest 里执行）；声明空 feature `test-support`。

依赖清单（[Cargo.toml:L18-L35](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/Cargo.toml#L18-L35)）：picker 依赖 `gpui`（UI 框架）、`ui`（设计系统组件）、`ui_input`（输入框抽象）、`workspace`（`ModalView`）、`db`（键值存储）、`language`、`project`、`menu`、`settings`、`theme`、`schemars`/`serde`/`serde_json`（序列化）、`util`、`anyhow`、`zed_actions`。上一讲已经看过这份清单，这里只需注意：**后续每个子模块各自用到其中一两个外部 crate**。

开发依赖（[Cargo.toml:L37-L40](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/Cargo.toml#L37-L40)）：

```toml
[dev-dependencies]
editor = { workspace = true, features = ["test-support"] }
gpui = { workspace = true, features = ["test-support"] }
settings.workspace = true
```

这三个开发依赖只服务于 picker.rs 末尾的内嵌测试模块：测试初始化函数 `init_test` 需要 `settings::SettingsStore::test`、`theme_settings::init` 和 `editor::init`（见 [src/picker.rs:L1786-L1791](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1786-L1791)），细节留给下一讲（u1-l3）。

库根本身则是全 crate 的「总装车间」：`Picker<D>` 结构体定义在 [src/picker.rs:L127-L153](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L127-L153)，`head`、`shape`、`size_bounds` 等字段直接使用子模块类型；`PickerDelegate` trait 从 [src/picker.rs:L164](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L164) 开始。测试专用 API 的两个 `#[cfg(any(test, feature = "test-support"))]` 门控示例：`logical_scroll_top_index`（[src/picker.rs:L1553-L1561](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1553-L1561)）与 `results_width`（[src/picker.rs:L1589-L1598](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1589-L1598)）。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「库根不是 lib.rs」以及 `test-support` 的跨 crate 使用。

**操作步骤**：

1. 在 `crates/picker` 目录下列出 src 下的文件：`ls src/`。确认**没有** `lib.rs`，只有本讲源码地图里列出的 10 个 `.rs` 文件。
2. 打开 `Cargo.toml`，找到 `[lib]` 段，确认 `path = "src/picker.rs"`。
3. 在仓库根目录执行：`grep -rn 'picker = { workspace = true, features = \["test-support"\]}' crates/*/Cargo.toml`。
4. 构建验证：`cargo build -p picker`（在仓库根目录执行）。

**需要观察的现象**：步骤 1 里 src 下确实没有 lib.rs；步骤 3 应输出 file_finder 与 recent_projects 两行。

**预期结果**：`cargo build -p picker` 成功，说明把库根改为 `picker.rs` 后 Cargo 一切正常。构建耗时与是否成功**待本地验证**（我只做了静态阅读与 grep 验证，没有实际执行构建）。

#### 4.1.5 小练习与答案

**练习 1**：如果删掉 `Cargo.toml` 里的 `[lib] path = "src/picker.rs"`，会发生什么？
**答案**：Cargo 会找默认的 `src/lib.rs`，而该文件不存在，构建直接报错（找不到库目标）。Zed 用 `[lib] path` 明确指向 `picker.rs`，正是仓库规范「库根用描述性命名」的体现。

**练习 2**：`test-support` 是空 feature（`test-support = []`），它怎么起到作用？
**答案**：feature 本身不编译任何条件代码，它只是一个可被 `#[cfg(feature = "test-support")]` 检测的布尔开关。picker 源码里若干测试辅助 API 用 `#[cfg(any(test, feature = "test-support"))]` 门控，正式构建不含它们；依赖方在自己的 dev-dependencies 打开该 feature 后，就能在自己的测试中调用这些 API。

**练习 3**：为什么 dev-dependencies 里有 `editor`？
**答案**：picker.rs 末尾的内嵌测试（`init_test`）需要初始化真实编辑器（`editor::init(cx)`），因为可搜索 picker 的头部底层是一个编辑器实例（见 4.4 节的 head 模块）。

### 4.2 模块声明与 pub use 门面：谁对外可见、以什么名字可见

#### 4.2.1 概念说明

picker.rs 的顶部（第 23-43 行）是全 crate 的「总开关面板」：9 条 `mod` 声明决定每个子模块的存在与可见性，10 条 `pub use` 决定哪些类型以什么名字对外暴露。

设计的核心思路是**门面模式**：

- 绝大多数模块声明为私有（`mod xxx;`），外部根本无法写出 `picker::shape::Shape` 这样的路径。
- 子模块里真正要给外部用的类型，统一在 crate 根 `pub use` 再导出，外部写 `picker::Preview` 即可。
- 少数「自成体系的组件」直接 `pub mod` 公开整个模块（如 `popover_menu`、`parts`、`highlighted_match_with_paths`）。

这样带来的好处：内部重构（改子模块文件名、挪动类型的定义位置）几乎不会破坏外部 API；同时外部使用者面对的 API 面最小、最稳定。

#### 4.2.2 核心流程

外部调用者使用 picker 的路径解析过程：

1. 外部 crate 写 `use picker::{Preview, PreviewLayout};`。
2. Rust 在 picker crate 根查找 `Preview`，命中再导出 `pub use preview::Preview;`。
3. 实际定义在私有模块 `preview` 里，但对调用者完全透明——调用者不需要（也无法）写出 `picker::preview::Preview`。
4. 若调用者试图使用未被再导出的内部类型（如 `shape::Shape`），编译器直接报「私有模块」错误，这是设计上有意的保护。

#### 4.2.3 源码精读

模块声明区（[src/picker.rs:L23-L31](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L23-L31)）——注意只有 3 个 `pub mod`：

```rust
mod footer;
mod head;
pub mod highlighted_match_with_paths;
pub mod parts;
mod persistence;
pub mod popover_menu;
mod preview;
mod render;
mod shape;
```

再导出区（[src/picker.rs:L33-L43](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L33-L43)）：

```rust
use crate::shape::RelativeHeight;      // 私有引入：构造函数签名用
use crate::shape::RelativeWidth;       // 私有引入：构造函数签名用
pub use footer::PickerAction;
pub use language::{HighlightedText, HighlightedTextBuilder};
pub use preview::Layout as PreviewLayout;
pub use preview::MatchLocation;
pub use preview::Preview;
pub use preview::PreviewBackend;
pub use preview::PreviewSource;
pub use preview::Update as PreviewUpdate;
pub use ui_input::ErasedEditor;
```

这段代码信息量很大，逐类说明：

- **私有模块类型的再导出**：`PickerAction`（footer）、`PreviewLayout`/`Preview`/`PreviewBackend`/`PreviewSource`/`PreviewUpdate`/`MatchLocation`（preview）。注意两次 `as` 改名——`Layout` 改成 `PreviewLayout` 是为了避免名字过于宽泛（外部还有 gpui 的 Layout 等同名类型），`Update` 改成 `PreviewUpdate` 同理。
- **跨 crate 的转发再导出**：`language::{HighlightedText, …}` 和 `ui_input::ErasedEditor` 并非 picker 定义，而是转发再导出。目的：delegate 实现者几乎必然要用这些类型，从 picker 一处 import 即可，不必额外依赖 language/ui_input。
- **两个私有 `use crate::shape::…`**：`RelativeWidth`/`RelativeHeight` 只在 crate 内部使用（如 `Picker::initial_width(width: impl Into<RelativeWidth>)` 的签名）。shape 模块本身私有，所以外部调用者无法命名这个类型——但外部可以传 `Rems`，因为 shape 里实现了 `From<Rems>` 自动转换。这是「类型不可见但可用」的巧妙手法。

可见性总表（本讲的速查核心）：

| 对外名字 | 实际定义处 | 可见方式 |
|---|---|---|
| `picker::Picker`、`picker::PickerDelegate` 等 | picker.rs 本体 | 直接 `pub` |
| `picker::PickerAction` | footer.rs | `pub use` 再导出 |
| `picker::PreviewLayout`、`picker::Preview`、`picker::PreviewBackend`、`picker::PreviewSource`、`picker::PreviewUpdate`、`picker::MatchLocation` | preview.rs | `pub use` 再导出（含改名） |
| `picker::HighlightedText`、`picker::ErasedEditor` | language / ui_input crate | 转发再导出 |
| `picker::popover_menu::PickerPopoverMenu` | popover_menu.rs | `pub mod` |
| `picker::parts::project_scan_indicator` | parts.rs | `pub mod` |
| `picker::highlighted_match_with_paths::{HighlightedMatch, HighlightedMatchWithPaths}` | highlighted_match_with_paths.rs | `pub mod` |
| `Head`、`Shape`、`Centered`、`SizeBounds`、`PickerConfig`、persistence 函数等 | head/shape/persistence 等 | `pub(crate)` 或私有模块内 `pub`——外部不可见 |

一个细节：shape.rs 里的 `RelativeWidth`、`ViewportFraction`、`SizeBounds` 声明为 `pub`，但所在模块 `shape` 是私有的，因此**有效可见性只有 crate 内部**。「模块私有」会压住「条目 pub」，这也是阅读时容易误判的地方。

#### 4.2.4 代码实践

**实践目标**：用 grep 提取模块声明与再导出清单，标出全部对外可见的类型。

**操作步骤**：

1. 在 `crates/picker` 下执行：`grep -n '^mod \|^pub mod ' src/picker.rs`。
2. 执行：`grep -n '^pub use \|^use crate::' src/picker.rs`。
3. 对每条 `pub use`，打开对应定义文件确认类型存在，例如 preview.rs 里的 `Layout`。
4. 把结果整理成 4.2.3 那张可见性表的自己的版本。

**需要观察的现象**：步骤 1 输出恰好 9 行（3 个 `pub mod` + 6 个私有 `mod`）；步骤 2 输出 10 条 `pub use` 和 2 条私有 `use crate::shape::…`。

**预期结果**：与 4.2.3 的表格一致（我在准备本讲时已实际执行过同样的 grep，以上行数与内容均与 HEAD 一致）。想进一步验证可见性，可运行 `cargo doc -p picker --no-deps` 后打开生成的文档页，检查再导出的类型是否出现在 picker 的模块列表里——文档只展示对外可见的条目（文档生成**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：外部 crate 想自定义预览面板，需要实现哪个 trait？通过什么路径引用它？
**答案**：实现 `PreviewBackend`（定义在私有模块 preview.rs 中），引用路径是 `picker::PreviewBackend`——由 crate 根再导出，外部无需（也无法）写 `picker::preview::PreviewBackend`。

**练习 2**：`pub use language::{HighlightedText, HighlightedTextBuilder};` 为什么出现在 picker 的 crate 根？这算不算「多管闲事」？
**答案**：不算。这是刻意的「便利再导出」：实现 `PickerDelegate::render_match` 时必然要构造高亮文本，把 `HighlightedText` 从 picker 转发出来，delegate 作者就不必在自己的 Cargo.toml 里额外添加对 language 的依赖，也避免了版本/路径的不一致。

**练习 3**：`pub use preview::Layout as PreviewLayout;` 为什么要改名？
**答案**：`Layout` 是个极常见的类型名（gpui 等多个 crate 都有）。再导出到 crate 根这样的「黄金位置」时改名成 `PreviewLayout`，能让外部 `use picker::PreviewLayout` 一眼可读，也避免与其他 crate 的 `Layout` 在调用方产生名字冲突。

### 4.3 渲染子系统：render.rs 与 render/window_controls.rs

#### 4.3.1 概念说明

`Picker<D>` 有近 2000 行的核心逻辑，但它的 `Render` 实现（决定「长什么样」）不在 picker.rs 里，而是整个搬到了 `render.rs`。手法是 Rust 的 **impl 块拆分**：同一个类型的方法可以分布在多个文件的多个 `impl` 块里。`render.rs` 里写着 `impl<D: PickerDelegate> Render for Picker<D>`，等于给库根里的类型「远程安装」渲染能力。

`render.rs` 内部又嵌套声明了 `pub mod window_controls;`，即 `render/window_controls.rs`，专门负责**拖拽调整大小**的逻辑。这个嵌套子模块虽然自身是 `pub mod`，但因为父模块 `render` 是私有的，所以实际可见性仍限于 crate 内部——这是「父模块可见性压住子模块」的又一例。

#### 4.3.2 核心流程

一帧渲染的简化流程（渲染细节在单元六展开，这里只看结构）：

1. GPUI 每帧调用 `Picker::render`（实现位于 render.rs）。
2. `render` 先调用 `finish_any_completed_resize` 落地上一次拖拽的结果。
3. 按当前预览布局（`Layout::Below` / `Right` / `Hidden` / 无预览）把工作分发给 `render_with_preview_below`、`render_with_preview_right` 或 `render_results`。
4. 需要拖拽手柄时，调用 window_controls 模块提供的 `render_resize`，为每条边/角渲染一个可拖拽的手柄。
5. 各渲染函数内部使用 shape 模块计算尺寸、persistence 模块持久化结果。

#### 4.3.3 源码精读

render.rs 的 import 区直接暴露了它的依赖面（[src/render.rs:L11-L23](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/render.rs#L11-L23)）：

```rust
use crate::shape::Shape;
use crate::{
    ElementContainer, Picker, PickerDelegate, PickerEditorPosition, Preview, ToggleMultiSelect,
    head::Head,
    preview::Layout,
    render::window_controls::{Bottom, Left, LeftCorner, Middle, Right, RightCorner},
};
use crate::{persistence, preview};
```

这一段是「依赖地图」的实证：render.rs 用了库根的类型与动作（`Picker`、`ToggleMultiSelect`）、head 的 `Head`、preview 的 `Layout` 与 `Preview`、shape 的 `Shape`、persistence 模块，以及自己子模块 window_controls 的六个方向类型。

嵌套子模块声明（[src/render.rs:L25](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/render.rs#L25)）：`pub mod window_controls;`——注意它声明在 render.rs 而不是库根，所以路径是 `crate::render::window_controls`。

Render trait 实现与布局分发（[src/render.rs:L27-L64](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/render.rs#L27-L64)）：`render` 方法用 `match &self.preview` 把 `Layout::Below` / `Layout::Right`（还会按窗口宽度判断是否降级为 Below）/ `Layout::Hidden` / `None` 四种情况分别导向三个渲染函数，随后外层统一包上边框、背景与圆角裁剪。

window_controls.rs 是全 crate 文档最密集的文件：开头 57 行的模块注释画满了 ASCII 拖拽示意图（[src/render/window_controls.rs:L1-L57](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/render/window_controls.rs#L1-L57)），讲解「拖右边缘让预览变宽、拖左边缘让列表变宽」等行为。核心抽象是 `Side` trait（[src/render/window_controls.rs:L82](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/render/window_controls.rs#L82)），每个可拖拽的边/角/中缝各有一个实现类型。它的依赖同样写在文件头（[src/render/window_controls.rs:L64-L65](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/render/window_controls.rs#L64-L65)）：`crate::shape::{…}` 与 `crate::{Picker, PickerDelegate, preview::Layout}`。

#### 4.3.4 代码实践

**实践目标**：不读函数体，仅凭 import 语句推导 render 子系统的依赖清单。

**操作步骤**：

1. 执行：`grep -n '^use crate::\|^use super::' src/render.rs src/render/window_controls.rs`。
2. 把输出的每一行拆成「文件 → 依赖的模块」的边。
3. 对照 4.2.3 的可见性表，标出哪些被引用的条目是 `pub(crate)`（如 `ElementContainer`、`Shape`）。
4. 阅读一遍 window_controls.rs 开头的 ASCII 示意图注释，不需要看懂全部，只需回答：这个模块解决什么问题？

**需要观察的现象**：步骤 1 在 render.rs 命中 3 组 `use crate::…`（shape、库根类型+window_controls、persistence+preview），在 window_controls.rs 命中 2 组（shape、库根类型+preview::Layout）。

**预期结果**：得到的依赖边与 4.5 节分层图里「扩展层」一行的内容完全一致（此 grep 我已实际验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么把 `Render` 实现放到单独的 render.rs，而不是写在 picker.rs 里？
**答案**：picker.rs 已经承载了状态管理、构造函数、键盘交互、匹配更新等约 2000 行逻辑；渲染是另一个正交关注点。拆到 render.rs 让每个文件职责单一，也缩短了阅读路径——想改外观只开 render.rs，想改交互只开 picker.rs。Rust 允许 impl 块与类型定义分处不同文件（同 crate 内），这给了拆分的自由。

**练习 2**：`render::window_controls` 声明为 `pub mod`，外部 crate 能访问吗？
**答案**：不能。可见性是「沿路径逐级收紧」的：路径上的每一级都必须可见，外部才能访问。父模块 `render` 是私有 `mod`，所以 `crate::render::window_controls` 对外部不可达，`pub` 只在 crate 内部有效。

**练习 3**：`render_with_preview_below` 和 `render_with_preview_right` 在 render.rs 中标记为 `pub(crate)` 而不是 `pub`，这暗示了什么？
**答案**：它们是 crate 内部的实现细节（被 `render` 分发调用），不属于对外承诺的 API。`pub(crate)` 表达了「本 crate 其他模块（比如库根）可以调用，但外部不行」的精确边界。

### 4.4 状态与数据模块：head、preview、shape、persistence、footer

#### 4.4.1 概念说明

这五个私有模块各自持有 `Picker` 的一块「状态/职责」：

- **head（头部）**：搜索输入框所在的位置。两种形态：带编辑器的（可搜索 picker）和空的（不可搜索 picker）。编辑器不是具体类型，而是 `Arc<dyn ErasedEditor>` trait 对象——具体实现由 ui_input crate 的全局工厂在运行时决定，从而把 picker 与编辑器实现解耦。
- **preview（预览）**：定义「预览窗格显示什么」的数据协议（`PreviewSource`、`MatchLocation`、`Update`）与「谁来画」的接口（`PreviewBackend`），以及三种布局 `Layout::Hidden / Below / Right`。
- **shape（几何）**：一套「视口比例 + rem」双分量的相对尺寸类型（`RelativeWidth`/`RelativeHeight`），加上 `Shape`（当前形状）与 `SizeBounds`（最小/最大约束），负责所有与窗口大小有关的数学。
- **persistence（持久化）**：把用户拖出来的窗口形状和上次用的预览布局写进 db 键值存储（`pickers_v2` 命名空间），下次打开同一个 picker 时恢复——这正是上一讲「`name()` 是持久化键」的落地处。
- **footer（底栏）**：默认底栏（预览切换按钮 + Actions 菜单）的渲染，以及菜单条目类型 `PickerAction`。

#### 4.4.2 核心流程

五个模块的依赖方向（谁 use 谁）构成清晰的分层：

```text
外部 crate（gpui、ui、ui_input、language、project、db、workspace、menu、settings…）
    ▲ 每个模块都直接 use 若干外部 crate
    │
叶子层   highlighted_match_with_paths.rs   parts.rs   head.rs   preview.rs
    （四者互不引用，也不引用 crate 内其他模块）
    ▲
    │ use preview（只取 Layout 枚举）
几何层   shape.rs
    ▲
    │ use preview + shape
持久层   persistence.rs
    ▲
    │ use head + preview + shape + persistence + footer
核心层   picker.rs（库根：声明全部模块、再导出公开类型）
    ▲
    │ use crate::{Picker, PickerDelegate, …}
扩展层   footer.rs   render.rs ─use─▶ render/window_controls.rs   popover_menu.rs
```

两点值得特别注意：

1. **分层几乎无环**：preview 和 shape 是被依赖的底座；persistence 在它们之上；picker.rs 总装；footer/render/popover_menu 是「挂在库根类型上的扩展」。
2. **唯一的「互相引用」发生在库根与扩展层之间**：picker.rs `pub use footer::PickerAction`，footer.rs 又 `use crate::Picker`。如前置知识所说，同 crate 内这是合法且常见的。

#### 4.4.3 源码精读

**head.rs**——`Head` 枚举两种形态（[src/head.rs:L8-L14](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/head.rs#L8-L14)）：

```rust
pub(crate) enum Head {
    Editor(Arc<dyn ErasedEditor>),
    Empty(Entity<EmptyHead>),
}
```

`Head::editor` 构造时通过全局工厂拿到编辑器实例（[src/head.rs:L17-L25](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/head.rs#L17-L25)，第 23 行调用 `ui_input::ERASED_EDITOR_FACTORY`）；`EmptyHead` 则是「看不见但能持焦点」的元素（[src/head.rs:L62-L78](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/head.rs#L62-L78)）。注意 `Head` 和 `EmptyHead` 都是 `pub(crate)`——纯粹的内部实现。

**preview.rs**——接口与数据（[src/preview.rs:L11-L18](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/preview.rs#L11-L18)）：`PreviewBackend` trait 定义 `update / render / adjust_to_new_size / clear` 四个方法；`Preview` 结构体包装 `Arc<dyn PreviewBackend>` 并持有当前 `layout`（[src/preview.rs:L21-L24](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/preview.rs#L21-L24)，注意 `layout` 字段是 `pub(crate)`）；`Layout` 枚举三种取值（[src/preview.rs:L26-L32](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/preview.rs#L26-L32)）。整个文件没有一行渲染代码——预览「画什么」由实现 `PreviewBackend` 的外部类型决定，这就是模块边界带来的解耦。

**shape.rs**——双分量尺寸类型由一个宏批量生成（宏定义 [src/shape.rs:L29-L149](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/shape.rs#L29-L149)，展开点 [src/shape.rs:L151-L152](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/shape.rs#L151-L152)）：`RelativeWidth` 与 `RelativeHeight` 都是「视口比例 + rems」的二元组，支持加减乘除，最终经 `as_pixels(window)` 换算成像素。`Shape` 枚举两态（[src/shape.rs:L217-L225](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/shape.rs#L217-L225)）：`Resizing`（拖拽中的绝对坐标）与 `HorizontallyCentered(Centered)`（常态，可持久化）。`SizeBounds` 及其默认约束（[src/shape.rs:L227-L254](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/shape.rs#L227-L254)）定义了 95% 视口宽、280px/320px 结果区最小尺寸等边界。文件第一行 `use crate::preview::Layout;`（[src/shape.rs:L5](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/shape.rs#L5)）是它唯一的 crate 内依赖——因为最小尺寸要按「预览在右/在下/无预览」三种布局分别组合。

**persistence.rs**——存取两件事（[src/persistence.rs:L11-L37](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/persistence.rs#L11-L37)）：`store_shape_for_this_layout` 把形状序列化成 `PickerConfig` 写入 `pickers_v2` 命名空间（键形如 `file_finder/right`，见 [src/persistence.rs:L93-L99](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/persistence.rs#L93-L99)），且必须先写 shape 再写 `LAST_PREVIEW_LAYOUT`（代码注释解释了原因）。`PickerConfig` 的三个字段都是视口分数而非像素（[src/persistence.rs:L120-L127](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/persistence.rs#L120-L127)），这样换一台分辨率的机器，记忆的尺寸仍然有意义。库根在两处消费它：构造时（`Picker::new` 里调用 `load_last_preview_layout` / `try_load_shape`）与切换布局时（[src/picker.rs:L1612-L1638](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1612-L1638) 的 `set_preview_layout`）。

**footer.rs**——`PickerAction` 枚举定义菜单条目三种形态（[src/footer.rs:L15-L23](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/footer.rs#L15-L23)）：`Header`（不可点的分组标题）、`Separator`（分隔线）、`Entry`（可点条目，可带开关状态）。它通过 `add_to_menu`（[src/footer.rs:L52-L81](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/footer.rs#L52-L81)）把自己翻译成 ui crate 的 `ContextMenu` 条目。`render_footer`（[src/footer.rs:L84-L93](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/footer.rs#L84-L93)）体现了典型的「委托优先」策略：delegate 覆盖了 `render_footer` 就用它的，否则落到 `render_default_footer`。注意这个文件的 `use` 区（[src/footer.rs:L6-L12](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/footer.rs#L6-L12)）引用了库根的 `Picker`、`PickerDelegate` 和四个 action 类型——这就是 4.4.2 说的「扩展层反向引用核心层」。

#### 4.4.4 代码实践

**实践目标**：用 grep 逐文件验证 4.4.2 的分层图，特别是「shape 只依赖 preview」这条最细的依赖边。

**操作步骤**：

1. 执行：`grep -n 'crate::' src/shape.rs`——观察只命中 `use crate::preview::Layout`（第 5 行）和 `crate::DEFAULT_MODAL_WIDTH/HEIGHT`（默认尺寸常量，属库根）。
2. 执行：`grep -n '^use crate::' src/persistence.rs src/footer.rs src/popover_menu.rs`——persistence 应命中 preview + shape；footer 应命中库根类型 + actions + preview；popover_menu 应只命中库根的 `Picker`/`PickerDelegate`。
3. 执行：`grep -c 'crate::' src/head.rs src/parts.rs src/highlighted_match_with_paths.rs`——三个叶子模块计数应为 0（它们只用外部 crate）。
4. 把以上结果整理成「文件 → 依赖模块」的边列表，与 4.4.2 的分层图互相印证。

**需要观察的现象**：每条 grep 的命中行与上文描述一致；叶子模块没有任何 `crate::` 引用。

**预期结果**：分层图得到静态验证（以上 grep 我在准备本讲时均已实际执行并核对）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Head::Editor` 存的是 `Arc<dyn ErasedEditor>` 而不是具体编辑器类型？
**答案**：picker 不想依赖任何具体编辑器实现。`ErasedEditor` 是 ui_input 定义的 trait 对象接口（擦除了具体类型），真正的实现由 `ERASED_EDITOR_FACTORY` 全局工厂在运行时注入。这样测试环境可以换上轻量实现，主程序可以换上完整编辑器，picker 的代码一行不改。

**练习 2**：`persistence.rs` 为什么把尺寸存成「视口分数」而不是像素值？
**答案**：像素值绑定当前窗口/屏幕大小，换个显示器就失真；视口分数是相对比例，跨设备依然合理。shape.rs 的 `PickerConfig::from_centered` / `into_centered` 负责两种表示的互转，读回时还会 clamp 到 0.0-1.0 防御损坏数据。

**练习 3**：`shape::Shape` 的文档注释说它「可能被持久化零到三次」，指哪三种情况？
**答案**：同一个 delegate 的形状按预览布局分别记忆：无预览一份、预览在下一份、预览在右一份（加上 `LAST_PREVIEW_LAYOUT` 记住上次用哪种布局）。所以用户如果三种形态都拖过尺寸，就有三份 shape 记录；从没拖过就是零份，打开时用默认尺寸。

### 4.5 共享组件与展示形态：parts、highlighted_match_with_paths、popover_menu

#### 4.5.1 概念说明

剩下三个 `pub mod` 是「整个 crate 里真正公开模块」：

- **parts（零件）**：文件只有 27 行，doc 注释一句话——「Components used in multiple pickers」（多个 picker 复用的组件）。目前唯一成员 `project_scan_indicator` 是「项目扫描进行中」的转圈提示。它是一颗**纯叶子**：不依赖 crate 内任何模块，任何外部 crate 都可以直接拿去用。
- **highlighted_match_with_paths**：搜索结果里常见的「高亮匹配文字 + 附属路径」的展示组件，核心是 `HighlightedMatch::join`——把多个高亮片段用分隔符拼接，并同步修正高亮位置的字节偏移。
- **popover_menu**：`PickerPopoverMenu` 是个包装器，把整个 `Entity<Picker<P>>` 塞进 ui 的 `PopoverMenu`，让一个 picker 能以「点击按钮弹出的气泡菜单」形态出现（比如状态栏按钮弹出的 picker），并把 picker 的 `DismissEvent` 转发出去。

#### 4.5.2 核心流程

以 `PickerPopoverMenu` 为例的组装流程：

1. 构造时立刻调用 `picker.set_popover()`，把该 picker 的展示形态（`Presentation`，库根里的私有枚举）切换为 Popover——此后它不画自己的模态背景。
2. `cx.subscribe(&picker, …)` 订阅 picker 的 `DismissEvent` 并原样转发，宿主界面据此关闭气泡。
3. `RenderOnce::render` 时把 `Entity<Picker<P>>` 作为菜单内容交给 `PopoverMenu`，配上触发按钮、锚点与偏移。

#### 4.5.3 源码精读

parts 的全部「设计文档」就一行（[src/parts.rs:L1](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/parts.rs#L1)）：`//! Components used in multiple pickers`；`project_scan_indicator` 在 [src/parts.rs:L7-L27](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/parts.rs#L7-L27)，逻辑是「有查询 且 项目工作区首扫未完成」时显示旋转加载图标。

`HighlightedMatch::join` 的字节偏移处理（[src/highlighted_match_with_paths.rs:L19-L47](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/highlighted_match_with_paths.rs#L19-L47)）：拼接每一段文本时维护 `byte_offset` 计数器，后一段的高亮位置都要加上前面所有文本（含分隔符）的字节长度，否则高亮会错位。该文件也是 crate 里唯一在库根之外带单元测试的模块（[src/highlighted_match_with_paths.rs:L103-L104](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/highlighted_match_with_paths.rs#L103-L104)）。

`PickerPopoverMenu` 的结构体与「切形态 + 转发事件」（[src/popover_menu.rs:L11-L24](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/popover_menu.rs#L11-L24)、[src/popover_menu.rs:L32-L54](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/popover_menu.rs#L32-L54)）：`new` 的第 39 行调用 `picker.set_popover()`，第 41-43 行完成 DismissEvent 的订阅转发。

#### 4.5.4 代码实践

**实践目标**：确认「叶子组件被 crate 外广泛复用」，体会 `pub mod` 的意义。

**操作步骤**：

1. 在仓库根目录执行：`grep -rln 'highlighted_match_with_paths::' crates/*/src/ | head -20`。
2. 执行：`grep -rln 'parts::project_scan_indicator\|project_scan_indicator' crates/*/src/ | head -20`。
3. 任选步骤 1 命中的一个 crate（如 project_symbols 或 outline），看它如何在 `render_match` 里构造 `HighlightedMatch`。

**需要观察的现象**：两个组件都在多个业务 crate 中被引用；使用方不需要 `use picker::…` 之外的任何路径。

**预期结果**：印证「`pub mod` 公开的组件是给全仓库复用的公共零件」。具体命中数量以本地执行为准（我未对本步骤做全量统计）。

#### 4.5.5 小练习与答案

**练习 1**：`parts` 和 `highlighted_match_with_paths` 为什么直接 `pub mod`，而 preview 用「私有模块 + 再导出」？
**答案**：preview 的类型（`Preview`、`Layout` 等）是 delegate 实现者必须实现的**协议**，需要在 crate 根有一个稳定的名字面；而 parts / highlighted_match_with_paths 是**自包含的展示组件**，独立成模块、按模块名使用（`picker::parts::project_scan_indicator`）反而更清晰，也不占用 crate 根的命名空间。

**练习 2**：`PickerPopoverMenu` 为什么要在构造函数里就调用 `set_popover()`，而不是让调用方自己设置？
**答案**：「以 popover 形态展示」是 `PickerPopoverMenu` 存在的前提，构造即切换保证了不可能出现「装在 PopoverMenu 里却仍按模态画背景」的不一致状态——把不变式（invariant）锁进构造函数，是防御性设计的典型做法。

**练习 3**：`HighlightedMatch::join` 里如果不维护 `byte_offset`，会出现什么 bug？
**答案**：第二段之后的高亮位置会按新字符串里的错误偏移去高亮——例如把 `foo/bar` 的 `bar` 段高亮位置 1（相对 bar 自身）直接用到拼接后的字符串上，实际高亮到的是 `foo` 中间的某个字符。分隔符长度也必须计入偏移。

## 5. 综合实践

**任务：手工绘制 picker crate 的完整模块依赖图，并标注全部对外可见的类型。**（本实践即本讲规格中的核心实践任务，纯源码阅读型，不需要改任何代码。）

**实践目标**：把 4.2-4.5 的局部观察合并成一张全局地图，做到「给你任何一个文件名，你就能说出它依赖谁、被谁依赖、对外暴露什么」。

**操作步骤**：

1. 打开 [src/picker.rs:L23-L43](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L23-L43)，抄下 9 条 `mod` 声明和全部 `pub use`。这是图的「骨架」。
2. 对 10 个模块文件逐一执行 `grep -n '^use crate::\|crate::[a-z_]*::' src/<文件>`，提取每条依赖边。
3. 在纸上（或任何画图工具里）按「叶子层 → 几何层 → 持久层 → 核心层 → 扩展层」画出箭头图。
4. 用不同颜色/记号标注：`pub mod` 模块、经 `pub use` 再导出的类型（`PreviewLayout`、`PickerAction`、`ErasedEditor`、`Preview`、`PreviewBackend`、`PreviewSource`、`PreviewUpdate`、`MatchLocation`、`HighlightedText`、`HighlightedTextBuilder`）、`pub(crate)` 内部条目（`Head`、`Shape`、`Centered`、`SizeBounds`、`PickerConfig` 等）。
5. 自测：遮住答案，回答三个问题——「render.rs 依赖哪些模块？」「shape.rs 被谁依赖？」「外部 crate 能否写出 `picker::shape::SizeBounds`？」
6. 交叉验证：运行 `cargo doc -p picker --no-deps` 并打开生成的 `picker` 文档页，你标注的「对外可见」清单应与文档收录的条目一致。

**需要观察的现象**：依赖箭头基本单向向下（扩展层指向核心层、核心层指向底座），唯一的「双向」是库根与扩展层（`pub use footer::PickerAction` ↔ `footer` 用 `crate::Picker`）。

**预期结果**：与 4.4.2 的分层图和 4.2.3 的可见性表一致。第 6 步的文档生成结果**待本地验证**。

## 6. 本讲小结

- picker 的库根是 `src/picker.rs`，由 [Cargo.toml:L11-L13](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/Cargo.toml#L11-L13) 的 `[lib] path` 指定（Zed 规范：库根用描述性命名而非 lib.rs）；`test-support` 是配合 `#[cfg(any(test, feature = "test-support"))]` 使用的空 feature，供 file_finder 等下游 crate 在测试中调用测试专用 API。
- crate 采用门面模式：9 个子模块中只有 3 个 `pub mod`（popover_menu、parts、highlighted_match_with_paths），其余全部私有；对外类型统一经 crate 根 `pub use` 再导出（含 `PreviewLayout`、`PickerAction` 与转发的 `ErasedEditor`、`HighlightedText`）。
- 模块依赖呈清晰分层：叶子（head/preview/parts/highlighted_match_with_paths）→ 几何（shape）→ 持久化（persistence）→ 核心总装（picker.rs）→ 扩展（footer/render/window_controls/popover_menu）；同 crate 内模块互相引用（如库根与 footer）完全合法。
- 渲染被拆到 render.rs（`impl Render for Picker<D>` 在那里），其嵌套子模块 `window_controls` 专管拖拽，`pub mod` 因父模块私有而实际仅 crate 内可见——可见性沿路径逐级收紧。
- 「可见性速查」是阅读任何 Rust crate 的通用技巧：先读库根的 `mod`/`pub use`，就能分清对外 API 与内部实现。

## 7. 下一步学习建议

- 下一讲（u1-l3）讲构建、测试与运行：`cargo build/test -p picker` 的具体用法、`init_test` 为什么必须初始化 settings/theme/editor，以及内嵌测试模块的结构——本讲 4.1 留下的伏笔在那里展开。
- 想提前热身的读者，可以通读 [src/render/window_controls.rs:L1-L57](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/render/window_controls.rs#L1-L57) 的 ASCII 拖拽示意图注释，它是 crate 里最好的「架构文档」。
- 进入单元二后，第一讲（u2-l1）将逐方法精读 `PickerDelegate` trait——那时你会真正用到本讲的地图：知道每个 trait 方法返回的类型（`Preview`、`HighlightedMatch` 等）分别定义在哪个模块。
