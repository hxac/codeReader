# 入口与动作注册：init、actions! 宏与快捷键

## 1. 本讲目标

上一讲我们认识了 markdown_preview crate 的整体定位：它是一个「胶水 + 面板」型 crate，自身只有三个源码文件。本讲我们回答一个更具体的问题：

> **Zed 是怎么把「Markdown 预览」这个功能挂进整个编辑器的？**

学完本讲，你应该能够：

1. 解释 `init(cx)` 这个仅 10 行的函数如何完成两件事：注册可序列化条目、给每个新 Workspace 注册动作。
2. 说出 `actions!` 宏生成了什么、动作名 `markdown::ScrollUpByItem` 里的 `markdown` 是什么含义。
3. 分清两套动作体系：crate 内定义的滚动/开关动作，与从 `zed_actions` 重导出的 `OpenPreview` / `OpenPreviewToTheSide`。
4. 在默认键位表中找到 `markdown::OpenPreview` 等绑定，并能亲手给动作追加自己的键位。

## 2. 前置知识

### 2.1 什么是「动作」（Action）

在传统 GUI 框架里，一个按钮点击、一次菜单选择，往往各自挂一个回调函数，彼此独立。Zed 的做法不同：它把「用户意图」抽象成一个个**动作**——比如「向下滚动一页」「打开预览」——每个动作是一个普通的 Rust 单元结构体（unit struct，即没有任何字段的结构体）。

这样设计的好处是**意图与触发方式解耦**：

- 键盘快捷键可以触发动作（键位表里写的是动作名，不是函数指针）；
- 命令面板可以列出动作；
- 代码里可以用 `window.dispatch_action(...)` 直接派发动作。

动作沿着元素树向上冒泡，直到某个元素用 `.on_action(...)` 声明「我来处理这个动作」为止。

### 2.2 什么是「键位上下文」（key context）

同一个按键在不同界面下应该做不同的事：在编辑器里按 `up` 是移动光标，在预览里按 `up` 是滚动预览。Zed 用**键位上下文**解决这个问题：每个界面元素可以声明自己处于什么上下文（比如 `"MarkdownPreview"`、`"Editor"`），键位表按 `context + 按键 → 动作` 三元组匹配。本讲会在第 4.4 节看到预览如何声明自己的上下文。

### 2.3 什么是 `cx`

在 gpui 里，`cx` 是「上下文」参数的惯用名。本讲会遇到两种：

- `&mut App`：应用级上下文，`init(cx: &mut App)` 拿到的就是它，能访问全局状态；
- `&mut Context<Workspace>`：更新 `Workspace` 实体时拿到，可以给这个 Workspace 注册动作。

对 `cx` 更深入的讨论留到后续讲义，本讲只需知道「`cx` 是与框架交互的把手」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `crates/markdown_preview/src/markdown_preview.rs` | crate 入口，仅 49 行 | `actions!` 宏调用、`init(cx)`、`zed_actions` 重导出 |
| `crates/markdown_preview/src/markdown_preview_view.rs` | 视图主体（约 3700 行） | `MarkdownPreviewView::register` 注册的 3 个打开动作；`render` 里的 `key_context` 与 `on_action` |
| `crates/zed_actions/src/lib.rs` | Zed 官方动作的集中定义处 | `preview::markdown` 模块中的 `OpenPreview` / `OpenPreviewToTheSide` |
| `crates/gpui/src/action.rs` | 动作机制的框架实现 | `actions!` 宏的定义与文档 |
| `crates/gpui/src/app.rs` | gpui 应用上下文 | `observe_new` 的行为 |
| `crates/workspace/src/workspace.rs` | 工作区（标签页、窗格的容器） | `register_serializable_item` 与 `Workspace::register_action` |
| `assets/keymaps/default-macos.json` | macOS 默认键位表 | `markdown::` 前缀的全部绑定 |
| `crates/zed/src/main.rs` | Zed 主程序 | `markdown_preview::init(cx)` 的调用位置 |

## 4. 核心概念与源码讲解

### 4.1 动作的定义：`actions!` 宏

#### 4.1.1 概念说明

markdown_preview crate 里所有「滚动」「开关预览」类动作，都集中定义在入口文件的一次宏调用里。`actions!` 是 gpui 提供的宏，它为每个名字生成一个实现了 `gpui::Action` trait 的单元结构体。

关键点：**动作的完整名字 = 命名空间 + 结构体名**。宏的第一个参数 `markdown` 就是命名空间，所以 `ScrollUpByItem` 这个结构体对应的动作名是 `markdown::ScrollUpByItem`——这正是键位表里出现的字符串。

#### 4.1.2 核心流程

一次 `actions!(markdown, [A, B, C])` 调用会：

1. 为 `A`、`B`、`C` 各生成一个 `pub struct`，自动派生 `Clone`、`PartialEq`、`Default`、`Debug` 和 `gpui::Action`；
2. `#[action(namespace = markdown)]` 使每个动作的名称解析为 `markdown::A` 这样的全名；
3. 宏文档说明：**注册两个同名动作会在 `App` 创建时直接 panic**——这就是为什么不同功能必须用不同命名空间。

#### 4.1.3 源码精读

先看 crate 入口的宏调用：

```rust
actions!(
    markdown,
    [
        /// Scrolls up by one page in the markdown preview.
        #[action(deprecated_aliases = ["markdown::MovePageUp"])]
        ScrollPageUp,
        ...
        /// Scrolls up by one markdown element in the markdown preview
        ScrollUpByItem,
        ...
        /// Closes the markdown preview and returns focus to the source editor.
        CloseAndReturnToEditor
    ]
);
```

完整定义见 [src/markdown_preview.rs:11-L37](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview.rs#L11-L37)。这段代码定义了 10 个动作：8 个滚动类（上/下翻页、上/下滚一行、上/下滚一个元素、滚到顶/底）、1 个打开跟随预览（`OpenFollowingPreview`）、1 个关闭预览返回编辑器（`CloseAndReturnToEditor`）。

注意 `ScrollPageUp` 上的 `#[action(deprecated_aliases = ["markdown::MovePageUp"])]`：动作曾用名 `markdown::MovePageUp`，用户旧键位表里若还写着旧名，通过这个别名依然能解析到新动作——这是动作改名不破坏用户配置的兼容手段。

再看宏本身的定义（框架侧）：

```rust
macro_rules! actions {
    ($namespace:path, [ $( $(#[$attr:meta])* $name:ident),* $(,)? ]) => {
        $(
            #[derive(::std::clone::Clone, ..., gpui::Action)]
            #[action(namespace = $namespace)]
            $(#[$attr])*
            pub struct $name;
        )*
    };
```

见 [crates/gpui/src/action.rs:24-L40](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/action.rs#L24-L40)。宏上方的文档注释（[crates/gpui/src/action.rs:20](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/action.rs#L20)）明确写了「会创建名为 `editor::MoveUp` 这样的动作」，而 [crates/gpui/src/action.rs:53](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/action.rs#L53) 警告了同名注册会 panic。

最后看重导出的一行：

```rust
pub use zed_actions::preview::markdown::{OpenPreview, OpenPreviewToTheSide};
```

见 [src/markdown_preview.rs:7](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview.rs#L7)。这两个动作**不是本 crate 定义的**，而是定义在 `zed_actions` crate 中：

```rust
pub mod preview {
    pub mod markdown {
        use gpui::actions;

        actions!(
            markdown,
            [
                /// Opens a markdown preview for the current file.
                OpenPreview,
                /// Opens a markdown preview in a split pane.
                OpenPreviewToTheSide,
            ]
        );
    }

    pub mod svg {
        ...
        actions!(
            svg,
            [
                /// Opens an SVG preview for the current file.
                OpenPreview,
                ...
            ]
        );
    }
}
```

见 [crates/zed_actions/src/lib.rs:889-L917](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/zed_actions/src/lib.rs#L889-L917)。这里有一个非常直观的佐证：`preview::markdown` 和 `preview::svg` 两个模块里都有叫 `OpenPreview` 的结构体，却不会冲突——因为它们的命名空间分别是 `markdown` 和 `svg`，完整动作名不同。Rust 的类型路径（`zed_actions::preview::svg::OpenPreview`）和动作注册名（`svg::OpenPreview`）是两套体系，后者才是键位表使用的。

**为什么要放在 `zed_actions` 里重导出？** `zed_actions` 是 Zed 官方定义「跨 crate 共享动作」的集中地，任何 crate 都能依赖它触发这些动作而不必依赖 markdown_preview 本身。而 markdown_preview 通过 `pub use` 把它们重新暴露出来，方便外部（比如测试或其他面板）直接写 `markdown_preview::OpenPreview` 拿到类型。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：亲手验证「键位表字符串 → 具体结构体」的映射关系，不再对 `markdown::` 前缀感到神秘。
2. **操作步骤**：
   - 打开 [src/markdown_preview.rs:11-L37](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview.rs#L11-L37)，数一数宏调用里一共定义了几个动作；
   - 在仓库根目录执行 `grep -rn "OpenFollowingPreview" assets/keymaps/`，看看这个动作有没有默认键位。
3. **需要观察的现象**：grep 应当没有任何输出。
4. **预期结果**：`OpenFollowingPreview`（跟随模式预览）**没有默认快捷键**，用户想用必须自己在键位表里绑定——这是本讲综合实践的主角之一。
5. 结论部分「OpenFollowingPreview 无默认绑定」基于当前 HEAD 的键位表检索，属源码事实；如果你在自己的运行环境中发现它有绑定，请检查是否加载了自定义键位表。

#### 4.1.5 小练习与答案

**练习 1**：如果有人在另一个 crate 里也写了 `actions!(markdown, [ScrollUp])`，会发生什么？

**答案**：两个动作的完整注册名都是 `markdown::ScrollUp`。根据 [crates/gpui/src/action.rs:53](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/action.rs#L53) 的说明，注册同名动作会在 `App` 创建时 panic。命名空间就是防止这种冲突的机制。

**练习 2**：键位表里写的 `"markdown::Copy"`（见 default-macos.json 第 210 行）也是本 crate 定义的吗？

**答案**：不是。本 crate 的 `actions!` 里没有 `Copy`。`markdown::Copy` 定义在 markdown crate 里（[crates/markdown/src/markdown.rs:535-L543](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown/src/markdown.rs#L535-L543)，同样是 `actions!(markdown, [...])` 调用）——`markdown` 这个命名空间并非 markdown_preview 独占，谁都可以用它注册动作，只要完整动作名不重复。（这也说明读代码时不能只看前缀猜归属，要搜索定义处。）

**练习 3**：`deprecated_aliases` 属性解决什么问题？

**答案**：动作改名后，用户旧键位表里还写着旧名（如 `markdown::MovePageUp`）。声明废弃别名后，旧名依然能解析到新动作，用户配置不会因为升级而失效。

---

### 4.2 `init(cx)`：crate 的引导函数

#### 4.2.1 概念说明

Zed 里几乎每个功能 crate 都约定提供一个 `pub fn init(cx: &mut App)`，由主程序在启动时调用一次，把这个功能「装」进应用。markdown_preview 的 `init` 只做两件事，但这两件事分别接入了 workspace 的两大机制：

1. **`register_serializable_item`**——告诉 workspace「`MarkdownPreviewView` 这种条目是可以序列化保存的」，这是预览标签页能在重启 Zed 后恢复的前提（第 u3-l3 讲会展开细节）；
2. **`observe_new` + 注册动作**——每当新建一个 Workspace（典型场景：开新窗口），自动给这个 Workspace 挂上「打开预览」的三个动作。

#### 4.2.2 核心流程

`init` 的执行流程可以画成：

```text
Zed 启动
  └─ main.rs 调用 markdown_preview::init(cx)          （只发生一次）
       ├─ ① workspace::register_serializable_item::<MarkdownPreviewView>(cx)
       │      把视图类型登记进全局注册表（按 serialized_item_kind 索引）
       └─ ② cx.observe_new(|workspace: &mut Workspace, window, cx| ...)
              .detach()                                  （订阅存活整个应用生命周期）
              └─ 每当有新 Workspace 实体被创建（新窗口/新工作区）
                   └─ MarkdownPreviewView::register(workspace, window, cx)
                        └─ 给这个 Workspace 注册 3 个打开预览的动作
```

#### 4.2.3 源码精读

`init` 全文如下（[src/markdown_preview.rs:39-L49](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview.rs#L39-L49)）：

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

逐行拆解：

- **第 40 行**：`register_serializable_item` 的实现在 [crates/workspace/src/workspace.rs:1100-L1118](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/workspace/src/workspace.rs#L1100-L1118)。它把 `MarkdownPreviewView` 的「反序列化函数」「清理函数」「向下转型函数」打包成一个描述符，插入全局注册表 `descriptors_by_kind`，键是 `I::serialized_item_kind()` 返回的字符串（对预览来说是 `"MarkdownPreview"`）。之后 workspace 从数据库恢复会话时，就是凭这个键找回该由谁负责重建条目。

- **第 42 行**：`cx.observe_new` 是 gpui 的通用机制，签名见 [crates/gpui/src/app.rs:2115-L2132](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/app.rs#L2115-L2132)。它的文档写得很清楚：「每当指定类型的实体被创建时就调用给定函数」。这里指定的类型是 `Workspace`，所以回调会在**每个新 Workspace 实体**创建时触发一次。

- **第 43–45 行**：`let Some(window) = window else { return }` 是一个防御性检查——`observe_new` 回调拿到的 `window` 是 `Option<&mut Window>`，理论上存在没有窗口的创建路径，此时直接跳过（预览动作的注册需要 `window`，拿不到就没法注册）。

- **第 48 行**：`.detach()` 让这个订阅永久存活。`observe_new` 返回一个 `Subscription`，若不保存它会在 `init` 函数返回时被 drop（订阅随之失效），之后新建的窗口就收不到动作注册了。detach 是明确表达「这个订阅要活到应用结束」。

最后确认调用点：主程序在一系列 `init` 中调用了它，见 [crates/zed/src/main.rs:781](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/zed/src/main.rs#L781)。它排在 `terminal_view::init`、`journal::init` 等一系列同级初始化之间——每个功能 crate 一行，这是 Zed 装配功能的统一模式。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：理解「init 只跑一次，但动作注册随每个窗口发生」。
2. **操作步骤**：
   - 阅读 [crates/zed/src/main.rs:766-L784](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/zed/src/main.rs#L766-L784)，数出这段启动序列里有多少个 crate 的 `init`；
   - 阅读 [crates/gpui/src/app.rs:2113-L2132](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/app.rs#L2113-L2132) 中 `observe_new` 的文档注释。
3. **需要观察的现象**：启动序列是一长串 `xxx::init(cx)` 调用，每个一行。
4. **预期结果**：你能口头回答「如果去掉 `.detach()`，会发生什么」——`Subscription` 在 `init` 返回时被 drop，第一个窗口可能已经注册了动作，但**之后新开的窗口**将不再有 `markdown::OpenPreview` 动作。
5. 以上行为推演基于 gpui 订阅的 drop 语义，未实际运行验证，属「待本地验证」结论；感兴趣的读者可以临时注释 `.detach()` 后编译观察。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `register_serializable_item` 放在 `init` 里全局做一次，而动作注册要放在 `observe_new` 回调里每个 Workspace 做一次？

**答案**：可序列化条目注册表是**应用级全局**的（存在 `cx.default_global::<SerializableItemRegistry>()` 里），按条目类型索引，注册一次即可；而 `Workspace::register_action` 把回调存在 **Workspace 实体自己的字段**（`workspace_actions`）上，每个窗口的 Workspace 是独立实体，必须各自注册一遍。

**练习 2**：`observe_new` 的回调为什么参数是 `&mut Workspace`，而 `register` 的第三个参数是 `&mut Context<Workspace>`？

**答案**：`observe_new` 内部已经把新建实体 downcast 并 `update` 了（见 [crates/gpui/src/app.rs:2122-L2130](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/app.rs#L2122-L2130)），回调拿到的已经是「正在更新的实体 + 它的 Context」。签名上 `on_new` 接收 `(&mut T, Option<&mut Window>, &mut Context<T>)`，只是 Rust 闭包里我们把第三个参数继续命名成了 `cx` 传下去。

---

### 4.3 每个 Workspace 一次：`MarkdownPreviewView::register` 与 `workspace.register_action`

#### 4.3.1 概念说明

第 4.2 节看到 `init` 会在新 Workspace 出现时调用 `MarkdownPreviewView::register`。这个方法注册了**三个「打开预览」动作**：

| 动作 | 完整名 | 行为 |
| --- | --- | --- |
| `OpenPreview` | `markdown::OpenPreview`（重导出自 zed_actions） | 在当前窗格里打开/激活预览 |
| `OpenPreviewToTheSide` | `markdown::OpenPreviewToTheSide`（同上） | 在旁边的分栏里打开预览 |
| `OpenFollowingPreview` | `markdown::OpenFollowingPreview`（本 crate 定义） | 打开/激活「跟随模式」预览 |

注意区分两种注册 API：

- `Workspace::register_action`——把回调挂在 **Workspace 级别**，只要焦点在当前窗口的任意位置都能触发（适合「打开预览」这种全局意图）；
- 元素上的 `.on_action(...)`——把回调挂在**具体 UI 元素**上，只有该元素（或其子元素）持有焦点时才会处理（适合预览内部的滚动动作，第 4.4 节展开）。

#### 4.3.2 核心流程

以按 `cmd-shift-v`（打开预览）为例：

```text
按键 cmd-shift-v
  └─ 键位表匹配：context "Editor && extension == md" → markdown::OpenPreview
       └─ 动作沿元素树冒泡，到达 Workspace 层
            └─ register_action 注册的回调执行：
                 ├─ resolve_active_item_as_markdown_editor(workspace, cx)
                 │    当前激活条目是 Markdown 编辑器吗？不是则静默返回
                 └─ 是 → open_preview_in_pane(workspace, editor, pane, window, cx)
                           在当前窗格中新增/激活预览条目
```

#### 4.3.3 源码精读

`register` 的源码（节选，[src/markdown_preview_view.rs:117-L156](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L117-L156)）：

```rust
pub fn register(workspace: &mut Workspace, _window: &mut Window, _cx: &mut Context<Workspace>) {
    workspace.register_action(move |workspace, _: &OpenPreview, window, cx| {
        if let Some(editor) = Self::resolve_active_item_as_markdown_editor(workspace, cx) {
            let pane = workspace.active_pane().clone();
            Self::open_preview_in_pane(workspace, editor, pane, window, cx);
        }
    });

    workspace.register_action(move |workspace, _: &OpenPreviewToTheSide, window, cx| {
        if let Some(editor) = Self::resolve_active_item_as_markdown_editor(workspace, cx) {
            let pane = workspace.active_pane().clone();
            Self::open_preview_to_the_side_of_pane(workspace, editor, pane, window, cx);
        }
    });

    workspace.register_action(move |workspace, _: &OpenFollowingPreview, window, cx| {
        if let Some(editor) = Self::resolve_active_item_as_markdown_editor(workspace, cx) {
            // Check if there's already a following preview
            ...
        }
    });
}
```

这段代码做了三件事，结构完全对称：

1. 每个回调先做**类型守卫**：`resolve_active_item_as_markdown_editor` 检查当前激活的条目是不是 Markdown 编辑器，不是就什么都不做（按了快捷键也没反应，这就是为什么在非 Markdown 文件里按 `cmd-shift-v` 没有效果）；
2. 是 Markdown 编辑器，则按动作语义选择 `open_preview_in_pane`（同窗格）或 `open_preview_to_the_side_of_pane`（分栏）；
3. `OpenFollowingPreview` 稍特殊：先在当前窗格里找有没有已存在的 Follow 模式预览，有就激活它，没有才新建——避免重复开出一排跟随预览。

而 `workspace.register_action` 本身的实现（[crates/workspace/src/workspace.rs:8013-L826](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/workspace/src/workspace.rs#L8013-L826)）很薄：

```rust
pub fn register_action<A: Action>(
    &mut self,
    callback: impl Fn(&mut Self, &A, &mut Window, &mut Context<Self>) + 'static,
) -> &mut Self {
    let callback = Arc::new(callback);

    self.workspace_actions.push(Box::new(move |div, _, _, cx| {
        let callback = callback.clone();
        div.on_action(cx.listener(move |workspace, event, window, cx| {
            (callback)(workspace, event, window, cx)
        }))
    }));
    self
}
```

它并没有立刻绑定任何东西，而是把「如何在一个 div 上挂 `on_action`」的闭包**积攒**在 `workspace_actions` 列表里；等 Workspace 渲染时统一应用。这个细节说明：Workspace 级动作最终也是通过元素树的 `.on_action` 生效的，只是挂载点始终是 Workspace 的根元素。

#### 4.3.4 代码实践（行为观察型）

1. **实践目标**：体会「类型守卫」——三个打开动作只在 Markdown 文件里生效。
2. **操作步骤**：
   - 在 Zed 中分别打开一个 `.rs` 文件和一个 `.md` 文件；
   - 在 `.rs` 文件里按 `cmd-shift-v`（macOS）或 `ctrl-shift-v`（Linux）；
   - 再切到 `.md` 文件里按同样的键。
3. **需要观察的现象**：`.rs` 文件里按键无任何反应；`.md` 文件里弹出预览。
4. **预期结果**：与 `resolve_active_item_as_markdown_editor` 的守卫逻辑一致。
5. 本实践需要在本地编译运行 Zed 才能操作，属「待本地验证」；若行为不符，回来重读 `resolve_active_item_as_markdown_editor` 的实现。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `OpenPreview` 和 `OpenPreviewToTheSide` 定义在 `zed_actions` 而 `OpenFollowingPreview` 定义在本 crate？

**答案**：`zed_actions` 里的动作是「官方公共动作词汇表」，供多个 crate 共享使用（例如其他面板也可能想触发打开 Markdown 预览）；`OpenFollowingPreview` 目前只有本 crate 关心，属于实现细节，就近定义即可。这不是硬性规则，而是放置倾向。

**练习 2**：`register` 的回调里为什么用 `move` 闭包？它捕获了什么？

**答案**：这里其实没有捕获任何外部变量（三个闭包体内只用了参数），`move` 是无伤大雅的惯用写法。`register_action` 的签名要求闭包 `'static`，`move` 确保即使未来闭包引用了外部变量也是按值捕获、满足 `'static` 约束。

---

### 4.4 从按键到动作：键位表与两级分发

#### 4.4.1 概念说明

动作定义好了、回调也注册了，中间还差一环：**哪个键触发哪个动作**。这一环由键位表（keymap）JSON 文件完成。Zed 的动作分发是「两级」的：

- **Workspace 级**：打开预览的三个动作挂在 Workspace 根元素上，任何焦点位置都可能触发（但受键位上下文限制，见下）；
- **视图级**：滚动等动作只挂在预览视图自己的元素上，且预览声明了专属上下文 `"MarkdownPreview"`，键位也只在预览聚焦时匹配。

#### 4.4.2 核心流程

```text
用户按键
  └─ 找到焦点元素，沿元素树收集 key context，得到如 "Editor && extension == md"
       └─ 在键位表中找 (context, key) 匹配项 → 得到动作名字符串 "markdown::OpenPreview"
            └─ 按名字解析出动作实例（ActionsRegistry）
                 └─ 动作从焦点元素向上冒泡
                      ├─ 先经过预览元素的 .on_action(...)（若匹配则处理并停止）
                      └─ 最终到 Workspace 根元素的 .on_action(...)（打开预览动作在这里处理）
```

#### 4.4.3 源码精读

**打开预览的绑定**在 macOS 默认键位表（[assets/keymaps/default-macos.json:640-L646](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/assets/keymaps/default-macos.json#L640-L646)）：

```json
{
  "context": "Editor && extension == md",
  "use_key_equivalents": true,
  "bindings": {
    "cmd-k v": "markdown::OpenPreviewToTheSide",
    "cmd-shift-v": "markdown::OpenPreview"
  },
},
```

注意上下文是 `Editor && extension == md`——**只有焦点在 Markdown 文件的编辑器里**这两个键才生效。这是键位层的第二重守卫（第一重是 4.3 节的类型守卫）。Linux 上对应绑定在 [assets/keymaps/default-linux.json:605-L606](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/assets/keymaps/default-linux.json#L605-L606)（`ctrl-k v` / `ctrl-shift-v`）。

**预览内部的滚动绑定**（[assets/keymaps/default-macos.json:1432-L1445](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/assets/keymaps/default-macos.json#L1432-L1445)）：

```json
{
  "context": "MarkdownPreview",
  "bindings": {
    "pageup": "markdown::ScrollPageUp",
    "pagedown": "markdown::ScrollPageDown",
    "up": "markdown::ScrollUp",
    "down": "markdown::ScrollDown",
    "alt-up": "markdown::ScrollUpByItem",
    "alt-down": "markdown::ScrollDownByItem",
    "cmd-up": "markdown::ScrollToTop",
    "cmd-down": "markdown::ScrollToBottom",
    "cmd-shift-v": "markdown::CloseAndReturnToEditor",
    "cmd-f": "buffer_search::Deploy"
  },
},
```

这张表正好是 4.1 节 8 个滚动动作的用武之地，外加 `CloseAndReturnToEditor`（同一个 `cmd-shift-v`，在编辑器上下文里是「打开」、在预览上下文里是「关闭」——一个按键随上下文切换语义的漂亮设计）。

那 `"MarkdownPreview"` 这个上下文名是从哪来的？看预览的 `render` 方法开头（[src/markdown_preview_view.rs:1648-L1666](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L1648-L1666)）：

```rust
div()
    .image_cache(self.image_cache.clone())
    .id("MarkdownPreview")
    .key_context("MarkdownPreview")
    .track_focus(&self.focus_handle(cx))
    ...
    .on_action(cx.listener(MarkdownPreviewView::scroll_page_up))
    .on_action(cx.listener(MarkdownPreviewView::scroll_page_down))
    .on_action(cx.listener(MarkdownPreviewView::scroll_up))
    .on_action(cx.listener(MarkdownPreviewView::scroll_down))
    .on_action(cx.listener(MarkdownPreviewView::scroll_up_by_item))
    .on_action(cx.listener(MarkdownPreviewView::scroll_down_by_item))
    .on_action(cx.listener(MarkdownPreviewView::scroll_to_top))
    .on_action(cx.listener(MarkdownPreviewView::scroll_to_bottom))
    .on_action(cx.listener(MarkdownPreviewView::close_and_return_to_editor))
```

两行关键代码：

- `.key_context("MarkdownPreview")`：向键位系统声明「我的子树处于 MarkdownPreview 上下文」，键位表里的 `"context": "MarkdownPreview"` 段由此匹配；
- `.on_action(cx.listener(MarkdownPreviewView::scroll_up_by_item))` 等：把 8 个滚动动作与关闭动作的处理函数逐个挂在这个 div 上。`cx.listener` 是 gpui 的惯用法——把「更新 `MarkdownPreviewView` 实体」的闭包适配成元素事件回调。

于是 4.3 节的 `Workspace::register_action` 与这里的 `.on_action` 形成互补：前者是「窗口级」动作（打开预览），后者是「视图级」动作（预览内滚动）。

#### 4.4.4 代码实践（键位定制型，本讲主实践）

1. **实践目标**：走通「键位表 → 动作 → 回调」全链路，并亲手给动作绑一个新键。
2. **操作步骤**：
   - **第一步**：在仓库里打开 [assets/keymaps/default-macos.json:640-L646](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/assets/keymaps/default-macos.json#L640-L646)，确认 `cmd-shift-v` 与 `cmd-k v` 的绑定（Linux 用户看 [assets/keymaps/default-linux.json:605-L606](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/assets/keymaps/default-linux.json#L605-L606)）；
   - **第二步**：编译运行 Zed，打开任意 `.md` 文件，分别按这两个键，观察「同窗格打开」与「分栏打开」的差异；
   - **第三步**：打开**用户键位表**（不是仓库里的 assets 文件，而是用户配置目录下的 `keymap.json`，可用命令面板的 `zed: open keymap` 进入），追加一段：
     ```json
     [
       {
         "context": "MarkdownPreview",
         "bindings": {
           "alt-shift-up": "markdown::ScrollUpByItem"
         }
       }
     ]
     ```
   - **第四步**：打开一个较长的 Markdown 预览，点击预览区域让其获得焦点，按 `alt-shift-up`。
3. **需要观察的现象**：
   - 第二步中两个快捷键分别产生「替换当前标签」与「右侧分栏」两种布局；
   - 第四步中 `alt-shift-up` 与默认的 `alt-up` 一样，让预览向上滚动一个 Markdown 元素（一个标题/段落/列表项）。
4. **预期结果**：新绑定生效，说明动作分发确实走 `"MarkdownPreview"` 上下文匹配 + 视图元素 `.on_action` 处理。
5. 若 `alt-shift-up` 无响应，可能是被更高优先级的上下文段（如 Editor 或 Workspace 级绑定）截获，可换一个冷门组合（如 `ctrl-alt-up`）重试。本实践需本地运行 Zed，属「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `cmd-shift-v` 能同时承担「打开预览」（Editor 上下文）和「关闭预览返回编辑器」（MarkdownPreview 上下文）两个职责而不冲突？

**答案**：键位匹配依赖焦点元素收集到的上下文链。焦点在编辑器时匹配到 `Editor && extension == md` 段；焦点在预览时匹配到 `MarkdownPreview` 段。两段各自把同一个键解析到不同动作，运行时永远只有一段生效。

**练习 2**：如果把 `.key_context("MarkdownPreview")` 这行删掉，键位表里 `"context": "MarkdownPreview"` 段的绑定会发生什么？

**答案**：预览子树不再声明该上下文，按键时上下文链里没有 `MarkdownPreview`，那段绑定永远无法匹配，所有滚动快捷键（pageup/down、alt-up/down 等）都会失效（除非恰好被更外层的默认段接住）。

**练习 3**：`buffer_search::Deploy` 也出现在 MarkdownPreview 段里，这说明什么？

**答案**：键位表段可以混用不同命名空间的动作。`buffer_search::Deploy` 定义在 buffer_search 相关 crate，但在预览聚焦时按 `cmd-f` 触发它，配合的是 MarkdownPreviewView 对 `SearchableItem` 的实现（第 u3-l2 讲的主题）。

---

## 5. 综合实践

**任务：给「跟随预览」补上快捷键，并整理一张完整的动作速查表。**

背景：4.1.4 节已确认 `OpenFollowingPreview` 没有默认键位。请完成：

1. 在用户键位表里为它添加绑定，放到「编辑器且是 Markdown 文件」的上下文里：

   ```json
   [
     {
       "context": "Editor && extension == md",
       "bindings": {
         "cmd-k cmd-v": "markdown::OpenFollowingPreview"
       }
     }
   ]
   ```

2. 编译运行 Zed，打开 `.md` 文件触发它，观察出现的预览与 `OpenPreview` 的区别（提示：跟随预览会随你切换编辑器文件而切换内容，这是第 u2-l3 讲的主题，此处只需记录现象）。
3. 再按一次同样的键，验证 4.3.3 节源码里的「已有跟随预览则激活而不新建」分支：标签数量不应增加。
4. 最后产出一张速查表（Markdown 文件即可），三列：**按键 → 动作全名 → 处理函数所在源码行**。至少覆盖本讲出现的 13 个动作（3 个打开 + 8 个滚动 + 关闭 + `buffer_search::Deploy`），处理函数一列填 `MarkdownPreviewView::register`（[src/markdown_preview_view.rs:117](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L117)）或 `render` 中的 `.on_action`（[src/markdown_preview_view.rs:1658-L1669](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L1658-L1669)）。

完成这张表后，你就把「按键 → 键位表 → 动作名 → 注册回调 → 源码处理函数」这条链完全打通了。

## 6. 本讲小结

- markdown_preview 的入口 `init(cx)` 只做两件事：全局注册一次可序列化条目（会话恢复的入口），再用 `cx.observe_new` 给每个新 Workspace 挂载动作注册。
- `actions!` 宏为每个名字生成一个单元结构体并注册动作，动作全名 = 命名空间 + 结构体名；`markdown::ScrollUpByItem` 的 `markdown` 就是命名空间，同名注册会 panic，`deprecated_aliases` 用于动作改名的向后兼容。
- 动作分两套来源：`OpenPreview` / `OpenPreviewToTheSide` 定义在 `zed_actions::preview::markdown` 并被重导出，其余 8 个滚动/开关动作直接定义在本 crate。
- 动作处理分两级：三个「打开」动作经 `Workspace::register_action` 挂在 Workspace 根元素（每窗口注册一次）；滚动/关闭动作经 `render` 里的 `.on_action` 挂在预览自己的 div 上。
- 键位表按 `(context, key) → 动作名` 匹配；预览通过 `.key_context("MarkdownPreview")` 声明上下文，`cmd-shift-v` 因此能在「编辑器里打开、预览里关闭」一键双义。
- `OpenFollowingPreview` 没有默认键位，是练习键位定制的天然素材。

## 7. 下一步学习建议

- 下一讲（u1-l3）转向**设置机制**：`MarkdownPreviewSettings` 如何从 `settings.json` 走到渲染容器，`markdown_preview_font_size` 又是如何被 `IncreaseBufferFontSize` 动作改写并持久化的——你会发现字体动作的注册方式与本讲 4.4 节如出一辙。
- 想提前看「打开预览」的下半程，可以直接阅读 [src/markdown_preview_view.rs:158-L200](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/markdown_preview/src/markdown_preview_view.rs#L158-L200) 中 `open_preview_in_pane` / `activate_or_add_preview` 的实现，这是第 u2-l2 讲的主菜。
- 对动作机制本身感兴趣的读者，建议通读 [crates/gpui/src/action.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/action.rs) 的宏文档与 `Action` trait 定义，再看 `Workspace::register_action` 积攒的闭包在渲染时如何被消费（[crates/workspace/src/workspace.rs:8035](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/workspace/src/workspace.rs#L8035) 起的 `add_workspace_actions_listeners`）。
