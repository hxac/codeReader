# Tiles：自由拼贴布局

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `Tiles` 与 u6-l1 讲过的 `Split`/`Tabs` 布局在「排布模型」上的根本区别——为什么一个叫「分屏树」、一个叫「自由画布」。
- 理解 `Tiles` 的内部数据结构：它如何用一张 `Vec<TileItem>` 加上每个面板的绝对像素矩形（`Bounds<Pixels>`）来表达整张画布。
- 读懂拖动面板时「磁性对齐」与调整面板尺寸时「边缘吸附」两套吸附算法的源码，并能解释它们各自的候选点、阈值和回退策略。
- 知道面板的层级（`z_index`）、撤销/重做（`History`）以及布局序列化（`TileMeta`/`PanelInfo::Tiles`）是如何与吸附机制配合的。
- 能够运行 `tiles` 示例、亲手拖动与缩放面板，并对照源码记录你观察到的吸附行为。

## 2. 前置知识

本讲是专家层 Dock 单元的第三讲，承接 u6-l1（`DockArea` 与 `DockItem` 布局树）与 u6-l2（`Panel`/`StackPanel`/`TabPanel` 与序列化）。阅读本讲前，你需要先建立以下认知：

- **布局树四种节点**：`DockItem` 有 `Split`（分屏，唯一可递归）、`Tabs`（标签页）、`Panel`（单面板叶子）、`Tiles`（自由拼贴）。本讲主角就是第四种 `Tiles`。
- **`Panel` trait 与 `PanelView`**：用户实现带具体类型的 `Panel`，容器持有擦除类型的 `dyn PanelView`，靠 blanket impl 衔接（u6-l2）。
- **序列化往返**：布局经 `dump`/`load` 与 `DockAreaState` 持久化，靠全局 `PanelRegistry` 按 `panel_name` 重建实体（u6-l2）。
- **GPUI 坐标与 `Bounds<Pixels>`**：`Bounds { origin: Point, size: Size }` 用绝对像素描述一个矩形；`left()/right()/top()/bottom()` 是它的四条边。
- **`WeakEntity` 与 `window.defer`**：构造布局节点时常传入 `&WeakEntity<DockArea>` 以建立实体并订阅事件，跨实体操作用 `window.defer` 推迟到下一帧（u6-l1）。

> 名词解释：本讲反复出现的「吸附（snap）」指——当面板的某条边（或某个角）离一个「目标位置」足够近时，系统自动把这条边对齐到该目标位置，就像被磁铁吸住一样。「目标位置」可能是画布的左/上边界（`0`）、另一个面板的某条边、或是网格的整数倍刻度。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/ui/src/dock/tiles.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs) | 本讲核心，约 1430 行。`Tiles` 视图、`TileItem` 数据项、拖动/缩放/吸附/撤销全部逻辑，以及单元测试。 |
| [crates/ui/src/dock/mod.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs) | 定义 `DockItem::Tiles` 变体与构造器 `DockItem::tiles`、`subscribe_tiles_item_drop`，以及中心区渲染 `Tiles` 的分支。 |
| [crates/ui/src/dock/state.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/state.rs) | 序列化结构 `TileMeta`（`bounds`+`z_index`）与 `PanelInfo::Tiles { metas }`，以及反序列化时调回 `DockItem::tiles` 的分支。 |
| [crates/ui/src/dock/tab_panel.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs) | `set_in_tiles` 标记：进入 Tiles 后 `TabPanel` 的可关闭性与「空了自毁」行为会改变。 |
| [crates/ui/src/theme/mod.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/theme/mod.rs) | 吸附与圆角的两个主题参数：`tile_grid_size`（吸附/网格刻度，默认 8px）、`tile_radius`（圆角，默认 0）。 |
| [crates/story/examples/tiles.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/examples/tiles.rs) | 可直接运行的 Tiles 示例，本讲综合实践的依据。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**4.1 Tiles 的结构与定位**、**4.2 面板拖动与磁性对齐**、**4.3 面板边缘吸附调整尺寸**、**4.4 持久化、层级与撤销重做**。其中 4.2 与 4.3 是「panel 边缘吸附」的两条主线——拖动时吸附的是「面板整体的对齐线」，缩放时吸附的是「正在移动的那条边」。

---

### 4.1 Tiles：自由拼贴画布与它在布局树中的定位

#### 4.1.1 概念说明

回到 u6-l1 的布局树：`Split` 是一棵递归二叉分屏树，每个子节点的大小由比例（`sizes`）和父容器宽度共同决定，子节点不能「自由漂浮」——它们被父容器牢牢约束在网格里。

`Tiles` 走的是完全相反的路子：它是一张**自由画布（freeform canvas）**。画布上每个面板都有自己独立的绝对坐标和尺寸（一个 `Bounds<Pixels>`），面板之间互不约束，可以重叠、可以错位、可以像桌面上的窗口一样任意摆放。你可以把它理解成 IDE 里那种「可拖来拖去、可叠放」的浮动面板区，而不是死板的左右分屏。

正因为是自由画布，`Tiles` 在布局树里是一个**叶子型容器**：它只能装面板，不能再嵌套 `Split`/`Tabs`（构造时会丢弃非 `Tabs`/`Panel` 的子项）。它通常作为 `DockArea` 的**中心区**（`set_center`）出现，因为停靠位（左/下/右 dock）内部并不支持 Tiles。

#### 4.1.2 核心流程

`Tiles` 的数据模型极简：

```
Tiles {
    panels: Vec<TileItem>,          // 一张「无序」面板表，顺序 == 层级顺序
    dragging_id / resizing_id,      // 当前正在拖动 / 缩放的面板 id
    bounds: Bounds<Pixels>,         // 画布自身的像素矩形（on_prepaint 时回填）
    history: History<TileChange>,   // 撤销重做栈
    scroll_handle: ScrollHandle,    // 画布可滚动（面板超出视口）
    ...
}

TileItem {
    id: EntityId,                   // 取自所装面板 view 的 entity_id
    panel: Arc<dyn PanelView>,      // 真正的面板（只允许 TabPanel）
    bounds: Bounds<Pixels>,         // 这个面板在画布上的绝对矩形
    z_index: usize,                 // 层级，越大越靠上
}
```

渲染时，`Tiles` 把每个 `TileItem` 用 `absolute()` 定位到它的 `bounds`，再按 `z_index` 排序后从下到上叠放。整个画布套在一个可滚动容器里，所以即便面板摆得比窗口还大，也能滚动查看。

> 关键直觉：`Split` 是「关系驱动」的布局（父约束子），`Tiles` 是「坐标驱动」的布局（每个面板自带绝对坐标）。这一差异决定了 Tiles 必须自己实现吸附、层级、撤销这些「自由摆放」才需要的机制——而 Split 模式根本不需要它们。

#### 4.1.3 源码精读

先看 `Tiles` 的字段定义，注意它实现了 `Panel`（所以它本身也能被当作一个面板放进布局树）、`Render`、`Focusable`，并向外 emit `PanelEvent`/`DismissEvent`/`DragDrop`：

[crates/ui/src/dock/tiles.rs:131-144](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L131-L144) —— `Tiles` 结构体，核心是 `panels: Vec<TileItem>` 与三组拖拽/缩放临时状态。

`TileItem` 是面板在画布上的「一条记录」：

[crates/ui/src/dock/tiles.rs:84-116](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L84-L116) —— `TileItem` 持有面板引用与绝对 `bounds`，`id` 由面板 view 的 `entity_id()` 派生，`z_index` 可链式设置。

再看它在布局树中的「身份」——`DockItem::Tiles` 变体：

[crates/ui/src/dock/mod.rs:103-109](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L103-L109) —— `DockItem::Tiles { size, items, view }`，其中 `view: Entity<Tiles>` 是真正的画布实体，`items` 是面板快照。

中心区渲染时，`DockArea` 专门为 `Tiles` 开了一个分支（与 Split/Tabs 那套 flex 分栏逻辑并列）：

[crates/ui/src/dock/mod.rs:1130-1134](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L1130-L1134) —— 当中心区是 `Tiles` 时直接 `this.child(view.clone())` 渲染整张画布，不走左/中/右 dock 的 flex 布局。

而构造一个 `Tiles` 节点的入口是 `DockItem::tiles`。它会创建 `Tiles` 实体，把传入的每个子项（只能是 `Tabs` 或 `Panel`）包成 `TileItem` 并 `add_item`：

[crates/ui/src/dock/mod.rs:269-321](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L269-L321) —— `DockItem::tiles(items, metas, dock_area, window, cx)`，`items` 与 `metas`（每个面板的初始 `bounds`/`z_index`）必须等长（`assert!`），非 Tabs/Panel 子项被静默忽略；构造后用 `window.defer` 订阅面板事件与拖放事件。

`add_item` 内部有一个硬约束——**只允许往画布里加 `TabPanel` 类型**，否则直接 panic：

[crates/ui/src/dock/tiles.rs:494-526](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L494-L526) —— `add_item` 把 `TabPanel` 标记为 `set_in_tiles(true)`，加入 `panels`，并 `window.defer` 让 `DockArea` 订阅该面板的事件。

为什么必须是 `TabPanel`？因为 Tiles 上面板要能被「拖出画布」或「关闭」，而这套可关闭、可拖拽、可空自毁的行为都封装在 `TabPanel` 里（见 4.4）。`set_in_tiles(true)` 会改变 `TabPanel` 的可关闭判定：

[crates/ui/src/dock/tab_panel.rs:185-188](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L185-L188) —— `set_in_tiles` 置位后，`closable` 在「不可拖拽」时也允许关闭（见下一行的 `|| self.in_tiles`）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认「Tiles 是叶子型自由画布、且只接受 TabPanel」这条结论。
2. **操作步骤**：
   - 打开 [crates/ui/src/dock/mod.rs:269-321](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L269-L321)，找到 `match item` 分支，数一下它接受哪几种 `DockItem`、其余走 `_ =>` 被丢弃。
   - 打开 [crates/ui/src/dock/tiles.rs:494-526](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L494-L526)，确认 `downcast::<TabPanel>()` 失败时 `panic!("only allows to add TabPanel type")`。
3. **需要观察的现象**：构造分支只匹配 `DockItem::Tabs` 与 `DockItem::Panel` 两种；`Panel` 变体里的 `view` 也会被要求能 downcast 成 `TabPanel`（否则 `add_item` panic）。
4. **预期结果**：你能用自己的话回答「为什么往 `DockItem::tiles` 里塞一个 `DockItem::split(...)` 不会报错但也看不到它」——因为它命中了 `_ =>` 分支被忽略。
5. 待本地验证：无（纯静态阅读即可确认）。

#### 4.1.5 小练习与答案

**练习 1**：`TileItem::new(panel, bounds)` 里 `id` 字段是怎么来的？为什么不直接用自增整数？

**参考答案**：`id` 取自 `panel.view().entity_id()`（[tiles.rs:103-110](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L103-L110)）。用面板自身的 `EntityId` 而非自增整数，是为了让 `TileItem` 的身份与底层 `Entity` 绑定——这样面板被拖动、序列化、撤销重做时，都能通过同一个 `entity_id` 找回它在 `panels` 列表里的位置（`index_of`），跨帧追踪同一块面板。

**练习 2**：`DockItem::tiles` 的签名要求 `items` 和 `metas` 等长（`assert!`），如果长度不等会发生什么？这是编译期还是运行期检查？

**参考答案**：会触发 `assert!(items.len() == metas.len())`（[mod.rs:279](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L279)），在运行期 panic。因为 `metas: Vec<impl Into<TileMeta> + Copy>` 和 `items: Vec<DockItem>` 是两个独立的动态长度向量，编译器无法在编译期证明它们等长，只能在运行期用断言守护，避免 `metas[ix]` 越界。

---

### 4.2 面板拖动与磁性对齐

#### 4.2.1 概念说明

在自由画布上拖动一块面板，如果完全跟手、不做任何对齐，用户很难把两块面板的边挨得严丝合缝——总会差那么几像素。`Tiles` 的做法是「磁性对齐（magnetic snap）」：拖动过程中，一旦被拖面板的某条边（左/右/上/下）离一个「目标位置」小于阈值，就把整块面板的对应坐标「啪」地吸过去。

候选的「目标位置」有两类：

1. **画布边缘**：左边界 `x=0`、上边界 `y=0`（拖到画布左上角时吸附）。
2. **其他面板的四条边**：画布上每一块其他面板的 `left/right/top/bottom`。

阈值取自主题参数 `cx.theme().tile_grid_size`（默认 8px）。注意：磁性对齐发生在**拖动期间**的每一帧（实时吸附），并且 X、Y 两轴独立处理——可能只吸 X 不吸 Y。

#### 4.2.2 核心流程

拖动一块面板 P 的每一帧，`update_position` 做的事：

```
1. 算出「跟手」的新原点 new_origin = 初始bounds原点 + (当前鼠标 - 初始鼠标)
2. 用 (new_origin, 初始尺寸) 构造一个 dragging_bounds
3. calculate_magnetic_snap(dragging_bounds, P的下标, 阈值) -> (snap_x, snap_y)
     - 先试画布左/上边缘 (0)
     - 再遍历其他面板，对 P 的四条边收集「最近」的吸附点
     - 两轴都已找到则提前返回
4. 把 snap_x/snap_y 覆盖到 new_origin
5. apply_boundary_constraints：上边界不低于0，左边界最多移到「还剩64px可见」
6. 写回 panels[ix].bounds.origin，push 一条 TileChange 到 history，cx.notify()
```

松手时（`on_mouse_up`）还有一次「网格圆整」：把最终原点对齐到 `tile_grid_size` 的整数倍，保证面板落点整齐。

还有一个贯穿全流程的小动作——`bring_to_front`：只要开始拖动一块面板，就把它挪到 `panels` 末尾（层级最高），这样它一定盖在其他面板之上。

#### 4.2.3 源码精读

先看磁性吸附的核心算法 `calculate_magnetic_snap`。它先用 `search_bounds`（被拖面板外扩一个阈值）圈定「附近的候选面板」，跳过远处的面板以减少计算；然后对 X、Y 两轴各自找最近候选点：

[crates/ui/src/dock/tiles.rs:242-359](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L242-L359) —— `calculate_magnetic_snap`。注意 X 轴的四个候选点：被拖面板左缘对齐他板左缘、左缘对齐他板右缘、右缘对齐他板左缘（此时原点要减去自身宽度）、右缘对齐他板右缘。`min_x_dist`/`min_y_dist` 起始就是阈值，只有严格小于它才更新，因此「距离恰等于阈值」不会吸附。

X 轴的四个候选点尤其值得品味（[tiles.rs:316-330](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L316-L330)）：

```text
(drag_left - other_left).abs()   => 把原点吸到 other_left   // 左缘贴左缘
(drag_left - other_right).abs()  => 把原点吸到 other_right  // 左缘贴他板右缘
(drag_right - other_left).abs()  => 把原点吸到 other_left - drag_width  // 右缘贴他板左缘
(drag_right - other_right).abs() => 把原点吸到 other_right - drag_width  // 右缘贴他板右缘
```

后两个候选把「右缘对齐」换算回「原点对齐」（原点 = 目标边 − 自身宽度），这样统一用 `new_origin.x = snap_x` 赋值即可。

接着是边界约束 `apply_boundary_constraints`：

[crates/ui/src/dock/tiles.rs:362-375](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L362-L375) —— 上边界强制 `y >= 0`（面板不能整个拖到画布上方看不见）；左边界允许部分出屏，但至少保留 64px 可见，`min_left = -width + 64`。

把吸附与约束串起来的是 `update_position`：

[crates/ui/src/dock/tiles.rs:377-431](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L377-L431) —— 注意顺序是「先磁性吸附 → 再边界约束」：这样即使吸附把面板推出了可视区，边界约束也会把它拉回来。注释明确说「smooth dragging」不在此处做网格圆整，圆整留到松手。

「拖动期间层级置顶」由 `bring_to_front` 完成：

[crates/ui/src/dock/tiles.rs:535-562](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L535-L562) —— `bring_to_front` 把目标面板 `remove` 后 `push` 到末尾（末尾 = `z` 序最高），并往 history 压一条 `old_order/new_order` 变更用于撤销层级。

> 数学说明：吸附判定本质是在一维上求「最近邻」。设被拖边坐标为 \(e\)，候选点集合为 \(C\)，阈值为 \(t\)，则吸附目标为
>
> \[
> \operatorname{snap}(e)=\begin{cases}\arg\min_{c\in C}|e-c|, & \min_{c\in C}|e-c| < t\\ \text{不吸附}, & \text{否则}\end{cases}
> \]
>
> 这正是 `calculate_magnetic_snap` 用 `min_x_dist`/`min_y_dist`（初值即阈值）逐步取更小值实现的——把「阈值」与「当前最小距离」合并成一个变量，简洁且高效。

#### 4.2.4 代码实践（运行 + 观察型）

1. **实践目标**：亲手感受磁性对齐，并验证「阈值 = `tile_grid_size`」。
2. **操作步骤**：
   - 运行示例：`cargo run -p gpui-component-story --example tiles`（这是 `story` 包自动发现的示例）。
   - 等窗口打开后，用鼠标按住任一面板的**顶部标题条**（drag bar，高 30px）拖动它，缓慢靠近另一块面板的左缘或上缘。
3. **需要观察的现象**：当两块面板的边距离约 8px 以内时，被拖面板会「啪」地吸过去对齐；拖向画布左上角时也会吸附到 `x=0`/`y=0`。拖动期间该面板始终盖在其他面板之上。
4. **预期结果**：你能复现「左缘贴左缘」「右缘贴他板左缘」等对齐效果；松手后面板落点会对齐到 8 的整数倍网格。
5. **改参数观察**：在示例运行后无法实时改主题，可阅读 [crates/ui/src/theme/mod.rs:229](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/theme/mod.rs#L229) 的默认值 `tile_grid_size: px(8.)`，理解阈值来源；若要改变吸附手感，调大此值会让吸附「更粘」、调小则更跟手。具体数值效果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`calculate_magnetic_snap` 为什么要在开头用 `search_bounds`（被拖面板外扩阈值）过滤候选面板？

**参考答案**：为了把吸附判定的复杂度从「全部面板」降到「附近面板」。吸附阈值只有几像素，远处的面板根本不可能成为吸附目标，先用矩形相交（AABB）剔除它们，避免对每块面板都算四条边的距离（[tiles.rs:295-313](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L295-L313)）。这在面板很多时是关键的性能优化。

**练习 2**：为什么 `update_position` 里是「先吸附、后边界约束」，而不是反过来？

**参考答案**：吸附算出的坐标可能把面板推出可视边界（例如吸附到一块已出屏面板的边）。若先做边界约束再吸附，吸附会覆盖掉约束效果；而先吸附再约束，边界约束作为最后一道闸，能保证「吸附之后仍不出屏」（[tiles.rs:398-410](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L398-L410)）。语义上是「尽量吸附，但不能违反可视性」。

---

### 4.3 面板边缘吸附调整尺寸

#### 4.3.1 概念说明

拖动改变的是面板「整体位置」，缩放改变的是面板「尺寸」。`Tiles` 给每块面板配了 5 个缩放手柄（resize handle）：左、右、上、下四条边，加上右下角的 `ResizeCorner` 角柄。拖手柄时，被拖的是「面板正在移动的那一条边」，对边则被钉住（pinned）。

缩放时的吸附与 4.2 不同——它不是「整体对齐」，而是「**正在移动的那条边**」去吸附邻近面板的边或画布边缘。例如你拖面板 P 的右缘向右，当 P 的右缘靠近邻居 B 的左缘时，P 的右缘会吸附到 B 的左缘，于是 P 的宽度正好延伸到 B 旁边，两块面板严丝合缝。

当附近没有合适的邻居边可吸附时，缩放会回退到「网格圆整」——把边对齐到 `tile_grid_size` 的整数倍，保证尺寸不会停在奇怪的像素值上。

#### 4.3.2 核心流程

缩放一条边的每一帧，`resize` 与 `compute_resized_bounds` 做的事：

```
resize(new_x?, new_y?, new_width?, new_height?):
  1. other_bounds = 除自己外所有面板的 bounds（候选吸附边来源）
  2. 找到正在缩放的面板 item，取 previous_bounds
  3. compute_resized_bounds(previous, new_x?, ..., other_bounds, grid_size)
  4. 几何确实变了才写回 item.bounds，并 push TileChange
  5. cx.notify()

compute_resized_bounds（X 轴为例）:
  - 若给了 new_x        => 左缘在动、右缘钉住：snapped_left = snap_edge(raw_left, 候选+画布0)
  - 否则若给了 new_width => 右缘在动、左缘钉住：snapped_right = snap_edge(raw_right, 候选)
  - snap_edge 命中 => 用吸附值；未命中 => round_to_nearest_ten_with 回退到网格
  - 宽度 = (对边 - 吸附边).max(MINIMUM_SIZE.width)
```

阈值同样来自 `tile_grid_size`。`MINIMUM_SIZE`（100×100）保证面板不会被缩到看不见。

#### 4.3.3 源码精读

吸附一条边到最近候选点的纯函数是 `snap_edge`——它和 4.2 的逻辑同构，但更通用、更易测试：

[crates/ui/src/dock/tiles.rs:1137-1148](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L1137-L1148) —— `snap_edge(edge, candidates, threshold)`：遍历候选，取「严格小于当前最佳距离」的最近候选；初始 `best_dist = threshold`，所以「距离 ≥ 阈值」时返回 `None`（不吸附）。

把 `snap_edge` 用到缩放上的核心是 `compute_resized_bounds`。它先从所有邻居面板收集 X/Y 各自的候选边，再根据「哪条边在动」决定吸附谁：

[crates/ui/src/dock/tiles.rs:1160-1228](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L1160-L1228) —— `compute_resized_bounds`。关键设计有三：

- **哪条边动由 `Option` 推断**：`new_x` 有值→左缘动（右缘钉住）；只有 `new_width` 有值→右缘动（左缘钉住）。Y 轴同理（见函数注释 [tiles.rs:1154-1159](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L1154-L1159)）。
- **画布边缘也是候选**：左缘/上缘移动时，把 `px(0.)` 也加进候选（[tiles.rs:1186-1188](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L1186-L1188) 与 [1206-1208](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L1206-L1208)），所以拖左缘靠左能吸到画布左边。
- **吸附失败回退网格**：`snap_edge(...).unwrap_or_else(|| round_to_nearest_ten_with(...))`（[tiles.rs:1188-1189](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L1188-L1189)），保证尺寸总落在整齐刻度上。

网格圆整函数：

[crates/ui/src/dock/tiles.rs:1230-1232](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L1230-L1232) —— `round_to_nearest_ten_with(value, grid_size) = (value / grid_size).round() * grid_size`，把任意像素值四舍五入到最近网格刻度。

把上述能力串起来的 `resize` 入口：

[crates/ui/src/dock/tiles.rs:433-492](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L433-L492) —— `resize` 收集 `other_bounds`（排除正在缩放的面板自身，[tiles.rs:448-453](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L448-L453)），调 `compute_resized_bounds`，仅在几何真正变化时写回并记历史。

那 5 个手柄是怎么画出来、又是怎么把鼠标位移翻译成 `resize` 调用的？看 `render_resize_handles`（它生成左、右、上、下、角五个绝对定位的 `div`，每个手柄挂 `on_drag_move<DragResizing>`）：

[crates/ui/src/dock/tiles.rs:631-947](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L631-L947) —— `render_resize_handles`。以**右缘手柄**为例（[tiles.rs:701-754](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L701-L754)）：它根据 `drag_data.last_position` 与当前鼠标位置算 `delta`，得到 `new_width = last_width + delta`（不小于 `MINIMUM_SIZE.width`），然后 `this.resize(None, None, Some(new_width), None, ...)`——注意只传 `new_width`、不传 `new_x`，于是 `compute_resized_bounds` 判定为「右缘动、左缘钉住」。

左缘手柄则相反（[tiles.rs:645-699](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L645-L699)）：它同时传 `new_x` 和 `new_width`，因为左缘移动时「原点和宽度都要改」，且由于传了 `new_x`，`compute_resized_bounds` 判定为「左缘动、右缘钉住」。角柄（[tiles.rs:867-944](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L867-L944)）则同时改宽和高。

手柄按下时记录起始状态并置顶：

[crates/ui/src/dock/tiles.rs:949-970](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L949-L970) —— `on_resize_handle_mouse_down` 记下 `ResizeDrag { side, last_position, last_bounds }`，并 `bring_to_front` 让缩放中的面板置顶。

> 单元测试佐证：`test_resize_right_edge_snaps_to_neighbor_left`（[tiles.rs:1361-1369](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L1361-L1369)）验证了「右缘拖到 197、邻居左缘在 200、阈值 8 → 吸附到 200、宽度变 200」；`test_resize_grid_rounds_when_no_neighbor_close`（[tiles.rs:1406-1411](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L1406-L1411)）验证了「无邻居时右缘 153 → 网格圆整到 152」。这两条测试是理解吸附与回退最直观的入口。

#### 4.3.4 代码实践（运行 + 阅读测试型）

1. **实践目标**：验证「缩放时的边缘吸附」与「无邻居时的网格回退」，并读懂测试如何固化这些行为。
2. **操作步骤**：
   - 运行 `cargo run -p gpui-component-story --example tiles`，把鼠标移到某块面板的**右边缘**（会出现 `ew-resize` 左右箭头光标），按住往右拖，靠近右侧邻居的左缘。
   - 再把面板拖到画布空旷处（四周无邻居），拖右缘随意停在某个非整数值。
   - 同时打开 [crates/ui/src/dock/tiles.rs:1361-1411](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L1361-L1411) 阅读 `test_resize_*` 系列测试。
3. **需要观察的现象**：右缘靠近邻居左缘时会吸附贴合（两面板无缝）；空旷处缩放时，宽度会跳到 8 的整数倍（如 152、160）。角柄可同时吸宽和高。
4. **预期结果**：运行行为与测试断言一致——`out.size.width == px(200.)`（吸附）与 `out.size.width == px(152.)`（圆整）。
5. 待本地验证：GUI 上的精确像素值需本地运行确认；测试断言可直接用 `cargo test -p gpui-component --lib dock::tiles::tests` 验证（注：按项目约定，测试非必须运行，但此命令可帮你确认断言）。

#### 4.3.5 小练习与答案

**练习 1**：拖「左缘手柄」时为什么 `resize` 要同时传 `new_x` 和 `new_width`，而拖「右缘手柄」只传 `new_width`？

**参考答案**：右缘移动时左缘（原点）不变，宽度 = 新右缘 − 原左缘，只需告诉 `compute_resized_bounds`「右缘在动」即可，故只传 `new_width`。左缘移动时左缘（原点）变了，且为了让**右缘钉住**，宽度必须同步调整（新宽度 = 原右缘 − 新左缘），所以必须同时传 `new_x`（让函数知道是左缘在动）和 `new_width`（给出新宽度）。`compute_resized_bounds` 正是用「有没有传 `new_x`」来区分这两种情形（[tiles.rs:1183-1198](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L1183-L1198)）。

**练习 2**：如果一块面板四周都没有邻居，拖它的右缘会发生什么？依据是哪段代码？

**参考答案**：会回退到网格圆整——`compute_resized_bounds` 里 `snap_edge(raw_right, &x_edges, grid_size)` 在 `x_edges` 为空或无候选足够近时返回 `None`，于是执行 `unwrap_or_else(|| round_to_nearest_ten_with(raw_right, grid_size))`（[tiles.rs:1195-1196](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L1195-L1196)），把右缘对齐到 `tile_grid_size` 的整数倍。`test_resize_grid_rounds_when_no_neighbor_close` 正是这条路径的固化。

**练习 3**：`snap_edge` 的阈值比较用的是「严格小于」(`dist < best_dist`，初值 `best_dist = threshold`)。这意味着「距离恰好等于阈值」会不会吸附？为什么这样设计？

**参考答案**：不会吸附。因为初值 `best_dist = threshold`，只有 `dist < threshold` 才会更新 `best`，距离恰为阈值时 `dist < best_dist` 不成立，返回 `None`（[tiles.rs:1137-1148](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L1137-L1148)）。这样设计让阈值成为一个清晰的「上界」——只在严格小于阈值时才认为「足够近」，避免边界条件（恰好等于阈值）产生歧义的吸附行为，也便于测试断言（如 `test_snap_edge_outside_threshold` 用 `120 - 100 = 20 ≥ 8` 验证不吸附）。

---

### 4.4 持久化、层级与撤销重做

#### 4.4.1 概念说明

自由画布带来一个副作用：用户精心摆好的布局必须能**存下来、下次恢复**，否则每次重启都回到初始状态太难用。同时，「自由」也意味着用户容易手滑把面板拖错位置，所以**撤销/重做**是刚需。

`Tiles` 用三套机制解决：

- **序列化**：`dump` 把每块面板的 `bounds`+`z_index` 收集成 `Vec<TileMeta>`，塞进 `PanelInfo::Tiles { metas }`；反序列化时 `DockItem::tiles` 用这些 `metas` 还原每块面板的初始矩形。
- **层级**：`panels` 向量的顺序就是绘制顺序（末尾在最上），`bring_to_front` 通过移动向量位置改层级；`z_index` 字段在排序时作为第一关键字。
- **撤销/重做**：一个 `History<TileChange>` 栈记录每次「几何变化」和「层级变化」，`Undo`/`Redo` action 回放这些变更。

#### 4.4.2 核心流程

```
dump(cx):
  for 每个 TileItem:
    children.push(面板自身.dump(cx))      // 面板内容序列化
    metas.push(TileMeta { bounds, z_index }) // 几何序列化
  state.info = PanelInfo::Tiles { metas }

load (反序列化, state.rs):
  PanelInfo::Tiles { metas } => DockItem::tiles(items, metas, ...) // 用 metas 还原 bounds

拖动/缩放每一帧:
  history.push(TileChange { old_bounds, new_bounds, ... })  // 记变更
  history 用 100ms group_interval 合并连续帧，避免一次拖动产生上百条历史

Undo / Redo:
  history.ignore = true  // 回放期间不再记历史
  对每条 change: 还原 old_bounds 或 old_order(undo) / new_* (redo)
  history.ignore = false
```

#### 4.4.3 源码精读

序列化出口——`Tiles::dump`：

[crates/ui/src/dock/tiles.rs:155-176](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L155-L176) —— `dump` 同时收集 `children`（各面板自身状态）和 `metas`（几何），最终 `state.info = PanelInfo::Tiles { metas }`。

`TileMeta` 与 `PanelInfo::Tiles` 的定义（可 `Serialize`/`Deserialize`）：

[crates/ui/src/dock/state.rs:75-94](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/state.rs#L75-L94) —— `TileMeta { bounds, z_index }`，默认值是 `origin(10,10)`、`size(200,200)`、`z_index 0`。

[crates/ui/src/dock/state.rs:107-108](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/state.rs#L107-L108) 与 [124-126](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/state.rs#L124-L126) —— `PanelInfo::Tiles { metas }`，序列化标签 `"tiles"`，构造捷径 `PanelInfo::tiles(metas)`。

反序列化入口——回到 `DockItem::tiles`：

[crates/ui/src/dock/state.rs:220](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/state.rs#L220) —— `PanelInfo::Tiles { metas } => DockItem::tiles(items, metas, &dock_area, window, cx)`，把存下来的 `metas` 作为每块面板的初始 `bounds` 喂回去（与 4.1 的构造器是同一个函数）。

撤销/重做的变更记录与回放。`TileChange` 同时能描述「几何变化」(`old_bounds/new_bounds`) 和「层级变化」(`old_order/new_order`)，二者互斥（用 `Option`）：

[crates/ui/src/dock/tiles.rs:31-49](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L31-L49) —— `TileChange` 实现 `HistoryItem`，带 `version` 字段。

`undo` 的回放逻辑（`redo` 对称）：

[crates/ui/src/dock/tiles.rs:565-589](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L565-L589) —— `undo` 先置 `history.ignore = true`（防止回放时又记历史），逐条还原 `old_bounds`（改几何）或 `old_order`（`remove` 后 `insert` 回原位改层级），最后恢复 `ignore` 并 `cx.notify()`。

`History` 在 `Tiles::new` 里被配了 100ms 的合并窗口：

[crates/ui/src/dock/tiles.rs:196](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L196) —— `History::new().group_interval(Duration::from_millis(100))`。一次连续拖动会触发几十帧 `update_position`，若无合并，撤销栈会被零碎位移填满；100ms 窗口把「同一拨连续操作」合并成一条历史，按一次 `Undo` 就能整体回退。

`Undo`/`Redo` 是通过 GPUI 的 `actions!` 宏声明的（[tiles.rs:25](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L25)），可在应用里绑定快捷键后触发 `undo`/`redo` 方法。

最后是层级排序与渲染。`sorted_panels` 按 `z_index` 升序、同 `z_index` 按 `panels` 原顺序：

[crates/ui/src/dock/tiles.rs:215-219](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L215-L219) —— `sorted_panels`，`sort_by(z_index then 原下标)`，决定绘制顺序（先画的在底下）。

整张画布的 `Render`：可滚动容器 + 按排序后的面板逐个 `render_panel` + 一个 `Scrollbar`；`on_prepaint` 回填 `self.bounds`，`on_drop` 把拖入的外部数据转成 `DragDrop` 事件：

[crates/ui/src/dock/tiles.rs:1254-1317](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L1254-L1317) —— `Render for Tiles`。注意 `on_mouse_up` 在这里挂到外层 `div`，统一收口拖动/缩放的「松手结算」（含网格圆整与历史记录，[tiles.rs:1065-1132](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L1065-L1132)）。

而 `DragDrop` 事件会被 `DockArea` 订阅并转发为 `DockEvent::DragDrop`，供上层（如示例里的 `println!("drag drop: ...")`）响应：

[crates/ui/src/dock/mod.rs:563-574](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L563-L574) —— `subscribe_tiles_item_drop` 把 `Tiles` 的 `DragDrop` 转成 `DockEvent::DragDrop`。

> 补充：`TabPanel` 在 Tiles 模式下还有「空了自毁」行为——当一块面板的所有 tab 被关空，它会通知 `DockArea` 把自己从画布移除（[tab_panel.rs:1163-1171](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tab_panel.rs#L1163-L1171)）。这正是 4.1 强调「只允许加 TabPanel」的原因——关闭/拖出的能力都封装在那里。

#### 4.4.4 代码实践（运行 + 改参数型）

1. **实践目标**：验证布局持久化与撤销合并，并理解 100ms 合并窗口的作用。
2. **操作步骤**：
   - 运行 `cargo run -p gpui-component-story --example tiles`，把几块面板拖到新位置、缩放几条边。
   - 关闭窗口（示例会在退出前把布局写到 `target/tiles.json`，见 [tiles.rs 示例:239-244](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/examples/tiles.rs#L239-L244)）。再次运行，确认面板回到了你离开时的位置——这就是 `dump`→`load` 经 `TileMeta`/`PanelInfo::Tiles` 的往返。
   - 打开生成的 `target/tiles.json`，找到 `"tiles"` 节点，观察每块面板存了 `bounds` 和 `z_index`。
3. **需要观察的现象**：重启后面板位置/尺寸/层级恢复；JSON 里 `"info": { "tiles": { "metas": [ { "bounds": {...}, "z_index": 0 }, ... ] } }`。
4. **预期结果**：自由摆放的布局被完整持久化，证明 `TileMeta` 捕获了「坐标驱动」布局的全部状态。
5. **改参数观察**：把 [crates/ui/src/dock/tiles.rs:196](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L196) 的 `group_interval` 从 100ms 调大到 1000ms 重新运行，连续拖动一段距离后触发一次 `Undo`，观察「一次撤销回退的范围」变大。具体手感待本地验证。

> 注：示例仅用于演示，**不要把对 `tiles.rs`（库源码）或示例的修改提交**。本实践的所有「改参数」均为本地学习用途。

#### 4.4.5 小练习与答案

**练习 1**：`TileChange` 为什么把 `old_bounds/new_bounds` 和 `old_order/new_order` 都设计成 `Option`？

**参考答案**：因为一次变更可能只是「纯几何变化」（拖动/缩放，`order` 不变）或「纯层级变化」（`bring_to_front`，`bounds` 不变），少数情况两者都有。用 `Option` 让一条 `TileChange` 能描述这几种情形而不浪费字段：几何变化时 `old_order/new_order = None`，`undo` 里 `if let Some(old_bounds)` 才改几何、`if let Some(old_order)` 才改层级（[tiles.rs:575-582](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L575-L582)）。

**练习 2**：`undo`/`redo` 为什么要先设 `history.ignore = true`，最后再设回 `false`？

**参考答案**：回放历史时，对 `panels` 的修改（改 `bounds`、移动顺序）会再次触发 `cx.notify()` 和后续逻辑，若不加保护，这些「回放动作」又会被当成新操作压进历史栈，导致撤销栈被污染、`Undo` 行为错乱（撤销一次反而多出几条历史）。`ignore = true` 让 `resize`/`update_position`/`bring_to_front` 里的 `if !self.history.ignore { push }` 跳过入栈（如 [tiles.rs:419](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L419) 与 [479](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L479)），保证回放干净。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「自由面板工作台」探索任务：

1. **运行示例**：`cargo run -p gpui-component-story --example tiles`，窗口里会出现 4 块用 `DockItem::tiles` 摆成 4 列的面板（见示例 `init_default_layout`，[tiles.rs 示例:311-352](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/examples/tiles.rs#L311-L352)，每块 380×280、间距 20、起点 (20,20)）。
2. **拖动 + 磁性对齐（4.2）**：拖住一块面板的标题条，慢慢靠近另一块的左缘/上缘，记录吸附发生的近似距离（应约等于 `tile_grid_size` 默认 8px）；把面板拖到画布左上角，确认吸附到 `x=0`/`y=0`。
3. **缩放 + 边缘吸附（4.3）**：拖某块面板的**右缘**向右靠近邻居左缘，确认两块无缝贴合；再把面板拖到空旷处缩放，确认宽度跳到 8 的整数倍（网格回退）；最后用右下角角柄同时改宽和高。
4. **层级（4.4）**：把两块面板拖到重叠位置，反复点击/拖动其中一块，观察它如何被 `bring_to_front` 置顶。
5. **持久化（4.4）**：摆好布局后关闭窗口，查看 `target/tiles.json` 里的 `"tiles"` 节点；再次运行示例，确认布局恢复。
6. **撤销（4.4）**：若你为本示例绑定了 `tiles::Undo` 快捷键（[tiles.rs:25](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L25) 声明的 action），连续拖动后按一次撤销，观察「一次撤销回退整段连续拖动」（100ms 合并窗口的效果）。未绑定快捷键时，可在源码里阅读 `undo`/`redo`（[tiles.rs:565-616](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/tiles.rs#L565-L616)）理解其行为。

**交付物**：一张记录表，列出你在第 2~4 步观察到的吸附目标（邻居边/画布边缘/网格）、近似阈值，以及第 5 步 JSON 里某块面板的 `bounds` 与 `z_index`。无法在本地确认的数值，标注「待本地验证」。

## 6. 本讲小结

- `Tiles` 是 Dock 布局树里的「自由画布」叶子节点，与 `Split` 的「关系驱动分屏树」相对——每个面板自带绝对 `Bounds<Pixels>` 坐标，可重叠、可错位，通常作为 `DockArea` 中心区。
- 画布只接受 `TabPanel` 类型的面板（`add_item` 会 downcast 校验），因为关闭/拖出/空自毁等能力都封装在 `TabPanel` 里，由 `set_in_tiles(true)` 标记启用。
- **拖动时的磁性对齐**（`calculate_magnetic_snap`）把面板的四条边对齐到「画布边缘」或「其他面板的边」，阈值即 `tile_grid_size`（默认 8px），X/Y 两轴独立、先吸附后做边界约束。
- **缩放时的边缘吸附**（`resize` + `compute_resized_bounds` + `snap_edge`）让「正在移动的那条边」吸附邻居边或画布边缘，无候选时回退到网格圆整；哪条边动由传入的 `Option` 推断，对边钉住。
- 层级靠 `panels` 向量顺序 + `z_index` 双关键字排序，`bring_to_front` 在拖动/缩放/点击时把面板置顶。
- 布局经 `TileMeta`(`bounds`+`z_index`) 与 `PanelInfo::Tiles` 序列化往返；撤销/重做用带 100ms 合并窗口的 `History<TileChange>`，回放时用 `ignore` 标志防止历史污染。

## 7. 下一步学习建议

- **横向对比 Split 模式的调整**：回到 u6-l1/u6-l2，对比 `StackPanel` 的 `ResizablePanelGroup`（按比例调宽）与本讲的像素级吸附，体会「关系驱动」与「坐标驱动」两套布局在尺寸调整上的根本差异。
- **阅读 `History` 模块**：本讲只用到了 `History::new().group_interval(...)`、`push`、`undo`/`redo`、`ignore`。建议读 `crates/ui/src/history/` 了解合并窗口的内部实现，它是 gpui-component 通用的撤销栈基础设施，也用在别处。
- **下一单元 u7（高性能数据展示）**：Tiles 常用来承载 `List`/`Table`/`Tree` 这类大数据面板。学完 u7 后，你可以尝试把一个虚拟化 `Table` 面板放进 Tiles 画布，观察自由布局与高性能渲染的组合效果。
- **动手扩展（可选）**：在理解吸附算法后，可尝试在本地分支上为 `calculate_magnetic_snap` 增加一条「吸附到面板中心线」的候选，验证你对候选点构造（参考 4.2.3 的四候选写法）的理解——但请勿提交对库源码的修改。
