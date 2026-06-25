# DockArea 与 DockItem 布局树

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `DockArea` 的「中心区 + 左/下/右三个 dock」总体架构，以及它如何用 flex 布局把它们拼成一个完整窗口。
- 看懂 `DockItem` 这个枚举如何用 `Split / Tabs / Panel / Tiles` 四种节点递归地表达任意复杂的面板布局树。
- 理解 `DockEvent`（`LayoutChanged` / `DragDrop`）这套事件在「布局变化 → 通知外部保存」链路中的作用。
- 能够运行 Dock 示例，并能动手调整一棵 `DockItem` 树、观察面板排布的变化。

这是「Dock 布局系统」单元（u6）的第一讲，只聚焦在**容器与布局树**本身；如何定义一个自定义 `Panel`、如何拖拽、如何序列化保存，是后续 u6-l2 的主题。

## 2. 前置知识

本讲假定你已经掌握下面这些（它们在之前的讲义里建立过）：

- **应用入口骨架**（u1-l4）：`application().run` → `gpui_component::init(cx)` → `cx.open_window` → 用 `Root` 包裹第一个视图。`dock::init(cx)` 就是在 `init` 里被调用的，所以**用 Dock 之前必须先 init**。
- **状态外置 + 无状态外壳**范式（u2/u4）：组件本身常常是 `RenderOnce` 无状态的，真正的跨帧状态放在外层 `View`（一个 `Entity`）里。
- **Tab / Sidebar 等导航组件**（u5-l5）：你已经知道「选中索引外置」「状态实体 + 无状态外壳」等约定，本讲的 `TabPanel`/`StackPanel` 仍然沿用它们。
- **GPUI 的 Entity 与事件订阅**：`cx.new`、`cx.subscribe`、`EventEmitter<T>`、`cx.emit(evt)` 这一整套实体间通信机制。

如果你对「面板（Panel）」「标签页（Tab）」这类 IDE 里常见的布局没有概念，可以想象一下 VS Code 的界面：左边是侧边栏、中间是编辑器、下面是终端面板、右边可能有预览——这种「固定停靠 + 中心可分屏」的结构，正是 `DockArea` 要表达的。

## 3. 本讲源码地图

本讲涉及的关键文件都集中在 `crates/ui/src/dock/` 目录下：

| 文件 | 作用 |
| --- | --- |
| [dock/mod.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs) | Dock 系统的「总枢纽」。定义 `DockArea`（总容器）、`DockItem`（布局树枚举）、`DockEvent`（事件），以及 `DockArea` 的全部增删改、序列化、订阅逻辑。 |
| [dock/dock.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/dock.rs) | 定义「停靠位」`Dock` 与 `DockPlacement`（Left/Bottom/Right/Center）。`Dock` 是左/下/右三向的固定容器，可开合、可拖拽调宽。 |
| [dock/state.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/state.rs) | 序列化与反序列化：`DockAreaState` / `DockState` / `PanelState` / `PanelInfo`。把一棵运行时 `DockItem` 树存成 JSON，再还原回来。 |
| [dock/panel.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/panel.rs) | `Panel` / `PanelView` trait 的定义（自定义面板的契约）与全局 `PanelRegistry`（按名字反序列化面板）。本讲只用到它的概念，详细实现见 u6-l2。 |
| [dock/stack_panel.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/stack_panel.rs) | `StackPanel`：`Split` 节点背后的可调宽分屏容器。 |
| [crates/story/examples/dock.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/examples/dock.rs) | 官方的 Dock 综合示例，本讲代码实践就基于它。 |

## 4. 核心概念与源码讲解

### 4.1 DockArea：中心区 + 三向 dock 的总管架构

#### 4.1.1 概念说明

`DockArea` 是整个 Dock 系统的**顶层容器**，也是一个 `Render` 视图（`Entity<DockArea>`）。你可以把它理解成一张「画布」：这张画布被切成五个区域——

```
┌──────────────────────────────────────────────┐
│              │                    │           │
│   Left Dock  │      Center        │ Right Dock│
│  (可选,可开合) │  (主区,可任意分屏)    │ (可选,可开合)│
│              │                    │           │
├──────────────┴────────────────────┴───────────┤
│                Bottom Dock (可选,可开合)         │
└──────────────────────────────────────────────┘
```

- **中心区（center）**是必有的，它本身是一棵 `DockItem` 布局树，可以随意 `Split`/`Tabs`/`Tiles`，是用户真正干活的主舞台。
- **左/下/右三个 dock** 是可选的，每个都是一个固定停靠位 `Dock`，特点是「**位置固定、不能拆分、只能整体开合与调宽**」（这点和中心区的 `Panel` 不同，中心区面板能被拖来拖去）。

> 术语澄清：`DockArea`（总容器）≠ `Dock`（左/下/右停靠位）≠ `DockItem`（布局树节点）。三者名字相近但职责完全不同，是初学最容易混淆的地方。

#### 4.1.2 核心流程

`DockArea` 的 `render` 用一个 `flex_row`（横向排列）把三者拼起来，结构如下（伪代码）：

```
div (size_full, relative)
├─ 当 zoom_view 存在时：只渲染 zoom_view（全屏放大某个面板）
└─ 否则：
     div (flex, flex_row, h_full)
     ├─ left_dock       (flex_none, 有则渲染)
     ├─ div (flex_1, flex_col)        ← 中心列
     │   ├─ 上：render_items(center)   (flex_1)
     │   └─ 下：bottom_dock            (有则渲染)
     └─ right_dock      (flex_none, 有则渲染)
```

关键点：

1. **左/右 dock 是 `flex_none`**，即不参与弹性伸缩，宽度由各自的 `size` 字段决定；**中心是 `flex_1`**，吃掉剩余空间。这就是「侧边固定、主区自适应」的由来。
2. **底部 dock 被放进中心列内部**（`flex_col` 的第二个子元素），所以它只会占据中心列的下方，而不会延伸到左右 dock 下方。
3. 顶层有一个 `on_prepaint` 钩子，把 DockArea 实际占据的像素矩形 `bounds` 记下来——这是后续拖拽调宽时换算鼠标坐标的基础。
4. `zoom_view` 一旦被设置，整张画布只渲染那一个被放大的视图，其余全部隐藏。

宽度上还有一条硬约束：任何 dock 的尺寸都不能小于最小值 `PANEL_MIN_SIZE`。其值定义在可调整面板模块里：

```rust
// crates/ui/src/resizable/mod.rs:12
pub(crate) const PANEL_MIN_SIZE: Pixels = px(100.);
```

也就是任何停靠区至少 100 像素。当中心区宽度为 \(W\)、左 dock 宽 \(L\)、右 dock 宽 \(R\) 时，中心区可用宽度为

\[
W_{\text{center}} = W - L - R,\qquad L,R \geq 100\text{px}
\]

#### 4.1.3 源码精读

先看 `DockArea` 的结构体定义，五个区域的字段一目了然：

[crates/ui/src/dock/mod.rs:44-74](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L44-L74) —— `DockArea` 持有 `center: DockItem` 和三个 `Option<Entity<Dock>>`，外加 `zoom_view`、`locked`、`panel_style` 等配置；`bounds` 用于拖拽换算，`_subscriptions` 收纳所有面板事件订阅。

构造函数 `DockArea::new` 会默认建一棵**空的横向 Split** 当中心区：

[crates/ui/src/dock/mod.rs:520-555](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L520-L555) —— 初始 `center` 是一个 `items`/`sizes` 都为空的 `DockItem::Split { axis: Horizontal, view: StackPanel }`，并立刻 `subscribe_panel` 订阅它的事件。也就是说「**新建的 DockArea 默认就有一个空中心区，等用户用 `set_center` 填充**」。

设置左/右/底部 dock 的三姊妹方法逻辑几乎一致，以左 dock 为例：

[crates/ui/src/dock/mod.rs:633-653](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L633-L653) —— `set_left_dock(panel, size, open, ...)` 先 `subscribe_item` 订阅传入的布局树事件，再 `cx.new` 一个 `Dock::left`，按需 `set_size`/`set_panel`/`set_open`，最后刷新「切换按钮」所属的 TabPanel。注意 `size` 与 `open` 都是**显式传入**的——这印证了「停靠位的尺寸/开合状态外置、由调用方决定」的设计。

最后看 `render` 如何把五区拼起来：

[crates/ui/src/dock/mod.rs:1116-1175](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L1116-L1175) —— 这段就是 4.1.2 伪代码的真实实现。留意三处细节：(1) `on_prepaint` 回写 `bounds`；(2) `zoom_view` 分支独占整屏；(3) 当 center 是 `DockItem::Tiles` 时走完全不同的渲染分支（直接渲染 tiles，因为 Tiles 是绝对定位自由布局，不参与 flex 流），其余走「left | (center+bottom) | right」的 flex 三段式。中心区本体由 `render_items` 根据 `DockItem` 变体取出对应 `view` 渲染：

[crates/ui/src/dock/mod.rs:1085-1092](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L1085-L1092) —— 四种 `DockItem` 各取自己的 `view`（`StackPanel`/`TabPanel`/`Tiles` 或 `Panel` 的 `view()`）转成 `AnyElement`。

#### 4.1.4 代码实践

**目标**：亲眼看到「中心区 + 三向 dock」的实际排布。

**步骤**：

1. 运行官方 Dock 示例（它就是一个完整 `DockArea`）：

   ```bash
   cargo run -p gpui-component-story --example dock
   ```

2. 观察窗口：你应该看到左侧 dock（含 List、Scrollbar/Accordion 等面板）、右侧 dock、底部 dock，以及中间一排可切换的标签页（Button/Input/Select…）。

3. 点击窗口底部状态栏的三个图标按钮（`PanelLeft`/`PanelBottom`/`PanelRight`），分别开合左/下/右 dock，观察中心区如何随之扩展/收缩——这正是 `set_open` 改变 `flex_none` 容器显隐、`flex_1` 中心区自动吃掉释放空间的效果。

**需要观察的现象**：关闭左 dock 后中心区向左扩展；重新打开后中心区被挤压回原宽度；底部 dock 只占中心列下方、不会跑到左右 dock 之下。

**预期结果**：界面布局与 4.1.1 的示意图一致。若运行失败（缺依赖等），记为「待本地验证」。

> 这三个开合按钮对应的源码就在示例里：[crates/story/examples/dock.rs:521-549](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/examples/dock.rs#L521-L549) 调用了 `area.toggle_dock(DockPlacement::Left/Bottom/Right, ...)`。

#### 4.1.5 小练习与答案

**练习 1**：为什么底部 dock 不会出现在左/右 dock 的下方？
**答案**：因为 `render` 把底部 dock 放进了**中心列**（一个 `flex_col` 容器，由中心区 + 底部 dock 组成）内部，而中心列本身位于「left | center-column | right」这个 `flex_row` 的中间。底部 dock 只能跟随中心列，无法越过左右 dock。

**练习 2**：`set_left_dock` 的三个参数 `panel / size / open` 为什么不由 `DockArea` 自己内部决定？
**答案**：遵循本库「状态外置」约定——停靠位的初始尺寸和开合属于「业务状态」，应由调用方（外层 View）显式给出，组件本身只负责把这些值渲染出来并响应交互。

---

### 4.2 DockItem：Split/Tabs/Panel/Tiles 四种布局节点构成的树

#### 4.2.1 概念说明

光有一个 `DockArea` 画布还不够，中心区（以及每个停靠位）里到底放什么、怎么排，由一棵 **`DockItem` 布局树**来描述。`DockItem` 是一个枚举，每个变体代表一种布局节点：

| 变体 | 背后实体 | 含义 | 是否可递归 |
| --- | --- | --- | --- |
| `Split` | `Entity<StackPanel>` | 沿某轴（水平/垂直）把多个子项**分屏**排列，带可拖拽的分隔条 | ✅ `items: Vec<DockItem>` |
| `Tabs` | `Entity<TabPanel>` | 把多个面板做成**标签页**，同一时刻只显示一个 | ❌ 叶子（装的是 `PanelView`） |
| `Panel` | `Arc<dyn PanelView>` | 单个面板，**最简叶子节点** | ❌ 叶子 |
| `Tiles` | `Entity<Tiles>` | **自由拼贴**布局，面板可任意拖动定位、边缘吸附 | ⚠️ 装的是 `TileItem` |

关键理解：**`Split` 是唯一的「可递归」节点**。一棵典型的中心区布局树长这样：

```
Split(Horizontal)                    ← 左右分屏
├─ Tabs [ButtonStory, InputStory]    ← 左：一组标签页
└─ Split(Vertical)                   ← 右：上下分屏
   ├─ Tabs [ImageStory]
   └─ Tabs [IconStory]
```

也就是说，任何复杂的 IDE 式布局，都可以用 `Split`（嵌套分屏）+ `Tabs`（标签页）这两类节点组合出来；`Panel` 是 `Tabs` 退化为单项的特例；`Tiles` 则是另一种「不参与 flex 流、自由定位」的布局，本讲不展开（见 u6-l3）。

#### 4.2.2 核心流程

构造一棵 `DockItem` 树有一组「构造器」方法，它们都接收 `&WeakEntity<DockArea>`、`window`、`cx`——因为构造节点时需要建立实体并**向 DockArea 订阅事件**：

```
DockItem::tabs(panels, &dock_area, window, cx)      → Tabs 节点
DockItem::tab(single_panel, ...)                    → Tabs 退化为单标签
DockItem::panel(single_panel)                       → Panel 叶子（无需 dock_area）
DockItem::split(axis, items, ...)                   → Split 节点（自动收集子项尺寸）
DockItem::v_split(items, ...)  / h_split(...)       → split 的垂直/水平快捷写法
DockItem::split_with_sizes(axis, items, sizes, ...)→ 带显式尺寸的 Split
DockItem::tiles(items, metas, ...)                  → Tiles 节点
```

以 `split_with_sizes` 为例，它的执行流程：

1. 用 `cx.new` 创建一个 `StackPanel`（带 `axis`）；
2. 遍历每个子 `DockItem`，取出它的 `view()`（`Arc<dyn PanelView>`），连同可选尺寸一起 `stack_panel.add_panel(...)` 加进去；
3. 用 `window.defer` 在当前帧结束后，回调 `DockArea` 去订阅这个 `StackPanel` 的事件（避免在构造中立即触发更新）；
4. 返回 `DockItem::Split { axis, items, sizes, view: stack_panel }`。

> 为什么 `add_panel` 在 `split_with_sizes` 里被调用了**两遍**？这是源码里一个值得注意的实现细节（见下方源码精读），它和 `StackPanel` 内部把面板「登记到列表」与「登记到可调整状态机」两步有关，属于实现细节而非公开约定。

无论节点是哪种，都有一个统一的「取视图」出口 `DockItem::view()`，返回 `Arc<dyn PanelView>`，这样上层容器（`DockArea`、`StackPanel`）就能用统一类型操作任意节点。

#### 4.2.3 源码精读

先看枚举定义，注意每个变体都带一个 `size: Option<Pixels>`（仅用于构建 Split 时指定本项尺寸）和对应的 `view`：

[crates/ui/src/dock/mod.rs:77-110](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L77-L110) —— `Split` 持有 `items: Vec<DockItem>`（递归！）、`sizes`、`view: Entity<StackPanel>`；`Tabs` 持有 `items: Vec<Arc<dyn PanelView>>`、`active_ix`、`view: Entity<TabPanel>`；`Panel` 只有一个 `view: Arc<dyn PanelView>`；`Tiles` 持有 `items: Vec<TileItem>`、`view: Entity<Tiles>`。

`split_with_sizes` 是理解「树如何被构建 + 事件如何被订阅」的核心：

[crates/ui/src/dock/mod.rs:216-259](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L216-L259) —— 注意两点：(1) 子项尺寸由 `item.get_size()` 收集（即每个子 `DockItem` 自带的 `size` 字段，可用 `.size(px)` 链式设置）；(2) 用 `window.defer` 把「向 DockArea 订阅」推迟到帧外，避免构造过程中回调造成借用冲突。

`tabs` 的构造则相对简单，把一组 `PanelView` 塞进一个新建的 `TabPanel`：

[crates/ui/src/dock/mod.rs:348-371](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L348-L371) —— `new_tabs` 创建 `TabPanel`、逐个 `add_panel`，并记录 `active_ix`。

统一的取视图出口：

[crates/ui/src/dock/mod.rs:374-381](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L374-L381) —— `view()` 对 `Split/Tabs/Tiles` 返回 `Arc::new(view.clone())`（把 `Entity` 包成 `PanelView`），对 `Panel` 直接返回内部 `view`。

运行时给布局树动态「加面板」的逻辑也值得一读，它体现了树的自适应性：

[crates/ui/src/dock/mod.rs:402-452](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L402-L452) —— `add_panel` 对 `Tabs` 直接追加；对 `Split` 则「找到第一个 Tabs 子项塞进去」，找不到就**新建一个 Tabs 节点挂到 Split 末尾**；对 `Tiles` 新建一个 `TabPanel` 包住面板再做成 `TileItem`。这正是「往 dock 拖一个新面板」时的内部行为。

而停靠位 `Dock` 本身是怎么渲染的、怎么拖拽调宽的？看 `Dock::render`：

[crates/ui/src/dock/dock.rs:384-416](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/dock.rs#L384-L416) —— 关闭且非底部时直接返回空 `div()`（不占布局）；否则按 `placement` 设宽/高，把内部 `panel` 的 `view` 当子元素，再追加一个 `resize_handle`（拖拽手柄）和一个用于捕获鼠标事件的 `DockElement`。注意注释明确写了「Dock 不支持渲染 Tiles」（第 408-409 行）——所以 `Dock` 内部只能是 `Split`/`Tabs`/`Panel`。

`DockPlacement` 枚举则把「左/下/右/中」具象化，并带 serde 重命名（用于序列化 JSON 的 key）：

[crates/ui/src/dock/dock.rs:28-60](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/dock.rs#L28-L60) —— `axis()` 方法说明：左/右 dock 的调宽轴是水平的（拖左右），底部 dock 是垂直的（拖上下），`Center` 不可达。

拖拽调宽时的尺寸夹取（clamp）逻辑，把 4.1.2 的最小尺寸约束落到实处：

[crates/ui/src/dock/dock.rs:309-377](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/dock.rs#L309-L377) —— `resize` 根据鼠标位置和 `placement` 反算新尺寸，并扣除**另一个方向上已打开 dock 的占用**（例如调左 dock 时要预留右 dock 的宽），最后 `clamp(PANEL_MIN_SIZE, max_size)`。这段也解释了「为什么三个 dock 不会互相挤爆」。

#### 4.2.4 代码实践

**目标**：动手改一棵 `DockItem` 树，理解 Split/Tabs 的组合规则。

**步骤**：

1. 打开 [crates/story/examples/dock.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/examples/dock.rs)，定位到 `reset_default_layout`（第 262-333 行）。这是示例「恢复默认布局」的入口，里面构造了 `left_panels` / `bottom_panels` / `right_panels` 三棵 `DockItem` 子树。

2. 阅读 `init_default_layout`（第 335-371 行）：它返回的是 `DockItem::v_split(vec![ DockItem::tabs([... 20 个 Story ...]) ])`——即「垂直 Split 包一层，里面是一组 Tabs」。注意 `v_split` 即使只包一个子项也能用。

3. **动手实验**：把 `reset_default_layout` 里 `right_panels` 的结构从「上下两个 tab」改成「一组 tabs」，即把

   ```rust
   let right_panels = DockItem::v_split(vec![
       DockItem::tab(StoryContainer::panel::<ImageStory>(window, cx), &dock_area, window, cx),
       DockItem::tab(StoryContainer::panel::<IconStory>(window, cx), &dock_area, window, cx),
   ], &dock_area, window, cx);
   ```

   改成

   ```rust
   let right_panels = DockItem::tabs(vec![
       Arc::new(StoryContainer::panel::<ImageStory>(window, cx)),
       Arc::new(StoryContainer::panel::<IconStory>(window, cx)),
   ], &dock_area, window, cx);
   ```

   （以上为示例修改，非项目原有代码。）

4. 先删除缓存文件 `target/docks.json`（示例会把布局序列化到这个文件，不删会用旧布局覆盖你的改动），再 `cargo run -p gpui-component-story --example dock`。

**需要观察的现象**：修改前，右侧 dock 是「上下分屏两个独立 tab 条」；修改后，右侧 dock 变成「一个 tab 条上两个标签页可切换」。这正对应 `Split` 与 `Tabs` 两种节点的视觉差异。

**预期结果**：右侧 dock 从分屏变为单 tab 条。若改动后编译/运行异常，记为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：要表达「左边一组标签页，右边再上下分两块」，应该用怎样的 `DockItem` 树？
**答案**：`DockItem::h_split(vec![ 左边: Tabs, 右边: DockItem::v_split(vec![上 Tabs, 下 Tabs]) ])`。即外层水平 `Split`，右子项再嵌一层垂直 `Split`。

**练习 2**：`DockItem::Panel` 和 `DockItem::Tabs`（只放一个面板时）有什么区别？
**答案**：`Panel` 是不带任何标题栏/tab 条的纯叶子，直接渲染面板的 `view()`；`Tabs`（哪怕只有一个面板）会经过 `TabPanel`，带标题栏与（必要时）tab 条。在停靠位里通常用 `Tabs`，因为 `Dock` 的渲染分支对 `Panel` 会套一层 `cached` 样式，而 `Tabs`/`Split` 直接渲染实体。

---

### 4.3 DockEvent：布局变化事件、Zoom 与序列化钩子

#### 4.3.1 概念说明

`DockArea` 实现了 `EventEmitter<DockEvent>`，会向**外部订阅者**（通常是你的应用主视图）广播两类事件：

```rust
pub enum DockEvent {
    /// 布局发生了变化，订阅它来保存布局（可能过于频繁，建议做防抖）
    LayoutChanged,
    /// 拖拽落点事件
    DragDrop(AnyDrag),
}
```

- **`LayoutChanged`** 是最常用的：每当面板被拖动重排、分隔条被拖动调宽、面板被关闭/新增，`DockArea` 都会发一个 `LayoutChanged`。外部订阅后，可以在回调里调用 `dock_area.dump(cx)` 把当前布局序列化成 `DockAreaState`，再写盘——这样下次启动就能 `load` 恢复。
- **`DragDrop(AnyDrag)`** 主要用于 Tiles 的「拖一个面板扔到画布」场景。

需要区分两个层次的事件：
- **`PanelEvent`**（在 `dock/panel.rs` 定义：`ZoomIn` / `ZoomOut` / `LayoutChanged`）是**底层面板**发出的。
- **`DockEvent`**（在 `dock/mod.rs` 定义）是 `DockArea` **对外**发出的。

`DockArea` 充当「翻译/中继」：它订阅底层面板/分屏的 `PanelEvent`，把其中的 `LayoutChanged` 翻译成对外的 `DockEvent::LayoutChanged`，把 `ZoomIn/ZoomOut` 翻译成「放大/还原某个面板」(`set_zoomed_in` / `set_zoomed_out`，即 4.1.2 里 `zoom_view` 的来源)。

#### 4.3.2 核心流程

事件在系统里的流动路径：

```
StackPanel 调宽分隔条 ─┐
TabPanel 关闭/拖动标签 ─┼─► emit PanelEvent::LayoutChanged
Dock 开合/调宽         ─┘
        │ DockArea 订阅 (subscribe_panel / subscribe_item)
        ▼
   DockArea 收到 PanelEvent::LayoutChanged
        │
        ├─► 更新 toggle 按钮归属 (update_toggle_button_tab_panels)
        └─► emit DockEvent::LayoutChanged  ──► 外部应用订阅
                                                    │
                                                    ▼
                                        外部: dock_area.dump(cx) 存盘
```

序列化/反序列化走的是另一条对称的链路：
- **存盘** `dump`：从 `center.view().dump(cx)` 递归收集每个面板的 `PanelState`，加上三个 `DockState`（含 placement/size/open），组成 `DockAreaState`。
- **恢复** `load`：把 `DockAreaState` 拆开，三个 `DockState::to_dock` 还原成 `Dock`，`center.to_item` 把 `PanelState` 树**递归重建**成一棵 `DockItem` 树。

`PanelState` 树与 `DockItem` 树是一一对应的，靠 `PanelInfo` 这个枚举区分节点类型（`stack`/`tabs`/`panel`/`tiles`）。

#### 4.3.3 源码精读

`DockEvent` 的定义与注释（注释提示了「可能过于频繁，建议防抖」）：

[crates/ui/src/dock/mod.rs:32-41](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L32-L41)。

`DockArea` 对外发事件的能力来自这个 impl：

[crates/ui/src/dock/mod.rs:1115](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L1115) —— `impl EventEmitter<DockEvent> for DockArea {}`。

「中继翻译」的核心在 `subscribe_panel`，它把底层 `PanelEvent` 映射成 `DockArea` 的动作或对外事件：

[crates/ui/src/dock/mod.rs:1022-1063](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L1022-L1063) —— `ZoomIn` → `set_zoomed_in`（设置 `zoom_view`）、`ZoomOut` → `set_zoomed_out`、`LayoutChanged` → 刷新 toggle 按钮并 `cx.emit(DockEvent::LayoutChanged)`。注意三个分支都用了 `cx.spawn_in(window, async move ...)` 把更新放到异步任务里执行，避免在事件回调中同步重入更新。

对 `Split` 节点的递归订阅则在 `subscribe_item`：

[crates/ui/src/dock/mod.rs:985-1019](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L985-L1019) —— 对 `Split` 先递归订阅每个子项，再订阅 `StackPanel` 本身的 `PanelEvent::LayoutChanged`（转成 `DockEvent::LayoutChanged`）；`Tabs` 的订阅放在 `StackPanel::insert_panel` 里完成（注释第 1010 行）；`Tiles` 放在 `Tiles::add_item` 里。

序列化的数据结构 `DockAreaState` / `DockState`：

[crates/ui/src/dock/state.rs:8-31](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/state.rs#L8-L31) —— `DockAreaState` 含 `version`（布局版本号，用于「结构大改后让旧存档失效」）、`center: PanelState`、三个 `Option<DockState>`；`DockState` 含 `placement`/`size`/`open`/`panel`。

节点类型标记 `PanelInfo`（序列化 JSON 里就是 `stack`/`tabs`/`panel`/`tiles` 这些 key）：

[crates/ui/src/dock/state.rs:96-109](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/state.rs#L96-L109) —— `Stack{sizes,axis}`（axis 0/1 表示水平/垂直）、`Tabs{active_index}`、`Panel(json)`（面板自定义数据）、`Tiles{metas}`。

恢复时的树重建 `PanelState::to_item`：

[crates/ui/src/dock/state.rs:171-222](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/state.rs#L171-L222) —— 根据 `info` 分支：`Stack` → `DockItem::split_with_sizes`、`Tabs` → `DockItem::tabs`（并对单子项做退化）、`Panel` → 通过全局 `PanelRegistry::build_panel` **按名字反序列化**出面板实体（找不到则给一个 `InvalidPanel`）、`Tiles` → `DockItem::tiles`。

> 这里的 `PanelRegistry::build_panel` 揭示了反序列化的关键：JSON 里只存了面板的**名字**（`panel_name`）和自定义 `info`，恢复时必须靠全局注册表「按名字找到构造函数」才能重建实体。这正是为什么 u6-l2 要讲 `PanelView` 与 `register_panel`——**不注册的名字无法被恢复**。

`DockArea` 的存/取入口：

[crates/ui/src/dock/mod.rs:928-981](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L928-L981) —— `load` 把三个 dock 还原、`center.to_item` 重建中心树、刷新 toggle 按钮；`dump` 反向收集。

最后，`state.rs` 自带一个反序列化测试，能让你直观看到一份真实存档长什么样：

[crates/ui/src/dock/state.rs:230-263](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/state.rs#L230-L263) —— 它加载 `crates/ui/fixtures/layout.json`，断言中心是 `StackPanel` 含 2 个子项、左 dock `size=350px` 且 `placement=Left`、底部 dock 含 2 个面板等。这是理解存档格式的最佳入口。

#### 4.3.4 代码实践

**目标**：观察 `DockEvent::LayoutChanged` 如何驱动「布局自动存盘」。

**步骤**：

1. 打开 [crates/story/examples/dock.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/examples/dock.rs)，阅读三段关键代码：
   - 订阅 `DockEvent`：第 82-90 行 `cx.subscribe_in(&dock_area, ...)`，回调里收到 `LayoutChanged` 就调 `save_layout`。
   - 防抖存盘：第 179-205 行 `save_layout`，用 `background_executor().timer(Duration::from_secs(10))` 延时 10 秒，且「和上次 state 相同则跳过」——这正是 4.3.1 注释里建议的「防抖」做法。
   - 退出时存盘：第 92-102 行 `cx.on_app_quit` 里 `dump` 后写盘。

2. 运行示例 `cargo run -p gpui-component-story --example dock`，拖动几个分隔条、切换几个标签后**保持窗口打开至少 10 秒**，观察终端是否打印 `Save layout...`。

3. 关闭窗口后，查看 `target/docks.json`（debug 模式路径，见第 37 行），用编辑器打开，对照 4.3.3 的 `PanelInfo` 字段，找出 `panel_name`、`stack`/`tabs` 节点、`left_dock`/`right_dock` 的 `size` 与 `open` 字段。

4. 再次启动示例：因为 `target/docks.json` 已存在，会走 `load_layout` 而非 `reset_default_layout`，你之前调整的布局会被恢复（注意版本号判断：第 224 行 `state.version != Some(MAIN_DOCK_AREA.version)` 会弹窗询问是否重置）。

**需要观察的现象**：拖动布局后约 10 秒看到 `Save layout...`；`docks.json` 内容随布局变化而更新；再次启动界面与上次关闭时一致。

**预期结果**：完整验证「交互 → `PanelEvent` → `DockEvent::LayoutChanged` → `dump` 存盘 → 下次 `load` 恢复」闭环。若本地无法编译运行，记为「待本地验证」，可改为纯阅读上述源码、口述事件流。

#### 4.3.5 小练习与答案

**练习 1**：为什么示例 `save_layout` 要延时 10 秒、且比较「与上次 state 是否相同」？
**答案**：因为 `DockEvent::LayoutChanged` 触发非常频繁（拖分隔条时会连发），若每次都立刻写盘会产生大量 IO 与卡顿。延时 + 去重 = 一种简易防抖（debounce），既保证最终落盘，又避免频繁写文件。

**练习 2**：如果把一个自定义 `Panel` 加进中心区并存盘，但忘记在启动时 `register_panel` 注册它的名字，下次 `load` 会发生什么？
**答案**：`PanelState::to_item` 走到 `PanelInfo::Panel` 分支时，`PanelRegistry::build_panel` 找不到该名字，会返回一个 `InvalidPanel`（见 `dock/panel.rs` 的 `build_panel`），界面上显示为「无效面板」占位，而不是你原来的面板。

---

## 5. 综合实践

把本讲三块内容串起来，完成下面这个**贯穿任务**：

> **任务**：基于 dock 示例，绘制并改造一棵中心区 `DockItem` 树，验证你对「容器 / 布局树 / 事件」三件套的理解。

具体做法：

1. **画图**：参照 4.1.4 运行的 dock 示例当前界面，在纸上画出它中心区的 `DockItem` 树（哪个是 `Split`、哪个是 `Tabs`、轴向是水平还是垂直）。完成后，对照 `crates/story/examples/dock.rs` 的 `reset_default_layout` + `init_default_layout`（第 262-371 行）核对答案。

2. **改造**：仿照 4.2.4，把中心区从「一层 `v_split` 包一组 `tabs`」改成「水平 `h_split`：左边一组 tabs、右边一个 `v_split`（上下两个 tab）」。删除 `target/docks.json` 后运行，确认界面变成左右分屏。

3. **事件追踪**：在示例的 `cx.subscribe_in(&dock_area, ...)` 回调里（第 82-90 行）加一行 `println!("DockEvent: {:?}", ev);`（示例修改），然后拖动分隔条、切换标签，观察控制台输出，体会 `LayoutChanged` 的触发频率，并理解为何需要防抖。

4. **存档验证**：查看生成的 `target/docks.json`，确认你改造后的树结构（左右分屏）被正确序列化——找到对应的 `stack` 节点和它的 `axis` 字段（0=水平）。

完成本任务后，你应该能独立地：看懂任意一棵 `DockItem` 树、预测它的渲染结果、并解释布局变化如何经 `DockEvent` 流向外部存盘逻辑。

> 说明：步骤 2 的改造代码属于示例修改，需自行编辑 `crates/story/examples/dock.rs`；本讲不提供可直接复制粘贴的完整 main，因为可运行范本就是该示例本身。自定义 `Panel` 的写法是 u6-l2 的内容，本讲聚焦在容器与树的组合方式。

## 6. 本讲小结

- `DockArea` 是顶层 `Render` 视图，用「left(flex_none) | center-column(flex_1) | right(flex_none)」的 flex 三段式拼出窗口，底部 dock 藏在中心列内部；所有 dock 尺寸受 `PANEL_MIN_SIZE = 100px` 下限约束。
- `DockItem` 是一棵**递归布局树**：`Split`（分屏，唯一可递归节点，背后是 `StackPanel`）/ `Tabs`（标签页，`TabPanel`）/ `Panel`（单面板叶子）/ `Tiles`（自由拼贴）。
- 任何 IDE 式复杂布局都能用 `Split` 嵌套 + `Tabs` 组合表达；停靠位 `Dock` 内部只支持 `Split`/`Tabs`/`Panel`，不支持 `Tiles`。
- 构造 `DockItem` 需要传入 `&WeakEntity<DockArea>`，因为构造时要建立实体并向 DockArea 订阅事件（用 `window.defer` 推迟到帧外）。
- `DockArea` 实现 `EventEmitter<DockEvent>`，把底层 `PanelEvent` 翻译成对外的 `LayoutChanged`/`DragDrop`，并处理 `ZoomIn/ZoomOut`；`LayoutChanged` 是「布局自动存盘」的钩子，因触发频繁建议防抖。
- 布局可经 `dump`/`load` 与 `DockAreaState` 序列化往返；反序列化靠全局 `PanelRegistry` 按面板 `panel_name` 重建实体，未注册的名字会变成 `InvalidPanel`。

## 7. 下一步学习建议

- **u6-l2 Panel / StackPanel / TabPanel 与拖拽、序列化**：本讲刻意回避了「如何定义一个自定义 `Panel`」。下一讲会精读 `Panel`/`PanelView` trait、`register_panel` 注册、面板的拖拽重排（`add_panel_at`/`Placement`）与 `StackPanel`/`TabPanel` 的容器内部，把你本讲看到的「叶子节点」真正填满。
- **u6-l3 Tiles：自由拼贴布局**：本讲对 `DockItem::Tiles` 只点到为止。如果你想理解「面板在画布上自由拖动、边缘吸附」的机制，那是下一讲的 `dock/tiles.rs`（注意它有独立的 `History` 撤销/重做、`MINIMUM_SIZE = 100×100` 等约束）。
- **延伸阅读**：可先浏览 `crates/ui/fixtures/layout.json`（被 4.3.3 的测试加载），对照 `PanelState`/`PanelInfo` 字段，建立「JSON 存档 ↔ 运行时树」的直觉；再读 `dock/stack_panel.rs` 全文，看 `Split` 的分隔条拖拽如何经 `ResizableState` 上报 `PanelEvent::LayoutChanged`。
