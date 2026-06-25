# Table 与 DataTable：大数据虚拟化表格

## 1. 本讲目标

学完本讲，你应该能够：

- 区分 gpui-component 中两个同名但定位完全不同的表格组件：**无状态、可组合的 `Table`** 与 **虚拟化、代理驱动的 `DataTable`**。
- 理解 `DataTable` 是如何同时**虚拟化行**与**虚拟化列**的（行用 GPUI 的 `uniform_list`，列用上一讲的 `VirtualList`）。
- 掌握 `Column` 列定义的结构与运行时的 `ColGroup`，以及列宽拖拽调整、列排序、左侧固定列的实现机制。
- 学会通过实现 `TableDelegate` trait，把任意结构化大数据接进 `DataTable`。

## 2. 前置知识

本讲是「高性能数据展示」单元的第二讲，承接 [u7-l1 虚拟化原理](./u7-l1-virtual-list-and-list.md)，请确保你已经理解以下概念：

- **虚拟化的本质**：每帧只渲染可见区间内的少数元素，把「可见元素数」与「总数据量」解耦。
- **`uniform_list`（GPUI 内置）**：假设所有项等高，按「滚动偏移 / 行高」直接算出可见行的整数区间，是最快的虚拟化方式。
- **`VirtualList`（本库）**：支持异构尺寸与横/纵双轴，但要求调用方预先给出每一项的尺寸（`item_sizes`），靠「前缀和 + 滚动偏移」的线性扫描求可见区间。
- **状态实体 + 无状态外壳范式**（`List`/`ListState`、`Input`/`InputState` 同构）：状态放进 `Entity`，外壳是 `RenderOnce` 组件。
- **`Sizable` / `Size`**：统一的 xs/sm/md/lg 尺寸机制（见 [u2-l2](./u2-l2-styled-and-sizable.md)）。

一个关键术语提前点明：本讲的 **「虚拟化」几乎全部发生在 `DataTable` 里**。简单版的 `Table` 没有任何虚拟化，它是为「几十行、需要灵活拼装」的小表格准备的——这一点会在 4.1 节用源码注释直接证明。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/ui/src/table/mod.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/mod.rs) | 模块入口，把子模块全部 `pub use` 再导出，并注册 `init` 绑定键盘快捷键。 |
| [crates/ui/src/table/table.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/table.rs) | **简单版 `Table`**：`Table`/`TableHeader`/`TableBody`/`TableRow`/`TableHead`/`TableCell`/`TableFooter`/`TableCaption`，全是无状态 `RenderOnce`，无虚拟化。 |
| [crates/ui/src/table/data_table.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/data_table.rs) | **虚拟化版 `DataTable`** 的外壳（`RenderOnce`），负责外观、焦点、绑定 action，本身不画表格内容。 |
| [crates/ui/src/table/state.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/state.rs) | **`TableState`**：虚拟化引擎（2300+ 行），真正画表头、虚拟化行、虚拟化列、处理拖拽缩放/排序/选择的地方。 |
| [crates/ui/src/table/column.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/column.rs) | **`Column` 列定义**、`ColumnGroup` 分组表头、运行时 `ColGroup`、`ColumnSort`/`ColumnFixed` 枚举。 |
| [crates/ui/src/table/delegate.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/delegate.rs) | **`TableDelegate` trait**：用户实现它来提供数据与渲染。 |
| [crates/story/src/stories/data_table_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/data_table_story.rs) | Story Gallery 里的完整范例：股票行情表（数千行 × 数十列）。 |

## 4. 核心概念与源码讲解

### 4.1 Table：无状态的简单表格（无虚拟化）

#### 4.1.1 概念说明

`Table` 是一组**可组合、无状态**的声明式积木，思路类似 HTML 的 `<table>`：你用 `TableHeader`/`TableBody`/`TableRow`/`TableHead`/`TableCell` 手动拼出结构。它**不做虚拟化、不做列管理**——把几千行数据塞进去会全部渲染，因此只适合**几十行级别**、需要灵活自定义的小表格。

这是源码注释里写死的定位，请直接看 doc comment：

[crates/ui/src/table/table.rs:10-13](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/table.rs#L10-L13) — 这段注释明确说 `Table` 是「simple, stateless, composable table without virtual scrolling or column management」，与 `DataTable` 形成对照。

#### 4.1.2 核心流程

`Table` 的渲染靠 **flex 布局 + 比例宽度** 实现，而不是真正的网格：

1. `TableRow` 是一个水平 flex 容器（`flex_row`），子元素就是一行里的单元格。
2. 每个 `TableCell`/`TableHead` 默认用 `flex_basis(relative(col_span))` + `flex_shrink_1`：即按 `col_span` 的比例瓜分行宽。
3. 若调用了 `.width(px)` 显式设宽，则改为该像素宽（`flex_shrink_0`，不收缩）。
4. 最小单元格宽度由常量 `MIN_CELL_WIDTH = 100px` 兜底。

尺寸（`Sizable`）通过 `ChildElement::with_ix` 链向下传递：`Table` 拿到 `size` 后，在 render 时把它转发给每个子元素，这样在 `Table` 上调一次 `.small()`，整张表都变小。

#### 4.1.3 源码精读

`MIN_CELL_WIDTH` 常量（单元格最小宽度兜底）：

[crates/ui/src/table/table.rs:8](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/table.rs#L8)

`Table` 的 `render`：一个 `div` 容器，把所有子元素按下标与尺寸转发渲染（这就是「尺寸自动下传」的实现点）：

[crates/ui/src/table/table.rs:87-103](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/table.rs#L87-L103)

`TableCell` 的 `render`：注意第 561-572 行的 `.when(style.size.width.is_none(), ...)` 分支——只有「没有显式设宽」时才用比例 `flex_basis`，否则尊重显式宽度；同时 `min_w(MIN_CELL_WIDTH * col_span)` 保证最小宽度：

[crates/ui/src/table/table.rs:553-575](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/table.rs#L553-L575)

#### 4.1.4 代码实践

**实践目标**：用最简 `Table` 拼一张 3 列的小表，体会「无虚拟化、可组合」。

**操作步骤**：

1. 参考 doc comment 里的示例写法（[table.rs:18-33](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/table.rs#L18-L33)），在你的视图 `render` 中写出：

   ```rust
   // 示例代码：基于 doc comment 改写
   Table::new()
       .small()
       .child(TableHeader::new().child(
           TableRow::new()
               .child(TableHead::new().child("Name"))
               .child(TableHead::new().child("Email"))
       ))
       .child(TableBody::new()
           .child(TableRow::new()
               .child(TableCell::new().child("John"))
               .child(TableCell::new().child("john@example.com")))
       )
   ```

2. 给某个 `TableCell` 加 `.text_right()` 与 `.col_span(2)`，观察对齐与宽度比例变化（见 [table.rs:509-525](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/table.rs#L509-L525)）。

**需要观察的现象**：单元等宽（默认 `flex_basis(relative(1))`）；改 `col_span(2)` 后该格变宽约一倍。

**预期结果**：能正常渲染并对样式变化有响应。**注意它没有任何滚动条**——这印证了它不做虚拟化。

#### 4.1.5 小练习与答案

**练习 1**：`Table` 为什么不适合渲染上万行数据？
**答案**：它没有虚拟化，每一行都会进入元素树参与布局与绘制，行数与渲染开销线性相关，大数据量会掉帧。大数据请用 `DataTable`。

**练习 2**：`Table` 上调用 `.small()` 后，子单元格为什么也会变小？
**答案**：`Table` 实现了 `Sizable`，把 `size` 存下；`render` 时通过 `.map(|(ix, c)| c.into_any(ix, self.size))` 把 `size` 经 `ChildElement::with_ix` 链传给每个子元素（[table.rs:96-101](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/table.rs#L96-L101)）。

---

### 4.2 DataTable：虚拟化的行 × 列渲染

#### 4.2.1 概念说明

`DataTable` 才是「大数据表格」：它同时**虚拟化行和列**，支持行/列/单元格三种选择、列宽拖拽、列拖动重排、左侧固定列、点击排序、右键菜单、滚动到底加载更多。

它遵循库的「状态实体 + 无状态外壳」范式：

- **外壳 `DataTable<D>`**（data_table.rs）：`RenderOnce` 组件，只负责外观（边框/背景/圆角）、`track_focus` 焦点、`key_context("DataTable")` 与把一组 action 绑定到 `TableState`。它本身不画表格内容。
- **状态 `TableState<D>`**（state.rs）：有状态实体，`render` 时真正画出表头、虚拟化行、虚拟化列、处理交互。
- **数据来源 `D: TableDelegate`**：你实现的 trait，提供行数、列数、列定义、单元格渲染（见 4.4 节）。

#### 4.2.2 核心流程：行与列分别怎么虚拟化

`DataTable` 用了**两套不同的虚拟化机制**，因为行与列的「等高性」不同：

```
            ┌──────────────────────────────────────────┐
表头 render  │ render_table_header                       │
            │   calculate_visible_leaf_col_range()      │ ← 列虚拟化（手动区间）
            │   只画可见列 + 左右 spacer 填充总宽        │
            ├──────────────────────────────────────────┤
表体 render  │ uniform_list(render_rows_count, ...)      │ ← 行虚拟化（GPUI 内置）
每一行       │   visible_range 回调 → 只渲染可见行       │
            │   ├─ 左侧固定列（常驻，不进虚拟列表）      │
            │   └─ virtual_list(Axis::Horizontal, ...)   │ ← 列虚拟化（本库 VirtualList）
            └──────────────────────────────────────────┘
```

**行虚拟化**：表体是 GPUI 的 `uniform_list`。因为每行等高（高度由 `options.size.table_row_height()` 决定），`uniform_list` 用最简单的整数除法算出可见行区间，无需前缀和。可见行号从回调参数 `visible_range: Range<usize>` 拿到。

**列虚拟化**：因为列宽各异（不能用整数除法），且要让表头与表体严格对齐，所以列的虚拟化是**手动算区间**：

- 在**表头**里，`calculate_visible_leaf_col_range` 根据水平滚动偏移做前缀和扫描，求出可见叶子列区间 `[range_start, range_end)`，并在两侧放 inert 的 spacer div 撑住总宽（让滚动条范围正确）。
- 在**表体每一行**里，复用上一讲的 `crate::virtual_list::virtual_list(..., Axis::Horizontal, col_sizes, ...)`，把这一行的可见列交给 VirtualList 渲染。

数学上，列的可见区间就是上一讲 VirtualList 的前缀和扫描：设第 `i` 列宽 `w_i`，前缀和 \( S_i = w_0 + w_1 + \dots + w_{i-1} \)，水平滚动偏移 `x`（取正值），可视宽 `W`，则

\[ \text{range\_start} = \min\{\, i \mid S_i + w_i > x \,\},\qquad \text{range\_end} = \min\{\, i \mid S_i > x + W + \text{overdraw} \,\} \]

源码里 `overdraw` 取 200px，防止快速滚动时边缘闪现。

#### 4.2.3 源码精读

`DataTable` 的 doc comment 列出了全部能力（选择/虚拟滚动/可缩放列/可移动列/固定列/可排序/右键菜单）：

[crates/ui/src/table/data_table.rs:52-88](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/data_table.rs#L52-L88)

`DataTable` 的 `render`：外壳只做焦点、`key_context`、action 绑定，再把 `self.state`（`TableState`）当子元素挂上去——真正画表格的是 `TableState::render`：

[crates/ui/src/table/data_table.rs:140-173](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/data_table.rs#L140-L173)

键盘导航的快捷键在 `init` 里绑定到 `key_context("DataTable")`（↑↓选行、←→/Tab 选列、Home/End、PageUp/Down、ESC 取消）：

[crates/ui/src/table/data_table.rs:15-29](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/data_table.rs#L15-L29)

`TableState` 持有**两个独立的滚动 handle**，分别对应行（纵）与列（横）虚拟化：

[crates/ui/src/table/state.rs:226-227](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/state.rs#L226-L227) — `vertical_scroll_handle: UniformListScrollHandle`（行，等高）与 `horizontal_scroll_handle: VirtualListScrollHandle`（列，异构宽）。

**行虚拟化**——表体用 `uniform_list`，可见行号来自回调 `visible_range`，每行调 `render_table_row`：

[crates/ui/src/table/state.rs:2222-2263](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/state.rs#L2222-L2263)

**列虚拟化（表头）**——`calculate_visible_leaf_col_range` 做前缀和扫描求可见叶子列区间，200px overdraw 防闪烁：

[crates/ui/src/table/state.rs:1470-1517](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/state.rs#L1470-L1517)

表头为什么也要虚拟化？源码注释解释得很清楚（1000+ 列时全量渲染 `render_th` 会掉到 60 帧以下，所以只画可见列 + 左右 spacer 撑总宽）：

[crates/ui/src/table/state.rs:1528-1543](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/state.rs#L1528-L1543)

**列虚拟化（表体每一行）**——行内用本库的 `virtual_list`（横轴），传入本行非固定列的尺寸 `col_sizes`，这正是 u7-l1 的 VirtualList：

[crates/ui/src/table/state.rs:1853-1871](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/state.rs#L1853-L1871)

**左侧固定列**：固定列**不进虚拟列表**，常驻渲染在最左侧，滚动时不跟随移动（靠 `ColumnFixed::Left` 识别）：

[crates/ui/src/table/state.rs:1753-1845](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/state.rs#L1753-L1845)

#### 4.2.4 代码实践

**实践目标**：亲眼看到 `DataTable` 的双轴虚拟化。

**操作步骤**：

1. 在仓库根目录运行 Gallery 并定位到 DataTable 故事（story 注册名为 `"DataTable"`，见 [data_table_story.rs:752-754](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/data_table_story.rs#L752-L754)）：

   ```bash
   cargo run -- DataTable
   ```

2. 在出现的股票行情表里，水平滚动到右侧再滚回左侧。

**需要观察的现象**：

- 纵向滚动时帧率稳定（行虚拟化生效，几千行不卡）。
- 左侧 ID/Market/Name/Symbol 四列**固定不动**，其余列横向滚动（固定列 + 列虚拟化）。
- 拖动表头列边界可改变列宽。

**预期结果**：滚动流畅、固定列不动、列宽可调。若本地无法运行 GUI，则标注「待本地验证」，改为阅读 `render` 源码（state.rs:2146 起）理解。

#### 4.2.5 小练习与答案

**练习 1**：为什么行用 `uniform_list` 而列用 `VirtualList`？
**答案**：行等高，可用「偏移/行高」整数除法直接求区间，`uniform_list` 最快；列宽各异，必须用前缀和扫描（`VirtualList`），无法用整数除法。

**练习 2**：表头与表体的列虚拟化用的是同一段代码吗？
**答案**：不是。表头用 `calculate_visible_leaf_col_range` 手动算区间 + spacer（state.rs:1470）；表体每行复用本库的 `virtual_list`（state.rs:1853）。两者要保证列宽一致才能对齐。

---

### 4.3 Column：列定义与运行时列宽

#### 4.3.1 概念说明

`Column` 是**静态列定义**（声明这一列叫什么、多宽、能否排序/缩放/移动/固定），是给 `TableDelegate::column()` 返回用的。而 `TableState` 内部维护一个**运行时**结构 `ColGroup`，它在 `Column` 基础上多了「当前实际宽度 `width`」与「渲染后落到的像素矩形 `bounds`」——因为列宽会被用户拖拽改变，运行时宽度与初始定义宽度会分叉。

#### 4.3.2 核心流程

1. `TableState::new` 时调 `prepare_col_groups`：遍历列下标，向 delegate 取 `Column`，包成 `ColGroup { column, width: column.width, bounds: default }`。
2. 用户拖拽列边界 → `resize_cols` 把 `col_groups[ix].width` 改为 clamp 后的新值 → `update_header_layout` 重算表头布局 → `cx.notify()` 重绘。
3. 渲染时一律用 `col_groups[i].width`（运行时宽度），而**不是** `column.width`（初始定义）——render 里有专门注释提醒这一点。

#### 4.3.3 源码精读

`Column` 的全部字段（key/name/align/sort/paddings/width/fixed/resizable/movable/selectable/min_width/max_width）：

[crates/ui/src/table/column.rs:11-51](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/column.rs#L11-L51)

`Column` 默认值：宽 100px、`resizable=true`、`movable=true`、`selectable=true`、`min_width=20px`、`max_width=f32::MAX`：

[crates/ui/src/table/column.rs:69-86](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/column.rs#L69-L86)

`min_width`/`max_width` 的 clamp 逻辑：设新边界时若当前 width 越界，会把它拉回边界内：

[crates/ui/src/table/column.rs:203-228](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/column.rs#L203-L228)

运行时 `ColGroup`（注意它比 `Column` 多了 `width` 与 `bounds`）：

[crates/ui/src/table/column.rs:237-253](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/column.rs#L237-L253)

`prepare_col_groups`：把 delegate 的列定义初始化成运行时 `ColGroup` 列表：

[crates/ui/src/table/state.rs:546-559](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/state.rs#L546-L559)

**列宽拖拽缩放**核心 `resize_cols`：把新宽度 clamp 到 `[min_width, max_width]`，写入运行时 `col_groups[ix].width`，重算表头并通知重绘：

[crates/ui/src/table/state.rs:1038-1058](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/state.rs#L1038-L1058)

拖拽手柄 `render_resize_handle`：一个 2px 宽、`cursor_col_resize` 的元素，用 `on_drag_move` 监听 `ResizeColumn` 拖动值，按鼠标位置算出该列应有宽度并调 `resize_cols`：

[crates/ui/src/table/state.rs:1224-1317](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/state.rs#L1224-L1317)

拖到表格左右边缘附近时自动横向滚动（方便把屏幕外的列拉进来）：

[crates/ui/src/table/state.rs:1010-1034](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/state.rs#L1010-L1034)

**点击排序** `perform_sort`：在 `Default → Descending → Ascending → Default` 三态间循环，清掉其他列的排序态，再调 delegate 的 `perform_sort`：

[crates/ui/src/table/state.rs:1060-1090](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/state.rs#L1060-L1090)

#### 4.3.4 代码实践

**实践目标**：理解列定义如何配置宽/固定/可缩放。

**操作步骤**：阅读 Story 里的列定义，前 4 列都是固定列且各有约束：

[crates/story/src/stories/data_table_story.rs:213-233](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/data_table_story.rs#L213-L233)

- `ID` 列：`.width(60.).fixed(ColumnFixed::Left).min_width(40.).max_width(100.)`——固定在左、宽 60、缩放范围 40~100。
- `Name` 列：`.width(180.).fixed(ColumnFixed::Left).max_width(300.)`——固定在左、宽 180、最大 300。

**需要观察的现象**：横向滚动时这 4 列不动；拖 ID 列边界，宽度被限制在 40~100px 之间。

**预期结果**：固定列不随横向滚动移动，列宽被 clamp 在 min/max 内。

#### 4.3.5 小练习与答案

**练习 1**：为什么渲染时要用 `col_groups[i].width` 而不是 `column.width`？
**答案**：`column.width` 是初始定义值，用户拖拽后实际宽度已改变，存在 `col_groups[i].width`。render 源码注释（state.rs:2227-2229）专门强调：`col.bounds.size.width` 在首帧 prepaint 前还是 0，必须用 `col.width`。

**练习 2**：把某列设为操作列（放按钮），不希望它参与选择，该怎么做？
**答案**：`.selectable(false)`（[column.rs:182-201](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/column.rs#L182-L201)），这样该列及其单元格都不会被选中。

---

### 4.4 TableDelegate：把数据接进表格

#### 4.4.1 概念说明

`TableDelegate` 是 `DataTable` 的数据与渲染契约——和 u7-l1 的 `ListDelegate` 同构。`TableState` 持有 `delegate: D`，每帧按需回调 delegate 的方法来取行数、列数、列定义，并渲染每个单元格。所有渲染方法（`render_td` 等）拿到的 `cx` 是 `Context<TableState<Self>>`，所以 delegate 可以订阅事件、发 action、更新自身数据。

#### 4.4.2 核心流程

`DataTable` 接入数据的四步：

1. 定义你的数据结构（如 `Vec<Stock>`）和列定义 `Vec<Column>`，放进一个 delegate 结构体。
2. `impl TableDelegate for YourDelegate`：必填 4 个方法。
3. `cx.new(|cx| TableState::new(delegate, window, cx))` 创建状态实体。
4. `DataTable::new(&state)` 渲染外壳。

**必填方法**（无默认实现）：

| 方法 | 作用 |
| --- | --- |
| `columns_count(&self, cx) -> usize` | 列数 |
| `rows_count(&self, cx) -> usize` | 行数 |
| `column(&self, col_ix, cx) -> Column` | 第 `col_ix` 列的列定义（仅在 prepare/refresh 时调用） |
| `render_td(&mut self, row_ix, col_ix, window, cx) -> impl IntoElement` | 渲染 `(row_ix, col_ix)` 单元格内容 |

**常用可选方法**：`render_th`（表头单元格）、`render_tr`（整行容器，可挂点击）、`group_headers`（多级分组表头）、`perform_sort`（排序）、`context_menu`（右键菜单）、`load_more`/`has_more`（滚动加载）、`visible_rows_changed`/`visible_columns_changed`（可见区间变化钩子，用于只更新可见数据）、`cell_text`（CSV 导出）。

#### 4.4.3 源码精读

`TableDelegate` trait 完整定义（必填方法 `render_td` 无默认体，其余多有默认实现）：

[crates/ui/src/table/delegate.rs:16-229](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/delegate.rs#L16-L229)

注意 `column` 的注释说「只在 prepare 或 refresh 时调用」——这是性能关键点，渲染每帧不会反复取列定义：

[crates/ui/src/table/delegate.rs:23-26](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/delegate.rs#L23-L26)

`visible_rows_changed` 的注释强调「会被频繁调用，务必快速，数据更新放后台任务」：

[crates/ui/src/table/delegate.rs:194-206](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/delegate.rs#L194-L206)

`TableState::new` 把 delegate 存入并立即 `prepare_col_groups`：

[crates/ui/src/table/state.rs:251-285](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/state.rs#L251-L285)

Story 里的真实 delegate 实现：`columns_count`/`rows_count`/`column` 三个必填方法，`column` 还能动态生成「额外列」用于测试超多列：

[crates/story/src/stories/data_table_story.rs:359-375](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/data_table_story.rs#L359-L375)

Story 的 `render_td`：按 `(row_ix, col_ix)` 取数据并返回元素（注释里还附了性能实测：561 个单元格渲染仅约 232µs，占单帧 2.6%）：

[crates/story/src/stories/data_table_story.rs:471-485](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/data_table_story.rs#L471-L485)

State 与外壳的接入点：

[crates/story/src/stories/data_table_story.rs:798](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/data_table_story.rs#L798) 与 [crates/story/src/stories/data_table_story.rs:1326](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/data_table_story.rs#L1326) — `let table = cx.new(|cx| TableState::new(delegate, window, cx));` 与 `DataTable::new(&self.table)`。

#### 4.4.4 代码实践

**实践目标**：从零实现一个最小 `TableDelegate`，渲染 5000 行 × 5 列，验证滚动流畅 + 列宽可拖。

**操作步骤**：

1. 新建一个独立 example crate 或在现有视图里，定义数据与 delegate（**示例代码**，参考 Story 精简）：

   ```rust
   // 示例代码
   struct Row { id: usize, name: String, score: f64, active: bool, note: String }

   struct MyDelegate { rows: Vec<Row>, columns: Vec<Column> }

   impl TableDelegate for MyDelegate {
       fn columns_count(&self, _cx: &App) -> usize { self.columns.len() }
       fn rows_count(&self, _cx: &App) -> usize { self.rows.len() }
       fn column(&self, col_ix: usize, _cx: &App) -> Column {
           self.columns[col_ix].clone()
       }
       fn render_td(&mut self, row_ix: usize, col_ix: usize,
                    _window: &mut Window, _cx: &mut Context<TableState<Self>>) -> impl IntoElement {
           let row = &self.rows[row_ix];
           let txt = match col_ix {
               0 => row.id.to_string(),
               1 => row.name.clone(),
               2 => format!("{:.2}", row.score),
               3 => if row.active { "是" } else { "否" }.into(),
               _ => row.note.clone(),
           };
           div().child(txt)
       }
   }
   ```

2. 生成 5000 行数据，列定义参考 4.3.4 节（前一两列用 `.fixed(ColumnFixed::Left)`）：

   ```rust
   // 示例代码
   let columns = vec![
       Column::new("id", "ID").width(60.).fixed(ColumnFixed::Left).text_center(),
       Column::new("name", "Name").width(160.).fixed(ColumnFixed::Left),
       Column::new("score", "Score").width(100.).text_right().sortable(),
       Column::new("active", "Active").width(80.).text_center(),
       Column::new("note", "Note").width(200.),
   ];
   ```

3. 创建状态与外壳（**示例代码**）：

   ```rust
   // 示例代码
   let delegate = MyDelegate { rows, columns };
   let table = cx.new(|cx| TableState::new(delegate, window, cx));
   // 在视图 render 里：
   // DataTable::new(&self.table)
   ```

4. 运行后：纵向滚动 5000 行观察帧率；拖动表头列边界改变列宽；横向滚动观察固定列。

**需要观察的现象**：

- 5000 行纵向滚动保持流畅（`uniform_list` 行虚拟化）。
- 拖列边界即时改变宽度（`resize_cols` clamp 到 min/max）。
- 前两列横向滚动时不移动（固定列）。

**预期结果**：以上三点均成立。若本地无法编译运行 GUI，则标注「待本地验证」，改为阅读 `TableState::render`（state.rs:2146）与 `render_table_row`（state.rs:1711）跟踪「数据如何通过 delegate 流到屏幕」的调用链。

#### 4.4.5 小练习与答案

**练习 1**：`TableDelegate` 的 4 个必填方法分别承担什么职责？
**答案**：`columns_count` 给列数、`rows_count` 给行数、`column` 给某列的静态 `Column` 定义（仅 prepare/refresh 时调）、`render_td` 渲染具体单元格内容。

**练习 2**：若你的数据来自网络，只想在滚动到某区间时按需加载，该用哪个方法？
**答案**：`visible_rows_changed(visible_range, ...)`（delegate.rs:194）——它在可见行变化时被调用，可在里面用后台任务加载该区间数据；同时配合 `has_more`/`load_more` 做触底加载。

---

## 5. 综合实践

把本讲四块串起来：构建一个「**任务管理表**」，要求同时用到本讲全部知识点。

任务清单：

1. **选对组件**：任务不到 20 行且需要每行放一个操作按钮 → 用 `Table`（4.1）；若是上万条历史任务 → 用 `DataTable`（4.2）。请先写出你的选型理由。
2. **实现 delegate**：为 `DataTable` 实现 `TableDelegate`，列至少包含「ID（固定左）、名称（固定左）、优先级（可排序）、负责人、截止日期、状态、操作」。
3. **列配置**：操作列 `.selectable(false)`；优先级列 `.sortable()`；ID/名称列 `.fixed(ColumnFixed::Left)`。
4. **数据规模**：灌入 5000 行假数据。
5. **观察清单**：
   - 纵向滚动是否流畅（行虚拟化）。
   - 横向滚动时 ID/名称是否固定。
   - 点击优先级表头是否三态排序。
   - 拖列边界是否改变宽度且被 min/max 限制。
   - 右键某行能否弹出菜单（实现 `context_menu`）。

通过这一个任务，你将完整走通「列定义 → delegate → 状态 → 外壳 → 交互」的全链路。

## 6. 本讲小结

- gpui-component 有**两个表格**：`Table`（无状态、可组合、**无虚拟化**，适合小表）与 `DataTable`（虚拟化、代理驱动，适合大数据）——选型第一准则。
- `DataTable` 遵循「状态实体 `TableState` + 无状态外壳 `DataTable` + 数据契约 `TableDelegate`」范式。
- **行虚拟化**用 GPUI 的 `uniform_list`（行等高，整数除法求区间）；**列虚拟化**在表头手动算 `calculate_visible_leaf_col_range`、在表体每行复用本库的 `virtual_list`（横轴，前缀和扫描）。
- **左侧固定列**不进虚拟列表，常驻渲染；滚动时纹丝不动。
- `Column` 是静态列定义，`ColGroup` 是运行时状态（多了被拖拽后的真实 `width` 与 `bounds`），渲染一律用运行时宽度。
- 列宽拖拽在 `resize_cols` 里 clamp 到 `[min_width, max_width]`；排序在 `perform_sort` 里做三态循环；二者都靠 `cx.notify()` 重绘。

## 7. 下一步学习建议

- **继续数据展示线**：本单元下一讲是 [u7-l3 Tree](./u7-l3-tree-component.md)，树形组件同样基于 `List`/虚拟化，并引入 `IndexPath` 定位机制。
- **回看虚拟化底座**：若对 `virtual_list` 的前缀和扫描还有疑问，重读 [u7-l1](./u7-l1-virtual-list-and-list.md) 的 VirtualList Element 实现。
- **深入源码**：`TableState`（state.rs）是本库最复杂的实体之一，建议通读其 `render`（state.rs:2146）→ `render_table_header`（state.rs:1519）→ `render_table_row`（state.rs:1711）三条主线，理解表头与表体如何靠同一套 `col_groups` 宽度严格对齐。
- **性能调优**：若你的表格列特别多，注意表头虚拟化的 200px overdraw（state.rs:1505）与 `render_th`/`render_td` 的开销，参考 Story `render_td` 注释里的性能实测（data_table_story.rs:471）。
