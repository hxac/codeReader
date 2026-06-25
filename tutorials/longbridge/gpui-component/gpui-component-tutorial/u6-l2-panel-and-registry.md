# Panel / StackPanel / TabPanel 与拖拽、序列化

## 1. 本讲目标

学完本讲后，你应该能够：

- 读懂 gpui-component 里「双层 trait」的面板抽象：用户实现的 `Panel` trait 与库内部用于擦除类型的 `PanelView` trait，以及它们如何靠 `impl<T: Panel> PanelView for Entity<T>` 这一个 blanket impl 衔接起来。
- 说出 `TabPanel`（标签页容器）和 `StackPanel`（分屏容器）各自管什么状态、它们之间有什么硬性约束（比如「`StackPanel` 的子节点只能是 `TabPanel` 或 `StackPanel`」）。
- 讲清楚面板拖拽（drag/drop）与分屏（split）的判定逻辑：鼠标落在面板哪个区域，会触发「并入标签」还是「向左/右/上/下分屏」。
- 手动完成「布局序列化保存 → 反序列化恢复」的完整闭环，理解 `PanelRegistry`、`PanelState`、`PanelInfo` 三者在这条链路里各自的角色，以及注册名缺失时为什么会得到一个 `InvalidPanel`。

这是「Dock 布局系统」单元（u6）的第二讲。u6-l1 讲了「容器与布局树」（`DockArea` + `DockItem`），本讲把镜头推进到**树里的节点本身**：每一片叶子或分屏是怎么定义、怎么交互、怎么存盘的。

## 2. 前置知识

本讲假定你已经掌握：

- **DockArea 与 DockItem 布局树**（u6-l1）：你知道 `DockItem` 有 `Split / Tabs / Panel / Tiles` 四种节点，`Split` 背后是 `StackPanel`，`Tabs` 背后是 `TabPanel`，中心区和停靠位的布局都由这棵树描述。
- **应用入口与 init**（u1-l4）：`gpui_component::init(cx)` 会顺带调用 `dock::init(cx)`，其中一步就是初始化 `PanelRegistry`。**漏掉 init，反序列化面板时会拿不到注册表。**
- **状态外置 + 无状态外壳**（u2/u4）：组件状态放在外层 `Entity`（GPUI 实体）里。本讲里 `TabPanel`、`StackPanel`、自定义面板**都是**有状态实体（`impl Render`），跨帧身份由 `Entity` 保证。
- **GPUI 实体与事件订阅**：`cx.new`、`cx.subscribe`、`EventEmitter<T>`、`cx.emit(evt)`、`WeakEntity<T>` 这一套。本讲的面板之间靠 `PanelEvent` 互相发消息。

一个直观比喻：把 `DockArea` 想象成一面「毛坯墙」，`StackPanel` 是把墙面「竖着或横着切开」的隔断，`TabPanel` 是每个隔间里「可以翻页的活页夹」，而你自己写的 `Panel` 就是夹进活页夹里的「一页内容」。本讲要回答的，正是「一页内容要满足什么契约」「隔间怎么切」「整面墙怎么拍照存档、下次怎么照着照片复原」。

## 3. 本讲源码地图

本讲涉及的关键文件都集中在 `crates/ui/src/dock/` 目录下：

| 文件 | 作用 |
| --- | --- |
| [panel.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs) | 定义面板抽象：用户实现的 `Panel` trait、库内部的 `PanelView` trait、全局 `PanelRegistry` 注册表，以及 `register_panel` 注册函数。 |
| [tab_panel.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs) | `TabPanel` 标签页容器：管理多个面板的标签切换、拖拽（`DragPanel`）、分屏判定（`on_panel_drag_move`）与真正执行分屏（`split_panel`）。 |
| [stack_panel.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/stack_panel.rs) | `StackPanel` 分屏容器：托管一组沿某 `Axis` 排列的子节点，借助可调宽（Resizable）能力实现拖拽调宽，并在子节点清空时自我销毁。 |
| [invalid_panel.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/invalid_panel.rs) | `InvalidPanel` 兜底面板：当反序列化时遇到「注册表里找不到的面板名」时，用它占位并提示用户。 |
| [state.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/state.rs) | 序列化数据结构：`PanelState`、`PanelInfo`、`DockAreaState`，以及把 `PanelState` 递归重建为 `DockItem` 的 `to_item`。 |

此外，[mod.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs) 里 `DockArea::dump` / `DockArea::load` 是「拍照存档 / 照片复原」的入口，本讲会反复引用它们。

---

## 4. 核心概念与源码讲解

### 4.1 PanelView 与 Panel：面板的双层抽象

#### 4.1.1 概念说明

Dock 系统里，「面板」是一个高度异构的概念：一个文件树面板、一个编辑器面板、一个终端面板，它们的内部状态千差万别。要让 `DockArea` / `StackPanel` / `TabPanel` 这些容器能**不关心具体类型**地统一持有任意面板，gpui-component 用了一个经典手法——**类型擦除（type erasure）**，并且拆成了两层 trait：

- **`Panel`（用户层）**：你写自定义面板时实现它。它的方法签名都是「带具体类型、好写」的：`fn title(&mut self, ...) -> impl IntoElement`、`fn panel_name(&self) -> &'static str`。它要求你的类型同时是 `EventEmitter<PanelEvent> + Render + Focusable`，也就是说**面板本身就是个可渲染、可聚焦、能发事件的 GPUI 实体**。
- **`PanelView`（库层）**：容器真正持有的对象引用类型 `Arc<dyn PanelView>`。它的所有方法返回值都被擦除成了 `AnyElement`、`AnyView`、`&'static str` 这类**对象安全**的形态。容器只认 `dyn PanelView`，不知道你面板的真实类型。

把这两层衔接起来的，是唯一一个 blanket impl：`impl<T: Panel> PanelView for Entity<T>`。意思是「任何一个实现了 `Panel` 的类型 `T`，把它包进 `Entity<T>` 之后，就自动获得了 `PanelView` 的全部能力」。于是「写起来好用」和「容器能用」两个目标被解耦了——你按 `Panel` 写，容器按 `PanelView` 用，中间靠 `Entity` 这座桥连起来。

#### 4.1.2 核心流程

一个自定义面板「从被定义到被容器使用」的流程：

1. 你定义 `struct MyPanel { ... }`，实现 `Panel`（以及必需的 `EventEmitter<PanelEvent>`、`Focusable`、`Render`）。
2. 用 `cx.new(|cx| MyPanel { ... })` 创建它，得到 `Entity<MyPanel>`。
3. 由于 blanket impl，这个 `Entity<MyPanel>` 自动满足 `PanelView`，可以 `Arc::new(entity)` 后塞进 `TabPanel` / `StackPanel`，或用 `DockItem::tab(entity, ...)` 包成布局节点。
4. 容器在渲染、查询、序列化时，只通过 `&dyn PanelView`（或 `Arc<dyn PanelView>`）调用方法；它调用的每一个 `PanelView` 方法，背后都转发到你 `Panel` 实现的具体方法上。

#### 4.1.3 源码精读

先看用户层的 `Panel` trait。它有一长串带默认实现的方法，唯一**必须**实现的是 `panel_name`：

这是 trait 定义与必填项 [panel.rs:52-66](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs#L52-L66)：

```rust
pub trait Panel: EventEmitter<PanelEvent> + Render + Focusable {
    /// 序列化、反序列化、识别面板用的名字。一旦定义就不能改。
    fn panel_name(&self) -> &'static str;

    /// 折叠后在标签上显示的短名，默认 None。
    fn tab_name(&self, cx: &App) -> Option<SharedString> { None }

    /// 面板标题，默认返回 "Unnamed"。
    fn title(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        t!("Dock.Unnamed")
    }
    // ... closable / zoomable / visible / set_active / set_zoomed / dump 等
}
```

几个关键方法（都有默认实现，按需覆写）：

- [panel.rs:89-94](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs#L89-L94) `closable` 默认 `true`：决定标题栏能否出现「关闭」。
- [panel.rs:96-101](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs#L96-L101) `zoomable` 默认 `Some(PanelControl::Menu)`：返回 `None` 表示完全不可放大（全屏），返回值还决定放大按钮显示在「菜单」还是「工具栏」。
- [panel.rs:103-108](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs#L103-L108) `visible` 默认 `true`：返回 `false` 的面板在标签栏被隐藏，但仍参与布局序列化（详见 4.4）。
- [panel.rs:110-122](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs#L110-L122) `set_active` / `set_zoomed`：容器在面板被切到、被放大时会回调它们，给你一个「感知自己状态变化」的钩子。
- [panel.rs:124-134](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs#L124-L134) `on_added_to` / `on_removed`：面板被插入 `TabPanel` 或被移除时的生命周期回调，`on_added_to` 会把自己所属的 `WeakEntity<TabPanel>` 传给你。
- [panel.rs:155-158](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs#L155-L158) `dump` 默认实现 `PanelState::new(self)`：序列化的核心入口，默认只存了 `panel_name`，**面板自身状态需要你自己覆写来补充**（见 4.4）。

再看库层的 `PanelView` trait——注意所有方法签名都「对象安全」了 [panel.rs:166-188](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs#L166-L188)：`title` 返回 `AnyElement`、`view` 返回 `AnyView`、`panel_name` 返回 `&'static str`。这些才能放进 `dyn PanelView` 里。

衔接两层的 blanket impl，节选关键的转发 [panel.rs:190-205](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs#L190-L205)：

```rust
impl<T: Panel> PanelView for Entity<T> {
    fn panel_name(&self, cx: &App) -> &'static str {
        self.read(cx).panel_name()        // 读实体，调 Panel 的方法
    }
    fn panel_id(&self, _: &App) -> EntityId {
        self.entity_id()                  // 用实体 id 作为面板唯一标识
    }
    fn title(&self, window: &mut Window, cx: &mut App) -> AnyElement {
        self.update(cx, |this, cx| this.title(window, cx).into_any_element())
        //   ^^^^^^ 把 &mut T 借出来，把返回的 impl IntoElement 擦成 AnyElement
    }
    // ...
}
```

可以看到，「对象安全」是用两个手段换来的：`&self.read(cx)` 把实体内容借出来（只读），`self.update(cx, ...)` 把 `&mut T` 借出来（可写），再用 `.into_any_element()` / `.into()` 把返回的具体类型擦除。

> 💡 **为什么要 `panel_id`**？面板的「同一性」靠 `EntityId`。`dyn PanelView` 的 `PartialEq` 也是比较 `view()` 得到的 `AnyView`（背后就是实体 id），见 [panel.rs:287-291](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs#L287-L291)。拖拽时判断「拖的面板是不是已经在当前标签里」就靠它。

#### 4.1.4 代码实践：追踪一次 `title()` 调用

1. **实践目标**：亲手验证「容器调 `PanelView::title` → blanket impl 转发 → 你的 `Panel::title`」这条链路确实存在。
2. **操作步骤**：
   - 打开 [panel.rs:203-205](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs#L203-L205)，确认 `PanelView::title` 内部调用了 `this.title(window, cx)`。
   - 打开 [tab_panel.rs:95-99](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L95-L99)，确认 `TabPanel::title` 又去调 `self.active_panel(cx).map(|panel| panel.title(...))`——也就是 `TabPanel` 自己也是个 `Panel`，它的 `title` 委托给了当前激活子面板的 `title`。
   - 用编辑器全局搜索 `impl Panel for`，统计项目里有多少种面板实现了 `title`（至少能看到 `TabPanel`、`StackPanel`、`StoryContainer`、`InvalidPanel`）。
3. **需要观察的现象**：`title` 这一个方法名，在「用户层 `Panel`」「库层 `PanelView`」「容器 `TabPanel`」三处都出现，但语义是层层委托的。
4. **预期结果**：你会得到一条 `TabPanel::title → active_panel.title → (PanelView blanket impl) → 你的面板::title` 的调用链。这正是双层 trait 的价值：调用方写的代码完全一样，被调用的真实逻辑千差万别。
5. 如需确认运行时行为，可在自定义面板的 `title` 里加一行 `println!("render title")`，再切换标签观察输出——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果一个面板只想被「放大」、不想出现「关闭」选项，应该覆写哪两个 `Panel` 方法？

> **答案**：保持 `closable` 返回 `false`（[panel.rs:92-94](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs#L92-L94)）即可去掉关闭；放大由 `zoomable` 控制，返回 `Some(...)` 才允许放大。

**练习 2**：为什么 `PanelView` 不能直接要求 `Render`，而要通过 `view() -> AnyView` 间接暴露视图？

> **答案**：`Render::render(&mut self, ...)` 返回 `impl IntoElement`（具体类型），不满足对象安全，无法放进 `dyn`。所以 `PanelView` 改用 `view() -> AnyView` 把视图擦除后交出，容器再统一用 `AnyView` 渲染。

---

### 4.2 TabPanel：标签页容器与拖拽、分屏

#### 4.2.1 概念说明

`TabPanel` 是 Dock 里最常打交道的容器。它对应 `DockItem::Tabs`，内部维护**一组面板**（`panels: Vec<Arc<dyn PanelView>>`）和一个**当前激活下标** `active_ix`——就像浏览器里一个窗口下的多个标签页，同一时间只显示一个。

但 `TabPanel` 不止是「翻页器」，它还承担了 Dock 最核心的交互：**拖拽重排**与**拖拽分屏**。把一个标签拖到另一个标签上，是「合并」；拖到面板的左/右/上/下边缘，是「分屏」。这套交互的判定全在 `TabPanel` 里。

`TabPanel` 自己也实现了 `Panel`（见 [tab_panel.rs:90-160](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L90-L160)），所以**标签页容器本身也能被放进更外层的容器**——这就是 `Split`（背后 `StackPanel`）能无限嵌套的基础。

#### 4.2.2 核心流程

拖拽一个面板标签时的判定与执行流程：

1. **开始拖拽**：在标签上按下并移动，`Tab` 元素通过 `on_drag` 创建一个 `DragPanel`（携带被拖面板 + 来源 `TabPanel` 的引用）作为拖拽物，见 [tab_panel.rs:772-781](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L772-L781)。
2. **拖动中**：在被拖面板**当前所在面板**的正文区上移动时，`on_panel_drag_move` 根据鼠标坐标算出 `will_split_placement`——这是「如果现在松手，会发生哪种分屏」的预判 [tab_panel.rs:914-938](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L914-L938)。
3. **松手（drop）**：`on_drop` 根据 `will_split_placement` 决定是「并入某个标签」还是「调 `split_panel` 真正切出一个新分屏」 [tab_panel.rs:940-991](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L940-L991)。
4. **收尾**：从来源标签移除（`detach_panel`），若来源空了就自我销毁（`remove_self_if_empty`），最后 `cx.emit(PanelEvent::LayoutChanged)` 通知容器「布局变了」。

分屏方向判定的数学很直观。面板正文区是一个矩形，宽 \(w\)、高 \(h\)，鼠标相对左上角为 \((x, y)\)：

- 左侧带：\(x < 0.35 w\) → `Placement::Left`
- 右侧带：\(x > 0.65 w\) → `Placement::Right`
- 顶部带：\(y < 0.35 h\) → `Placement::Top`
- 底部带：\(y > 0.65 h\) → `Placement::Bottom`
- 中心区：以上都不满足 → `None`（并入当前标签）

也就是说面板被四条带 + 中央方框切成「十字 + 中心」五个落区，分别对应左/右/上/下分屏与合并。

#### 4.2.3 源码精读

`TabPanel` 的字段 [tab_panel.rs:67-88](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L67-L88)：

```rust
pub struct TabPanel {
    focus_handle: FocusHandle,
    dock_area: WeakEntity<DockArea>,
    stack_panel: Option<WeakEntity<StackPanel>>,  // 所属父分屏，None 表示不能分屏/移动
    pub(crate) panels: Vec<Arc<dyn PanelView>>,    // 一组面板
    pub(crate) active_ix: usize,                   // 当前激活下标
    pub(crate) closable: bool,                     // 是否允许整体关闭
    // ...
    will_split_placement: Option<Placement>,       // 拖拽预判的分屏方向
    in_tiles: bool,                                // 是否用在 Tiles 布局里
}
```

拖拽物 `DragPanel` 携带了「谁被拖」和「从哪拖来」 [tab_panel.rs:35-45](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L35-L45)：

```rust
pub(crate) struct DragPanel {
    pub(crate) panel: Arc<dyn PanelView>,       // 被拖的面板
    pub(crate) tab_panel: Entity<TabPanel>,     // 来源标签页
}
```

分屏方向判定（注意阈值是 0.35 / 0.65）[tab_panel.rs:914-938](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L914-L938)：

```rust
fn on_panel_drag_move(&mut self, drag: &DragMoveEvent<DragPanel>, _: &mut Window, cx: &mut Context<Self>) {
    let bounds = drag.bounds;
    let position = drag.event.position;
    if position.x < bounds.left() + bounds.size.width * 0.35 {
        self.will_split_placement = Some(Placement::Left);
    } else if position.x > bounds.left() + bounds.size.width * 0.65 {
        self.will_split_placement = Some(Placement::Right);
    } else if position.y < bounds.top() + bounds.size.height * 0.35 {
        self.will_split_placement = Some(Placement::Top);
    } else if position.y > bounds.top() + bounds.size.height * 0.65 {
        self.will_split_placement = Some(Placement::Bottom);
    } else {
        self.will_split_placement = None; // 中心 → 并入当前标签
    }
    cx.notify()
}
```

松手时的 `on_drop` [tab_panel.rs:940-991](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L940-L991) 关键逻辑：

```rust
fn on_drop(&mut self, drag: &DragPanel, ix: Option<usize>, active: bool, window, cx) {
    let panel = drag.panel.clone();
    let is_same_tab = drag.tab_panel == cx.entity();
    // ... 同标签、单面板等边界处理 ...

    // 1. 从来源标签移除（同标签用 detach_panel，跨标签回来源 update）
    if is_same_tab { self.detach_panel(panel.clone(), window, cx); }
    else { let _ = drag.tab_panel.update(cx, |v, cx| { v.detach_panel(...); v.remove_self_if_empty(...); }); }

    // 2. 决定「分屏」还是「并入标签」
    if let Some(placement) = self.will_split_placement {
        self.split_panel(panel, placement, None, window, cx);   // 切新分屏
    } else if let Some(ix) = ix {
        self.insert_panel_at(panel, ix, window, cx)             // 插到指定标签位
    } else {
        self.add_panel_with_active(panel, active, window, cx)   // 追加到末尾
    }

    self.remove_self_if_empty(window, cx);
    cx.emit(PanelEvent::LayoutChanged);
}
```

「能不能拖」由 `draggable` 决定，它要求**未锁定**且**不是最后一个面板** [tab_panel.rs:429-434](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L429-L434)。「是不是最后一个」会沿着父 `StackPanel` 一直向上问（[tab_panel.rs:406-416](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L406-L416)），保证「整个 dock 只剩这一个面板时无法拖走」，否则 dock 会变空。

#### 4.2.4 代码实践：观察分屏阈值

1. **实践目标**：理解「十字 + 中心」五落区，并感受 0.35/0.65 阈值的影响。
2. **操作步骤**：
   - 阅读上面的 `on_panel_drag_move`，在纸上画一个矩形，标出四条宽 \(0.35\) 的边带和中央方框。
   - （可选修改）把 [tab_panel.rs:925-932](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L925-L932) 里的 `0.35` 改成 `0.2`、`0.65` 改成 `0.8`，让边缘带变窄、中心合并区变大。
3. **需要观察的现象**：改之前，鼠标稍微偏离中心就触发分屏；改之后，需要拖到非常靠近边缘才分屏，中心合并区明显变大。
4. **预期结果**：拖拽行为的「分屏灵敏度」与阈值直接相关。这是纯源码阅读即可理解的关系；若要运行验证，可用 `cargo run -p gpui-component-story --example dock`（见 4.4 综合实践）拖动标签观察——**待本地验证**。
5. **注意**：本实践如需修改源码，请在**本地副本**上做，不要改动仓库主干；项目规则禁止修改源码。

#### 4.2.5 小练习与答案

**练习 1**：为什么「整个 dock 只剩最后一个面板」时，这个面板的标签拖不动？

> **答案**：`draggable` 要求 `!is_last_panel(cx)`（[tab_panel.rs:432-434](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L432-L434)），而 `is_last_panel` 会沿父 `StackPanel` 递归向上判断（[tab_panel.rs:406-416](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L406-L416)）。全 dock 仅剩一个面板时，无论从哪一层看都是「最后一个」，故禁止拖走，避免 dock 变空。

**练习 2**：把面板拖到另一个标签页**正中央**松手，会发生什么？和拖到「标签栏空白处」松手有何区别？

> **答案**：拖到正文区正中央 `will_split_placement = None`，面板被 `insert_panel_at` / `add_panel_with_active` 并入目标标签页成为一个新标签（[tab_panel.rs:982-987](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L982-L987)）。拖到标签栏空白处走的是 `last_empty_space` 的 `on_drop`（[tab_panel.rs:810-823](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L810-L823)），效果类似（追加到末尾），但同样不触发分屏。

---

### 4.3 StackPanel：分屏容器

#### 4.3.1 概念说明

`StackPanel` 对应 `DockItem::Split`，是「把一块区域沿水平或垂直方向切成几份」的容器。它的子节点**只能是 `TabPanel` 或另一个 `StackPanel`**（这是硬约束，下面会看到 assert）。切的方向由 `axis: Axis` 决定：`Horizontal` 时子节点左右排，`Vertical` 时上下排。

`StackPanel` 本身不画分隔线和拖拽手柄——那套「可调宽」的交互来自它内部持有的 `ResizablePanelGroup`（来自 `resizable` 模块）。`StackPanel` 只负责「管理子节点列表 + 记录每份尺寸 + 在子节点清空时自我销毁」。

#### 4.3.2 核心流程

`StackPanel` 的几个关键行为：

1. **新增子节点**：`insert_panel` 会先 `assert_panel_is_valid`（必须是 `TabPanel`/`StackPanel`），再去重、插入、用 `ResizableState` 记录尺寸，最后发 `LayoutChanged`。新增时若没给尺寸，就按「容器尺寸 / (现有份数 + 1)」平均分，但**不低于 `PANEL_MIN_SIZE`（100px）**。
2. **建立父子关系**：插入时用 `window.defer` 推迟执行，把子节点的 `parent`（或 `stack_panel`）设为自己，并让 `DockArea` 订阅子节点的事件——推迟是为了避免在构造实体时反向 update 它。
3. **移除与自毁**：`remove_panel` 移除后调 `remove_self_if_empty`；若自己不是 root 且子节点已空，就请父 `StackPanel` 把自己也移除掉，从而让布局树自动「修剪」枯枝。
4. **冒泡 resize 事件**：`StackPanel::new` 里订阅了内部 `ResizableState` 的 `ResizablePanelEvent`，一旦用户拖拽调宽，就向上 `cx.emit(PanelEvent::LayoutChanged)`，最终触发 DockArea 的「布局变了 → 该存盘了」链路。

#### 4.3.3 源码精读

字段 [stack_panel.rs:21-28](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/stack_panel.rs#L21-L28)：

```rust
pub struct StackPanel {
    pub(super) parent: Option<WeakEntity<StackPanel>>,   // 父分屏；None 表示这是 root
    pub(super) axis: Axis,
    focus_handle: FocusHandle,
    pub(crate) panels: SmallVec<[Arc<dyn PanelView>; 2]>, // 子节点（内联 2 个，多了才堆分配）
    state: Entity<ResizableState>,                        // 尺寸状态
    _subscriptions: Vec<Subscription>,
}
```

「子节点类型」硬约束 [stack_panel.rs:106-112](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/stack_panel.rs#L106-L112)：

```rust
fn assert_panel_is_valid(&self, panel: &Arc<dyn PanelView>) {
    assert!(
        panel.view().downcast::<TabPanel>().is_ok()
            || panel.view().downcast::<StackPanel>().is_ok(),
        "Panel must be a `TabPanel` or `StackPanel`"
    );
}
```

这正是 u6-l1 讲过的「Dock 内部不支持把任意 `Panel` 直接塞进 `Split`」的来源——自由内容必须先被 `DockItem::tabs(...)` 包成一个 `TabPanel` 才能进 `Split`。

插入时的尺寸计算与事件冒泡 [stack_panel.rs:254-269](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/stack_panel.rs#L254-L269)：

```rust
let size = match size {
    Some(size) => size,
    None => {
        let state = self.state.read(cx);
        (state.container_size() / (state.sizes().len() + 1) as f32).max(PANEL_MIN_SIZE)
        //                                                           ^^^^^^^^^^^^^^^^
        //                                          不低于 100px，避免面板被挤没
    }
};
self.panels.insert(ix, panel.clone());
self.state.update(cx, |state, cx| { state.insert_panel(Some(size), Some(ix), cx); });
cx.emit(PanelEvent::LayoutChanged);   // 通知布局变化
cx.notify();
```

自我销毁逻辑（非 root 且子节点空）[stack_panel.rs:313-331](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/stack_panel.rs#L313-L331)：先让父 `StackPanel` 把自己 `remove_panel`，再发 `LayoutChanged`。这条递归保证了「关掉最后一个标签 → 空的 `TabPanel` 自毁 → 它所在的 `StackPanel` 若也空了再自毁」，布局树不会留下空壳。

渲染时把子节点交给 `ResizablePanelGroup` [stack_panel.rs:417-434](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/stack_panel.rs#L417-L434)：

```rust
impl Render for StackPanel {
    fn render(&mut self, _: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        h_flex().size_full().overflow_hidden().bg(cx.theme().tokens.tab_bar).child(
            ResizablePanelGroup::new("stack-panel-group")
                .with_state(&self.state)
                .axis(self.axis)
                .children(self.panels.clone().into_iter().map(|panel| {
                    resizable_panel().child(panel.view()).visible(panel.visible(cx))
                    //                                  ^^^^^^^^^^^^^^^^^^^^^^
                    //                       invisible 的面板在此被隐藏但仍占位
                })),
        )
    }
}
```

#### 4.3.4 代码实践：阅读「布局树自动修剪」

1. **实践目标**：理解关闭最后一个标签后，布局树是如何自我修剪的。
2. **操作步骤**：
   - 顺着这条链读源码：`TabPanel::remove_panel`（[tab_panel.rs:331-342](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L331-L342)）→ `remove_self_if_empty`（[tab_panel.rs:358-370](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L358-L370)）→ 父 `StackPanel::remove_panel`（[stack_panel.rs:274-291](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/stack_panel.rs#L274-L291)）→ 父的 `remove_self_if_empty`（[stack_panel.rs:313-331](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/stack_panel.rs#L313-L331)）。
   - 画出两层 `Split`（外层水平、内层垂直）各含一个 `TabPanel` 的场景，设想：关闭内层 `TabPanel` 的最后一个标签，会发生什么？
3. **需要观察的现象**：空的 `TabPanel` 让内层 `StackPanel` 把自己移除 → 内层 `StackPanel` 变空 → 外层 `StackPanel` 把内层也移除。这是一段「沿父链向上」的递归清理。
4. **预期结果**：`is_root`（[stack_panel.rs:78-80](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/stack_panel.rs#L78-L80)）判断「`parent` 为 `None` 即根」，根 `StackPanel` 即使空了也不会自毁（它是中心区的兜底容器）。所以递归清理会在根节点停下。
5. 这是源码阅读型实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `StackPanel` 用 `SmallVec<[Arc<dyn PanelView>; 2]>` 而不是普通 `Vec`？

> **答案**：分屏容器绝大多数情况下只有 2 个子节点（一次切一刀）。`SmallVec<[T; 2]>` 在元素 ≤ 2 时内联存储（不堆分配），正好契合这个高频场景，减少分配开销。

**练习 2**：直接把一个 `Entity<MyPanel>`（实现了 `Panel` 但不是 `TabPanel`）塞给 `StackPanel::add_panel` 会怎样？

> **答案**：`assert_panel_is_valid`（[stack_panel.rs:106-112](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/stack_panel.rs#L106-L112)）会 panic。正确做法是用 `DockItem::tab(my_panel, ...)` 先包成 `TabPanel`，这也是 `DockItem::split_with_sizes` 内部的做法（[mod.rs:216-259](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L216-L259)）。

---

### 4.4 PanelRegistry：序列化、反序列化与 InvalidPanel 兜底

#### 4.4.1 概念说明

这是本讲的「收官」模块，回答「整面墙怎么拍照存档、下次怎么照着照片复原」。整条链路围绕三个角色：

- **`PanelRegistry`（全局注册表）**：一个注册到 `App` 的 `Global`，本质是「面板名 → 反序列化闭包」的哈希表。`dock::init(cx)` 会把它建好。**反序列化时，库只认 `panel_name` 字符串，靠注册表找到对应的闭包来重建实体。**
- **`PanelState`（序列化节点）**：一棵和 `DockItem` 一一对应的树。每个节点记录 `panel_name`、`children`、以及一个 `info`。
- **`PanelInfo`（节点附加信息）**：区分四种节点——`Stack{sizes, axis}`、`Tabs{active_index}`、`Panel(serde_json::Value)`（叶子面板的自定义状态）、`Tiles{metas}`。

存盘用 `DockArea::dump(cx) -> DockAreaState`，恢复用 `DockArea::load(state, ...)`。两者互为逆操作，中间的桥梁就是「递归地把 `PanelState` 重建为 `DockItem`」的 `to_item`。

关键约束：**叶子面板（你的自定义 `Panel`）的真实业务状态，库无法替你猜**。`Panel::dump` 的默认实现只存了 `panel_name`，所以你必须**覆写 `dump` 把自己的状态塞进 `PanelInfo::Panel(json)`**，并且**用 `register_panel` 注册一个能从那段 json 重建实体的闭包**——这两件事必须成对出现，否则恢复后面板会丢状态，甚至变成 `InvalidPanel`。

#### 4.4.2 核心流程

完整的「存 → 取」闭环：

```
DockArea::dump                          DockArea::load
   │                                        │
   ▼                                        ▼
center.view().dump(cx)                 state.center.to_item(...)
   │  (递归)                                │  (递归)
   ▼                                        ▼
每个面板.dump(cx) -> PanelState         按 PanelInfo 分派：
   │                                        ├─ Stack  → DockItem::split_with_sizes
   │                                        ├─ Tabs   → DockItem::tabs(...).active_index
   │                                        ├─ Panel  → PanelRegistry::build_panel(name, ...)
   │                                        └─ Tiles  → DockItem::tiles(...)
   ▼                                        │
DockAreaState  ──── serde_json ────►  文件 / 内存  ──── serde_json ────► DockAreaState
```

反序列化叶子的关键在 `PanelRegistry::build_panel`：

1. 用 `panel_name` 去 `items` 表里查闭包。
2. **查到** → 调闭包，传入 `dock_area`、`PanelState`、`PanelInfo`，闭包负责 `cx.new` 一个新实体并返回 `Box<dyn PanelView>`。
3. **没查到** → 返回一个 `InvalidPanel` 占位（不会 panic，但面板内容变成一行「该面板类型未注册」的提示，并保留 `old_state` 以便下次再 dump 时原样写回）。

#### 4.4.3 源码精读

序列化数据结构 [state.rs:68-109](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/state.rs#L68-L109)：

```rust
#[derive(Serialize, Deserialize)]
pub struct PanelState {
    pub panel_name: String,
    pub children: Vec<PanelState>,
    pub info: PanelInfo,
}

#[derive(Serialize, Deserialize)]
pub enum PanelInfo {
    Stack { sizes: Vec<Pixels>, axis: usize }, // axis: 0=水平, 1=垂直
    Tabs { active_index: usize },
    Panel(serde_json::Value),                   // 叶子面板的自定义状态（任意 JSON）
    Tiles { metas: Vec<TileMeta> },
}
```

注意 `axis` 被序列化成了 `usize`（0/1）而非枚举，`Panel(serde_json::Value)` 用 `serde_json::Value` 承载任意业务状态——这是「库不关心你存什么」的关键。

`TabPanel` 和 `StackPanel` 各自如何 dump：

- `TabPanel::dump`（[tab_panel.rs:147-154](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L147-L154)）：把每个子面板 `dump` 成 child，`info` 设为 `Tabs{active_index: self.active_ix}`。
- `StackPanel::dump`（[stack_panel.rs:44-53](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/stack_panel.rs#L44-L53)）：把每个子节点 `dump` 成 child，`info` 设为 `Stack{sizes, axis}`，sizes 来自 `ResizableState`。

逆向重建 `PanelState::to_item`（最核心的一段）[state.rs:171-222](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/state.rs#L171-L222)：

```rust
fn to_item(&self, dock_area, window, cx) -> DockItem {
    let info = self.info.clone();
    let items: Vec<DockItem> = self.children.iter()
        .map(|child| child.to_item(dock_area.clone(), window, cx))   // 递归重建子树
        .collect();
    match info {
        PanelInfo::Stack { sizes, axis } => DockItem::split_with_sizes(axis, items, sizes, ...),
        PanelInfo::Tabs { active_index } => {
            // 把子项摊平成面板列表（跳过 invalid），再建 Tabs 并设 active_index
            DockItem::tabs(items_flat, ...).active_index(active_index, cx)
        }
        PanelInfo::Panel(_) => {
            // 叶子：靠注册表按名字重建
            let view = PanelRegistry::build_panel(&self.panel_name, dock_area, self, &info, window, cx);
            DockItem::tabs(vec![view.into()], ...)
        }
        PanelInfo::Tiles { metas } => DockItem::tiles(items, metas, ...),
    }
}
```

注册表本体 [panel.rs:293-353](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs#L293-L353)：

```rust
pub struct PanelRegistry {
    pub(super) items: HashMap<String, Arc<dyn Fn(WeakEntity<DockArea>, &PanelState, &PanelInfo, &mut Window, &mut App) -> Box<dyn PanelView>>>,
}

pub fn build_panel(panel_name, dock_area, panel_state, panel_info, window, cx) -> Box<dyn PanelView> {
    if let Some(view) = Self::global(cx).items.get(panel_name).cloned()
        .map(|f| f(dock_area, panel_state, panel_info, window, cx)) {
        return view;                          // 查到 → 调闭包重建
    } else {
        Box::new(cx.new(|cx| InvalidPanel::new(panel_name, panel_state.clone(), window, cx)))
        //      ^^^^^^^^ 没查到 → InvalidPanel 兜底（不 panic）
    }
}
```

注册函数 `register_panel` [panel.rs:356-371](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs#L356-L371) 把「面板名 + 反序列化闭包」插进表里：

```rust
pub fn register_panel<F>(cx: &mut App, panel_name: &str, deserialize: F)
where F: Fn(WeakEntity<DockArea>, &PanelState, &PanelInfo, &mut Window, &mut App) -> Box<dyn PanelView> + 'static
{
    PanelRegistry::init(cx);
    PanelRegistry::global_mut(cx).items.insert(panel_name.to_string(), Arc::new(deserialize));
}
```

兜底面板 `InvalidPanel` 会保留原始状态 [invalid_panel.rs:10-33](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/invalid_panel.rs#L10-L33)：它的 `dump` 直接返回 `self.old_state.clone()`。这意味着「即使某次启动没注册某面板，存盘时也会把它的原始状态原样写回」——不会因为一次未注册就永久丢失布局信息。

最后是入口 [mod.rs:957-981](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L957-L981) 的 `DockArea::dump`：`center.view().dump(cx)` 递归拍照，三个 dock 各自走 `DockState::new`；[mod.rs:928-952](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L928-L952) 的 `load` 则把每个 dock 的 `DockState::to_dock` 和 center 的 `to_item` 拼回来。

**真实序列化产物长什么样？** 项目自带一份测试夹具 [crates/ui/src/fixtures/layout.json](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/fixtures/layout.json)，节选 center 区：

```json
{
  "center": {
    "panel_name": "StackPanel",
    "children": [
      {
        "panel_name": "TabPanel",
        "children": [
          { "panel_name": "StoryContainer", "children": [],
            "info": { "panel": { "story_klass": "ButtonStory" } } }
          // ... 更多 StoryContainer ...
        ],
        "info": { "tabs": { "active_index": 0 } }
      }
    ],
    "info": { "stack": { "sizes": [704.0, 263.0], "axis": 1 } }
  }
}
```

可以清楚看到「StackPanel 包 TabPanel 包 StoryContainer（叶子，info.panel 存了 story_klass）」的嵌套，与 `DockItem` 树一一对应。

#### 4.4.4 代码实践：实现一个可序列化的自定义 Panel

这是本讲的主实践，对应综合实践的前半部分。我们仿照 `crates/story/examples/tiles.rs` 里的 `ContainerPanel` 和 `StoryContainer`，从零写一个**带自定义状态、能存盘恢复**的面板。

1. **实践目标**：把「定义 `Panel` → 覆写 `dump` → 注册反序列化闭包」三步亲手做一遍，验证恢复后面板状态不丢。

2. **操作步骤**：

   (a) 准备一个最小应用骨架（参考 [crates/story/examples/dock.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/examples/dock.rs)）。在你的 `main` 里依次调用 `gpui_component::init(cx)`、注册面板、打开窗口。

   (b) 定义一个带计数器状态的面板。**示例代码**（非项目原有，仅作演示）：

   ```rust
   use gpui::{App, Context, EventEmitter, FocusHandle, Focusable, IntoElement, Render, Window};
   use gpui_component::dock::{Panel, PanelEvent, PanelInfo, PanelState};
   use serde::{Deserialize, Serialize};

   #[derive(Serialize, Deserialize, Clone)]
   struct CounterState { count: i32 }               // ① 要存盘的业务状态

   struct CounterPanel { count: i32, focus: FocusHandle }

   impl Panel for CounterPanel {
       fn panel_name(&self) -> &'static str { "CounterPanel" }   // ② 名字一旦定义不要改

       // ③ 覆写 dump：把 count 塞进 PanelInfo::Panel(json)
       fn dump(&self, _cx: &App) -> PanelState {
           let mut state = PanelState::new(self);
           state.info = PanelInfo::panel(
               serde_json::to_value(CounterState { count: self.count }).unwrap()
           );
           state
       }
       // title / render 略：显示 "Count: {count}" 即可
   }
   impl EventEmitter<PanelEvent> for CounterPanel {}
   impl Focusable for CounterPanel { fn focus_handle(&self, _: &App) -> FocusHandle { self.focus.clone() } }
   // impl Render ...
   ```

   (c) **成对地**注册反序列化闭包（与 dump 必须配套）：

   ```rust
   use gpui_component::dock::{PanelInfo, PanelRegistry, register_panel};

   register_panel(cx, "CounterPanel", |_dock_area, _state, info, window, cx| {
       // ④ 从 info 里把状态读回来，重建实体
       let count = match info {
           PanelInfo::Panel(v) => serde_json::from_value::<CounterState>(v.clone()).unwrap().count,
           _ => 0,
       };
       Box::new(cx.new(|cx| CounterPanel { count, focus: cx.focus_handle() }))
   });
   ```

   (d) 把面板放进 `DockArea`（用一个 `DockItem::tabs` 或 `tab`），参考 [crates/story/examples/dock.rs:335-371](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/examples/dock.rs#L335-L371) 的 `init_default_layout`。

   (e) 存盘/读盘照搬 dock 示例：`dock_area.read(cx).dump(cx)` → `serde_json::to_string_pretty` → 写文件；读盘用 `serde_json::from_str::<DockAreaState>` → `dock_area.load(state, ...)`。见 [crates/story/examples/dock.rs:207-260](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/examples/dock.rs#L207-L260)。

3. **需要观察的现象**：
   - 把 `count` 加到 5，退出（触发 `on_app_quit` 存盘），重启应用 → 面板应显示 `Count: 5`（状态被恢复）。
   - **故意注释掉 `register_panel` 那段**再重启 → 面板变成 `InvalidPanel`，显示「`CounterPanel` panel type is not registered」，且文件里的 `CounterPanel` 节点仍被原样保留（因 `InvalidPanel::dump` 回吐 `old_state`）。

4. **预期结果**：注册时状态闭环正常；未注册时优雅降级为 `InvalidPanel` 而非崩溃。

5. 如需运行，参考下一节「综合实践」的命令；本机未跑过，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`PanelInfo::Panel` 为什么用 `serde_json::Value` 而不是泛型 `PanelInfo::Panel<T>`？

> **答案**：`PanelState` 要能 `Serialize + Deserialize` 并放进 `dyn`/`Global` 语境，泛型参数会让枚举无法擦除类型、也无法统一存档。用 `serde_json::Value` 把「业务状态」当成不透明的 JSON，库就能在不关心具体类型的前提下搬运它，类型还原的责任交给每个面板自己的 `register_panel` 闭包。

**练习 2**：如果某面板的 `panel_name` 在两个版本之间被改了名，老存档恢复时会怎样？

> **答案**：`build_panel` 按新名字查不到旧闭包（[panel.rs:340-351](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs#L340-L351)），返回 `InvalidPanel` 占位，但原始 `PanelState` 被 `old_state` 保留。这就是为什么 trait 文档强调「`panel_name` 一旦定义就不能改」（[panel.rs:55-59](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs#L55-L59)）。要兼容老存档，可以同时注册新旧两个名字指向同一个闭包。

**练习 3**：`DockAreaState` 里有个 `version` 字段（[state.rs:13-14](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/state.rs#L13-L14)），它解决什么问题？

> **答案**：当你彻底重构了面板结构（比如增删字段），老存档可能无法直接用。`version` 让你在加载时比对版本号，发现不兼容就提示用户「重置为默认布局」——dock 示例的 `load_layout` 正是这么做的（[crates/story/examples/dock.rs:222-243](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/examples/dock.rs#L222-L243)）。

---

## 5. 综合实践：自定义 Panel + 拖拽重排 + 序列化恢复

把本讲四块串起来，完成一个完整任务（即本讲的 `practice_task`）：

**任务**：实现一个自定义 `Panel`，放进 `DockArea` 并启用拖拽重排，运行后将布局序列化为 JSON 再恢复。

**推荐做法**：直接复用项目自带的 dock 示例作为脚手架，它已经把 DockArea、存盘、读盘、版本检查全部写好了，你只需把其中一个面板换成自己的。

1. **运行脚手架**：

   ```bash
   cargo run -p gpui-component-story --example dock
   ```

   你会看到 Story Gallery 那套 dock 布局。试着拖动标签到面板左/右/上/下边缘，观察分屏；拖到中心或标签栏空白处，观察合并。这验证了 4.2 的拖拽/分屏逻辑。

2. **阅读脚手架的关键实现**（全部在 [crates/story/examples/dock.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/examples/dock.rs)）：
   - `StoryWorkspace::save_layout`（[L179-205](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/examples/dock.rs#L179-L205)）：订阅 `DockEvent::LayoutChanged`，用 10 秒定时器**防抖**后调 `dump` 存盘——因为 `LayoutChanged` 触发非常频繁（拖一次发很多次）。
   - `load_layout`（[L214-260](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/examples/dock.rs#L214-L260)）：读 `target/docks.json`，比版本，调 `load`。
   - `on_app_quit`（[L92-102](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/examples/dock.rs#L92-L102)）：退出时再存一次。

3. **加入你的自定义面板**：按 4.4.4 的 `CounterPanel` 示例，在 `main` 里（`init` 之后）调用 `register_panel(cx, "CounterPanel", ...)`，并在 `init_default_layout` 里用 `DockItem::tab(CounterPanel::new(...), ...)` 加一个标签。

4. **验证完整闭环**：
   - 调整布局（拖拽分屏、关标签）、把计数器加到某个值，正常退出。
   - 检查 `target/docks.json`，应能看到一个 `"panel_name": "CounterPanel"` 的节点，`info.panel` 里带着你的 `{ "count": N }`。
   - 再次运行 `cargo run -p gpui-component-story --example dock`，布局与计数器值都应恢复。

5. **观察 InvalidPanel 降级**：注释掉 `register_panel` 那行重新运行，确认面板变为 `InvalidPanel` 提示而非崩溃，且 JSON 中节点仍被保留。

> 关于拖拽：dock 示例的 `DockArea` 默认未锁定（`is_locked` 为 false），所以标签可拖。若发现拖不动，检查是否调过 `set_locked(true)`（[mod.rs:699-708](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L699-L708)），锁定后只允许调宽、不允许分屏/移动。

以上运行步骤**待本地验证**（本环境为只读分析环境，未实际编译运行）。

## 6. 本讲小结

- gpui-component 用**双层 trait** 抽象面板：你实现带具体类型的 `Panel`，容器持有擦除类型的 `dyn PanelView`，二者靠 `impl<T: Panel> PanelView for Entity<T>` 这一个 blanket impl 衔接，调用方代码统一、被调逻辑各异。
- `TabPanel` 是标签页容器（`DockItem::Tabs`），管一组面板 + `active_ix`，并承载拖拽与分屏交互：鼠标落点经 `on_panel_drag_move` 按 0.35/0.65 阈值判定为「左/右/上/下分屏」或「中心并入」，`on_drop` 据此走 `split_panel` 或 `insert_panel_at`。
- `StackPanel` 是分屏容器（`DockItem::Split`），子节点**只能是 `TabPanel` 或 `StackPanel`**（`assert_panel_is_valid`），把可调宽交互委托给 `ResizablePanelGroup`，并在子节点清空时沿父链自我销毁、自动修剪布局树。
- 序列化是「`PanelState` + `PanelInfo`」与 `DockItem` 一一对应的树形存档：`Stack/Tabs/Panel/Tiles` 四种 `PanelInfo` 对应四类节点。叶子面板的业务状态由你覆写 `Panel::dump` 塞进 `PanelInfo::Panel(json)`。
- 反序列化靠全局 `PanelRegistry`：按 `panel_name` 查反序列化闭包重建实体；**查不到不 panic，返回保留原状态的 `InvalidPanel` 占位**。`register_panel`（注册）与 `Panel::dump`（存档）必须成对实现，否则恢复后丢状态。
- `DockArea::dump` / `DockArea::load` 是存取入口，`PanelState::to_item` 是递归重建的核心；`DockAreaState.version` 用于跨版本兼容，布局变化事件 `LayoutChanged` 需防抖后再存盘。

## 7. 下一步学习建议

- **u6-l3 Tiles：自由拼贴布局**：本讲的 `DockItem::Split` 只能做规则的分屏，而 `Tiles` 允许面板像浮窗一样自由摆放、边缘吸附。建议接着读 `crates/ui/src/dock/tiles.rs`，看 `PanelInfo::Tiles{metas}` 里的 `TileMeta{bounds, z_index}` 是如何描述自由位置与层级的——它与本讲的 `Stack`/`Tabs` 是同级的第四种布局节点。
- **重读 u6-l1**：现在你已经掌握了节点内部（本讲），回头再看 u6-l1 的 `DockItem` 布局树与 `DockEvent` 中继，会有「从骨架到血肉」的完整感。
- **横向对比**：把本讲的「双层 trait + 类型擦除」与 u5-l5 Select/Combobox 的「状态实体 + 无状态外壳」对比阅读，体会 gpui-component 在不同复杂度下复用的同一套抽象思想。
- **动手扩展**：试着给你的自定义面板实现 `toolbar_buttons` 和 `dropdown_menu`（[panel.rs:136-153](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs#L136-L153)），让标题栏右侧出现自定义工具按钮——这是把面板做成「真正可用」的最后一块拼图。
