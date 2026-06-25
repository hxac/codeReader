# 虚拟化原理：VirtualList 与 List

## 1. 本讲目标

本讲是「高性能数据展示」单元的第一讲。学完后你应当能够：

- 说清楚「虚拟化（virtualization）」到底省掉了什么，以及它为什么能让 10 万级数据依然流畅。
- 读懂通用虚拟滚动元素 `VirtualList` 的布局与可见区间算法，并会用 `v_virtual_list` / `h_virtual_list` 渲染异构尺寸的行或列。
- 理解基于代理（delegate）的 `List` 组件如何把「数据、渲染、键盘导航、搜索、懒加载」解耦，并能用 `ListDelegate` 渲染上万条数据项。

本讲属于进阶内容，需要你已掌握 u2-l2 讲过的 `Styled` / `RenderOnce` / 有状态 `Render` 视图、`Entity` 与 GPUI 的元素（Element）生命周期（`request_layout` → `prepaint` → `paint`）。

## 2. 前置知识

阅读本讲前，先建立这几个直觉：

- **GPUI 的元素三段式生命周期**：一个自定义 `Element` 在每帧会依次调用 `request_layout`（量尺寸、报名布局）、`prepaint`（确定最终位置、做命中测试）、`paint`（实际绘制）。虚拟化的核心工作几乎全发生在 `prepaint` 阶段。
- **滚动偏移是负数**：GPUI 里 `ScrollHandle::offset()` 返回的 y 通常是负值——内容向上滚，相当于把内容的原点向上「拉」，所以 offset 为负。本讲源码里你会反复看到 `-(scroll_offset.y)` 这样的写法，就是把负偏移换算成「已经滚进视口的正距离」。
- **状态外置范式**：gpui-component 一贯把跨帧状态放进 `Entity`，把无状态外观留给 `RenderOnce`。`List` 同样遵循「`ListState`（有状态实体）+ `List`（无状态外壳）」的组合，和 u4-l4 的 `Input`/`InputState` 完全同构。
- **代理（delegate）模式**：列表不直接持有你的业务数据，而是通过一个你实现的 `ListDelegate` trait 去「问」它：第几段有几条？第几条长什么样？这样新增一种列表只需写一个 delegate，不用改库。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/ui/src/virtual_list.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs) | 通用虚拟滚动元素 `VirtualList`，支持横/纵两轴、每项可不同尺寸；`List` 与 `Table` 都复用它。 |
| [crates/ui/src/list/list.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list.rs) | 列表组件 `List` 与其状态 `ListState`：内置搜索框、键盘导航、滚动、懒加载。 |
| [crates/ui/src/list/delegate.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/delegate.rs) | `ListDelegate` trait：使用者实现它来提供数据与渲染。 |
| [crates/ui/src/list/cache.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/cache.rs) | `RowsCache`：把「分段数据」拍平成一行行的渲染条目，并为 `VirtualList` 准备尺寸表。 |
| [crates/ui/src/index_path.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/index_path.rs) | `IndexPath { section, row, column }`：列表里定位一条数据的三元坐标。 |
| [crates/story/src/stories/virtual_list_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/virtual_list_story.rs) | `VirtualList` 的演示 Story，最高 50 万项仍可流畅滚动。 |
| [crates/story/src/stories/list_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/list_story.rs) | `List` 的演示 Story：分组、搜索、懒加载、键盘导航俱全。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先讲虚拟化的核心思想与可见区间算法（4.1），再精读通用元素 `VirtualList`（4.2），接着是基于代理的 `List`/`ListState`（4.3），最后是使用者要实现的 `ListDelegate` 契约（4.4）。

### 4.1 虚拟化的核心思想：只渲染可见项

#### 4.1.1 概念说明

假设你要在窗口里显示 1 万条数据。最朴素的写法是把 1 万个元素全部塞进一个滚动容器——这会带来两个灾难：

1. **布局成本**：GPUI 每帧都要对这 1 万个元素跑一遍布局（量尺寸、算位置），即使 9990 个都在视口外看不见。
2. **绘制与内存成本**：每个元素都要持有自己的绘制状态，1 万个元素就是 1 万份开销，滚动时还可能触发大量重排。

虚拟化的思路很直接：**既然视口（viewport）一次只能显示几十条，那就只构建并渲染这几十条，其余的根本不进入元素树。** 滚动时，根据当前滚动偏移动态计算「现在该显示第几条到第几条」，把滚出视口的销毁、滚进视口的创建。对用户而言看到的是连续的列表，对引擎而言每帧只处理一个很小的窗口。

`VirtualList` 的文档注释把这条原则写在了文件最顶部（[crates/ui/src/virtual_list.rs:1-12](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L1-L12)）：它受 GPUI 内置 `uniform_list` 启发，但区别在于**每一项可以有不同的尺寸**（适合行高不一的表格）。

#### 4.1.2 核心流程

虚拟化的关键在于「给定滚动偏移，求出可见区间 `[first, last)`」。设主轴（纵向列表就是 y 轴）上：

- 第 \(i\) 项的主轴尺寸为 \(s_i\)（已包含它与下一项之间的间隙 \(g\)，最后一项不含）；
- 第 \(i\) 项的起点（前缀和）为：

\[
o_i = \sum_{k=0}^{i-1} s_k
\]

- 整条列表的内容总长：

\[
L = \sum_{i=0}^{n-1} s_i
\]

滚动偏移记为 \(d\)（负数，向上滚）。视口在「内容坐标」里覆盖的区间是 \([-d,\;-d + H]\)，其中 \(H\) 是视口高度、\(p\) 是上内边距。则：

- **第一个可见项** `first`：最小的 \(i\)，使得该项的底边超过已滚进视口的距离，即 \(o_i + s_i > -(d + p)\)；
- **最后一个可见项** `last`：最小的 \(i\)，使得该项底边超过视口下沿，即 \(o_i + s_i > -d + H\)，再 +1 留一点余量。

最终只对 `[first, min(last, n))` 调用渲染闭包，其余项完全不参与布局与绘制。

#### 4.1.3 源码精读

上述算法在 `VirtualList::prepaint` 里是一段线性扫描。先看「求第一个可见项」：

```rust
let mut cumulative_size = px(0.);
let mut first_visible_element_ix = 0;
for (i, &size) in item_sizes.iter().enumerate() {
    cumulative_size += size;
    if cumulative_size > -(scroll_offset.y + paddings.top) {
        first_visible_element_ix = i;
        break;
    }
}
```

这段对应 [crates/ui/src/virtual_list.rs:656-665](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L656-L665)。`item_sizes` 是预先算好的「每项主轴尺寸（含 gap）」数组，`cumulative_size` 即前缀和 \(o_i + s_i\)（该 item 的底边）。`-(scroll_offset.y + paddings.top)` 就是「已滚进视口的距离」。命中即得到 `first`。`last` 的求法对称（[crates/ui/src/virtual_list.rs:667-682](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L667-L682)）。

得到区间后，**只对这个区间调用渲染闭包**：

```rust
let visible_range = first_visible_element_ix
    ..cmp::min(last_visible_element_ix, self.items_count);

let items = (self.render_items)(visible_range.clone(), window, cx);
```

见 [crates/ui/src/virtual_list.rs:686-689](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L686-L689)。`(self.render_items)` 是用户传入的闭包，输入一个 `Range<usize>`、输出一组元素——这正是「按需渲染」的接口边界：你永远只会被要求渲染可见的那一小段。

随后这些可见项被逐一摆到「内容原点 + 该项前缀和 + 滚动偏移」的位置，并整体套上一层 `ContentMask` 裁掉越界部分（[crates/ui/src/virtual_list.rs:691-720](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L691-L720)）。视口外的项既不在这里出现，自然也不会被 `paint`（[crates/ui/src/virtual_list.rs:728-752](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L728-L752)）。

#### 4.1.4 代码实践

**目标**：用纸笔（或心算）验证可见区间算法，建立对「只渲染可见项」的直觉。

**步骤**：

1. 假设一个纵向 `VirtualList`，共 1000 项，每项高度 30px、间隙 0；视口高 300px、上内边距 0。
2. 当前 `scroll_offset.y = -600px`（向下滚了 600px）。按 4.1.2 的公式手算 `first` 与 `last`。
3. 打开 [crates/ui/src/virtual_list.rs:656-684](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L656-L684)，核对你的答案。

**需要观察的现象 / 预期结果**：前缀和底边首次超过 600 的项是第 20 项（\(20 \times 30 = 600\)，严格大于才命中，故 `first = 20`）；底边首次超过 \(600 + 300 = 900\) 的是第 30 项，`last = 31`。可见区间 `[20, 31)`，共 11 项——与「视口高 300 / 每项 30 ≈ 10 项 +1 余量」吻合，其余 989 项不渲染。

#### 4.1.5 小练习与答案

**练习 1**：如果把每项高度从 30px 改成 60px（其他不变），同样的视口下可见区间大约变成多少？

**答案**：视口能容纳 \(300 / 60 = 5\) 项，`last - first` 约为 6（含 +1 余量），可见项数量约减半。这说明**单项越高，虚拟化收益越大**——可见项越少，每帧工作量越小。

**练习 2**：为什么 `VirtualList` 要求调用方提前提供 `item_sizes`，而不是自己量每一项？

**答案**：若要量第 9000 项的尺寸，就得先把它渲染出来——这就违背了「只渲染可见项」。所以必须由调用方在「不渲染」的前提下给出尺寸（要么等高、要么已知）。`List` 通过「先量一个代表项、假设全员等高」来满足这一约束（见 4.3）。

### 4.2 VirtualList：通用虚拟滚动元素

#### 4.2.1 概念说明

`VirtualList` 是一个**自定义 GPUI `Element`**（不是 `RenderOnce` 组件），负责把一组「尺寸已知、数量可能极大」的项以虚拟化方式画出来。它的特点：

- **双轴**：通过 `v_virtual_list`（纵向，[crates/ui/src/virtual_list.rs:132-143](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L132-L143)）或 `h_virtual_list`（横向，[crates/ui/src/virtual_list.rs:152-163](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L152-L163)）创建。
- **异构尺寸**：每项可有自己的尺寸，但必须事先给出 `item_sizes: Rc<Vec<Size<Pixels>>>`；纵向列表只取每项的 `height`，横向只取 `width`。
- **跨轴尺寸由「测第一项」得到**：纵向列表的统一列宽，是通过测量第 0 项得到的（[crates/ui/src/virtual_list.rs:293-316](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L293-L316)）。

#### 4.2.2 核心流程

一帧内 `VirtualList` 做三件事：

1. **`request_layout`**：测第 0 项得到跨轴尺寸；按 `item_sizes` 算出每项的主轴尺寸（含 gap）`sizes`、每项起点 `origins`（前缀和）、内容总尺寸 `content_size`；用 `with_element_state` 缓存这些结果，仅当 `item_sizes` 变化时才重算（[crates/ui/src/virtual_list.rs:384-438](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L384-L438)）。
2. **`prepaint`**：算内容区、各 item 的 `Bounds`；读滚动偏移并做边界裁剪（不能滚出内容）；按 4.1 的算法求可见区间；只渲染可见项并定位（[crates/ui/src/virtual_list.rs:518-726](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L518-L726)）。
3. **`paint`**：把 prepaint 存下的可见项逐个绘制（[crates/ui/src/virtual_list.rs:728-752](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L728-L752)）。

滚动控制由 `VirtualListScrollHandle` 提供，它内部包了一个标准 `ScrollHandle` 并额外记录 `items_count` 与「延迟滚动到某项」的请求（[crates/ui/src/virtual_list.rs:37-44](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L37-L44)）。`scroll_to_item(ix, strategy)` 把请求暂存，下一帧 prepaint 时再换算成像素偏移（[crates/ui/src/virtual_list.rs:100-121](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L100-L121)）。

#### 4.2.3 源码精读

构造函数把「渲染闭包」包成「按可见区间返回 `AnyElement` 列表」的形式：

```rust
let render_range = move |visible_range, window: &mut Window, cx: &mut App| {
    view.update(cx, |this, cx| {
        f(this, visible_range, window, cx)
            .into_iter()
            .map(|component| component.into_any_element())
            .collect()
    })
};
```

见 [crates/ui/src/virtual_list.rs:178-185](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L178-L185)。注意它持有的是 `Entity<V>`（你的视图），每次只在可见区间上 `update` 视图并调用你传入的闭包 `f`——这就是「回调式按需渲染」。

真实用法可看 `VirtualListStory`：它一次性造 5000 项（[crates/story/src/stories/virtual_list_story.rs:31-32](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/virtual_list_story.rs#L31-L32)），切换到 `Size 2` 测试用例时甚至造 **50 万项**（[crates/story/src/stories/virtual_list_story.rs:59-62](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/virtual_list_story.rs#L59-L62)），而每帧只渲染可见的那几十行。其 `v_virtual_list` 调用如下（[crates/story/src/stories/virtual_list_story.rs:257-278](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/virtual_list_story.rs#L257-L278)）：

```rust
v_virtual_list(
    cx.entity().clone(),
    "items",
    self.item_sizes.clone(),
    move |story, visible_range, _, cx| {
        story.visible_range = visible_range.clone();   // 顺便把可见区间存起来，用于界面展示
        visible_range
            .map(|ix| { /* 构造第 ix 行的元素 */ })
            .collect()
    },
)
.track_scroll(&self.scroll_handle)
```

注意三点：① 闭包入参就是 `visible_range`，你只造这一段；② `.track_scroll(&self.scroll_handle)` 把滚动句柄接上，外部按钮才能 `scroll_to_item`；③ `item_sizes` 用 `Rc` 共享，切换数据规模时整体替换即可。

#### 4.2.4 代码实践

**目标**：亲眼看到虚拟化对超大数据的支撑能力。

**步骤**：

1. 在仓库根目录运行 Gallery，并聚焦到 VirtualList 演示页：

   ```bash
   cargo run -- VirtualList
   ```

2. 点击 `Size 2` 按钮（对应 50 万项）。再用滚动条或鼠标滚轮快速滚动。
3. 界面顶部会实时打印 `visible_range: (a..b)`，观察这个区间的宽度。

**需要观察的现象 / 预期结果**：尽管数据有 50 万项，`visible_range` 的宽度始终只有几十（取决于视口高度与行高），滚动应当流畅。**待本地验证**：在不同性能机器上帧率会有差异，可配合 `MTL_HUD_ENABLED=1`（macOS）或 `samply record cargo run` 观察帧率与 CPU。

#### 4.2.5 小练习与答案

**练习 1**：`VirtualList` 为什么要测「第 0 项」来决定列宽，而不是测当前可见的第一项？

**答案**：因为列宽是整列共享的固定属性，必须在布局阶段就定下来；而布局阶段（`request_layout`）时尚不知道滚动位置、也不知道可见项是谁。测第 0 项是「不依赖滚动状态」的稳定选择。`List` 则更进一步允许你用 `set_item_to_measure_index` 指定用哪一项当「测量样本」。

**练习 2**：`VirtualListScrollHandle` 为什么要「延迟」到下一帧 prepaint 才真正滚动，而不是在 `scroll_to_item` 调用时就设好偏移？

**答案**：因为要把「第 ix 项」换算成像素偏移，需要知道该项的 `Bounds`，而 `Bounds` 只有在 prepaint 阶段布局完成后才确定。所以先把请求暂存（`deferred_scroll_to_item`），等下一帧拿到 `items_bounds` 后再换算（[crates/ui/src/virtual_list.rs:581-588](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L581-L588)）。

### 4.3 List 与 ListState：基于代理的列表组件

#### 4.3.1 概念说明

`VirtualList` 已经能虚拟化，但它「太底层」：你得自己管 `item_sizes`、自己写渲染闭包、自己处理选中与键盘。`List` 是在 `VirtualList` 之上封装的**成品列表组件**，额外提供：

- **分段（section）**：数据可分多段，每段带独立的 header/footer；
- **键盘导航**：上/下选择、回车确认、ESC 取消，且键绑定挂在 `key_context("List")` 上（[crates/ui/src/list/list.rs:26-35](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list.rs#L26-L35)）；
- **内置搜索框**：`searchable(true)` 后顶部出现一个 `Input`，输入即触发 delegate 的 `perform_search`；
- **懒加载**：滚到底部附近自动调用 delegate 的 `load_more`；
- **事件**：通过 `ListEvent::{Select, Confirm, Cancel}` 向外汇报（[crates/ui/src/list/list.rs:37-45](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list.rs#L37-L45)）。

代价是：**`List` 假定所有条目等高**（文档明确写 "List required all items has the same height"，见 [crates/ui/src/list/list.rs:67-70](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list.rs#L67-L70)）。若需要不等高，请退回直接用 4.2 的 `VirtualList`。

#### 4.3.2 核心流程

`List` 的数据流如下：

1. **拍平缓存**：每帧 `render` 开头调用 `prepare_items_if_needed`（[crates/ui/src/list/list.rs:420-450](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list.rs#L420-L450)）。它先「量一个代表项」得到统一的 item/header/footer 尺寸，再交给 `RowsCache::prepare_if_needed`，把所有分段拍平成一维的 `RowEntry` 序列（`Entry` / `SectionHeader` / `SectionFooter`），并生成与之对应的 `entries_sizes`（[crates/ui/src/list/cache.rs:167-220](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/cache.rs#L167-L220)）。
2. **委托 VirtualList 渲染**：`render_items` 把 `rows_cache.entries_sizes` 当作 `item_sizes` 喂给 `v_virtual_list`，闭包里按一维下标从缓存取 `RowEntry`，再分别调 delegate 的 `render_item` / `render_section_header` / `render_section_footer`（[crates/ui/src/list/list.rs:519-559](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list.rs#L519-L559)）。
3. **懒加载探测**：闭包里顺带调 `load_more_if_need(entities_count, visible_range.end, ...)`，当可见区末尾距数据末尾小于阈值时触发 `load_more`（[crates/ui/src/list/list.rs:316-341](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list.rs#L316-L341)）。
4. **键盘导航**：`SelectUp`/`SelectDown` 动作借助 `RowsCache::prev`/`next` 在拍平序列里找上一个/下一个 `Entry`（自动跳过空段、首尾环绕），见 [crates/ui/src/list/cache.rs:101-165](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/cache.rs#L101-L165)。

#### 4.3.3 源码精读

`ListState` 是有状态实体，持有 delegate、缓存、滚动句柄、选中态等（[crates/ui/src/list/list.rs:70-88](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list.rs#L70-L88)）。它的 `Render` 把搜索框、虚拟列表、加载态组合起来，并挂上各 action 处理器（[crates/ui/src/list/list.rs:592-684](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list.rs#L592-L684)）。`List` 本身是 `RenderOnce` 外壳，只持有 `Entity<ListState<D>>` 并把它作为子视图（[crates/ui/src/list/list.rs:739-761](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list.rs#L739-L761)）——和 `Input`/`InputState` 完全同构的「外壳 + 状态实体」范式。

`render_items` 里把可见一维下标映射回 `IndexPath` 的闭包是理解全链路的关键：

```rust
v_virtual_list(
    cx.entity(),
    "virtual-list",
    rows_cache.entries_sizes.clone(),
    move |list, visible_range: Range<usize>, window, cx| {
        list.load_more_if_need(entities_count, visible_range.end, window, cx);
        visible_range
            .map(|ix| {
                let Some(entry) = rows_cache.get(ix) else { return div(); };
                div().children(match entry {
                    RowEntry::Entry(index) =>
                        Some(list.render_list_item(index, window, cx).into_any_element()),
                    RowEntry::SectionHeader(s) => /* 调 delegate.render_section_header */,
                    RowEntry::SectionFooter(s) => /* 调 delegate.render_section_footer */,
                })
            })
            .collect::<Vec<_>>()
    },
)
```

见 [crates/ui/src/list/list.rs:520-558](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list.rs#L520-L558)。注意那段源码注释特别说明：**`v_virtual_list` 这里不能加行间 gap**，因为 section 的 header/footer 即便返回 `None` 也会作为一个空子项占位，gap 会让排版错乱——这也是为什么 `List` 的间距由各 `RowEntry` 自身的内边距控制。

选中与点击的闭环在 `render_list_item`：它给每行套上 `div`，可选中时挂 `on_click`（点击即确认）与右键 `on_mouse_down`（记录右键项），并把 delegate 返回的 item 调 `.selected(..)` / `.secondary_selected(..)`（[crates/ui/src/list/list.rs:452-495](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list.rs#L452-L495)）。这要求 delegate 的 `type Item` 实现 `Selectable`（见 4.4）。

#### 4.3.4 代码实践

**目标**：体验 `List` 的键盘导航、分段与搜索。

**步骤**：

1. 运行 List 演示页：

   ```bash
   cargo run -- List
   ```

2. 用鼠标点选中某行，再按 `↓`/`↑` 移动选择，观察选中条如何跨段移动、空段被跳过。
3. 在顶部搜索框输入关键字，观察列表如何过滤、并自动滚回顶部。
4. 勾选 `Lazy Load` 复选框后滚到底部，观察 1 秒后新增 200 条数据。

**需要观察的现象 / 预期结果**：键盘导航跨段时不应选中 section header；搜索后会触发 `perform_search` 并 `scroll_to_item(0, Top)`（[crates/ui/src/list/list.rs:289-305](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list.rs#L289-L305)）；懒加载由 `load_more_if_need` 在可见区接近末尾时触发（阈值在 delegate 里被改成 150，见 [crates/story/src/stories/list_story.rs:314-316](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/list_story.rs#L314-L316)）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`List` 用 `RowsCache` 把分段数据拍平成一维，再交给 `VirtualList`。为什么不直接让 `VirtualList` 支持「分段」概念？

**答案**：职责分离。`VirtualList` 只关心「一串尺寸已知的项」，保持通用、可被 `Table` 等复用；分段、header/footer、键盘环绕导航这些「列表语义」属于上层，由 `List` + `RowsCache` 负责。拍平后用 `RowEntry` 枚举区分项的种类，是一层很干净的适配。

**练习 2**：`ListStory` 里有些分段 `items_count` 返回 0（[crates/story/src/stories/list_story.rs:198-205](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/list_story.rs#L198-L205)）。这些空段会显示成什么？

**答案**：什么都不显示。`RowsCache::prepare_if_needed` 对 `items_count == 0` 的段直接返回空、连 header/footer 都不放（[crates/ui/src/list/cache.rs:198-200](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/cache.rs#L198-L200)），键盘导航的 `prev`/`next` 也会跳过它们（有单元测试 `test_prev_next_with_empty_sections` 覆盖，[crates/ui/src/list/cache.rs:329-383](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/cache.rs#L329-L383)）。

### 4.4 ListDelegate：数据与渲染的契约

#### 4.4.1 概念说明

`ListDelegate` 是使用者要实现的 trait，它是 `List` 与你业务数据之间的契约（[crates/ui/src/list/delegate.rs:10-11](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/delegate.rs#L10-L11)）。`List` 不会问你要「全部数据」，而是按需回调几个方法：

- 必须实现：`items_count(section)`、`render_item(ix)`、`set_selected_index(ix)`；
- 可选覆盖：`sections_count`（默认 1）、`render_section_header/footer`、`perform_search`、`loading`/`has_more`/`load_more` 等。

它的关联类型 `type Item: Selectable + IntoElement` 约定了「每条数据对应的元素」必须可被选中、可渲染。

#### 4.4.2 核心流程

写一个 delegate 通常是这几步：

1. 定义承载业务数据的结构体（如 `CompanyListDelegate`），持有原始数据、当前选中下标、查询串等。
2. 实现 `items_count`：返回每段有多少条（可先在 `perform_search` 里把过滤结果写进 `matched_*` 字段，`items_count` 直接读它）。
3. 实现 `render_item`：按 `IndexPath` 取出一条数据，包成 `Self::Item`（注意调 `.selected(..)` 反映选中态）。
4. 实现 `set_selected_index`：保存选中下标并 `cx.notify()`，让列表重绘高亮。
5. 按需覆盖 `perform_search`（过滤）、`has_more`+`load_more`（懒加载）。

`ListState::new(delegate, window, cx)` 会构造状态实体；可选链式 `.searchable(true)`；最后 `List::new(&state)` 拿到无状态外壳放进你的视图。

#### 4.4.3 源码精读

trait 的三个必须方法签名（[crates/ui/src/list/delegate.rs:31-47](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/delegate.rs#L31-L47)）：

```rust
/// 返回某一段的条目数；为 0 的段整段（含 header/footer）都不渲染。
fn items_count(&self, section: usize, cx: &App) -> usize;

/// 渲染第 ix 条；返回 None 会跳过该项。
/// NOTE: 每条 item 应当等高。
fn render_item(
    &mut self, ix: IndexPath, window: &mut Window, cx: &mut Context<ListState<Self>>,
) -> Option<Self::Item>;

/// 记录选中下标（只存，不触发确认）。
fn set_selected_index(&mut self, ix: Option<IndexPath>, window: &mut Window, cx: &mut Context<ListState<Self>>);
```

懒加载三个方法（[crates/ui/src/list/delegate.rs:145-170](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/delegate.rs#L145-L170)）：`has_more` 默认 `false`（不开启）、`load_more_threshold` 默认 20（距底部少于 20 条时触发）、`load_more` 默认空。`ListStory` 把阈值调到 150 并在 `load_more` 里 spawn 一个后台任务延时 1 秒追加 200 条（[crates/story/src/stories/list_story.rs:318-338](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/list_story.rs#L318-L338)），演示了「滚动到底自动分页」。

真实 delegate 的 `render_item` 长这样（[crates/story/src/stories/list_story.rs:288-300](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/list_story.rs#L288-L300)）：

```rust
fn render_item(&mut self, ix: IndexPath, _, _, _) -> Option<Self::Item> {
    let selected = Some(ix) == self.selected_index || Some(ix) == self.confirmed_index;
    if let Some(company) = self.matched_companies[ix.section].get(ix.row) {
        return Some(CompanyListItem::new(ix, company.clone(), selected));
    }
    None
}
```

注意 `CompanyListItem` 实现了 `Selectable`（[crates/story/src/stories/list_story.rs:63-72](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/list_story.rs#L63-L72)），满足 `type Item: Selectable + IntoElement` 的约束；`ListState` 内部会在它上面再调一次 `.selected(..)` / `.secondary_selected(..)` 同步高亮态（[crates/ui/src/list/list.rs:471-474](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list.rs#L471-L474)）。

构造入口极简（[crates/story/src/stories/list_story.rs:383](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/list_story.rs#L383)）：

```rust
let company_list = cx.new(|cx| ListState::new(delegate, window, cx).searchable(true));
```

#### 4.4.4 代码实践

**目标**：写一个最小的 `ListDelegate`，渲染 10000 条等高数据，体会「实现三个方法就能得到一个能搜索、能键盘导航、能虚拟化的列表」。

**步骤**（以下为示例代码，需自行放入一个 Story 或 example 视图中编译运行）：

```rust
// 示例代码：最小 ListDelegate
use gpui::{IntoElement, Render, Window, div};
use gpui_component::{
    IndexPath, Label, Selectable, list::{List, ListDelegate, ListState},
};

// 1. 数据载体 + 选中态
struct CounterDelegate {
    items: Vec<String>,          // 10000 条
    selected: Option<IndexPath>,
}

// 每行元素：必须实现 Selectable + IntoElement
#[derive(IntoElement)]
struct CounterRow { ix: usize, selected: bool }
impl Selectable for CounterRow {
    fn selected(mut self, s: bool) -> Self { self.selected = s; self }
    fn is_selected(&self) -> bool { self.selected }
}
impl gpui::RenderOnce for CounterRow {
    fn render(self, _: &mut Window, _: &mut gpui::App) -> impl IntoElement {
        div().px_3().py_2().child(Label::new(format!("第 {} 项", self.ix)))
    }
}

impl ListDelegate for CounterDelegate {
    type Item = CounterRow;

    fn items_count(&self, _section: usize, _: &gpui::App) -> usize { self.items.len() }

    fn render_item(&mut self, ix: IndexPath, _: &mut Window, _: &mut gpui::Context<ListState<Self>>)
        -> Option<Self::Item> {
        Some(CounterRow { ix: ix.row, selected: Some(ix) == self.selected })
    }

    fn set_selected_index(&mut self, ix: Option<IndexPath>, _: &mut Window,
        cx: &mut gpui::Context<ListState<Self>>) {
        self.selected = ix;
        cx.notify();
    }
}

// 2. 在你的视图 new 里：
// let delegate = CounterDelegate { items: (0..10000).map(|i| format!("{}", i)).collect(), selected: None };
// let state = cx.new(|cx| ListState::new(delegate, window, cx).searchable(true));
// 3. render 里：List::new(&state)
```

**需要观察的现象 / 预期结果**：列表瞬间出现（不会因 10000 项而卡顿），滚动流畅，键盘上下可选，搜索框可过滤（需补 `perform_search`，否则搜索不会改变数据——这是「待本地验证」的点：默认 `perform_search` 是空实现，见 [crates/ui/src/list/delegate.rs:15-22](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/delegate.rs#L15-L22)）。

#### 4.4.5 小练习与答案

**练习 1**：上面的 `CounterDelegate` 没有实现 `perform_search`，在搜索框里输入文字会发生什么？

**答案**：输入会触发 `on_query_input_event`，但默认 `perform_search` 返回 `Task::ready(())` 且不改数据（[crates/ui/src/list/delegate.rs:15-22](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/delegate.rs#L15-L22)），所以 `items_count` 不变、列表内容不变，只是选中重置并滚回顶部（[crates/ui/src/list/list.rs:280-305](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list.rs#L280-L305)）。要让搜索生效，需像 `ListStory` 那样在 `perform_search` 里过滤数据并更新 `matched_*` 字段。

**练习 2**：为什么 `set_selected_index` 里要写 `cx.notify()`，而 `render_item` 里不用？

**答案**：`set_selected_index` 改变的是 delegate 自身持有的 `selected` 字段，属于跨帧状态变化，必须 `notify` 让 `ListState` 下一帧重绘、把新高亮反映出来。`render_item` 是每帧按需调用的纯渲染函数，本身就在重绘流程里，不需要也不会主动 notify。

## 5. 综合实践

把本讲三块串起来，做一个「虚拟化 vs 非虚拟化」的对照实验，亲手验证 4.1 的理论收益。

**任务**：在同一个 Story 视图里并排（或切换）展示两种实现，都渲染 10000 条数据：

1. **虚拟化版**：用 4.4 的 `CounterDelegate` + `List`，数据 10000 条。
2. **非虚拟化版**：用一个普通 `div().overflow_scroll()`，把 10000 个 `Label` 全部 `.child()` 进去（即把 `v_virtual_list` 换成一次性渲染全部）。

**操作步骤**：

1. 参照 `crates/story/src/stories/virtual_list_story.rs` 新增一个 Story（或修改本地副本），同时持有两套渲染逻辑，用按钮切换。
2. 运行 `cargo run -- <你的Story名>`，分别在两种模式下：
   - 用鼠标滚轮快速从头滚到底；
   - 观察初始打开到可交互的等待时间；
   - （可选）用 `samply record cargo run` 或 macOS 的 `MTL_HUD_ENABLED=1` 看帧率/CPU。

**需要观察的现象 / 预期结果**：

- 虚拟化版：10000 条几乎瞬间可用，滚动帧率稳定；`visible_range` 始终是几十的宽度。
- 非虚拟化版：首次布局明显变慢（要排 10000 个元素），滚动时帧率下降、内存占用更高。

**预期结论**：可见项数与总数据量解耦——这正是 `VirtualList` 在 4.1 算法上保证的。把数据量从 1 万调到 10 万、50 万，虚拟化版几乎无差别，而非虚拟化版会越来越卡。**待本地验证**：具体帧率与内存数值依机器而定，重点观察「随数据量增长的趋势差异」。

> 进阶：再给虚拟化版补上 `has_more` + `load_more`（参照 [crates/story/src/stories/list_story.rs:318-338](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/list_story.rs#L318-L338)），让它变成「滚动到底自动追加」的无限列表，体会懒加载与虚拟化的配合。

## 6. 本讲小结

- **虚拟化的本质**：每帧只渲染可见区间 `[first, last)` 内的几十项，其余项不进入元素树；可见区间由「前缀和 + 滚动偏移」的线性扫描求得（[crates/ui/src/virtual_list.rs:656-689](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L656-L689)）。
- **`VirtualList`** 是通用虚拟滚动 `Element`，支持横/纵双轴与异构尺寸，但要求调用方预先给出 `item_sizes`；跨轴尺寸由测第 0 项得到。
- **`List`/`ListState`** 是 `VirtualList` 之上的成品列表，额外提供分段、键盘导航、搜索、懒加载、事件，但假定全员等高；它通过 `RowsCache` 把分段数据拍平成一维喂给 `VirtualList`。
- **`ListDelegate`** 是使用者契约：实现 `items_count` / `render_item` / `set_selected_index` 三个必须方法即可，`type Item: Selectable + IntoElement`。
- **状态范式**：`List`（无状态 `RenderOnce` 外壳）+ `ListState`（有状态实体），与 `Input`/`InputState` 同构；选中态外置到 delegate 并靠 `cx.notify()` 闭环。
- **性能选型**：等高大数据用 `List`；不等高或更自由的虚拟化用 `VirtualList`；两者都把「可见项数」与「总数据量」解耦。

## 7. 下一步学习建议

- **下一讲 u7-l2（Table 与 DataTable）**：`Table` 同样复用 `VirtualList`，并把虚拟化从「行」扩展到「行 + 列」双向，并支持列宽调整。读完本讲再去读 `table.rs` 会非常顺。
- **延伸阅读源码**：`crates/ui/src/list/cache.rs` 的 `RowsCache`（拍平与导航）、`crates/ui/src/scroll/` 的 `Scrollbar` 与 `ScrollableElement`（`VirtualListStory` 里 `.scrollbar(&handle, axis)` 的来源）。
- **对比 GPUI 原语**：本讲的 `VirtualList` 文档注释里给了 Zed `uniform_list` 的链接（[crates/ui/src/virtual_list.rs:6-8](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/virtual_list.rs#L6-L8)），可对照阅读，理解 gpui-component 为支持「异构尺寸」做了哪些改造。
- **回到树形数据**：u7-l3 的 `Tree` 处理层级数据，但其展开/折叠后的渲染同样依赖本讲建立的「可见项」思维。
