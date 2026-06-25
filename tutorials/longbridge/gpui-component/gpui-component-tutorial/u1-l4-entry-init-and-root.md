# 应用入口、init 初始化与 Root

## 1. 本讲目标

通过本讲，你将掌握一个 GPUI Component 应用的「最小骨架」是如何搭起来的。具体目标：

- 理解为什么 `gpui_component::init(cx)` 必须在使用任何组件**之前**调用，以及它到底初始化了哪些子系统。
- 掌握从 `Application` 到 `open_window` 再到 `Root` 的标准窗口创建流程。
- 理解 `Root` 作为窗口顶层视图的职责：它如何统一承载 Sheet、Dialog、Notification、键盘导航与文本选择。
- 能够参照 `examples/hello_world` 独立写出一个最小可运行的应用。

本讲承接 [u1-l2 构建与运行](u1-l2-build-and-run.md)（你已经知道如何用 `cargo run` 启动应用）和 [u1-l3 仓库结构与 crate 划分](u1-l3-repo-structure.md)（你已经了解 `ui` 核心库与 `story` 展示库的分工），从「怎么跑起来」进入到「跑起来时程序内部到底发生了什么」。

## 2. 前置知识

阅读本讲前，你需要具备以下基础认知（前几讲已建立）：

- **Cargo Workspace**：仓库是多个 crate 组成的工作区，`cargo run` 默认编译 `default-members`（即 Story Gallery）。
- **GPUI**：来自 Zed 编辑器团队的底层 GUI 渲染框架，是 gpui-component 的地基。
- **Render / RenderOnce**：GPUI 中「有状态视图」与「无状态组件」两种渲染模式的区别。
- **View / Context / Window**：GPUI 应用的基本对象——窗口（`Window`）、视图（视图实体）和应用上下文（`App` / `Context`）。

如果你对 Rust 的闭包、`Result`/`expect`、所有权还不熟悉，建议先补一下基础，因为启动代码大量使用闭包传递视图构造逻辑。

本讲会用到的几个 GPUI 核心概念：

| 概念 | 通俗解释 |
|------|---------|
| `Application` | 整个桌面应用的「容器」，负责事件循环、平台集成。 |
| `cx` | 运行时的上下文对象（`&mut App` 或 `&mut Context<T>`），几乎一切操作都要通过它。 |
| `open_window` | 在应用中创建一个系统窗口，并返回窗口里的根视图。 |
| `cx.new(\|_\| ...)` | 创建一个新的视图实体（entity），返回它的句柄。 |
| 焦点（Focus） | 当前接收键盘输入的元素，由 `FocusHandle` 表示。 |

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
|------|------|
| [crates/ui/src/lib.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/lib.rs) | 核心库的入口，定义了公开的 `init(cx)` 函数，逐一初始化所有子系统。 |
| [crates/ui/src/root.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs) | 定义 `Root` 视图——每个窗口必须的顶层视图，管理浮层、通知、焦点与文本选择。 |
| [examples/hello_world/src/main.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/examples/hello_world/src/main.rs) | 最小应用示例，展示了 `init` → `open_window` → `Root` 的完整骨架。 |
| [crates/story/src/main.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/main.rs) | Story Gallery 的入口，用更高层的 `init` / `create_new_window` 封装了同样的流程。 |
| [crates/story/src/lib.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/lib.rs) | Story 库的 `init` 与 `create_new_window` 实现，演示了真实项目如何复用并扩展核心流程。 |

## 4. 核心概念与源码讲解

本讲拆分为三个最小模块：

1. **init 初始化**：理解 `lib.rs` 中的 `init` 函数做了什么。
2. **Root 顶层视图**：理解为什么每个窗口的第一个视图必须是 `Root`，它管理了什么。
3. **完整启动流程**：把 `Application` → `open_window` → `Root` 串成一条可运行的主线。

### 4.1 模块一：init 初始化函数

#### 4.1.1 概念说明

GPUI Component 不是一个「引用几个函数就能用」的纯函数库，它内部有一批**需要全局状态**的子系统：主题系统、全局状态、Dock 布局注册表、键盘快捷键绑定、输入系统等等。这些子系统在第一次被使用前，必须往 `App` 上下文里「注册自己」。

`init(cx)` 就是这个统一入口。它相当于告诉应用：「请把这套组件库需要的所有地基都铺好」。它的文档注释写得很明确：

> Initialize the components. You must initialize the components at your application's entry point.

如果你忘了调用它，会出现各种「看起来毫无关联」的崩溃，比如读取主题时 panic、Dock 面板无法反序列化、键盘 Tab 键不工作等——这些都是因为某个子系统的全局注册步骤被跳过了。

#### 4.1.2 核心流程

`init` 本身不复杂，它的核心是「按固定顺序调用每个子模块各自的 `init`」：

```text
init(cx)
  ├─ theme::init(cx)        # 主题：注册全局 Theme 单例、明暗模式
  ├─ global_state::init(cx) # 全局状态机制
  ├─ inspector::init(cx)    # 调试检查器（仅 debug/inspector 特性）
  ├─ root::init(cx)         # Root 的快捷键绑定（Tab / 复制等）
  ├─ focus_trap::init(cx)   # 焦点陷阱
  ├─ color_picker::init(cx) # 颜色选择器
  ├─ date_picker::init(cx)  # 日期选择器
  ├─ dock::init(cx)         # Dock 布局注册表
  ├─ sheet::init(cx)        # 抽屉
  ├─ combobox::init(cx)     # 组合框
  ├─ select::init(cx)       # 下拉选择
  ├─ input::init(cx)        # 输入系统（Rope、光标等）
  ├─ list::init(cx)         # 列表
  ├─ dialog::init(cx)       # 对话框
  ├─ popover::init(cx)      # 弹出层
  ├─ menu::init(cx)         # 菜单
  ├─ table::init(cx)        # 表格
  ├─ text::init(cx)         # 文本渲染
  ├─ tree::init(cx)         # 树形组件
  └─ tooltip::init(cx)      # 提示
```

**为什么要讲究顺序？** 这里隐含一个依赖关系。`theme::init` 必须排在最前面，因为后续几乎所有组件在初始化或首次渲染时都可能读取 `cx.theme()`。如果某个子系统在主题就绪前尝试读主题色，就会取到默认空值甚至 panic。可以把这种关系理解为一个**偏序关系**（partial order）：

\[ \text{theme} \prec \text{几乎所有其它子系统}, \quad \text{root} \prec \text{依赖焦点的子系统} \]

`init` 函数给出的就是这组偏序的一个合法拓扑排序。实际开发中你不需要纠结这个顺序，只要记住「在最开头整体调一次 `gpui_component::init(cx)`」即可。

#### 4.1.3 源码精读

公开的 `init` 函数定义在核心库入口，它把所有子系统的初始化集中在一处：

[crates/ui/src/lib.rs:104-129](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/lib.rs#L104-L129) — 核心库的统一初始化入口，逐行调用每个子模块的 `init`。

关键片段（节选关键几行，完整代码请看上方链接）：

```rust
/// Initialize the components.
///
/// You must initialize the components at your application's entry point.
pub fn init(cx: &mut App) {
    theme::init(cx);
    global_state::init(cx);
    #[cfg(any(feature = "inspector", debug_assertions))]
    inspector::init(cx);
    root::init(cx);
    // ... 其余子系统的 init
    tooltip::init(cx);
}
```

注意两点细节：

1. **`inspector::init` 带 `#[cfg(...)]` 条件编译**：调试检查器只在 `inspector` 特性开启或 `debug_assertions`（即 debug 构建）下才会注册。这意味着发布版（release）应用不会包含调试面板，是一种常见的「按需编译」实践。
2. **没有返回值**：`init` 通过 `&mut App` 直接修改全局状态，所有「注册」副作用都发生在 `cx` 上。这也解释了为什么它必须在使用组件前调用——注册过的全局状态要先生效。

#### 4.1.4 代码实践

**实践目标**：通过对比「调用 `init`」与「不调用 `init`」的行为，直观感受初始化的必要性。

**操作步骤**：

1. 打开 [examples/hello_world/src/main.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/examples/hello_world/src/main.rs)。
2. 运行该示例：`cargo run -p hello_world`，确认窗口能正常出现、主题颜色正确。
3. 临时把 `main` 中的 `gpui_component::init(cx);` 这一行注释掉，重新编译运行。
4. 观察现象（待本地验证）：
   - 窗口是否还能正常显示？
   - 按钮的背景色、文字颜色是否变成了「默认/错误」的值（因为没有主题）？
   - 是否出现 panic 或读取全局主题时的报错？
5. 恢复 `init(cx);` 这一行。

**需要观察的现象**：注释掉 `init` 后，由于 `cx.theme()` 这类依赖全局主题的调用没有就绪，最可能出现 panic 或样式错乱；恢复后一切正常。

**预期结果**：你将直观地确认——`init` 不是可有可无的样板代码，而是组件库正常工作的地基。

#### 4.1.5 小练习与答案

**练习 1**：`init` 函数里 `theme::init(cx)` 为什么必须放在最前面？如果把它挪到 `tooltip::init(cx)` 之后会发生什么？

> **参考答案**：因为后续子系统（以及组件首次渲染时）会读取 `cx.theme()`。主题是最底层的公共依赖，所以必须先初始化。如果挪到最后，排在它前面的子系统在初始化时若访问主题，会读到未就绪的全局状态，可能 panic 或得到默认空主题。

**练习 2**：为什么 `inspector::init(cx)` 前面要加 `#[cfg(any(feature = "inspector", debug_assertions))]`？

> **参考答案**：调试检查器只在开发期（debug 构建或显式开启 `inspector` 特性）才有意义。用条件编译可以保证发布版不包含这坨调试代码，减小体积、避免泄露内部信息。

### 4.2 模块二：Root 顶层视图

#### 4.2.1 概念说明

`Root` 是 gpui-component 里一个**强制约定**：**每个窗口的第一个视图（最顶层视图）必须是 `Root`**。这不是 GPUI 的硬性要求，而是 gpui-component 的设计约定——因为很多组件依赖 `Root` 来「托管」自己。

为什么需要这样一个顶层视图？因为像 Dialog（对话框）、Sheet（抽屉）、Notification（通知）这类**浮层**，它们要浮在窗口最上层、要管理焦点切换、要全局唯一。如果把它们的状态散落在各个业务视图里，会很难协调。gpui-component 的做法是：把这些「窗口级」的能力统一收口到 `Root` 这一个视图里。

只要你的窗口顶层是 `Root`，那么任何业务代码（哪怕在很深的嵌套里）都能通过 `window.root::<Root>()` 拿到这个唯一实例，然后调用 `push_notification`、`open_dialog` 等方法。这就是「必须第一个是 Root」的根本原因。

#### 4.2.2 核心流程

`Root` 在内部用几个字段集中托管窗口级状态：

```text
Root 视图
  ├─ view: AnyView            # 你真正的业务视图（Root 只是个壳）
  ├─ active_sheet             # 当前打开的抽屉（最多一个）
  ├─ active_dialogs: Vec      # 当前打开的对话框（可叠加多个）
  ├─ notification             # 通知列表
  ├─ tooltip_overlay          # 全局提示层
  ├─ native_menu_overlay      # 原生菜单回退层
  ├─ text_selection           # 窗口级文本选择状态
  └─ ...
```

渲染时，`Root` 会把你传入的业务视图 `view` 作为子元素，并在其上叠加这些浮层层级。其它代码访问 `Root` 的标准方式有两个静态方法：

```text
Root::update(window, cx, |root, window, cx| { ... })  # 可变访问，用于 push/open/close
Root::read(window, cx)                                  # 只读访问
```

它们内部都是 `window.root::<Root>()` 找到这个唯一的 Root 实体。如果窗口顶层不是 `Root`，`window.root::<Root>()` 会返回 `None`，于是这些方法会 panic（带明确的 BUG 提示信息）。

#### 4.2.3 源码精读

`Root` 的结构体定义和文档注释说明了一切：

[crates/ui/src/root.rs:35-60](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L35-L60) — `Root` 的文档注释（说明它必须是窗口第一个视图、用于管理 Sheet/Dialog/Notification）与字段定义。

节选：

```rust
/// Root is a view for the App window for as the top level view (Must be the first view in the window).
///
/// It is used to manage the Sheet, Dialog, and Notification.
pub struct Root {
    style: StyleRefinement,
    view: AnyView,
    pub(crate) active_sheet: Option<ActiveSheet>,
    pub(crate) active_dialogs: Vec<ActiveDialog>,
    ...
    pub notification: Entity<NotificationList>,
    ...
}
```

创建一个 `Root` 需要传入你的业务视图、窗口和上下文：

[crates/ui/src/root.rs:93-113](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L93-L113) — `Root::new` 构造函数，保存业务视图并初始化各浮层为空。

```rust
pub fn new(view: impl Into<AnyView>, window: &mut Window, cx: &mut Context<Self>) -> Self {
    Self {
        style: StyleRefinement::default(),
        view: view.into(),
        active_sheet: None,
        active_dialogs: Vec::new(),
        ...
        notification: cx.new(|cx| NotificationList::new(window, cx)),
        tooltip_overlay: cx.new(|_| TooltipOverlay::new()),
        ...
    }
}
```

`root::init`（由 `init(cx)` 调用）负责在 `Root` 的 key context 上注册快捷键：

[crates/ui/src/root.rs:24-33](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L24-L33) — 绑定 Tab / Shift-Tab / 复制（cmd-c 或 ctrl-c）到 `Root` 上下文。

```rust
pub(crate) fn init(cx: &mut App) {
    cx.bind_keys([
        KeyBinding::new("tab", Tab, Some(CONTEXT)),
        KeyBinding::new("shift-tab", TabPrev, Some(CONTEXT)),
        #[cfg(target_os = "macos")]
        KeyBinding::new("cmd-c", Copy, Some(CONTEXT)),
        #[cfg(not(target_os = "macos"))]
        KeyBinding::new("ctrl-c", Copy, Some(CONTEXT)),
    ]);
}
```

其它代码如何拿到 `Root` 来弹出浮层？看这两个静态方法，注意它们的 panic 信息正是这条「必须顶层是 Root」约定的体现：

[crates/ui/src/root.rs:132-150](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L132-L150) — `Root::update` / `Root::read`，通过 `window.root::<Root>()` 定位唯一实例，找不到就 panic。

```rust
pub fn update<F, R>(window: &mut Window, cx: &mut App, f: F) -> R
where F: FnOnce(&mut Self, &mut Window, &mut Context<Self>) -> R,
{
    let root = window
        .root::<Root>()
        .flatten()
        .expect("BUG: window first layer should be a gpui_component::Root.");
    root.update(cx, |root, cx| f(root, window, cx))
}
```

最后看 `Root` 怎么渲染：它把你传入的业务视图作为 `child`，并叠加浮层覆盖物。`key_context(CONTEXT)` 让前面注册的快捷键只在这个窗口生效：

[crates/ui/src/root.rs:539-568](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L539-L568) — `Root` 的 `render`，把业务视图和各 overlay 组合，可选地用 `window_border()` 包裹（Linux 客户端装饰）。

```rust
impl Render for Root {
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        window.set_rem_size(cx.theme().font_size);
        let inner = div()
            .id("root")
            .key_context(CONTEXT)
            .on_action(cx.listener(Self::on_action_tab))
            // ... 样式与背景
            .child(TextSelectionController)
            .child(self.view.clone())            // 你的业务视图
            .child(self.tooltip_overlay.clone())
            .child(self.native_menu_overlay.clone());
        // ...
    }
}
```

可以看到，`self.view.clone()` 才是真正显示在窗口里的业务内容，`Root` 在它外面包了一层管理壳。

#### 4.2.4 代码实践

**实践目标**：理解「顶层必须是 Root」这条约定，并学会通过 `window` 上的扩展方法调用 Root 能力（这里以通知为例，相关 API 在后续浮层讲义详讲）。

**操作步骤**：

1. 打开 `crates/story/src/lib.rs`，找到 `StoryContainer` 的 `toolbar_buttons`（约 584 行起），你会看到按钮点击里调用了 `window.push_notification("...", cx)`。
2. 思考：`window.push_notification` 内部最终一定会通过 `window.root::<Root>()` 找到 Root，再调用 `root.push_notification(...)`。它之所以能成功，正是因为 `create_new_window` 把顶层视图设成了 `Root`（见 4.3.3）。
3. 阅读源码跟踪：从 [crates/ui/src/root.rs:389-398](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L389-L398) 的 `push_notification` 方法看它如何把通知写入 `self.notification`。

**需要观察的现象**：通知能正常弹出，依赖「窗口顶层是 Root」这条隐式契约成立。

**预期结果**：你将理解——凡是需要窗口级浮层（通知、对话框、抽屉）的 API，前提都是窗口第一层视图是 `Root`。

#### 4.2.5 小练习与答案

**练习 1**：如果你不把窗口的第一个视图设为 `Root`，而是直接放自己的业务视图，调用 `window.push_notification` 会怎样？

> **参考答案**：`window.root::<Root>()` 会返回 `None`，相关方法要么静默失败要么 panic（例如 `Root::update` 会 `expect` 失败，打印 `"BUG: window first layer should be a gpui_component::Root."`）。因此通知、对话框、抽屉等窗口级功能都会不可用。

**练习 2**：`Root` 里的 `view: AnyView` 字段是干什么用的？

> **参考答案**：它是用户真正想要显示的业务视图。`Root` 本身只是个「管理壳」，在 `render` 时通过 `child(self.view.clone())` 把业务视图渲染出来，并在其外层叠加通知、对话框等浮层和键盘导航逻辑。

### 4.3 模块三：完整启动流程 Application → open_window → Root

#### 4.3.1 概念说明

有了 `init` 和 `Root`，我们就能拼出完整的应用启动流程。gpui-component 的所有应用（无论是最小示例还是 Story Gallery）都遵循同一个骨架：

```text
1. 创建 Application（GPUI 的事件循环容器）
2. app.run(|cx| {
3.     gpui_component::init(cx);          // 必须最先调用
4.     cx.open_window(options, |window, cx| {
5.         let view = cx.new(|_| 你的业务视图);
6.         cx.new(|cx| Root::new(view, window, cx))  // 用 Root 包裹
       })
   })
```

这个骨架之所以重要，是因为它把「平台事件循环」「组件库初始化」「窗口与视图创建」「Root 托管」四件事按固定顺序串了起来。无论项目多复杂，入口都长这样。

#### 4.3.2 核心流程

最小示例 `hello_world` 是理解这个骨架的最佳样本。它的结构分两块：

- **业务视图 `Example`**：实现 `Render`，用 `div()` 摆放一个标题文本和一个按钮。
- **`main` 函数**：按上面的骨架创建应用、初始化、开窗口、用 Root 包裹。

`main` 里有一个细节值得注意：`cx.open_window` 被放在 `cx.spawn(async move |cx| { ... })` 里异步执行。这是因为开窗口涉及平台层操作，放在异步任务里更稳妥；`.detach()` 表示这个异步任务不需要被等待。当然，开窗口本身也可以直接同步调用，异步只是其中一种写法。

#### 4.3.3 源码精读

先看最小示例的完整入口：

[examples/hello_world/src/main.rs:23-41](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/examples/hello_world/src/main.rs#L23-L41) — `main` 函数，完整展示了 `application().run` → `init` → `open_window` → `Root` 骨架。

```rust
fn main() {
    gpui_platform::application().run(move |cx| {
        // This must be called before using any GPUI Component features.
        gpui_component::init(cx);

        cx.spawn(async move |cx| {
            cx.open_window(WindowOptions::default(), |window, cx| {
                let view = cx.new(|_| Example);
                // This first level on the window, should be a Root.
                cx.new(|cx| {
                    Root::new(view, window, cx).bg(cx.theme().background)
                })
            })
            .expect("Failed to open window");
        })
        .detach();
    });
}
```

读这段代码，注意几个关键点：

1. `gpui_platform::application()` 创建 `Application`，`.run(...)` 启动事件循环，闭包参数 `cx` 就是 `&mut App`。
2. **第一行就是 `gpui_component::init(cx);`**，注释也强调「必须在使用任何 GPUI Component 特性之前」。
3. `cx.open_window` 的闭包返回的，是 `cx.new(|cx| Root::new(view, window, cx).bg(...))`——即窗口的第一个视图就是 `Root`。注释 `"This first level on the window, should be a Root."` 正是本讲强调的约定。
4. `Root` 实现了 `Styled` trait，所以可以直接链式调用 `.bg(cx.theme().background)` 来设置背景色。

再看业务视图 `Example` 是怎么写的：

[examples/hello_world/src/main.rs:4-21](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/examples/hello_world/src/main.rs#L4-L21) — 业务视图 `Example`，实现 `Render`，用 `div` 摆放文本和按钮，按钮点击打印日志。

```rust
pub struct Example;
impl Render for Example {
    fn render(&mut self, _: &mut Window, _: &mut Context<Self>) -> impl IntoElement {
        div()
            .v_flex().gap_2().size_full()
            .items_center().justify_center()
            .child("Hello, World!")
            .child(
                Button::new("ok")
                    .primary()
                    .label("Let's Go!")
                    .on_click(|_, _, _| println!("Clicked!")),
            )
    }
}
```

这就是一个「无状态业务视图」的最小写法：结构体 `Example` 不持有任何字段，`render` 每次都返回一个用 `div()` + `Button` 组装出来的元素树。

**真实项目如何复用这个骨架**：Story Gallery 把同样的流程封装成了 `create_new_window`。先看 Gallery 入口：

[crates/story/src/main.rs:4-20](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/main.rs#L4-L20) — Gallery 入口，调用更高层的 `init` 和 `create_new_window`。

```rust
fn main() {
    let app = gpui_platform::application().with_assets(Assets);
    let name = std::env::args().nth(1);   // 支持 cargo run -- <story_name>
    app.run(move |cx| {
        init(cx);
        cx.activate(true);
        create_new_window(
            "GPUI Component",
            move |window, cx| Gallery::view(name.as_deref(), window, cx),
            cx,
        );
    });
}
```

注意这里调用的 `init` 是 **`gpui_component_story::init`**（来自 story 库），它内部会先调用核心的 `gpui_component::init`，再额外初始化日志、主题预设、HTTP 客户端、快捷键等：

[crates/story/src/lib.rs:156-184](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/lib.rs#L156-L184) — Story 库的 `init`，先 `gpui_component::init(cx)`（第 183 行），再追加 tracing、AppState、themes、stories 等项目级初始化。

而 `create_new_window` 的核心，最终还是落回到「用 `Root` 包裹」这一步：

[crates/story/src/lib.rs:127-141](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/lib.rs#L127-L141) — `create_new_window_with_size` 内部 `open_window` 后，用 `StoryRoot` 包业务视图，再用 `Root` 作为窗口第一层。

```rust
let window = cx
    .open_window(options, |window, cx| {
        let view = crate_view_fn(window, cx);
        let story_root = cx.new(|cx| StoryRoot::new(title.clone(), view, window, cx));
        // ...
        cx.new(|cx| Root::new(story_root, window, cx))   // 窗口第一层仍是 Root
    })
    .expect("failed to open window");
```

可以看到：即便 Gallery 多包了一层 `StoryRoot`（用来放标题栏、菜单层），最外层第一层依然是 `Root`。这就是「窗口顶层必须是 Root」这条约定在真实项目里的体现。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：参照 `hello_world`，从零编写一个属于你自己的最小应用：窗口里渲染一个标题文本和一个按钮，按钮点击后在控制台打印一句话。

**操作步骤**：

1. 复制 `examples/hello_world` 目录，改名为 `examples/my_first_app`（或直接在 `hello_world` 上修改后用 `git checkout` 恢复，避免改动源码）。
2. 修改 `Cargo.toml` 里的 `name = "my_first_app"`，其余依赖保持不变：

   ```toml
   [package]
   name = "my_first_app"
   version = "0.1.0"
   edition.workspace = true

   [dependencies]
   anyhow.workspace = true
   gpui.workspace = true
   gpui_platform.workspace = true
   gpui-component = { workspace = true }
   ```

3. 编辑 `src/main.rs`，把业务视图改成你的版本，例如：

   ```rust
   use gpui::*;
   use gpui_component::{button::*, *};

   pub struct MyView;
   impl Render for MyView {
       fn render(&mut self, _: &mut Window, _: &mut Context<Self>) -> impl IntoElement {
           div()
               .v_flex()
               .gap_2()
               .size_full()
               .items_center()
               .justify_center()
               .child("我的第一个 GPUI Component 应用")
               .child(
                   Button::new("greet")
                       .primary()
                       .label("点我打招呼")
                       .on_click(|_, _, _| println!("你好，gpui-component！")),
               )
       }
   }

   fn main() {
       gpui_platform::application().run(move |cx| {
           gpui_component::init(cx);          // 第一步：初始化
           cx.spawn(async move |cx| {
               cx.open_window(WindowOptions::default(), |window, cx| {
                   let view = cx.new(|_| MyView);
                   cx.new(|cx| Root::new(view, window, cx).bg(cx.theme().background))
                   //            ^^^^ 第二步：窗口第一层必须是 Root
               })
               .expect("Failed to open window");
           })
           .detach();
       });
   }
   ```

   > 说明：上面的 `.child("我的第一个 GPUI Component 应用")` 用的是字符串形式，这是 `hello_world` 原版就采用的、确定可用的写法。等学了后续文本组件讲义后，你也可以换成更丰富的文本展示组件。

4. 运行你的应用：`cargo run -p my_first_app`。

**需要观察的现象**：

- 窗口正常弹出，背景为主题色（因为 `.bg(cx.theme().background)`）。
- 窗口里有标题文本和一个 primary 风格的按钮。
- 点击按钮后，终端控制台打印出 `你好，gpui-component！`。

**预期结果**：你得到一个完全由自己搭起来的最小应用，验证了「init → open_window → Root」三步骨架的正确性。

**进阶（可选）**：把 `cx.open_window` 从 `cx.spawn(...).detach()` 里拿出来，直接同步调用 `cx.open_window(...)`（仍在 `app.run` 的闭包内），对比两种写法都能正常开窗，体会异步包装并非必需。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `Root::new(view, window, cx)` 改成直接 `cx.new(|_| Example)`（不包 Root），程序还能编译运行吗？会有什么问题？

> **参考答案**：能编译也能运行，窗口也会显示 `Example` 的内容。但窗口顶层不再是 `Root`，于是所有依赖 `window.root::<Root>()` 的窗口级能力（通知、对话框、抽屉、Tab 焦点循环、文本选择）都会失效。这正是为什么注释反复强调「This first level on the window, should be a Root.」。

**练习 2**：Story Gallery 的 `init`（`gpui_component_story::init`）和核心库的 `gpui_component::init` 是什么关系？

> **参考答案**：前者包含后者。`gpui_component_story::init` 在第 183 行先调用 `gpui_component::init(cx)` 铺好组件库地基，再追加项目专属的初始化（tracing 日志、AppState、themes 主题预设、stories、HTTP 客户端、全局快捷键等）。这是一种「核心初始化 + 项目扩展初始化」的分层模式，你也可以在自己的项目里照搬。

## 5. 综合实践

把本讲的三个模块串起来，完成一个「会弹通知的最小应用」综合任务：

1. 基于 4.3.4 你已经搭好的 `my_first_app`，把按钮的 `on_click` 从 `println!` 改成弹一条通知。gpui-component 提供了基于 `Root` 的窗口级通知扩展方法（在 [crates/story/src/lib.rs:590-600](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/lib.rs#L590-L600) 可以看到 `window.push_notification("...", cx)` 的真实用法）。
2. 把 `on_click` 闭包的参数补全为 `|_, window, cx|`（按钮点击回调的第二个参数是 `&mut Window`），调用 `window.push_notification("按钮被点击了！", cx)`。
3. 运行应用，点击按钮，观察右上角（或主题配置的通知位置）是否弹出通知。
4. 反思：这条通知之所以能弹出，依赖了本讲的哪两个关键点？
   - **点 A**：`init(cx)` 已被调用，`dialog`/`notification` 等子系统已注册。
   - **点 B**：窗口第一层是 `Root`，所以 `window.push_notification` 能通过 `window.root::<Root>()` 找到承载通知的 `Root` 实例。

> ⚠️ 如果你的应用里 `window.push_notification` 方法找不到，说明需要确认是否引入了 `WindowExt` trait（在 `lib.rs` 中由 `pub use window_ext::WindowExt;` 导出，参考 [crates/ui/src/lib.rs:100](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/lib.rs#L100)）。引入该 trait 后该方法即可用；通知/对话框的完整 API 会在 [u5-l1](u5-l1-dialog-and-alert.md) 与 [u5-l3](u5-l3-sheet-and-notification.md) 详讲。

完成这个综合实践后，你就真正掌握了「初始化 + Root 托管」如何共同支撑起窗口级交互。

## 6. 本讲小结

- **`init(cx)` 是强制前置步骤**：它按固定顺序（主题最前）注册组件库的所有全局子系统，必须在使用任何组件特性之前调用。
- **`init` 内部是子模块初始化的拓扑排序**：`theme`、`root`、`dock`、`input` 等各有 `*_init(cx)`，`init` 把它们集中调用，`inspector` 还带条件编译。
- **窗口的第一个视图必须是 `Root`**：这是 gpui-component 的核心约定，`Root` 集中托管 Sheet、Dialog、Notification、焦点导航和文本选择。
- **`Root` 是个「管理壳」**：它把你传入的业务视图 `view` 作为子元素渲染，外层叠加各类浮层与 overlay。
- **标准启动骨架**：`application().run` → `init(cx)` → `open_window` → `cx.new(|cx| Root::new(view, window, cx))`，所有应用（含 Story Gallery）都遵循此骨架。
- **真实项目复用骨架**：Story Gallery 把流程封装成 `init` + `create_new_window`，但窗口第一层依然是 `Root`。

## 7. 下一步学习建议

本讲解决了「应用如何启动、顶层结构是什么」。接下来建议：

1. **[u2-l1 主题系统](u2-l1-theme-system.md)**：本讲多次出现 `cx.theme()`，下一讲深入讲解 `Theme` 单例、`ThemeColor` 配色与明暗模式切换——这是所有组件视觉的基础。
2. **[u2-l2 样式系统：Styled 与 Sizable](u2-l2-styled-and-sizable.md)**：本讲的 `div().v_flex().gap_2()`、`Root.bg(...)` 都依赖 `Styled` trait，下一讲系统讲解类 CSS 的样式 API 与尺寸体系。
3. 继续阅读源码：通读 [crates/ui/src/root.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs) 中 `open_dialog` / `open_sheet_at` / `push_notification` 等方法，提前感受浮层讲义要展开的内容。
4. 跑通你在 4.3.4 创建的 `my_first_app`，作为后续所有讲义的「实验沙盒」——之后每学一个组件，都可以往这个应用里加一个演示按钮。
