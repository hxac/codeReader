# u3-l1 Render 与视图：从状态到元素树

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释「视图 = 实现了 `Render` 的实体」这句统一设计的含义，并写出自己的第一个多视图组合界面。
2. 说出 `Render`、`View`、`ViewElement`、`AnyElement`、`AnyView` 各自的角色，以及一条 `Entity<T>` 是如何一步步被包装成可绘制对象的。
3. 理解 `ViewElement` 如何用实体 id（`EntityId`）作为元素 id，让同一个视图的内部状态跨帧存续。
4. 完整跟踪 `cx.notify()` 到下一帧重绘的链路，并解释渲染缓存（`.cached()`）何时复用、何时失效。

本讲是第三单元（声明式 UI）的第一讲，承接 u2 建立的 Entity / Context / notify 概念，把它们落到「屏幕上的像素」这一层。

## 2. 前置知识

学习本讲前，你需要理解以下 u2 已建立的概念（用一句话复习）：

- **实体（Entity）**：应用状态住在 `App` 持有的实体表里，`Entity<T>` 只是「编号 + 类型标签」的句柄，读写要经 `cx.read` / `cx.update` 完成（u2-l2）。
- **Context 与 notify**：`Context<T>` 是实体更新闭包里的上下文；调用 `cx.notify()` 表示「我变了，可能影响显示」（u2-l3）。本讲将揭晓 notify 之后到底发生了什么。
- **效果队列与 flush_effects**：notify 首先进入效果队列，在最外层更新结束时统一派发（u2-l3）。

两个本讲新引入的直觉，请先建立：

1. **立即模式 + 保留模式的混合**（u1-l1 已提出，本讲给出源码证据）：元素树是「立即」的——每次窗口重绘都从根视图的 `render()` 重建整棵树，上一帧的元素和回调全部丢弃；应用状态是「保留」的——实体跨帧存活。`Render` 正是连接两个世界的函数：**状态 →（render）→ 元素树**。
2. **「视图」不是新东西**。GPUI 没有一个独立的 View 类。任何实体只要实现了 `Render` 这个只有一个方法的 trait，就自动成为视图。这种「统一」意味着你学过的所有实体知识（所有权、弱句柄、观察者）对视图全部适用。

> 术语提示：本讲频繁出现「元素（Element）」与「视图（View）」两个词。粗略地说：元素负责一次性的布局与绘制（每帧重建），视图持有跨帧的状态（实体）。窗口里的界面 = 一棵以根视图为顶点、由元素组成的树。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/element.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/element.rs) | 定义 `Element`、`Render`、`RenderOnce`、`IntoElement`、`AnyElement` 等元素层核心 trait | `Render` trait 的定义与文档 |
| [src/view.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs)（约 500 行） | 视图层全部机制：`AnyView`、`View` trait、`ViewElement` 及其三阶段实现、渲染缓存 | 本讲主战场，逐段精读 |
| [src/window.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/window.rs) | 窗口与绘制管线（约 7400 行，u4-l3 会整体精读） | 只看 `WindowInvalidator`、`mark_view_dirty`、`draw`、`draw_roots` 四个与视图相关的片段 |
| [src/app.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/app.rs) | App 全局状态容器 | `App::notify`、`record_entities_accessed` |
| [src/key_dispatch.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/key_dispatch.rs) | 派发树（记录元素树结构） | `view_path_reversed`：如何由子视图找到全部祖先视图 |
| [examples/hello_world.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/examples/hello_world.rs) | 最小可运行示例（u1-l2 已逐行读） | 作为实践的改造基底 |
| [examples/view_example/view_example_main.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/examples/view_example/view_example_main.rs) | 多视图组合的官方示例 | 观摩「一个实体被多个视图共享」的写法 |

## 4. 核心概念与源码讲解

### 4.1 Render：让实体变成视图

#### 4.1.1 概念说明

`Render` 是 GPUI 中区分「普通实体」与「视图」的唯一标记。它的定义只有一个方法：给定窗口和上下文，产出一棵元素树：

- **解决什么问题**：实体是纯状态，不知道自己该如何显示；`Render` 把「状态如何映射为界面」这件事写成实体的一个方法，使界面成为状态的函数——你永远通过修改状态（然后 notify）来改变界面，而不是直接操作界面。
- **为什么需要它**：如果状态和显示分离成两套体系（像传统 MVC），两者同步会变成无尽的胶水代码。`Render` 让每个状态自带显示逻辑，框架负责在合适的时机调用它。

#### 4.1.2 核心流程

一个视图从诞生到上屏的流程：

1. 定义结构体（如 `HelloWorld { text: SharedString }`），持有全部需要的状态。
2. 为它实现 `Render`，在 `render()` 里用 `div()` 等元素描述界面。
3. 在 `cx.open_window` 的回调里 `cx.new(|_| HelloWorld { .. })` 创建实体——这一步和创建普通实体完全一样（u2-l2 的两阶段 `reserve→insert`）。
4. 该实体被设为窗口根视图。此后每当窗口重绘，框架调用它的 `render()` 重建元素树。

#### 4.1.3 源码精读

先看 `Render` trait 本体，它短得可以整个贴出来。这段代码是整个 trait 的定义，注释明确说出「视图就是实现了 Render 的实体」：

[src/element.rs:161-166](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/element.rs#L161-L166)

```rust
/// An object that can be drawn to the screen. This is the trait that distinguishes "views" from
/// other entities. Views are `Entity`'s which `impl Render` and drawn to the screen.
pub trait Render: 'static + Sized {
    /// Render this view into an element tree.
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement;
}
```

注意三个细节：

- `render(&mut self, ...)` 拿的是实体的**可变借用**——渲染过程可以直接修改自身状态（比如惰性计算缓存值）。
- 参数是 `&mut Context<Self>`，所以 render 内部可以 spawn 任务、emit 事件、读取其他实体。
- 返回值是 `impl IntoElement`：任何能转换成元素的东西都行，通常是一个 `Div`，也可以是另一个视图（见 4.2）。

element.rs 开头的模块文档用一段话讲清了「每帧重建」的心智模型，值得一读——元素树由根视图的 `Render::render()` 递归构建，交给 Taffy 布局后绘制，下一帧开始前整棵元素树连同注册的回调都被丢弃，周而复始：

[src/element.rs:8-14](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/element.rs#L8-L14)

再看使用侧。hello_world 示例的视图定义：结构体持有状态 `text`，

[examples/hello_world.rs:9-14](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/examples/hello_world.rs#L9-L14)

```rust
struct HelloWorld {
    text: SharedString,
}

impl Render for HelloWorld {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        div() /* ... 一长串样式链 ... */
    }
}
```

创建根视图的位置在 `open_window` 的第二个回调里，与创建普通实体毫无区别：

[examples/hello_world.rs:100-104](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/examples/hello_world.rs#L100-L104)

```rust
|_, cx| {
    cx.new(|_| HelloWorld {
        text: "World".into(),
    })
},
```

「实现 Render 的实体可以被当作元素使用」这件事由一条 blanket 实现达成——任何 `Entity<V>`（`V: Render`）都实现了 `IntoElement`，转换结果是 `ViewElement`：

[src/view.rs:95-101](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L95-L101)

```rust
impl<V: 'static + Render> IntoElement for Entity<V> {
    type Element = ViewElement<Entity<V>>;

    fn into_element(self) -> Self::Element {
        ViewElement::new(self)
    }
}
```

这就是为什么 `div().child(self.child_view.clone())` 能直接编译：`child()` 接收 `impl IntoElement`，而 `Entity<ChildView>` 满足它。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「render 每次窗口重绘都会重新执行」。

1. 复制 `examples/hello_world.rs` 为 `examples/render_log.rs`（cargo 会自动发现 examples/ 下新增的文件；若未被发现，仿照 [Cargo.toml:177-179](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/Cargo.toml#L177-L179) 增加一个 `[[example]]` 段）。
2. 在 `render()` 的第一行加一条日志：`println!("HelloWorld::render");`。
3. 运行 `cargo run -p gpui --example render_log`。
4. 用鼠标拖动窗口边缘改变窗口尺寸若干次，观察终端输出。
5. 不做任何操作静置几秒，再观察输出。

**需要观察的现象**：窗口尺寸每变化一次，终端应出现一条（或多条）`HelloWorld::render`；完全静止时通常没有新输出——GPUI 是按需重绘的，没有变化就没有帧。

**预期结果**：日志条数与「窗口需要重绘的次数」对应，而不是按固定帧率刷屏。具体每次 resize 触发几条日志取决于平台事件合并策略，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`Render::render` 返回 `impl IntoElement` 而不是某个具体类型（如 `Div`），这样做的好处是什么？

<details>参考答案：不同的 render 可以返回不同类型的元素——`div()`、`Svg`、`Empty`、甚至另一个视图的 `ViewElement`，调用方（框架）不需要知道具体类型，只要求「能转换成元素」。这也让 render 内部可以按条件返回不同结构的界面。</details>

**练习 2**：一个实体如果没有实现 `Render`，能出现在界面上吗？它还有用吗？

<details>参考答案：不能直接作为视图渲染，但它依然可以作为「数据实体」存在，被别的视图在 render 里读取（`entity.read(cx)`）。view_example 示例中 `Editor` 的 `String` 数据平面就是这种用法：一个实体可以被多个视图共享展示。</details>

**练习 3**：为什么 `render` 拿到的是 `&mut self` 而不是 `&self`？

<details>参考答案：渲染阶段框架已经通过实体的 update 租约（u2-l2 的 lease 机制）拿到了可变借用，允许视图在渲染时做惰性更新（例如根据当前宽度重算换行缓存）。同时签名保证了渲染发生在实体的更新闭包内，与其他时刻的访问不会产生可变别名。</details>

### 4.2 ViewElement：实体世界与元素世界的桥梁

#### 4.2.1 概念说明

`Entity<V>` 实现了 `IntoElement`，但实体本身不是元素——转换的结果是 `ViewElement<V>`，一个**包装器元素**。它对外表现为普通元素（参与布局、绘制），对内负责：

1. 在合适的时机调用被包装视图的 `render()`，把返回的元素树挂到当前帧；
2. 用实体的 `EntityId` 作为自己的元素 id，让这个视图子树拥有独立的元素状态命名空间；
3. （可选）跨帧缓存渲染结果（见 4.4）。

理解 `ViewElement` 的关键是：**元素每帧重建，但「元素状态」按键（GlobalElementId）跨帧存活**。窗口维护一张 `element_states` 表，键是元素 id 路径（`GlobalElementId`），值是任意类型的状态。`ViewElement` 把实体 id 编进这条路径，于是同一个视图内部的滚动位置、hover 状态等跨帧不丢。

#### 4.2.2 核心流程

`ViewElement` 实现 `Element` trait 的三阶段生命周期（u4-l1 将系统讲解，这里只需知道调用顺序）：

```text
request_layout   →  向布局引擎申报尺寸；调用视图的 render() 得到子元素并让其申报布局
prepaint         →  布局结果确定后提交边界；注册 hitbox、焦点等（缓存判定也发生在这一步）
paint            →  真正向场景添加图元、注册事件监听
```

关键分支：`ViewElement` 内部有「有实体身份（stateful）」与「无实体身份（stateless）」两条路径，由 `entity_id()` 是否返回 `Some` 决定：

- **stateful 路径**：包装的是 `Entity<T: Render>` 或 `AnyView`。有专属元素 id 命名空间，用 `with_rendered_view` 标记「当前正在渲染哪个视图」，参与缓存判定。
- **stateless 路径**：包装的是 `RenderOnce` 组件（u3-l6 详讲）。没有实体身份，用类型名做隔离命名空间，每帧直接重渲染，不参与视图级缓存。

#### 4.2.3 源码精读

先看统一的 `View` trait——它是 `Render`（有状态视图）和 `RenderOnce`（无状态组件）背后的统一模型，2025 年重构后成为 `ViewElement` 的泛型参数约束：

[src/view.rs:171-195](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L171-L195)

```rust
pub trait View: 'static + Sized {
    /// This view's identity, if it has one. ...
    fn entity_id(&self) -> Option<EntityId>;

    /// Render this view into an element tree, consuming `self`.
    fn render(self, window: &mut Window, cx: &mut App) -> impl IntoElement;
}
```

文档注释说明了身份的意义：`entity_id()` 返回 `Some` 时，该 id 成为视图的元素 id——视图获得独立的元素 id 命名空间（内部 `use_state` / `.id(..)` 不会与兄弟视图冲突），且对该实体的 `cx.notify()` 只重渲染这个视图的子树。

三条 blanket 实现把两种编程界面接进同一个模型。`RenderOnce` 组件是没有身份的 `View`：

[src/view.rs:197-207](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L197-L207)

而实现 `Render` 的实体是以自身 id 为身份的 `View`——注意它的 `render` 是消费 `self`（拿到 `Entity<T>` 的所有权）后调用 `update` 进入实体，再调用 `Render::render`：

[src/view.rs:209-221](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L209-L221)

```rust
impl<T: Render> View for Entity<T> {
    fn entity_id(&self) -> Option<EntityId> {
        Some(Entity::entity_id(self))
    }

    fn render(self, window: &mut Window, cx: &mut App) -> impl IntoElement {
        self.update(cx, |this, cx| {
            Render::render(this, window, cx).into_any_element()
        })
    }
}
```

这段就是「实体世界 → 元素世界」的桥头堡：`View::render`（元素侧，消费所有权、拿 `&mut App`）转译为 `Render::render`（实体侧，可变借用、拿 `&mut Context<T>`）。类型擦除发生在 `into_any_element()`——产物是 `AnyElement`，一个分配在元素 arena 上的类型擦除盒子（[src/element.rs:587-599](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/element.rs#L587-L599)），这样不同类型的子元素才能装进同一个 `children` 列表。

接着看 `ViewElement` 的结构体与构造：

[src/view.rs:239-260](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L239-L260)

```rust
pub struct ViewElement<V: View> {
    view: Option<V>,
    entity_id: Option<EntityId>,
    cached_style: Option<StyleRefinement>,
    /* debug 构建下还有 source 位置，供 inspector 使用 */
}

impl<V: View> ViewElement<V> {
    pub fn new(view: V) -> Self {
        let entity_id = view.entity_id();
        ViewElement { entity_id, cached_style: None, view: Some(view), /* .. */ }
    }
}
```

`view: Option<V>` 佐证了「一次性」：构造时装入视图，`request_layout` 里调用 `render` 后被 `take()` 取走消费，本帧结束即随元素树一起丢弃。

它的元素 id 直接来自实体 id：

[src/view.rs:302-304](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L302-L304)

```rust
fn id(&self) -> Option<ElementId> {
    self.entity_id.map(ElementId::View)
}
```

最后看 `request_layout` 的双路径实现（有删节）：

[src/view.rs:314-343](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L314-L343)

```rust
fn request_layout(/* .. */) -> (LayoutId, Self::RequestLayoutState) {
    if let Some(entity_id) = self.entity_id {
        // Stateful path: create a reactive boundary.
        window.with_rendered_view(entity_id, |window| {
            // .. 未启用缓存时：
            let mut element = self.view.take().unwrap()
                .render(window, cx).into_any_element();
            let layout_id = element.request_layout(window, cx);
            (layout_id, Some(element))
            // .. 启用 .cached(style) 时只按外部给的样式申报布局，返回 None
        })
    } else {
        // Stateless path: isolate subtree via type name (no entity identity).
        window.with_id(ElementId::Name(std::any::type_name::<V>().into()), |window| {
            /* 同样 render + request_layout */
        })
    }
}
```

两个要点：

- **未缓存的视图在每次窗口重绘时都会重新执行 `Render::render`**——就在 `request_layout` 里的这行 `self.view.take().unwrap().render(window, cx)`。这就是「立即模式」的落地处。
- `with_rendered_view` 会把视图的实体 id 压入 `rendered_entity_stack`（[src/window.rs:4768-4778](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/window.rs#L4768-L4778)），prepaint 阶段再通过 `set_view_id` 把「哪个视图占哪个派发树节点」记录下来（[src/key_dispatch.rs:226-233](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/key_dispatch.rs#L226-L233)）——这正是 4.4 中「沿树找祖先」的数据来源。

窗口的根视图同样走这条路：`draw_roots` 把根 `AnyView` 转成元素后从 `request_layout` 开始驱动整棵树：

[src/window.rs:3092-3098](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/window.rs#L3092-L3098)

#### 4.2.4 代码实践

**实践目标**：源码阅读型实践——用调用链把本节内容串起来。

1. 在 [src/view.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs) 中定位 `impl<V: View> Element for ViewElement<V>`，依次阅读 `request_layout`、`prepaint`、`paint` 三个方法。
2. 阅读窗口侧的跨帧状态存取 [src/window.rs:3770-3787](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/window.rs#L3770-L3787)：`with_element_state` 以 `(GlobalElementId, TypeId)` 为键，优先从 `next_frame` 取、再从 `rendered_frame`（上一帧）取。
3. 回答：一个嵌套视图 `A → B → C`，其中 B 是视图、C 是 B 内部 `.id("c")` 的 div，C 的元素状态完整键由什么构成？

**需要观察的现象**：`with_element_state` 的键 = 元素 id 路径 + 状态类型；路径中包含 B 的 `ElementId::View(实体id)`。

**预期结果**：C 的键形如 `GlobalElementId([View(B_id), Name("c")])` + `TypeId::of::<C 的状态类型>()`。因此即使 A 里再嵌一个结构相同的视图 B2，B2 内部的 "c" 状态也不会与 B 的冲突——实体 id 不同，路径不同。

#### 4.2.5 小练习与答案

**练习 1**：`ViewElement` 的 `view` 字段为什么是 `Option<V>` 而不是 `V`？

<details>参考答案：因为渲染是一次性消费。`request_layout` 中 `self.view.take()` 把视图取出、调用 `render`（按值消费 `self` 的路径）后置 `None`；同一帧内再次访问会 `unwrap` 失败，这是「元素树每帧重建、视图句柄每帧重新装入」设计的直接体现。</details>

**练习 2**：如果把同一个视图实体（同一个 `Entity<T>` 句柄）作为 child 放进两个不同的父视图，会出现什么问题？

<details>参考答案：两个位置会生成两个 `ViewElement`，但它们的元素 id 相同（都来自该实体的 EntityId）。`View` trait 的文档（[src/view.rs:186-190](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L186-L190)）明确警告：同一实体 id 的两个视图不能出现在同一父元素下的兄弟位置，否则它们的内部元素状态（滚动偏移、use_state 等）会静默冲突。要复用「同一个数据的多份展示」，应创建两个实体（如 view_example 中两个输入框各持一个 String），或使用读取同一实体的 RenderOnce 组件。</details>

**练习 3**：`View` trait 与 `Render` trait 的方法签名里，`render` 的第一个参数一个是 `self`（消费所有权）一个是 `&mut self`，为什么不同？

<details>参考答案：`View::render(self, ..)` 处于元素世界：元素是按值构造、每帧重建的一次性数据，消费所有权让框架可以自由移动它；`Render::render(&mut self, ..)` 处于实体世界：实体状态由 App 持有，只能拿借用（经由 update 租约）。两者的转换（`Entity<T>` 的 `View` 实现）正是从「所有权世界」进入「借用世界」的边界。</details>

### 4.3 AnyView：类型擦除的视图句柄

#### 4.3.1 概念说明

`Entity<T>` 是泛型句柄，但框架经常需要在不知道 `T` 的情况下保存「一个视图」：窗口要保存任意类型的根视图、弹出框要保存任意类型的提示视图、拖拽时要保存任意类型的预览视图。`AnyView` 就是为此准备的类型擦除句柄——它由两部分组成：

1. `AnyEntity`：类型擦除的实体句柄（u2-l2 已见过 `AnyEntity`）；
2. 一个**函数指针** `render: fn(&AnyView, &mut Window, &mut App) -> AnyElement`：知道如何调用原始类型 `V` 的渲染。

这与 Rust 标准的 `dyn Trait` 对象不同：`AnyView` 没有胖指针 vtable，只是一个普通结构体 + 单个函数指针，`Clone` 的代价只是克隆实体句柄。它本身也实现了 `View`，因此擦除后的视图照样能作为元素使用，走同一套 `ViewElement` 机制。

#### 4.3.2 核心流程

类型擦除与还原的流程：

```text
Entity<V> ──From──▶ AnyView { entity: AnyEntity, render: any_view::render::<V> }
                        │
                        │ View::render 被调用时
                        ▼
              (self.render)(&self, window, cx)   // 经函数指针转到具体类型的代码
                        │
                        ▼
              downcast::<V>() 还原 Entity<V> ──▶ update ──▶ Render::render
```

#### 4.3.3 源码精读

`AnyView` 的定义与构造：

[src/view.rs:18-31](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L18-L31)

```rust
#[derive(Clone, Debug)]
pub struct AnyView {
    entity: AnyEntity,
    render: fn(&AnyView, &mut Window, &mut App) -> AnyElement,
}

impl<V: Render> From<Entity<V>> for AnyView {
    fn from(value: Entity<V>) -> Self {
        AnyView {
            entity: value.into_any(),
            render: any_view::render::<V>,
        }
    }
}
```

`From` 实现把「具体类型的渲染入口」固化成函数指针 `any_view::render::<V>`——这是零开销单态化的类型擦除：不需要动态派发整张 vtable，只擦「render 这一个方法」。

函数指针指向的实现（有删节）：

[src/view.rs:151-169](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L151-L169)

```rust
pub(crate) fn render<V: 'static + Render>(
    view: &AnyView, window: &mut Window, cx: &mut App,
) -> AnyElement {
    let view = view.clone().downcast::<V>().unwrap();
    // debug 构建下记录视图类型名，供无障碍调试归类
    view.update(cx, |view, cx| view.render(window, cx).into_any_element())
}
```

`AnyView` 自己实现 `View`，把 `render` 委托给函数指针：

[src/view.rs:82-93](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L82-L93)

配套的还原工具：`downcast` 可把 `AnyView` 还原成具体的 `Entity<T>`（类型不符时原样返回 `Err`），`downgrade` / `AnyWeakView::upgrade` 提供弱句柄（[src/view.rs:43-61](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L43-L61) 与 [src/view.rs:111-126](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L111-L126)）。

窗口根视图就是 `AnyView`——`Window` 的字段声明：

[src/window.rs:1135](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/window.rs#L1135)

```rust
pub(crate) root: Option<AnyView>,
```

这就是为什么 `cx.open_window` 能接受任意 `E: Render` 作为根视图：擦除后窗口无需泛型参数。`WindowHandle<E>::root_view` 再通过 `downcast::<E>()` 把根视图还原回具体类型供外部读取（[src/window.rs:6452](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/window.rs#L6452) 附近）。

最后看一个「一个实体、多个展示」的实例。view_example 示例中，`CursorReadout` 是无状态组件（`RenderOnce`），它持有 `Entity<Editor>` 并在渲染时读取光标位置，与编辑它的 `Input` 组件并排显示——同一实体、两种视图，互不干扰：

[examples/view_example/view_example_main.rs:38-59](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/examples/view_example/view_example_main.rs#L38-L59)

#### 4.3.4 代码实践

**实践目标**：源码阅读型实践——找到框架内所有「必须类型擦除」的位置。

1. 在 crates/gpui/src 内执行（或用编辑器全局搜索）检索 `AnyView` 的使用点。
2. 对每个使用点回答：为什么这里不能直接用泛型 `Entity<E>`？

**需要观察的现象**：典型位置包括：`Window` 的 `root` 字段（窗口根视图类型不定）；弹出对话框 `prompt` 的视图；拖拽预览 `active_drag.view`；`AnyView::cached` 缓存接口。

**预期结果**：共同规律是「结构体字段需要长期保存一个视图，而视图的具体类型由调用方决定」——Rust 结构体字段必须有确定类型，泛型会像涟漪一样扩散到所有外层类型，函数指针擦除把泛型参数截断在这一个字段上。

#### 4.3.5 小练习与答案

**练习 1**：`AnyView` 与 `Box<dyn View>` 相比有什么优势？

<details>参考答案：`AnyView` 是固定大小的普通结构体（擦除的实体句柄 + 一个函数指针），`Clone` 廉价、无需堆分配 vtable；`Box<dyn View>` 是堆上的胖指针，且 `View::render(self, ..)` 按值消费 self，与 trait 对象的借用语义配合得不好。GPUI 的做法等价于「只擦除一个方法的 vtable」，够用且更省。</details>

**练习 2**：`AnyView::entity_id()` 返回什么？为什么 `AnyView` 的相等比较只比较实体、不比较函数指针？

<details>参考答案：返回内部实体的 `EntityId`（[src/view.rs:68-71](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L68-L71)）。因为函数指针由实体的具体类型唯一决定（同一个 `Entity<V>` 擦除出的函数指针必然相同），实体相等即意味着二者完全等价。</details>

**练习 3**：窗口如何做到 `open_window` 时接受任意类型的根视图？

<details>参考答案：`open_window` 的回调返回 `Entity<E>`（E: Render），框架立即通过 `From<Entity<E>> for AnyView` 擦除类型存入 `Window.root: Option<AnyView>`；需要具体类型时（如 `WindowHandle<E>` 的读取方法）再 `downcast::<E>()` 还原，失败时返回 `Result` 的 `Err`。</details>

### 4.4 渲染缓存：cx.notify() 与下一帧重绘

#### 4.4.1 概念说明

这是本讲最重要的一节。前两节留下了两个悬念：

- `cx.notify()` 之后到底发生了什么？
- 「每帧重建元素树」听起来很浪费，框架如何跳过没变的部分？

答案分两层：

1. **窗口层（粗粒度）**：窗口只在「脏」的时候才重绘。`cx.notify()` 的第一作用是把所有正在显示该实体的窗口标记为脏，请求下一帧。没有 notify、没有 resize、没有焦点变化，就没有帧。
2. **视图层（细粒度）**：重绘发生时，未启用缓存的视图跟随父级整棵重建（诚实但简单）；启用了 `.cached(style)` 的视图则在 prepaint 阶段做缓存判定，条件满足时**直接重放上一帧录制的 prepaint/paint 区间，完全跳过 `Render::render`**。判定条件里最重要的一条就是：该视图的实体是否被 notify 过（即是否在 `dirty_views` 集合里）。

一句话总结：**`cx.notify()` = 让相关窗口安排下一帧 + 让这个视图的缓存失效**。

#### 4.4.2 核心流程

完整链路（这是本讲的「主干调用链」，建议动手跟踪一遍）：

```text
① cx.notify()（实体更新闭包内）
      │  Context::notify → App::notify(entity_id)
      ▼
② App::notify：查 window_invalidators_by_entity
      │  有窗口正在显示该实体 → WindowInvalidator::invalidate_view
      │        · invalidator.dirty_views.insert(entity)
      │        · invalidator.dirty = true（窗口脏）
      │        · 唤醒平台调度下一帧
      │  同时入队 Effect::Notify（观察者路径，u2-l3）
      ▼
③ 下一帧 Window::draw
      │  invalidate_entities()：取出 invalidator 的脏视图集合
      │  mark_view_dirty(每个脏视图)：连同其全部祖先视图加入 window.dirty_views
      ▼
④ draw_roots：从根 ViewElement 走 request_layout / prepaint / paint
      │  每个视图元素在 prepaint 中做缓存判定：
      │    复用条件（全部满足）：
      │        bounds 不变 ∧ content_mask 不变 ∧ text_style 不变
      │        ∧ 实体 id ∉ dirty_views ∧ 非 window.refresh()
      │    满足 → reuse_prepaint / reuse_paint（重放录制区间，跳过 render）
      │    不满足 → 重新调用 Render::render，录制新的区间
      ▼
⑤ 帧收尾：dirty_views.clear()；登记本帧访问过的全部实体，
   供下一次 App::notify 判断「哪些窗口正在显示谁」
```

缓存命中条件写成公式：

\[ \text{复用} \iff \text{bounds 相同} \;\land\; \text{content\_mask 相同} \;\land\; \text{text\_style 相同} \;\land\; \text{id} \notin \text{dirty\_views} \;\land\; \lnot\, \text{refreshing} \]

#### 4.4.3 源码精读

**第一步：notify 的两个去向。** `Context::notify` 只有一行，转发给 `App::notify`：

[src/app/context.rs:229-231](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/app/context.rs#L229-L231)

`App::notify` 是整个响应式系统的枢纽（有删节）：

[src/app.rs:2640-2675](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/app.rs#L2640-L2675)

```rust
pub fn notify(&mut self, entity_id: EntityId) {
    // 取出所有「当前窗口正在显示该实体」的失效器
    let live_invalidators = /* 从 window_invalidators_by_entity 过滤 tracked_entities */;

    if live_invalidators.is_empty() {
        // 没有窗口显示它：只走观察者路径
        if self.pending_notifications.insert(entity_id) {
            self.pending_effects.push_back(Effect::Notify { emitter: entity_id });
        }
    } else {
        for invalidator in &live_invalidators {
            invalidator.invalidate_view(entity_id, self);
        }
    }
}
```

注意细节：即使走窗口失效路径，`Effect::Notify` 依然会入队（见下面 `invalidate_view` 内部），所以 `cx.observe` 注册的观察者总能收到通知——**重绘与观察者通知是两条并行的效果**。

**第二步：窗口失效器。** 每个窗口有一个 `WindowInvalidator`，`invalidate_view` 把视图记入脏集合、把窗口标记为脏并唤醒平台帧调度：

[src/window.rs:160-179](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/window.rs#L160-L179)

```rust
pub fn invalidate_view(&self, entity: EntityId, cx: &mut App) -> bool {
    let mut inner = self.inner.borrow_mut();
    inner.dirty_views.insert(entity);
    if inner.draw_phase == DrawPhase::None {
        let became_dirty = !inner.dirty;
        inner.dirty = true;
        // .. 唤醒平台调度器安排下一帧 ..
        cx.push_effect(Effect::Notify { emitter: entity });
        // ..
    }
}
```

**第三步：下一帧开始时展开「祖先视图」。** `Window::draw` 开头调用 `invalidate_entities`（[src/window.rs:2848](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/window.rs#L2848)），它把失效器里的脏视图逐个交给 `mark_view_dirty`：

[src/window.rs:1921-1934](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/window.rs#L1921-L1934)

```rust
fn mark_view_dirty(&mut self, view_id: EntityId) {
    // Mark ancestor views as dirty. If already in the `dirty_views` set, then all its ancestors
    // should already be dirty.
    for view_id in self.rendered_frame.dispatch_tree.view_path_reversed(view_id) {
        if !self.dirty_views.insert(view_id) {
            break;
        }
    }
}
```

`view_path_reversed`（[src/key_dispatch.rs:587-596](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/key_dispatch.rs#L587-L596)）从该视图的派发树节点向上遍历父节点，收集途经的所有视图 id。**为什么祖先也要标脏？** 因为若某个缓存的祖先视图直接复用上一帧录制的 prepaint 区间，录制内容里包含的是子视图的旧输出——脏的子视图将永远没有机会重新渲染。所以必须让祖先也走「重新渲染」分支，重建出通往脏子视图的路径。

**第四步：缓存判定本体。** `ViewElement::prepaint` 的有身份路径（删节）：

[src/view.rs:371-432](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L371-L432)

```rust
// 命中缓存：重放上一帧录制的 prepaint 区间
if let Some(mut element_state) = element_state
    && element_state.cache_key.bounds == bounds
    && element_state.cache_key.content_mask == content_mask
    && element_state.cache_key.text_style == text_style
    && !window.dirty_views.contains(&entity_id)
    && !window.refreshing
{
    window.reuse_prepaint(element_state.prepaint_range.clone());
    // 重新登记该子树读取过的实体（保持窗口对它们的追踪）
    cx.entities.extend_accessed(&element_state.accessed_entities);
    return (None, element_state);
}

// 未命中：重新渲染整个子树
let (mut element, accessed_entities) = cx.detect_accessed_entities(|cx| {
    let mut element = self.view.take().unwrap()
        .render(window, cx).into_any_element();
    element.layout_as_root(bounds.size.into(), window, cx);
    element.prepaint_at(bounds.origin, window, cx);
    element
});
```

跨帧保存的缓存条目结构：

[src/view.rs:285-296](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L285-L296)

```rust
struct ViewElementState {
    prepaint_range: Range<PrepaintStateIndex>, // 上一帧 prepaint 的录制区间
    paint_range: Range<PaintIndex>,            // 上一帧 paint 的录制区间
    cache_key: ViewElementCacheKey,            // bounds + content_mask + text_style
    accessed_entities: FxHashSet<EntityId>,    // 该子树渲染时读过的实体
}
```

paint 阶段对应地选择「画新元素」或「重放旧区间」：

[src/window.rs 之外，src/view.rs:462-483](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L462-L483)

```rust
if let Some(element) = element {
    element.paint(window, cx);            // 本帧重新渲染过 → 画新的
} else {
    window.reuse_paint(element_state.paint_range.clone()); // 复用 → 重放旧的
}
```

**如何启用缓存：** `Entity::cached(style)`（[src/view.rs:223-235](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L223-L235)），文档写明契约：「子树会被复用，直到该实体被 notify（或缓存的 bounds / 文本样式变化）；缓存要求确定的外部尺寸，不会按内容测量」。crate 内部的真实用例可参考 [src/elements/deferred.rs:140-145](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/elements/deferred.rs#L140-L145)：面板视图以 `.cached(StyleRefinement::default().size_full())` 嵌入根视图。

**强制全量重绘的开关：** `Window::refresh` 把 `refreshing` 置真并标脏，下一次绘制将忽略所有视图缓存：

[src/window.rs:1998-2004](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/window.rs#L1998-L2004)

**第五步：帧收尾。** `dirty_views` 每帧清空（[src/window.rs:2884](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/window.rs#L2884)）；`record_entities_accessed`（[src/window.rs:2975-2987](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/window.rs#L2975-L2987)）把本帧绘制中**被访问过的所有实体**（不只是视图实体——渲染中 `read` 过的数据实体也会被自动记录，见 [src/app/entity_map.rs:156-160](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/app/entity_map.rs#L156-L160)）登记到 `App` 的 `tracked_entities` / `window_invalidators_by_entity` 表（[src/app.rs:1096-1113](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/app.rs#L1096-L1113)），上一帧登记但本帧未访问的会被移除。这就是第②步 `App::notify` 能判断「哪些窗口正在显示该实体」的数据来源——一张每帧重建的「窗口 ↔ 实体」倒排表。

#### 4.4.4 代码实践

**实践目标**：源码阅读型实践——列出所有能让一个缓存视图失效的方式，并各找到一行源码依据。

1. 阅读缓存判定（[src/view.rs:386-392](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L386-L392)），列出五个条件。
2. 对每个条件，向自己提问「什么用户行为会改变它」并写下答案。

**预期结果**（对照用）：

| 失效条件 | 触发场景 |
| --- | --- |
| bounds 变化 | 窗口 resize、父级布局变化导致该视图位置/尺寸改变 |
| content_mask 变化 | 滚动容器移动、遮挡边界变化 |
| text_style 变化 | 父级字体/字号样式变化 |
| 实体 id ∈ dirty_views | 对该实体（或其祖先视图实体）调用 `cx.notify()` |
| window.refreshing | 代码调用 `window.refresh()` 强制全量重绘 |

3. 思考题：`cx.notify()` 一个从未被任何窗口渲染的实体，会发生什么？

**预期结果**：第②步中 `live_invalidators` 为空，只入队 `Effect::Notify`，观察者收到回调，但没有任何窗口被标脏——通知与重绘彼此独立。

#### 4.4.5 小练习与答案

**练习 1**：为什么 GPUI 默认不让所有视图自动缓存，而要显式调用 `.cached(style)`？

<details>参考答案：缓存要求外部给定确定尺寸（`cached_style` 参与布局，跳过按内容测量），且缓存期间子树「冻结」——只有对该实体的 notify 能刷新它。显式 opt-in 让开发者对这两点代价有明确认知。`ViewElement::cached` 的文档（[src/view.rs:262-275](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L262-L275)）说明它特意保持 crate 私有：无状态的 `RenderOnce` 组件没有「notify 契约」，缓存将永远无法失效，所以只允许实体支撑的视图走 `Entity::cached` / `AnyView::cached` 进入。</details>

**练习 2**：对一个子视图调用 `cx.notify()`，为什么它的父视图（假设父视图也缓存了）也会重新渲染？

<details>参考答案：`mark_view_dirty` 通过派发树把脏视图的全部祖先也加入 `dirty_views`（[src/window.rs:1921-1934](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/window.rs#L1921-L1934)）。否则父视图命中缓存、重放旧 prepaint 区间时，会把子视图的旧输出一并原样重放，脏的子视图就永远没有机会重建。</details>

**练习 3**：一个视图在 render 里 `read` 了另一个数据实体（不是视图）。之后 notify 这个数据实体，这个视图所在窗口会重绘吗？如果该视图启用了缓存，它的内容会刷新吗？

<details>参考答案：会重绘——`read` 会把该实体记入访问集合（[src/app/entity_map.rs:156-160](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/app/entity_map.rs#L156-L160)），帧收尾登记进倒排表，notify 时窗口被标脏；窗口重绘时未缓存的路径全部重新 render，读取到新值。但**启用了缓存的该视图自身不会刷新**——数据实体不在派发树的视图路径上，不会进入 `dirty_views` 对该视图的判定，视图命中缓存继续重放旧内容。这正是 `Entity::cached` 文档「子树复用直到**该实体**被 notify」契约的另一面：缓存视图依赖的外部数据应尽量通过其自身实体的状态提供。刷新缓存的可靠手段是 notify 视图自己的实体，或 `window.refresh()`。</details>

## 5. 综合实践

**任务**：把 u2 的 Counter 升级为「父视图 + 嵌套子视图」的双视图应用，用日志验证本讲的缓存理论。这是本讲规格指定的实践任务。

### 5.1 编写示例

在 `examples/` 下新建 `counter_views.rs`（示例代码，仿照 hello_world 的三段式骨架）：

```rust
// 示例代码：examples/counter_views.rs
#![cfg_attr(target_family = "wasm", no_main)]

use gpui::{
    App, Bounds, Context, Entity, Render, StyleRefinement, Window, WindowBounds, WindowOptions,
    div, prelude::*, px, rgb, size,
};
use gpui_platform::application;

/// 子视图：独立的计数实体
struct ChildView {
    count: i32,
}

impl Render for ChildView {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        println!("[render] ChildView");
        div()
            .flex_1()
            .bg(rgb(0x505050))
            .justify_center()
            .items_center()
            .text_xl()
            .text_color(rgb(0xffffff))
            .child(format!("child count = {}", self.count))
    }
}

/// 根视图：自己的计数 + 嵌套的子视图
struct RootView {
    count: i32,
    child: Entity<ChildView>,
}

impl Render for RootView {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        println!("[render] RootView");
        div()
            .flex()
            .flex_col()
            .size(px(400.))
            .gap_2()
            .p_2()
            .bg(rgb(0x333333))
            .text_color(rgb(0xffffff))
            .child(
                div()
                    .id("parent-btn")
                    .flex()
                    .justify_center()
                    .p_2()
                    .bg(rgb(0x777777))
                    .rounded_md()
                    .child(format!("parent count = {} (点击我)", self.count))
                    .on_click(cx.listener(|this, _event, _window, cx| {
                        this.count += 1;
                        cx.notify();
                    })),
            )
            .child(
                div()
                    .id("child-btn")
                    .flex()
                    .justify_center()
                    .p_2()
                    .bg(rgb(0x999999))
                    .rounded_md()
                    .child("点我增加 child count")
                    .on_click({
                        let child = self.child.clone();
                        move |_event, _window, cx: &mut App| {
                            child.update(cx, |child, cx| {
                                child.count += 1;
                                cx.notify();
                            });
                        }
                    }),
            )
            // 第一步先用普通嵌入；第二步把这一行换成下面的 cached 版本
            .child(self.child.clone())
            // .child(self.child.clone().cached(StyleRefinement::default().flex_1()))
    }
}

fn run_example() {
    application().run(|cx: &mut App| {
        let bounds = Bounds::centered(None, size(px(400.), px(400.)), cx);
        cx.open_window(
            WindowOptions {
                window_bounds: Some(WindowBounds::Windowed(bounds)),
                ..Default::default()
            },
            |_, cx| {
                cx.new(|cx| RootView {
                    count: 0,
                    child: cx.new(|_| ChildView { count: 0 }),
                })
            },
        )
        .unwrap();
        cx.activate(true);
    });
}

#[cfg(not(target_family = "wasm"))]
fn main() {
    run_example();
}
```

说明：`on_click` 定义在 `StatefulInteractiveElement` 上，所以两个按钮都加了 `.id(...)`（u5-l2 会系统讲解）；父按钮用 `cx.listener`，子按钮演示从外部用 `entity.update` 更新另一个实体（u2-l3 的两种回写方式）。

### 5.2 第一步：观察「未缓存」的行为

1. 保持 `.child(self.child.clone())`，运行 `cargo run -p gpui --example counter_views`。
2. 点击 parent 按钮一次，记录终端输出。
3. 点击 child 按钮一次，记录终端输出。

**预期结果**：两种情况下都输出**两行**——`[render] RootView` 和 `[render] ChildView`。因为未缓存的视图随窗口整树重建：只要窗口重绘（无论谁 notify），根视图的 render 必然执行，路径上的子视图 render 也随之执行。这验证了 4.2 的结论：「cx.notify() 只重绘自己」在未缓存时并不成立，notify 的准确语义是「让窗口安排下一帧 + 视图缓存失效」。

### 5.3 第二步：观察「缓存」的行为

1. 换用 `.child(self.child.clone().cached(StyleRefinement::default().flex_1()))`（注意同时需要 `use gpui::Styled;`，已包含在 `prelude::*` 中），重新运行。
2. 点击 parent 按钮若干次，观察输出。
3. 点击 child 按钮若干次，观察输出。

**预期结果**：

- 点 parent 按钮：每次只有 `[render] RootView` 一行——子视图的实体没被 notify、`flex_1` 在固定 400px 的父容器里 bounds 稳定，缓存命中，`ChildView::render` 被跳过。
- 点 child 按钮：每次两行都出现——child 进入 `dirty_views`，且 `mark_view_dirty` 把祖先 RootView 一并标脏（4.4 练习 2 的行为）。

**需要观察的现象**：如果第二步中把 `StyleRefinement::default().flex_1()` 换成随内容变化的样式（例如不给确定尺寸），bounds 每帧不同，缓存将频繁失效，日志回到每次两行——这正是 `Entity::cached` 文档强调「缓存要求确定尺寸」的原因。具体帧数与日志条数待本地验证。

### 5.4 检查点

完成后，你应该能不假思索地回答：

1. 为什么点 parent 按钮时子视图界面上的数字没有变，但子视图也没有重新渲染？（缓存重放的是旧图元，数字本来就没变，二者一致。）
2. 如果把子视图的 `cx.notify()` 删掉只做 `child.count += 1`，会发生什么？（窗口不脏 → 通常没有下一帧 → 界面不更新；这是「忘了 notify」的经典 bug，u2-l3 已预警，现在你明白了它的底层原因。）

## 6. 本讲小结

- **视图 = 实现 `Render` 的实体**：`Render` 只有一个方法（[src/element.rs:161-166](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/element.rs#L161-L166)），实体知识全部适用；`Entity<V>` 经 blanket `IntoElement`（[src/view.rs:95-101](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L95-L101)）可直接作为 child 使用。
- **`ViewElement` 是两个世界的桥**：元素侧按值消费、每帧重建；实体侧可变借用、跨帧存活；实体 id 成为元素 id（[src/view.rs:302-304](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/view.rs#L302-L304)），为视图子树提供独立的元素状态命名空间。
- **`AnyView` = 擦除实体 + 一个 render 函数指针**：固定大小、廉价克隆，用于窗口根视图等「保存未知类型视图」的位置。
- **未缓存的视图在每次窗口重绘时重新 render**（`ViewElement::request_layout` 里的 `take().render()`）；窗口本身只在脏时重绘。
- **`cx.notify()` 的完整语义**：经 `App::notify` 把正在显示该实体的窗口标脏并安排下一帧，同时入队 `Effect::Notify` 通知观察者；下一帧该视图连同祖先进入 `dirty_views`。
- **缓存是显式 opt-in**：`entity.cached(style)` 后，prepaint 阶段按「bounds / content_mask / text_style 不变 ∧ 未脏 ∧ 非 refresh」判定，命中则重放录制的 prepaint/paint 区间、跳过 render；帧收尾重建「窗口 ↔ 被访问实体」倒排表并清空脏集合。

## 7. 下一步学习建议

本讲建立了「状态 → 元素树」的映射，但刻意回避了两个问题：元素树长什么样、以及样式如何生效。建议按顺序继续：

1. **u3-l2（div 与 Tailwind 风格样式 API）**：实践里反复出现的 `div().flex().p_2()` 链式调用下一讲系统讲解；你将理解 `Styled` trait 与样式方法命名规律。
2. **u3-l3（Style 与 StyleRefinement）**：本讲 cached 用到的 `StyleRefinement::default().flex_1()` 是什么、样式如何沿树合成与继承。
3. **u3-l6（RenderOnce 与组件化复用）**：本讲 `View` trait 的 stateless 分支（`RenderOnce` 组件）的完整用法——`CursorReadout` 那种「读取实体但不持有实体」的组件。
4. 提前留一个钩子给 **u4-l1（Element trait 三阶段生命周期）**：本讲只从 `ViewElement` 的视角旁观了 request_layout / prepaint / paint，届时你将自己实现一个元素。
5. 源码延伸阅读：若想确认 4.4 的链路，带着 [src/window.rs:2836](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/window.rs#L2836) 的 `Window::draw` 从头读一遍 `draw_roots`，那是 u4-l3 的预演。
