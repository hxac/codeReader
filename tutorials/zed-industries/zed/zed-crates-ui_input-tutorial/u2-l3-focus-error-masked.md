# 交互状态：焦点、Tab 顺序、错误提示与 masked 切换

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `Focusable` 委托与 `track_focus` 如何让 InputField 的「外壳容器」和「内芯 editor」共享同一个焦点身份。
2. 说明 `tab_index` 与 `tab_stop` 两个构造期配置在 render 时如何组合出三种焦点行为分支。
3. 掌握 `set_error` 的完整链路：运行期写入错误字段 → `cx.notify()` → 下一帧渲染红色边框与错误文案。
4. 读懂 masked 眼睛按钮中 `cx.listener` 闭包如何翻转外壳状态、同步内芯并请求重渲染。
5. 理解「哪些状态变化需要手动 `cx.notify()`、哪些不需要」这条 GPUI 声明式渲染的关键纪律。

本讲是第二单元的收尾：u2-l1 讲了数据模型，u2-l2 讲了渲染布局，本讲把两者串起来，专讲**会随用户交互而变化的状态**。

## 2. 前置知识

### 2.1 焦点（focus）与 FocusHandle

在 GUI 里，「焦点」回答一个问题：**接下来的键盘事件发给谁？** 同一时刻一个窗口只有一个焦点元素。GPUI 用 `FocusHandle` 表示焦点身份——它是一个可克隆的句柄，指向全局焦点表中的一个条目。谁拿到这个句柄，谁就能：

- 调用 `focus(window, cx)` 把焦点抢过来；
- 调用 `is_focused(window)` / `contains_focused(window, cx)` 查询焦点状态；
- 在句柄上配置 `tab_index` / `tab_stop`，决定它在 Tab 导航顺序中的位置。

### 2.2 外壳与内芯（承接 u2-l1）

InputField 是「外壳＋内芯」结构：外壳（`InputField` 结构体的 `label`、`error`、`masked` 等字段）只存配置与展示状态；内芯是 `editor: Arc<dyn ErasedEditor>`，真正的文本编辑、光标、焦点都发生在内芯背后那个 `Entity<Editor>` 里。本讲的很多细节都在回答同一个问题：**外壳如何把焦点、错误、遮蔽这些交互状态传递给内芯，又如何把内芯的状态变化反映回 UI？**

### 2.3 Option 的三态语义（承接 u2-l1）

`Option<bool>` 类型的 `masked` 字段有三个状态：

| 取值 | 含义 | UI 表现 |
| --- | --- | --- |
| `None` | 非敏感字段 | 不渲染眼睛按钮 |
| `Some(true)` | 敏感字段，初始遮蔽 | 内容显示为圆点，按钮显示 Eye 图标 |
| `Some(false)` | 敏感字段，初始明文 | 内容明文显示，按钮显示 EyeOff 图标 |

### 2.4 声明式渲染与 cx.notify（承接 u2-l2）

GPUI 的渲染是声明式的：`render` 方法描述「当前状态下 UI 长什么样」，状态变化后必须调用 `cx.notify()` 标记实体为脏，框架才会调度下一次 `render`。**改了字段却忘记 notify，是 GPUI 开发中最常见的「UI 不更新」bug**——本讲 4.4 节会精确划出这条红线。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/ui_input/src/input_field.rs` | 本讲主战场：`Focusable` 委托、tab 配置、`set_error`、masked 按钮、`cx.notify` 全在这里 |
| `crates/ui_input/src/ui_input.rs` | crate 根：`ErasedEditor` trait 声明了本讲用到的 `focus_handle`、`set_masked` 等方法 |
| `crates/gpui/src/window.rs` | `FocusHandle` 的定义、`tab_index`/`tab_stop`/`contains_focused` 方法、`focus_next` |
| `crates/gpui/src/elements/div.rs` | `track_focus` 的实现，以及焦点句柄如何被注册进渲染帧 |
| `crates/gpui/src/tab_stop.rs` | `TabStopMap`：Tab 导航顺序的数据结构与跳过逻辑 |
| `crates/gpui/src/app/context.rs` | `cx.listener` 的实现（弱引用 + 实体更新） |
| `crates/gpui/src/app.rs` | `App::notify` 的底层实现 |
| `crates/ui/src/components/button/icon_button.rs` | `IconButton` 的 `on_click` 签名（`Clickable` trait） |
| `crates/editor/src/editor.rs` | `ErasedEditorImpl`：trait 方法到真实 `Editor` 的委托 |
| `crates/settings_ui/src/pages/skill_creator.rs` | 真实消费者：`set_error` 驱动的表单校验 |

## 4. 核心概念与源码讲解

### 4.1 track_focus 与 Tab 配置逻辑

#### 4.1.1 概念说明

InputField 自己不处理按键、不显示光标——这些是内芯 editor 的事。但用户期望的体验是：**点击输入框的任何位置（包括边框、图标区域）都能聚焦进编辑器，且聚焦后整个输入框高亮**。

GPUI 的解法很优雅：让外壳容器和内芯编辑器**共享同一个 `FocusHandle`**。焦点身份只有一个，谁持有句柄谁就能查询和操控它：

- `Focusable` 委托（input_field.rs L49-53）：外部对 `Entity<InputField>` 调 `focus_handle(cx)` 时，拿到的其实是内芯 editor 的句柄。这样外部可以用统一的方式聚焦任何可聚焦组件。
- `track_focus`（render 中）：外壳的 `h_flex` 容器声明「我追踪这个句柄的焦点状态」。焦点落在该句柄上时，容器应用焦点样式（u2-l2 讲过的 `border_focused` 边框就是这样生效的）。

而 `tab_index` / `tab_stop` 是**构造期配置**（builder 方法），存放在外壳字段里，等到每次 render 时才应用到焦点句柄上——这就是「配置」与「焦点身份」的分离。

#### 4.1.2 核心流程

Tab 键聚焦一个 InputField 的完整链路：

```text
构建期:  InputField::new(...)                     → tab_index = None, tab_stop = true（默认）
配置期:  .tab_index(0) / .tab_stop(false)          → 写入外壳字段
渲染期:  render()
           ├─ focus_handle = editor.focus_handle(cx)         取内芯句柄
           ├─ configured_handle = 按 tab_index/tab_stop 三分支改造句柄
           ├─ h_flex().track_focus(&configured_handle)        容器追踪该句柄
           │    ├─ 布局期: window.set_focus_handle(...)       句柄注册进 dispatch tree
           │    └─ 绘制期: window.next_frame.tab_stops.insert(handle)  句柄注册进 Tab 顺序表
           └─ （焦点态）contains_focused(...) == true → 边框换成 border_focused
运行期:  用户按 Tab
           └─ window.focus_next(cx)
                └─ rendered_frame.tab_stops.next(...)
                     └─ 跳过 tab_stop == false 的节点，选中下一个 → window.focus(handle)
```

三分支逻辑（对应源码 L167-173）：

| 分支 | 触发条件 | 对句柄的操作 | 效果 |
| --- | --- | --- | --- |
| ① 显式序号 | `tab_index = Some(i)` | `.tab_index(i).tab_stop(self.tab_stop)` | 进入 Tab 顺序，序号为 `i`；`tab_stop` 默认 `true` |
| ② 显式排除 | `tab_index = None` 且 `tab_stop = false` | `.tab_stop(false)` | 从 Tab 顺序中排除（点击、编程聚焦仍有效） |
| ③ 默认 | `tab_index = None` 且 `tab_stop = true` | 原样返回 | 不修改句柄，沿用句柄自身的注册状态（见 4.1.3 的源码分析） |

#### 4.1.3 源码精读

**第一步：Focusable 委托。** [src/input_field.rs:L49-L53](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L49-L53) 实现了 `Focusable` trait，把 `focus_handle` 直接委托给内芯 editor——这就是「共享焦点身份」的起点。注意 `ErasedEditor` trait 恰好暴露了 `focus_handle` 方法（[src/ui_input.rs:L27](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/ui_input.rs#L27)），类型擦除没有挡住焦点体系。

**第二步：两个配置字段与 builder。** [src/input_field.rs:L38-L41](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L38-L41) 声明了 `tab_index: Option<isize>`（三态：`None` 表示未配置）和 `tab_stop: bool`。对应的 builder 方法在 [src/input_field.rs:L97-L105](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L97-L105)：`tab_index` 写入 `Some(index)`，`tab_stop` 直接覆写布尔值。

**第三步：render 中的三分支。** [src/input_field.rs:L167-L173](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L167-L173) 是本模块的核心——根据两个字段组合出 `configured_handle`。注意这两个方法调用发生在 `FocusHandle` 上（不是元素上）：

```rust
let configured_handle = if let Some(tab_index) = self.tab_index {
    focus_handle.tab_index(tab_index).tab_stop(self.tab_stop)
} else if !self.tab_stop {
    focus_handle.tab_stop(false)
} else {
    focus_handle
};
```

**第四步：FocusHandle 上的两个 builder。** [crates/gpui/src/window.rs:L571-L589](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L571-L589) 是 `FocusHandle::tab_index` 与 `FocusHandle::tab_stop` 的实现。它们是 `mut self -> Self` 的链式方法，且除了改自身字段，还会写入全局焦点表（`self.handles.write().get_mut(self.id)`）里的 `FocusRef` 条目——**因此任何持有该句柄克隆的代码都能看到修改**。`FocusHandle` 的字段定义见 [crates/gpui/src/window.rs:L525-L533](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L525-L533)。

> 顺带区分：`div.rs` 里还有一套**元素级**的 `.tab_index()` / `.tab_stop()`（[crates/gpui/src/elements/div.rs:L758-L778](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/elements/div.rs#L758-L778)），作用在元素的 interactivity 上。InputField 没有用它们，而是直接在句柄上配置后交给 `track_focus`——效果是把配置「写进焦点身份本身」。

**第五步：track_focus 让容器追踪句柄。** [src/input_field.rs:L184](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L184) 在 `h_flex` 上调用 `.track_focus(&configured_handle)`。其实现见 [crates/gpui/src/elements/div.rs:L749-L756](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/elements/div.rs#L749-L756)：把 `interactivity.focusable` 置真、把句柄存进 `tracked_focus_handle`。由于句柄已被显式设置，div.rs 中「可聚焦元素自动配置 tab 属性」的分支（[crates/gpui/src/elements/div.rs:L2145-L2160](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/elements/div.rs#L2145-L2160)，条件含 `tracked_focus_handle.is_none()`）会被跳过——InputField 在句柄上做的配置就是最终配置。

**第六步：句柄进入渲染帧。** 布局期 [crates/gpui/src/elements/div.rs:L2216-L2217](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/elements/div.rs#L2216-L2217) 调用 `window.set_focus_handle` 注册焦点 id；绘制期 [crates/gpui/src/elements/div.rs:L2437-L2439](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/elements/div.rs#L2437-L2439) 把句柄插入 `next_frame.tab_stops`。`TabStopMap::insert`（[crates/gpui/src/tab_stop.rs:L77-L90](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/tab_stop.rs#L77-L90)）会记下句柄当前的 `tab_index`（参与排序的路径）和 `tab_stop` 标志。

**第七步：Tab 键的旅程。** [crates/gpui/src/window.rs:L2089-L2098](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L2089-L2098) 的 `focus_next` 从 `rendered_frame.tab_stops` 取下一个停靠点；`TabStopMap::next`（[crates/gpui/src/tab_stop.rs:L111-L117](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/tab_stop.rs#L111-L117)）在遍历时跳过 `tab_stop == false` 的节点——这就是「分支②被 Tab 跳过」的最终落点。

**第八步：焦点边框的判定。** [src/input_field.rs:L196-L199](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L196-L199) 用的是 `contains_focused` 而不是 `is_focused`。两者的差别见 [crates/gpui/src/window.rs:L604-L613](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L604-L613)：`is_focused` 要求句柄本身正是焦点，`contains_focused` 则允许焦点位于句柄的子树内——编辑器内部若有更细的焦点节点，容器边框依然高亮。

**一个值得深挖的细节：默认分支③到底可不可以被 Tab 聚焦？** `FocusHandle::new` 的默认值是 `tab_index: 0, tab_stop: false`（[crates/gpui/src/window.rs:L541-L555](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L541-L555)）。而 editor 创建焦点句柄用的是 `cx.focus_handle()`（[crates/editor/src/editor.rs:L2220](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/editor/src/editor.rs#L2220)），等价于 `FocusHandle::new`；在整个 editor crate 中检索不到任何 `.tab_stop(...)` 调用。因此按源码推断：**默认配置（分支③）的 InputField 注册进 Tab 表时 `tab_stop` 为 `false`，会被 Tab 导航跳过，但仍可通过鼠标点击或编程调用 `focus()` 聚焦**。外壳字段的默认 `tab_stop: true` 只在分支①②中实际生效。这一推断与字段文档注释「can be focused via Tab key」的直觉相悖，具体运行表现待本地验证（见练习 3）。

#### 4.1.4 代码实践

源码阅读型实践：跟踪一条 Tab 键调用链。

1. **实践目标**：把 4.1.2 的流程图与真实源码一一对应，验证「配置 → 句柄 → Tab 表 → 导航」四步的每一跳。
2. **操作步骤**：
   - 打开 [src/input_field.rs:L167-L184](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L167-L184)，确认三分支代码与 `track_focus` 调用；
   - 跳转到 `FocusHandle::tab_index` / `tab_stop`（window.rs L571-L589），注意它们如何写入全局焦点表；
   - 跳转到 `TabStopMap::insert`（tab_stop.rs L77-L90），找到 `tab_stop: focus_handle.tab_stop` 这一行；
   - 最后看 `focus_next`（window.rs L2089-L2098）。
3. **需要观察的现象**：纯阅读，无运行现象。重点观察「句柄的 `tab_stop` 标志」这一个布尔值如何跨越三层结构（InputField 字段 → FocusHandle 字段 → TabStopNode 字段）传递。
4. **预期结果**：你能在纸上画出「按下 Tab → `focus_next` → `tab_stops.next` 跳过 `tab_stop == false` → `window.focus(handle)` → 容器 `contains_focused` 变真 → 边框高亮」的完整时序。

#### 4.1.5 小练习与答案

**练习 1**：为什么 InputField 的 `h_flex` 容器要 `track_focus(&configured_handle)`，而不是让内芯 editor 自己处理焦点样式？

**答案**：输入框的边框、背景、圆角都是**外壳**的样式（u2-l2 讲过 `InputFieldStyle`）。内芯 editor 只负责文本区域。若容器不追踪焦点，聚焦后边框不会高亮——除非把边框样式搬进 editor，那会破坏「外壳管装饰、内芯管编辑」的分层。`track_focus` 让容器监听（并注册）同一个焦点句柄，装饰与编辑共享一个焦点身份。

**练习 2**：`FocusHandle::tab_index` 为什么除了改自身还要写全局焦点表（`FocusMap`）里的条目？

**答案**：`FocusHandle` 是可克隆的值类型，克隆体各自持有 `tab_index`/`tab_stop` 字段的副本。若只改自身，其他克隆（比如 editor 内部持有的那份）看到的仍是旧值。写入全局 `FocusMap` 的 `FocusRef` 条目后，任何通过 `for_id` 重建的句柄（见 window.rs L557-L569）都会取到最新配置——这保证「配置写进焦点身份本身」的语义。

**练习 3**：默认配置（不调用 `tab_index` 也不调用 `tab_stop`）的 InputField，按 Tab 能否聚焦它？请给出源码依据。

**答案**：按源码推断**不能**。依据链：分支③原样返回句柄（input_field.rs L167-173）→ `FocusHandle::new` 默认 `tab_stop: false`（window.rs L541-555）→ editor 用 `cx.focus_handle()` 创建句柄（editor.rs L2220）且 editor crate 中无 `.tab_stop(true)` 调用 → `TabStopMap::next` 跳过 `tab_stop == false` 的节点（tab_stop.rs L111-L117）。若要可 Tab 聚焦，应显式调用 `.tab_index(0)`。此结论待本地验证——验证方法见本讲综合实践。

### 4.2 set_error 与错误样式

#### 4.2.1 概念说明

表单组件的核心职责之一是**校验反馈**：用户提交了不合法的内容，输入框要变红并说明原因。`error` 是 InputField 中唯一一个**运行期可变**的展示字段——label、placeholder、图标等都是构造期定死的配置，而错误会随着用户输入随时出现和消失。

这带来一个与 builder 方法截然不同的 API 形态：

- builder（如 `label`）：`mut self -> Self`，构造期一次性消费；
- `set_error`：`&mut self + &mut Context<Self>`，运行期在既有实体上调用，且**必须配合 `cx.notify()`** 才能反映到 UI。

#### 4.2.2 核心流程

```text
校验方（如 skill_creator 页面）
  └─ field.update(cx, |field, cx| field.set_error(Some("必填项"), cx))
       ├─ self.error = Some("必填项")     写外壳字段
       ├─ cx.notify()                     标记实体脏 → 调度重渲染
       └─ （返回）
下一帧 render()
  ├─ has_error = self.error.is_some()
  ├─ error_border = cx.theme().status().error_border     从主题取错误色
  ├─ .when(聚焦,  border_color = border_focused)          先写焦点边框
  ├─ .when(has_error, border_color = error_border)        后写错误边框 → 覆盖
  └─ .when_some(self.error, |this, error|                 输入框下方渲染
        this.child(Label::new(error).size(Small).color(Error)))   红色小号文案
```

#### 4.2.3 源码精读

**字段与文档。** [src/input_field.rs:L44-L46](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L44-L46) 声明 `error: Option<SharedString>`，文档注释明确说了两件事：置位后边框变红、消息显示为字段下方的提示小字；传 `None` 清除。

**set_error 方法。** [src/input_field.rs:L113-L119](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L113-L119) 只有三行：`error.map(Into::into)` 完成字符串到 `SharedString` 的转换，然后 `cx.notify()`。签名 `Option<impl Into<SharedString>>` 让调用方既能传 `&str` 也能传 `String`。

**render 侧的三段消费。** [src/input_field.rs:L164-L165](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L164-L165) 预先缓存 `has_error` 布尔值和主题错误色（注意错误色来自 `cx.theme().status()`，与普通边框色的取色区域不同）。[src/input_field.rs:L196-L200](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L196-L200) 中两个 `.when` 的**书写顺序**是关键：焦点边框先写、错误边框后写。承接 u2-l2 的结论——fluent 链式样式后写覆盖先写——所以**错误优先于焦点**：一个既聚焦又出错的字段显示红色边框，而不是主题的聚焦高亮。最后 [src/input_field.rs:L231-L233](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L231-L233) 用 `.when_some` 在 `v_flex` 尾部追加一个 `LabelSize::Small` + `Color::Error` 的标签——这就是「输入框下方的红色提示文案」。

**真实消费者。** Zed 的 skill 创建页是 `set_error` 的标准用法：[crates/settings_ui/src/pages/skill_creator.rs:L395-L401](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/settings_ui/src/pages/skill_creator.rs#L395-L401) 的 `recompute_name_error` 先用 `validate_name` 校验文本，再把结果通过 `field.update(cx, |field, cx| field.set_error(error, cx))` 写回字段；`recompute_description_error`（L403-L410）同构。注意 `error` 是 `Result` 的 `.err()`——校验通过时是 `None`，正好复用「传 `None` 清除」的语义。

#### 4.2.4 代码实践

修改 preview，给组件预览加一个「出错状态」的示例（本地练习性修改，随时可用 `git checkout crates/ui_input/` 恢复）：

1. **实践目标**：亲眼看到 `set_error(Some(...))` 的红色边框与提示文案。
2. **操作步骤**：
   - 打开 [src/input_field.rs:L248-L271](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L248-L271)，在 `preview` 中仿照 `input_small` 新增一个实体（示例代码）：

     ```rust
     let input_error = cx.new(|cx| {
         let mut field = InputField::new(window, cx, "placeholder").label("With Error");
         field.set_error(Some("This field is required."), cx);
         field
     });
     ```

     并把它加入 `example_group` 的 `single_example` 列表；
   - 在 Zed 仓库根目录执行 `cargo run`（首次构建较久），启动后打开命令面板执行 `workspace: open component preview`，在 Forms & Input 分区找到 InputField。
3. **需要观察的现象**：新示例的输入框边框为红色，且输入框下方有一行红色小字「This field is required.」；其他两个示例不受影响。
4. **预期结果**：`set_error` 在 `cx.new` 闭包内即可调用（闭包参数正是 `&mut Context<InputField>`），红色边框与文案在同一帧渲染中出现。构建与界面入口的具体表现待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：一个字段当前既被聚焦又有错误，边框是什么颜色？如果调换 L196-L200 中两个 `.when` 的顺序，结果会变吗？

**答案**：红色（`error_border`）。会变——调换后焦点边框后写，将覆盖错误边框，出错的聚焦字段显示聚焦高亮，错误被视觉掩盖。这正是「后写覆盖先写」规则的实际意义：书写顺序就是一种优先级声明。

**练习 2**：为什么 `set_error` 需要 `cx: &mut Context<Self>` 参数，而 builder 方法 `label` 不需要？

**答案**：builder 在构造期消费 `self`，组件还未成为实体，不存在「重渲染」的概念；`set_error` 修改的是**已经渲染过的实体**的状态，必须通过 `Context` 调用 `cx.notify()` 告诉框架「这个实体的 render 结果过期了，需要重画」。没有这一步，字段值变了但界面不动。

**练习 3**：`skill_creator.rs` 中校验通过时为什么不需要一个单独的 `clear_error` 方法？

**答案**：因为 `set_error` 的参数是 `Option`，校验通过时 `validate_name(&name).err()` 返回 `None`，调用 `set_error(None, cx)` 即完成清除。一个方法同时承担「设置」与「清除」，`Option` 的 `None` 就是清除语义（与 `masked`、`tab_index` 的三态设计一脉相承）。

### 4.3 masked 切换按钮与 cx.listener

#### 4.3.1 概念说明

密码、API Key 这类敏感字段需要「遮蔽显示」（内容显示为圆点），同时给用户一个眼睛按钮临时切换明文。这个小小的按钮浓缩了三个 GPUI 惯用法：

1. **状态放在外壳**：`masked: Option<bool>` 是外壳字段（单一事实来源），内芯 editor 的遮蔽状态由外壳同步过去；
2. **`cx.listener` 桥接两个世界**：元素的 `on_click` 回调签名是 `Fn(&ClickEvent, &mut Window, &mut App)`——只有 `&mut App`，摸不到 `&mut InputField`。`cx.listener` 把「更新自身实体」的闭包包装成这个签名，中间用**弱引用**安全地找回实体；
3. **图标与提示语随状态翻转**：遮蔽时显示 Eye 图标、tooltip 为 "Show"（点击后将显示明文）；明文时显示 EyeOff、tooltip 为 "Hide"。

#### 4.3.2 核心流程

```text
render():
  if let Some(masked) = self.masked { editor.set_masked(masked, ...) }   每帧把外壳状态同步进内芯
  ...
  .when_some(self.masked, |this, is_masked| this.child(
      IconButton::new("toggle-masked", if is_masked { Eye } else { EyeOff })
        .tooltip(Tooltip::text(if is_masked { "Show" } else { "Hide" }))
        .on_click(cx.listener(|this, _, window, cx| { ... }))))

用户点击眼睛按钮:
  on_click 回调（&mut App 上下文）
    └─ cx.listener 包装层: weak_entity.update(cx, |this, cx| 闭包体)
         ├─ if let Some(ref mut masked) = this.masked
         │    ├─ *masked = !*masked                       翻转外壳状态
         │    ├─ this.editor.set_masked(*masked, window, cx)  同步内芯
         │    └─ cx.notify()                              请求重渲染（图标/tooltip 换新）
         └─ （若实体已释放，update 返回 Err，.ok() 静默跳过）
```

#### 4.3.3 源码精读

**状态初始化的时机很讲究。** [src/input_field.rs:L150-L152](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L150-L152) 位于 `render` 的开头：只要 `masked` 是 `Some`，**每一次渲染**都会把该值推给内芯。这行代码是「外壳为单一事实来源」的保障——即使某次状态同步丢失，下一次任何原因触发的重渲染都会把内芯纠正回来（自愈）。注意 `masked` 为 `None` 时什么都不做，内芯保持默认的非遮蔽状态。

**按钮的构造。** [src/input_field.rs:L206-L218](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L206-L218) 用 `.when_some(self.masked, ...)` 条件装配 `IconButton`：图标在 `Eye`/`EyeOff` 间二选一，`icon_size(IconSize::Small)`、`icon_color(Color::Muted)` 保持低调，`Tooltip::text(...)` 提供悬停提示。整个按钮挂在 `h_flex` 的**最后一个 child** 位置（排在 editor 之后），所以显示在输入框右侧。

**listener 闭包逐行读。** [src/input_field.rs:L219-L227](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L219-L227) 是点击处理的核心：

```rust
.on_click(cx.listener(
    |this, _, window, cx| {
        if let Some(ref mut masked) = this.masked {
            *masked = !*masked;
            this.editor.set_masked(*masked, window, cx);
            cx.notify();
        }
    },
))
```

四个参数各有来历：`this: &mut InputField`（listener 帮你找回的实体引用）、`_` 是被忽略的 `ClickEvent`、`window` 和 `cx: &mut Context<InputField>`。闭包体三步：翻转外壳状态 → 同步内芯 → 请求重渲染。外层的 `if let Some(...)` 守卫意味着 `masked == None`（非敏感字段）时按钮根本不存在，这段防御只是双保险。

**cx.listener 的实现。** [crates/gpui/src/app/context.rs:L252-L260](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/context.rs#L252-L260) 展开了魔法：`self.entity().downgrade()` 拿到弱引用，返回的闭包在被调用时执行 `view.update(cx, |view, cx| f(view, e, window, cx)).ok()`。两个要点：其一，**弱引用**意味着若 `InputField` 实体已被释放，`update` 返回 `Err`，`.ok()` 把错误静默吞掉——回调安全地成为空操作，不会 panic、也不会延长实体生命周期；其二，闭包通过 `update` 借到 `&mut InputField` 和 `&mut Context<InputField>`，这正是闭包体能同时改状态、调 `set_masked`、发 `notify` 的原因。

**on_click 的原始签名。** [crates/ui/src/components/button/icon_button.rs:L162-L169](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui/src/components/button/icon_button.rs#L162-L169) 是 `Clickable` trait 的 `on_click`，参数类型 `Fn(&gpui::ClickEvent, &mut Window, &mut App)`——对照之后就能理解为什么必须借助 `listener` 做适配。

**set_masked 穿过类型擦除。** trait 侧的声明在 [src/ui_input.rs:L23](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/ui_input.rs#L23)。实现侧：[crates/editor/src/editor.rs:L12073-L12075](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/editor/src/editor.rs#L12073-L12075) 的 `ErasedEditorImpl(Entity<Editor>)` 是 editor crate 提供的实现体，其 `set_masked`（[crates/editor/src/editor.rs:L12177-L12181](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/editor/src/editor.rs#L12177-L12181)）把调用委托给真实 `Editor::set_masked`（[crates/editor/src/editor.rs:L8719](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/editor/src/editor.rs#L8719)）——遮蔽的真正渲染发生在 editor 内部，本讲不展开（u3-l1 会系统讲 trait 与实现）。

#### 4.3.4 代码实践

继续在 preview 里加示例（接 4.2.4 的本地修改）：

1. **实践目标**：观察眼睛按钮的初始状态、点击翻转、tooltip 变化。
2. **操作步骤**：在 `preview` 中再新增两个实体（示例代码）：

   ```rust
   let input_masked = cx.new(|cx| {
       InputField::new(window, cx, "secret").label("Masked").masked(true)
   });
   let input_visible = cx.new(|cx| {
       InputField::new(window, cx, "secret").label("Initially Visible").masked(false)
   });
   ```

   加入 `single_example` 列表后重新 `cargo run`，打开组件预览。在 Masked 示例中输入几个字符，点击眼睛按钮若干次。
3. **需要观察的现象**：输入的字符先显示为圆点；点击眼睛后变明文，按钮图标从 Eye 变为 EyeOff，悬停提示从 "Show" 变为 "Hide"；再点一次回到遮蔽。Initially Visible 示例初始就是明文，但同样有按钮。
4. **预期结果**：图标、tooltip、内容遮蔽三者始终同步翻转——因为它们都由同一个 `masked` 字段在下一帧渲染中统一决定。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `IconButton::new` 的第一个参数（元素 id）从 `"toggle-masked"` 改成 `0`（整数 id），功能还正常吗？两种写法有什么差别？

**答案**：功能正常。第一个参数是 `impl Into<ElementId>`，字符串和整数都能构成元素 id；id 的作用是给这个有状态元素（按钮有悬停、按下等状态）一个稳定的身份，让框架在前后帧之间匹配状态。差别只在可读性与调试时的辨识度，语义上等价。

**练习 2**：`cx.listener` 的闭包里为什么用弱引用而不是直接捕获 `Entity<InputField>` 的强引用？

**答案**：强引用会形成「实体 → 渲染出的元素回调 → 实体」的引用环，实体永远不被释放（内存泄漏）。弱引用不增加引用计数，实体释放后回调自动失效（`update` 返回 `Err` 被 `.ok()` 吞掉）。这与 CLAUDE.md 中「用 `WeakEntity` 避免相互递归句柄导致实体永不释放」的指导一致。

**练习 3**：`masked(true)` 之后，用户点眼睛按钮切换到明文，然后组件因为其他原因（比如兄弟组件更新触发的整棵树重渲染）又渲染了一次——内容会变回圆点吗？

**答案**：不会。因为点击时闭包翻转的是**外壳字段** `this.masked`（`Some(false)`），而 L150-L152 每帧同步的是外壳字段的当前值。用户点击后外壳状态已是 `false`，重渲染同步给内芯的也是 `false`。这就是「外壳是单一事实来源」的价值：内芯的遮蔽状态只是外壳状态的投影，不会被意外重置。

### 4.4 cx.notify 状态刷新

#### 4.4.1 概念说明

GPUI 不会自动追踪「哪些字段影响了渲染」。`render` 只在两种情况下被调用：实体被 notify，或框架认为需要重画。因此每一个修改外壳状态的地方，都必须问一句：**要不要 `cx.notify()`？**

InputField 给出了一个精确的答案，可以概括成一条规则：

> **改内芯实体（editor）的状态，不需要外壳 notify——内芯自己会通知；改外壳字段（error、masked），必须 `cx.notify()`。**

#### 4.4.2 核心流程

```text
修改内芯:  field.set_text("hi", window, cx)      委托 editor.set_text
             └─ editor 实体内部自行 notify        → editor 重渲染（它自己是独立实体）
             └─ 外壳无需任何动作                  → 外壳的 label/边框等不受影响，无需重画

修改外壳:  field.set_error(Some("..."), cx)
             └─ self.error = ...                  字段变了
             └─ cx.notify()                       → InputField 实体标记脏 → 下一帧 render
                                                  → 红边框 + 错误文案出现
```

#### 4.4.3 源码精读

**两处 notify。** 整个 input_field.rs 中 `cx.notify()` 只出现两次：`set_error` 内的 [src/input_field.rs:L118](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L118) 和眼睛按钮 listener 闭包内的 [src/input_field.rs:L224](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L224)。两个方法恰好就是本讲的两个主角（错误、遮蔽），并非巧合——**它们是仅有的两个运行期修改外壳状态的操作**。

**对比：不需要 notify 的方法。** [src/input_field.rs:L129-L143](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L129-L143) 的 `text`/`clear`/`set_text`/`set_masked` 全部只拿 `&self`（连 `&mut self` 都不要），直接转发给内芯。它们不需要 notify 有两层原因：一是可变状态在内芯实体里（承接 u2-l1 的结论「修改文本只需 `&self`」）；二是内芯被修改时会自行通知自己的实体，editor 的重渲染由它自己的机制完成，而外壳的渲染结果（外壳包着的 `AnyElement` 是 `editor.render(...)` 每帧现场生成的）会在外壳重画时一并刷新。

**notify 的底层。** `Context<T>` 上的 `notify` 最终落到 [crates/gpui/src/app.rs:L2665-L2669](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L2665-L2669) 的 `App::notify(entity_id)`：按实体 id 取出该实体注册的「窗口失效器」并逐个触发——本质是把「这个实体变了」广播给所有渲染它的窗口，窗口随后调度重绘。`cx.observe` 注册的观察者回调也是由同一条通知链路驱动的。

**每帧同步作为自愈机制。** 再看一次 [src/input_field.rs:L150-L152](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L150-L152)：`masked` 的同步发生在 render 里而不是只在构造时做一次。结合 notify 的粒度可以理解这个设计的用意——外壳与内芯是两个实体、两次独立的通知流，把同步放在 render 里意味着「每次外壳重画都重新对齐一次内芯」，状态不会因为某条通知链路的缺失而永久漂移。

#### 4.4.4 代码实践

反证实验：删掉 notify 会怎样（本地练习性修改）。

1. **实践目标**：亲眼区分「内芯刷新」与「外壳刷新」两条独立通路。
2. **操作步骤**：
   - 在 4.3.4 的基础上（preview 已有 masked 示例），把 [src/input_field.rs:L224](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L224) 的 `cx.notify();` 注释掉，重新构建运行，在组件预览的 Masked 示例中输入字符并点击眼睛按钮。
3. **需要观察的现象**：点击眼睛后——输入内容的遮蔽**会**切换（圆点变明文或反之），但按钮图标**不会**从 Eye 换成 EyeOff，tooltip 也不变；直到你做出任何触发外壳重渲染的操作（例如点击另一个输入框使其聚焦，焦点边框切换会带动重画）后，图标才「追」上正确状态。
4. **预期结果**：内容遮蔽由内芯实体驱动（`editor.set_masked` → editor 自己的通知），不依赖外壳 notify；图标和 tooltip 由外壳渲染，丢了 notify 就停在上一次的状态。此现象为源码推理的预期，待本地验证。验证后记得恢复代码。

#### 4.4.5 小练习与答案

**练习 1**：如果给 InputField 新增一个运行期方法 `set_label(&mut self, label: Option<SharedString>, cx: &mut Context<Self>)`，方法体里需要什么？

**答案**：`self.label = label;` 之后必须 `cx.notify()`。label 是外壳字段，直接影响 render 输出；不 notify 则界面不更新。可以对照 `set_error`（L113-L119）的写法。

**练习 2**：`field.text(cx)` 为什么连 `&self` 就够、而且永远不需要 notify？

**答案**：`text` 是只读查询，委托给 `editor().text(cx)` 从内芯实体读取快照，不修改任何状态。notify 是「状态变了，请重画」的请求，读操作与之无关。

**练习 3**：`cx.notify()` 调用后 UI 一定立刻更新吗？

**答案**：不会「立刻」。notify 只是把实体标记为脏并触发窗口的重新绘制调度，实际的重渲染发生在框架的下一帧绘制循环中。所以在同一同步代码块里连续调用 `set_error` 两次，中间不会产生两次渲染——框架会合并，最终只渲染最后一次设置的状态。

## 5. 综合实践

把本讲的 Tab 配置、错误状态、masked 三个主题串成一个可运行的验证场景（对应本讲规格中的实践任务）。

### 5.1 实践目标

构建一个包含多个 InputField 的视图，验证：

1. `tab_index(0)` + `tab_stop(true)` 的字段排在 Tab 顺序最前；
2. `tab_stop(false)` 的字段无法通过 Tab 聚焦，但鼠标点击仍可聚焦；
3. `set_error(Some("必填项"))` 产生红色边框与提示文案，`set_error(None)` 清除。

### 5.2 操作步骤

**第一步：修改 preview。** 打开 [src/input_field.rs:L248-L271](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L248-L271)，把 `preview` 中的示例列表替换为以下内容（示例代码，本地练习用，不必提交）：

```rust
fn preview(window: &mut Window, cx: &mut App) -> AnyElement {
    let field_a = cx.new(|cx| {
        let mut field = InputField::new(window, cx, "第一个字段")
            .label("字段 A（tab_index 0，带错误）")
            .tab_index(0)
            .tab_stop(true);
        field.set_error(Some("必填项"), cx);
        field
    });

    let field_b = cx.new(|cx| {
        InputField::new(window, cx, "第二个字段")
            .label("字段 B（不可 Tab 聚焦）")
            .tab_stop(false)
    });

    let field_c = cx.new(|cx| {
        InputField::new(window, cx, "第三个字段")
            .label("字段 C（默认配置）")
    });

    v_flex()
        .gap_6()
        .children(vec![example_group(vec![
            single_example("Tab & Error Demo", v_flex()
                .gap_2()
                .child(div().child(field_a))
                .child(div().child(field_b))
                .child(div().child(field_c))
                .into_any_element()),
        ])])
        .into_any_element()
}
```

**第二步：构建运行。** 在 Zed 仓库根目录执行 `cargo run`（首次构建较久）。

**第三步：打开组件预览。** 应用启动后打开命令面板，执行 `workspace: open component preview`，在 Forms & Input 分区找到 InputField 的 `Tab & Error Demo` 示例。

**第四步：验证 Tab 行为。** 先点击预览面板顶部的过滤输入框获得焦点，然后连续按 Tab，观察焦点依次落在哪个字段。

**第五步：验证错误状态。** 观察字段 A 的红色边框与下方红色「必填项」文案。然后将代码中的 `field.set_error(Some("必填项"), cx);` 改为 `field.set_error(None, cx);`（或直接删掉这一行），重新构建，确认红色边框与文案消失。

### 5.3 需要观察的现象

1. 字段 A：红色边框 + 「必填项」红色小字；按 Tab 时它是示例区域内第一个（或优先）获得焦点的字段（`tab_index(0)` 排序最前）。
2. 字段 B：按 Tab 时焦点**跳过**它；但鼠标点击它的输入区域后仍能聚焦并输入（`tab_stop(false)` 只排除键盘导航，不影响鼠标与编程聚焦——见 4.1.3 第六、七步）。
3. 字段 C（默认配置）：根据 4.1.3 的源码分析，它应当也被 Tab 跳过（`FocusHandle::new` 默认 `tab_stop: false`）——这是验证练习 3 推断的直接机会。
4. 焦点落在任一字段时边框变为 `border_focused`；字段 A 聚焦时边框保持红色（错误优先于焦点）。

### 5.4 预期结果

- Tab 行为与上表一致（其中字段 C 的表现是本实践最有价值的观察点：如果它确实被跳过，就证实了「默认分支不改句柄 + 句柄默认 `tab_stop: false`」的推断；如果它能被 Tab 聚焦，说明存在本讲未覆盖的注册路径，欢迎回到 4.1.3 的链路里找漏）。
- 错误状态的设置与清除都如 4.2 描述。
- 以上运行表现待本地验证——本讲义作者未在此环境运行过 Zed。

### 5.5 进阶（可选）：动态清除错误

上面第五步用「改代码 + 重新构建」清除错误，略显笨拙。真实应用中错误是**随输入动态变化**的——这需要监听内芯的编辑事件，在回调里重算校验。这正是下一单元的内容（u3-l3 会讲 `editor().subscribe(Box::new(...))` 模式），此处只给出预告性的示例代码：

```rust
// 示例代码：订阅编辑事件，输入非空即清除错误（完整模式见 u3-l3）
let subscription = field_a.update(cx, |field, cx| {
    field.editor().subscribe(
        Box::new(|event, _window, _cx| {
            if matches!(event, ui_input::ErasedEditorEvent::BufferEdited) {
                // 这里需要通过弱引用找回 field_a 实体后再 set_error，
                // 具体写法在 u3-l3 的 skill_creator 实战中展开
            }
        }),
        window, cx,
    )
});
```

注意 `subscribe` 返回的 `Subscription` 被 drop 时订阅即取消，因此真实代码必须把它存进实体的字段——这个坑以及完整解法留给下一讲。

## 6. 本讲小结

- **焦点身份共享**：`Focusable` 委托（L49-53）＋ `h_flex` 的 `track_focus`（L184）让外壳容器与内芯 editor 共享同一个 `FocusHandle`，装饰性焦点样式（边框高亮）因此能挂在外壳上。
- **tab 配置的三分支**：`tab_index = Some(i)` → 设置序号与停靠标志；`None + tab_stop(false)` → 显式排除；默认 → 原样返回。配置写在句柄上并同步进全局焦点表（window.rs L571-L589），最终由 `TabStopMap.next` 在导航时跳过 `tab_stop == false` 的节点。
- **错误是运行期状态**：`set_error`（L113-L119）写字段＋`cx.notify()`；渲染侧靠两个 `.when` 的书写顺序实现「错误边框覆盖焦点边框」（L196-L200），并在输入框下方追加红色小号 Label（L231-L233）。
- **masked 的单一事实来源**：状态存在外壳的 `masked: Option<bool>` 字段，render 每帧同步进内芯（L150-L152）；眼睛按钮的点击处理由 `cx.listener` 适配——弱引用找回实体、翻转状态、同步内芯、请求重渲染（L219-L227）。
- **notify 的红线**：改内芯（editor 实体）不需要外壳 notify，内芯自己会通知；改外壳字段（error、masked）必须 `cx.notify()`，否则图标、文案等外壳渲染停在上一次状态。

## 7. 下一步学习建议

本讲两次撞上了类型擦除的边界：`editor.set_masked(...)`、`editor.focus_handle(cx)` 都是 `ErasedEditor` trait 的方法，而综合实践的进阶部分则直接用到了 `ErasedEditorEvent::BufferEdited` 订阅。下一讲 **u3-l1「ErasedEditor trait 与事件模型：类型擦除的艺术」** 将系统拆解这套抽象：13 个 trait 方法的分类、`ErasedEditorImpl` 如何用 `Entity<Editor>` 委托实现、`as_any` 类型还原逃生舱，以及为什么只保留 `BufferEdited` 和 `Blurred` 两个事件。

如果你更想先看「这些机制在真实产品里怎么用」，也可以先跳到 **u3-l3「消费方实战」**，其中 skill_creator 的订阅式校验就是本讲 `set_error` 的完整实战版。阅读源码时建议带着一个问题：本讲 4.4 的 notify 规则，在那些消费者代码里是如何被遵守（或被依赖）的？
