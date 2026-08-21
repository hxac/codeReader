# RenderOnce 与组件化复用

## 1. 本讲目标

学完本讲，你应该能够：

1. 准确区分 `Render` 与 `RenderOnce`：前者是「有状态实体视图」的渲染协议，拿 `&mut self` 和 `Context<Self>`；后者是「无状态组件」的渲染协议，按值拿走 `self`，只有 `&mut App`。
2. 会用 `#[derive(IntoElement)]` 宏把自己的组件变成可以直接塞进 `div().child(...)` 的元素，并能说出这个宏到底生成了什么代码。
3. 理解两者的性能差异：组件每帧跟随父视图重建，没有身份也没有缓存；实体视图可以靠 `cx.notify()` 精确重绘，还能用 `Entity::cached` 显式缓存整棵子树。
4. 掌握 GPUI 官方推荐的「组件模式」三件套：props 结构体 + `new()` 构造函数 + `impl RenderOnce`，并能判断一个需求该用组件还是该用视图。

## 2. 前置知识

本讲建立在 u3-l1（Render 与视图）和 u3-l2（div 与样式 API）之上，先把两个旧结论串起来，再引出本讲的主角。

**视图 = 实现了 Render 的实体（复习）。** u3-l1 讲过：GPUI 没有独立的「View 类」，任何实体只要实现 `Render` trait 就是视图。`render(&mut self, window, cx: &mut Context<Self>)` 每帧被调用，从实体当前状态生成一棵元素树。视图有 `EntityId`，`cx.notify()` 能把它标脏、只重绘它的子树。

**元素树每帧重建（复习）。** u1-l1 讲过 GPUI 是「混合立即/保留模式」：元素树是立即模式的，每帧从根视图重建、帧末丢弃；应用状态是保留模式的，存放在跨帧存活的实体里。

**新问题：重复的元素组合怎么办？** 写界面时你会很快发现自己在反复写同样的 `div` 组合——一张卡片是「标题行 + 正文 + 边框 + 圆角」，一个列表行是「24 个单元格横排」。直接复制粘贴这些链式调用会让代码失控。GPUI 的官方答案写在 [src/element.rs:28-32](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L28-L32) 的模块文档里（中文意译）：

> 大多数时候你不需要实现自己的元素。GPUI 提供了大量内置元素覆盖常见场景，推荐用它们构造**组件（components）**——使用 `RenderOnce` trait 和 `#[derive(IntoElement)]` 宏。只有需要手动接管布局与绘制过程时（比如自定义布局算法、渲染代码编辑器）才去实现 Element。

本讲就是把这半句话展开成完整的一课。**组件（component）** 在 GPUI 语境里指「元素组合的配方」：一个普通的 Rust 结构体，字段就是配方参数（前端常称 props），实现了 `RenderOnce` 之后，每个实例在被布局时展开成一棵现成的元素子树。**按值构造（by value）** 是关键词——组件接收所有权，用完即弃，下一帧重新来过。

**ChildElement 的进入门槛（复习）。** u3-l2 讲过 `div().child(...)` 接收任何 `impl IntoElement` 的东西。字符串、`div()`、`svg()` 之所以能当孩子，是因为它们都实现了 `IntoElement` trait。本讲要回答的问题就是：我自己写的结构体如何跨过这道门槛。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/element.rs`（节选） | `Render`、`RenderOnce`、`IntoElement`、`ParentElement` 四个 trait 的定义，以及「推荐用组件而非自定义元素」的模块文档 |
| `src/view.rs`（节选） | `View` 统一 trait（`Render` 与 `RenderOnce` 在此合流）、`ViewElement` 包装元素的两条执行路径、`Entity::cached` 视图缓存 |
| `crates/gpui_macros/src/derive_into_element.rs` | `#[derive(IntoElement)]` 的全部实现，只有 24 行——本讲最重要的源码反而最短 |
| `crates/gpui_macros/src/gpui_macros.rs`（节选） | 过程宏的入口声明 `#[proc_macro_derive(IntoElement)]` |
| `examples/view_example/view_example_main.rs` | `CursorReadout`：一个读取实体数据的无状态组件，「两个视图共享一个实体、零接线」的示范 |
| `examples/data_table.rs` | `TableRow`：万行表格的行组件，展示组件模式在虚拟化列表里的大规模应用 |

> 提示：前两个文件在 `gpui` crate 里，宏文件在旁边的 `gpui_macros` crate 里。用 `pub use` 反查定义位置是 u1-l3 教过的基本功——`#[derive(IntoElement)]` 里的 `IntoElement` 与 trait `IntoElement` 同名但不同物，一个在宏命名空间、一个在类型命名空间。

## 4. 核心概念与源码讲解

### 4.1 RenderOnce：把「元素组合的配方」封装成类型

#### 4.1.1 概念说明

对比两个 trait 的签名，一行就能看出本质区别：

- `Render` 是**实体视图**的协议：`render(&mut self, window, cx: &mut Context<Self>)`。它拿的是可变引用，说明这个对象每帧都在、下一帧还要用；它拿得到 `Context<Self>`，说明它可以 `cx.notify()`、`cx.emit()`、`cx.listener()`——它是响应式图里的一个节点。
- `RenderOnce` 是**无状态组件**的协议：`render(self, window, cx: &mut App)`。它按值拿走 `self`——这个对象用这一次就没了；它只拿得到 `&mut App`（所有上下文的根，u2-l3），拿不到 `Context<Self>`，因为它背后没有实体、没有 `EntityId`、没有身份。

官方文档对 `RenderOnce` 的定位写得很清楚（[src/element.rs:174-179](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L174-L179)，中文意译）：

> 可以在任何实现了这个 trait 的类型上 derive `IntoElement`。它用来把**纯数据**构造成可复用的**组件**。把组件理解为「某种元素模式的配方」，`RenderOnce` 让你调用这个模式，同时不破坏元素 API 的链式构建风格。

「不破坏链式构建风格」值得强调：组件的构造函数 `Card::new(...)` 本身就是链式的，返回 `Self`，可以继续接任何样式方法吗？不行——`Card` 不是 `div`，没有样式方法。它的链式体现在「组件作为孩子嵌入父元素的链」上：`div().child(Card::new(...).whatever())` 这种组合不打断外层 `div` 的链。

**「无状态」不等于「不能读状态」。** 这是最容易误解的一点。组件自己不拥有跨帧状态，但它可以持有 `Entity<T>` 句柄当字段，在 `render` 里 `read(cx)` 读别人家的状态。本讲源码地图里的 `CursorReadout` 就是这么干的。区别在于：它读到的数据变了，不会有人自动通知它重绘——必须由**持有数据的实体**（或它的观察者）触发重绘，组件只是被动地「下次被渲染时读最新值」。

#### 4.1.2 核心流程

一帧之内，一个组件从构造到消失的完整旅程：

```text
父视图 render() 被调用
  │
  ├─ Card::new(title, body, color)      ← 构造组件：把数据按值移进去（纯数据搬运，便宜）
  │
  └─ div().child(card)                  ← child 要求 impl IntoElement
        │
        ├─ into_element()               ← 由 derive 宏生成：包成 ViewElement<Card>
        │
        └─ into_any()                   ← 类型擦除成 AnyElement，装入 div 的孩子列表
              │
父元素树构建完毕，进入布局阶段
  │
  └─ ViewElement::request_layout
        │
        └─ RenderOnce::render(self, ..) ← 组件在此刻展开：返回真正的 div 子树
              │
              └─ 子树继续参与 Taffy 布局 → prepaint → paint
                    │
帧末：整棵元素树连同组件实例、所有回调一起被丢弃
下一帧：从父视图 render() 开始，一切重来
```

要点是「构造」与「展开」是分离的两步：`Card::new(...)` 只是打包数据，`RenderOnce::render` 才在布局阶段把配方展开成元素。中间隔着 `ViewElement` 这层包装，它是组件世界与元素世界之间的桥（4.2 节精读）。

#### 4.1.3 源码精读

**两个渲染协议并排看。** [src/element.rs:161-184](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L161-L184) 连续定义了 `Render` 和 `RenderOnce`：

```rust
pub trait Render: 'static + Sized {
    /// Render this view into an element tree.
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement;
}

/// You can derive [`IntoElement`] on any type that implements this trait.
/// It is used to construct reusable `components` out of plain data...
pub trait RenderOnce: 'static {
    /// Render this component into an element tree. Note that this method
    /// takes ownership of self, as compared to [`Render::render()`] method
    /// which takes a mutable reference.
    fn render(self, window: &mut Window, cx: &mut App) -> impl IntoElement;
}
```

四个差异逐个记：`&mut self` 对 `self`（引用对所有权）；`Context<Self>` 对 `App`（专属上下文对根上下文）；`Sized` 对 `'static`（视图要 Sized 因为通过实体访问，组件要 `'static` 因为要被擦除存放）；文档注释明确说组件由「plain data（纯数据）」构成。

**View trait：两条协议在 view.rs 合流。** `Render` 和 `RenderOnce` 看似平行，其实共用一套下游机制。统一者是 `View` trait（[src/view.rs:182-195](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/view.rs#L182-L195)）：

```rust
pub trait View: 'static + Sized {
    /// This view's identity, if it has one...
    fn entity_id(&self) -> Option<EntityId>;

    /// Render this view into an element tree, consuming `self`.
    fn render(self, window: &mut Window, cx: &mut App) -> impl IntoElement;
}
```

接着是两个 blanket impl（自动覆盖所有实现者的通用实现）。第一个把**所有** `RenderOnce` 类型变成无身份的 View（[src/view.rs:197-207](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/view.rs#L197-L207)）：

```rust
/// A stateless component (`RenderOnce`) is a `View` with no identity.
impl<T: RenderOnce> View for T {
    fn entity_id(&self) -> Option<EntityId> {
        None
    }

    #[inline]
    fn render(self, window: &mut Window, cx: &mut App) -> impl IntoElement {
        RenderOnce::render(self, window, cx)
    }
}
```

`entity_id()` 永远返回 `None`——这就是「无身份」的正式定义。第二个把实现了 `Render` 的实体变成有身份的 View（[src/view.rs:210-221](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/view.rs#L210-L221)）：

```rust
/// An entity that renders itself (`Render`) is a `View` keyed on its own id.
impl<T: Render> View for Entity<T> {
    fn entity_id(&self) -> Option<EntityId> {
        Some(Entity::entity_id(self))
    }

    #[inline]
    fn render(self, window: &mut Window, cx: &mut App) -> impl IntoElement {
        self.update(cx, |this, cx| {
            Render::render(this, window, cx).into_any_element()
        })
    }
}
```

注意实体路径的 `render` 里调了 `self.update(cx, ...)`——这正是 u2-l2 讲过的「租约」：渲染视图必须先从 `EntityMap` 租出状态。组件路径完全没有这一步，因为它就是栈上/父元素字段里的一个普通值。`View` trait 的文档（[src/view.rs:171-181](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/view.rs#L171-L181)）还说明了身份的实际后果：有 id 的视图获得独立的元素 id 命名空间（内部 `use_state`、`.id(...)` 不会在兄弟之间撞车），且 `cx.notify()` 只重绘这个视图的子树；`None` 就按无状态组件对待。

**实例：CursorReadout，读实体数据的无状态组件。** [examples/view_example/view_example_main.rs:40-59](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/view_example/view_example_main.rs#L40-L59)：

```rust
/// A tiny stateless view that reads an editor's cursor and is composed *beside*
/// the thing editing it — two views over one entity, zero wiring.
#[derive(IntoElement)]
struct CursorReadout {
    editor: Entity<Editor>,
}

impl CursorReadout {
    fn new(editor: Entity<Editor>) -> Self {
        Self { editor }
    }
}

impl gpui::RenderOnce for CursorReadout {
    fn render(self, _window: &mut Window, cx: &mut App) -> impl IntoElement {
        let cursor = self.editor.read(cx).cursor;
        div()
            .text_sm()
            .text_color(hsla(0., 0., 0.45, 1.))
            .child(SharedString::from(format!("cursor @ {cursor}")))
    }
}
```

这是组件模式的标准形态，四个组成部分一个不少：

1. **props 结构体**：`#[derive(IntoElement)] struct CursorReadout { editor: Entity<Editor> }`——字段就是参数，这里参数是一个实体句柄（印证 4.1.1 的「无状态可读状态」）。
2. **构造函数**：`new(editor) -> Self`，与链式 API 无缝衔接。
3. **渲染实现**：`render(self, ...)` 按值拿走自己，`self.editor.read(cx)` 读出光标位置，然后返回一小棵 `div` 子树。注意 `format!` 的结果用 `SharedString` 包装——u3-l5 讲过的零拷贝字符串句柄，虽然字符串本身每帧新建，但这是组件式的「轻量重建」哲学：不要为了省这点分配把组件升级成实体。
4. **使用处**（[view_example_main.rs:96-104](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/view_example/view_example_main.rs#L96-L104)）：`Input::editor(owned.clone()).width(px(320.))` 和 `CursorReadout::new(owned)` 并排做兄弟——同一个 `owned` 实体，一个负责编辑、一个负责展示光标，组件自己不用订阅任何东西。注释里的「two views over one entity, zero wiring」就是这个意思：输入框改了实体状态、触发重绘，下一帧 `CursorReadout::render` 重新执行、读到新光标。

顺带说明例子里的一个背景细节：根视图用 `window.use_state(cx, ...)` 创建状态（[view_example_main.rs:72-77](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/view_example/view_example_main.rs#L72-L77)），这是一个按调用位置生成 ID、让状态「随元素连续渲染而存活」的便捷 API（文档见 [src/window.rs:3748-3754](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L3748-L3754)）。它属于实体状态话题，本讲只需要知道它返回 `Entity<T>`，因此能被组件当 props 持有。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`RenderOnce` 是协议、`#[derive(IntoElement)]` 是入场景」这一分工——删掉宏，协议还在，但组件进不了 `child(...)`。

**操作步骤**：

1. 运行示例，先看正确行为：

   ```bash
   cargo run -p gpui --example view_example
   ```

2. 在第三张卡片（「Input — from an Editor」区）旁边的灰色小字就是 `CursorReadout` 的输出。在输入框里打字、按左右方向键，观察 `cursor @ N` 实时变化——注意组件没有任何 `observe`/`subscribe` 代码，刷新是被「编辑器实体 notify → 根视图重绘 → 组件重新展开 → 重新读取」这条链带动的。
3. 把 [view_example_main.rs:40](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/view_example/view_example_main.rs#L40) 的 `#[derive(IntoElement)]` 这一行注释掉，然后：

   ```bash
   cargo check -p gpui --example view_example
   ```

4. 阅读编译错误（关注错误指向的 trait bound 与代码位置：`CursorReadout::new(owned)` 处的 `child` 调用）。看完后恢复宏，确认编译通过。

**需要观察的现象**：

- 第 2 步里 `cursor @ N` 随按键更新，且组件源码里没有任何订阅代码。
- 第 3 步里编译失败。`ParentElement::child` 的签名要求 `child: impl IntoElement`，而全 crate 搜索（见 4.2.3 的 grep 证据）确认不存在 `impl<T: RenderOnce> IntoElement for T` 这样的 blanket impl——唯一的桥梁就是 derive 宏生成的那个 impl。

**预期结果**：错误信息形如「the trait bound `CursorReadout: IntoElement` is not satisfied」并指向 `.child(CursorReadout::new(owned))`；具体措辞随编译器版本略有差异（待本地验证）。恢复宏后一切照旧。

#### 4.1.5 小练习与答案

**练习 1**：`RenderOnce::render` 拿 `self` 而不是 `&mut self`，这个设计上的差别暗示了组件生命周期的什么事实？

<details>
<summary>参考答案</summary>

按值接收意味着组件实例在一次渲染中被消耗（`ViewElement` 内部用 `Option<V>` 存它，`render` 时 `take()` 拿走，见 4.2.3 的源码）。它不跨帧存活——帧末整棵元素树连同组件一起丢弃，下一帧由父视图重新构造、重新展开。也因此它不需要 `&mut self`：没有「下一次调用」需要保留可变状态。
</details>

**练习 2**：`CursorReadout` 是无状态组件，但它显示的光标会实时更新。是谁触发了包含它的那部分界面重绘？

<details>
<summary>参考答案</summary>

编辑器实体（`Editor`）。输入或移动光标时 `Editor` 的状态更新会调用 `cx.notify()`（u3-l1 讲过的 notify 链路），把显示该实体的窗口标脏；下一帧根视图 `render` 重新执行，`CursorReadout` 被重新构造与展开，`render` 里 `self.editor.read(cx)` 读到的就是最新光标。组件是「搭便车」的：它自己没有任何刷新机制。
</details>

**练习 3**：如果想让 `CursorReadout` 在光标变化时自己闪烁一下（需要保存「上次闪烁时间」），还能用 `RenderOnce` 吗？

<details>
<summary>参考答案</summary>

不能直接用。跨帧状态要么放进实体（把组件升级为「持有 `Entity<FlickerState>` 的实体视图」，实现 `Render`），要么借用元素级状态机制（如 `use_keyed_state`，它按元素 id 存续）。`RenderOnce` 组件自身没有跨帧存储位置——它的字段每帧都是新构造的。正确做法是：状态归实体，`RenderOnce` 只做展示。
</details>

### 4.2 #[derive(IntoElement)]：组件如何变成可放置的孩子

#### 4.2.1 概念说明

上一节留下的问题：`RenderOnce` 只定义了「如何展开」，没回答「如何进入元素树」。`div().child(x)` 要求 `x: impl IntoElement`，而 `IntoElement` 是个需要实现的 trait：

```rust
/// Implemented by any type that can be converted into an element.
pub trait IntoElement: Sized {
    /// The specific type of element into which the implementing type is converted.
    type Element: Element;

    /// Convert self into a type that implements [`Element`].
    fn into_element(self) -> Self::Element;
}
```

（定义在 [src/element.rs:144-157](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L144-L157)。）

理论上你可以自己写这个 impl，但它是纯模板代码——每个组件的写法一模一样。`#[derive(IntoElement)]` 宏存在的意义就是把这 10 行模板自动生成出来。用一个宏而不是 blanket impl 是有原因的：如果写 `impl<T: RenderOnce> IntoElement for T`，那么**所有** `RenderOnce` 类型都自动成为元素，包括那些你只想当「内部辅助类型」不想暴露出去的；derive 让「能当孩子用」成为显式选择。在 crates/ 目录里实测（`rg 'derive\(…IntoElement…\)' --type rust`）：zed 仓库有 **103 个文件、共 126 处** `#[derive(IntoElement)]`（含与其他 derive 合并书写的形态），它是 zed 自己 UI 代码里最主流的组件封装方式。

宏与 trait 同名不冲突：Rust 的宏和类型住在不同命名空间，`use gpui::IntoElement` 会把两者都引进来。这也解释了两个示例的导入差异——view_example 显式 `use gpui::{IntoElement, ...}`（[view_example_main.rs:27-30](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/view_example/view_example_main.rs#L27-L30)），data_table 只 `use gpui::prelude::*`（[data_table.rs:8](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/data_table.rs#L8)）也能用宏，因为 prelude 里的 `IntoElement`（[src/prelude.rs:5-9](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/prelude.rs#L5-L9)）同时导入了两个命名空间的条目。

#### 4.2.2 核心流程

derive 宏的展开结果可以用一条等式概括：

```text
#[derive(IntoElement)]
struct Card { .. }
        │ 宏展开为
        ▼
impl gpui::IntoElement for Card {
    type Element = gpui::ViewElement<Card>;
    fn into_element(self) -> Self::Element {
        gpui::ViewElement::new(self)
    }
}
```

于是组件进入元素树的完整链条是：

1. `div().child(card)` → `child` 内部调 `card.into_element()`（[src/element.rs:193-199](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L193-L199)）。
2. 得到 `ViewElement<Card>`——它自己实现了 `Element`，于是紧接着 `into_any()` 被类型擦除成 `AnyElement` 装进 div 的孩子列表（[src/element.rs:138-141](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L138-L141)；`AnyElement::new` 把元素分配进帧级元素 arena，见 [src/element.rs:588-599](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L588-L599)，arena 机制 u4-l3 再展开）。
3. 布局阶段轮到这个 `ViewElement` 时，它检查 `entity_id`：`None`（组件）走无身份路径，`Some(id)`（实体视图）走响应式路径。

对照记忆：实体视图也走同一座桥——[src/view.rs:95-101](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/view.rs#L95-L101) 为 `Entity<V: Render>` 手写了几乎一样的 `IntoElement` impl（`type Element = ViewElement<Entity<V>>`）。区别只在宏为每个组件类型生成的 impl 里 `Self` 就是组件本身。

#### 4.2.3 源码精读

**宏的全部实现，24 行。** [gpui_macros/src/derive_into_element.rs:5-24](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macros/src/derive_into_element.rs#L5-L24)（注意这个文件在 `gpui_macros` crate，不在 `gpui`）：

```rust
pub fn derive_into_element(input: TokenStream) -> TokenStream {
    let ast = parse_macro_input!(input as DeriveInput);
    let type_name = &ast.ident;
    let (impl_generics, type_generics, where_clause) = ast.generics.split_for_impl();

    let r#gen = quote! {
        impl #impl_generics gpui::IntoElement for #type_name #type_generics
        #where_clause
        {
            type Element = gpui::ViewElement<Self>;

            #[track_caller]
            fn into_element(self) -> Self::Element {
                gpui::ViewElement::new(self)
            }
        }
    };

    r#gen.into()
}
```

逐行读：

- `ast.generics.split_for_impl()`：把泛型参数拆开，所以**泛型组件也能 derive**（比如 `struct Row<T> { item: T }`），impl 会带上相同的泛型约束。
- `type Element = gpui::ViewElement<Self>`：关联类型指向 `ViewElement`——4.1.3 里那个统一了两种协议的包装元素。这就是「组件」与「视图」共用全部下游机制的技术落点。
- `#[track_caller]`：让 `ViewElement::new` 里记录的构造位置指向**调用者**的代码行而非宏内部，供 inspector 定位元素源码（u7-l5 会见到）。

宏的入口声明在 [gpui_macros/src/gpui_macros.rs:34-37](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_macros/src/gpui_macros.rs#L34-L37)，再经 [src/gpui.rs:109-111](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/gpui.rs#L109-L111) 重导出为 `gpui::IntoElement`（与 trait 同名，见 4.2.1 的命名空间说明）。

**ViewElement 的两条执行路径。** `ViewElement::new` 只做一件事：问一下身份，存起来（[src/view.rs:248-260](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/view.rs#L248-L260)）。真正分岔发生在 `request_layout`（[src/view.rs:314-360](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/view.rs#L314-L360)）。组件走的是 `else` 分支（[src/view.rs:344-359](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/view.rs#L344-L359)）：

```rust
} else {
    // Stateless path: isolate subtree via type name (no entity identity).
    window.with_id(
        ElementId::Name(std::any::type_name::<V>().into()),
        |window| {
            let mut element = self
                .view
                .take()
                .unwrap()
                .render(window, cx)
                .into_any_element();
            let layout_id = element.request_layout(window, cx);
            (layout_id, Some(element))
        },
    )
}
```

四个细节：

1. `self.view.take().unwrap()`：`view` 字段是 `Option<V>`，`take` 拿走组件、同帧内不可能再渲染第二次——这是「按值构造」在源码里的物理形态。
2. `.render(window, cx)`：`View::render` → blanket impl → `RenderOnce::render`，配方在此展开。
3. `window.with_id(ElementId::Name(type_name::<V>()))`：无身份组件用**类型名**做元素 id 命名空间，保证不同类型组件内部的 `use_state`、`.id()` 状态互不串台。注意它按类型隔离而不按实例——两个同类型兄弟组件的内部状态若要各自独立，得靠组件内部显式 `.id()` 区分，这是组件模式的一个坑。
4. 实体路径（`if let Some(entity_id)` 分支）则调 `window.with_rendered_view(entity_id, ...)` 建立「响应式边界」，还能进入缓存逻辑——4.3 节对比两条路径的性能含义。

**child / children：组件进门的最后一公里。** [src/element.rs:186-209](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L186-L209)：

```rust
pub trait ParentElement {
    /// Extend this element's children with the given child elements.
    fn extend(&mut self, elements: impl IntoIterator<Item = AnyElement>);

    /// Add a single child element to this element.
    fn child(mut self, child: impl IntoElement) -> Self ... {
        self.extend(std::iter::once(child.into_element().into_any()));
        self
    }

    /// Add multiple child elements to this element.
    fn children(mut self, children: impl IntoIterator<Item = impl IntoElement>) -> Self ... {
        self.extend(children.into_iter().map(|child| child.into_any_element()));
        self
    }
}
```

`child` 和 `children` 是同一个机制的两个入口：都先把每项 `into_element()`（derive 宏生成的 impl 在这里生效），再 `into_any()` 擦除成 `AnyElement`。`children` 接收任何 `IntoIterator`，所以数组、`Vec`、迭代器链都能直接喂——综合实践会用到。

#### 4.2.4 代码实践

**实践目标**：亲眼看到宏生成的代码，建立「derive 不是魔法」的信心。

**操作步骤**（两种方式，任选）：

1. **宏展开方式**：安装 cargo-expand 后运行（待本地验证，需要 nightly 工具链）：

   ```bash
   cargo expand -p gpui --example data_table | grep -A 8 "impl.*IntoElement for TableRow"
   ```

2. **源码阅读方式**（无需任何工具）：对照 4.2.3 的宏源码，手写展开 `TableRow` 的 impl——把 `#type_name` 换成 `TableRow`、泛型部分留空即可。然后验证你的手写版与 4.2.2 的等式一致。
3. 再做一个小实验：给你手写的 impl 换一个关联类型（比如 `type Element = Div`，`into_element` 返回 `div()`），想想这在语义上错在哪。

**需要观察的现象**：

- 第 1 步的输出应与你的手写展开一致：`impl gpui::IntoElement for TableRow { type Element = gpui::ViewElement<TableRow>; ... }`（待本地验证，输出可能包含大量其他展开噪音）。
- 第 3 步的思想实验：`into_element` 返回 `div()` 意味着所有 `TableRow` 都变成同一个匿名 div——组件的 props（`ix`、`quote`）没有地方存放，`RenderOnce::render` 永远不会被调用，行内容全部丢失。`ViewElement<Self>` 的必要性在于它把组件本体（连同 props）搬进元素世界。

**预期结果**：能不看资料复述「derive 生成了什么、为什么必须是 `ViewElement<Self>`」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 GPUI 用 derive 宏而不是 `impl<T: RenderOnce> IntoElement for T` 一劳永逸？

<details>
<summary>参考答案</summary>

至少两个理由：(1) 显式选择——blanket impl 会让所有 `RenderOnce` 类型自动成为可用孩子，无法只把某个类型当内部辅助；derive 让「作为 UI 组件被放置」成为类型的显式设计意图。(2) 与实体视图对称——`Entity<V: Render>` 的 `IntoElement` impl 是手写的（view.rs:95-101），两种协议各自显式声明入场景，模型更清晰。此外 blanket impl 还可能与用户为同一类型写的其他 `IntoElement` impl 冲突。
</details>

**练习 2**：`impl IntoElement for Div`、`impl IntoElement for Entity<V>`、derive 生成的 `impl IntoElement for Card` 三者的 `type Element` 分别是什么？

<details>
<summary>参考答案</summary>

`Div` 的是自身（`div.rs:1989`，div 自己就是 Element）；`Entity<V>` 的是 `ViewElement<Entity<V>>`（view.rs:95-101）；`Card` 的是 `ViewElement<Card>`（宏生成）。后两者共用 `ViewElement` 这座桥，这正是 View 统一模型（4.1.3）在 `IntoElement` 层的体现。
</details>

**练习 3**：一个 `Vec<Card>` 能直接 `.children(cards)` 吗？依据是什么？

<details>
<summary>参考答案</summary>

能。`children` 的签名是 `impl IntoIterator<Item = impl IntoElement>`，`Vec<Card>` 是 `IntoIterator`、`Item = Card`，而 `Card` 经 derive 实现了 `IntoElement`。甚至数组 `.map(|(t, b, c)| Card::new(t, b, c))` 产生的迭代器也能直接喂给 `children`。
</details>

### 4.3 组件模式与性能：每帧重建 vs 视图缓存

#### 4.3.1 概念说明

现在已经能写组件了，但「什么时候该用组件、什么时候必须上实体视图」需要一张性能与能力的账。先把组件模式的固定套路总结成模板（**示例代码**，综合实践会完整运用）：

```rust
// 1. props 结构体：字段即参数，纯数据（或轻量句柄）
#[derive(IntoElement)]
struct Card {
    title: SharedString,
    body: SharedString,
    accent: Hsla,
}

// 2. 构造函数：链式入口
impl Card {
    fn new(title: impl Into<SharedString>, body: impl Into<SharedString>, accent: Hsla) -> Self {
        Self { title: title.into(), body: body.into(), accent }
    }
}

// 3. 渲染实现：按值展开成元素子树
impl RenderOnce for Card {
    fn render(self, _window: &mut Window, _cx: &mut App) -> impl IntoElement {
        div() /* ...用 self.title / self.body / self.accent 组装样式与内容... */
    }
}
```

**性能账的两边。** 组件这边的成本模型很朴素：每当包含它的父视图重渲染，组件就被重新构造、`render` 重新执行一次，产物是一棵小的 `div` 配置树。构造 `Card` 是几次指针移动；展开是十几次链式调用往 `StyleRefinement` 里填 `Option` 字段（u3-l3 讲过补丁的存储形态）——都便宜，但**每帧都付**。收益是零生命周期管理：不占 `EntityMap` 的 slot、不进观察者表、没有 id 冲突风险。

实体视图那边则是「重一次，省每帧」：视图有身份，`cx.notify()` 只把它的子树标脏（u3-l1 的 dirty_views 机制）；更进一步，`Entity::cached(style)` 可以把整棵渲染子树**缓存**起来，命中时连 `render` 都不执行，直接重放上一帧录制的绘制区间。缓存失效条件在源码里是一个精确的合取式（见 4.3.3），可以写成：

\[
\text{hit} \iff
(\text{bounds} = \text{bounds}') \wedge
(\text{content\_mask} = \text{mask}') \wedge
(\text{text\_style} = \text{style}') \wedge
\neg\,\text{dirty\_views}(id) \wedge
\neg\,\text{refreshing}
\]

四个「不变」加「没被标脏」加「不是强制刷新」，全部成立才复用。

**为什么缓存只对实体视图开放？** 这不是实现偷懒，是逻辑必然。缓存一段渲染结果，前提是存在一个可靠的失效信号在数据变化时把它作废；实体视图的契约恰恰是 `cx.notify()`（u3-l1）。无状态组件没有实体、没有 id、没有 notify，「冻结的子树」将永远不会失效。源码注释把这层因果写得很直白（[src/view.rs:262-274](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/view.rs#L262-L274)）：缓存「crate-private on purpose」，只有实体支撑的视图才 sound，并点名「a stateless view has no such contract, so a frozen subtree could never be invalidated」——所以你只能通过 `Entity::cached` 到达它。

**大规模应用：万行表格。** 组件「每帧重建」的账在大列表里依然划算，关键在于让 props 足够轻。`data_table` 示例用 10000 行数据证明这一点：每行是一个 `TableRow` 组件，props 只有两个字段——行号 `usize` 和 `Rc<Quote>`（引用计数共享，不复制数据）。虚拟化列表（uniform_list，u6-l1 专题）每帧只为**可见**的几十行构造组件，每行的展开成本是一趟 24 个单元格的链式调用。10000 个实体（每个占 EntityMap slot、可被 notify、可被观察）与每帧几十个轻量组件之间，组件模式是明显赢家。

#### 4.3.2 核心流程

面对「组件还是视图」的决策，按下面的顺序问自己：

```text
这个 UI 单元需要满足以下任一条件吗？
  ├─ 自己拥有跨帧可变状态（滚动位置、输入草稿、动画进度……）
  ├─ 需要被 cx.notify() 精确重绘（自身独立变化，不想连累父视图）
  ├─ 需要缓存整棵子树（内容昂贵且变化少，用 Entity::cached）
  └─ 需要接收事件/被订阅（EventEmitter、cx.subscribe）
      │
      ├─ 是任一条 → 实体视图：struct + Entity<T> + impl Render
      │             （状态放实体；展示部分仍可拆成 RenderOnce 组件复用）
      │
      └─ 全否 → RenderOnce 组件：#[derive(IntoElement)] + new() + impl RenderOnce
```

两个方向可以嵌套混用，这正是「三种编程层级按需混用」（u1-l1）的具体体现：根视图是实体（拥有状态），中间的卡片是组件（纯配方），组件里再嵌实体视图（局部交互密集区）完全合法。

#### 4.3.3 源码精读

**TableRow：万行表格的行组件。** props 与构造（[examples/data_table.rs:140-148](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/data_table.rs#L140-L148)）：

```rust
#[derive(IntoElement)]
struct TableRow {
    ix: usize,
    quote: Rc<Quote>,
}
impl TableRow {
    fn new(ix: usize, quote: Rc<Quote>) -> Self {
        Self { ix, quote }
    }
    ...
}
```

字段类型的选择就是性能课：`ix` 是 `Copy` 的整数；`quote` 是 `Rc<Quote>`——注释（[data_table.rs:255-257](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/data_table.rs#L255-L257)）写着「Use `Rc` to share the same quote data across multiple items, avoid cloning」。`Quote` 有 20 多个字段，若按值存放，每行构造都要深拷贝一次。

渲染实现（[examples/data_table.rs:236-253](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/data_table.rs#L236-L253)）：

```rust
impl RenderOnce for TableRow {
    fn render(self, _window: &mut Window, _cx: &mut App) -> impl IntoElement {
        let color = self.quote.change_color();
        div()
            .flex()
            .flex_row()
            .border_b_1()
            .border_color(rgb(0xE0E0E0))
            .bg(if self.ix.is_multiple_of(2) {
                rgb(0xFFFFFF)
            } else {
                rgb(0xFAFAFA)
            })
            .py_0p5()
            .px_2()
            .children(FIELDS.map(|(key, width)| self.render_cell(key, px(width), color)))
    }
}
```

值得学的三个点：斑马纹直接在 `render` 里按 `self.ix` 奇偶算（无状态也能做出「看起来有记忆」的效果，因为数据在 props 里）；24 个单元格经 `.children(FIELDS.map(...))` 一次喂入（4.2.3 讲的 `children` 入口）；涨跌颜色先算一次再传给每个 cell。辅助方法 `render_cell`（[data_table.rs:150-206](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/data_table.rs#L150-L206)）按字段名 match 出对应 `div`——组件内部用普通方法拆分复杂度，不需要任何框架机制。

组件的供给端在 `DataTable` 实体视图的 `render` 里（[examples/data_table.rs:424-445](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/data_table.rs#L424-L445)）：

```rust
uniform_list(
    "items",
    self.quotes.len(),
    cx.processor(move |this, range: Range<usize>, _, _| {
        this.visible_range = range.clone();
        let mut items = Vec::with_capacity(range.end - range.start);
        for i in range {
            if let Some(quote) = this.quotes.get(i) {
                items.push(TableRow::new(i, quote.clone()));
            }
        }
        items
    }),
)
.size_full()
.track_scroll(&self.scroll_handle),
```

读法：`DataTable`（实体，持有全部 10000 条 `Rc<Quote>`）通过 `uniform_list` 的回调只对**可见区间** `range` 构造 `TableRow`；`quote.clone()` 克隆的是 `Rc` 指针。这是一个标准的「实体存数据 + 组件做展示」分层。

**实体视图这边的对照组：Entity::cached。** [src/view.rs:223-235](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/view.rs#L223-L235)：

```rust
impl<T: Render> Entity<T> {
    /// Embed this entity as a cached [`ViewElement`] laid out at `style`.
    ///
    /// The rendered subtree is reused until the entity is notified (or the
    /// cached bounds / text style change). Caching requires a definite size:
    /// a cached view is laid out from `style` and is *not* measured from its
    /// contents. ...
    pub fn cached(self, style: StyleRefinement) -> ViewElement<Entity<T>> {
        ViewElement::new(self).cached(style)
    }
}
```

注意代价条款：缓存视图从给定的 `style` 布局、**不**按内容测量——所以缓存要求你能预先给出确定尺寸（这正是 4.3.1 公式里 bounds 不变条款的源头之一）。缓存命中的判定实现在 prepaint 的实体路径里（[src/view.rs:380-401](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/view.rs#L380-L401)）：

```rust
if let Some(mut element_state) = element_state
    && element_state.cache_key.bounds == bounds
    && element_state.cache_key.content_mask == content_mask
    && element_state.cache_key.text_style == text_style
    && !window.dirty_views.contains(&entity_id)
    && !window.refreshing
{
    let prepaint_start = window.prepaint_index();
    window.reuse_prepaint(element_state.prepaint_range.clone());
    ...
}
```

五个条件逐条对应 4.3.1 的合取式；命中后 `reuse_prepaint` 直接重放上一帧录制的 prepaint 区间，`render` 完全跳过。而组件路径（4.2.3 的 `else` 分支）里没有任何缓存代码——不是「暂时没实现」，是 4.3.1 解释的契约缺失。

#### 4.3.4 代码实践

**实践目标**：用日志亲眼验证两个结论——(a) 组件确实每帧重建；(b) 虚拟化列表下重建的只是可见行。

**操作步骤**：

1. 运行表格示例：

   ```bash
   cargo run -p gpui --example data_table
   ```

2. 在 [data_table.rs:237](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/data_table.rs#L237) 的 `RenderOnce::render` 函数体第一行临时加一句（这是你学习时的本地实验，读完删掉即可）：

   ```rust
   eprintln!("TableRow render: ix={}", self.ix);
   ```

3. 不做任何操作静置几秒，观察终端；然后拖动表格右侧滚动条快速滚动，再观察；最后把鼠标悬停在表格行上（触发 hover 样式）观察。

**需要观察的现象**：

- 静置时日志是否持续打印（预期：不打印——没有 notify 就没有重绘，组件重建是被动的）。
- 滚动时打印的 `ix` 范围：应该只有窗口高度能容纳的几十个连续行号，而不是 0..10000。
- hover 时同一批行号是否重复打印（预期：会——hover 改变样式触发重绘，可见行全部重新展开）。

**预期结果**：三个观察都符合预期即验证了本节账本：组件重建跟着父级重绘走（被动、每帧付一次、范围受虚拟化限制）。若你的机器上帧调度导致 hover 之外的额外打印，记录频率并与同学讨论原因（GPU vsync、窗口管理器重绘策略都可能带来差异，待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`TableRow` 的字段为什么是 `Rc<Quote>` 而不是 `Quote` 或 `Entity<Quote>`？

<details>
<summary>参考答案</summary>

`Quote` 按值则每次构造行组件都深拷贝 20 多个字段，滚动时每帧几十次；`Entity<Quote>` 则每行一个实体，10000 个实体占据 EntityMap slot 并带来生命周期管理成本，而每行数据从不独立变化、也不需要被单独 notify。`Rc` 恰好匹配需求：多行可共享同一份数据（示例注释原话），克隆只是引用计数加一，组件只需读它。
</details>

**练习 2**：为什么 `ViewElement::cached` 是 `pub(crate)`，只通过 `Entity::cached` / `AnyView::cached` 暴露？

<details>
<summary>参考答案</summary>

源码注释（view.rs:266-270）说明：缓存的 soundness 依赖「`Context::notify` 作废缓存」这一契约，只有实体支撑的视图能提供；无状态组件没有失效信号，冻结的子树永远不会更新。把构造入口限定在 `Entity`/`AnyView`（它们必然实体支撑）在类型层面杜绝了误用。
</details>

**练习 3**：一个每秒变一次的时钟组件，用 `RenderOnce` 实现有什么问题？给出最小改造方案。

<details>
<summary>参考答案</summary>

问题：谁来触发重绘？`RenderOnce` 组件没有实体、不能 `cx.notify()`，每秒的 `Timer` 完成后没有任何机制把包含它的子树标脏，界面会停在第 0 秒。最小改造：让某个实体视图做宿主——例如根视图在 `render` 里 `cx.spawn` 一个循环任务，每次 `timer(1s)` 后 `cx.notify()`（u2-l5 的前台任务模式）；时钟组件本身仍可以是 `RenderOnce`，每帧被宿主的重绘带着重新读取时间并展开。状态与刷新归实体、展示归组件，正是 4.3.2 决策流程的结论。
</details>

## 5. 综合实践

把 u3-l2 里练过的「卡片布局」正式组件化：写一个 `Card` 组件接收标题、内容、颜色三个参数，父视图用 `.children()` 一次渲染五张不同参数的卡片。完成后你将完整走过「props 设计 → derive → 展开 → 批量放置」的全流程。

**实践目标**：独立完成一个可复用组件，并体会「父视图一行、子内容全在组件里」的封装收益。

**操作步骤**：

1. 在 `examples/` 下新建 `card_grid.rs`，写入以下完整代码（**示例代码**，基于 hello_world 的三段式骨架与 view_example 的组件写法）：

   ```rust
   #![cfg_attr(target_family = "wasm", no_main)]

   use gpui::{
       App, Bounds, Context, Hsla, IntoElement, Render, RenderOnce, SharedString, Window,
       WindowBounds, WindowOptions, div, hsla, prelude::*, px, rgb, size,
   };
   use gpui_platform::application;

   /// 卡片组件的「配方」：三个 props 全部按值接收。
   #[derive(IntoElement)]
   struct Card {
       title: SharedString,
       body: SharedString,
       accent: Hsla,
   }

   impl Card {
       fn new(
           title: impl Into<SharedString>,
           body: impl Into<SharedString>,
           accent: Hsla,
       ) -> Self {
           Self {
               title: title.into(),
               body: body.into(),
               accent,
           }
       }
   }

   impl RenderOnce for Card {
       fn render(self, _window: &mut Window, _cx: &mut App) -> impl IntoElement {
           div()
               .flex()
               .flex_col()
               .gap(px(4.))
               .p(px(12.))
               .w(px(180.))
               .rounded_md()
               .bg(rgb(0xffffff))
               .border_1()
               .border_color(self.accent)
               .child(div().text_sm().text_color(self.accent).child(self.title))
               .child(
                   div()
                       .text_xs()
                       .text_color(rgb(0x555555))
                       .child(self.body),
               )
       }
   }

   struct CardGrid;

   impl Render for CardGrid {
       fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
           let cards = [
               ("组件", "RenderOnce 按值接收 self", hsla(0.0, 0.7, 0.5, 1.)),
               ("身份", "entity_id 永远返回 None", hsla(0.11, 0.7, 0.5, 1.)),
               ("宏", "derive 生成 IntoElement", hsla(0.22, 0.7, 0.45, 1.)),
               ("桥", "ViewElement 打包进元素树", hsla(0.55, 0.7, 0.5, 1.)),
               ("缓存", "仅限实体视图的专利", hsla(0.78, 0.7, 0.5, 1.)),
           ];
           div()
               .flex()
               .flex_col()
               .size_full()
               .bg(rgb(0xf0f0f0))
               .p(px(24.))
               .gap(px(12.))
               .children(cards.map(|(title, body, accent)| Card::new(title, body, accent)))
       }
   }

   fn run_example() {
       application().run(|cx: &mut App| {
           let bounds = Bounds::centered(None, size(px(560.0), px(480.0)), cx);
           cx.open_window(
               WindowOptions {
                   window_bounds: Some(WindowBounds::Windowed(bounds)),
                   ..Default::default()
               },
               |_, cx| cx.new(|_| CardGrid),
           )
           .unwrap();
           cx.activate(true);
       });
   }

   #[cfg(not(target_family = "wasm"))]
   fn main() {
       run_example();
   }

   #[cfg(target_family = "wasm")]
   #[wasm_bindgen::prelude::wasm_bindgen(start)]
   pub fn start() {
       gpui_platform::web_init();
       run_example();
   }
   ```

2. 运行（cargo 会自动发现 `examples/` 下的新文件；若你的环境未自动发现，可在 `Cargo.toml` 补一条 `[[example]]`，或直接把代码粘进 `hello_world.rs`）：

   ```bash
   cargo run -p gpui --example card_grid
   ```

3. 五张卡片将纵向排列，左边框颜色各不相同。做三个小改造并观察：
   - 把 `CardGrid::render` 里 `.children(cards.map(...))` 改成 `.child(Card::new(...))` 只放一张，验证两种放置入口；
   - 给 `Card` 加第四个 props（如 `selected: bool`），在 `render` 里用 u3-l2 讲过的 `.when(self.selected, |this| this.border_2())` 做条件样式；
   - 故意删掉 `#[derive(IntoElement)]`，跑 `cargo check -p gpui --example card_grid` 读编译错误（对照 4.1.4），再恢复。

**需要观察的现象**：

- 五张卡片共用同一份 `render` 逻辑，但标题、正文、颜色各异——「配方」被实例化五次；
- 第 3 步条件样式只在 `selected: true` 的卡片上生效；
- 删宏后编译失败，错误指向 `.children(cards.map(...))` 处的 trait bound。

**预期结果**：窗口显示五张彩色边框卡片；三个改造全部符合预期。运行效果待本地验证。

## 6. 本讲小结

- `Render` 与 `RenderOnce` 是两种渲染协议：前者属于有状态实体视图（`&mut self` + `Context<Self>`，有 `EntityId`），后者属于无状态组件（按值 `self` + `&mut App`，无身份）；两者在 `View` trait 上合流，共用 `ViewElement` 这座进入元素树的桥。
- 组件是「元素组合的配方」：props 结构体 + `new()` + `impl RenderOnce` 三件套；无状态不等于不能读状态——组件可持有 `Entity` 句柄，在每次展开时读取最新值（`CursorReadout` 范式）。
- `#[derive(IntoElement)]` 只生成一个 10 行的模板 impl：`type Element = ViewElement<Self>`、`into_element` 调 `ViewElement::new(self)`。没有 blanket impl，derive 是唯一的显式入场景；zed 仓库里有 103 个文件在使用它。
- `ViewElement` 按身份分岔：`None`（组件）走 `with_id(type_name)` 的无身份路径，每帧 `take()` 组件并展开一次；`Some(id)`（实体视图）建立响应式边界，可进入缓存。
- 性能账：组件每帧随父视图重建（构造 + 展开都便宜但每帧都付），实体视图可被 `cx.notify()` 精确重绘、还可用 `Entity::cached(style)` 缓存整棵子树；缓存只对实体开放，因为失效契约（notify）只存在于实体侧。
- 大规模列表的标准分层：实体存数据（`DataTable` 持有 `Vec<Rc<Quote>>`）+ 虚拟化列表控制可见范围 + 组件做行展示（`TableRow`，props 用 `Rc` 共享避免深拷贝）。

## 7. 下一步学习建议

本讲结束了第 3 单元（声明式 UI）。你已经掌握了「用 div 与内置元素组装界面、用组件封装复用」的全部基础，第 4 单元将打开元素机制的引擎盖：

- **下一讲 u4-l1（Element trait 三阶段生命周期）**：本讲反复出现的 `request_layout` / `prepaint` / `paint` 将被逐方法精读。你会理解 `ViewElement` 展开组件后，子树在框架内部如何真正走完布局与绘制——组件是「不实现 Element」的最后一层抽象，再往下就是命令式世界。
- **u6-l1（uniform_list）**：本讲 4.3 的 `data_table` 只浅尝了虚拟化列表，专题讲义会拆解 `visible_range` 的计算与滚动状态管理。
- **延伸阅读**：workspace 里的 `crates/ui` 是 Zed 官方的 GPUI 组件库（按钮、图标、面板……），几乎每个文件都是 `#[derive(IntoElement)]` 的成熟范例，适合对照本讲的模板阅读；阅读时留意它们如何在组件内嵌套 `Entity` 视图处理交互状态——正是 4.3.2 决策流程的工业级应用。
- 若想巩固本讲，回到 `examples/view_example` 通读 `example_input.rs` 与 `example_text_area.rs`：它们展示了「实体持有编辑状态 + 组件负责外观」的完整分层，与本讲的 `CursorReadout` 一脉相承。
