# u7-l3 对话框、菜单与系统通知

## 1. 本讲目标

学完本讲，你应该能够：

- 用 `window.prompt(...)` 弹出确认/警告对话框，并通过返回的 `oneshot::Receiver<usize>` 异步拿到用户点了哪个按钮。
- 说清一条 prompt 从调用到回传答案的完整事件流：`PromptBuilder` 策略分发 → 平台原生对话框（如 macOS `NSAlert`）或 GPUI 自绘兜底 → `PromptResponse` 事件 → `oneshot` 回传 → 清除 prompt、恢复焦点。
- 用 `App::set_prompt_builder` 接管全部 prompt 的渲染方式。
- 用 `Menu` / `MenuItem` / `OsAction` 描述应用菜单，理解菜单项点击最终汇入 `cx.dispatch_action` 这条与键盘快捷键完全相同的派发链路。
- 用 `cx.show_system_notification` 发系统通知、按 tag 撤回，并用 `cx.on_system_notification_response` 接收用户对通知本体或通知按钮的点击。

## 2. 前置知识

本讲是三个「系统集成」主题的合辑，它们共享同一个底层思想：**把操作系统能力抽象成 GPUI 的普通 API**。阅读前请确保已理解以下前置概念（前几讲已建立）：

- **Platform 抽象**（u7-l1）：`App` 持有 `Rc<dyn Platform>`，每个窗口持 `Box<dyn PlatformWindow>`。对话框、菜单、通知最终都落到这两个 trait 的某个方法上；平台给不出的能力，trait 里有默认的 no-op 或「返回 None」兜底。
- **Action 与派发链路**（u5-l3/u5-l4）：Action 是命名的用户意图，派发走「全局捕获 → 路径捕获 → 路径冒泡 → 全局冒泡」。本讲你会发现：**菜单只是 Action 的又一个触发入口**。
- **oneshot channel**：`futures::channel::oneshot` 是一次性、单值、有界的异步信道——`Sender::send` 只能成功一次，`Receiver` 是可 `await` 的 future。GPUI 用它把「用户将来会点某个按钮」表达成一个值。
- **实体与事件**（u2-l2/u2-l3）：`cx.new` 创建实体，`cx.emit` 发事件，`cx.subscribe` 订阅事件。自绘 prompt 就是一个会 emit `PromptResponse` 事件的普通实体视图。
- **窗口生命周期**（u7-l2）：`WindowHandle` / `AnyWindowHandle`、`window.remove_window()`、窗口关闭回调。

一个贯穿本讲的直觉：这三块 API 都是**请求-响应**模型，且响应都异步到达。GPUI 的做法是「发起时同步返回一个可等待的东西（Receiver）或注册一个回调」，绝不阻塞主线程——这与平台事件循环的单前台线程模型（u2-l1）一致。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/window/prompts.rs` | prompt 的核心机制：`PromptResponse` 事件、`PromptHandle`、`RenderablePromptHandle`、`PromptBuilder` 枚举、内置的 `FallbackPromptRenderer` 自绘对话框 |
| `src/window.rs` | `Window::prompt`（统一入口）、`build_custom_prompt`、draw 管线中 prompt 的绘制层叠位置、`on_window_should_close` 关闭否决钩子 |
| `src/platform.rs` | `PromptLevel` / `PromptButton` 定义；`PlatformWindow::prompt`、`Platform::set_menus`、系统通知四件套的 trait 契约与 `SystemNotification` 系列数据结构 |
| `src/platform/app_menu.rs` | `Menu` / `MenuItem` / `OsMenu` / `OsAction` 数据模型，以及 `init_app_menus` 注册的三个平台回调 |
| `src/app.rs` | 应用侧包装：`App::set_menus`、`set_prompt_builder`、`show_system_notification`、`on_system_notification_response` 等 |
| `examples/window.rs` | 可运行的 prompt 按钮示例（含非英文按钮写法） |
| `examples/set_menus.rs` | 可运行的菜单示例：checked 勾选、禁用项、子菜单、quit 动作 |
| `examples/system_notifications.rs` | 可运行的通知示例：发送、按 tag 替换、撤回、响应回传 |
| `examples/on_window_close_quit.rs` | 窗口关闭与退出联动，综合实践的参照 |

平台实现侧还会引用两个兄弟 crate 的文件（`gpui_macos`、`gpui_linux`），仅用于对照行为，不要求精读。

## 4. 核心概念与源码讲解

### 4.1 window.prompt：模态对话框的统一入口

#### 4.1.1 概念说明

「模态对话框」（modal dialog）指打断用户与窗口其余部分交互、强制先做选择的弹窗，典型如「未保存，要退出吗？」。GPUI 把它做成 `Window` 的一个方法：传入语气级别、消息、详情和一组按钮，返回一个可 `await` 的 `oneshot::Receiver<usize>`——`await` 的结果就是被点按钮在数组里的下标。

设计上有两个值得注意的取舍：

1. **返回下标而不是按钮枚举**。`prompt` 是泛型方法，按钮由调用方任意给出，所以协议退化为「第几个」。调用方通常写成 `const SAVE: usize = 0` 或直接 `match` 魔数，配合命名常量保证可读。
2. **同一条 API 覆盖两种实现**。优先用操作系统原生对话框（观感一致、支持系统级键盘行为），平台给不出时退回 GPUI 自绘。调用方完全无感。

#### 4.1.2 核心流程

调用 `window.prompt(...)` 后的执行流：

```text
window.prompt(level, message, detail, answers, cx)
  │
  ├─ cx.prompt_builder.take()          # 取出策略对象（同时用于重入保护）
  │    ├─ PromptBuilder::Default：
  │    │    1. platform_window.prompt(...)   # 问平台要原生对话框
  │    │       ├─ Some(receiver) → 直接返回   # 如 macOS 的 NSAlert
  │    │       └─ None → build_custom_prompt # 如 Linux X11，走自绘
  │    └─ PromptBuilder::Custom(闭包)：
  │         无条件 build_custom_prompt         # 应用接管了渲染
  │
  └─ cx.prompt_builder = Some(builder)  # 用完放回
```

自绘路径（`build_custom_prompt`）：

```text
建 oneshot channel → PromptHandle 持 sender
  → 调 builder 闭包得到 RenderablePromptHandle
  → window.prompt = Some(handle)          # 挂到窗口上
  → 返回 receiver 给调用方
用户点击按钮
  → prompt 视图 emit PromptResponse(ix)
  → 订阅回调 sender.send(ix)
  → window.prompt.take()                  # 摘除对话框
  → 恢复 previous_focus
  → 调用方 await 得到 ix
```

#### 4.1.3 源码精读

统一入口在 `src/window.rs`，签名透露了按钮的 ergonomics 设计——`answers: &[T] where T: Clone + Into<PromptButton>`，所以调用方可以直接写 `&["OK", "Cancel"]`：

[src/window.rs:5748-5787](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L5748-L5787) — `Window::prompt` 的全部逻辑：先 `take()` 走 `prompt_builder`（重入保护：对话框未关完再弹第二个会命中 `unreachable!` panic），再把 `&[T]` 逐个 `into()` 成 `PromptButton`，然后按 `Default`/`Custom` 两分支拿 receiver，最后把 builder 放回。

其中「问平台要原生对话框」的一步是 `PlatformWindow` 的必需方法，平台不支持时返回 `None`：

[src/platform.rs:830-836](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform.rs#L830-L836) — `PlatformWindow::prompt` 的 trait 契约：参数已是 `&[PromptButton]`，返回 `Option<oneshot::Receiver<usize>>`。

对照两个平台实现可以看清「两条路径」如何落地。Linux X11 直接放弃原生对话框：

[../gpui_linux/src/linux/x11/window.rs:1497-1505](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/x11/window.rs#L1497-L1505) — X11 窗口的 `prompt` 恒返回 `None`，于是 Linux 上 `window.prompt` 永远落到 GPUI 自绘兜底。（Wayland 实现同理。）

macOS 则构造真正的 `NSAlert`，且注释解释了一个非常典型的跨平台细节——为什么要把初始键盘焦点移到「最后一个非 cancel、非默认」的按钮上：

[../gpui_macos/src/window.rs:1507-1553](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_macos/src/window.rs#L1507-L1553) — `NSAlert` 的第一个按钮占据回车键、cancel 按钮占据 Escape 键且默认拿到焦点，导致「保存 / 不保存 / 取消」三按钮里中间那个键盘不可达；这段代码把初始焦点移到末尾的非 cancel 按钮，并给每个按钮 `setTag: ix`，用 tag 把「点了哪个」映射回下标。

`PromptLevel` 与 `PromptButton` 定义在 `src/platform.rs`：

[src/platform.rs:2150-2161](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform.rs#L2150-L2161) — `PromptLevel` 三档：`Info` / `Warning` / `Critical`，只是「语气分级」，GPUI 自绘渲染器目前甚至没有用它区分样式（字段名是 `_level`），但 macOS 会映射到不同的 `NSAlert` 样式。

[src/platform.rs:2163-2214](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform.rs#L2163-L2214) — `PromptButton` 是三变体枚举 `Ok(SharedString)` / `Cancel(SharedString)` / `Other(SharedString)`；`From<&str>` 实现会把小写后的 `"ok"` / `"cancel"` 字符串识别成对应变体，其余一律 `Other`——这就是 `&["OK", "Cancel"]` 能直接工作的原因。

最简调用姿势直接来自示例：

[src/examples/window.rs:270-287](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/window.rs#L270-L287) — 用 `&["OK", "Cancel"]` 调 `window.prompt`，把返回的 receiver 丢进 `cx.spawn` 里 `await`，按下标分支处理。注意 prompt 调用发生在鼠标点击回调（同步上下文）里，而答案在异步任务里到达——这是使用 prompt 的标准节奏。

[src/examples/window.rs:288-305](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/window.rs#L288-L305) — 非英文按钮写法：显式用 `PromptButton::ok("确定")` / `PromptButton::cancel("取消")` 构造，绕过 `From<&str>` 的英文识别逻辑。

#### 4.1.4 代码实践

**实践目标**：亲手触发一次 prompt，观察「同步发起、异步收答案」的节奏，并验证 Linux 上走的是自绘路径。

**操作步骤**：

1. 在仓库 `crates/gpui` 目录运行 `cargo run -p gpui --example window`。
2. 点击窗口中的 `Prompt` 按钮，再点 `Prompt (non-English)` 按钮。
3. 分别点击对话框的确定/取消，观察终端输出。
4. 把按钮数组改成三个：`&["Save", "Don't Save", "Cancel"]`（示例代码），再运行一次。

```rust
// 示例代码：修改 examples/window.rs 中的 Prompt 按钮（非项目原有代码）
.child(button("Prompt x3", |window, cx| {
    let answer = window.prompt(
        PromptLevel::Warning,
        "未保存的更改将丢失",
        Some("确定要退出吗？"),
        &["Save", "Don't Save", "Cancel"],
        cx,
    );
    cx.spawn(async move |_| {
        match answer.await {
            Ok(0) => println!("保存"),
            Ok(1) => println!("不保存"),
            _ => println!("取消"),
        }
    })
    .detach();
}))
```

**需要观察的现象**：对话框弹出后主界面无法点击（模态）；点击按钮前终端无输出，点击后立即打印；Linux 上对话框是 GPUI 自绘的白色小卡片加灰色遮罩（原因见 4.2），而不是系统样式。

**预期结果**：三按钮版本点 `Don't Save` 时 `answer.await` 得到 `1`。注意 `"Save"` 小写后是 `"save"`，会落入 `From<&str>` 的 `_ => Other` 分支——只有 `"ok"` / `"cancel"` 两个词有特殊身份。

**待本地验证**：第 4 步的输出（本讲义编写环境未运行 GUI，无法替你确认终端打印）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `prompt` 的 `answers` 参数设计成 `&[T] where T: Clone + Into<PromptButton>`，而不是直接收 `&[PromptButton]`？

**答案**：为了调用方的人体工学。`Into<PromptButton>` 让 `&["OK", "Cancel"]` 这种字符串字面量数组直接可用（`From<&str>` 会顺手把 ok/cancel 识别成语义变体），同时保留了 `PromptButton::ok(...)` 这种显式构造非英文按钮的能力。`Clone` 则是因为方法内部要把每项转换成拥有的 `Vec<PromptButton>`。

**练习 2**：在 Linux 上调用 `window.prompt`，最终用户看到的是谁画的对话框？

**答案**：GPUI 自绘的 `FallbackPromptRenderer`（见 4.2）。因为 X11/Wayland 的 `PlatformWindow::prompt` 返回 `None`，`Window::prompt` 的 `Default` 分支会 `unwrap_or_else` 落到 `build_custom_prompt`。

**练习 3**：如果用户直接关掉 macOS 的原生 `NSAlert`（比如按了 Escape），调用方 `answer.await` 会得到什么？

**答案**：Escape 会触发 cancel 按钮的 key equivalent（实现里给 cancel 按钮设置了 `setKeyEquivalent:`），所以正常路径下得到 cancel 按钮的下标；只有当 sender 一侧被丢弃（如窗口整个销毁）时 `await` 才返回 `Err(Canceled)`。稳妥的写法是用 `match`/`unwrap_or` 兜住 `Err`。

### 4.2 PromptBuilder 与自绘 prompt：FallbackPromptRenderer 全链路

#### 4.2.1 概念说明

`PromptBuilder` 是挂在 `App` 上的**策略对象**：`Default` 变体的行为是「先问平台、不行就用内置兜底渲染器」，`Custom(闭包)` 变体则完全由应用接管。应用通过 `cx.set_prompt_builder(...)` 注入自定义渲染——Zed 编辑器就是这么让所有对话框长成自家风格的。

内置兜底是 `FallbackPromptRenderer`：一个再普通不过的 GPUI 实体视图。它证明了一件事——**GPUI 的 prompt 没有任何黑魔法，对话框本身就是一棵元素树**，你已经学过的 Render、track_focus、on_click、cx.emit 在这里全部适用。

#### 4.2.2 核心流程

自绘 prompt 的完整生命周期（这是本讲最重要的一条链）：

```text
build_custom_prompt (window.rs)
  1. oneshot::channel() → (sender, receiver)
  2. PromptHandle { sender }
  3. 调 builder 闭包:
       fallback_prompt_renderer (prompts.rs)
       a. cx.new(FallbackPromptRenderer { ... })      # 建对话框实体
       b. handle.with_view(view, window, cx):
          - 订阅 view 的 PromptResponse 事件（cx.subscribe + detach）
          - 记录 previous_focus = window.focused(cx)
          - window.focus(prompt 的 focus_handle)      # 抢焦点
  4. window.prompt = Some(RenderablePromptHandle)

用户点击第 ix 个按钮（paint 期注册的 on_click）
  → cx.emit(PromptResponse(ix))
  → 订阅回调:
       sender.send(ix)                    # 答案回传，oneshot 完成
       window_handle.update: window.prompt.take()   # 摘掉对话框
       window.focus(previous_focus)       # 焦点还给弹窗前的元素
```

绘制层叠方面，prompt 在 `Window::draw` 中是一个**独立的根元素**，盖在普通树和 deferred 浮层之上（呼应 u6-l3 的层叠顺序：普通树 → deferred 浮层 → 模态 prompt → 拖拽预览 → tooltip）。代码里 prompt、active_drag、tooltip 三者是 `if/else if` 互斥的——同一时刻窗口只画其中一个顶层覆盖物。

#### 4.2.3 源码精读

事件与句柄类型：

[src/window/prompts.rs:13-21](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window/prompts.rs#L13-L21) — `PromptResponse(pub usize)` 是「选了第几个选项」的事件；`Prompt` trait 是 `EventEmitter<PromptResponse> + Focusable` 的组合别名，任何同时满足这两点的视图都能当 prompt 用。

[src/window/prompts.rs:34-63](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window/prompts.rs#L34-L63) — `PromptHandle::with_view` 是事件流的中枢：`cx.subscribe` 订阅 prompt 视图的 `PromptResponse`；回调里用 `sender.take()` 保证 oneshot 只发一次，然后跨窗口句柄更新——`window.prompt.take()` 摘掉对话框、把焦点还给 `previous_focus`。订阅 `.detach()` 而不是存 `Subscription`，因为这个订阅的生命周期就是「到用户点按钮为止」。

兜底渲染器的构造与渲染：

[src/window/prompts.rs:73-91](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window/prompts.rs#L73-L91) — `fallback_prompt_renderer` 函数：`cx.new` 创建 `FallbackPromptRenderer` 实体，再交给 `handle.with_view` 接上事件流。它的签名正是自定义 builder 必须遵守的形状（对比 4.2.4 实践）。

[src/window/prompts.rs:94-100](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window/prompts.rs#L94-L100) — `FallbackPromptRenderer` 的字段：`_level`（目前未用于渲染）、message、detail、actions、自己的 `FocusHandle`。一个普通实体，没有任何特殊身份。

[src/window/prompts.rs:130-147](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window/prompts.rs#L130-L147) — 按钮的生成方式：对 `actions` 枚举出 `(ix, action)`，每个按钮是一个带 `.id(ix)` 的 div（u5-l2 讲过：有状态交互必须先 `.id()`），`on_click` 里 `cx.emit(PromptResponse(ix))` 并 `stop_propagation`。下标 ix 就是回传给调用方的答案。

[src/window/prompts.rs:149-177](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window/prompts.rs#L149-L177) — 「模态」的实现：底层铺一块 `bg(opaque_grey(0.5, 0.6))` 的全屏半透明遮罩吃掉视觉，对话框本体 `track_focus` 抢走键盘焦点——两招合起来挡住与主界面的交互。

策略对象与窗口侧的挂载点：

[src/window/prompts.rs:198-232](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window/prompts.rs#L198-L232) — `PromptBuilder` 枚举与 `Deref` 实现：`Default` 解引用到 `fallback_prompt_renderer` 函数，`Custom` 解引用到应用闭包。`Window::prompt` 里 `(prompt_builder)(...)` 这句函数调用正是穿过这个 Deref 完成的。

[src/app.rs:744](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L744) 与 [src/app.rs:855](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L855) — `App` 上的 `prompt_builder` 字段，`App::new` 时初始化为 `Some(PromptBuilder::Default)`。

[src/app.rs:2598-2619](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L2598-L2619) — `App::set_prompt_builder` 注入 `Custom` 闭包，`reset_prompt_builder` 恢复默认。闭包签名与 `fallback_prompt_renderer` 完全一致。

`window.prompt` 字段与绘制层叠：

[src/window.rs:1199](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L1199) — `Window` 结构上的 `prompt: Option<RenderablePromptHandle>` 字段，就是「当前是否有对话框」的全部状态。

[src/window.rs:3129-3150](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L3129-L3150) — `Window::draw` 的 prepaint 段：若有 prompt，把它的视图转成 `AnyElement`，作为**第二个根**走 request_layout + `stretch_auto_size_to_fill` + `prepaint_as_root`——与主根完全平行的三阶段流程；`else if` 分支说明 active_drag 与 tooltip 与它互斥。

[src/window.rs:3161-3169](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L3161-L3169) — paint 段的顺序：`root_element.paint` → inspector → `paint_deferred_draws`（u6-l3 的浮层）→ **prompt** → drag → tooltip。这就是模态对话框永远盖住包括 popover 在内一切浮层的原因。

[src/window.rs:5805-5811](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L5805-L5811) — `Window::has_active_prompt`：只对 GPUI 自绘的 prompt 返回 `true`（平台原生对话框不经过这个字段），文档注释明确区分了这两种情况。

#### 4.2.4 代码实践

**实践目标**：用 `set_prompt_builder` 亲手接管一次 prompt 渲染，验证「对话框就是普通视图」。

**操作步骤**：

1. 复制 `examples/window.rs` 为 `examples/my_prompt.rs`（记得在 `Cargo.toml` 的 `[[example]]` 区块补一条声明，或直接改原文件后用 `git checkout` 还原）。
2. 在 `application().run(...)` 回调开头、`open_window` 之前加上自定义 builder（示例代码）：

```rust
// 示例代码：接管 prompt 渲染（非项目原有代码）
use gpui::{App, PromptButton, PromptLevel, Render, RenderablePromptHandle,
           Window, div, prelude::*};

struct MyPromptRenderer {
    message: String,
    focus: gpui::FocusHandle,
}

impl Render for MyPromptRenderer {
    fn render(&mut self, _: &mut Window, cx: &mut gpui::Context<Self>) -> impl IntoElement {
        div()
            .track_focus(&self.focus)
            .size_full()
            .flex()
            .flex_col()
            .justify_around()
            .bg(gpui::red())
            .child(div().child(self.message.clone()))
            .child(
                div()
                    .id("ok")
                    .child("确定")
                    .on_click(cx.listener(|_, _, _, cx| {
                        cx.emit(gpui::PromptResponse(0));
                        cx.stop_propagation();
                    })),
            )
    }
}

impl gpui::EventEmitter<gpui::PromptResponse> for MyPromptRenderer {}
impl gpui::Focusable for MyPromptRenderer {
    fn focus_handle(&self, _: &gpui::App) -> gpui::FocusHandle { self.focus.clone() }
}

fn my_prompt_renderer(
    _level: PromptLevel,
    message: &str,
    _detail: Option<&str>,
    _actions: &[PromptButton],
    handle: gpui::PromptHandle,
    window: &mut Window,
    cx: &mut App,
) -> RenderablePromptHandle {
    let view = cx.new(|cx| MyPromptRenderer {
        message: message.to_string(),
        focus: cx.focus_handle(),
    });
    handle.with_view(view, window, cx)
}

// run 回调里：
// cx.set_prompt_builder(my_prompt_renderer);
```

3. 运行 `cargo run -p gpui --example my_prompt`，点 Prompt 按钮。

**需要观察的现象**：对话框变成全屏红色背景 + 「确定」；点击后对话框消失、焦点回到之前的位置；终端照常打印 `You have clicked Ok`——**回传协议（`PromptResponse(0)` → oneshot）完全没有变化**，换的只是渲染。

**预期结果**：自定义 builder 生效，说明 `PromptBuilder::Custom` 分支确实绕过了平台与兜底渲染器。

**待本地验证**：示例代码需要自行补全 import 与 `Cargo.toml` 声明后编译运行，本讲义未替你执行。

#### 4.2.5 小练习与答案

**练习 1**：`PromptHandle::with_view` 里为什么用 `sender.take()` 而不是直接 `sender.send(...)`？

**答案**：`sender` 被闭包以 `Option` 形式捕获（`let mut sender = Some(self.sender)`）。`take()` 之后闭包内变成 `None`，重复触发 `PromptResponse` 时第二次 `if let Some(sender)` 直接跳过——配合 oneshot「只能发一次」的天性，双保险地保证对话框只结算一次。

**练习 2**：自绘 prompt 如何做到「盖住 popover 等浮层」？

**答案**：不是靠 z-order 数值，而是靠 `Window::draw` 的 paint 顺序：prompt 作为一个独立根元素在 `paint_deferred_draws` 之后才 paint，后画的图元自然盖住先画的（u4-l3 的批次顺序），所以模态 prompt 恒在 deferred 浮层之上、又低于拖拽预览与 tooltip（互斥的 else-if 分支）。

**练习 3**：`window.has_active_prompt()` 在什么条件下返回 `true`？

**答案**：仅当 `Window.prompt` 字段有值，即当前对话框是 GPUI 在窗口内自绘的（`Custom` builder 或平台返回 `None` 触发的兜底）。平台原生对话框（如 macOS `NSAlert`）不经过这个字段，期间 `has_active_prompt()` 返回 `false`。

### 4.3 应用菜单：Menu/MenuItem 与动作绑定

#### 4.3.1 概念说明

GPUI 的菜单是**纯数据描述 + 平台渲染**：`Menu` / `MenuItem` 组成一棵不可变的菜单树，`cx.set_menus(...)` 一次性整树替换。它不是增量更新的——状态变了（比如某个选项要打勾）就重新调一次 `set_menus`，把最新的树推给平台。

菜单最重要的身份是 **Action 的第三个触发入口**：

```text
键盘按键 ──┐
代码 dispatch ──┼──→ 同一套 Action 派发链路（u5-l3/u5-l4）→ 处理函数
菜单项点击 ──┘
```

`MenuItem::Action` 携带一个 `Box<dyn Action>`，用户点击后平台回调把它交给 `cx.dispatch_action`，之后与键盘触发完全同路。这也解释了为什么菜单与 keymap 绑定：`set_menus` 会把应用的 `Keymap` 一并传给平台，macOS 用它查出每个动作的 `KeyBinding` 显示成菜单项右侧的快捷键（⌘Q 之类）。

`OsAction`（Cut/Copy/Paste/SelectAll/Undo/Redo）是给操作系统看的「动作类别标注」：告诉平台这个菜单项对应标准编辑操作，平台可以附加专门行为（如 macOS 的服务集成）。

#### 4.3.2 核心流程

菜单从注册到点击的全链路：

```text
应用启动
  App::new → init_app_menus(platform, cx)      # 注册三个平台回调（见下）
  cx.set_menus([Menu::new("File").items([...])])
    → App::set_menus → platform.set_menus(menus, &keymap)
       macOS: create_menu_bar + app.setMainMenu_（keymap 用于显示快捷键）
       Linux: 存入 common.menus，get_menus 可读回（无系统菜单栏）

用户点击菜单项
  平台 → on_app_menu_action 回调 → cx.dispatch_action(action)
    → 走 u5-l4 的四阶段派发 → App::on_action 注册的全局监听器 / 路径监听器

菜单即将展开
  平台 → on_will_open_app_menu → cx.clear_pending_keystrokes()
菜单项是否置灰
  平台 → on_validate_app_menu_command → cx.is_action_available(action)
```

`init_app_menus` 在 `App::new` 时自动注册（[src/app.rs:877](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L877)），应用不需要手动调。

#### 4.3.3 源码精读

数据模型：

[src/platform/app_menu.rs:4-35](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform/app_menu.rs#L4-L35) — `Menu { name, items, disabled }` 与 builder 方法 `new` / `items` / `disabled`。纯数据，无行为。

[src/platform/app_menu.rs:75-104](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform/app_menu.rs#L75-L104) — `MenuItem` 四种形态：`Separator`（分隔线）、`Submenu(Menu)`（子菜单）、`SystemMenu(OsMenu)`（由操作系统填充内容的菜单，目前只有 macOS 的 Services）、`Action { name, action: Box<dyn Action>, os_action, checked, disabled }`。菜单项的核心载荷就是一个 Action 值。

[src/platform/app_menu.rs:126-149](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform/app_menu.rs#L126-L149) — 两个构造函数：`MenuItem::action(name, action)` 造普通动作项；`MenuItem::os_action(name, action, os_action)` 额外携带 OS 标注。

[src/platform/app_menu.rs:176-184](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform/app_menu.rs#L176-L184) — `checked` builder：只对 `Action` 变体生效，其余形态静默忽略（`disabled` 同理，额外支持 `Submenu`）。这就是 set_menus 示例里「List Mode 打勾」的实现位置。

[src/platform/app_menu.rs:307-329](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform/app_menu.rs#L307-L329) — `OsAction` 六个变体。文件顶部的 TODO 注释还透露了演化方向：这些映射将来应重构为 GPUI 内建 action。

三个平台回调（菜单事件汇入 App 的地方）：

[src/platform/app_menu.rs:331-359](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform/app_menu.rs#L331-L359) — `init_app_menus`：① `on_will_open_app_menu` → 菜单展开前清空 pending keystrokes（防止 u5-l4 讲的多键序列被菜单打断后误触发）；② `on_validate_app_menu_command` → 用 `is_action_available` 决定菜单项可否点击；③ `on_app_menu_action` → `cx.dispatch_action(action)`，菜单点击正式汇入与键盘完全相同的派发链路。注意三者都通过 `cx.to_async()` 拿弱引用句柄、在平台线程回调时 `borrow_mut` 回到 App——单前台线程模型的又一次体现。

应用侧入口：

[src/app.rs:2399-2413](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L2399-L2413) — `App::set_menus`：收集迭代器后连同 `keymap` 一起交给平台；`get_menus` 读回当前菜单；`set_dock_menu` 设置 dock 右键菜单（macOS）。

[src/platform.rs:231](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform.rs#L231) — `Platform::set_menus(menus, keymap)` 是必需实现的方法——每个平台都必须决定「菜单放哪」。

两个平台实现对照：

[../gpui_macos/src/platform.rs:1041-1051](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_macos/src/platform.rs#L1041-L1051) — macOS：`create_menu_bar(&menus, ..., keymap)` 构建原生 `NSMenu` 并 `setMainMenu_`；keymap 在建菜单时被用来给每个动作查快捷键、显示为 key equivalent。

[../gpui_linux/src/linux/platform.rs:621-629](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_linux/src/linux/platform.rs#L621-L629) — Linux：只把菜单 `owned()` 后存进 `common.menus`，`get_menus` 读回；Linux 没有系统级菜单栏，菜单数据留给应用层自绘使用（Zed 编辑器就在应用内自己画菜单）。

完整示例：

[src/examples/set_menus.rs:93-113](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/set_menus.rs#L93-L113) — `set_app_menus` 的全貌：一个顶层菜单里混排了 OS 子菜单（Services）、分隔线、禁用项、checked 勾选项、嵌套子菜单和 Quit 动作项。注意它先 `cx.global::<AppState>()` 读状态再决定 checked——菜单是状态的函数，和视图一个道理。

[src/examples/set_menus.rs:116-128](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/set_menus.rs#L116-L128) — `actions!(set_menus, [Quit, ToggleCheck])` 定义动作；`toggle_check` 处理函数改完 `AppState` 后**重调 `set_app_menus`**，把新的勾选状态推给菜单——「数据驱动、整树重设」的活教材；`quit` 则直接 `cx.quit()`。

[src/examples/set_menus.rs:25-40](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/set_menus.rs#L25-L40) — 注册顺序：`cx.activate(true)` 把应用带到前台（macOS 上才看得见菜单栏）→ `cx.on_action(quit)` / `cx.on_action(toggle_check)` 注册全局动作监听 → `set_app_menus`。菜单项的 action 能被点到，靠的正是这些监听器。

#### 4.3.4 代码实践

**实践目标**：运行菜单示例，验证「菜单点击 → 全局 on_action」的链路，并亲手让勾选状态翻转。

**操作步骤**：

1. 运行 `cargo run -p gpui --example set_menus`（在 Linux 上窗口内容正常显示，但不会有系统菜单栏——菜单数据只是被存了起来；macOS 上能看到完整菜单栏）。
2. 阅读 `set_app_menus` 与 `toggle_check`：找出 `List Mode` 的勾选从哪来。
3. 在 `toggle_check` 里加一行日志（示例代码）：`println!("view_mode -> {:?}", app_state.view_mode == ViewMode::List);`
4. 给 Quit 动作绑一个快捷键（示例代码），在 `run_example` 里 `set_app_menus` 之前加：

```rust
// 示例代码：为菜单动作绑定快捷键（非项目原有代码）
use gpui::KeyBinding;
cx.bind_keys([KeyBinding::new("ctrl-alt-q", Quit, None)]);
```

**需要观察的现象**：macOS 上点 `Mode → List/Grid` 菜单项，勾选标记随之移动；点 `Quit` 应用退出并打印 `Gracefully quitting the application...`。绑键后按 `ctrl-alt-q` 也能触发同一条 `quit` 路径——菜单与键盘殊途同归。

**预期结果**：Linux 上所有交互入口都不可见（无菜单栏）但代码路径完好；macOS 上勾选/退出/快捷键全部可验证。

**待本地验证**：本环境为 Linux 且无显示服务器，上述现象需在你的桌面环境确认；尤其「菜单项快捷键显示」仅 macOS 可见。

#### 4.3.5 小练习与答案

**练习 1**：用户点击一个菜单项后，处理函数是怎么被找到的？

**答案**：平台触发 `on_app_menu_action` 回调（`init_app_menus` 注册）→ `cx.dispatch_action(action)` → 走 u5-l4 的四阶段派发（全局捕获/路径捕获/路径冒泡/全局冒泡）→ 命中 `cx.on_action(quit)` 这类全局监听器或元素树上的 `on_action`。与键盘触发动作完全同路。

**练习 2**：为什么 `set_menus` 每次都要传 `&Keymap`？

**答案**：菜单项需要显示快捷键提示。macOS 平台实现用 keymap 为每个 `MenuItem::Action` 查 `KeyBinding`，显示为菜单项右侧的 key equivalent（如 ⌘Q）。这也是菜单与 keymap 两个系统唯一的耦合点。

**练习 3**：`MenuItem::action("x", A).checked(true)` 若作用在 `Separator` 上会发生什么？

**答案**：什么也不发生。`checked` builder 内部 `match &mut self`，只有 `Action` 变体的字段会被改写，其余分支空处理后原样返回 `self`（`disabled` 对 `Submenu` 也生效）。

### 4.4 系统通知：SystemNotification 的发送与响应

#### 4.4.1 概念说明

前两块（对话框、菜单）发生在应用窗口内或由应用窗口触发，系统通知则真正「出应用」：发到操作系统的通知中心（macOS 通知中心、Windows 操作中心、Linux 的通知服务）。应用可能已经失焦甚至被遮挡，通知是拉回用户注意力的手段。

四个 API 构成完整闭环：

| API | 作用 |
| --- | --- |
| `cx.set_app_identity(id, name)` | 设置进程身份（Windows AppUserModelID、macOS 展示名），须在开窗/发通知前调用 |
| `cx.show_system_notification(n)` | 发送通知；**同 tag 的新通知会替换旧的**（平台支持时） |
| `cx.dismiss_system_notification(tag)` | 按 tag 尽力撤回已送达或待送达的通知 |
| `cx.on_system_notification_response(f)` | 注册用户点击通知（本体或按钮）后的回调；**后注册覆盖先注册** |

`tag` 是核心身份机制：它既是「替换同一条通知」的键，也是响应回传时标识「用户点的哪条通知」的凭证。

#### 4.4.2 核心流程

```text
发送：cx.show_system_notification(SystemNotification { tag, title, body, actions })
  → App → platform.show_system_notification
     （平台默认实现为 no-op，macOS/Windows 有真实实现）

用户点击通知本体或某个 action 按钮
  → 平台在主线程回调 → App::on_system_notification_response 包装的闭包
  → 应用回调收到 SystemNotificationResponse { tag, action_id }
       action_id = Some("open")   → 点了 id 为 "open" 的按钮
       action_id = None           → 点了通知本体
  → 应用按 tag 路由，更新实体状态 → cx.notify() 刷新 UI
```

测试侧（为 u7-l4 铺垫）：`TestPlatform` 把通知记录在 `shown` / `delivered` 两个列表里，`TestAppContext` 暴露 `shown_system_notifications()` / `delivered_system_notifications()` 读取，并用 `simulate_system_notification_response(...)` 模拟用户点击——**不发真实通知也能测通知逻辑**。

#### 4.4.3 源码精读

数据结构（都在 `src/platform.rs`）：

[src/platform.rs:374-389](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform.rs#L374-L389) — `SystemNotification { tag, title, body, actions }`。tag 的文档注释明确了双重身份：替换键 + 响应回传凭证；`actions` 为空就是不带按钮的普通通知，不支持按钮的平台直接忽略。

[src/platform.rs:391-409](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform.rs#L391-L409) — `SystemNotificationAction { id, label }`（按钮）与 `SystemNotificationResponse { tag, action_id: Option<SharedString> }`（响应）。`action_id` 为 `None` 即用户点的是通知本体——这个 `Option` 是响应路由的第一个分叉。

trait 契约与默认 no-op：

[src/platform.rs:259-291](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform.rs#L259-L291) — `set_app_identity` / `show_system_notification` / `dismiss_system_notification` / `on_system_notification_response` 四个方法的默认实现全是 `_ = ...;` 丢弃式 no-op，保证不支持通知的平台照样编译运行；文档同时写清了各方法的语义边界（如撤回是 best-effort、回调必须在主线程）。这是 u7-l1 讲过的「能力缺失型默认实现」的典型样本。

应用侧包装：

[src/app.rs:1502-1522](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L1502-L1522) — `App::set_app_identity`、`show_system_notification`、`dismiss_system_notification`：一行转发到 platform，无附加逻辑。

[src/app.rs:1527-1538](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L1527-L1538) — `App::on_system_notification_response`：把应用层回调（拿 `&mut App`）包成平台层回调（裸 response），靠 `self.this` 弱引用在 App 还活着时 `borrow_mut` 进入。文档注明**后注册覆盖先注册**——全局只有一个分发点，应用应在此统一路由（示例正是这么做的）。

完整示例（本讲最佳参照）：

[src/examples/system_notifications.rs:38-60](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/system_notifications.rs#L38-L60) — 点击「Show or replace」：`revision` 自增后 `cx.show_system_notification(...)`，带 `open` / `snooze` 两个 action 按钮；tag 固定为 `NOTIFICATION_TAG`，所以**连点多次是替换而不是堆叠**；发送后立即更新状态栏并 `cx.notify()`。

[src/examples/system_notifications.rs:62-67](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/system_notifications.rs#L62-L67) — 「Dismiss」按钮：`cx.dismiss_system_notification(NOTIFICATION_TAG)` 按 tag 撤回。

[src/examples/system_notifications.rs:99-120](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/system_notifications.rs#L99-L120) — 启动侧三件事：`cx.set_app_identity("dev.zed.gpui.system-notifications", ...)` 先立身份；`cx.on_system_notification_response` 里按 `action_id` 的 `Some/None` 分叉更新状态栏——统一分发点 + tag 路由的标准写法；随后才开窗。

[src/examples/system_notifications.rs:77-81](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/system_notifications.rs#L77-L81) — macOS 专属提示：只有从 app bundle 运行时系统才投递通知——这是排查「通知不显示」的第一嫌疑。

#### 4.4.4 代码实践

**实践目标**：跑通通知的「发送 → 替换 → 撤回 → 响应回传」闭环。

**操作步骤**：

1. 运行 `cargo run -p gpui --example system_notifications`。
2. 连点「Show or replace」三四次，观察系统通知中心。
3. 点「Dismiss」，观察通知消失。
4. 再发一条，然后去系统通知中心点通知上的 `Open` 按钮 / `Snooze` 按钮 / 通知本体，回到应用看状态栏文字。

**需要观察的现象**：第 2 步通知不堆叠，只有一条且标题里的编号在变（tag 替换语义）；第 4 步状态栏分别显示 `Received action 'open' for tag 'gpui-system-notification-example'`、`Received action 'snooze' ...`、`Notification body clicked ...`。

**预期结果**：三个来源（两个按钮 + 本体）在 `action_id` 的 `Some(id)` / `None` 上分叉，与应用代码里的 `match action_id` 一一对应。

**待本地验证**：通知投递依赖桌面环境与权限设置；无头环境或 Linux 未配置通知服务时会静默 no-op。macOS 上需从 app bundle 运行示例才有通知。

#### 4.4.5 小练习与答案

**练习 1**：如何区分用户点的是通知上的按钮还是通知本体？

**答案**：看 `SystemNotificationResponse.action_id`：`Some(id)` 是按下了 `id` 对应的 `SystemNotificationAction` 按钮；`None` 是点击了通知本体。`id` 来自发送时 `actions` 数组里各按钮的 `id` 字段。

**练习 2**：连续发三条 tag 相同、tag 不同、再 tag 相同的通知，最终通知中心里有几条？

**答案**：两条。同 tag 的第三条会替换第一条（同 tag 覆盖），tag 不同的第二条独立存在。tag 是稳定身份，不是唯一性约束——想堆叠就用不同 tag，想更新就用同一 tag（聊天应用的「新消息」通知常这么用）。

**练习 3**：通知发出后应用毫无反应，`on_system_notification_response` 的回调也没触发，可能有哪些原因？

**答案**：按可能性排查：① 平台不支持或通知被系统拒绝（trait 默认实现就是 no-op）；② 用户拒绝了通知授权（文档明确提到 authorization denied 时静默失败）；③ macOS 上应用没跑在 app bundle 里（示例注释专门提醒）；④ Windows 上没先 `set_app_identity` 设置 AppUserModelID；⑤ 回调被后来的注册覆盖——它是替换式单槽位，不是多播订阅。

## 5. 综合实践

把三块串成一个经典需求：**退出前确认**。目标行为：

1. 用户点窗口关闭按钮（或快捷键触发关闭）→ 不直接关，弹三按钮 prompt：「保存 / 放弃 / 取消」。
2. 选「保存」→（模拟）保存后发一条系统通知「已保存」，然后真正关闭窗口。
3. 选「放弃」→ 直接关闭窗口。
4. 选「取消」→ 什么都不发生。
5. 同时为应用配置一个带快捷键的 `File` 菜单（`Quit` 项），点菜单项与按快捷键走同一条动作路径。

关键难点是**关闭否决是同步的，而 prompt 的答案是异步的**。`Window::on_window_should_close` 的回调必须同步返回 `bool`：

[src/window.rs:5952-5963](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L5952-L5963) — 回调返回 `false` 则窗口不关。因此标准模式是：首次询问时发起 prompt 并返回 `false`（先否决），用 `Rc<Cell<bool>>` 标志防止重复弹窗；异步拿到答案后，需要真正关闭时调用 `window.remove_window()`（应用主动关闭，不会再走平台的 should-close 询问）。

参考实现骨架（示例代码，非项目原有代码；建议复制 `examples/window.rs` 为 `examples/quit_confirm.rs` 修改，并补 `Cargo.toml` 声明）：

```rust
// 示例代码：退出前确认 + File 菜单 + 保存通知
use gpui::{
    App, KeyBinding, Menu, MenuItem, PromptButton, PromptLevel, SystemNotification,
    WindowOptions, actions, div, prelude::*,
};
use gpui_platform::application;
use std::{cell::Cell, rc::Rc};

actions!(quit_confirm, [Quit]);

struct Editor { dirty: bool }

impl Render for Editor {
    fn render(&mut self, _: &mut gpui::Window, _: &mut gpui::Context<Self>) -> impl IntoElement {
        div().size_full().flex().items_center().justify_center()
            .child(if self.dirty { "有未保存的更改" } else { "已保存" })
    }
}

fn run_example() {
    application().run(|cx: &mut App| {
        // 1) 动作 + 快捷键 + 菜单：三个入口，一条链路
        cx.bind_keys([KeyBinding::new("ctrl-cmd-q", Quit, None)]);
        cx.on_action(|_: &Quit, cx| cx.quit()); // 菜单/快捷键最终都到这里（本例直接退出，
                                                // 与关闭按钮的确认流程互不影响）
        cx.set_menus([Menu::new("File").items([
            MenuItem::action("Quit", Quit),
        ])]);

        // 2) 关闭确认状态
        let confirming = Rc::new(Cell::new(false));

        let handle = cx.open_window(WindowOptions::default(), |_, cx| {
            cx.new(|_| Editor { dirty: true })
        }).unwrap();

        handle.update(cx, |_, window, cx| {
            let confirming = confirming.clone();
            window.on_window_should_close(cx, move |window, cx| {
                if confirming.get() {
                    // 上一轮确认尚未结束（或已确认放行过），直接否决
                    return false;
                }
                confirming.set(true);
                let answer = window.prompt(
                    PromptLevel::Warning,
                    "保存更改后再退出吗？",
                    None,
                    &[
                        PromptButton::ok("保存"),        // 下标 0
                        PromptButton::new("放弃"),        // 下标 1
                        PromptButton::cancel("取消"),     // 下标 2
                    ],
                    cx,
                );
                let confirming = confirming.clone();
                let window_handle = window.window_handle();
                cx.spawn(async move |cx: &mut gpui::AsyncApp| {
                    let close = match answer.await {
                        Ok(0) => {
                            // 选「保存」：先发系统通知，再关闭
                            cx.update(|cx: &mut App| {
                                cx.show_system_notification(SystemNotification {
                                    tag: "quit-confirm".into(),
                                    title: "已保存".into(),
                                    body: "更改已写入磁盘，窗口即将关闭。".into(),
                                    actions: vec![],
                                });
                            }).ok();
                            true
                        }
                        Ok(1) => true,           // 放弃
                        _ => { confirming.set(false); false } // 取消/信道取消：复位标志
                    };
                    if close {
                        cx.update_window(window_handle, |_, window, _| window.remove_window())
                            .ok();
                    }
                }).detach();
                false // 同步否决，答案异步到达后再决定去留
            });
        });
        cx.activate(true);
    });
}
```

**操作步骤**：

1. 建 `examples/quit_confirm.rs`，粘入上述骨架（补 `main`/`start` 双入口，参照 `examples/window.rs` 结尾的写法），在 `Cargo.toml` 补 `[[example]]`。
2. `cargo run -p gpui --example quit_confirm`，点窗口关闭按钮，分别试三个按钮。
3. 验证菜单：macOS 上 `File → Quit`；绑定的 `ctrl-cmd-q` 快捷键在 Linux 上也可直接触发 `Quit` 动作验证 `bind_keys` 生效。
4. 观察选「保存」时系统通知是否出现（Linux 需桌面通知服务）。

**需要观察的现象**：点关闭按钮 → 灰色遮罩 + 三按钮对话框；「取消」后再次点关闭能重新弹窗（标志复位）；「保存」先出通知、随后窗口关闭；「放弃」直接关闭。整个过程中主界面不可交互（模态）。

**预期结果**：同步否决 + 异步决定的模式成立；`remove_window()` 不会再触发 should-close 回调（macOS 上该询问只在用户发起关闭时由平台调用，见 `windowShouldClose:` 选择子的注册处 [../gpui_macos/src/window.rs:415](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_macos/src/window.rs#L415)）。

**待本地验证**：骨架代码未在本环境编译运行，请自行补全 `main`/`start` 入口后编译验证。另需注意：`cx.on_action` 里的直接退出与关闭按钮的确认流程是两条独立路径，若希望「Cmd-Q 也走确认」，应把 `Quit` 处理改成对窗口发起关闭而不是 `cx.quit()`，留给读者作为进阶改造。Linux 上 prompt 为自绘样式、通知依赖桌面环境。

## 6. 本讲小结

- `window.prompt(level, message, detail, answers, cx)` 是模态对话框的统一入口，返回 `oneshot::Receiver<usize>`：同步发起、异步收答案，答案就是按钮下标。
- 实现分两路：`PromptBuilder::Default` 先问 `PlatformWindow::prompt`（macOS 用 `NSAlert`，cancel 绑 Escape、焦点精心分配），平台返回 `None`（如 Linux）或 `PromptBuilder::Custom` 时走 GPUI 自绘。
- 自绘 prompt 没有黑魔法：`FallbackPromptRenderer` 是普通实体视图，点击 `emit PromptResponse(ix)` → 订阅回调 `sender.send(ix)`、`window.prompt.take()`、恢复焦点；绘制上它是 `Window::draw` 里的第二个根元素，paint 在 deferred 浮层之后，天然盖住一切应用内浮层。
- 菜单是纯数据树（`Menu`/`MenuItem`/`OsAction`），整树替换式更新；菜单项的核心载荷是 `Box<dyn Action>`，点击经 `on_app_menu_action → cx.dispatch_action` 汇入与键盘快捷键完全相同的派发链路；`set_menus` 连带 keymap，macOS 据此显示快捷键。
- 系统通知四件套 `set_app_identity` / `show` / `dismiss` / `on_response` 构成闭环；`tag` 同时是替换键与响应路由凭证，`action_id: Option` 区分点按钮还是点本体；平台默认实现是 no-op，macOS 需从 app bundle 运行。
- 「同步否决、异步决定」是用 prompt 拦截窗口关闭的标准模式：`on_window_should_close` 返回 `false` + 标志防重入，答案到达后再 `remove_window()`。

## 7. 下一步学习建议

- **u7-l4（测试 GPUI 应用）**：本讲的 `simulate_system_notification_response`、`shown_system_notifications` 等 `TestAppContext` API 正是下一讲的主角——prompt、菜单、通知都可以在无 GUI 环境下测试。
- **阅读 `src/app/test_context.rs` 的通知测试**（L1146 起）：看官方如何断言 tag 替换与响应路由，抄回自己的测试。
- **追踪 Zed 主程序的真实用法**：`crates/workspace/src/workspace.rs` 中多处 `window.prompt(...)` 调用展示了带 `cx.listener`、实体更新的生产级写法；`crates/zed/src/zed.rs` 的 `on_system_notification_response` 展示了多窗口路由。
- **回顾 u5-l3/u5-l4**：如果菜单→动作的派发链路还有模糊，回到 Action 体系与键位派发两讲补课，本讲的菜单只是那套链路的一个触发入口。
