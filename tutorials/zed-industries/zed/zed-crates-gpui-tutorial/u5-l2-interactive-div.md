# InteractiveElement 与状态化元素

## 1. 本讲目标

上一讲（u5-l1）我们自顶向下看了 GPUI 的输入事件模型：平台输入被翻译成 `PlatformInput`，在**上一帧的渲染结果**上做命中测试，再按 Capture/Bubble 两阶段派发给监听器。本讲自底向上补齐另一半：**元素的交互能力是怎么来的**。读完本讲，你应该能够：

1. 解释为什么 `on_click` 这类监听是 **paint 阶段注册、只活一帧**，而不是持久存在的回调表；
2. 说清 `InteractiveElement` 与 `StatefulInteractiveElement` 两个 trait 的分界：哪些交互不需要状态、哪些必须先 `.id()`；
3. 理解 hitbox（命中盒）如何充当「几何 + 拓扑 + 遮挡」三合一的交互基础设施；
4. 掌握 hover/active 状态样式的生效路径，以及 `ElementId` 在其中扮演的角色；
5. 会用 `.id()`、`on_click`、`hover`、`active` 组合出完整的可交互组件。

## 2. 前置知识

- **元素三阶段**（u4-l1）：每个元素每帧依次走过 `request_layout` → `prepaint` → `paint`。本讲大量依赖这一点：hitbox 在 prepaint 阶段插入，监听器在 paint 阶段注册。
- **元素树每帧重建**（u3-l1/u4-l1）：`render()` 产出的元素树是立即模式的，帧末即被丢弃；跨帧存活的东西只有实体状态和以 `(GlobalElementId, TypeId)` 为键的元素状态表。
- **事件派发两阶段**（u5-l1）：Capture 阶段从后往前（先父后子，用于「越界检测」类场景），Bubble 阶段从前往后（先子后父，常规处理）；`cx.stop_propagation()` 可中断。
- **StyleRefinement**（u3-l3）：样式以「全 Option 补丁」的形式存储，最终按 base → focus → hover → active 的顺序 `refine` 合成。

一个值得先建立的心智模型：GPUI 的交互不是「给元素挂一个长期有效的回调」，而是「**每帧重新申报一次自己关心的东西**」。元素在 paint 阶段把闭包推进窗口的监听器列表，事件到来时窗口拿着这个列表去问每一个闭包「这个事件跟你有关吗」。这带来了两个直接后果：

- 回调闭包里捕获的 `hitbox`、实体弱引用等，天然就是「最新一帧」的数据，不存在陈旧引用问题；
- 帧与帧之间元素可以自由地增删、改 id、换交互，不需要任何「解绑」操作。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/elements/div.rs`（约 5200 行） | div 元素与全部交互机制 | `Interactivity` 结构体、`InteractiveElement`/`StatefulInteractiveElement` 两个 trait、`Stateful<E>` 包装、`paint_mouse_listeners`、`compute_style_internal`、文末的 hover 测试 |
| `src/window.rs`（约 7500 行） | 窗口运行时 | `Hitbox`/`HitboxId`/`HitboxBehavior`、`insert_hitbox`、`Frame::hit_test`、`on_mouse_event`、`dispatch_mouse_event`、`ElementId` 枚举 |
| `src/elements/img.rs` | 图片元素 | 一个「自带 id、直接实现 StatefulInteractiveElement」的反例参照 |
| `examples/scrollable.rs` | 双向滚动示例 | `.id()` + `.overflow_scroll()` 的标准组合 |
| `examples/opacity.rs` | 透明度动画示例 | `.id("panel")` + `on_click(cx.listener(...))`，以及无 id 元素上的 `.hover()` |

## 4. 核心概念与源码讲解

### 4.1 InteractiveElement 与 Interactivity：交互配置存在哪里

#### 4.1.1 概念说明

`div()` 链式调用里的所有交互方法——`on_mouse_down`、`hover`、`occlude`、`key_context`……——并不是 60 个独立字段上的 60 个方法，而是统一写入一个叫 `Interactivity` 的结构体。`InteractiveElement` trait 只有一个必需方法 `interactivity()`，用来拿到这个结构体的可变引用：

```rust
// src/elements/div.rs:730-734
/// A trait for elements that want to use the standard GPUI event handlers that don't
/// require any state.
pub trait InteractiveElement: Sized {
    /// Retrieve the interactivity state associated with this element
    fn interactivity(&mut self) -> &mut Interactivity;
```

[div.rs:730-734](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L730-L734) —— trait 文档点明了它的定位：**不需要状态的**标准事件处理器都放这里。

「不需要状态」是什么意思？以 `on_mouse_down` 为例：事件到达时，只需拿 hitbox 问一句「鼠标在我身上吗」，是就调用回调，全程不需要记住任何跨帧信息。而 `on_click` 不同——它必须记住「鼠标是否曾在我身上按下过」，等 mouse up 到来时才能判定这是一次完整点击。这个「记住」就是状态，正是 4.2 节 `Stateful<E>` 要解决的问题。

#### 4.1.2 核心流程

`Interactivity` 是一个约 50 个字段的大杂烩，可以按用途分成四组：

- **身份**：`element_id`（`Option<ElementId>`，由 `.id()` 填入）；
- **样式补丁**：`base_style`（链式样式写这里，见 u3-l2）、`hover_style`、`active_style`、`focus_style`、`group_hover_style`、`drag_over_styles`……
- **监听器**：`mouse_down_listeners`、`mouse_up_listeners`、`click_listeners`、`scroll_wheel_listeners`、`hover_listener`……全是 `Vec<Box<dyn Fn>>`；
- **杂项**：`hitbox_behavior`（遮挡模式）、`key_context`（键位上下文）、`tracked_focus_handle`、`tracked_scroll_handle`、`tooltip_builder` 等。

链式调用的执行流程可以用伪代码概括：

```
div()                        → 创建默认 Interactivity
  .p_4()                     → 写 base_style.padding
  .hover(|s| s.bg(...))      → 写 hover_style = Some(补丁)
  .on_mouse_down(左键, cb)   → push 一个闭包进 mouse_down_listeners
  .occlude()                 → hitbox_behavior = BlockMouse
帧末（paint 阶段）
  → 这些字段被消费：闭包注册进窗口，样式参与合成
```

#### 4.1.3 源码精读

先看 `Interactivity` 结构体本身，注意 `element_id` 字段的注释——它直接预告了 `.id()` 的意义：

```rust
// src/elements/div.rs:2022-2032（节选）
/// The interactivity struct. Powers all of the general-purpose
/// interactivity in the `Div` element.
#[derive(Default)]
pub struct Interactivity {
    /// The element ID of the element. In id is required to support a stateful subset of the interactivity such as on_click.
    pub element_id: Option<ElementId>,
    /// Whether the element was clicked. This will only be present after layout.
    pub active: Option<bool>,
    /// Whether the element was hovered. This will only be present after paint if an hitbox
    /// was created for the interactive element.
    pub hovered: Option<bool>,
```

[div.rs:2022-2032](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L2022-L2032) —— 交互能力的总仓库：id、按下/悬停标志、各类样式补丁与监听器都在这一个结构体里。

再看 `hover` 与 `on_mouse_down` 两个典型方法，体会「链式方法只是写字段/push 闭包」：

```rust
// src/elements/div.rs:805-813
/// Apply the given style to this element when the mouse hovers over it
fn hover(mut self, f: impl FnOnce(StyleRefinement) -> StyleRefinement) -> Self {
    debug_assert!(
        self.interactivity().hover_style.is_none(),
        "hover style already set"
    );
    self.interactivity().hover_style = Some(Box::new(f(StyleRefinement::default())));
    self
}
```

[div.rs:805-813](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L805-L813) —— `hover` 把闭包产出的 `StyleRefinement` 存进 `hover_style`。注意 `debug_assert`：同一元素重复设 hover 样式在调试构建下会 panic（后者本会静默覆盖前者）。

```rust
// src/elements/div.rs:832-839
/// Bind the given callback to the mouse down event for the given mouse button.
/// The fluent API equivalent to [`Interactivity::on_mouse_down`].
fn on_mouse_down(
    mut self,
    button: MouseButton,
    listener: impl Fn(&MouseDownEvent, &mut Window, &mut App) + 'static,
) -> Self {
    self.interactivity().on_mouse_down(button, listener);
    self
}
```

[div.rs:832-839](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L832-L839) —— trait 上的流式方法只是转发；真正 push 闭包的是命令式版本：

```rust
// src/elements/div.rs:122-136
pub fn on_mouse_down(
    &mut self,
    button: MouseButton,
    listener: impl Fn(&MouseDownEvent, &mut Window, &mut App) + 'static,
) {
    self.mouse_down_listeners
        .push(Box::new(move |event, phase, hitbox, window, cx| {
            if phase == DispatchPhase::Bubble
                && event.button == button
                && hitbox.is_hovered(window)
            {
                (listener)(event, window, cx)
            }
        }));
}
```

[div.rs:122-136](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L122-L136) —— 注意被 push 的闭包在**事件到来时**才检查三件事：阶段是 Bubble、按钮匹配、`hitbox.is_hovered(window)`。这就是「无状态」交互的全部判定逻辑：命中测试是实时的，不需要记忆。

这套「命令式 + 流式」双 API 是刻意设计的：自定义 `Element` 内部持有 `Interactivity` 时用命令式版本（第一个参数是 `&mut self` 的 `Interactivity`），div 链上用流式版本。二者文档互相引用（`The imperative API equivalent of ...`）。

最后看遮挡类方法，它们不改监听器，而是改写 hitbox 的行为标志：

```rust
// src/elements/div.rs:1187-1209（节选）
/// Block the mouse from all interactions with elements behind this element's hitbox.
/// Typically `block_mouse_except_scroll` should be preferred.
fn occlude(mut self) -> Self {
    self.interactivity().occlude_mouse();
    self
}
...
/// Block non-scroll mouse interactions with elements behind this element's hitbox.
fn block_mouse_except_scroll(mut self) -> Self {
    self.interactivity().block_mouse_except_scroll();
    self
}
```

[div.rs:1187-1209](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L1187-L1209) —— 浮层遮住下层交互的标准做法，4.3 节讲 `HitboxBehavior` 时会看到它的消费端。

#### 4.1.4 代码实践

**实践目标**：验证「无状态监听器不要求 id」，并观察回调参数。

1. 打开 `examples/opacity.rs`，找到第 148-152 行那组 emoji div：

```rust
// examples/opacity.rs:145-152
.child(
    div()
        .flex()
        .children(["🎊", "✈️", "🎉", "🎈", "🎁", "🎂"].map(|emoji| {
            div()
                .child(emoji.to_string())
                .hover(|style| style.opacity(0.5))
        })),
```

[opacity.rs:145-152](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/opacity.rs#L145-L152) —— 每个 emoji div 都没有 `.id()`，却能用 `.hover()`（无状态样式）。

2. 给其中一个 emoji div 追加一行 `.on_mouse_down(MouseButton::Left, |e, _, _| println!("down at {:?}", e.position))`（示例代码，需在文件头部补充 `MouseButton` 导入）。
3. 运行 `cargo run -p gpui --example opacity`，在 emoji 上按下鼠标左键。
4. **观察现象**：终端打印出窗口坐标系的 `position`（u5-l1 讲过：要换算元素局部坐标需减去 `bounds.origin`）；在 emoji 之外按下则不打印。
5. **预期结果**：无 id 的元素可以正常使用 `on_mouse_down`/`hover` 等 `InteractiveElement` 方法。若这一步与本地观察不符，以待本地验证为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `InteractiveElement` 的必需方法只有 `interactivity()`，而不是为每类事件各设一个必需方法？

**答案**：因为所有交互配置最终都汇聚到 `Interactivity` 这一个结构体里。trait 只需要暴露「拿到它」的入口，几十个带默认实现的方法就能统一操作任何拥有该结构体的元素。这也是 `UniformList`、`Img`、`Svg` 等其他元素能复用整套交互 API 的原因——它们内部同样持有一个 `Interactivity`。

**练习 2**：`.on_mouse_down(MouseButton::Left, ...)` 和 `.on_any_mouse_down(...)` 在注册内容上有何本质区别？

**答案**：没有本质区别，两者 push 的闭包结构相同，都检查 `hitbox.is_hovered(window)`；区别只在判定条件——前者多一个 `event.button == button` 检查（见 [div.rs:122-136](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L122-L136)），后者接受任意按钮（[div.rs:158-168](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L158-L168)）。

### 4.2 Stateful\<E\> 与 ElementId：状态化元素的类型守门

#### 4.2.1 概念说明

`on_click` 需要跨帧记忆「按下是否发生在我身上」；`overflow_scroll` 需要跨帧记忆滚动偏移；`on_hover` 需要跨帧记忆「上一帧是否悬停」以产生进入/离开事件。这些记忆必须有一个跨帧的存放处——u4-l1 讲过的窗口元素状态表，以 `(GlobalElementId, TypeId)` 为键。**没有 id 就没有键，就没有状态**。

GPUI 用类型系统把这条规则硬性化了：

- `InteractiveElement::id()` 消费 `Self`、返回 `Stateful<Self>`；
- 需要 state 的方法（`on_click`、`active`、`on_hover`、`overflow_scroll`、`track_scroll`、`focusable`、无障碍标注……）全部收进 `StatefulInteractiveElement` trait；
- 该 trait 在 gpui 内部只为 `Stateful<E>`（以及自带 id 的 `Img`）实现。

于是「给无 id 的 div 挂 on_click」根本过不了编译，而不是运行期静默失效。

#### 4.2.2 核心流程

```
div()  ──.id("panel")──▶  Stateful<Div>
                                │
        ┌───────────────────────┴───────────────────────┐
   InteractiveElement                              StatefulInteractiveElement
 (无状态：hover、on_mouse_down、      （有状态：on_click、active、on_hover、
  occlude、key_context、group…)        overflow_scroll、focusable、role…）
```

`Stateful<E>` 是零成本包装：它只是把 `E` 装进一个字段，然后把 `Styled`、`InteractiveElement`、`ParentElement`、`Element` 全部原样转发给内部元素。唯一的「增值」是类型层面的——让 `StatefulInteractiveElement` 的 blanket 实现生效。

`ElementId` 是一个枚举，支持字符串、整数、名字+整数、UUID、路径、焦点句柄等多种形态；同层兄弟元素必须 id 不同，而**完整键是「沿元素树的 id 路径」**（`GlobalElementId`，u4-l1 讲过），所以不同父节点下的同名 id 不冲突。

#### 4.2.3 源码精读

`id()` 的定义——注意它同时做两件事：写入 element_id 字段、改变返回类型：

```rust
// src/elements/div.rs:742-747
/// Assign this element an ID, so that it can be used with interactivity
fn id(mut self, id: impl Into<ElementId>) -> Stateful<Self> {
    self.interactivity().element_id = Some(id.into());

    Stateful { element: self }
}
```

[div.rs:742-747](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L742-L747)

`Stateful` 的定义与它的 trait 实现几乎是「透明转发」的教科书：

```rust
// src/elements/div.rs:3865-3868
/// A wrapper around an element that can store state, produced after assigning an ElementId.
pub struct Stateful<E> {
    pub(crate) element: E,
}
```

[div.rs:3865-3868](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L3865-L3868) —— 就一个字段。

```rust
// src/elements/div.rs:3879-3893
impl<E> StatefulInteractiveElement for Stateful<E>
where
    E: Element,
    Self: InteractiveElement,
{
}

impl<E> InteractiveElement for Stateful<E>
where
    E: InteractiveElement,
{
    fn interactivity(&mut self) -> &mut Interactivity {
        self.element.interactivity()
    }
}
```

[div.rs:3879-3893](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L3879-L3893) —— 空的 impl 块就是「授权」：从此 `Stateful<Div>` 拥有全部有状态方法。`Element` 的实现（[div.rs:3895-3969](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L3895-L3969)）则把三阶段全部委托给内部元素，其中 `fn id(&self)` 直接返回内部元素登记的 id。

`StatefulInteractiveElement` 的代表性成员：

```rust
// src/elements/div.rs:1244-1246
/// A trait for elements that want to use the standard GPUI interactivity features
/// that require state.
pub trait StatefulInteractiveElement: InteractiveElement {
```

[div.rs:1244-1246](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L1244-L1246)

```rust
// src/elements/div.rs:1524-1534
/// Bind the given callback to click events of this element.
fn on_click(mut self, listener: impl Fn(&ClickEvent, &mut Window, &mut App) + 'static) -> Self
where
    Self: Sized,
{
    self.interactivity().on_click(listener);
    self
}
```

[div.rs:1524-1534](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L1524-L1534) —— `on_click` 收到的 `ClickEvent` 是合成事件，4.4 节讲它的合成过程。

```rust
// src/elements/div.rs:1460-1465
/// Set the overflow x and y to scroll.
fn overflow_scroll(mut self) -> Self {
    self.interactivity().base_style.overflow.x = Some(Overflow::Scroll);
    self.interactivity().base_style.overflow.y = Some(Overflow::Scroll);
    self
}
```

[div.rs:1460-1465](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L1460-L1465) —— 有趣的是 `overflow_scroll` 只是写样式字段；它需要 id 的原因是滚动偏移要存进元素状态（4.4 节 `paint_scroll_listener` 会看到 `Rc<RefCell<Point<Pixels>>>` 形式的偏移就来自那里）。

`ElementId` 枚举与最常用的元组形式：

```rust
// src/window.rs:6612-6638（节选）
/// An identifier for an [`Element`].
///
/// Can be constructed with a string, a number, or both, as well
/// as other internal representations.
#[derive(Clone, Debug, Eq, PartialEq, Hash)]
pub enum ElementId {
    /// The ID of a View element
    View(EntityId),
    /// An integer ID.
    Integer(u64),
    /// A string based ID.
    Name(SharedString),
    ...
    /// A combination of a name and an integer.
    NamedInteger(SharedString, u64),
```

[window.rs:6612-6638](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L6612-L6638) —— 列表场景推荐 `NamedInteger` 形态；`From<(&'static str, usize)>` 已实现（[window.rs:6732-6736](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L6732-L6736)），所以 `.id(("row", i))` 是合法写法。

反例参照——`Img` 不经 `Stateful` 包装直接实现两个 trait：

```rust
// src/elements/img.rs:519-533
impl InteractiveElement for Img {
    fn interactivity(&mut self) -> &mut Interactivity {
        &mut self.interactivity
    }
}
...
impl StatefulInteractiveElement for Img {}
```

[img.rs:519-533](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/img.rs#L519-L533) —— 因为 `Img` 自身能从图片来源推导出稳定的元素 id，不需要外部再指定。这说明 `Stateful` 不是唯一途径，只是 div 的途径。

两个示例中的标准用法：

```rust
// examples/scrollable.rs:10-16
div()
    .size_full()
    .id("vertical")
    .p_4()
    .overflow_scroll()
    .bg(gpui::white())
```

[scrollable.rs:10-16](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/scrollable.rs#L10-L16) —— `.id()` 紧跟在尺寸之后、滚动样式之前，是可滚动容器的固定句式。

```rust
// examples/opacity.rs:90-92
div()
    .id("panel")
    .on_click(cx.listener(Self::start_animation))
```

[opacity.rs:90-92](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/opacity.rs#L90-L92) —— `.id()` + `on_click` + `cx.listener`（u2-l3 讲过 listener 如何把元素回调适配到实体方法）。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「无 id 不能用有状态交互」是编译期约束。

1. 在 `examples/scrollable.rs` 里把第 12 行的 `.id("vertical")` 删掉，保留 `.overflow_scroll()`。
2. 运行 `cargo check -p gpui --examples`。
3. **观察现象**：编译失败。错误信息类似 `no method named overflow_scroll found for struct Div`，并提示 `Stateful<Div>` 上存在同名方法。
4. 恢复 `.id("vertical")`，把 id 改成 `.id(0)`（`usize` 也是合法的 `ElementId`），再次编译。
5. **预期结果**：第 3 步必现编译错误（这是类型系统保证的）；第 4 步编译通过——id 的具体形态不影响能力，只影响状态表的键。

#### 4.2.5 小练习与答案

**练习 1**：同一个列表里循环生成 100 个按钮，都写 `.id("btn")` 会怎样？

**答案**：编译能过（GPUI 不做静态查重），但同层兄弟共享同一个 `GlobalElementId`，它们的元素状态会在窗口状态表中互相覆盖/串台——点击计数、按下状态会错乱。正确写法是 `.id(("btn", i))` 生成 `NamedInteger` 形式的唯一 id。运行期某些状态错用会在 debug 构建下以 panic 暴露（如 `with_element_state` 的重入断言），但 id 撞车本身主要表现为状态串扰。

**练习 2**：`Stateful<E>` 会给运行时带来额外开销吗？

**答案**：不会。它是单字段包装，`Element` 实现全部委托内部元素，三阶段行为与包装前完全一致；「有状态」的开销来自状态表里的 `InteractiveElementState`（也只在真正用到时才创建），而不是这个类型本身。

**练习 3**：为什么 `on_hover`（回调）需要 id，而 `hover`（样式）在 `InteractiveElement` 上？

**答案**：`on_hover` 要产生「进入/离开」两个事件，必须记住上一帧的悬停布尔值作对比——这是状态；`hover` 样式在每帧 paint 时用 hitbox 实时判定即可生效（见 4.4.3 对 `compute_style_internal` 的分析），判定本身不需要记忆。（但注意 4.4.5 练习 3 讨论的细节：悬停**转移触发重绘**这件事仍依赖状态。）

### 4.3 hitbox：命中测试的几何基础

#### 4.3.1 概念说明

hitbox（命中盒）是 prepaint 阶段登记到窗口的一块矩形区域，携带遮挡行为标志。它是三类问题的统一答案：

1. **几何**：鼠标位置是否落在元素 bounds 内（还要与 content_mask 求交，处理滚动裁剪）；
2. **拓扑**：命中测试按插入顺序的逆序遍历，后绘制的（视觉在上的）元素先命中——这决定了 Bubble 阶段「最上层优先」的语义；
3. **遮挡**：`HitboxBehavior` 让上层元素能把下层的鼠标交互整体屏蔽（浮层场景），或只屏蔽非滚动交互（「遮住但允许穿透滚动」）。

一个容易混淆的点：hitbox 不是监听器。hitbox 是「这块区域存在且可被命中」的登记；监听器是 paint 阶段注册的闭包，闭包捕获 hitbox 的克隆，在事件到来时查询。二者一静一动。

#### 4.3.2 核心流程

```
prepaint 阶段
  Interactivity::prepaint
    ├─ should_insert_hitbox()? ── 有监听器/hover 样式/滚动/焦点/组… 才需要
    └─ window.insert_hitbox(bounds, hitbox_behavior) → push 进 next_frame.hitboxes
帧结束后 next_frame 成为 rendered_frame

事件到来时
  Window::dispatch_mouse_event
    └─ rendered_frame.hit_test(mouse_position)
         从后往前遍历 hitboxes：
           bounds ∩ content_mask 包含鼠标 → 收进 ids
           遇 BlockMouse → 停止（其后的不再参与 hover）
           遇 BlockMouseExceptScroll → 标记 hover_hitbox_count（其后只配滚动）
  每个监听器闭包拿自己捕获的 HitboxId 查询：在 ids 前 N 个里吗？
```

#### 4.3.3 源码精读

`Hitbox` 结构——id、bounds、内容遮罩、行为四件套：

```rust
// src/window.rs:818-831
/// A rectangular region that potentially blocks hitboxes inserted prior.
/// See [Window::insert_hitbox] for more details.
#[derive(Clone, Debug, Deref)]
pub struct Hitbox {
    /// A unique identifier for this hitbox.
    pub id: HitboxId,
    /// The bounds of the hitbox.
    #[deref]
    pub bounds: Bounds<Pixels>,
    /// The content mask when the hitbox was inserted.
    pub content_mask: ContentMask<Pixels>,
    /// Flags that specify hitbox behavior.
    pub behavior: HitboxBehavior,
}
```

[window.rs:818-831](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L818-L831)

插入时机与「何时才需要 hitbox」的判定：

```rust
// src/elements/div.rs:2275-2279（Interactivity::prepaint 内）
let hitbox = if self.should_insert_hitbox(&style, window, cx) {
    Some(window.insert_hitbox(bounds, self.hitbox_behavior))
} else {
    None
};
```

[div.rs:2271-2283](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L2271-L2283) —— 纯展示的 div 不插 hitbox，省下命中测试成本。

```rust
// src/elements/div.rs:2292-2316（节选）
fn should_insert_hitbox(&self, style: &Style, window: &Window, cx: &App) -> bool {
    self.hitbox_behavior != HitboxBehavior::Normal
        || self.window_control.is_some()
        || style.mouse_cursor.is_some()
        || self.group.is_some()
        || self.scroll_offset.is_some()
        || self.tracked_focus_handle.is_some()
        || self.hover_style.is_some()
        ...
        || !self.click_listeners.is_empty()
        || !self.scroll_wheel_listeners.is_empty()
        ...
}
```

[div.rs:2292-2316](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L2292-L2316) —— 一份「什么元素需要参与鼠标交互」的完整清单：遮挡、光标、组、滚动、焦点、hover、任一类监听器、拖放、tooltip……这也是排错时的重要检查点：**如果这些条件一个都不满足，元素对鼠标完全透明**。

`insert_hitbox` 本体（prepaint 限定，帧内递增 id）：

```rust
// src/window.rs:4731-4745
pub fn insert_hitbox(&mut self, bounds: Bounds<Pixels>, behavior: HitboxBehavior) -> Hitbox {
    self.invalidator.debug_assert_prepaint();

    let content_mask = self.content_mask();
    let mut id = self.next_hitbox_id;
    self.next_hitbox_id = self.next_hitbox_id.next();
    let hitbox = Hitbox {
        id,
        bounds,
        content_mask,
        behavior,
    };
    self.next_frame.hitboxes.push(hitbox.clone());
    hitbox
}
```

[window.rs:4731-4745](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L4731-L4745) —— `debug_assert_prepaint()` 是 u4-l1 讲过的阶段状态机：在 paint 阶段调用它会 panic。

命中测试的遍历逻辑（遮挡语义的核心）：

```rust
// src/window.rs:1081-1103
pub(crate) fn hit_test(&self, position: Point<Pixels>) -> HitTest {
    let mut set_hover_hitbox_count = false;
    let mut hit_test = HitTest::default();
    for hitbox in self.hitboxes.iter().rev() {
        let bounds = hitbox.bounds.intersect(&hitbox.content_mask.bounds);
        if bounds.contains(&position) {
            hit_test.ids.push(hitbox.id);
            if !set_hover_hitbox_count
                && hitbox.behavior == HitboxBehavior::BlockMouseExceptScroll
            {
                hit_test.hover_hitbox_count = hit_test.ids.len();
                set_hover_hitbox_count = true;
            }
            if hitbox.behavior == HitboxBehavior::BlockMouse {
                break;
            }
        }
    }
    if !set_hover_hitbox_count {
        hit_test.hover_hitbox_count = hit_test.ids.len();
    }
    hit_test
}
```

[window.rs:1081-1103](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L1081-L1103) —— 逆序遍历（视觉上层在前）；`BlockMouse` 直接截断；`BlockMouseExceptScroll` 只截断「可 hover 前缀」，其后的 hitbox 仍留在 `ids` 里供滚动查询。

查询端——`is_hovered` 与 `should_handle_scroll` 的分工：

```rust
// src/window.rs:765-803（节选）
pub fn is_hovered(self, window: &Window) -> bool {
    // If this hitbox has captured the pointer, it's always considered hovered
    if window.captured_hitbox == Some(self) {
        return true;
    }
    if window.last_input_was_keyboard() {
        return false;
    }
    self.hit_test(window)
}

fn hit_test(self, window: &Window) -> bool {
    let hit_test = &window.mouse_hit_test;
    for id in hit_test.ids.iter().take(hit_test.hover_hitbox_count) {
        if self == *id {
            return true;
        }
    }
    false
}

/// Checks if the hitbox contains the mouse and should handle scroll events.
pub fn should_handle_scroll(self, window: &Window) -> bool {
    window.mouse_hit_test.ids.contains(&self)
}
```

[window.rs:765-811](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L765-L811) —— 三个值得记住的细节：(1) 键盘模态下 `is_hovered` 恒为 false，防止键盘导航时鼠标下的元素闪高亮；(2) 指针被某 hitbox 捕获时它恒为 hovered（拖拽场景）；(3) `is_hovered` 只查 `hover_hitbox_count` 前缀、`should_handle_scroll` 查全表——这正是 `BlockMouseExceptScroll` 的实现方式。

`HitboxBehavior` 两个变体的文档非常清晰（[window.rs:865-922](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L865-L922)），此处不再赘引。

#### 4.3.4 代码实践

**实践目标**：直观感受「上层 hitbox 屏蔽下层」与遮挡开关的作用。

1. 复制 `examples/scrollable.rs` 为 `examples/hitbox_lab.rs`，并在 `Cargo.toml` 追加一段 `[[example]] name = "hitbox_lab" path = "examples/hitbox_lab.rs"`（本仓库示例都显式声明；也可以直接改 scrollable.rs 本体，则无需动 Cargo.toml）。
2. 把 `render` 改为：一个 300×300 的底层 div（`.id("bottom")`，`on_click` 打印 bottom、`hover` 变蓝），再绝对定位一个 100×100 的上层 div 盖在它左上角（`.id("top")`，`on_click` 打印 top）（示例代码）。
3. 运行 `cargo run -p gpui --example hitbox_lab`，分别把鼠标移到上层、底层未被遮挡区域。
4. **观察现象**：鼠标在上层时只有上层高亮；点击穿透检查——点上层区域只打印 top。把鼠标压着两层交界缓慢移动，注意悬停高亮的切换是干净利落的，不会两层同时亮。
5. 给上层 div 追加 `.block_mouse_except_scroll()` 再观察底层滚动行为；再换成 `.occlude()` 对比。
6. **预期结果**：`occlude` 后底层连滚动都收不到；`block_mouse_except_scroll` 后底层 hover/click 仍被屏蔽但滚轮可穿透到底层滚动容器。具体表现待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 hitbox 在 prepaint 而不是 paint 阶段插入？

**答案**：命中盒的语义是「这一帧的几何事实」，需要在绘制前定稿：事件派发用的 `rendered_frame.hitboxes` 是上一帧插入的完整集合；同时 hitbox 要带上当时的 `content_mask`（滚动裁剪），而 content_mask 在 prepaint 期间由外向内逐层压栈确定。`insert_hitbox` 里的 `debug_assert_prepaint()` 从调试构建上强制了这一约定。

**练习 2**：两个兄弟 div 部分重叠、都没设遮挡，鼠标在重叠区，两边的 `on_mouse_down` 都会触发吗？

**答案**：都会进入命中列表 `ids`（无人截断），但事件派发的 Bubble 阶段按注册逆序（视觉上层优先）调用，先触发的可以 `cx.stop_propagation()` 阻止后者。不主动 stop 的话两个都会收到——这就是为什么模态浮层要显式 `occlude()` 或 stop，而不能依赖「只有一个收到」。

**练习 3**：`hitbox.is_hovered(window)` 与 `hitbox.bounds.contains(&window.mouse_position())` 有何区别？

**答案**：后者只做纯几何包含，忽略 content_mask 裁剪（元素被滚动出可视区时仍可能「包含」）、忽略遮挡（被浮层盖住仍返回 true）、忽略键盘模态抑制。前者是框架语义下的「可交互悬停」。永远用前者。

### 4.4 paint_mouse_listeners：paint 阶段的监听器注册与 click 合成

#### 4.4.1 概念说明

这是本讲的主菜：把前三节串起来。`paint_mouse_listeners` 是 `Interactivity::paint` 中的一个步骤，负责把帧初收集的所有监听器闭包「搬进」窗口，并补上几套框架级合成逻辑：

- **click 合成**：浏览器语义的「同元素按下+抬起」才触发 `on_click`；
- **hover 状态翻转**：让悬停转移触发视图重绘；
- **键盘激活**：元素聚焦时 Enter/Space 等价于点击（无障碍要求）；
- **滚动监听**：`overflow_scroll` 的偏移维护。

「注册进窗口」的终点是 `window.on_mouse_event`，其文档直白地说明了生命周期：

> Register a mouse event listener on the window for the next frame... When the next frame is rendered the listener will be cleared.

即监听器列表是**帧级**的：本帧注册、供本帧渲染结果使用、下帧重画时全部作废重来。

#### 4.4.2 核心流程

一次完整点击的旅程（结合 u5-l1 的派发机制）：

```
【第 N 帧】
paint: paint_mouse_listeners
  ├─ 各类监听器 drain 出 Interactivity，包装后 push 进 next_frame.mouse_listeners
  ├─ click 三件套：注册 mouse_down 记录器 / mouse_move 拖拽检测 / mouse_up 合成器
  └─ hover 监听器：比较 hover_state 与实时 is_hovered，变化则写状态 + cx.notify(视图)
帧提交：next_frame ⇄ rendered_frame

【事件】平台 MouseDown 到达
  Window::dispatch_mouse_event
  ├─ rendered_frame.hit_test(mouse_position) 更新 mouse_hit_test
  ├─ Capture 阶段：正序遍历监听器列表（先注册的先收到——
  │   父元素先于子元素注册，故从视觉底层/外层向顶层传播）
  └─ Bubble 阶段：逆序遍历，后注册的先收到（子元素/视觉上层优先）
       click 记录器命中 → pending_mouse_down = Some(event) → window.refresh()
【事件】平台 MouseUp 到达
  ├─ Capture：若 pending 存在且仍 hover → 取出存入 captured_mouse_down
  └─ Bubble：组装 ClickEvent::Mouse{down, up} → 逐个调用 click_listeners
```

hover/active 样式的生效路径：

```
hover 监听器检测到进入/离开
  → hover_state.element 翻转 + cx.notify(current_view)   ← 触发下一帧
下一帧 request_layout/prepaint
  → compute_style_internal：无 hitbox 时用 hover_state 回退值
下一帧 paint
  → compute_style_internal：hitbox.is_hovered() 实时值
  → style.refine(hover_style)                            ← 高亮真正画出来
```

#### 4.4.3 源码精读

`Interactivity::paint` 中调用监听器注册的位置——注意它嵌在 hitbox 存在的分支里：

```rust
// src/elements/div.rs:2440-2476（节选）
if let Some(hitbox) = hitbox {
    #[cfg(debug_assertions)]
    self.paint_debug_info(global_id, hitbox, &style, window, cx);

    if let Some(drag) = cx.active_drag.as_ref() {
        if let Some(mouse_cursor) = drag.cursor_style {
            window.set_window_cursor_style(mouse_cursor);
        }
    } else {
        if let Some(mouse_cursor) = style.mouse_cursor {
            window.set_cursor_style(mouse_cursor, hitbox);
        }
    }
    ...
    self.paint_mouse_listeners(hitbox, element_state.as_mut(), window, cx);
    self.paint_scroll_listener(hitbox, &style, window, cx);
}

self.paint_keyboard_listeners(window, cx);
```

[div.rs:2440-2476](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L2440-L2476) —— 没有插入 hitbox 的元素（4.3 的 `should_insert_hitbox` 为 false）根本走不到监听器注册，双保险。

注册的主体——`drain` 是关键词，消费即清空，保证闭包只注册一次、Interactivity 帧内即弃：

```rust
// src/elements/div.rs:2667-2672（paint_mouse_listeners 内）
for listener in self.mouse_down_listeners.drain(..) {
    let hitbox = hitbox.clone();
    window.on_mouse_event(move |event: &MouseDownEvent, phase, window, cx| {
        listener(event, phase, &hitbox, window, cx);
    })
}
```

[div.rs:2667-2693](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L2667-L2693) —— 每个 4.1 节 push 进来的判定闭包在此被第二层包装：捕获 hitbox 克隆，透传事件、阶段。`mouse_up/mouse_move/mouse_exit/scroll_wheel/pinch` 各类同构。

`window.on_mouse_event` 的落点：

```rust
// src/window.rs:4843-4861
/// Register a mouse event listener on the window for the next frame. The type of event
/// is determined by the first parameter of the given listener. When the next frame is rendered
/// the listener will be cleared.
pub fn on_mouse_event<Event: MouseEvent>(
    &mut self,
    mut listener: impl FnMut(&Event, DispatchPhase, &mut Window, &mut App) + 'static,
) {
    self.invalidator.debug_assert_paint();

    self.next_frame.mouse_listeners.push(Some(Box::new(
        move |event: &dyn Any, phase: DispatchPhase, window: &mut Window, cx: &mut App| {
            if let Some(event) = event.downcast_ref() {
                listener(event, phase, window, cx)
            }
        },
    )));
}
```

[window.rs:4843-4861](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L4843-L4861) —— 类型擦除 + 按事件类型 downcast 分发；`debug_assert_paint()` 再一次强制阶段。

click 合成的第一步——mouse down 时记录 pending（注意 `window.refresh()`）：

```rust
// src/elements/div.rs:2827-2840
window.on_mouse_event({
    let pending_mouse_down = pending_mouse_down.clone();
    let hitbox = hitbox.clone();
    let has_aux_click_listeners = !aux_click_listeners.is_empty();
    move |event: &MouseDownEvent, phase, window, _cx| {
        if phase == DispatchPhase::Bubble
            && (event.button == MouseButton::Left || has_aux_click_listeners)
            && hitbox.is_hovered(window)
        {
            *pending_mouse_down.borrow_mut() = Some(event.clone());
            window.refresh();
        }
    }
});
```

[div.rs:2827-2840](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L2827-L2840) —— `pending_mouse_down` 是存进 `InteractiveElementState` 的 `Rc<RefCell<Option<MouseDownEvent>>>`（[div.rs:3476](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L3476)），这就是 on_click 需要 id 的根本原因。`window.refresh()` 让按下瞬间就重绘——`active()` 样式（按下变色）由此立即生效，而不是等点击完成。

click 合成的最后一步——mouse up 时配对：

```rust
// src/elements/div.rs:2954-3001（节选）
window.on_mouse_event({
    let mut captured_mouse_down = None;
    let hitbox = hitbox.clone();
    move |event: &MouseUpEvent, phase, window, cx| match phase {
        // Clear the pending mouse down during the capture phase,
        // so that it happens even if another event handler stops
        // propagation.
        DispatchPhase::Capture => {
            let mut pending_mouse_down = pending_mouse_down.borrow_mut();
            if pending_mouse_down.is_some() && hitbox.is_hovered(window) {
                captured_mouse_down = pending_mouse_down.take();
                window.refresh();
            } else if pending_mouse_down.is_some() {
                // Clear the pending mouse down event (without firing click handlers)
                // if the hitbox is not being hovered.
                ...
                pending_mouse_down.take();
                window.refresh();
            }
        }
        // Fire click handlers during the bubble phase.
        DispatchPhase::Bubble => {
            if let Some(mouse_down) = captured_mouse_down.take() {
                let btn = mouse_down.button;

                let mouse_click = ClickEvent::Mouse(MouseClickEvent {
                    down: mouse_down,
                    up: event.clone(),
                });
                ...按左键/非左键分发 click_listeners 或 aux_click_listeners
            }
        }
    }
});
```

[div.rs:2954-3001](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L2954-L3001) —— 精妙之处在阶段分工：Capture 阶段取走 pending（即使别人 stop_propagation 也会执行，保证状态机不卡死），Bubble 阶段才真正触发回调。「按下在 A、抬起在 B」则 pending 被清空但不触发——与浏览器一致。中间还有一段拖拽检测（[div.rs:2842-2885](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L2842-L2885)）：mouse move 距按下点超过 `DRAG_THRESHOLD`（2 像素，[div.rs:48](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L48)）即转为拖拽、取消点击——细节留给 u5-l6 拖放一讲。

hover 状态翻转监听（注意它对无 id 元素的差别待遇）：

```rust
// src/elements/div.rs:2716-2741
if self.hover_style.is_some()
    || self.base_style.mouse_cursor.is_some()
    || cx.active_drag.is_some() && !self.drag_over_styles.is_empty()
{
    let hitbox = hitbox.clone();
    let hover_state = self.hover_style.as_ref().and_then(|_| {
        element_state
            .as_ref()
            .and_then(|state| state.hover_state.as_ref())
            .cloned()
    });
    let current_view = window.current_view();

    window.on_mouse_event(move |_: &MouseMoveEvent, phase, window, cx| {
        let hovered = hitbox.is_hovered(window);
        let was_hovered = hover_state
            .as_ref()
            .is_some_and(|state| state.borrow().element);
        if phase == DispatchPhase::Capture && hovered != was_hovered {
            if let Some(hover_state) = &hover_state {
                hover_state.borrow_mut().element = hovered;
                cx.notify(current_view);
            }
        }
    });
}
```

[div.rs:2716-2741](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L2716-L2741) —— 悬停转移时写 `hover_state` 并 `cx.notify(current_view)` 触发重绘。`hover_state` 来自元素状态表——**没有 id 就没有它，翻转分支就成了空转**（样式判定仍会实时做，见下，但没人通知视图重画）。仓库里的测试 `group_hover_styles_update_only_on_transitions`（[div.rs:4297-4346](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L4297-L4346)）验证了「转移才重绘、停留不重绘」的语义：鼠标在同状态内移动 `render_count` 不变。

hover/active 样式最终如何变成像素——`compute_style_internal` 的合成顺序：

```rust
// src/elements/div.rs:3321-3337（hover 部分）
if let Some(hover_style) = self.hover_style.as_ref() {
    let is_hovered = if let Some(hitbox) = hitbox {
        hitbox.is_hovered(window)
    } else if let Some(element_state) = element_state.as_ref() {
        element_state
            .hover_state
            .as_ref()
            .map(|state| state.borrow().element)
            .unwrap_or(false)
    } else {
        false
    };

    if is_hovered {
        style.refine(hover_style);
    }
}
```

[div.rs:3301-3338](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L3301-L3338) —— 有 hitbox（paint 阶段）用实时判定；无 hitbox（request_layout/prepaint 阶段，hitbox 尚未插入）回退到 hover_state 记忆值。这就是「同一帧内布局用记忆、绘制用实时」的双轨设计。

```rust
// src/elements/div.rs:3371-3387（active 部分）
if let Some(element_state) = element_state {
    let clicked_state = element_state
        .clicked_state
        .get_or_insert_with(Default::default)
        .borrow();
    if clicked_state.group
        && let Some(group) = self.group_active_style.as_ref()
    {
        style.refine(&group.style)
    }

    if let Some(active_style) = self.active_style.as_ref()
        && clicked_state.element
    {
        style.refine(active_style)
    }
}
```

[div.rs:3371-3387](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L3371-L3387) —— active 样式完全依赖 `clicked_state`（元素状态），再次解释了为何 `.active()` 在 `StatefulInteractiveElement` 上。合成顺序 base → focus → hover → active 与 u3-l3 的结论呼应：**active 恒胜 hover，hover 恒胜 base**。

派发端回顾（u5-l1 已讲，这里看列表消费的代码形态）：

```rust
// src/window.rs:5195-5218（节选）
let mut mouse_listeners = mem::take(&mut self.rendered_frame.mouse_listeners);

// Capture phase, events bubble from back to front. Handlers for this phase are used for
// special purposes, such as detecting events outside of a given Bounds.
for listener in &mut mouse_listeners {
    let listener = listener.as_mut().unwrap();
    listener(event, DispatchPhase::Capture, self, cx);
    if !cx.propagate_event {
        break;
    }
}

// Bubble phase, where most normal handlers do their work.
if cx.propagate_event {
    for listener in mouse_listeners.iter_mut().rev() {
        ...
    }
}
```

[window.rs:5195-5218](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L5195-L5218) —— 注册顺序即 paint 顺序：注意本节第一段引文里 `paint_mouse_listeners` 在 `f(&style, window, cx)`（绘制子元素）**之前**执行，所以父元素的监听器先于子元素注册。于是正序遍历使先注册的（视觉底层/外层）先收到 Capture，逆序遍历使后注册的（视觉上层/更深的子元素）先进入 Bubble 处理——「子元素优先响应、父元素兜底」的语义正是这样实现的。

滚动监听作为收尾——`should_handle_scroll` 的用武之地：

```rust
// src/elements/div.rs:3203-3207（paint_scroll_listener 内）
window.on_mouse_event(move |event: &ScrollWheelEvent, phase, window, cx| {
    if phase == DispatchPhase::Bubble && hitbox.should_handle_scroll(window) {
        let mut scroll_offset = scroll_offset.borrow_mut();
        let old_scroll_offset = *scroll_offset;
        let mut delta = event.delta.pixel_delta(line_height);
```

[div.rs:3203-3248](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L3203-L3248) —— 滚动用 `should_handle_scroll` 而非 `is_hovered`（4.3 讲过原因：浮层下面的容器仍应可滚）；偏移变化才 `cx.notify(current_view)`（[div.rs:3245-3247](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L3245-L3247)），静止悬停不无谓重绘。

#### 4.4.4 代码实践

**实践目标**：跟踪一次点击的完整链路，并验证「按下即重绘」。

1. 打开 `examples/opacity.rs`，在 `start_animation`（[opacity.rs:54-59](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/opacity.rs#L54-L59)）开头加一行 `println!("click: {:?} / {:?}", e.mouse().map(|m| m.down.position), e.mouse().map(|m| m.up.position));`（示例代码；`ClickEvent::mouse()` 返回 `Option<&MouseClickEvent>`，键盘触发时为 None）。
2. 运行示例，点击红色边框面板，观察打印的 down/up 坐标一致（同元素按下抬起）。
3. 在面板上按下、拖出窗口外（超过 2 像素）、松开——观察**没有**打印：拖拽阈值使这次手势不再算点击。
4. 给面板加 `.active(|s| s.border_color(gpui::green()))`（示例代码），按下不放观察边框立即变绿、松开恢复——对应上面 mouse_down 记录器里 `window.refresh()` 的效果。
5. **预期结果**：1-4 均可在本地复现；若某步现象不同，记录实际行为并与本节源码对照定位差异。

#### 4.4.5 小练习与答案

**练习 1**：为什么监听器不做成「注册一次永久有效」，而要每帧重注册？

**答案**：元素树是立即模式的：每帧的元素可能完全不同（条件渲染、列表增删、位置变化）。每帧重注册让闭包捕获的 hitbox、bounds、实体句柄天然对齐当前帧，杜绝「回调引用了已不存在的元素」这类陈旧性问题；代价是每帧重建闭包的分配成本，GPUI 用帧级 bump 分配（u4-l3）消化它。这也解释了 `on_mouse_event` 文档里 "When the next frame is rendered the listener will be cleared"。

**练习 2**：mouse down 记录器为什么要调 `window.refresh()` 而不是 `cx.notify(view)`？

**答案**：`refresh()` 强制下一帧忽略所有视图缓存全量重绘（u4-l3 讲过 `refreshing` 标志），因为按下状态影响的是**样式合成**而非实体数据——若视图被 `.cached()` 缓存，普通 notify 可能命中缓存重放而不重新 compute_style。click 状态机涉及 `pending_mouse_down`/`clicked_state` 的及时反馈，refresh 是最稳妥的失效方式。代价较高，所以只在状态翻转时调用。

**练习 3**：`opacity.rs` 里无 id 的 emoji div 用 `.hover()`，悬停高亮一定及时出现吗？

**答案**：不确定。hover 样式在 paint 时由 `hitbox.is_hovered()` 实时判定（本节 `compute_style_internal` 引文），所以**只要有一帧被画出来**样式就是对的；但「悬停进入/离开该触发重绘」依赖 hover_state 监听器的 `cx.notify`，而 hover_state 只存在于有 id 的元素的 `InteractiveElementState` 里（[div.rs:2721-2726](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L2721-L2726) 里 `element_state` 为 None 时 `hover_state` 也为 None）。若视图内没有任何 id 元素或其他失效源，悬停转移可能不触发重绘，高亮要等下一次无关重绘才浮现。结论：**hover 样式想可靠生效，给元素带上 id**（或保证视图里有别的重绘驱动力，如该示例动画运行时的 `request_animation_frame`）。此行为推断自源码，建议按第 5 节综合实践第 4 步本地验证。

## 5. 综合实践

**任务**：实现一个 4×4 按钮网格——每个按钮 hover 高亮、按下变色、点击计数递增；再对比去掉 `.id()` 后的行为差异。这是本讲四个最小模块的完整串联。

**准备**：复制 `examples/opacity.rs` 为 `examples/button_grid.rs`（去掉其中的 img/svg 部分，只保留骨架），并在 `Cargo.toml` 的 `[[example]]` 列表末尾追加：

```toml
[[example]]
name = "button_grid"
path = "examples/button_grid.rs"
```

**核心代码**（示例代码，非仓库原有）：

```rust
use gpui::{App, Bounds, Context, Window, WindowBounds, WindowOptions, div, prelude::*, px, size};
use gpui_platform::application;

struct ButtonGrid {
    counts: Vec<u32>, // 16 个按钮的点击计数
}

impl ButtonGrid {
    fn increment(&mut self, index: usize, _: &gpui::ClickEvent, _: &mut Window, cx: &mut Context<Self>) {
        self.counts[index] += 1;
        cx.notify();
    }
}

impl Render for ButtonGrid {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        div().size_full().flex_col().gap_2().p_4().children((0..16).map(|i| {
            let count = self.counts[i];
            div()
                .id(("btn", i))                       // NamedInteger 形式的唯一 id —— 一切状态交互的前提
                .flex_1()
                .flex()
                .items_center()
                .justify_center()
                .bg(gpui::blue().opacity(0.3))
                .hover(|s| s.bg(gpui::blue().opacity(0.6)))   // 悬停高亮
                .active(|s| s.bg(gpui::green().opacity(0.8))) // 按下变色
                .on_click(cx.listener(move |this, event, window, cx| {
                    this.increment(i, event, window, cx)
                }))
                .child(format!("{}: {}", i, count))
        }))
    }
}

fn main() {
    application().run(|cx: &mut App| {
        let bounds = Bounds::centered(None, size(px(400.), px(400.)), cx);
        cx.open_window(
            WindowOptions { window_bounds: Some(WindowBounds::Windowed(bounds)), ..Default::default() },
            |_, cx| cx.new(|_| ButtonGrid { counts: vec![0; 16] }),
        )
        .unwrap();
        cx.activate(true);
    });
}
```

**操作步骤与观察点**：

1. `cargo run -p gpui --example button_grid`，确认网格渲染。
2. **hover**：鼠标扫过按钮，背景在 0.3 与 0.6 透明度间切换；对照 4.4.3——转移触发 `cx.notify`，停留不触发。
3. **active**：按住按钮变绿（mouse_down 记录器的 `window.refresh()` 立即兑现），松开恢复并计数 +1。
4. **id 对比实验**：把 `.id(("btn", i))` 改成 `.id("btn")`（16 个按钮共用一个 id），观察计数互相串扰——元素状态表键冲突的直接后果。
5. **无 id 实验**：删掉 `.id(...)` 行，保留 `.on_click`：编译失败（StatefulInteractiveElement 不可用）；再退一步只保留 `.hover`/`.active` 中的 hover（active 同样需要 id，删掉）：编译通过，但观察悬停转移时高亮是否总能及时刷新——对照 4.4.5 练习 3 的源码分析，记录本地实际行为。
6. **预期结果**：步骤 2、3、4 的行为可稳定复现；步骤 5 的运行期表现取决于视图内是否存在其他重绘驱动力，如与推断不符请以本地现象为准并回源码核对。

## 6. 本讲小结

- div 的全部交互能力汇聚在 `Interactivity` 结构体：身份（element_id）、样式补丁（hover/active/focus…）、监听器（各类 `Vec<Box<dyn Fn>>`）、行为标志（hitbox_behavior 等）；`InteractiveElement` trait 只要求交出它的可_mut引用。
- `InteractiveElement`（无状态）与 `StatefulInteractiveElement`（有状态）以 `.id() → Stateful<E>` 为界：需要跨帧记忆的交互（on_click、active、on_hover、滚动）被类型系统挡在无 id 元素之外；`ElementId` 是窗口元素状态表的键的材料，完整键是沿树的 id 路径。
- hitbox 在 prepaint 阶段插入，是「几何 + 拓扑 + 遮挡」三合一的基础设施：逆序命中测试让视觉上层优先，`BlockMouse`/`BlockMouseExceptScroll` 实现浮层遮挡与滚动穿透，键盘模态会抑制 `is_hovered`。
- 监听器在 paint 阶段由 `paint_mouse_listeners` 从 Interactivity `drain` 出来、经 `window.on_mouse_event` 注册进 `next_frame.mouse_listeners`，**只活一帧**——每帧重申报换取零陈旧引用。
- click 是合成事件：Bubble 阶段记录 pending mouse down（伴随 `window.refresh()` 使 active 样式立即生效）→ Capture 阶段配对取出 → Bubble 阶段组装 `ClickEvent` 触发回调；按下与抬起不在同一元素、或移动超过 2 像素拖拽阈值，都不构成点击。
- hover/active 样式在 `compute_style_internal` 中按 base → focus → hover → active 顺序 refine 合成；paint 阶段用 hitbox 实时判定，布局/预绘制阶段回退到元素状态里的记忆值；悬停转移触发重绘依赖有 id 的 hover_state。

## 7. 下一步学习建议

本讲搞定了「鼠标如何唤醒元素」。下一讲 **u5-l3（Action 体系）** 把交互从鼠标扩展到键盘：`actions!` 宏、`ActionRegistry` 与 `on_action` 的标准写法——注意 `on_action` 同样是 paint 阶段注册的监听器（本讲 `paint_keyboard_listeners` 的邻居），你会发现同一套「每帧重申报」哲学。之后 **u5-l4（键位派发链路）** 讲 Keymap/KeyContext/DispatchTree 如何把按键路由到正确的 on_action——其中 `key_context()`（本讲 4.1 出现过的 `Interactivity` 字段）正是那条链路的起点。若想先横向巩固，建议重读 [examples/scrollable.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/scrollable.rs#L10-L44) 的嵌套滚动（两个方向的 `overflow_scroll` 如何靠 hitbox 的 `should_handle_scroll` 区分），以及 div.rs 文末的三个 hover 相关测试（[div.rs:4297-4409](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/elements/div.rs#L4297-L4409)），它们是本讲结论的可执行版。
