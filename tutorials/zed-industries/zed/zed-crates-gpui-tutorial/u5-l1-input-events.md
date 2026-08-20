# 输入事件模型：鼠标、键盘与触摸

## 1. 本讲目标

学完本讲，你应该能够：

- 列举 `MouseDownEvent`、`MouseMoveEvent`、`KeyDownEvent` 等事件结构中的关键字段，知道每个字段回答什么问题。
- 解释 `PlatformInput` 这个枚举如何把 macOS、Linux、Windows、Web、触摸屏等五花八门的平台输入统一成一种「货币」。
- 会做 hitbox 内的坐标换算：把窗口坐标的点击位置转换成元素内部的局部坐标。
- 理解 `ScrollDelta` 的 `Pixels` / `Lines` 两形态，以及为什么滚轮命中判断要用 `should_handle_scroll` 而不是 `is_hovered`。
- 了解 `InputHandler` / `EntityInputHandler` 的 `paste` 钩子如何接收「平台发起」的粘贴（包括 web 端携带图片的 `ClipboardItem`），以及它与自定义 `Paste` action 的区别。
- 会用新增的 `PlatformInput::kind_name` 为日志与遥测提供输入类别的短静态名。

本讲是第 5 单元「交互机制」的第一讲：先把「事件长什么样、从哪里来、怎么送到你的闭包」讲清楚，后续的 `InteractiveElement`（u5-l2）、Action（u5-l3）、焦点（u5-l5）都建立在这些数据结构之上。

## 2. 前置知识

阅读本讲前，你应当已经掌握（对应前面各讲的结论）：

- **div 与链式样式**（u3-l2）：`div()` 是配置收集器，`.on_click`、`.on_mouse_down` 等监听方法挂在 `InteractiveElement` trait 上，链式调用时实际写入 `Interactivity` 结构的各个 listener 向量。
- **Element 三阶段**（u4-l1）：元素每帧走 `request_layout → prepaint → paint`。**交互监听是 paint 阶段注册的**——这一点在本讲会反复出现，它是理解「为什么监听器每帧都要重新挂」的钥匙。
- **窗口绘制管线**（u4-l3）：平台帧回调驱动 `Window::draw`，渲染结果放进 `rendered_frame`，之后到来的输入事件在「上一帧画好的界面」上做命中测试与派发。
- **实体与 Context**（u2-l2 / u2-l3）：`cx.listener(...)` 把元素事件回调绑定到某个实体的 `&mut self` 上，是事件处理的标准写法。

几个本讲要用但可能陌生的术语，先给通俗解释：

- **hitbox（命中盒）**：元素在 paint 阶段向窗口登记的一块矩形区域，附带「鼠标是否悬停在上面」的查询能力。事件派发时，框架靠它决定「这个事件跟这个元素有没有关系」。
- **Capture / Bubble（捕获 / 冒泡）**：借鉴自 Web DOM 的两段式派发。同一事件先按注册顺序正着走一遍（Capture，用于「越靠近底层越先看到」的特殊处理），再倒着走一遍（Bubble，绝大多数业务监听都在这一段）。
- **IME（输入法编辑器）**：输入法组合文字时需要和应用程序反复交换「预编辑文本」「候选窗口位置」等信息，`InputHandler` trait 就是这套交换协议。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/interactive.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs) | 输入事件的「字典」：所有事件结构体（`MouseDownEvent`、`KeyDownEvent`、`ScrollWheelEvent`、`TouchEvent`…）、`PlatformInput` 枚举与 `kind_name` 都定义在这里，约 950 行。 |
| [src/input.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/input.rs) | 文本输入抽象：`EntityInputHandler` trait（面向你的视图实体）与 `ElementInputHandler`（把前者适配成平台可用的 `InputHandler`），含 `paste` 钩子。 |
| [src/elements/div.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs) | 监听器的注册与触发侧：`InteractiveElement` trait、`Interactivity` 的各 `on_*` 方法、`ClickEvent` 的合成逻辑都在这个文件里。 |
| [src/window.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs) | 派发枢纽：`dispatch_event` / `dispatch_mouse_event` / `dispatch_key_event`，以及 `Hitbox`、`Window::on_mouse_event`、`Window::handle_input`。 |
| [src/platform.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs) | 平台侧契约：`InputHandler` trait（含 `paste` 默认实现）、`PlatformInputHandler` 包装、`ClipboardItem` / `ClipboardEntry`。 |
| [src/platform/keystroke.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform/keystroke.rs) | `Keystroke` 与 `Modifiers` 的定义。 |
| [examples/mouse_pressure.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/mouse_pressure.rs) | 最小可运行的输入事件示例：监听触控板压力。 |
| [examples/input.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/input.rs) | 交互全集示例：同时示范了 `EntityInputHandler` 实现与 action 式粘贴，是本讲 4.5 的活教材。 |
| [src/profiler.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/profiler.rs) 与 [src/profiler/journal.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/profiler/journal.rs) | `kind_name` 的消费方（profiler feature 下），本讲只看接口，内部机制留给 u7-l6。 |

记忆技巧：**事件「长什么样」在 `interactive.rs`，事件「怎么送到你手里」在 `div.rs` + `window.rs`，文本输入「怎么和输入法打交道」在 `input.rs` + `platform.rs`**。

## 4. 核心概念与源码讲解

### 4.1 PlatformInput：平台输入的统一货币

#### 4.1.1 概念说明

操作系统报告输入的方式千差万别：macOS 有 `NSEvent`，Linux/X11 有 `XCB` 事件，浏览器有 DOM 事件，触摸屏还有独立的触摸协议。如果每种平台的代码都直接处理原生事件，gpui 的元素层就没法保持跨平台。

GPUI 的解法是定义一个**统一枚举 `PlatformInput`**：各平台后端（gpui_macos / gpui_linux / gpui_windows / gpui_web）负责把自己的原生事件翻译成这个枚举的某个变体，然后交给 `Window::dispatch_event`。对元素和监听器来说，世界上只有一种输入事件——`PlatformInput`。

配套的还有一个密封 trait（sealed trait，外部无法实现）`InputEvent`：每个具体事件结构体都实现它，提供 `to_platform_input()` 方法把自己「装箱」成 `PlatformInput`。这个方法是平台代码和测试工具构造 `PlatformInput` 的标准途径。

#### 4.1.2 核心流程

一次输入从硬件到你的闭包的旅程：

```text
操作系统 / 浏览器原生事件
        │  平台后端翻译（gpui_macos / gpui_linux / gpui_web …）
        ▼
PlatformInput（统一枚举）
        │  Window::dispatch_event（window.rs）
        │    ├─ 记录 mouse_position / modifiers / 输入模态（键盘/鼠标/触摸）
        │    ├─ FileDrop 等特殊事件先翻译成 MouseMove / MouseUp
        │    └─ 按 mouse_event() / keyboard_event() 分流
        ▼
dispatch_mouse_event / dispatch_key_event
        │  在 rendered_frame 上命中测试（hit_test）
        │  Capture 阶段：按注册顺序正序调用监听器
        │  Bubble 阶段：逆序调用监听器（业务代码的主战场）
        ▼
你在 .on_mouse_down(...) / .on_key_down(...) 里注册的闭包
```

两个值得注意的设计：

1. **监听器存在 `rendered_frame` 里**（paint 阶段注册、下一帧到来时仍然有效、再下一帧被替换），所以事件派发永远发生在「已经画好的界面」上——这就是为什么元素移动后旧监听器不会误触发。
2. **传播可以被打断**：任何监听器调用 `cx.stop_propagation()` 后，同阶段后续监听器不再收到该事件。

#### 4.1.3 源码精读

统一枚举本身——12 个变体覆盖了键盘、鼠标、滚轮、手势、文件拖放与触摸：

- [src/interactive.rs:760-787](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L760-L787) —— `PlatformInput` 枚举定义。注意它不是「大而全的原始事件」：变体已经做过语义归一（例如「修饰键变化」独立成 `ModifiersChanged`，而不是伪装成某个键的按下）。

密封 trait 与装箱方法：

- [src/interactive.rs:8-21](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L8-L21) —— `InputEvent` trait（`Sealed` 保证只有 crate 内部的事件类型能实现）加上 `KeyEvent` / `MouseEvent` / `GestureEvent` 三个分类标记 trait。`to_platform_input(self)` 消费自身返回装箱后的枚举。

派发入口（分流逻辑）：

- [src/window.rs:5009-5011](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L5009-L5011) —— `dispatch_event` 的开头。第一件事（在启用 `profiler` feature 时）就是把 `event.kind_name()` 交给 `window_profiler.begin_input` 记一条输入派发计时——这是 `kind_name` 的第一个消费者，4.6 节详述。
- [src/window.rs:5032-5066](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L5032-L5066) —— 用 `match` 逐变体把 `position` / `modifiers` 吸进窗口自己的状态（`self.mouse_position`、`self.modifiers`）。注释解释了原因：平台 API 查询鼠标位置只能在主线程做，所以 GPUI 自己跟踪。
- [src/window.rs:5128-5132](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L5128-L5132) —— 分流终点：`mouse_event()` / `keyboard_event()` 把枚举按「鼠标类 / 键盘类」粗分后送进两条派发管线。触摸事件两者都不是，走独立通道。

两阶段派发的实现：

- [src/window.rs:5195-5216](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L5195-L5216) —— `dispatch_mouse_event` 的核心循环：Capture 正序、Bubble **逆序**（`rev()`，即视觉上最靠前的元素先收到），任一监听器 `stop_propagation` 即中断。开头 [src/window.rs:5182-5186](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L5182-L5186) 还先用 `rendered_frame.hit_test(self.mouse_position())` 做命中测试，命中结果变化时重置光标样式。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「监听器存在帧上、派发发生在渲染结果上」这一机制。

**操作步骤**：

1. 打开 [examples/data_table.rs:323-339](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/data_table.rs#L323-L339)，这是 `window.on_mouse_event` 的真实用例：在 `canvas` 元素的 paint 回调里注册 `MouseDownEvent` / `MouseUpEvent` 监听。
2. 运行示例：在仓库根目录执行 `cargo run -p gpui --example data_table`。
3. 阅读时注意两点：paint 回调签名是 `move |thumb_bounds, _, window, _|`，第一个参数正是 prepaint 阶段算好的 bounds；注册动作 `window.on_mouse_event(...)` 只能在 paint 阶段调用（内部有 `debug_assert_paint` 断言，见 [src/window.rs:4848-4861](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L4848-L4861)）。

**需要观察的现象**：表格可以滚动、滚动条可以拖动——也就是「上一帧 paint 注册的监听器在本帧事件里生效」。

**预期结果**：示例正常交互；若你把 `window.on_mouse_event` 的调用挪出 paint 回调（例如挪到 `render` 方法体里），会触发断言失败（debug 构建下 panic）。**待本地验证**（取决于是否安装好 Rust 工具链与 GUI 环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PlatformInput` 的监听器存在 `rendered_frame` 而不是「注册一次永久有效」？

**参考答案**：GPUI 的元素树是立即模式的，每帧从根视图重建（u3-l1 的核心结论），元素本身跨帧不存在，自然不能长期持有监听器的注册。把监听器挂在帧上，等价于「这一帧画出来的界面声明了它想响应什么」；下一帧重新声明。这也自动解决了元素销毁后监听器悬空的问题。

**练习 2**：Capture 与 Bubble 两个阶段各适合做什么？

**参考答案**：Capture 正序（底层先看）适合「全局拦截 / 越权检查」，例如 `on_mouse_down_out`（鼠标在元素外按下）就必须在 Capture 阶段判断，否则会被元素内部的 Bubble 监听器 `stop_propagation` 截住；Bubble 逆序（视觉最前者先看）是业务点击、悬停的主通道，符合「用户点的是他看到的最上层」的直觉。

### 4.2 MouseDownEvent 与 MouseMoveEvent：鼠标事件与 hitbox 坐标换算

#### 4.2.1 概念说明

鼠标类事件是最高频的输入。两个最基础的结构：

- `MouseDownEvent` / `MouseUpEvent`：按下与释放，携带按钮、位置、修饰键、**点击计数**（click_count，用于区分单击 / 双击 / 三击）。
- `MouseMoveEvent`：移动，携带位置、**当前按住的按钮**（`pressed_button`，用于判断拖拽）。

**关键坐标系知识**：所有事件里的 `position` 都是**窗口坐标**（原点在窗口左上角，单位是逻辑像素 `Pixels`）。而写交互逻辑时你几乎总需要**元素局部坐标**（「点击在我这个元素的哪个位置」）。换算方法：`局部坐标 = event.position - 元素.bounds.origin`。这个减法在 GPUI 代码里无处不在。

另一个关键概念是 **hitbox 判定**：`on_mouse_down` 等监听器触发前会检查 `hitbox.is_hovered(window)`——鼠标不仅要落在矩形内，还不能被更上层的 `occlude` 元素挡住，且当前输入模态不能是键盘（键盘导航时会抑制悬停判定）。

#### 4.2.2 核心流程

以 `.on_mouse_down(MouseButton::Left, listener)` 为例的完整触发链：

```text
用户按下左键
  → 平台翻译为 PlatformInput::MouseDown(MouseDownEvent { button: Left, position, ... })
  → Window::dispatch_event 记录 mouse_position/modifiers
  → dispatch_mouse_event：
      Capture 正序 → Bubble 逆序遍历帧上全部鼠标监听器
  → div 在 paint 时注册的包装闭包被调用，它检查：
      phase == Bubble
      && event.button == 指定按钮
      && hitbox.is_hovered(window)
  → 三个条件都满足 → 你的 listener(event, window, cx) 执行
```

坐标换算的两种常见形态：

```text
形态 A（元素内相对坐标）：local = event.position - element_bounds.origin
形态 B（拖拽启动偏移）：  cursor_offset = event.position - hitbox.origin
```

拖拽还有一个距离阈值：按下点与移动点距离超过 `DRAG_THRESHOLD`（2 逻辑像素）才算拖拽开始，用来区分「手抖」和「真拖」。

#### 4.2.3 源码精读

事件结构体：

- [src/interactive.rs:137-154](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L137-L154) —— `MouseDownEvent`：`button`、`position`（窗口坐标）、`modifiers`、`click_count`（连续点击次数）、`first_mouse`（是否窗口激活后的首次聚焦点击）。
- [src/interactive.rs:483-494](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L483-L494) —— `MouseMoveEvent`：`position`、`pressed_button: Option<MouseButton>`、`modifiers`。
- [src/interactive.rs:504-509](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L504-L509) —— 便捷方法 `dragging()`：左键按住即视为拖拽中。
- [src/interactive.rs:442-470](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L442-L470) —— `MouseButton` 枚举：`Left` / `Right` / `Middle` / `Navigate(Back|Forward)`，`all()` 返回全部按钮。

监听器注册侧（`Interactivity` 的命令式方法，trait 方法只是转发）：

- [src/elements/div.rs:118-136](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L118-L136) —— `Interactivity::on_mouse_down`：把闭包包一层「三条件检查」（Bubble + 按钮匹配 + `hitbox.is_hovered(window)`）后压入 `mouse_down_listeners`。**读这段代码就等于读懂了所有 `on_*` 监听器的通用模式**。
- [src/elements/div.rs:296-309](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L296-L309) —— `Interactivity::on_mouse_move`：同样检查 hover，所以「鼠标移过元素」才会触发，而非全窗口移动都触发。
- [src/elements/div.rs:828-839](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L828-L839) —— `InteractiveElement` trait 上的流式 `on_mouse_down`（定义于 [src/elements/div.rs:732](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L732) 的 trait），只是调用上面的命令式版本。

hitbox 判定的语义：

- [src/window.rs:818-862](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L818-L862) —— `Hitbox` 结构与 `is_hovered` / `should_handle_scroll`。文档注释详细解释了 `is_hovered` 返回 `false` 的三种情况：被 `BlockMouse` 挡住、被 `BlockMouseExceptScroll` 挡住、当前是键盘模态。**滚轮请用 `should_handle_scroll`**（见 4.4）。

坐标换算的真实范例：

- [src/elements/div.rs:2853-2858](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L2853-L2858) —— 框架自己写的两处换算：`(event.position - mouse_down.position).magnitude() > DRAG_THRESHOLD`（拖拽阈值判定，常量 `DRAG_THRESHOLD = 2.` 定义于 [src/elements/div.rs:48](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L48)）和 `cursor_offset = event.position - hitbox.origin`（拖拽物相对于源的偏移）。
- [examples/data_table.rs:328-337](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/data_table.rs#L328-L337) —— 应用层写法：`thumb_bounds.contains(&ev.position)` 先判断点是否在滚动条滑块内，再用 `ev.position - thumb_bounds.origin - table_bounds.origin` 换算局部坐标。

#### 4.2.4 代码实践

**实践目标**：实现「点击处显示局部坐标」的探针，验证窗口坐标 → 局部坐标的换算。

**操作步骤**（示例代码，基于 mouse_pressure 骨架改写）：

1. 复制 `examples/mouse_pressure.rs` 为 `examples/position_probe.rs`，并在 `Cargo.toml` 追加一个 `[[example]]` 块（格式见 [Cargo.toml:177-179](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/Cargo.toml#L177-L179)，name 填 `position_probe`、path 填 `examples/position_probe.rs`；示例必须显式声明才会被构建，这是 u1-l4 验证过的规则）。也可以直接改造 `mouse_pressure.rs` 本身以省去这一步。
2. 把状态字段改成 `last_down: Option<Point<Pixels>>`，render 里挂监听并显示：

```rust
// 示例代码：div 内监听左键按下，换算局部坐标
div()
    .id("probe")
    .size(px(300.))
    .on_mouse_down(
        MouseButton::Left,
        cx.listener(|this, event: &MouseDownEvent, _window, cx| {
            // event.position 是窗口坐标；这里 div 铺满窗口，
            // 局部坐标恰好等于窗口坐标。想看到差异，
            // 可以给 div 加 .m_32() 再观察数值变化。
            this.last_down = Some(event.position);
            cx.notify();
        }),
    )
    .when_some(this.last_down, |el, p| {
        el.child(format!("down at window ({:.0}, {:.0})", p.x, p.y))
    })
```

3. 运行：`cargo run -p gpui --example position_probe`（在仓库根目录执行）。

**需要观察的现象**：点击后显示的坐标；给 div 加上 `.m_32()`（32px 外边距）后，同一个物理位置显示的数值不变（因为它显示的是窗口坐标）——这正是要换算的原因。

**预期结果**：你能直观看到 `event.position` 与元素边界无关、始终是窗口坐标系；按 4.2.2 的公式减去 `bounds.origin` 后才会得到元素内坐标。若手头没有 GUI 环境（如无头 CI），此步**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如何区分「双击」与「两次单击」？

**参考答案**：看 `MouseDownEvent.click_count`（[src/interactive.rs:149-150](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L149-L150)）——平台负责在时间窗口内累计点击次数，`click_count == 2` 即双击。不要自己在监听器里记录上次点击时间戳重复造轮子。

**练习 2**：`MouseMoveEvent::dragging()` 什么时候返回 true？为什么不用 `modifiers.shift` 之类的字段判断？

**参考答案**：`pressed_button == Some(MouseButton::Left)` 时返回 true（[src/interactive.rs:504-509](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L504-L509)）。拖拽的物理事实是「按住左键移动」，与键盘修饰键无关；`data_table.rs:350` 里 `if !ev.dragging() { return; }` 就是滚动条拖动的守卫条件。

**练习 3**：为什么 `on_mouse_move` 的监听器在鼠标移出元素后就不再触发？

**参考答案**：注册闭包里检查了 `hitbox.is_hovered(window)`（[src/elements/div.rs:304-308](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L304-L308)），鼠标不在 hitbox 上时直接跳过你的回调。想监听全窗口移动要用 `window.on_mouse_event`（4.1.4 的 data_table 用法）。

### 4.3 KeyDownEvent：Keystroke 与 Modifiers

#### 4.3.1 概念说明

键盘事件比鼠标多一层抽象：`KeyDownEvent` 里装的不是裸按键，而是一个 **`Keystroke`** 结构，把「按了什么键」拆成修饰键状态 + 键名 + 可能产生的字符三部分。这个拆分是为了应对键盘布局的复杂性：同一个物理按键在不同布局下产出不同字符，非 ASCII 布局（如泰文）上 `key` 是 ASCII 等价键、真实输入字符在 `key_char` 里。

`Modifiers` 则是独立的五布尔结构（control / alt / shift / platform / function），它不仅随键盘事件传来，还随鼠标事件、滚轮事件传来——「Ctrl+滚轮缩放」「Shift+点击扩展选区」都靠它。

还要注意：**`KeyDownEvent` 是「原始按键」层，不是「快捷键」层**。把按键映射成业务操作（action）要经过 Keymap 与 DispatchTree，那是 u5-l3 / u5-l4 的主题。本讲只关心原始事件。

#### 4.3.2 核心流程

```text
平台按键事件
  → 翻译为 PlatformInput::KeyDown(KeyDownEvent { keystroke, is_held, prefer_character_input })
  → Window::dispatch_event → dispatch_key_event
  → 若窗口脏：先补画一帧（保证监听器对应最新界面）
  → 键盘监听器沿 DispatchTree 派发（Capture → Bubble）
  → （并行通道） keystroke 参与 Keymap 匹配 → 命中则派发 Action（u5-l3 详述）
```

字段速查：

| 字段 | 回答的问题 |
| --- | --- |
| `keystroke.key` | 按键上印的字符（ASCII 等价），如 `"s"`、`"enter"` |
| `keystroke.key_char` | 该组合实际会输入的字符，如 option-s 是 `"ß"`，cmd-s 是 `None` |
| `keystroke.modifiers` | 按下时的修饰键状态 |
| `is_held` | 是否按住重复（key repeat） |
| `prefer_character_input` | 是否应优先当字符输入而非快捷键（Windows 上 AltGr 场景） |

#### 4.3.3 源码精读

- [src/interactive.rs:23-35](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L23-L35) —— `KeyDownEvent` 结构：`keystroke` + `is_held` + `prefer_character_input`，doc 注释明确说明了 AltGr 的场景。
- [src/platform/keystroke.rs:16-33](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform/keystroke.rs#L16-L33) —— `Keystroke`：`modifiers` / `key` / `key_char` 三字段，注释给了泰文布局的例子。
- [src/platform/keystroke.rs:446-477](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform/keystroke.rs#L446-L477) —— `Modifiers` 五布尔 + `modified()`（任一修饰键按下）与 `secondary()`（macOS 看 cmd、其他平台看 ctrl 的「语义次要键」）。
- [src/interactive.rs:60-67](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L60-L67) —— `ModifiersChangedEvent`：单独的修饰键变化事件（按下 shift 本身不产生 KeyDown）。它还能 `Deref` 到 `Modifiers`（[src/interactive.rs:77-83](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L77-L83)）。
- [src/elements/div.rs:465-479](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L465-L479) —— `Interactivity::on_key_down`：注意与鼠标监听的区别——**没有 hitbox 检查**，键盘事件按焦点路径派发而不是按鼠标位置命中。
- [src/interactive.rs:868-888](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L868-L888) —— crate 内测试视图：`.on_key_down` 里 `cx.stop_propagation()` 后，同一次按键不再触发 action 派发（配套断言见 [src/interactive.rs:913-923](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L913-L923)，`saw_key_down` 与 `saw_action` 同时为 true 的用例是「不 stop」的情况）。

#### 4.3.4 代码实践

**实践目标**：用测试基础设施在无 GUI 环境下观察键盘事件与修饰键事件。

**操作步骤**：

1. 阅读 [src/interactive.rs:925-951](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L925-L951) 的测试 `test_multi_modifier_gesture_does_not_dispatch_standalone_modifier_binding`：它先 `bind_keys` 一个 `shift` 绑定，再用 `simulate_modifiers_change` 序列模拟「按 alt → alt+shift → shift → 无」。
2. 运行它：`cargo test -p gpui multi_modifier`。
3. 把注意力放在断言上：第一组模拟结束时 `saw_action` 为 false（多修饰键手势不触发单修饰键绑定），第二组（纯 shift 按下再松开）为 true。

**需要观察的现象**：修饰键变化走的是 `ModifiersChangedEvent` 通道，而 `shift` 单键绑定只在「干净的按下-释放」时命中。

**预期结果**：测试通过。这属于源码阅读型实践，**命令需在本地仓库执行验证**。

#### 4.3.5 小练习与答案

**练习 1**：`Keystroke.key` 与 `key_char` 有什么区别？为什么 cmd-s 的 `key_char` 是 `None`？

**参考答案**：`key` 是按键印字（布局无关的 ASCII 等价），`key_char` 是该组合实际输入的字符。cmd-s 不产生任何可输入字符（它是纯快捷键组合），所以是 `None`；option-s 在 macOS 美式布局上会产出 `ß`（[src/platform/keystroke.rs:29-32](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform/keystroke.rs#L29-L32) 的注释原例）。

**练习 2**：想实现「Ctrl+点击 = 打开右键菜单」，应该读哪个字段？

**参考答案**：读 `MouseDownEvent.modifiers`（4.2.3 引用的结构体字段），而不是尝试在键盘监听器里记录 Ctrl 状态。事件自带修饰键快照，跨事件自己维护状态既容易失效也不必要。

### 4.4 ScrollDelta：滚轮、触摸与压力/手势事件

#### 4.4.1 概念说明

`ScrollWheelEvent` 最特别的地方是它的 `delta` 字段——类型 `ScrollDelta` 是个两形态枚举：

- `Pixels(Point<Pixels>)`：**精确**像素增量（触控板双指滑动、高精度滚轮）。
- `Lines(Point<f32>)`：**非精确**的「行数」增量（传统滚轮一格一格）。

为什么必须区分？因为传统鼠标滚轮没有物理分辨率，只能报告「滚了几格」；而触控板是连续的像素流。统一处理的标准做法是调 `pixel_delta(line_height)`：`Lines` 乘以一个每行高度换算成像素，`Pixels` 原样返回。

本模块还顺带覆盖三个「非鼠标也非键盘」的事件：

- `TouchEvent`：触摸屏原始事件，带 `TouchPhase` 生命周期与压力 `force`。
- `MousePressureEvent`：Force Touch 触控板压力（目前仅 macOS），`examples/mouse_pressure.rs` 是官方示例。
- `PinchEvent`：双指捏合缩放手势，`delta` 为缩放比例增量。

滚轮命中判断的特殊规则也在这里：用 `should_handle_scroll` 而不是 `is_hovered`，因为悬浮层（如遮罩）可能挡住鼠标交互但不应挡住其下方内容的滚动。

#### 4.4.2 核心流程

滚轮事件处理的标准姿势：

```text
收到 ScrollWheelEvent { delta, touch_phase, modifiers, position }
  → 判定命中：hitbox.should_handle_scroll(window)（不是 is_hovered！）
  → 统一单位：delta.pixel_delta(line_height) 得到 Point<Pixels>
  → 取负号与否取决于你的滚动语义（GPUI 惯例：delta.y 为负 = 内容向上滚）
  → 累加滚动偏移 → cx.notify() 触发重绘
```

`ScrollDelta::coalesce` 的合并规则（用于把高频小事件合成一次滚动）：

- 同号（同向）→ 相加；
- 异号（反向）→ 用后者覆盖前者（新方向优先）。

#### 4.4.3 源码精读

- [src/interactive.rs:511-525](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L511-L525) —— `ScrollWheelEvent`：`position` / `delta` / `modifiers` / `touch_phase`（触摸屏滚动也有相位）。
- [src/interactive.rs:543-556](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L543-L556) —— `ScrollDelta` 枚举与默认值（默认 `Lines`，说明传统滚轮是最古老的形态）。
- [src/interactive.rs:595-610](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L595-L610) —— `precise()` 与 `pixel_delta(line_height)`：后者是统一单位的官方换算函数。
- [src/interactive.rs:612-653](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L612-L653) —— `coalesce`：同号相加、异号取新的合并算法。
- [src/elements/div.rs:360-374](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L360-L374) —— `Interactivity::on_scroll_wheel`：注意命中检查用的是 `hitbox.should_handle_scroll(window)`，与鼠标类监听器的 `is_hovered` 形成对照。
- [src/interactive.rs:85-128](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L85-L128) —— `TouchPhase`（Started/Moved/Ended/Cancelled）与 `TouchEvent`。doc 注释写明了派发契约：触摸在 `Started` 时做一次遮挡感知的命中测试，后续事件即使移出仍派发给起始位置的元素（即「触摸捕获」语义）。
- [src/interactive.rs:219-243](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L219-L243) —— `PressureStage`（Zero/Normal/Force）与 `MousePressureEvent`（`pressure: f32` 0..1、`stage`、`position`、`modifiers`），注释说明目前仅 macOS 触控板实现。
- [src/interactive.rs:558-576](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L558-L576) —— `PinchEvent`：`delta: f32` 正为放大、负为缩小（0.1 = 10% 缩放增量）。
- [examples/mouse_pressure.rs:14-33](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/mouse_pressure.rs#L14-L33) 与 [examples/mouse_pressure.rs:36-47](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/mouse_pressure.rs#L36-L47) —— 压力事件的完整用法：`.on_mouse_pressure(cx.listener(Self::on_mouse_pressure))`，回调里读 `pressure_event.pressure` 与 `.stage` 后 `cx.notify()` 刷新界面。这是**本讲推荐第一个跑起来的示例**。

#### 4.4.4 代码实践

**实践目标**：区分精确与非精确滚轮增量，观察 `pixel_delta` 的换算效果。

**操作步骤**：

1. 运行 `cargo run -p gpui --example mouse_pressure`，确认环境可用（macOS 上可用触控板重按体验压力事件；Linux/Windows 上窗口正常打开但没有压力事件——该事件仅在 macOS 报告）。
2. 在 4.2.4 的探针示例里追加滚轮监听（示例代码）：

```rust
// 示例代码：显示滚轮增量的两种形态
.on_scroll_wheel(cx.listener(|this, event: &ScrollWheelEvent, window, cx| {
    let (kind, raw) = match &event.delta {
        ScrollDelta::Pixels(p) => ("pixels", format!("({:.1}, {:.1})", p.x, p.y)),
        ScrollDelta::Lines(l) => ("lines", format!("({:.2}, {:.2})", l.x, l.y)),
    };
    let unified = event.delta.pixel_delta(window.line_height());
    this.scroll_info = format!(
        "{kind} {raw} → pixel_delta ({:.1}, {:.1})",
        unified.x, unified.y
    );
    cx.notify();
}))
```

3. 分别用触控板双指滑动（预期 `pixels`）和鼠标滚轮（预期 `lines`，每格约 ±1.0 或 ±3.0）操作。

**需要观察的现象**：两种设备产生的 `ScrollDelta` 形态不同；`Lines` 形态经 `pixel_delta(line_height)` 后变成与行高成比例的像素值。

**预期结果**：界面上能实时看到形态标签与换算后的数值。无滚轮/触控板设备时**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么滚轮监听不能用 `is_hovered` 判定命中？

**参考答案**：因为存在「挡住鼠标但不挡滚动」的合法场景（`HitboxBehavior::BlockMouseExceptScroll`，由 `block_mouse_except_scroll` 设置）——例如覆盖层让下层不可点击但允许继续滚动其下方内容。`should_handle_scroll` 专门表达这层语义，见 [src/window.rs:844-862](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L844-L862) 的文档说明。

**练习 2**：`ScrollDelta::coalesce` 为什么「异号时用后者覆盖」而不是返回零？

**参考答案**：用户快速反向滚动时，新方向代表最新意图，覆盖旧增量能让滚动立即跟随新方向；返回零会让滚动「卡死」到事件流结束。见 [src/interactive.rs:612-653](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L612-L653) 的注释与实现。

**练习 3**：触摸事件的 `TouchPhase::Cancelled` 意味着什么，处理时必须做什么？

**参考答案**：系统接管了这次触摸、它不会正常结束；文档要求消费方「完全回滚进行中的交互，当作触摸从未提交」（[src/interactive.rs:96-99](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L96-L99)）。例如拖拽中收到 Cancelled 应把元素放回原位而不是停在半路。

### 4.5 InputHandler 与 EntityInputHandler：文本输入与 paste 钩子

#### 4.5.1 概念说明

纯按键事件不足以支撑文本编辑：输入法组合、候选窗定位、系统粘贴等都需要「平台 ↔ 应用」双向对话。GPUI 把这套协议抽象成三个层次：

1. **`InputHandler`**（[src/platform.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs)）：平台侧看到的接口，方法基本是 Apple `NSTextInputClient` API 的 1:1 映射（选区读写、预编辑标记、范围换算、候选窗定位）。它是类型擦除的 `dyn` 对象。
2. **`EntityInputHandler`**（[src/input.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/input.rs)）：面向你的视图实体的强类型版本，每个方法都带 `&mut Context<Self>`，可以直接改实体状态。
3. **`ElementInputHandler<V>`**：适配器，持有 `Entity<V>`，把 `InputHandler` 的每个调用转发成对实体的 `update`。

粘贴有**两条完全不同的路径**，这是本模块的重点：

- **应用自定义 action 路径**：你定义 `Paste` action、绑定 `cmd-v`、在处理器里主动 `cx.read_from_clipboard()`——一切由应用发起。
- **平台发起路径**：某些平台把粘贴作为**输入事件**投递而非可拦截的按键——最典型是 Web 的 DOM `paste` 事件（用户在浏览器里按 Cmd+V 时页面拿到的是 paste 事件）。这时平台手里已经握着剪贴板内容，需要应用提供 `paste` 钩子来接收。

#### 4.5.2 核心流程

平台发起粘贴的调用链：

```text
Web: DOM paste 事件（或 macOS IME 的 insertText:paste）
  → 平台代码构造 ClipboardItem（web 端可能同时含 String 与 Image 条目）
  → PlatformInputHandler::paste(item)                     （platform.rs）
  → InputHandler::paste(item, window, cx)                  （trait 默认实现）
  → ElementInputHandler::paste → view.update(...)          （input.rs 适配层）
  → EntityInputHandler::paste(item, window, cx)            （你的视图实体方法）
  → 默认实现：item.text() 取纯文本 → replace_text_in_range 插入
```

而应用自定义路径只是：`cmd-v` 按键 → Keymap 匹配 `Paste` action → 处理器里 `cx.read_from_clipboard()` 自己拿数据。两条路径最终都落到 `replace_text_in_range` 这类编辑原语上。

#### 4.5.3 源码精读

trait 层的 paste 钩子（带默认实现，只插入纯文本部分）：

- [src/platform.rs:1734-1744](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L1734-L1744) —— `InputHandler::paste` 的默认实现与文档：明确写着「把粘贴作为输入事件而非应用 action 投递的平台（如 web 的 DOM paste 事件）会带着完整剪贴板内容调用它」。
- [src/input.rs:6-12](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/input.rs#L6-L12) 与 [src/input.rs:40-45](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/input.rs#L40-L45) —— `EntityInputHandler` trait 与其 `paste` 默认实现：`item.text()` 取出全部字符串条目拼接后经 `replace_text_in_range(None, ...)` 插入当前选区。

适配层：

- [src/input.rs:107-124](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/input.rs#L107-L124) —— `ElementInputHandler<V>`：持有 `Entity<V>` 与元素 bounds，构造入口是 `new(element_bounds, view)`。
- [src/input.rs:191-194](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/input.rs#L191-L194) —— `ElementInputHandler` 的 `paste` 转发：`self.view.update(cx, |view, cx| view.paste(item, window, cx))`，即从类型擦除世界回到强类型实体。

挂载点（必须在 paint 阶段调用）：

- [src/window.rs:4819-4841](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L4819-L4841) —— `Window::handle_input`：元素聚焦时把 `Box<dyn InputHandler>` 包进 `PlatformInputHandler` 存入**下一帧**的 `input_handlers`——和鼠标监听器一样是「每帧重新声明」的模式。
- [examples/input.rs:552-557](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/input.rs#L552-L557) —— 官方用法：自定义元素的 paint 里 `window.handle_input(&focus_handle, ElementInputHandler::new(bounds, self.input.clone()), cx)`。

平台包装层：

- [src/platform.rs:1433-1449](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L1433-L1449) —— `PlatformInputHandler`：`AsyncWindowContext` + `Box<dyn InputHandler>` 的组合，是平台代码（运行在异步上下文）访问输入处理的桥。
- [src/platform.rs:1522-1526](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L1522-L1526) —— `PlatformInputHandler::paste(item)`：通过 `cx.update` 短暂借用窗口后转发给 handler。

剪贴板数据结构：

- [src/platform.rs:2304-2354](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L2304-L2354) —— `ClipboardItem`（`entries: Vec<ClipboardEntry>`）与 `ClipboardEntry`（`String` / `Image` / `ExternalPaths` 三形态）：一次粘贴可以同时携带文本和图片。
- [src/platform.rs:2390-2399](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L2390-L2399) —— `ClipboardItem::text()`：只拼接字符串条目——这就是默认 `paste` 丢掉图片的原因，也是你需要覆写它的原因。

web 平台的真实调用方（注意此文件在 gpui_web crate，链接相应指向 crates/gpui_web）：

- [../gpui_web/src/events.rs:490-549](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_web/src/events.rs#L490-L549) —— DOM paste 处理：同步收集 `clipboardData` 里的文件条目（注释解释了为什么必须同步：处理器返回后浏览器会清空 item 列表），无图片时直接 `handler.paste(ClipboardItem::new_string(text))`；有图片时异步读出字节，构造 `entries = [String, Image, ...]` 的 `ClipboardItem` 再调 `handler.paste`。**截图粘贴进 web 版输入框走的就是这条路**。

两条路径的活对照：

- [examples/input.rs:144-148](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/input.rs#L144-L148) —— action 式 `paste`：`Paste` action 处理器里主动 `cx.read_from_clipboard()` 并把换行替换成空格后插入。它与 `EntityInputHandler::paste` 是并存的两条通道。
- [examples/input.rs:274-332](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/input.rs#L274-L332) —— `TextInput` 实现 `EntityInputHandler` 的选区与替换方法（`replace_text_in_range` 是所有文本插入的最终汇聚点）。

#### 4.5.4 代码实践

**实践目标**：跑通官方文本输入示例，找到 paste 钩子在源码里的位置。

**操作步骤**：

1. 运行 `cargo run -p gpui --example input`，在输入框里打字、用 cmd/ctrl-v 粘贴文本，观察选区与预编辑行为。
2. 打开 [examples/input.rs:274](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/input.rs#L274) 起的 `impl EntityInputHandler for TextInput`，数一数它实现了哪些方法（`text_for_range`、`selected_text_range`、`marked_text_range`、`replace_text_in_range`……）。
3. 思考题式验证：在该 impl 里追加一个显式 `paste` 覆写（示例代码）：

```rust
// 示例代码：覆写 paste 钩子，观察平台发起的粘贴
fn paste(&mut self, item: ClipboardItem, window: &mut Window, cx: &mut Context<Self>) {
    eprintln!(
        "EntityInputHandler::paste called, {} entries",
        item.entries.len()
    );
    // 不调用默认逻辑，改为插入占位文本以证明钩子确实被走到
    self.replace_text_in_range(None, "[pasted]", window, cx);
}
```

4. 重新运行示例并粘贴。

**需要观察的现象**：终端 stderr 打印条目数；输入框插入 `[pasted]` 而不是剪贴板原文——证明这次粘贴走的是平台发起的 `paste` 钩子而非 `Paste` action（如果两个通道都触发，你会先看到 action 处理器的效果再被钩子覆盖，具体顺序因平台而异）。

**预期结果**：在 macOS 上 cmd-v 通常经 IME/按键派发仍走 action 路径（此钩子不一定被触发）；在 web 构建（wasm）里 DOM paste 事件必走此钩子。桌面行为**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `InputHandler` 的方法都用 UTF-16 偏移而不是 Rust 惯用的字节偏移或字符索引？

**参考答案**：因为该 trait 是 `NSTextInputClient` 的 1:1 映射（[src/platform.rs:1669-1673](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L1669-L1673)），而 Web 的 DOM Range 同样以 UTF-16 为单位。用平台的交换单位可以避免两头反复换算；`examples/input.rs` 里的 `range_from_utf16` / `range_to_utf16` 就是边界处的翻译函数。

**练习 2**：默认 `paste` 实现会丢掉剪贴板里的图片。如果要做「粘贴截图」，应该在哪一层覆写？

**参考答案**：在视图实体的 `EntityInputHandler::paste` 覆写（或为 `InputHandler` 提供自定义实现），遍历 `item.entries` 找 `ClipboardEntry::Image`；默认实现只调 `item.text()`（[src/input.rs:41-45](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/input.rs#L41-L45)），图片条目被静默忽略。

**练习 3**：`Window::handle_input` 为什么要求在 paint 阶段调用，而且只对聚焦元素生效？

**参考答案**：它内部有 `debug_assert_paint()` 断言（[src/window.rs:4833](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L4833)），且 `if focus_handle.is_focused(self)` 才把 handler 存进 `next_frame.input_handlers`。输入法对话对象必须始终对应「当前聚焦的编辑区」，而聚焦状态每帧都可能变，所以与监听器一样采用「每帧重新声明」的模式。

### 4.6 PlatformInput::kind_name：给输入类别起一个短名字

#### 4.6.1 概念说明

提交 `1861e58f98`（gpui: Journal foreground work between frames and report hang incidents）给 `PlatformInput` 增加了一个小而实用的方法：`kind_name()`——为每个变体返回一个短静态字符串（`"key_down"`、`"scroll_wheel"`、`"mouse_move"`……）。

它解决的问题是**可观测性里的命名一致性**：卡顿分析、日志、遥测都需要回答「这条记录是由哪种输入触发的」。如果各处自己写 `match` 拼 `Debug` 字符串，会出现 `"MouseDown"`、`"mouse-down"`、`"mouse down"` 多种拼法，无法聚合。一个返回 `&'static str` 的统一方法让所有消费方共享同一套名字，且静态生命周期意味着零分配、可以直接存进紧凑的记录结构。

注意它**不是**给应用开发者日常用的 API——你写交互逻辑时应该 match 具体事件；`kind_name` 的服务对象是 profiler/journal 这类横切观测层（u7-l6 详述）。

#### 4.6.2 核心流程

```text
PlatformInput 到达 Window::dispatch_event
  →（cfg(feature = "profiler")）window_profiler.begin_input(event.kind_name())
  → profiler 记录 WindowActivity::Input { kind, started_at }
  → 派发完成后 end_input(caused_invalidation)
  → 写入 journal 的 InputTiming { kind, start, end, caused_invalidation }
  → HangDetector 分析时，每条输入记录都带着人类可读的类别名
```

#### 4.6.3 源码精读

- [src/interactive.rs:824-841](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L824-L841) —— `kind_name` 本体：一个 12 臂 match，每臂返回全小写下划线命名的 `&'static str`，doc 注释写明用途是「diagnostics and telemetry」。
- [src/window.rs:5009-5011](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L5009-L5011) —— 第一个消费者：`dispatch_event` 开头把它交给 `window_profiler.begin_input`（仅 `profiler` feature 下编译）。
- [src/profiler.rs:940-948](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/profiler.rs#L940-L948) —— `begin_input(kind: &'static str)`：kind 被原样存进 `WindowActivity::Input`，静态生命周期让它可以直接内嵌在活动记录里。
- [src/profiler/journal.rs:115-127](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/profiler/journal.rs#L115-L127) —— `InputTiming` 结构：`kind`（即 `kind_name` 的返回值）与起止时间、是否引发窗口失效并存在一起——日后看到卡顿报告里「input (scroll_wheel) took 120ms」就来自这里。

#### 4.6.4 代码实践

**实践目标**：在你的事件探针里为每条到达的事件打上类别名。

**操作步骤**：

1. 在 4.2.4 / 4.4.4 的探针示例顶部补充导入 `use gpui::InputEvent;`（`to_platform_input` 是该 trait 的方法，而 `InputEvent` **不在** [src/prelude.rs:5-9](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/prelude.rs#L5-L9) 的 prelude 里）。
2. 在各监听器里把收到的事件克隆后装箱再取类别名（示例代码）：

```rust
// 示例代码：on_mouse_move 监听器里打印类别名
.on_mouse_move(cx.listener(|_this, event: &MouseMoveEvent, _window, _cx| {
    // 事件结构体都是 Clone；to_platform_input 消费 self，所以先 clone
    let kind = event.clone().to_platform_input().kind_name();
    eprintln!("dispatched input kind: {kind}"); // → "mouse_move"
}))
```

3. 对 `on_scroll_wheel`、`on_key_down` 重复同样的模式，预期分别打印 `scroll_wheel`、`key_down`。

**需要观察的现象**：终端输出的类别名与 4.6.3 的枚举一一对应；同一次物理滚动会连续打印多条 `scroll_wheel`（高精度设备事件很密）。

**预期结果**：每条派发到该元素的事件都有统一的短名输出。**待本地验证**（需要 GUI 环境）。

#### 4.6.5 小练习与答案

**练习 1**：`kind_name` 为什么返回 `&'static str` 而不是 `String` 或 `format!` 的结果？

**参考答案**：名字集合在编译期就完全确定（12 个变体），静态字符串零分配、零成本拷贝，还能直接存进 `InputTiming` 这类紧凑记录结构（[src/profiler/journal.rs:120](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/profiler/journal.rs#L120)）。返回 `String` 意味着每次派发输入都堆分配一次，对一个每秒可能触发上百次的路径是不可接受的。

**练习 2**：如果你要给自己定义的「合成输入」记录日志，应该怎么复用这套命名？

**参考答案**：不要发明新名字——构造对应的 `PlatformInput`（各事件结构体都实现了 `InputEvent::to_platform_input`），直接调它的 `kind_name()`。测试工具 `VisualTestContext::simulate_event` 就是这么把任意事件装箱派发的（[src/app/test_context.rs:925-929](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app/test_context.rs#L925-L929)）。

## 5. 综合实践

**任务：实现一个「输入仪表盘」示例**，把本讲六个模块串起来。

需求：

1. 一个 400×400 的面板，同时监听 `on_mouse_down`（记录按钮 + 窗口坐标 + click_count）、`on_mouse_move`（实时位置 + 是否拖拽中）、`on_scroll_wheel`（delta 形态与换算值）。
2. 界面分三行实时显示上述三类信息的关键字段（用 4.4.4 的 `format!` 模式）。
3. 每条到达的事件同时用 `PlatformInput::kind_name` 打印类别名到 stderr。
4. 在面板内画一个 100×100 的内层方块，点击它时显示**相对内层方块的局部坐标**（用 `event.position - bounds.origin`；获取 bounds 可以借助 canvas 元素 prepaint 回调拿到的 bounds，参照 [examples/data_table.rs:323-339](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/data_table.rs#L323-L339) 的做法，或用 `.id()` + `window.bounds()` 相关调试设施）。

实现提示：

- 骨架直接复制 [examples/mouse_pressure.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/mouse_pressure.rs)（它已经包含 Application/open_window/cx.new/Render 四步曲与 `cx.listener` 用法）。
- 新建文件需要同时在 Cargo.toml 声明 `[[example]]`（见 [Cargo.toml:177-179](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/Cargo.toml#L177-L179) 的格式）。
- 需要的导入：`MouseButton`、`MouseDownEvent`、`MouseMoveEvent`、`ScrollWheelEvent`、`ScrollDelta`、`InputEvent`（trait），加上原有的 `gpui::prelude::*`。
- 别忘了每个监听器最后 `cx.notify()`，否则界面不会刷新（u2-l3 的响应式循环）。

验收标准（自测清单）：

- [ ] 移动鼠标时第二行数字连续变化；
- [ ] 按住左键拖动时显示 `dragging: true`；
- [ ] 触控板滑动显示 `pixels` 形态、鼠标滚轮显示 `lines` 形态，且都有换算后的像素值；
- [ ] 点击内层方块显示的局部坐标落在 0..100 区间，点击方块外则显示窗口坐标语义；
- [ ] stderr 里能看到 `mouse_move` / `mouse_down` / `scroll_wheel` 等类别名。

若没有 GUI 环境，退化方案：把该示例改写成 `#[gpui::test]`，用 `VisualTestContext` 的 `simulate_mouse_move` / `simulate_mouse_down` / `simulate_event(ScrollWheelEvent { .. })`（[src/app/test_context.rs:804-864](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app/test_context.rs#L804-L864)）驱动同样的监听器并断言状态字段——这同时预习了 u7-l4 的测试方法。

## 6. 本讲小结

- `PlatformInput` 是所有平台输入的统一枚举，平台后端负责把原生事件翻译进来；事件派发发生在 `rendered_frame` 上，监听器是 paint 阶段注册、每帧重建的。
- 鼠标事件的 `position` 一律是窗口坐标，元素局部坐标要用 `event.position - bounds.origin` 换算；`click_count`、`pressed_button`、`first_mouse` 等字段分别解决连击、拖拽、聚焦点击的判定。
- 键盘事件的核心是 `Keystroke` 三元组（modifiers / key / key_char），它把布局差异吸收掉；修饰键状态还随鼠标、滚轮事件一起流动。
- `ScrollDelta` 分 `Pixels`（精确）与 `Lines`（非精确）两形态，统一换算靠 `pixel_delta(line_height)`；滚轮命中判定用 `should_handle_scroll` 而非 `is_hovered`。
- 文本输入走 `InputHandler` / `EntityInputHandler` 双层抽象：平台发起的粘贴（如 web 的 DOM paste，可携带图片 `ClipboardItem`）经 `paste` 钩子进入视图实体，与应用自定义 `Paste` action 是两条并存通道。
- 新增的 `PlatformInput::kind_name` 为 12 个变体提供统一的短静态名，供 profiler/journal 等观测层零成本记录「哪种输入触发了这次工作」。

## 7. 下一步学习建议

下一讲（u5-l2「InteractiveElement 与状态化元素」）将深入本讲反复出现的 `div.rs`：`on_click` 等监听器为什么必须配合 `.id()` 生成的 `Stateful<Div>` 才能管理 hover/active 状态，以及 4.4 里一笔带过的 `ClickEvent` 合成细节（本讲 4.2.3 只引了注册侧，合成逻辑在 [src/elements/div.rs:2827-3001](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L2827-L3001)，阅读它之前建议先学完 u5-l2 的状态化元素概念）。

继续阅读的建议路径：

1. 通读 [src/interactive.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs) 的 `ClickEvent` 辅助方法区（`is_right_click` / `standard_click` / `first_focus` 等，[src/interactive.rs:296-429](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/interactive.rs#L296-L429)），体会「鼠标/键盘/触摸三种点击归一」的设计。
2. 对照 [examples/input.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/input.rs) 通读一遍完整实现，它是本讲 4.5 的最佳全长教材。
3. 对卡顿观测感兴趣的话，预习 `kind_name` 的下游：[src/profiler.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/profiler.rs) 与 [src/profiler/journal.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/profiler/journal.rs)（u7-l6 系统讲解）。
