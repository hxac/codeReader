# 输入框：Input 与 InputState 基础

## 1. 本讲目标

学完本讲，你应该能够：

- 看懂 `crates/ui/src/input/` 模块的整体结构，知道它用「一个状态引擎 + 多种输入形态」的方式组织代码。
- 解释 `InputState` 与 `Input` 为什么是**解耦**的：一个有状态、一个无状态，以及这种设计带来的好处。
- 用 `InputState::new(...)` 创建一个输入状态，用 `Input::new(&state)` 把它渲染出来。
- 监听 `InputEvent`（`Change` / `PressEnter` / `Focus` / `Blur`），实现「实时打印输入」和「回车确认」。
- 说清 `set_value` 对单行与多行输入的不同处理：单行把光标放在文本**末尾**，却把视图滚回**开头**（让超长值显示开头而非结尾）；多行则把选区重置为 `0..0`。
- 独立完成一个带清空按钮的搜索框。

本讲是后续 `u8` 文本渲染、`u9` 代码编辑器与 LSP 的入口——它们都建立在同一个 `InputState` 引擎之上。

## 2. 前置知识

阅读本讲前，请先建立以下认知（来自前置讲义）：

- **GPUI 的 Entity 与 View 模型**：跨帧持久的状态用「实体（`Entity<T>`）」承载，组件视图通过 `cx.new(|cx| ...)` 创建，状态在多帧之间保留。无状态组件则派生 `IntoElement`，每帧用 `RenderOnce` 重建，不持有跨帧状态（见 u2-l2）。
- **RenderOnce 与 Render**：无状态组件实现 `render(self, ...)`（消费 self）；有状态视图实现 `render(&mut self, ...)`。本讲的 `Input` 是前者，`InputState` 是后者。
- **主题与样式**：通过 `cx.theme()` 取语义色（见 u2-l1），通过 `Styled` / `Sizable` trait 链式设置样式与尺寸（见 u2-l2）。
- **事件订阅**：GPUI 中，要接收一个实体发出的事件，需要在创建它的地方用 `cx.subscribe(&entity, handler)`（或 `subscribe_in`）建立订阅。

一个关键直觉：在 gpui-component 里，**「视图（怎么画）」和「状态（记什么）」通常是分开的**。普通展示组件（Label、Badge）没有状态；而像输入框这种需要记住光标、选区、历史记录的组件，会把状态单独抽成一个 `Entity`。本讲正是这一模式最典型的例子。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/ui/src/input/mod.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/mod.rs) | 输入模块的「目录页」：声明所有子模块，决定哪些类型对外 `pub use` 导出。 |
| [crates/ui/src/input/state.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs) | `InputState` 的全部实现：状态字段、构造函数、builder 方法、事件、键盘动作分发。本讲最核心的文件。 |
| [crates/ui/src/input/input.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/input.rs) | `Input` 组件：绑定到 `Entity<InputState>` 的无状态视图组件，负责外观、前缀/后缀、清空按钮等。 |
| [crates/ui/src/input/element.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/element.rs) | `TextElement`：把 `InputState` 的文本、光标、滚动实际绘制到屏幕上。本讲关注它如何消费 `set_value` 的延迟滚动偏移，避免光标与文本错位闪烁。 |
| [crates/ui/src/input/mode.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/mode.rs) | `InputMode` 枚举：单行 / 多行 / 自动增长 / 代码编辑器四种模式。 |
| [crates/story/src/stories/input_story.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/story/src/stories/input_story.rs) | Story Gallery 里的输入框演示，是最好的「真实用法」参考。 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开（外加一个 `set_value` 的光标与滚动细节）：先鸟瞰整个 `input` 模块（4.1），再深入有状态引擎 `InputState`（4.2，含 `set_value` 的光标/滚动行为），最后看无状态视图 `Input`（4.3）。

### 4.1 input 模块概览：一个状态引擎，多种输入形态

#### 4.1.1 概念说明

打开 `crates/ui/src/input/` 目录，你会发现里面不只有一个「输入框」，而是一整套输入系统：普通文本框、数字输入、验证码（OTP）输入、代码编辑器、甚至内置的搜索面板。这些看起来形态各异的控件，**底层共用同一套引擎**——`InputState`。

这种设计的动机是：无论表面长什么样，输入控件要做的事情高度一致——存储文本、管理光标与选区、处理键盘/鼠标/IME 输入、维护撤销重做历史、和语法高亮/LSP 协作。把这些逻辑写一遍太浪费，于是库把它们抽成 `InputState`，而 `Input` / `NumberInput` / `OtpInput` 只是在它之上包了不同的「外壳」。

#### 4.1.2 核心流程

模块的组织方式是经典的 Rust「私有 `mod` + 选择性 `pub use` 再导出」：

```text
crates/ui/src/input/
├── mod.rs         ← 目录页：声明子模块、对外导出
├── state.rs       ← InputState（有状态引擎）★ 本讲核心
├── input.rs       ← Input（普通单行/多行文本视图）★ 本讲核心
├── element.rs     ← TextElement（文本/光标/滚动的实际绘制）★ 本讲涉及
├── mode.rs        ← InputMode（四种模式）
├── number_input.rs← NumberInput（带增减按钮的数字输入）
├── otp_input.rs   ← OtpInput / OtpState（验证码输入）
├── mask_pattern.rs← 输入掩码（如电话号码格式）
├── search.rs      ← 代码编辑器内的查找面板
├── display_map/   ← 文本坐标映射（缓冲区↔显示，含折叠）— u9 深入
├── cursor.rs / selection.rs / movement.rs ← 光标、选区、移动
├── history.rs     ← 撤销/重做
├── rope_ext.rs    ← Rope 文本结构扩展
├── lsp/           ← LSP 集成（诊断、补全、悬停、跳转）— u9 深入
├── highlighter    ←（在 highlighter 模块）Tree-sitter 语法高亮 — u9 深入
└── ...            ← blink_cursor、indent、popovers 等
```

#### 4.1.3 源码精读

[crates/ui/src/input/mod.rs:4-22](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/mod.rs#L4-L22) 把所有子模块声明为私有（`mod`，而非 `pub mod`），这意味着外部不能直接按路径访问内部文件，只能通过下面的再导出来用。

随后 [crates/ui/src/input/mod.rs:24-38](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/mod.rs#L24-L38) 用 `pub use` 把「需要给用户用的类型」重新导出到 `crate::input::` 名字空间下。注意这几行透露了模块的对外能力面：

- `pub use input::*;` —— 导出 `Input` 组件。
- `pub use state::*;` —— 导出 `InputState` 与 `InputEvent`。
- `pub use number_input::{NumberInput, NumberInputEvent, NumberStep, StepAction};` —— 数字输入。
- `pub use otp_input::*;` —— 验证码输入。
- `pub use rope_ext::{...}` 与 `pub use ropey::Rope;` —— 文本底层用的是 [ropey](https://docs.rs/ropey) 的 `Rope` 数据结构（一种适合频繁插入删除的「绳子」文本结构，本讲暂不深入，u9 会精读）。

> 💡 一句话记忆：**`input` 模块 = `InputState` 引擎 + 多种外壳（`Input` / `NumberInput` / `OtpInput`）+ 一堆支持设施**。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立「一个引擎、多种外壳」的直觉。
2. **操作步骤**：
   - 打开 `crates/ui/src/input/mod.rs`，数一下 `mod` 声明共有多少个子模块。
   - 打开 `crates/ui/src/input/number_input.rs:30-56`，观察 `NumberInput` 的字段，注意它第一个字段就是 `state: Entity<InputState>`——和 `Input` 完全一样。
   - 打开 `crates/ui/src/input/otp_input.rs:12-30`，观察 `OtpState` 内部也持有一个 `input_state: Entity<InputState>`。
3. **需要观察的现象**：三种外壳都「绑定」到同一个 `Entity<InputState>` 类型。
4. **预期结果**：你会确认「无论表面形态如何，内部都是 `InputState` 在干活」这一结论。

---

### 4.2 InputState：输入系统的有状态引擎

#### 4.2.1 概念说明

`InputState` 是输入系统的「大脑」。它是一个 GPUI 实体（`Entity<InputState>`），实现了三个关键 trait：

- `Render`：它本身也能被渲染（绘制文本、光标、占位符）。
- `Focusable`：可以被聚焦、参与 Tab 导航。
- `EventEmitter<InputEvent>`：会向外发出输入事件。

> ⚠️ 关键认知：**`InputState` 不是 `Input` 组件的一个字段，而是独立存在的实体**。你的业务 View 负责创建并持有它（`cx.new(|cx| InputState::new(...))`），然后把它**借给** `Input` 组件去画。状态归你所有，`Input` 只是一个负责「怎么画」的临时外壳。这就是本讲的标题词——**解耦（decoupling）**。

这种解耦带来三个好处：

1. **状态持久**：外壳每帧重建，但 `Entity<InputState>` 身份稳定，光标位置、撤销历史不会丢。
2. **复用引擎**：同一套 `InputState` 能驱动普通文本框、数字框、代码编辑器。
3. **双向通信**：你既能通过事件「被动接收」用户的输入，也能主动调用 `set_value` / `focus` / `clean` 来「控制」输入框。

#### 4.2.2 核心流程

一个输入框的完整数据流：

```text
① 创建   cx.new(|cx| InputState::new(window, cx).placeholder("..."))
              ↓ 返回 Entity<InputState>，由你的 View 持有
② 渲染   Input::new(&state)         ← 把实体借给外壳去画
③ 订阅   cx.subscribe_in(&state, window, handler)  ← 准备收事件
④ 输入   用户敲键 → 键盘动作（Enter/Backspace…）
              ↓ window.listener_for 路由到 InputState 的方法
⑤ 改文本  replace_text_in_range_silent → 更新内部 Rope
              ↓
⑥ 发事件  cx.emit(InputEvent::Change) → cx.notify() 触发重绘
              ↓
⑦ 收事件  你的 handler 收到 InputEvent::Change，调用 state.read(cx).value() 取最新值
```

注意第 ⑥ 步：内部文本用的是 `Rope`（ropey），对外读取时通过 `value()` 转成普通字符串 `SharedString`。

#### 4.2.3 源码精读

**（1）事件类型 `InputEvent`**

[crates/ui/src/input/state.rs:122-128](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L122-L128) 定义了 `InputState` 会发出的全部事件，只有四种：

```rust
#[derive(Clone)]
pub enum InputEvent {
    Change,
    PressEnter { secondary: bool, shift: bool },
    Focus,
    Blur,
}
```

- `Change`：文本被修改（输入、删除、粘贴、程序化 `set_value` 之外的真实编辑都会发）。
- `PressEnter`：回车。`secondary` / `shift` 区分主回车、次回车（如某些键盘的小回车）和是否按住 Shift。
- `Focus` / `Blur`：获得 / 失去焦点。

[crates/ui/src/input/state.rs:454](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L454) 的 `impl EventEmitter<InputEvent> for InputState {}` 让 `cx.emit(...)` 与 `cx.subscribe(...)` 生效。

**（2）构造函数与默认值**

[crates/ui/src/input/state.rs:460-552](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L460-L552) 是 `InputState::new`。它做了三件事：创建 `FocusHandle`（并 `tab_stop(true)` 让输入框能被 Tab 聚焦）、创建闪烁光标实体、注册若干订阅（观察光标闪烁以触发重绘、窗口激活时启停光标、`on_focus` / `on_blur` 回调）。其余字段都用合理默认值初始化，例如 `text: "".into()`、`mode: InputMode::default()`（默认单行）、`emit_events: true`。

**（3）状态字段（节选）**

`InputState` 结构体字段非常多（[crates/ui/src/input/state.rs:340-452](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L340-L452)），下表只列与本讲相关的：

| 字段 | 含义 |
| --- | --- |
| `text: Rope` | 真正的文本内容（ropey 的 Rope）。 |
| `placeholder: SharedString` | 空文本时显示的占位提示。 |
| `mode: InputMode` | 输入模式（单行/多行/自动增长/代码编辑器）。 |
| `selected_range: Selection` | 当前选区的字节区间。 |
| `masked: bool` | 是否以密码圆点形式显示（掩码）。 |
| `focus_handle: FocusHandle` | 焦点句柄，决定能否被聚焦。 |
| `scroll_handle: ScrollHandle` | 滚动句柄，记录视图当前滚动到的位置。 |
| `deferred_scroll_offset: Option<Point<Pixels>>` | 「延迟滚动偏移」：在下一帧绘制时强制覆盖滚动（4.2.6 会用到）。 |
| `history: History<Change>` | 撤销/重做历史。 |
| `emit_events: bool` | 是否对外发事件（某些内部操作会临时关闭）。 |

其中 `deferred_scroll_offset` 字段在 [crates/ui/src/input/state.rs:391-393](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L391-L393)，原本服务于 `scroll_to`，`set_value` 也复用它（见 4.2.6）。

**（4）builder 方法（链式配置）**

`InputState::new` 返回 `Self`，因此可以链式调用 builder。常用方法：

- [placeholder](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L619-L622)：设置占位符。
- [default_value](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L1154-L1165)：设置初始文本。
- [masked(true)](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L884-L888)：密码掩码（仅单行模式，`debug_assert!` 会校验）。
- [pattern(regex)](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L1016)：用正则约束可输入内容。
- [validate(closure)](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L1038)：自定义校验闭包。
- [multi_line(true)](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L557-L560)：切到多行模式。
- [code_editor(language)](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L589-L594)：切到代码编辑器模式（u9 精讲）。
- [clean_on_escape()](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L899-L903)：按 Esc 清空。

模式由 `InputMode` 枚举管理，见 [crates/ui/src/input/mode.rs:22-50](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/mode.rs#L22-L50)，分 `PlainText` / `AutoGrow` / `CodeEditor` 三种，`is_single_line()` / `is_code_editor()` 等方法用于判断（[mode.rs:103-111](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/mode.rs#L103-L111)）。

**（5）读取与控制方法**

- [value()](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L1167-L1170)：把内部 `Rope` 转成 `SharedString` 返回，这是你读取用户输入的标准入口。
- [text()](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L1183-L1186)：返回 `&Rope`，需要高性能访问原始文本时用。
- [set_value(text, window, cx)](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L778-L820)：程序化设置文本。它做两件值得记住的事：
  1. 临时把 `emit_events` 设为 `false`（[state.rs:791](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L791)），所以**程序化设值不会触发 `Change` 事件**，避免你主动设值又被自己的监听器当成用户输入处理。
  2. 按单行/多行分别处理光标与滚动——这是 4.2.6 的主题。
- [clean(window, cx)](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L1603-L1607)：清空文本（清空按钮调用它）。
- [focus(window, cx)](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L1211-L1217)：聚焦输入框并启动光标闪烁。

**（6）事件是在哪里发出的？**

文本真正被修改后，`replace_text_in_range_silent` 末尾会发 `Change`：[crates/ui/src/input/state.rs:2863-2865](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L2863-L2865)。

```rust
if self.emit_events {
    cx.emit(InputEvent::Change);
}
cx.notify();
```

回车则在 [enter()](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L1563-L1601) 里发 `PressEnter`（[state.rs:1597-1600](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L1597-L1600)）。`Focus` / `Blur` 在 [on_focus](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L2297-L2302) / [on_blur](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L2304-L2325) 中发出。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：理解「程序化设值不发事件」这一反直觉设计。
2. **操作步骤**：阅读 `set_value`（[state.rs:784-820](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L784-L820)），关注第 790-794 行对 `history.ignore` 和 `emit_events` 的临时切换。
3. **需要观察的现象**：`set_value` 在调用 `replace_text` 前关闭事件，调用后再打开。
4. **预期结果**：得出结论——「如果想让用户输入和程序设值走同一段处理逻辑，需要在监听器里区分来源；`set_value` 本身不会触发 `Change`」。这一结论在综合实践中会被验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `InputState` 要实现 `Focusable` trait？如果只实现 `Render` 不实现 `Focusable` 会怎样？

> **参考答案**：输入框必须能被键盘聚焦才能接收键盘输入。`Focusable` 提供 `focus_handle`，让 `Input` 组件能用 `.track_focus(&state.focus_handle)` 接入焦点系统，也让 `state.focus()` 能主动聚焦。不实现 `Focusable`，输入框就无法进入「可输入」状态，Tab 也无法选中它。

**练习 2**：`value()` 返回 `SharedString`，而 `text()` 返回 `&Rope`。日常读取用户输入该用哪个？为什么内部要用 `Rope`？

> **参考答案**：日常用 `value()` 即可，它把 `Rope` 转成普通字符串，使用最简单。内部用 `Rope` 是因为输入框要频繁地在中部插入/删除字符，`Rope`（ropey）是基于树的文本结构，插入删除是 \( O(\log n) \)，远优于 `String` 的 \( O(n) \) 搬移，能支撑大文件编辑（u9 会看到它支撑数十万行）。

---

### 4.3 Input：绑定到 InputState 的无状态视图组件

#### 4.3.1 概念说明

`Input` 是你真正「画」到界面上的组件。它派生 `IntoElement`、实现 `RenderOnce`，是**无状态**的——每帧由你的 View 重建。它唯一的「身份」是它绑定的那个 `Entity<InputState>`。

> 三步用法口诀：**建状态 → 借给 Input → 订阅事件**。

```rust
// ① 在你的 View 里创建并持有状态
let state = cx.new(|cx| InputState::new(window, cx).placeholder("搜索..."));
// ② 渲染时把状态借给 Input
Input::new(&state).cleanable(true).prefix(Icon::new(IconName::Search))
// ③ 订阅事件
cx.subscribe_in(&state, window, Self::on_input_event);
```

#### 4.3.2 核心流程

`Input` 的 `render` 做四件事：

1. 把外壳上的配置（`disabled`、`size`、`text_align` 等）回写到 `InputState`（因为真正绘制文本的是 `InputState` 自己）。
2. 搭建外层 `div`：背景、圆角、边框、焦点边框、内边距（都用 `cx.theme()` 取色 + `Styled` 设置）。
3. 注册大量键盘/鼠标动作，用 `window.listener_for(&self.state, InputState::xxx)` 把它们路由到 `InputState` 的方法。
4. 按顺序摆放：`prefix` → 文本区（单行直接放 `InputState` 实体；多行走 `render_editor`） → `suffix` 区（加载动画、密码切换、清空按钮、自定义 suffix）。

#### 4.3.3 源码精读

**（1）结构体与构造**

[crates/ui/src/input/input.rs:33-55](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/input.rs#L33-L55) 定义 `Input`，第一个字段就是 `state: Entity<InputState>`——印证了「外壳绑定引擎」。[Input::new](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/input.rs#L75-L95) 接收 `&Entity<InputState>` 并克隆一份持有。

**（2）常用 builder 方法**

| 方法 | 作用 | 位置 |
| --- | --- | --- |
| `.prefix(el)` / `.suffix(el)` | 输入框前/后附加元素（如搜索图标） | [input.rs:97-105](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/input.rs#L97-L105) |
| `.cleanable(true)` | 文本非空时显示清空按钮 | [input.rs:137-141](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/input.rs#L137-L141) |
| `.mask_toggle()` | 密码框加「眼睛」切换按钮 | [input.rs:144-147](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/input.rs#L144-L147) |
| `.disabled(true)` | 禁用 | [input.rs:150-153](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/input.rs#L150-L153) |
| `.appearance(false)` | 去掉边框/背景（融入其他容器） | [input.rs:120-123](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/input.rs#L120-L123) |

`Input` 还实现了 `Sizable`（[input.rs:57-62](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/input.rs#L57-L62)）和 `Styled`（[input.rs:238-242](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/input.rs#L238-L242)），所以可以用 `.small()` / `.large()` 调尺寸、用 `.w_full()` 等设样式，和库里其它组件一致（见 u2-l2）。

**（3）清空按钮的显示逻辑**

[crates/ui/src/input/input.rs:283-288](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/input.rs#L283-L288) 决定何时显示清空按钮：

```rust
let show_clear_button = self.cleanable
    && !state.disabled
    && !state.loading
    && state.text.len() > 0
    && state.mode.is_single_line();
```

即：开启了 `cleanable`、未禁用、未加载、文本非空、且是单行模式。点击时调用 `state.clean()` 并重新聚焦，见 [input.rs:427-437](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/input.rs#L427-L437)。

**（4）真实用法范例**

[crates/story/src/stories/input_story.rs:223-224](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/story/src/stories/input_story.rs#L223-L224) 是最简洁的真实示例：

```rust
.child(Input::new(&self.input1).cleanable(true))
.child(Input::new(&self.input2)),
```

而事件处理在 [input_story.rs:179-204](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/story/src/stories/input_story.rs#L179-L204)：收到 `Change` 就 `state.read(cx).value()` 取值并 `println!`，收到 `PressEnter` 打印回车信息。订阅本身在 [input_story.rs:142-146](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/story/src/stories/input_story.rs#L142-L146) 用 `cx.subscribe_in(&input1, window, Self::on_input_event)` 建立。

#### 4.3.4 代码实践（综合型，见第 5 节）

详细的搜索框实践放在第 5 节综合实践，这里先给一个最小验证：

1. **实践目标**：验证「prefix/suffix/cleanable」的视觉效果。
2. **操作步骤**：运行 Story Gallery，进入 `Input` 页面（`cargo run` 后在左侧找到 Input）。
3. **需要观察的现象**：带 `cleanable` 的输入框在有文字时右侧出现清空「×」；带 `prefix` 的输入框左侧有搜索图标。
4. **预期结果**：与 [input_story.rs:246-260](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/story/src/stories/input_story.rs#L246-L260) 的配置一一对应。

#### 4.3.5 小练习与答案

**练习 1**：`Input` 是无状态组件，那为什么它 `new` 时必须传入一个 `&Entity<InputState>`？如果两个 `Input` 绑定同一个 `InputState` 会怎样？

> **参考答案**：因为 `Input` 只负责「画」，真正的文本/光标/选区都在 `InputState` 里。`Input` 必须绑定一个状态实体才有内容可画。若两个 `Input` 绑定同一实体，它们会共享同一份文本和光标——在一处输入，另一处同步变化（这通常不是你想要的，但能证明「状态独立于视图」）。

**练习 2**：清空按钮点击后，代码除了调用 `state.clean()` 还做了什么？为什么？

> **参考答案**：见 [input.rs:427-437](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/input.rs#L427-L437)，还调用了 `state.focus(window, cx)`。因为点击按钮会让焦点转移到按钮上，输入框失焦；清空后立即把焦点抢回输入框，用户可以继续输入，体验更自然。

---

### 4.4 set_value 的光标与滚动：单行置尾、视图归位

> 这是本讲相对独立的一个最小模块，对应 PR #2510（`input: Show the start of a long value after set_value`）。它讲清一个容易踩的视觉细节，串起 `InputState`（`state.rs`）与绘制层（`element.rs`）。

#### 4.4.1 概念说明

直觉上，给输入框 `set_value` 一个超长字符串（比如一个很长的 URL），你希望它像 HTML 的 `<input>` 那样**显示值的开头**。但在旧实现里，光标被挪到末尾后，「跟随光标的滚动」会把视图也一起带到值的**末尾**，于是用户看到的是值的尾巴——既不直观，也容易让人以为值被截断了。

修复思路是**把「光标位置」和「视图滚动」解耦**：

- 单行输入：光标照旧放在文本**末尾**（`selected_range = end..end`），但额外写一个「延迟滚动偏移」`deferred_scroll_offset = (0,0)`，强制下一帧把视图拉回**开头**。
- 多行输入：直接把选区清成 `0..0`（光标在开头），无需特殊滚动。

> 关键认知：光标在哪、视图滚到哪，是**两件独立的事**。`InputState` 负责记录「想滚到哪」（`deferred_scroll_offset`），`TextElement` 负责在绘制时消费这个意图。

#### 4.4.2 核心流程

```text
set_value(long_value)
   ├─ replace_text(...)              ← 文本被替换（期间 emit_events=false）
   ├─ 单行：selected_range = end..end ← 光标在末尾
   │  多行：selected_range.clear()    ← 光标在开头 0..0
   └─ scroll_handle.set_offset((0,0))
      └─ 单行：deferred_scroll_offset = Some((0,0))  ← 标记「下一帧强制归位」
                              │
                              ↓ 下一帧 TextElement 绘制
      scroll_offset = deferred_scroll_offset          ← 文本用归位偏移绘制
      光标横向偏移也取 deferred_scroll_offset.x        ← 光标与文本同源，不闪烁
```

为什么需要「延迟一帧」？因为正常的「跟随光标的滚动」逻辑会在绘制时根据光标位置重算滚动量；只有用 `deferred_scroll_offset` 在绘制阶段**最后覆盖**它，才能保证这一帧视图停在开头。

#### 4.4.3 源码精读

**（1）`set_value` 的光标与滚动分支**

[crates/ui/src/input/state.rs:796-816](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L796-L816) 是本次更新的核心：

```rust
// Place the caret at the end for single-line inputs (like HTML
// `<input>`); multi-line inputs reset the selection to the start.
if self.mode.is_single_line() {
    let end = self.text.len();
    self.selected_range = (end..end).into();   // 光标在末尾
} else {
    self.selected_range.clear();               // 多行：选区 0..0
}
...
// Move scroll to the start. For single-line the caret is at the end, so
// override the cursor-follow scroll for the next painted frame to keep
// the start visible; the deferred offset is consumed during that paint.
self.scroll_handle.set_offset(point(px(0.), px(0.)));
if self.mode.is_single_line() {
    self.deferred_scroll_offset = Some(point(px(0.), px(0.)));  // 强制下一帧归位
}
```

注意 `deferred_scroll_offset` 字段并非这次新增——它早就存在于 [crates/ui/src/input/state.rs:391-393](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L391-L393)，原本服务于 `scroll_to`，这里只是复用它来表达「下一帧把视图拉回开头」的意图。

**（2）绘制层如何消费延迟偏移（并避免光标闪烁）**

光把文本拉回开头还不够：如果光标仍跟着「跟随光标的滚动」走，就会出现「文本在开头、光标漂在中间」的错位闪烁。所以 `TextElement` 让光标的横向偏移**也取自** `deferred_scroll_offset`：

[crates/ui/src/input/element.rs:530-540](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/element.rs#L530-L540)

```rust
// Match the caret to the deferred scroll target (applied below) that
// the text paints at; otherwise the caret follows the cursor-scroll
// while the text uses the deferred offset, flashing it mid-field.
let cursor_scroll_x = state
    .deferred_scroll_offset
    .map(|offset| offset.x)
    .unwrap_or(scroll_offset.x);

let cursor_x = bounds.left() + cursor_pos.x + line_number_width + cursor_scroll_x;
```

随后在 [crates/ui/src/input/element.rs:555-557](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/element.rs#L555-L557)，文本的滚动量也被同一个 `deferred_scroll_offset` 覆盖：

```rust
if let Some(deferred_scroll_offset) = state.deferred_scroll_offset {
    scroll_offset = deferred_scroll_offset;
}
```

两者用同一份偏移，文本与光标就不会错位。

**（3）测试守护**

这条行为有专门的测试：[test_set_value_single_line_caret_at_end_view_at_start](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L3529-L3577)（[state.rs:3533](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L3533)）。它用一个长到必然溢出的 URL 调用 `set_value`，断言：

- 调用后立即 `selected_range == Selection::new(len, len)`（光标在末尾）；
- `deferred_scroll_offset == Some(point(px(0.), px(0.)))`（视图被强制归位）；
- 绘制稳定后 `scroll_size.width > input_bounds.width`（值确实溢出，测试不空转）且 `scroll_handle.offset().x == px(0.)`（视图停在开头，显示值的开头而非尾巴）。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：建立「光标位置」与「视图滚动」解耦的直觉。
2. **操作步骤**：
   - 阅读 [set_value](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L778-L820)，对比单行分支（`selected_range = end..end` + `deferred_scroll_offset`）与多行分支（`selected_range.clear()`）。
   - 阅读 [element.rs:530-557](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/element.rs#L530-L557)，确认文本与光标用的是同一个滚动偏移。
3. **需要观察的现象**：光标 x 偏移与文本 scroll_offset 都来自 `deferred_scroll_offset`。
4. **预期结果**：能解释「为什么单行 `set_value` 后光标在末尾、视图却在开头」——因为 `deferred_scroll_offset` 在绘制阶段同时覆盖了文本滚动与光标偏移。

#### 4.4.5 小练习与答案

**练习 1**：为什么单行和多行对 `set_value` 的选区处理不同（单行置尾、多行归零）？

> **参考答案**：单行输入通常代表「一个完整值」（如 URL、搜索词），用户习惯把光标留在末尾以便继续追加，但又希望一眼看到值的开头，故用「光标置尾 + 视图归位」两段处理。多行输入更像编辑器文本，`set_value` 往往是「载入新内容」，把选区归零、光标回到开头更符合「从头开始阅读/编辑」的预期。

**练习 2**：如果不改 `element.rs`，只改 `set_value` 写 `deferred_scroll_offset`，会出现什么视觉问题？

> **参考答案**：文本会用 `deferred_scroll_offset` 归位到开头，但光标的横向位置仍按「跟随光标的滚动」算，于是光标会落在视图中间甚至偏右，与开头的文本错位闪烁。`element.rs:530-540` 让光标也取 `deferred_scroll_offset.x`，正是为了让两者同源、消除错位。

---

## 5. 综合实践

**任务**：实现一个**带清空按钮的搜索框**——监听输入值变化实时打印，回车时输出最终内容，点击清空按钮清空并保持焦点；再用一个按钮调用 `set_value` 写入一个超长字符串，验证输入框显示的是值的**开头**而非结尾。

这是把本讲四个模块串起来的最佳练习：创建 `InputState`（4.2）、绑定 `Input` 并用 `cleanable`（4.3）、订阅 `InputEvent`（4.2）、观察 `set_value` 的光标/滚动行为（4.4）。

### 5.1 实践目标

- 掌握 `InputState` 的创建、`Input` 的绑定、事件订阅的完整闭环。
- 亲手验证两条结论：① `set_value` 不触发 `Change`；② 单行 `set_value` 一个超长值后，视图显示值的开头。

### 5.2 操作步骤

以 `examples/hello_world` 为模板（见 [examples/hello_world/src/main.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/examples/hello_world/src/main.rs)），新建一个示例。下面是**示例代码**（非项目原有代码）：

```rust
// examples/search_input/src/main.rs （示例代码）
use gpui::*;
use gpui_component::{button::*, input::*, *};

pub struct SearchExample {
    state: Entity<InputState>,
    _subscriptions: Vec<Subscription>,
}

impl SearchExample {
    fn on_input_event(
        &mut self,
        _state: &Entity<InputState>,
        event: &InputEvent,
        _window: &mut Window,
        cx: &mut Context<Self>,
    ) {
        match event {
            // 实时打印输入值变化
            InputEvent::Change => {
                let text = self.state.read(cx).value();
                println!("[实时] 当前输入: {:?}", text);
            }
            // 回车输出最终内容
            InputEvent::PressEnter { .. } => {
                let text = self.state.read(cx).value();
                println!("[回车] 提交搜索: {:?}", text);
            }
            InputEvent::Focus => println!("聚焦"),
            InputEvent::Blur => println!("失焦"),
        }
    }

    /// 点击按钮：调用 set_value 写入一个超长字符串。
    fn inject_long_value(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let long = format!("https://example.com/v1/users?{}", "x=1&".repeat(120));
        self.state.update(cx, |s, cx| s.set_value(long, window, cx));
    }
}

impl Render for SearchExample {
    fn render(&mut self, window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        v_flex()
            .size_full()
            .items_center()
            .justify_center()
            .gap_2()
            .child("搜索框示例")
            .child(
                Input::new(&self.state)
                    .w(px(320.))                                       // 固定宽度，便于看到溢出
                    .cleanable(true)                                   // 清空按钮
                    .prefix(Icon::new(IconName::Search).small()),       // 前缀搜索图标
            )
            .child(
                Button::new("inject")
                    .small()
                    .outline()
                    .label("set_value 写入超长 URL")
                    .on_click(cx.listener(move |this, _, window, cx| {
                        this.inject_long_value(window, cx);
                    })),
            )
    }
}

fn main() {
    gpui_platform::application().run(move |cx| {
        // 1. 必须先 init（见 u1-l4）
        gpui_component::init(cx);

        cx.spawn(async move |cx| {
            cx.open_window(WindowOptions::default(), |window, cx| {
                let view = cx.new(|cx| {
                    // 2. 创建 InputState，设占位符；返回的实体由 View 持有
                    let state = cx.new(|cx| {
                        InputState::new(window, cx).placeholder("输入关键词后回车搜索...")
                    });
                    // 3. 订阅事件（闭包捕获 state 之前先建好订阅）
                    let _subscriptions = vec![
                        cx.subscribe_in(&state, window, SearchExample::on_input_event),
                    ];
                    SearchExample { state, _subscriptions }
                });
                // 4. 窗口第一层必须是 Root（见 u1-l4）
                cx.new(|cx| Root::new(view, window, cx).bg(cx.theme().background))
            })
            .expect("Failed to open window");
        })
        .detach();
    });
}
```

> ⚠️ 注意 `cx.new` 里闭包的写法：`state` 是先 `cx.new` 出来的 `Entity<InputState>`，再用它做 `subscribe_in`。如果顺序写反（先 subscribe 再有 state），会编译不过。

### 5.3 需要观察的现象

1. 输入 `hello`，控制台应逐字符打印 5 次 `[实时] 当前输入: "h"` … `"hello"`。
2. 按回车，打印一次 `[回车] 提交搜索: "hello"`。
3. 文本非空时，输入框右侧出现清空按钮；点击后文本清空、焦点留在输入框，并打印一次 `[实时] 当前输入: ""`（因为 `clean()` 走的是真实编辑路径，会发 `Change`）。
4. 点击「set_value 写入超长 URL」按钮：输入框被填入一个很长的值，**可见区显示的是它的开头** `https://example.com/v1/users?...`，而不是它的尾巴（这就是 4.4 讲的「视图归位」）。此时把光标用方向键移到末尾，可以确认光标其实停在值的**末尾**。

### 5.4 预期结果与验证点

- **验证「程序化设值不发事件」**：点击注入按钮后，文本被改成了超长 URL，但**控制台并没有打印 `[实时]`**——因为 `set_value` 内部关闭了 `emit_events`（见 4.2.3、4.2.4）。这与「用户输入/清空按钮」会触发 `Change` 形成鲜明对比。
- **验证「视图显示值的开头」**：注入后输入框左端可见 `https://example.com/...`；这与 [test_set_value_single_line_caret_at_end_view_at_start](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/state.rs#L3533) 中「`scroll_handle.offset().x == px(0.)`（long value should display from its start, not its tail）」的断言一致。
- 若某一步行为与预期不符，对照 [input_story.rs:179-204](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/story/src/stories/input_story.rs#L179-L204) 检查事件处理写法。

> 编译运行命令（参考 u1-l2 的 examples 运行方式）：`cargo run -p search_input`（需先在 workspace 根 `Cargo.toml` 的 `members` 加入该示例目录）。运行结果受本地环境影响，若图标不显示请确认已按 u2-l3 提供图标资源。

---

## 6. 本讲小结

- `crates/ui/src/input/` 是一整套输入系统，用「**一个 `InputState` 引擎 + 多种外壳**（`Input` / `NumberInput` / `OtpInput`）」组织，内部文本用 ropey 的 `Rope` 存储。
- `InputState` 是有状态的实体，实现 `Render` + `Focusable` + `EventEmitter<InputEvent>`，承载文本、光标、选区、模式、历史、掩码、滚动等全部状态，由你的业务 View 持有。
- `Input` 是绑定到 `Entity<InputState>` 的**无状态** `RenderOnce` 组件，只负责外观与交互外壳，这正是「视图与状态解耦」的核心。
- 四种事件：`Change`（文本变）、`PressEnter`（回车）、`Focus`、`Blur`；用 `cx.subscribe_in(&state, window, handler)` 接收，用 `state.read(cx).value()` 取值。
- 读取用 `value()`，主动控制用 `set_value()` / `clean()` / `focus()`；注意 `set_value` **不会**触发 `Change` 事件。
- `set_value` 把「光标位置」与「视图滚动」解耦：单行光标置尾、却用 `deferred_scroll_offset` 把视图拉回开头（显示长值开头）；多行选区归零。`element.rs` 让光标偏移与文本滚动共用同一份偏移，避免错位闪烁。
- `Input` 通过 `.cleanable(true)` / `.prefix()` / `.suffix()` / `.mask_toggle()` 等链式方法配置，并实现 `Sizable` / `Styled`，与库里其它组件用法一致。

## 7. 下一步学习建议

- **横向扩展输入形态**：阅读 [number_input.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/number_input.rs) 与 [otp_input.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/input/otp_input.rs)，体会它们如何复用 `InputState`，并尝试用 `NumberInput` 做一个带步进的数字输入。
- **纵向深入引擎**：本讲的 `InputState` 是 u8（文本渲染）和 u9（代码编辑器 + Tree-sitter + LSP）的地基。下一阶段建议先学 u8 的 `TextView`，再进入 u9 精读 `Rope` / `DisplayMap` / `cursor` / `selection` 这些本讲只点到为止的内部机制。`deferred_scroll_offset` 与 `scroll_to`、`DisplayMap` 的坐标映射也将在 u9 展开。
- **配合表单**：学完本讲后，结合 u4-l1 的 `Form` / `Field`，把输入框组织成带校验反馈的表单（`Field` 的错误提示槽位正好可以接 `InputState` 的 `validate` 结果）。
