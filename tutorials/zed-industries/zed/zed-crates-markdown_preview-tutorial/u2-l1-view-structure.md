# 视图核心结构：MarkdownPreviewView 与两种预览模式

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐字段说出 `MarkdownPreviewView` 十三个字段各自的用途、生命周期，以及「谁负责更新它」。
2. 区分 `MarkdownPreviewMode::Default`（固定绑定一个编辑器）与 `MarkdownPreviewMode::Follow`（跟随当前活动编辑器）的行为差异，并说出它们在数据库中的序列化表示。
3. 讲清 `MarkdownPreviewEvent`、`EditorState`、`PreviewLinkTarget` 三个配套类型在整体设计中扮演的角色。
4. 读懂构造函数 `MarkdownPreviewView::new`，并能画出两种模式在构造时走的不同订阅分支（`observe_in` 与 `subscribe_in`）。

本讲是单元二的第一讲：后面所有机制（防抖更新、滚动同步、渲染、链接导航）都建立在这些字段和类型之上。

## 2. 前置知识

### 2.1 Entity 与 WeakEntity

在 GPUI 中，`Entity<T>` 是指向实体状态 `T` 的强句柄（类似智能指针）。`WeakEntity<T>` 是弱句柄：不阻止实体被释放，使用前要先 `upgrade()` 成 `Entity`。若两个实体互持强句柄（A 里有 `Entity<B>`，B 里又有 `Entity<A>`），引用计数永不归零，造成内存泄漏——所以 `MarkdownPreviewView` 对宿主工作区只保留 `WeakEntity<Workspace>`。

### 2.2 Subscription：用字段「保活」一个订阅

`cx.observe(...)` / `cx.subscribe(...)` 返回一个 `Subscription`。**订阅被 drop 时自动失效**。把它存进结构体字段（通常以下划线 `_` 开头命名，表示「持有但不读取」），订阅的生命周期就和实体绑定在一起了。`_markdown_subscription` 和 `EditorState::_subscription` 都是这个套路。

### 2.3 observe 与 subscribe 的区别

- `cx.observe(&entity, callback)`：目标实体每次调用 `cx.notify()`（状态可能变化、需要重渲染）时触发。**触发频繁，适合「任意变化都要看一眼」的场景**。
- `cx.subscribe(&entity, callback)`：目标实体显式 `cx.emit(event)` 时才触发。**事件驱动，只在关键节点发生**。

这个区别是理解本讲 4.3 节「为什么 Follow 用 `observe_in` 而 Default 用 `subscribe_in`」的钥匙。

### 2.4 EventEmitter：实体对外广播事件

一个实体声明 `impl EventEmitter<E> for T {}` 后，就可以在更新时 `cx.emit(event)` 向外广播 `E` 类型事件，其他实体用 `cx.subscribe` 接收。`MarkdownPreviewView` 声明了 `EventEmitter<MarkdownPreviewEvent>` 和 `EventEmitter<SearchEvent>` 两种事件（见 [src/markdown_preview_view.rs:1499-1500](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L1499-L1500)），前者驱动持久化，后者驱动搜索。

### 2.5 Option\<Task\>：「进行中的工作」槽位

`Task<R>` 是一个可等待的未来；**drop 即取消**。`pending_update_task: Option<Task<Result<()>>>` 这个字段语义是「当前是否有一个防抖更新任务在飞行中」——槽位有值就表示任务还活着。这是 GPUI 代码里管理异步工作的常见手法（下一讲 u2-l4 防抖更新会展开）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `crates/markdown_preview/src/markdown_preview_view.rs` | 预览视图的全部实现（约 3700 行，近半是测试） | 结构体定义、模式枚举、配套类型、`new`/`set_editor`、序列化中 mode 的存取 |
| `crates/markdown/src/markdown.rs` | markdown crate：解析与渲染 Markdown 的通用组件 | `Markdown` 实体的字段构成、`reset`/`is_parsing` 接口 |

回忆 u1-l1 的结论：markdown_preview 是「胶水 + 面板」crate，解析交给 markdown crate。本讲 4.4 节会看清这条边界具体长什么样。

## 4. 核心概念与源码讲解

### 4.1 MarkdownPreviewView 结构体：十三个字段各司其职

#### 4.1.1 概念说明

`MarkdownPreviewView` 是一个 GPUI 视图实体：它既持有「显示什么」（markdown 实体），也持有「绑定谁」（active_editor）、「怎么交互」（focus_handle、scroll_handle、hovered_url）和「正在进行什么」（pending_update_task、markdown_parse_pending）。

理解这个结构体的诀窍是**按数据的流向分组**，而不是按声明顺序背：

- **上游绑定**：`workspace`、`active_editor`、`mode` —— 我在哪个工作区里、绑着哪个编辑器、用什么策略绑。
- **内容与渲染**：`markdown`、`_markdown_subscription`、`image_cache`、`scroll_handle` —— 预览本体。
- **同步状态**：`active_source_index`、`base_directory` —— 与源编辑器对齐的光标偏移、相对路径基准目录。
- **瞬态工作**：`pending_update_task`、`hovered_url`、`markdown_parse_pending`、`focus_handle` —— 飞行中的任务、悬停提示、解析标志、焦点。

#### 4.1.2 核心流程

整体数据流（本讲先建立全景，细节在后续各讲展开）：

```text
源编辑器 Editor（Entity<Editor>）
    │  EditorEvent（Edited / SelectionsChanged / ...）
    ▼
MarkdownPreviewView（本讲主角，持有 EditorState 订阅）
    │  防抖后读出 buffer 全文，调 markdown.reset(text)
    ▼
markdown 实体（Entity<Markdown>，内容引擎）
    │  后台解析 → parsed_markdown → cx.notify()
    ▼
Render::render 装配元素树 → 屏幕上的预览面板
```

反向还有两条细流：用户在预览里点击/滚动 → `scroll_handle` 与各类回调；模式切换与工作区事件 → `set_editor` 重新绑定。

#### 4.1.3 源码精读

结构体定义在 [src/markdown_preview_view.rs:56-74](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L56-L74)：

```rust
const REPARSE_DEBOUNCE: Duration = Duration::from_millis(200);

pub struct MarkdownPreviewView {
    workspace: WeakEntity<Workspace>,
    active_editor: Option<EditorState>,
    focus_handle: FocusHandle,
    markdown: Entity<Markdown>,
    _markdown_subscription: Subscription,
    active_source_index: Option<usize>,
    scroll_handle: ScrollHandle,
    image_cache: Entity<RetainAllImageCache>,
    base_directory: Option<PathBuf>,
    pending_update_task: Option<Task<Result<()>>>,
    hovered_url: Option<SharedString>,
    mode: MarkdownPreviewMode,
    /// Search results depend on the parsed markdown, which lags behind the source while a
    /// background parse is in flight. Tracked so matches can be invalidated once it lands.
    markdown_parse_pending: bool,
}
```

这是本讲最核心的代码。逐字段的用途、写入点与生命周期：

| # | 字段 | 用途 | 谁在什么时候更新它 | 生命周期 |
| --- | --- | --- | --- | --- |
| 1 | `workspace` | 宿主工作区的弱引用，用于重绑编辑器、序列化取路径 | `new` 构造时一次性写入，之后只读 | 与视图同生共死 |
| 2 | `active_editor` | 当前绑定的源编辑器及其事件订阅（见下方 `EditorState`） | `set_editor`：初始绑定、Follow 跟随切换、Default 重绑 | 每次换绑整体替换 |
| 3 | `focus_handle` | 键盘焦点句柄，render 里 `track_focus` 用 | `new` 里 `cx.focus_handle()` 创建；之后由 gpui 焦点系统驱动 | 与视图同生共死 |
| 4 | `markdown` | 内容引擎实体：源文本 + 解析结果 + 渲染状态 | `new` 里创建，实体句柄不再替换；**内部状态**由 `markdown.reset(...)` 等更新 | 与视图同生共死 |
| 5 | `_markdown_subscription` | 观察 markdown 实体（解析状态落地）的订阅 | `new` 里建立，字段只是保活，无人读取 | 与视图同生共死 |
| 6 | `active_source_index` | 源文本字节/字符偏移，表示「光标在源文件的哪里」，驱动滚动同步 | `sync_preview_to_source_index`（[L635](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L635)） | 随光标频繁变化 |
| 7 | `scroll_handle` | 驱动滚动容器的句柄，8 个滚动动作靠它 | `new` 里创建；`scroll_*` 动作方法操作它 | 与视图同生共死 |
| 8 | `image_cache` | 图片缓存，render 里 `div().image_cache(...)` 挂载（[L1649](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L1649)） | `new` 里 `RetainAllImageCache::new(cx)` 创建 | 与视图同生共死 |
| 9 | `base_directory` | 相对路径图片/链接的解析基准（源文件所在目录） | `set_editor`（[L482](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L482)）与 `FileHandleChanged` 事件（[L455-457](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L455-L457)） | 随源文件换绑/改名变化 |
| 10 | `pending_update_task` | 飞行中的防抖更新任务；`Some` 即「已有任务」 | `update_markdown_from_active_editor`（[L546](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L546)） | 200ms 级别，随任务完成被替换 |
| 11 | `hovered_url` | 当前悬停链接的 URL，驱动 `LinkPreview` 气泡 | `on_url_hover` 回调（[L1020-1027](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L1020-L1027)）写入；`set_editor` 换绑时清空（[L483](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L483)） | 悬停级别，秒级 |
| 12 | `mode` | Default / Follow，绑定策略 | `new` 参数确定后**永不改变** | 与视图同生共死 |
| 13 | `markdown_parse_pending` | 后台解析是否在飞行中，配合搜索结果失效 | `_markdown_subscription` 的 observe 回调（[L317-321](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L317-L321)） | 解析级别，毫秒到秒级 |

三个配套类型也定义在同一片区域，各自回答一个设计问题：

**① `EditorState` —— 「绑定一个编辑器」需要带上什么？** 见 [src/markdown_preview_view.rs:100-103](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L100-L103)：

```rust
struct EditorState {
    editor: Entity<Editor>,
    _subscription: Subscription,
}
```

答案：编辑器句柄 + 对它的事件订阅。两者必须绑在一起存——换编辑器时旧订阅要随旧 `EditorState` 一起被 drop 掉，否则会收到已解绑编辑器的事件。这个「句柄 + 订阅打包替换」的模式保证了 `set_editor` 的原子性。

**② `MarkdownPreviewEvent` —— 视图要对外广播什么？** 见 [src/markdown_preview_view.rs:105-109](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L105-L109)：

```rust
#[derive(Clone, Copy, Debug)]
pub enum MarkdownPreviewEvent {
    SourceEditorChanged,
    SourceFileHandleChanged,
}
```

只有两个变体：换绑了源编辑器、源文件句柄变了（如另存为/改名）。它们是 `Copy` 的轻量信号，唯一的消费者是 workspace 的序列化机制——`should_serialize` 恰好只匹配这两个事件（[src/markdown_preview_view.rs:2024-2029](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L2024-L2029)）。也就是说：**这个事件的存在理由是「告诉持久化层：该把新路径/新绑定写进数据库了」**（u3-l3 会展开）。

**③ `PreviewLinkTarget` —— 预览内导航的目标有几种？** 见 [src/markdown_preview_view.rs:111-114](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L111-L114)：

```rust
enum PreviewLinkTarget {
    Heading(SharedString),
    Position { row: u32, column: u32 },
}
```

私有枚举（没有 `pub`）：要么滚到某个标题锚点（`#fragment`），要么把编辑器光标定位到某行某列（`file.rs:12` 这类链接）。它是链接点击链路（u2-l8）的中间产物，`navigate_to_link_target` 消费它（[L650-663](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L650-L663)）。它和结构体字段无关，但和 `markdown` 实体的 `pending_heading_scroll` 能力配套，本讲只需记住「两类目标」。

#### 4.1.4 代码实践

**实践一：给每个字段写一句「生命周期 + 谁更新」注释。**

1. **实践目标**：把 4.1.3 的字段表内化成自己的笔记，做到不查表也能说出任意字段的写入点。
2. **操作步骤**：
   - 打开 `crates/markdown_preview/src/markdown_preview_view.rs`，定位到第 58 行的 `pub struct MarkdownPreviewView`。
   - 对照上面字段表里的「谁在什么时候更新它」一列，在编辑器里（不要改动仓库源码，可以在你自己的 fork 或草稿里）为 13 个字段各写一行中文注释，格式如：`// 生命周期：与视图同生共死；写入点：仅 set_editor()。`
   - 对每个声明的写入点，用编辑器的「转到定义 / 查找引用」验证：比如 `active_source_index` 的写入点在第 635 行，确认它确实只在这一处被赋值。
3. **需要观察的现象**：你会发现一半以上的字段写入点集中在两三个函数里（`new` 和 `set_editor`），这就是本 crate 状态收敛的证据。
4. **预期结果**：13 个字段全部有注释，且每条注释的行号引用都能在源码中找到对应赋值语句。

**待本地验证**：字段写入点的完整清单依赖全局搜索（`self.<字段> =`），建议本地用 `grep -n "self.active_source_index =" crates/markdown_preview/src/markdown_preview_view.rs` 逐一核实，避免遗漏。

#### 4.1.5 小练习与答案

**练习 1**：`workspace` 为什么是 `WeakEntity<Workspace>` 而不是 `Entity<Workspace>`？

**参考答案**：Workspace 持有所有 Pane，Pane 持有包括预览在内的所有条目，即 Workspace → … → MarkdownPreviewView 已经是一条强引用链。如果视图再强引用回 Workspace，就形成循环引用，两个实体都无法释放。弱引用打破了环；使用时通过 `upgrade()` 拿临时强引用，取不到就说明工作区已销毁（`new` 里的 `log::error!("Failed to listen to workspace updates")` 分支就是升级失败的兜底）。

**练习 2**：`mode` 字段会在视图存活期间改变吗？从哪里可以证明？

**参考答案**：不会。全文件搜索 `self.mode` 的赋值只有 `new` 中构造时的 `mode`（第 331 行，来自构造参数）；此外所有出现都是读取（如 `find_existing_independent_preview_item_idx` 的第 216 行、`serialize` 的第 2017 行）。一个预览从创建到销毁绑定策略不变——想换策略就关掉重开一个。

**练习 3**：为什么 `EditorState` 要把 `_subscription` 和 `editor` 打包在一起，而不是在结构体上另设一个 `editor_subscription: Option<Subscription>` 字段？

**参考答案**：因为订阅必须严格随「被订阅的编辑器」同生共死。打包后，`set_editor` 里 `self.active_editor = Some(EditorState { editor, _subscription })` 这一句赋值就同时完成「换编辑器 + 换订阅 + 丢掉旧订阅」三件事，旧订阅随旧 `EditorState` 被 drop 而自动失效，不可能出现「订阅着 A 却显示 B」的错位状态。拆成两个字段则要小心维护两者的一致性，容易出 bug。

### 4.2 MarkdownPreviewMode：Default 与 Follow 的行为差异与序列化表示

#### 4.2.1 概念说明

同一个 `MarkdownPreviewView` 结构体，靠 `mode` 字段呈现两种截然不同的使用体验：

- **`Default`（默认模式）**：`cmd-shift-v` 打开的普通预览。它和「触发打开动作的那个编辑器」**固定绑定**——之后你切到别的 Markdown 文件，预览纹丝不动。它的身份由「源 buffer」定义，所以对同一文件再次触发打开动作会**复用**已有预览（`find_existing_independent_preview_item_idx` 只匹配 Default 模式，[L216](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L216)）。
- **`Follow`（跟随模式）**：`OpenFollowingPreview` 动作创建的预览。它**跟随当前活动编辑器**——你切到哪个 Markdown 文件它就显示哪个。整个面板同时只该有一个 Follow 预览，所以该动作先找已有的 Follow 预览激活，找不到才新建（[L132-155](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L132-L155)）。

一个记忆口诀：**Default 是「一个文件的面板」，Follow 是「一个工作流的面板」**。

#### 4.2.2 核心流程

两种模式从创建到持久化的差异对照：

```text
Default 模式：
  OpenPreview / OpenPreviewToTheSide
    └─ create_markdown_view(mode=Default)
         └─ new(): subscribe_in(workspace) 监听 ItemAdded/ItemRemoved
              └─（仅当绑定编辑器变成孤儿时）find_canonical_editor 重绑
  序列化：abs_path + mode=0，恢复后仍是「该文件的预览」

Follow 模式：
  OpenFollowingPreview
    └─ 已有 Follow 预览？→ 激活之；否则 create_following_markdown_view(mode=Follow)
         └─ new(): observe_in(workspace) 监听活动条目变化
              └─ workspace_updated → set_editor(新的 markdown 编辑器)
  序列化：abs_path(最近跟随的文件) + mode=1，恢复后继续跟随
```

#### 4.2.3 源码精读

枚举与数据库表示见 [src/markdown_preview_view.rs:76-98](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L76-L98)：

```rust
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum MarkdownPreviewMode {
    /// The preview will always show the contents of the provided editor.
    Default,
    /// The preview will "follow" the currently active editor.
    Follow,
}

impl MarkdownPreviewMode {
    fn to_db(self) -> i64 {
        match self {
            Self::Default => 0,
            Self::Follow => 1,
        }
    }

    fn from_db(value: i64) -> Self {
        match value {
            1 => Self::Follow,
            _ => Self::Default,
        }
    }
}
```

注意 `from_db` 的兜底分支：任何非 1 的值（包括未来的新枚举值）都退回 `Default`。这是「老版本数据库里读到未知模式也不崩溃」的防御式写法——枚举可能演进，但读取永远安全。

两个工厂函数唯一区别就是传入的 mode，见 [src/markdown_preview_view.rs:249-283](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L249-L283)：

```rust
pub fn create_markdown_view(/* ... */) -> Entity<MarkdownPreviewView> {
    // ...
    MarkdownPreviewView::new(MarkdownPreviewMode::Default, editor, /* ... */)
}

fn create_following_markdown_view(/* ... */) -> Entity<MarkdownPreviewView> {
    // ...
    MarkdownPreviewView::new(MarkdownPreviewMode::Follow, editor, /* ... */)
}
```

**mode 如何进入持久化**：`serialize` 把 `self.mode.to_db()` 连同源文件绝对路径一起写入 SQLite（[src/markdown_preview_view.rs:1998-2022](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L1998-L2022)，关键行 2017：`let mode = self.mode.to_db();`）。`deserialize` 读回时用 `MarkdownPreviewMode::from_db(mode_value)` 还原（[src/markdown_preview_view.rs:1955-1960](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L1955-L1960)），再把它原样传给 `MarkdownPreviewView::new(mode, editor, ...)`（[L1983](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L1983)）——所以重启后 Follow 预览恢复的仍是 Follow 预览。

一个值得品味的行为：Follow 预览序列化的是**它最近跟随的文件路径**。测试 `follow_preview_serialized_path_updates_when_followed_editor_changes` 明确断言了这一点（[src/markdown_preview_view.rs:3178-3307](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L3178-L3307)）：先跟随 `a.md` 序列化断言存了 `/dir/a.md`，再 `set_editor(editor_b)` 后断言数据库变成了 `/dir/b.md`——失败信息写着 "a Follow preview should persist the source editor it most recently followed"。

#### 4.2.4 代码实践

**实践二：用两个测试读出两种模式的语义差异。**

1. **实践目标**：不看文档、只看测试断言，说出 Default 与 Follow 的行为契约。
2. **操作步骤**：
   - 阅读 [src/markdown_preview_view.rs:3310](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L3310) 的测试 `default_preview_stays_bound_to_invoking_editor_across_splits`——从名字提炼 Default 的契约：「拆分窗格后仍绑定触发它的那个编辑器」。
   - 阅读 [src/markdown_preview_view.rs:3178-3307](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L3178-L3307) 的 `follow_preview_serialized_path_updates_when_followed_editor_changes`——提炼 Follow 的契约：「换绑即换持久化路径」。
   - （可选，需本地 Rust 环境）运行：`cargo test -p markdown_preview follow_preview_serialized -- --nocapture`，观察断言通过。
3. **需要观察的现象**：两个测试对「预览绑定谁」的断言方向完全相反——一个断言**不变**（跨窗格仍绑原编辑器），一个断言**跟随变**（set_editor 后路径立刻换）。
4. **预期结果**：能写出两句契约：Default = 绑定创建时刻的编辑器，除重绑规范编辑器外不换；Follow = 绑定当前活动编辑器，随时可换。

**待本地验证**：`cargo test` 需要 Zed 全仓依赖编译，首次耗时较长；若环境受限，仅做源码阅读也完全能完成本实践。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `find_existing_independent_preview_item_idx` 要显式排除 Follow 预览（`view.read(cx).mode == MarkdownPreviewMode::Default` 这个条件）？

**参考答案**：这个函数服务于 `OpenPreview` 的「复用已有预览」逻辑。如果允许匹配 Follow 预览，对一个文件 A 触发 OpenPreview 可能激活的却是正在跟随文件 B 的 Follow 预览，且激活后它不会改变跟随目标——用户看到的是 B 的内容，完全违背「打开 A 的预览」的意图。排除 Follow 后，匹配的都是「绑定这个 buffer 的独立预览」，语义精确。

**练习 2**：`MarkdownPreviewMode` 存数据库为什么用 `i64` 而不是字符串 `"default"`/`"follow"`？

**参考答案**：SQLite 列类型偏好整数，`to_db`/`from_db` 把枚举与存储格式解耦——枚举重命名变体不影响已有数据，存储格式调整也不侵入枚举使用处。`from_db` 的 `_ => Self::Default` 兜底同时保证了前向兼容（读到未知整数不 panic）。代价是可读性略差，这在内部数据库中是常见取舍。

**练习 3**：Follow 预览重启恢复后，`active_editor` 绑定的是什么？它会立刻开始跟随吗？

**参考答案**：绑定的是 `deserialize` 中用恢复的 buffer 新建的编辑器（`Editor::for_buffer` + `MarkdownPreviewView::new(mode, ...)`，[L1979-1984](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L1979-L1984)）。会立刻恢复跟随能力：`new` 中 Follow 分支注册的 `observe_in(workspace)` 在构造时就已挂上，之后工作区活动条目一变，`workspace_updated` 就会 `set_editor` 切到用户实际交互的编辑器。

### 4.3 构造函数 new：两种模式走不同的订阅分支

#### 4.3.1 概念说明

`new` 是理解两种模式分叉的最好入口：**前半段两种模式完全相同**（创建内容引擎、初始化字段、绑定初始编辑器），**后半段按 mode 走两条不同的订阅分支**：

- Follow 分支用 `cx.observe_in(workspace, ...)`——观察工作区的每次 notify，即「活动条目变了吗？变就换绑」。
- Default 分支用 `cx.subscribe_in(workspace, ...)`——只订阅工作区的**事件**，且只关心 `ItemAdded`/`ItemRemoved` 两种，用于把孤儿编辑器重绑为规范编辑器。

为什么不对称？源码注释（[L356-358](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L356-L358)）给出理由：`observe` 会在工作区**每次** `cx.notify()` 时触发——而工作区 notify 极其频繁（包括每次光标移动引发的界面刷新）。Default 模式的重绑检查根本不需要这么高的频率，只在「条目增删」这种事件节点检查即可，用 `subscribe` 把检查从光标移动的热路径上摘下来。Follow 模式则恰恰需要高频感知活动条目变化，`observe` 的语义正合适。

#### 4.3.2 核心流程

`new`（[src/markdown_preview_view.rs:285-368](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L285-L368)）的执行步骤：

1. 创建 `markdown` 实体（空内容 + 完整解析选项，见 4.4 节）。
2. 初始化 13 个字段（含挂上 `_markdown_subscription`）。
3. 调用 `set_editor(active_editor, ...)` 完成首次绑定（见 4.3.3 第三段）。
4. **按 mode 分支**：

```text
match mode {
    Follow  => cx.observe_in(workspace, ...)   // 每次 workspace notify：
    //        读 active_item → 是 markdown 编辑器且不是自己？→ set_editor

    Default => cx.subscribe_in(workspace, Self::on_workspace_event)
    //        只在 ItemAdded / ItemRemoved 事件：
    //        find_canonical_editor(找同一 buffer 的规范编辑器)
    //        → 与当前不同？→ set_editor
}
```

两个分支殊途同归于 `set_editor`——它是**唯一**的换绑入口。

#### 4.3.3 源码精读

先看公共前半段，创建内容引擎与初始化字段（[src/markdown_preview_view.rs:293-335](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L293-L335)）：

```rust
cx.new(|cx| {
    let markdown = cx.new(|cx| {
        Markdown::new_with_options(
            SharedString::default(),
            Some(language_registry),
            None,
            MarkdownOptions {
                parse_html: true,
                render_mermaid_diagrams: true,
                parse_heading_slugs: true,
                render_metadata_blocks: true,
                ..Default::default()
            },
            cx,
        )
    });
    let mut this = Self {
        // ...13 个字段初始化，含 _markdown_subscription: cx.observe(&markdown, ...)...
        mode,
        markdown_parse_pending: false,
    };

    this.set_editor(active_editor, window, cx);
```

`_markdown_subscription` 的回调体（[L313-323](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L313-L323)）做两件事：调用 `sync_active_root_block`（把 `active_source_index` 同步给 markdown 实体，用于高亮当前区块），以及维护 `markdown_parse_pending`——解析从「在飞行中」变为「落地」的那一刻，向搜索系统广播 `SearchEvent::MatchesInvalidated`（搜索结果基于旧解析，必须作废重算）。

然后是本讲的关键分叉（[src/markdown_preview_view.rs:337-364](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L337-L364)）：

```rust
match mode {
    MarkdownPreviewMode::Follow => {
        if let Some(workspace) = &workspace.upgrade() {
            cx.observe_in(workspace, window, |this, workspace, window, cx| {
                let item = workspace.read(cx).active_item(cx);
                this.workspace_updated(item, window, cx);
            })
            .detach();
        } else {
            log::error!("Failed to listen to workspace updates");
        }
    }
    MarkdownPreviewMode::Default => {
        // After workspace restoration the bound editor may be an orphan that
        // wraps the right buffer but isn't the canonical Editor instance in
        // any pane. Re-binding to the workspace's editor for our buffer is
        // what restores cursor-driven scroll sync — `SelectionsChanged` only
        // fires from the editor the user actually interacts with.
        //
        // Subscribing to `workspace::Event` (rather than `observe`) keeps the
        // rebind check off the cursor-move hot path; `observe` would fire on
        // every workspace `cx.notify`.
        if let Some(workspace) = &workspace.upgrade() {
            cx.subscribe_in(workspace, window, Self::on_workspace_event).detach();
        }
    }
}
```

注意两个分支都以 `.detach()` 结尾：这些工作区级监听不存字段，显式脱管让它们活到实体销毁（`Context` 上的订阅会随实体一起清理）。

**Follow 分支的落点** `workspace_updated`（[src/markdown_preview_view.rs:370-383](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L370-L383)）：条件是「活动条目存在、不是预览自己（防止自己激活时把自己换掉）、能当作 Editor、且是 Markdown 文件」——四个条件全过才 `set_editor`。

**Default 分支的落点** `on_workspace_event` + `find_canonical_editor`（[src/markdown_preview_view.rs:494-532](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L494-L532)）：先把事件过滤到只剩 `ItemAdded | ItemRemoved`，再在「工作区中所有包着同一 buffer 的编辑器」里找规范实例——如果当前绑定的编辑器还在任何窗格中就保持不变，否则换成第一个同 buffer 的编辑器。注释解释了动机：工作区恢复后 `deserialize` 新建的那个编辑器可能是「孤儿」（不在任何窗格里），而 `SelectionsChanged` 事件只会从用户实际操作的编辑器发出，不重绑就收不到光标事件、滚动同步会失灵。

**两条分支共同的换绑入口** `set_editor`（[src/markdown_preview_view.rs:436-492](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L436-L492)）值得整段精读，它的步骤：

1. **幂等短路**：要绑的编辑器和当前相同就直接返回（L437-441）——Follow 模式下活动条目频繁变化，这一行挡住了大量无效换绑。
2. `cx.subscribe_in(&editor, ...)` 订阅编辑器事件，按事件分类响应：`Edited`/`BufferEdited`/`DirtyChanged`/`BuffersEdited` → 触发防抖更新；`FileHandleChanged` → 刷新 `base_directory` 并广播 `SourceFileHandleChanged`；`SelectionsChanged` → 滚动同步到光标位置；其余忽略。
3. 更新 `base_directory`、清空 `hovered_url`，把「编辑器 + 新订阅」打包进 `active_editor`（旧订阅随之销毁）。
4. 立即 `update_markdown_from_active_editor(false, true, ...)` 做一次**非防抖**的立即更新（新文件要马上显示）。
5. 若之前已有绑定（`had_active_editor`），广播 `MarkdownPreviewEvent::SourceEditorChanged` → 触发持久化把新路径写库。

#### 4.3.4 代码实践

**实践三：画出两种模式在 `new` 中的订阅分支对照图。**

1. **实践目标**：用一张图固化「公共前半段 + 模式分叉 + 殊途同归到 set_editor」的结构。
2. **操作步骤**：
   - 通读 [src/markdown_preview_view.rs:285-368](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L285-L368)，确认前 3 步（创建 markdown、初始化字段、set_editor）在 `match mode` 之前。
   - 在自己的笔记里画出如下对照图（或用 mermaid）：

```text
                 MarkdownPreviewView::new(mode, editor, workspace, ...)
                        │
        ┌───────────────┼────────────────────────┐
        │ 公共：cx.new → markdown 实体（空文本+4个解析选项）
        │ 公共：初始化 13 字段（含 _markdown_subscription = observe(markdown)）
        │ 公共：set_editor(初始编辑器)  ← 首次绑定 + 立即内容更新
        └───────────────┬────────────────────────┘
                        │ match mode
        ┌───────────────┴───────────────┐
   mode = Follow                   mode = Default
        │                               │
  observe_in(workspace)        subscribe_in(workspace)
  每次 workspace notify         仅 ItemAdded / ItemRemoved 事件
        │                               │
  workspace_updated()          on_workspace_event()
  活动条目是 md 编辑器且≠自己    find_canonical_editor()
        │                      找同 buffer 的在窗格编辑器，不同才换
        └───────────────┬───────────────┘
                        ▼
                set_editor(new_editor)
        （幂等短路 → 订阅新编辑器事件 → 打包 EditorState →
          立即更新内容 → 若非首次绑定，emit SourceEditorChanged）
```

   - 画完后回到源码核对三处细节：① Follow 的 observe 回调里 `item.item_id() != cx.entity_id()` 的自我排除；② Default 注释中「为什么不用 observe」的性能理由；③ 两个分支 `.detach()` 的位置。
3. **需要观察的现象**：图中「公共 → 分叉 → 汇聚」的形状清晰；能指出 `set_editor` 是唯一汇聚点。
4. **预期结果**：一张包含上述节点与要点的对照图；对着图能口头复述两种模式从构造到换绑的全过程。

**待本地验证**：图中每个节点都对应源码真实行号（285-368、370-383、436-492、494-532），建议画完后逐节点点开链接核对。

#### 4.3.5 小练习与答案

**练习 1**：Follow 分支用 `observe_in`，Default 分支用 `subscribe_in`，源码注释给出的核心理由是什么？

**参考答案**：`observe` 在工作区每次 `cx.notify()` 时都触发，而工作区 notify 在光标移动等高频路径上也会发生；Default 模式的重绑检查只需要在条目增删时进行，用 `subscribe` 只监听 `workspace::Event::ItemAdded | ItemRemoved`，把检查从光标热路径上移除。Follow 模式需要的恰恰是「活动条目一变就要感知」的高频语义，所以用 `observe` 是正确取舍。

**练习 2**：`workspace_updated` 为什么要检查 `item.item_id() != cx.entity_id()`？

**参考答案**：预览自己被激活时也会成为 workspace 的活动条目并触发 observe 回调；不排除的话，Follow 预览会把「自己」当作候选编辑器去 `act_as::<Editor>`。虽然预览不是 Editor、后面的检查大概率也会拦住，但显式的 id 比较既是最便宜的早退条件，也把意图（「别跟随我自己」）写进了代码。

**练习 3**：`set_editor` 第 4 步的 `update_markdown_from_active_editor(false, true, ...)` 中两个布尔参数分别是什么意思？为什么这里传 `false`？

**参考答案**：第一个是 `wait_for_debounce`（是否等 200ms 防抖），第二个是 `should_reveal`（更新后是否把预览滚动到光标处）。这里传 `false` 是因为换绑意味着显示一份**新文件**——必须立即渲染，等防抖会让用户在切换后看到一段时间的旧内容或空白。编辑中的增量更新（`Edited` 事件）才传 `true` 走防抖。这套参数的完整机制是下一讲 u2-l4 的主题。

### 4.4 markdown 实体：预览视图的「内容引擎」

#### 4.4.1 概念说明

`Entity<Markdown>` 是 `MarkdownPreviewView` 最重要的字段：预览显示的一切内容都住在它里面。markdown crate 是通用组件（欢迎页、设置编辑器等处也在用），`Markdown` 实体封装了：

- **源文本与解析结果**：`source` + `parsed_markdown`（后台解析产出）。
- **渲染交互状态**：选中、按下的链接、代码块复制状态、搜索高亮等。
- **滚动/定位请求**：`autoscroll_request`、`pending_heading_scroll`（配合本讲的 `PreviewLinkTarget`）。

职责边界的意义：**视图管生命周期与绑定，markdown 实体管内容与解析**。预览视图不自己解析 Markdown，也不存解析树；它只在合适的时机调 `reset` 塞进新文本、订阅其变化。这让「解析在后台线程进行、解析期间旧内容保持可见」这类复杂策略完全留在 markdown crate 内实现。

#### 4.4.2 核心流程

内容更新的闭环（简化版，u2-l4 展开）：

```text
编辑器事件 →（防抖 200ms）→ markdown.reset(全文)
    → reset 内部：源文本不同 → 保留旧 parsed_markdown → cx.self().parse(cx) 后台解析
    → 解析完成 → markdown 实体 cx.notify()
    → 预览视图的 _markdown_subscription 触发：
        sync_active_root_block + 更新 markdown_parse_pending（落地瞬间发搜索失效事件）
    → 视图重渲染读取新 parsed_markdown
```

#### 4.4.3 源码精读

`Markdown` 实体的字段定义在 [crates/markdown/src/markdown.rs:449-482](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown/src/markdown.rs#L449-L482)（节选关键项）：

```rust
pub struct Markdown {
    source: SharedString,
    // ...pressed_link / pressed_footnote_ref / selection 等交互状态...
    autoscroll_request: Option<usize>,
    pending_heading_scroll: Option<SharedString>,
    active_root_block: Option<usize>,
    parsed_markdown: ParsedMarkdown,
    // ...
    should_reparse: bool,
    pending_parse: Option<Task<()>>,
    // ...mermaid 图状态、代码块复制/换行状态、右键菜单上下文...
    search_highlights: Rc<[Range<usize>]>,
    active_search_highlight: Option<usize>,
}
```

对照预览视图可以建立映射：`pending_parse` 对应视图侧的 `markdown_parse_pending`（一个在引擎内部、一个在视图侧追踪），`active_root_block` 由视图的 `sync_active_root_block`（[src/markdown_preview_view.rs:644-648](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L644-L648)）经 `set_active_root_for_source_index` 写入，`pending_heading_scroll` 服务 `PreviewLinkTarget::Heading` 的锚点跳转。

预览视图在 `new` 里对它的初始化（见 4.3.3 第一段引用）有两个值得注意的点：初始内容是 `SharedString::default()`（空字符串，等 `set_editor` 的首次更新填充）；四个解析选项全开——HTML 解析、Mermaid 图、标题锚点 slug（锚点跳转的前提）、元数据块渲染，刻画了「预览面板要尽可能完整呈现文档」的产品意图。

`reset` 是视图驱动内容更新的唯一正门（[crates/markdown/src/markdown.rs:1011-1037](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown/src/markdown.rs#L1011-L1037)）：

```rust
pub fn reset(&mut self, source: SharedString, cx: &mut Context<Self>) {
    if &source == self.source() {
        if self.pending_parse.is_none() {
            // 源文本没变且无解析在飞：兑现之前挂起的锚点/偏移滚动
            if let Some(slug) = self.pending_heading_scroll.take() { /* ... */ }
            else if let Some(source_index) = self.pending_autoscroll.take() { /* ... */ }
        }
        return;                       // ① 内容相同：短路，不重解析
    }
    // ② 内容不同：换源、清交互与滚动状态...
    self.source = source;
    // ...
    // Don't clear parsed_markdown here - keep existing content visible until new parse completes
    self.parse(cx);                   // ③ 后台解析，旧内容保持可见
}
```

三段结构：**① 内容没变就短路**（还顺手处理挂起的滚动请求——防抖期间多次 `reset` 同文本只算一次）；**② 换内容时清空的是交互与滚动状态**；**③ 关键注释：不清 `parsed_markdown`**——旧解析结果留到新解析完成才替换，用户在连续输入时看到的是「旧版面 + 延迟更新」而不是闪烁空白。

最后是与本讲直接相关的两个查询接口：`is_parsing`（[crates/markdown/src/markdown.rs:910](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown/src/markdown.rs#L910)）供 `_markdown_subscription` 回调判断解析状态，`set_active_root_for_source_index`（[crates/markdown/src/markdown.rs:996-1009](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown/src/markdown.rs#L996-L1009)）内部有「目标区块没变就早退」的去抖，避免光标在同一段落内移动时反复重渲染。

#### 4.4.4 代码实践

**实践四：源码阅读型实践——追踪一次换绑中的 markdown 实体状态变化。**

1. **实践目标**：把「视图字段 ↔ markdown 实体字段」的映射关系走一遍真实调用链。
2. **操作步骤**：
   - 从 `set_editor`（[L436](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L436)）出发，跟着第 488 行的 `update_markdown_from_active_editor(false, true, ...)` → `schedule_markdown_update`（[L556](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L556)）一路读到对 `markdown.reset(...)` 的调用，记下这条链上经过的每个函数名。
   - 打开 [crates/markdown/src/markdown.rs:1011-1037](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown/src/markdown.rs#L1011-L1037)，标注 reset 三段逻辑（短路 / 清状态 / 保留旧解析）各自保护了什么。
   - 回到视图侧 [L313-323](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L313-L323)，写下 markdown 实体 notify 后视图做了哪两件事。
3. **需要观察的现象**：这条链清晰地展示了「视图不碰解析、markdown 不管编辑器」的边界——链上从编辑器读文本、把文本交给引擎、订阅引擎变化，三种职责各属一方。
4. **预期结果**：得到一张 `set_editor → update_markdown_from_active_editor → schedule_markdown_update → markdown.reset → parse → notify → _markdown_subscription` 的完整调用链笔记。

**待本地验证**：调用链中间环节（如 `schedule_markdown_update` 内部对 reset 的具体调用位置）行号建议在本地源码中再核对一遍，因为该函数体较长。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `MarkdownPreviewView` 不自己保存 `ParsedMarkdown`，而是每次通过 `Entity<Markdown>` 访问？

**参考答案**：`ParsedMarkdown` 是 markdown crate 的内部类型，解析时机（后台线程、防抖、增量）和缓存策略都封装在 `Markdown` 实体里。视图若自己持有一份，就必须复制解析逻辑、维护两份一致性。通过实体持有，视图只做「塞文本 + 订阅变化 + 读结果」，解析策略升级（比如未来换成增量解析）对视图零改动——这是典型的所有权边界设计。

**练习 2**：`reset` 在源文本相同时为什么要短路返回，而不是无脑重新解析？

**参考答案**：调用方是防抖更新链路：用户停止输入 200ms 后任务醒来读全文调 reset，文本很可能与上次相同（比如只有选区变化）。短路避免了完全相同的重复解析开销；同时这个分支还承担了「兑现挂起的滚动请求」的职责——`pending_heading_scroll`/`pending_autoscroll` 挂起时若触发全量重解析会丢失滚动目标，趁短路分支处理掉最合适。

**练习 3**：预览视图的 `markdown_parse_pending` 与 markdown 实体的 `pending_parse` 是什么关系？为什么视图要另存一个标志？

**参考答案**：`pending_parse` 是引擎内部「解析任务在飞」的事实来源（`Task<()>` 槽位）；`markdown_parse_pending` 是视图侧的观察副本，由 `_markdown_subscription` 回调从 `markdown.is_parsing()` 同步。视图需要它实现跨实体的事件顺序判断：「我标记过搜索结果待失效，且解析刚刚落地」这一组合只能靠在 observe 回调里比较前后两次 `is_parsing` 才能识别——这正是回调里 `if this.markdown_parse_pending && !is_parsing` 发出 `SearchEvent::MatchesInvalidated` 的条件（[L317-321](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L317-L321)）。

## 5. 综合实践

**综合任务：为 `MarkdownPreviewView` 建立一份「结构 + 模式 + 引擎」三视图档案。**

把本讲四个实践串起来，产出一份可长期维护的笔记文件（建议放在你自己的笔记库，不要提交到 Zed 仓库）：

1. **字段档案**（实践一）：13 个字段 × 用途 / 写入点行号 / 生命周期三列的表格。
2. **模式档案**（实践二 + 三）：
   - 一段话对比 Default 与 Follow 的行为契约，各配一个测试名作为「证据引用」；
   - `new` 的订阅分支对照图（公共前半段 → `observe_in` vs `subscribe_in` → 汇聚 `set_editor`），并注明 Default 选择 `subscribe` 的性能理由（注释原文位置 [L356-358](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L356-L358)）。
3. **引擎档案**（实践四）：`set_editor` 到 `markdown.reset` 再到 `_markdown_subscription` 回调的完整调用链，附 reset 三段逻辑的注释。
4. **验证环节**（可选）：如果你本地能编译 Zed，把 `REPARSE_DEBOUNCE`（[L56](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L56)）临时改成 2000ms 再运行，亲身感受 `pending_update_task` 所在链路的防抖节奏（改完记得还原，且不要把修改提交进仓库）。

完成标准：合上源码，仅凭档案就能回答——「Follow 预览切到新文件时，13 个字段里哪几个会变、按什么顺序变、谁触发持久化」。（参考答案：`set_editor` 幂等检查 → 新订阅替换旧订阅打包进 `active_editor` → `base_directory` 更新、`hovered_url` 清空 → `pending_update_task` 装入立即更新任务 → `active_source_index` 经滚动同步更新 → 解析落地后 `markdown_parse_pending` 翻转；持久化由 `MarkdownPreviewEvent::SourceEditorChanged` 触发 `should_serialize` → `serialize` 把新路径与 mode 写库。）

## 6. 本讲小结

- `MarkdownPreviewView` 十三个字段可按「上游绑定 / 内容与渲染 / 同步状态 / 瞬态工作」四组理解，大部分字段的写入点高度收敛在 `new` 与 `set_editor` 两个函数。
- `EditorState` 把「编辑器句柄 + 事件订阅」打包替换，保证换绑的原子性；`MarkdownPreviewEvent` 只有两个变体，专职通知持久化层；`PreviewLinkTarget` 是链接导航的两类目标（标题锚点 / 行列位置）。
- `MarkdownPreviewMode::Default` 固定绑定触发它的编辑器、按 buffer 复用；`Follow` 跟随活动编辑器、全窗格唯一；数据库中以 0/1 存储，`from_db` 对未知值兜底为 Default。
- `new` 的公共前半段（创建 markdown 实体、初始化字段、首次 `set_editor`）之后按模式分叉：Follow 用 `observe_in`（需要高频感知活动条目），Default 用 `subscribe_in`（把重绑检查移出光标热路径），两者汇聚于唯一的换绑入口 `set_editor`。
- `Entity<Markdown>` 是内容引擎：视图只负责在换绑/编辑时 `reset` 文本、订阅其变化；「内容不变短路、换内容保留旧解析」等策略全部封装在引擎内部。

## 7. 下一步学习建议

本讲搞清了「结构长什么样、字段归谁管」，下一讲 **u2-l2（打开预览的完整链路）** 会补上「视图怎么进入窗格」：从 `OpenPreview` 动作到 `pane.add_item` 的每一帧，以及按 buffer 复用已有预览的判定。之后再按依赖顺序进入 **u2-l3（编辑器绑定与 Follow 模式）**——那一讲会把本讲 4.3 节的 `set_editor` 事件分类展开成完整的事件响应表。继续阅读建议：先精读 [src/markdown_preview_view.rs:436-532](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L436-L532)（set_editor 与两个 workspace 回调），再看测试 `default_preview_stays_bound_to_invoking_editor_across_splits`（[L3310](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L3310) 起）巩固两种模式的差异。
