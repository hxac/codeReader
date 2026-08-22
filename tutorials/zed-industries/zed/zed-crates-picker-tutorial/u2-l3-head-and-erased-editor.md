# Head 与 ErasedEditor：可搜索与不可搜索两种头部

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Head` 枚举两个变体（`Editor` 与 `Empty`）各自的用途，以及它们如何影响 `Picker` 的焦点行为。
2. 解释 `ErasedEditor` trait 对象为什么存在：它如何让 picker crate 完全不依赖 editor crate，而由 `ERASED_EDITOR_FACTORY` 全局工厂在运行期注入真正的编辑器实现。
3. 徒手画出两条完整调用链：
   - 键入文本 → `BufferEdited` 事件 → `Head::with_editor` 的订阅回调 → `Picker::on_input_editor_event` → `update_matches`；
   - 点击窗口外 → 失焦 → `on_empty_head_blur` / `on_input_editor_event(Blurred)` → `cancel` → `DismissEvent`。
4. 理解 `weak_entity`、`cx.on_blur`、`track_focus` 这三个 GPUI 工具在「解除耦合」与「失焦取消」中的用法。

## 2. 前置知识

### 2.1 trait 对象与「类型擦除」

Rust 中 `Arc<dyn ErasedEditor>` 是一个** trait 对象**：`dyn ErasedEditor` 表示「任意实现了 `ErasedEditor` 这个 trait 的具体类型」，`Arc` 让它可以在多个所有者之间共享。调用方只能通过 trait 定义的方法操作它，看不见内部的 конкретный 类型——这就是「擦除」（erased）的含义。好处是：picker 不需要知道编辑器到底是什么，只要它会读写文本、会报告焦点即可。

### 2.2 GPUI 的焦点系统：FocusHandle

GPUI 中焦点不挂在「控件」上，而是挂在 [`FocusHandle`](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/head.rs#L61-L72) 这个轻量句柄上：

- `cx.focus_handle()` 创建一个句柄；
- 元素树里某个 `div` 调用 `.track_focus(&handle)`，就把「这块屏幕区域」与句柄绑定；
- `handle.focus(window, cx)` 把焦点交给该句柄；
- 一个实体实现了 `Focusable` trait（返回自己的 `FocusHandle`），框架才知道「聚焦这个实体」是什么意思。

关键结论：**句柄必须被某个真实元素 `track_focus`，焦点才有落点**。本讲的 `EmptyHead` 正是「只为持焦而存在的隐形元素」。

### 2.3 强引用、弱引用与订阅

- `Entity<T>` 是强引用，会维持 `T` 存活；`WeakEntity<T>` 是弱引用，实体释放后调用其 `update` 会返回 `Err`。
- `editor.subscribe(...)` 返回一个 `Subscription`，它被 drop 时取消订阅；`.detach()` 让它脱离调用方生命周期、长期存活。
- 如果订阅回调里捕获的是强 `Entity<Picker<D>>`，那么只要编辑器活着，Picker 就永远不会被释放——这就是互相持有导致的内存泄漏。`Head::with_editor` 用 `cx.weak_entity()` 规避了这一点。

### 2.4 与前几讲的衔接

u2-l2 介绍了七个构造函数只是「参数收集器」，最终都汇入私有的 `Picker::new`。本讲展开其中一个此前未细讲的参数：`head`。u1-l3 提过「editor 仅是 picker 的测试依赖」，本讲将解释这背后的机制。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `crates/picker/src/head.rs` | Head 枚举与 EmptyHead 的定义（85 行的小文件） | 全文件精读 |
| `crates/picker/src/picker.rs` | 库根：Picker 结构、构造函数、事件处理 | head 的创建与消费、失焦取消路径 |
| `crates/picker/src/render.rs` | Render 实现 | 头部在结果面板中的两个挂点 |
| `crates/ui_input/src/ui_input.rs` | ErasedEditor trait、事件枚举、全局工厂 | 三段核心定义 |
| `crates/ui_input/src/input_field.rs` | 工厂的另一个消费者 | 印证工厂模式的通用性 |
| `crates/editor/src/editor.rs` | 工厂的注册处与 ErasedEditorImpl 实现 | `editor::init` 与 `subscribe` 的事件翻译 |
| `crates/picker/Cargo.toml` | 依赖声明 | ui_input 是正式依赖、editor 只是 dev-dependency |

## 4. 核心概念与源码讲解

### 4.1 Head 枚举：可搜索与不可搜索两种头部

#### 4.1.1 概念说明

picker 顶部的输入区（head）只有两种形态：

- **`Head::Editor(Arc<dyn ErasedEditor>)`**：带一个查询输入框，用户可以键入文本来过滤列表。命令面板、文件查找器都是这种。
- **`Head::Empty(Entity<EmptyHead>)`**：没有输入框，只是一个纯列表（比如某些确认型选择器）。此时头部由一个**不可见但可持有焦点**的 `EmptyHead` 实体充当。

为什么要区分？因为「不可搜索的 picker」仍然需要焦点——键盘的上下键、回车确认都要求 picker 是焦点元素，否则按键事件不会路由给它。没有编辑器，就得有别的持焦载体，这就是 `EmptyHead` 存在的全部理由。

注意 `Head` 是 `pub(crate)` 私有类型，外部 crate **无法直接构造**，只能通过构造函数间接选择形态：

```rust
// head.rs
pub(crate) enum Head {
    Editor(Arc<dyn ErasedEditor>),
    Empty(Entity<EmptyHead>),
}
```

#### 4.1.2 核心流程

构造函数到 Head 形态的映射关系：

```text
Picker::uniform_list / list / uniform_list_with_preview / list_with_preview
    └─> Head::editor(placeholder, Self::on_input_editor_event, ...)
        └─> 从全局工厂要一个编辑器

Picker::list_with_preview_and_query_editor
    └─> Head::with_editor(query_editor, ...)     # 编辑器由调用方造好传入

Picker::nonsearchable_uniform_list / nonsearchable_list
    └─> Head::empty(Self::on_empty_head_blur, ...)
        └─> 新建 EmptyHead 实体并注册失焦回调
```

`Head` 形态对焦点的影响集中在 `Focusable` 实现里：`Picker` 自己不创建焦点句柄，而是**把焦点完全委托给头部**——可搜索时焦点在编辑器里，不可搜索时焦点在隐形 div 上。于是 `Picker::focus()`、workspace 注册 reopenable picker 用的 `focus_handle`，全都经由头部获得。

#### 4.1.3 源码精读

Head 枚举的定义（含两段说明用途的文档注释）：

> [head.rs:L7-L14](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/head.rs#L7-L14)
> 定义 `Head`：`Editor` 变体携带一个可过滤列表的编辑器 trait 对象；`Empty` 变体携带 `Entity<EmptyHead>`，表示这个 picker 只是一个条目列表。

`Picker` 结构体把 head 作为字段持有（`head.rs` 中的类型在这里被消费）：

> [picker.rs:L127-L153](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L127-L153)
> `Picker<D>` 的字段表。第 130 行的 `head: Head` 就是本讲主角，它与 `delegate`、`element_container`、`preview` 并列为 Picker 的四大组成。

焦点委托——本模块最重要的一小段代码：

> [picker.rs:L441-L448](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L441-L448)
> `impl Focusable for Picker`：按 head 变体分发——编辑器存在时返回编辑器的焦点句柄，否则返回 `EmptyHead` 的句柄。**这决定了「聚焦 picker」在两种形态下分别落到哪里。**

两个可搜索构造函数如何创建头部：

> [picker.rs:L460-L469](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L460-L469)
> `Picker::uniform_list`：调用 `Head::editor(delegate.placeholder_text(...), Self::on_input_editor_event, ...)`。注意传入的是**关联函数指针** `Self::on_input_editor_event`，它将成为编辑器事件的统一入口。

不可搜索构造函数对照组：

> [picker.rs:L553-L570](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L553-L570)
> `nonsearchable_uniform_list` 与 `nonsearchable_list`：唯一差别是 `Head::empty(Self::on_empty_head_blur, ...)`，注册的是**失焦回调**而非编辑器事件回调。

外部传入编辑器的特殊变体：

> [picker.rs:L525-L549](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L525-L549)
> `list_with_preview_and_query_editor`：调用方自己造好 `Arc<dyn ErasedEditor>` 传入，走 `Head::with_editor`。这样宿主可以预先定制编辑器（比如预填文本），再交给 picker 接管。

渲染侧，头部在结果面板有两个挂点（顶部或底部，取决于 `editor_position`）：

> [render.rs:L209-L219](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/render.rs#L209-L219)
> `render_results` 面板顶部：`Head::Editor` 时渲染编辑器（`Start` 位置），`Head::Empty` 时渲染 `h_flex().child(empty_head.clone())`——把隐形实体放进元素树，让它真正参与布局与焦点跟踪。

> [render.rs:L270-L280](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/render.rs#L270-L280)
> 面板底部：`End` 位置时编辑器渲染在这里；`Empty` 同样兜底渲染。两个挂点保证 `editor_position` 委托方法返回 `Start`/`End` 都有归宿。

#### 4.1.4 代码实践

**实践：用两个构造函数分别构建 picker，观察 query() 的差异**

1. **实践目标**：亲手验证 Head 形态对 `query()` 与焦点的影响。
2. **操作步骤**：
   - 打开 `crates/picker/src/picker.rs` 文件末尾的 `tests` 模块，找到 `init_test` 与任一使用 `Picker::uniform_list` 的测试（如 `test_clicking_non_selectable_item_does_not_confirm`）。
   - 在自己的 fork 中仿照该测试新写一个 `#[gpui::test]`：用同一个 `TestDelegate` 分别以 `Picker::nonsearchable_uniform_list(delegate, window, cx)` 构造视图。
   - 在断言中调用 `picker.update(cx, |picker, cx| assert_eq!(picker.query(cx), ""))`，并读取 `picker.focus_handle(cx)` 确认非空。
3. **需要观察的现象**：`nonsearchable` 构造出的 picker，`query(cx)` 恒返回空字符串（因为 `Head::Empty` 分支直接返回 `""`）；而 `uniform_list` 构造出的 picker 初始 query 同样为空，但可以调用 `picker.set_query("abc", window, cx)` 后读到 `"abc"`。
4. **预期结果**：两条断言均通过；`set_query` 对 `nonsearchable` 版本是无操作（`if let Head::Editor` 不匹配）。
5. 若无法运行测试环境，本实践可降级为源码阅读：对照 [picker.rs:L1315-L1327](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1315-L1327) 中 `query`/`set_query` 对两个变体的不同处理，写下结论——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Head` 被声明为 `pub(crate)` 而不是 `pub`？如果改成 `pub` 会有什么后果？

**答案**：`pub(crate)` 保证外部 crate 无法绕过七个构造函数、自己拼装 `Head`。头部与订阅、失焦回调、编辑器工厂的装配逻辑（`Head::editor`/`with_editor`/`empty`）是一个整体，暴露枚举会让调用方可能构造出「没注册事件回调的 Editor」或「没绑定 blur 回调的 Empty」，破坏失焦取消等不变量。同时 `EmptyHead` 也是 `pub(crate)`，对外不可见，外部只能通过 `Picker` 的公开 API（如 `query`、`set_query`）间接与头部交互。库根的 `pub use ui_input::ErasedEditor;`（[picker.rs:L43](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L43)）只是把 trait 类型再导出，供 delegate 在 `render_editor` 签名中使用。

**练习 2**：`Picker::focus()`（[picker.rs:L804-L806](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L804-L806)）对可搜索与不可搜索 picker 分别把焦点给了谁？

**答案**：它调用 `self.focus_handle(cx).focus(window, cx)`，而 `focus_handle` 来自 `Focusable` 实现的 head 分发。可搜索时焦点进入查询编辑器（用户可以直接打字）；不可搜索时焦点落在 `EmptyHead` 渲染的那个隐形 `div` 上（用户按上下键即触发 `SelectNext`/`SelectPrevious`）。

### 4.2 ErasedEditor 与 ERASED_EDITOR_FACTORY：延迟决定编辑器实现

#### 4.2.1 概念说明

`ErasedEditor` 是定义在 ui_input crate 里的 trait，把「一个单行文本编辑器」抽象成十来个方法：读写文本、设置占位符、全选、聚焦、渲染、订阅事件等。picker 的查询框只依赖这组方法，**完全不知道也不需要知道**背后是 Zed 的 `Editor` 实体。

为什么这么绕？看 picker 的依赖表就明白了：picker 的正式依赖里有 `ui_input` 而**没有 editor**；editor 只出现在 dev-dependencies 里：

> [Cargo.toml:L31-L38](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/Cargo.toml#L31-L38)
> `[dependencies]` 含 `ui.workspace` 与 `ui_input.workspace`；`[dev-dependencies]` 里才有 `editor`。即发布产物中 picker 不链接 editor 的实现，测试时才引入。

ui_input 的 crate 文档一语道破这样分层的原因：

> [ui_input.rs:L1-L3](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/ui_input/src/ui_input.rs#L1-L3)
> 「本 crate 提供表单类 UI 组件（输入框、数字框等）。它不能放进 `ui` crate，因为它依赖 `editor`。」

也就是说：editor（完整的代码编辑器，数千行、依赖 syntax/multi_buffer 等一大串 crate）太重，ui 和 picker 都不想直接依赖它；于是 editor 的能力被「擦除」进 `ErasedEditor` trait，真正的实现由 editor crate 自己在初始化时注册进一个全局工厂。

`ERASED_EDITOR_FACTORY` 是一个 `OnceLock` 静态变量——进程生命周期里只能写入一次的槽位。picker 在构造头部时从中**取**出函数指针并调用；editor crate 在 `editor::init` 时向其中**放**入构造函数。两边在编译期互不知晓，在运行期经此会合。

#### 4.2.2 核心流程

编辑器的诞生与事件订阅，完整时序：

```text
应用启动
  └─ zed 启动时调用 editor::init(cx)
       └─ ERASED_EDITOR_FACTORY.set(|window, cx| {
              cx.new(|cx| Editor::single_line(window, cx))
                .update(cx, |editor, cx| editor.erased(cx))   // 包成 ErasedEditorImpl
          })

用户打开某个 picker（如命令面板）
  └─ Picker::uniform_list(delegate, window, cx)
       └─ Head::editor(placeholder, Self::on_input_editor_event, window, cx)
            ├─ editor = (ERASED_EDITOR_FACTORY.get().unwrap())(window, cx)
            │     # 若 editor::init 从未被调用，这里 unwrap 会 panic
            └─ Head::with_editor(editor, placeholder, edit_handler, window, cx)
                 ├─ editor.set_placeholder_text(...)
                 ├─ let this = cx.weak_entity();          # WeakEntity<Picker<D>>
                 ├─ editor.subscribe(Box::new(move |event, window, cx| {
                 │      this.update(cx, |this, cx| (edit_handler)(this, &event, window, cx)).ok();
                 │  })).detach();
                 └─ 返回 Head::Editor(editor)

用户键入一个字符
  └─ Editor 内部 buffer 变化，emit EditorEvent::BufferEdited
       └─ ErasedEditorImpl::subscribe 的 window.subscribe 回调
            └─ 翻译成 ErasedEditorEvent::BufferEdited
                 └─ Head 注册的闭包：this.update(...) 恢复 Picker 强引用
                      └─ (edit_handler)(this, &event, ...) = Picker::on_input_editor_event
                           └─ match BufferEdited => self.update_matches(query, ...)
```

#### 4.2.3 源码精读

trait 全貌与事件枚举、工厂：

> [ui_input.rs:L16-L37](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/ui_input/src/ui_input.rs#L16-L37)
> `ErasedEditor` trait：文本读写（`text`/`set_text`/`clear`）、占位符（`set_placeholder_text`）、光标（`move_selection_to_end`/`select_all`）、显示控制（`set_masked`/`set_read_only`/`set_multiline`）、`focus_handle`、`subscribe`、`render`、`as_any`。任何能满足这组契约的类型都能充当 picker 的查询框。

> [ui_input.rs:L39-L45](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/ui_input/src/ui_input.rs#L39-L45)
> `ErasedEditorEvent` 只有两个变体 `BufferEdited` 与 `Blurred`——picker 关心的仅此两件事：文本变了、焦点丢了。紧接着的 `ERASED_EDITOR_FACTORY: OnceLock<fn(&mut Window, &mut App) -> Arc<dyn ErasedEditor>>` 就是全局工厂槽位。

工厂的取用与订阅装配（本模块最核心的代码）：

> [head.rs:L16-L25](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/head.rs#L16-L25)
> `Head::editor`：从工厂取出编辑器构造函数并调用（`get().unwrap()` 意味着工厂未初始化时直接 panic），随后转交 `with_editor` 完成装配。

> [head.rs:L27-L47](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/head.rs#L27-L47)
> `Head::with_editor`：先设置占位符；再取 `cx.weak_entity()`（此时 `V = Picker<D>`，弱引用）；然后 `editor.subscribe(...)` 注册一个 `Box` 闭包——闭包内 `this.update(cx, ...).ok()` 尝试恢复强引用并调用 `edit_handler`，实体若已释放则 `.ok()` 静默吞掉 `Err`、回调变空操作。`Subscription` 被 `.detach()`，与编辑器同寿命。

工厂的注册端（editor crate）：

> [editor.rs:L394-L397](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/editor/src/editor.rs#L394-L397)
> `editor::init` 内：`ERASED_EDITOR_FACTORY.set(...)`，构造函数创建一个**单行** `Editor` 实体并调用 `.erased(cx)` 包成 trait 对象。`_ =` 忽略的是「重复注册」的 `Err`（`OnceLock` 二次 set 失败）。

> [editor.rs:L1732-L1734](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/editor/src/editor.rs#L1732-L1734)
> `Editor::erased`：把编辑器实体包进 `ErasedEditorImpl(cx.entity())`。

> [editor.rs:L12082-L12083](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/editor/src/editor.rs#L12082-L12083)
> `ErasedEditorImpl(Entity<Editor>)`：对 `ErasedEditor` 的具体实现，内部就是编辑器实体的包装。

> [editor.rs:L12171-L12185](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/editor/src/editor.rs#L12171-L12185)
> `subscribe` 的实现：`window.subscribe(&self.0, ...)` 订阅 `EditorEvent`，在回调里做**事件翻译**——`BufferEdited`/`Blurred` 两种 `EditorEvent` 映射为同名 `ErasedEditorEvent`，其余事件一律丢弃（`_ => return`）。这是「编辑器内部上百种事件」到「picker 关心的两种事件」的漏斗。

测试中工厂为何可用：

> [picker.rs:L1786-L1793](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1786-L1793)
> `init_test`：先装载设置与主题，再调用 `editor::init(cx)`。这一步除了初始化编辑器，还顺带注册了 `ERASED_EDITOR_FACTORY`——所以 crate 内测试能用 `Picker::uniform_list`（其内部 `Head::editor` 要调工厂）。

工厂模式的另一消费者（印证通用性）：

> [input_field.rs:L56-L61](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/ui_input/src/input_field.rs#L56-L61)
> `InputField::new`（表单输入框组件）同样从 `ERASED_EDITOR_FACTORY` 取编辑器。工厂不是 picker 专用的设施，而是「轻量 crate 需要编辑器能力」的统一入口。

#### 4.2.4 代码实践

**实践：追踪 BufferEdited 事件从按键到 update_matches 的完整旅程**（本讲的主实践）

1. **实践目标**：亲手走通 `Head::with_editor` 订阅回调 → `on_input_editor_event` → `update_matches` 的调用链，并用日志证实它。
2. **操作步骤**：
   - 在自己的 fork 中，给 [picker.rs:L1177-L1202](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1177-L1202) 的 `on_input_editor_event` 函数体开头加一行日志（示例代码）：

     ```rust
     // 示例代码：仅用于观察事件流
     eprintln!("[picker] editor event: {:?}", event);
     ```

   - 运行 crate 内任一测试（如 `cargo test -p picker test_clicking_non_selectable_item_does_not_confirm`），在测试里对 picker 调用 `set_query("a", window, cx)`。
   - 同时在 [head.rs:L38-L41](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/head.rs#L38-L41) 的闭包里临时加一行 `eprintln!("[head] forwarding event")`，观察两层日志的出现顺序。
3. **需要观察的现象**：`set_query` 触发 `EditorEvent::BufferEdited`（因为 `set_text` 修改了 buffer），随后依次出现 `[head] forwarding event` 与 `[picker] editor event: BufferEdited`；测试窗口销毁时可能出现 `Blurred` 事件日志。
4. **预期结果**：日志顺序证明事件链是 `Editor 实体 → ErasedEditorImpl::subscribe（翻译）→ Head 闭包（weak_entity 恢复引用）→ Picker::on_input_editor_event`。注意：若 picker 实体已释放，`[picker]` 日志不会出现而 `[head]` 也不会出现（闭包持有的是 `WeakEntity`，`update` 直接失败返回 `Err`，被 `.ok()` 吞掉）——**待本地验证**。
5. 阅读型替代（无需改码）：对照上述四个文件的行号，手写一份「事件旅行图」，标注每一步的文件、行号、函数名与数据形态变化（`EditorEvent` → `ErasedEditorEvent` → 调用 `update_matches`）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `Head::with_editor` 里的 `cx.weak_entity()` 换成 `cx.entity()`（强引用），会发生什么？

**答案**：订阅闭包将持有 `Entity<Picker<D>>` 强引用，而 `Subscription` 被 `detach()` 后与编辑器同寿命。于是只要编辑器实体还活着，Picker 就永远不会被释放——编辑器是 Picker 的字段，Picker 又经闭包被编辑器的订阅间接持有，形成引用环，造成内存泄漏（picker 关闭后仍驻留内存，且失焦回调还会继续触发）。弱引用让闭包无法维持 Picker 存活；实体释放后 `this.update(...)` 返回 `Err`，`.ok()` 把它变成空操作，订阅自然失效。

**练习 2**：`ERASED_EDITOR_FACTORY` 为什么用 `OnceLock`（只能写一次）而不是 `RefCell<Option<fn>>` 之类的可变槽位？

**答案**：编辑器实现全局只需决定一次——由应用的 `editor::init` 在启动时注册。`OnceLock` 是线程安全的「一次性初始化」原语：注册端 `set` 只在首次成功（editor.rs 中 `_ = ...set(...)` 就是容忍后续重复 init 的失败），消费端 `get()` 无需加锁、任何线程可读。若用可变槽位，就要自己处理并发读写与「工厂中途被换掉」这类不该发生的状态，而 trait 对象 `Arc<dyn ErasedEditor>` 一旦发出去也不应再有「换实现」的语义。

**练习 3**：`Picker::refresh_placeholder`（[picker.rs:L1213-L1223](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1213-L1223)）为什么对 `Head::Empty` 什么都不做？

**答案**：占位符（placeholder）是编辑器的概念——输入框为空时显示的灰色提示文本。`Head::Empty` 没有编辑器，自然没有占位符可言；该方法只在 `Editor` 变体下重新调用 `delegate.placeholder_text` 并 `editor.set_placeholder_text` 刷新，顺带 `cx.notify()` 触发重渲染。

### 4.3 失焦取消路径：Blurred 与 on_blur 两条支线

#### 4.3.1 概念说明

模态选择器的「点外面就关掉」体验，本质上是一个焦点事件：用户点击 picker 之外的区域 → 焦点离开 picker → 框架收到失焦通知 → picker 主动取消自己。

两种头部感知失焦的方式完全不同：

- **可搜索头部**：编辑器自己会发出 `ErasedEditorEvent::Blurred`（经 `ErasedEditorImpl::subscribe` 从 `EditorEvent::Blurred` 翻译而来），`on_input_editor_event` 的 `Blurred` 分支处理。
- **不可搜索头部**：没有任何编辑器，于是 `Head::empty` 用 GPUI 的 `cx.on_blur(&focus_handle, ...)` 直接在 `EmptyHead` 的焦点句柄上注册失焦回调，落到 `on_empty_head_blur`。

两条支线最终都汇合到 `Picker::cancel`：检查 `delegate.should_dismiss()`，清空多选状态，调用 `delegate.dismissed(...)`，并 `cx.emit(DismissEvent)` 通知宿主（通常是 workspace 的模态层）关闭这个视图。

#### 4.3.2 核心流程

两条失焦路径的对照：

```text
路径 A：可搜索（Head::Editor）
  编辑器失焦
    └─ EditorEvent::Blurred
         └─ ErasedEditorEvent::Blurred
              └─ Picker::on_input_editor_event (Blurred 分支)
                   ├─ menu_focused = actions_menu_handle.is_focused(window, cx)
                   │               || actions_menu_handle.is_deployed()
                   │               || delegate.has_another_open_menu(window, cx)
                   └─ 若 draws_own_container() && window.is_window_active() && !menu_focused
                        └─ self.cancel(&menu::Cancel, ...)

路径 B：不可搜索（Head::Empty）
  EmptyHead 的 focus_handle 失焦
    └─ cx.on_blur 注册的回调（Head::empty 中装配）
         └─ Picker::on_empty_head_blur
              └─ 若 window.is_window_active()
                   └─ self.cancel(&menu::Cancel, ...)

汇合点：
  Picker::cancel
    ├─ delegate.should_dismiss() ?   # 默认 true，delegate 可一票否决
    ├─ delegate.clear_selection(cx)
    ├─ delegate.dismissed(window, cx)   # 委托的退场钩子（必答方法）
    └─ cx.emit(DismissEvent)            # 宿主据此关闭模态
```

`EmptyHead` 为什么「不可见但可持焦」？它的 `render` 只返回一个 `div().track_focus(&self.focus_handle(cx))`——一个不带任何尺寸、背景、内容的空 div，唯一作用是把焦点句柄绑定到元素树的这个位置。没有这行 `track_focus`，句柄就不属于任何元素，`focus()` 无处落点，键盘事件与 `on_blur` 都不会触发。

#### 4.3.3 源码精读

`Head::empty` 与 `EmptyHead` 的全部秘密（head.rs 后半部分）：

> [head.rs:L49-L58](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/head.rs#L49-L58)
> `Head::empty`：`cx.new(EmptyHead::new)` 创建实体后，用 `cx.on_blur(&head.focus_handle(cx), window, blur_handler)` 在它的焦点句柄上注册失焦回调，并 `detach()` 让订阅长期存活。构造时传入的 `blur_handler` 就是 `Self::on_empty_head_blur`。

> [head.rs:L61-L72](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/head.rs#L61-L72)
> `EmptyHead` 结构体只有一个字段 `focus_handle: FocusHandle`；`new` 时经 `cx.focus_handle()` 创建。文档注释「An invisible element that can hold focus」就是它的全部职责。

> [head.rs:L74-L84](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/head.rs#L74-L84)
> `Render` 实现返回 `div().track_focus(&self.focus_handle(cx))`——隐形但可持焦的落点；`Focusable` 实现返回自持的句柄。这两段代码虽短，缺一则整个 nonsearchable picker 的键盘交互与失焦取消都会失效。

可搜索头部的失焦分支（带三重守卫）：

> [picker.rs:L1177-L1202](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1177-L1202)
> `on_input_editor_event`：函数开头用 `let ... else { panic!("unexpected call") }` 断言头部确实是 `Editor`（防御性编程——该回调只应被编辑器事件触发）。`BufferEdited` 分支取 `editor.text(cx)` 作为查询并 `update_matches`；`Blurred` 分支先算 `menu_focused`（footer 的 Actions 菜单正被聚焦/已展开，或 delegate 自报还有别的菜单打开），只有在 `draws_own_container()`（Modal/Popover 形态）且窗口激活且菜单未开时才 `cancel`。注释解释了原因：打开 footer 菜单本身会让编辑器失焦，不能因此关掉 picker。

不可搜索头部的失焦回调（守卫更少）：

> [picker.rs:L1204-L1211](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L1204-L1211)
> `on_empty_head_blur`：同样以 `let ... else { panic! }` 断言头部是 `Empty`；只检查 `window.is_window_active()` 就调用 `cancel`。与可搜索路径相比少了菜单守卫与 `draws_own_container` 检查——这两条支线的守卫条件并不对称，阅读时值得留意。

汇合点——取消的完整语义：

> [picker.rs:L989-L996](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L989-L996)
> `Picker::cancel`：`should_dismiss()`（默认 `true`）给 delegate 否决权；随后清多选、调 `delegate.dismissed(...)`（PickerDelegate 九个必答方法之一，u2-l1 讲过）、`cx.emit(DismissEvent)` 让宿主关闭模态。注意 `menu::Cancel` 的行为不只是「关窗口」，还包含 delegate 的退场清理。

谁决定「失焦就关」的适用范围：

> [picker.rs:L780-L787](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L780-L787)
> `draws_own_container()`：只有 `Presentation::Modal` 与 `Presentation::Popover` 返回 `true`；`Embedded` 形态由外层容器负责关闭，失焦不自行 cancel。这是 `Blurred` 分支的第一道守卫（u8-l2 会展开 Presentation 体系）。

`on_blur` / `subscribe` / `detach` 的落点回顾：`Head::empty` 里 `cx.on_blur(...).detach()` 与 `Head::with_editor` 里 `editor.subscribe(...).detach()` 手法一致——都是「注册一次性装配、之后不再管理生命周期」。

#### 4.3.4 代码实践

**实践：写笔记说明 nonsearchable 构造失焦即取消的完整调用链**

1. **实践目标**：不看讲义，凭源码独立复述 `nonsearchable_uniform_list` 从构造到失焦取消的全链路，并解释 `EmptyHead` 为何只渲染 `track_focus` 的 div。
2. **操作步骤**：
   - 从 [picker.rs:L553-L561](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L553-L561) 出发，依次定位每一跳：`Head::empty`（head.rs L49-L58）→ `EmptyHead::new`（L66-L72）→ `cx.on_blur` 注册（L55-L56）→ 渲染时的 `track_focus`（L74-L78）与 render.rs 的挂点（L209-L219）→ `on_empty_head_blur`（picker.rs L1204-L1211）→ `cancel`（L989-L996）。
   - 为每一跳记录：文件、行号、函数、发生了什么、为什么需要这一跳。
   - 回答两个问题并写进笔记：
     a. 如果 `EmptyHead::render` 返回的 div 不调用 `track_focus`，链路会断在哪一环？
     b. `on_empty_head_blur` 里为什么要检查 `window.is_window_active()`？（提示：窗口整体失活——比如用户切换到别的应用——时窗口内所有元素都会失焦，此时不应视为「用户点了外面」。）
3. **需要观察的现象**：笔记能覆盖至少 7 跳且无断链；对 a 的回答应指向「焦点句柄没有绑定到任何元素 → `focus()` 无落点 → 键盘事件不路由 → `on_blur` 无从触发」。
4. **预期结果**：完成的调用链笔记应与 4.3.2 的流程图一致；问题 b 的答案应能区分「窗口内焦点转移」与「窗口整体失活」两种失焦。
5. 本实践为源码阅读型，无需运行命令即可完成。

#### 4.3.5 小练习与答案

**练习 1**：`on_input_editor_event` 和 `on_empty_head_blur` 开头都有 `let Head::X = &self.head else { panic!("unexpected call") }`。既然两个回调只会被各自的头部触发，这个断言是多余的吗？

**答案**：不多余，它是廉价的运行期不变量检查。类型系统无法表达「这个回调只与这个变体绑定」——绑定关系发生在运行期的构造函数里（`Head::editor` 配 `on_input_editor_event`，`Head::empty` 配 `on_empty_head_blur`）。如果未来有人改动构造函数配错了回调，`let-else` 会让错误在第一次触发时立即 panic 暴露，而不是静默走错分支。后续 `match` 也因此能安全地解构出编辑器引用（`BufferEdited` 分支要用 `editor.text(cx)`）。

**练习 2**：`Blurred` 分支里 `menu_focused` 的三个条件分别防住哪种场景？

**答案**：`actions_menu_handle.is_focused(window, cx)` 防住「用户正用键盘操作 footer 的 Actions 菜单」（焦点移进了菜单）；`actions_menu_handle.is_deployed()` 防住「菜单已展开但焦点尚在过渡」的时序缝隙；`delegate.has_another_open_menu(window, cx)` 留给 delegate 自报的额外菜单（PickerDelegate 的同名可选方法，注释说明是为 delegate 自己加的菜单服务的）。三者任一成立都不取消——因为打开菜单必然让编辑器失焦，这是「预期内的失焦」而非「点外面」。

**练习 3**：两条失焦支线的守卫条件不对称（可搜索路径多两道检查）。从产品角度举一个 `on_empty_head_blur` 只检查 `is_window_active` 也够用的理由。

**答案**：非搜索型 picker 没有 footer Actions 菜单与搜索框常驻焦点的交互（它没有编辑器可失焦的菜单弹层场景），最常见的宿主是简单确认列表；对其而言「失焦 ≈ 用户已离开」，加上菜单守卫没有对应场景支撑。当然这是基于当前代码的推断——若未来 nonsearchable picker 也挂上菜单，这个不对称就需要重新审视（这也是阅读源码时值得记下的「设计留白」）。

## 5. 综合实践

**任务：编写《一次点击的旅程》事件追踪笔记，覆盖两种头部**

把本讲三个模块串起来，产出一份可归档的追踪笔记：

1. **准备**：在自己的 fork 中同时给三处加临时日志（示例代码）：`head.rs` 的 `with_editor` 订阅闭包、`picker.rs` 的 `on_input_editor_event` 与 `on_empty_head_blur`。
2. **场景 A（可搜索）**：仿照 crate 内测试写一个 `#[gpui::test]`，用 `Picker::uniform_list` 开窗；先 `set_query("it")` 观察 `BufferEdited` → `update_matches` 链；再用 `window.dispatch_action` 或焦点操作触发失焦，观察 `Blurred` → `cancel` 链。
3. **场景 B（不可搜索）**：用 `Picker::nonsearchable_uniform_list` 构造，重复失焦实验，对照 `on_empty_head_blur` 只检查 `is_window_active` 的差异。
4. **产出**：一份含两个场景的时序图（文字版即可），每一步标注文件、行号与守卫条件；结尾用三句话回答：
   - `ErasedEditor` 解决了什么耦合问题；
   - `EmptyHead` 为什么必须渲染 `track_focus` 的 div；
   - 两条失焦支线为何守卫不同。
5. **验证**：`cargo test -p picker` 全绿后删除日志。若测试环境不可用，标注「待本地验证」并只交源码阅读版笔记。

## 6. 本讲小结

- `Head` 是 `pub(crate)` 的两态枚举：`Editor(Arc<dyn ErasedEditor>)` 提供查询输入框，`Empty(Entity<EmptyHead>)` 用隐形实体兜底持焦；外部只能经七个构造函数选择形态。
- `Picker` 的 `Focusable` 把焦点完全委托给头部——可搜索时焦点在编辑器，不可搜索时焦点在 `EmptyHead` 的隐形 div 上。
- `ErasedEditor` trait + `ERASED_EDITOR_FACTORY`（`OnceLock` 全局工厂）让 picker 的正式依赖只需 ui_input 而无需链接重量级的 editor crate；具体实现 `ErasedEditorImpl` 由 `editor::init` 在启动时注册，测试里的 `init_test` 也靠它补上工厂。
- 事件链：`EditorEvent::BufferEdited/Blurred` → `ErasedEditorImpl::subscribe` 翻译 → `Head::with_editor` 的闭包（持 `WeakEntity<Picker<D>>`，避免引用环）→ `Picker::on_input_editor_event` → `update_matches` / `cancel`。
- 失焦取消有两条支线：可搜索走 `Blurred` 事件（守卫：`draws_own_container` + 窗口激活 + 无菜单打开），不可搜索走 `cx.on_blur`（守卫：仅窗口激活），最终都汇合到 `cancel` → `delegate.dismissed` + `DismissEvent`。
- `EmptyHead` 的 `div().track_focus(&handle)` 是「句柄必须绑定真实元素才能持焦」这一 GPUI 机制的教科书式应用。

## 7. 下一步学习建议

下一讲进入单元三的第一篇 **u3-l1 查询更新流水线**：本讲到 `on_input_editor_event` 调用 `update_matches` 为止，下一讲继续向下追——`update_matches_with_options`、`PendingUpdateMatches` 双任务结构的同步取消语义，以及 `matches_updated` 里列表 reset、预览刷新与 `cx.notify()` 的顺序。

延伸阅读建议：

- 重读 [picker.rs:L441-L448](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/picker/src/picker.rs#L441-L448) 与 GPUI 的 `Focusable`/`FocusHandle` 文档，巩固焦点系统。
- 浏览 `crates/ui_input/src/input_field.rs` 的 `InputField`，看 `ErasedEditor` 的第二个消费者如何复用同一工厂。
- 提前扫一眼 `crates/picker/src/popover_menu.rs`，注意 `PickerPopoverMenu` 同样转发 `DismissEvent`，与本讲的 `cancel → DismissEvent` 呼应（u8-l2 展开）。
