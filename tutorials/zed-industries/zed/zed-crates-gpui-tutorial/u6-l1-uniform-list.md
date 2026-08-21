# 虚拟化列表入门：uniform_list

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解**等高虚拟化**的核心思想：列表可以有一百万行，但任意时刻只有「视口内 + 边界余量」的二三十行被真正实例化为元素。
2. 读懂 `uniform_list` 的三阶段实现：它如何只测量一个条目就推导出整个列表的几何信息，如何根据滚动偏移算出可见区间，如何用 `ContentMask` 裁掉越界部分。
3. 会用 `UniformListScrollHandle` 做程序化滚动：`scroll_to_item` 配合 `ScrollStrategy`，理解「延迟到下一帧 prepaint 才生效」的执行模型。
4. 掌握 `UniformListIter` 风格回调（`Fn(Range<usize>, &mut Window, &mut App) -> Vec<R>`）与 `cx.processor` 的配合——这是按需构建子项的标准写法。
5. 能独立完成本讲综合实践：用 `uniform_list` 渲染十万行数据并实现「跳转到指定行」，同时用可见区间统计验证内存中从未实例化全部行。

本讲是第 6 单元「高级 UI 模式」的第一讲，也是虚拟化系列（u6-l1 等高列表 → u6-l2 变高列表）的入口。

## 2. 前置知识

### 2.1 什么是虚拟化（virtualization），为什么需要它

回想 u3-l1 的心智模型：**元素树是立即模式的，每帧从根视图重建**。如果一个列表有一万行， naive 的做法是 `children` 里塞一万个 `div`——这意味着每帧要走完一万次 Taffy 布局、一万次 prepaint、一万次 paint，而用户其实只能看到屏幕里的二三十行。

虚拟化的思路一句话就能说清：

> **数据全量保留在实体里（一万个 `Rc<Quote>` 很便宜），元素只为可见的行创建（二三十个元素很便宜）。**

这需要回答三个问题：

1. 不布局全部行，怎么知道列表总高度（滚动条需要它）？——**等高列表**的答案是：测一行，乘以行数。
2. 怎么知道当前该渲染哪些行？——用「滚动偏移 ÷ 行高」做除法，算出可见区间。
3. 视口外的行画到哪里去了？——根本不创建；至于恰好跨在视口边缘的行，用 `ContentMask`（裁剪蒙版）把越界部分裁掉。

`uniform_list` 适用的前提写在它的模块文档第一行：**所有条目等高**。行高不一致时请用 u6-l2 的 `list` 元素。

### 2.2 本讲要用到的既有知识

- **元素三阶段**（u4-l1）：`request_layout`（向 Taffy 申报）→ `prepaint`（拿到最终 bounds、登记 hitbox）→ `paint`（提交绘制）。本讲的 `UniformList` 是一个完整的自定义 `Element` 实现，是 u4-l1 理论的绝佳复习材料——但它有一个特殊之处：它**绕过了 Taffy 的子树布局**，自己排布子项。
- **Interactivity 与滚动**（u5-l2）：`div` 的全部交互配置（id、样式补丁、监听器）汇聚于 `Interactivity` 结构体；滚轮滚动由 `Interactivity` 在 paint 阶段注册的监听器驱动，修改一个共享的 `scroll_offset`。`UniformList` 内部也持有一个 `Interactivity`，滚动机制完全复用。
- **焦点与 id**（u5-l5、u5-l2）：列表条目要可点击、可悬停，必须 `.id(ix)`——跨帧状态以 `(GlobalElementId, TypeId)` 为键存取；键盘导航（如 j/k 选行）则依赖 `track_focus` 与自定义 action。
- **`Rc<RefCell<T>>` 句柄模式**（u2-l2）：`UniformListScrollHandle` 与 `Entity`/`ScrollHandle` 同构——句柄只是共享可变状态的引用计数指针，读写经 `borrow`/`borrow_mut` 完成。

### 2.3 一个容易混淆的概念：`ScrollHandle` 与 `UniformListScrollHandle`

GPUI 有两个滚动句柄，本讲都会碰到：

| 句柄 | 定义位置 | 职责 |
| --- | --- | --- |
| `ScrollHandle` | `src/elements/div.rs` | 通用滚动状态：偏移量、列表 bounds、最大偏移等，供 `div` 的 `overflow_scroll` 使用 |
| `UniformListScrollHandle` | `src/elements/uniform_list.rs` | 包装一个 `base_handle: ScrollHandle`，再叠加虚拟化特有状态：待执行的 `scroll_to_item` 请求、上次布局记录的条目尺寸、是否垂直翻转 |

后者是前者的**组合而非继承**——Rust 惯用的组合模式。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/elements/uniform_list.rs` | 等高虚拟列表的完整实现（约 870 行，含测试） | 构造函数、`Element` 三阶段、`UniformListScrollHandle`、装饰协议 |
| `examples/uniform_list.rs` | 最小可运行示例（50 行） | `uniform_list` + `cx.processor` + `.id(ix)` 的标准写法 |
| `examples/data_table.rs` | 万行数据表演示 | `TOTAL_ITEMS = 10000`、`Rc<Quote>` 共享数据、`visible_range` 可视化、自制滚动条 |
| `src/elements/div.rs` | `ScrollHandle` 定义与 `Interactivity` 滚动机制 | `offset`/`set_offset`/`max_offset`；滚轮事件如何写进共享偏移 |
| `src/elements/list.rs` | 变高列表（u6-l2 主角） | 本讲只借用 `ListSizingBehavior`、`ListHorizontalSizingBehavior` 两个枚举 |
| `src/app/context.rs` | `Context<T>` 的方法 | `cx.processor`：把视图方法适配成列表回调 |

## 4. 核心概念与源码讲解

### 4.1 UniformList：等高虚拟化元素

#### 4.1.1 概念说明

`UniformList` 是 GPUI 官方提供的等高虚拟列表元素。它的模块文档直白地说明了性能取舍：

> Rather than use the full taffy layout system, uniform_list simply measures the first element and then lays out all remaining elements in a line based on that measurement. This is much faster than the full layout system, but only works for elements with uniform height.

（不再走完整的 Taffy 布局系统，uniform_list 只测量第一个元素，然后按该测量值把其余元素排成一条线。这比完整布局系统快得多，但只适用于等高元素。）

这句话是理解本讲的钥匙：

- **测量一次**：整个列表只需测量 1 个条目，就得到行高 `item_height` 与代表性行宽。
- **算术代替布局**：第 `ix` 行的位置是算出来的（`origin.y + item_height * ix`），不是 Taffy 排出来的。第 ix 行的布局约束也是算出来的（`AvailableSpace::Definite(item_height)`）。
- **总量也用算术**：列表内容总高度 = `item_height * item_count`，滚动条和 overscroll 钳制都基于它。

#### 4.1.2 核心流程

一帧之内 `UniformList` 的完整旅程（承接 u4-l1 的三阶段）：

```text
request_layout 阶段
  1. measure_item：渲染回调临时构建 1 个测量条目，
     以 (MaxContent 宽, MinContent 高) 为约束测出 item_size
  2. 向 Interactivity 申报自身样式：
     - Infer 模式：内容高度 = item_size.height * item_count（供父容器推断尺寸）
     - Auto 模式（默认）：跟随父容器分配的尺寸（示例里的 .h_full()/.size_full()）

prepaint 阶段
  3. 计算去掉边框内边距后的 padded_bounds
  4. 重新测量条目 → content_size = (最宽条目宽, item_height * item_count)
  5. 取走 scroll_handle 中积压的 deferred_scroll_to_item 请求（若有），
     按策略直接改写共享 scroll_offset
  6. overscroll 钳制：滚动越界则拉回边界
  7. 算出可见区间：
     first = floor( (滚动距离 - padding.top) / item_height )
     last  = ceil ( (滚动距离 + 视口高) / item_height )
  8. 用渲染回调只为 visible_range 构建元素
  9. 逐条 layout_as_root + prepaint_at：
     位置 = padded_bounds.origin + scroll_offset + (0, item_height * ix)
  10. 在 ContentMask(bounds) 内同样处理装饰元素

paint 阶段
  11. 逐条 paint 可见条目，再 paint 装饰
```

可见区间的数学（行内公式）：

滚动偏移约定为**负值向下**（内容整体上移），因此「向下滚动距离」\( s = -\text{offset}_y \ge 0 \)。设行高 \( h \)、视口高 \( V \)、顶部内边距 \( p \)，则：

\[ \text{first} = \left\lfloor \frac{s - p}{h} \right\rfloor, \qquad \text{last} = \left\lceil \frac{s + V}{h} \right\rceil, \qquad \text{visible} = \text{first} \,..\, \min(\text{last}, \text{item\_count}) \]

注意 `last` 用的是 `ceil`：一个只露出半个身子的行也必须渲染，否则视口底部会出现空洞。`first` 用 `floor` 同理（顶部露头的行也要画，被 ContentMask 裁掉超出部分）。

#### 4.1.3 源码精读

**入口函数 `uniform_list`**——三个参数决定了列表的一切：[src/elements/uniform_list.rs:21-55](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L21-L55)

这段代码做了三件事：把 `id` 转成 `ElementId`（虚拟列表必须有 id，它的跨帧滚动状态靠 id 定位）；在基础样式里默认写入 `overflow.y = Scroll`（第 31-32 行，所以列表天然可滚）；把用户的渲染函数 `f` 包装成返回 `SmallVec<AnyElement>` 的 `render_range` 闭包（第 34-39 行）。注意第三个参数的签名——这就是「UniformListIter 回调」：

```rust
f: impl 'static + Fn(Range<usize>, &mut Window, &mut App) -> Vec<R>
```

框架把**要渲染的行号区间**递给你，你返回**这些行对应的元素**。`item_count` 只是一个数字，数据本身仍在你的实体里，回调里按 `range` 索引取用。

**`UniformList` 结构体**：[src/elements/uniform_list.rs:57-69](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L57-L69)

关键字段：`item_count`（总行数）、`item_to_measure_index`（用哪一行做测量代表，默认第 0 行）、`render_items`（类型擦除后的渲染回调，内联容量 64 的 `SmallVec` 意味着多数帧零堆分配）、`interactivity`（复用 u5-l2 的交互机制）、`scroll_handle`（可选的外部滚动句柄）。

**request_layout：一次测量，算出总高**：[src/elements/uniform_list.rs:275-329](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L275-L329)

第 283 行调用 `measure_item`（下文详述）拿到 `item_size`；`ListSizingBehavior::Infer` 分支里，测量闭包用一行乘法回答了「列表想要多高」：`desired_height = item_size.height * max_items`（第 295 行），并和外层给定的确定高度取小。`ListSizingBehavior` 定义在 [src/elements/list.rs:188-194](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L188-L194)：`Infer` 表示「按内容推尺寸」，`Auto`（默认）表示「父容器给多大我用多大」。默认 `Auto` 模式下列表高度完全取决于父容器，这正是构造函数文档第 18-20 行要求「渲染进一个 overflow-y: hidden 且有固定（或最大）高度的容器」的原因——示例代码里的 `.h_full()`、`.size_full()` 就是在满足这个前提。

**measure_item：只为一行付出布局成本**：[src/elements/uniform_list.rs:658-680](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L658-L680)

它让渲染回调临时构建第 `item_to_measure_index` 行（用 `min` 防越界），以 `(MaxContent 或给定宽, MinContent 高)` 为约束调用 `layout_as_root`，返回测量尺寸。`with_width_from_item`（[src/elements/uniform_list.rs:622-625](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L622-L625)）可以换一行做「代表」——名字强调 width，因为等高列表里行高人人相同，而宽度可能因内容长短而异（比如最长的用户名决定列表宽度）。一个值得注意的细节：`request_layout`（第 283 行）和 `prepaint`（第 359 行）各调用一次 `measure_item`，即每帧会额外构建并布局一个「测量专用条目」——这是用一次小布局换取万行免布局的代价。

**prepaint：可见区间的诞生地**：[src/elements/uniform_list.rs:396-511](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L396-L511)

前面几步算几何：`compute_style` 合成样式、扣除 border 与 padding 得到 `padded_bounds`（第 340-352 行），内容总尺寸 `content_size.height = longest_item_size.height * self.item_count`（第 365-368 行）。随后是本讲最核心的两段：

可见区间计算（第 473-480 行）：

```rust
let first_visible_element_ix =
    (-(scroll_offset.y + padding.top) / item_height).floor() as usize;
let last_visible_element_ix = ((-scroll_offset.y + padded_bounds.size.height)
    / item_height)
    .ceil() as usize;
let visible_range = first_visible_element_ix
    ..cmp::min(last_visible_element_ix, self.item_count);
```

对照 4.1.2 的公式逐项读：`-scroll_offset.y` 就是滚动距离 \( s \)；`floor`/`ceil` 保证半露的行也算可见；`min` 防止滚到列表末尾后区间越界。

条目排布（第 492-511 行，在 `window.with_content_mask(Some(content_mask), ...)` 内执行）：

```rust
let item_origin = padded_bounds.origin
    + scroll_offset
    + point(Pixels::ZERO, item_height * ix);
let available_space = size(
    AvailableSpace::Definite(available_width),
    AvailableSpace::Definite(item_height),
);
item.layout_as_root(available_space, window, cx);
item.prepaint_at(item_origin, window, cx);
```

第 `ix` 行的纵向位置是纯算术 `item_height * ix` 加上当前滚动偏移；布局约束直接钉死行高为 `Definite(item_height)`——条目内部的 Taffy 子树照常布局，但**行与行之间不再经过 Taffy**。`ContentMask { bounds }`（第 492 行）保证顶部/底部露头的行只画出视口内的部分。

**paint：收尾**：[src/elements/uniform_list.rs:541-567](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L541-L567)

把 prepaint 存进 `frame_state.items` 的可见条目逐个 paint，再 paint 装饰元素。注意 paint 阶段不做任何计算——可见性决策全部在 prepaint 完成。

**标准用法示例**：[examples/uniform_list.rs:11-39](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/uniform_list.rs#L11-L39)

这个 50 行的示例是最佳起点：`cx.processor` 把视图方法适配成列表回调（下一小节详述），每行 `div().id(ix)` 拿到 id 才能挂 `on_click`（u5-l2 的规则：跨帧交互状态需要 id），`.h_full()` 给列表确定高度。

#### 4.1.4 代码实践：亲眼看到「只渲染可见行」

**实践目标**：运行万行数据表示例，通过界面标签直接观察可见区间的变化，建立虚拟化的直觉。

**操作步骤**：

1. 运行 data_table 示例（它内置 `TOTAL_ITEMS: usize = 10000`，见 [examples/data_table.rs:12](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/data_table.rs#L12)）：

   ```bash
   cargo run -p gpui --example data_table
   ```

2. 观察窗口顶部标签：`Total 10000 items, visible range: 0..N`。这行文字来自 [examples/data_table.rs:387-391](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/data_table.rs#L387-L391)，而 `visible_range` 是渲染回调每帧顺手记录的（第 432-441 行：`this.visible_range = range.clone()`）。

3. 上下滚动列表，观察 `visible_range` 的变化；再用鼠标按住右侧自制滚动条（灰色圆角小块）拖动，观察区间大幅跳动。

**需要观察的现象**：无论怎么滚，`range` 的长度始终只有几十（约等于视口高 ÷ 行高 + 1），而 `Total` 恒为 10000。

**预期结果**：数据有一万行，元素永远只有几十个——这就是虚拟化。窗口高度不同 N 值会不同，具体数值**待本地验证**（取决于窗口尺寸与缩放系数）。

#### 4.1.5 小练习与答案

**练习 1**：列表共 1000 行、行高 20px、视口高 300px。向下滚动 45px 后，可见区间是多少？

**答案**：滚动距离 \( s = 45 \)。first = ⌊45/20⌋ = 2，last = ⌈(45+300)/20⌉ = ⌈17.25⌉ = 18，所以可见区间是 `2..18`，共 16 行。第 2 行只露出 5px（40~45px 被滚出视口），第 17 行只露出 340~360 中的 15px——两行都是「半露头」，由 ContentMask 裁剪。

**练习 2**：为什么 `last_visible_element_ix` 用 `ceil` 而 `first_visible_element_ix` 用 `floor`？如果都改成 `round` 会怎样？

**答案**：视口边界上的行可能只露出一部分。`floor` 保证顶部任何与视口相交的行都被创建，`ceil` 保证底部同理。改成 `round` 后，露出不足一半的行不会被创建，视口顶部/底部会出现一条没画出来的「空洞」；由于这些行连元素都不存在，hitbox 也无从注册，空洞区域还会丢失点击与悬停。

**练习 3**：`measure_item` 每帧会被调用几次？每次调用发生了什么？

**答案**：两次——`request_layout`（[uniform_list.rs:283](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L283)）一次、`prepaint`（第 359 行）一次。每次都会让渲染回调额外构建一个「测量条目」并对它做一次完整的 `layout_as_root`。这是等高虚拟化的固定开销：每帧 2 次小布局，换取其余 999 行的零布局。

### 4.2 UniformListScrollHandle：滚动状态与程序化滚动

#### 4.2.1 概念说明

`UniformListScrollHandle` 是「视图与列表之间的滚动契约」，文档注释写明用法：**把它存在你的视图里，每帧经 `.track_scroll(&handle)` 传给 uniform_list**（[src/elements/uniform_list.rs:77-80](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L77-L80)）。

它内部是 `Rc<RefCell<UniformListScrollState>>`，状态三件套（[src/elements/uniform_list.rs:113-122](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L113-L122)）：

| 字段 | 作用 |
| --- | --- |
| `base_handle: ScrollHandle` | 通用滚动状态（当前偏移、bounds、最大偏移），滚轮滚动直接写它 |
| `deferred_scroll_to_item` | 挂起的程序化滚动请求（本模块的核心设计） |
| `last_item_size` | 上次布局记录的 `ItemSize`（视口尺寸 item + 内容尺寸 contents），供 `is_scrollable` 等查询 |
| `y_flipped` | 列表是否垂直翻转（第 0 行显示在最底部，聊天界面用） |

**为什么程序化滚动要「延迟」**？因为 `scroll_to_item(ix, strategy)` 被调用时（往往在 action 处理器里），列表此刻并不知道行高——行高是 prepaint 阶段测量出来的。所以句柄只把请求存进 `deferred_scroll_to_item`，等下一次 prepaint 由列表自己换算成像素偏移。这个「记录意图、下帧兑现」的模式贯穿 GPUI（对照 u5-l2 的 pending click、u5-l4 的 PendingInput）。

#### 4.2.2 核心流程

一次 `scroll_to_item` 的完整生命周期：

```text
任意时刻（如 action 处理器、按钮回调）
  scroll_to_item(ix, strategy)
    └─ 仅写入 deferred_scroll_to_item = Some(DeferredScrollToItem { .. })

下一帧 prepaint（uniform_list.rs:372-379）
  handle.last_item_size = 本次布局的 ItemSize   ← 顺手记录
  deferred_scroll_to_item.take()                 ← 取走并清空请求

同一次 prepaint（uniform_list.rs:415-471）
  计算 item_top = item_height * ix、item_bottom = item_top + item_height
  判断目标行是否已在视口内（is_above / is_below）
  非严格模式且行可见 → 不滚动
  否则按策略写入共享 scroll_offset：
    Top    → offset.y = -(item_top - offset_px).clamp(0, max)
    Center → offset.y = -(item_center - viewport_center).clamp(0, max)
    Bottom → offset.y = -(item_bottom - V).clamp(0, max)
    Nearest → 先归约为 Top（目标在上方）或 Bottom（目标在下方）
  本帧随后的可见区间计算直接用新 offset → 滚动立即生效
```

`ScrollStrategy` 四种策略（[src/elements/uniform_list.rs:83-99](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L83-L99)）：`Top` 把目标行贴到视口顶、`Bottom` 贴到视口底、`Center` 居中（上方行数不够时取最近可达位置）、`Nearest` 只在目标不可见时以最小滚动量露出来。严格（strict）与非严格（non-strict）的区别：非严格模式下行已可见就不动；严格模式（`scroll_to_item_strict`）无视可见性，强制对齐到策略位置。带 `offset` 的变体（`scroll_to_item_with_offset` 等）先把视口从对应边缘「缩进」若干行再套用策略，适合给固定表头、底部输入框让位。

#### 4.2.3 源码精读

**句柄与状态定义**：[src/elements/uniform_list.rs:77-80](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L77-L80)、[src/elements/uniform_list.rs:113-122](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L113-L122)

`pub struct UniformListScrollHandle(pub Rc<RefCell<UniformListScrollState>>)`——字段是 pub 的，data_table 示例就是直接 `self.scroll_handle.0.borrow().base_handle.bounds()` 取底层句柄的（[examples/data_table.rs:278-294](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/data_table.rs#L278-L294)）。

**一族滚动 API**：[src/elements/uniform_list.rs:145-213](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L145-L213)

`scroll_to_item`（第 150-157 行）只做一件事：借出状态、写入 `DeferredScrollToItem { item_index, strategy, offset: 0, scroll_strict: false }`。`scroll_to_item_strict`（第 163-170 行）仅 `scroll_strict: true` 之差；`scroll_to_item_with_offset` / `scroll_to_item_strict_with_offset`（第 172-213 行）再叠加缩进行数。查询类 API 也在此：`is_scrollable`（第 230-237 行，比较 `last_item_size` 的内容高与视口高）、`is_scrolled_to_end`（第 239-249 行，`-offset.y >= max_offset.y`，不可滚时返回 `None`）、`scroll_to_bottom`（第 251-254 行，巧妙地复用 `scroll_to_item(usize::MAX, Bottom)`——巨大的行号经 clamp 后必然落在最大偏移处）。

**prepaint 兑现请求**：[src/elements/uniform_list.rs:372-379](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L372-L379)

这一小段是「延迟兑现」的取件口：可变借出句柄状态，先记录 `last_item_size`（视口尺寸 `padded_bounds.size` + 内容尺寸 `content_size`），再 `take()` 走挂起的请求。

**策略计算**：[src/elements/uniform_list.rs:415-471](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L415-L471)

第 427-434 行先算出目标行的顶/底坐标与可见性判定：`is_above = item_top < scroll_top + offset_pixels`、`is_below = item_bottom > scroll_top + list_height`。第 436-468 行是策略主体，注意所有写入都经过 `.clamp(Pixels::ZERO, max_scroll_offset)`，其中 `max_scroll_offset = (content_height - list_height).max(0)`（第 445-446 行）——目标行超出列表末尾时（比如 `usize::MAX`）会自然钳到列表底部。写入完成后第 470 行 `scroll_offset = *updated_scroll_offset`，让**同一帧**紧接着的可见区间计算（第 473 行起）就使用新偏移。

**track_scroll：一根线接两头**：[src/elements/uniform_list.rs:682-687](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L682-L687)

```rust
pub fn track_scroll(mut self, handle: &UniformListScrollHandle) -> Self {
    self.interactivity.tracked_scroll_handle = Some(handle.0.borrow().base_handle.clone());
    self.scroll_handle = Some(handle.clone());
    self
}
```

两行赋值各接一头：`base_handle` 交给 `Interactivity`，于是**滚轮/触摸板滚动**由 div.rs 的既有机制驱动——paint 阶段注册的 `ScrollWheelEvent` 监听器把像素增量写进共享 `scroll_offset`（[src/elements/div.rs:3195-3207](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L3195-L3207)），prepaint 阶段再与 tracked handle 的状态互相同步（[src/elements/div.rs:2330-2339](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L2330-L2339)）；`scroll_handle` 整体留给 UniformList 自己，于是**程序化滚动与 last_item_size 记录**得以工作。不调用 `track_scroll` 时列表仍可显示，但句柄读不到正确的偏移、也无法程序化滚动。

**cx.processor：回调拿到视图状态的关键**：[src/app/context.rs:264-272](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app/context.rs#L264-L272)

列表回调的第三参数是 `&mut App`——裸上下文摸不到你的视图实体。`cx.processor` 捕获 `self.entity()`，把 `Fn(&mut T, E, &mut Window, &mut Context<T>) -> R` 适配成 `Fn(E, &mut Window, &mut App) -> R`：每次回调先 `view.update(...)` 借出视图再执行闭包。data_table 的回调正是借此直接读 `this.quotes.get(i)` 并顺手写 `this.visible_range`（[examples/data_table.rs:432-441](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/data_table.rs#L432-L441)）。它与 u2-l3 介绍的 `cx.listener` 是同一个思想在不同签名上的应用。

#### 4.2.4 代码实践：四种滚动策略对比器

**实践目标**：体感理解 `ScrollStrategy` 的差异与「非严格模式行已可见则不滚」的语义。

**操作步骤**（示例代码，基于 examples/uniform_list.rs 改写；也可以直接复制该示例到 examples/ 下新建文件后修改，但注意新文件需在 Cargo.toml 注册 `[[example]]` 才能被 `cargo run --example` 找到，更省事的做法是直接暂改原示例文件）：

1. 给视图加一个 `UniformListScrollHandle` 字段和一个目标行号字段：

   ```rust
   // 示例代码
   struct UniformListExample {
       scroll_handle: UniformListScrollHandle,
   }
   ```

2. 在列表上方加四个按钮，分别调用不同策略滚动到第 40 行（列表共 50 行、视口约显示十几行）：

   ```rust
   // 示例代码
   div().flex().flex_row().gap_2().child(
       div().id("top").child("Top").on_click(cx.listener(|this, _, _, _| {
           this.scroll_handle.scroll_to_item_strict(40, ScrollStrategy::Top);
       })),
   )
   // Bottom / Center / Nearest 同理，各改一个 id、一个策略
   ```

3. 列表本体挂上句柄：`uniform_list(...).track_scroll(&self.scroll_handle).h_full()`。

4. 先手动滚到列表中部，再依次点四个按钮。

**需要观察的现象**：`Top` 把第 40 行贴到视口顶端；`Bottom` 贴到底端；`Center` 尽量居中（注意它上方行数不足时贴顶）；`Nearest` 在第 40 行已可见时**完全不滚动**，不可见时只滚最小距离。

**预期结果**：与 4.2.2 的公式一致。点击后滚动发生在下一帧，人眼无感知延迟。具体呈现**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：在按钮回调里调用 `scroll_to_item(5, Top)` 之后立刻读 `scroll_handle.0.borrow().base_handle.offset()`，能读到新偏移吗？

**答案**：不能。`scroll_to_item` 只写入 `deferred_scroll_to_item`，真正的偏移换算发生在**下一次 prepaint**（uniform_list.rs 第 415-471 行），而且写的是共享 `scroll_offset`，由 Interactivity 的 prepaint（div.rs 第 2330 行起）同步回 base_handle。回调时读到的是旧值。想拿到换算结果，最早也要到下一帧。

**练习 2**：`is_scrolled_to_end` 为什么返回 `Option<bool>` 而不是 `bool`？它依赖句柄里的哪个字段？

**答案**：因为「滚到底」对不可滚动的列表没有意义——内容不超屏时返回 `None` 表示问题本身不成立，调用方必须处理这个第三态。判定用 `base_handle.max_offset().y <= 0` 区分可滚与否，而 max_offset 的正确性依赖 prepaint 每帧记录的 `last_item_size`（视口与内容尺寸），这也再次说明句柄必须每帧经 `track_scroll` 传给列表，否则这些查询都是过期数据。

**练习 3**：翻阅官方测试 `test_scroll_strategy_nearest`（[src/elements/uniform_list.rs:724-864](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L724-L864)）：47 行、行高 20px、视口 200px。首次 `SelectNext` 走到第 10 行时，断言的可见区间 `1..11` 是怎么算出来的？

**答案**：前 9 次选择第 1~9 行，行都在初始视口内（`Nearest` 非严格模式 → 不滚，区间保持 `0..10`）。选到第 10 行时：item_top = 200、item_bottom = 220 > scroll_top + 200 → `is_below` 成立，Nearest 归约为 Bottom；offset.y = -(220 - 200) = -20。于是 first = ⌊20/20⌋ = 1，last = ⌈(20+200)/20⌉ = 11，区间 `1..11`——正好露出行 1 到行 10，共 10 行，与断言 `ix - 9..ix + 1` 相符。

### 4.3 视口裁剪：ContentMask、overscroll 钳制与装饰

#### 4.3.1 概念说明

虚拟化有三个「边界问题」要处理，本模块逐一拆解：

1. **滚过头怎么办**——用户拖自制滚动条或惯性滚动可能让偏移超出内容范围，需要钳制（clamping）。
2. **半露的行画到哪**——位置由算术排定，超出视口的部分交给 `ContentMask` 裁剪，GPU 层面裁掉蒙版外的像素。
3. **跨行的视觉元素怎么办**——选中高亮、缩进参考线这类东西不属于任何一行，却要跟着滚动走。`UniformListDecoration` 协议就是为它们准备的。

另外还有一个容易忽视的点：`UniformList` 是 `InteractiveElement`（[src/elements/uniform_list.rs:714-718](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L714-L718)），所以 `div` 上的样式与交互方法（`on_click`、`key_context`、`track_focus` 等）它全都可用——虚拟列表同时也是一个「大 div」。

#### 4.3.2 核心流程

overscroll 钳制的数学（[src/elements/uniform_list.rs:396-413](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L396-L413)）：

纵向可滚的前提是内容高于视口。设视口高 \( V \)、内容高 \( C \)（\( C > V \)），则偏移 \( \text{offset}_y \) 的合法区间是：

\[ -(C - V) \;\le\; \text{offset}_y \;\le\; 0 \]

代码里的写法是 `max_scroll_offset = padded_bounds.size.height - content_height`（一个负数，第 400 行），当 `scroll_offset.y < max_scroll_offset`（滚过头）时直接改写共享偏移并钳回边界（第 402-405 行）。注意它同时写 `shared_scroll_offset`（跨帧共享、供鼠标监听器累计）与本地 `scroll_offset`（本帧计算用）两处。横向同理（第 407-413 行），仅在 `ListHorizontalSizingBehavior::Unconstrained`（行宽可超列表宽，定义见 [src/elements/list.rs:218-224](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L218-L224)）时生效。

装饰（decoration）的执行模型：prepaint 在排完可见条目后，对每个装饰调用 `compute(visible_range, bounds, scroll_offset, item_height, item_count, ...)`（第 513-532 行），拿到一个 `AnyElement` 后以整个列表视口为约束 `layout_as_root` 并 `prepaint_at(bounds.origin)`。装饰元素通常内部用 `absolute()` 定位 + `top(item_height * 选中行 - 滚动量)` 画出跟随滚动的高亮。

#### 4.3.3 源码精读

**ContentMask 包住排布全程**：[src/elements/uniform_list.rs:492-511](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L492-L511)

`let content_mask = ContentMask { bounds };` 后紧跟 `window.with_content_mask(Some(content_mask), |window| { ... })`——条目的布局、prepaint 全在蒙版内执行。`bounds` 是列表自身（未含滚动偏移）的边界，因此滚出视口的行部分（首行上方、末行下方）被裁掉。这与 u4-l3 讲过的 `Scene`/蒙版机制是同一套。

**装饰协议**：[src/elements/uniform_list.rs:578-593](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L578-L593)

```rust
pub trait UniformListDecoration {
    fn compute(
        &self,
        visible_range: Range<usize>,
        bounds: Bounds<Pixels>,
        scroll_offset: Point<Pixels>,
        item_height: Pixels,
        item_count: usize,
        window: &mut Window,
        cx: &mut App,
    ) -> AnyElement;
}
```

协议把虚拟列表的全部几何事实一次性递给实现者。紧接着的 blanket impl（[src/elements/uniform_list.rs:595-618](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L595-L618)）让 `Entity<T: UniformListDecoration>` 直接可作为装饰传入——装饰自身是实体，可以有自己的跨帧状态（比如当前选中行），`compute` 里经 `self.update` 借出。挂接方式是链式方法 `.with_decoration(decoration)`（第 652-656 行）。

**data_table 的自制滚动条**（装饰思想的姊妹实践）：[examples/data_table.rs:296-374](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/data_table.rs#L296-374)

GPUI 当前不内置滚动条，这个示例手搓了一个：滚块位置由 `-scroll_top / scroll_height` 百分比算出（第 304-308 行），拖动时反解百分比写回 `scroll_handle.set_offset`（第 367 行，`ScrollHandle::set_offset` 定义于 [src/elements/div.rs:4199-4202](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L4199-L4202)），再 `cx.notify` 触发下一帧。它是「读偏移 → 写偏移 → 重绘」闭环的绝佳范例，其中所有几何读数都来自 `UniformListScrollState` 的 `base_handle` 与 `last_item_size`。

**y_flipped：倒序聊天列表**（顺带一提）：[src/elements/uniform_list.rs:482-490](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L482-L490) 与 [src/elements/uniform_list.rs:689-711](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/uniform_list.rs#L689-L711)

`.y_flipped(true)` 让第 0 行出现在底部（聊天/日志场景）。实现上先做区间镜像（`item_count - range.end .. item_count - range.start`）再 `reverse()` 渲染顺序，`scroll_to_item` 的目标行号也随之镜像（第 422-424 行），切换翻转时还会换算保留当前偏移（第 696-707 行）。初学阶段了解即可。

#### 4.3.4 代码实践：实现一个选中行高亮装饰

**实践目标**：动手实现 `UniformListDecoration`，在指定行下方铺一层半透明高亮，验证「装饰跟随滚动」。

**操作步骤**（示例代码）：

1. 定义一个装饰实体，持有选中行号：

   ```rust
   // 示例代码
   struct RowHighlight {
       selected: usize,
       handle: UniformListScrollHandle,
   }

   impl UniformListDecoration for RowHighlight {
       fn compute(
           &self,
           visible_range: Range<usize>,
           bounds: Bounds<Pixels>,
           scroll_offset: Point<Pixels>,
           item_height: Pixels,
           _item_count: usize,
           _window: &mut Window,
           _cx: &mut App,
       ) -> AnyElement {
           if !visible_range.contains(&self.selected) {
               return Empty.into_any_element(); // 选中行不可见时无需绘制
           }
           let top = item_height * self.selected + scroll_offset.y;
           div()
               .absolute()
               .top(top)
               .left_0()
               .w_full()
               .h(item_height)
               .bg(rgb(0x3B82F6).opacity(0.2f32))
               .into_any_element()
       }
   }
   ```

2. 用 `cx.new(|_| RowHighlight { selected: 7, handle: handle.clone() })` 创建装饰实体，挂到列表上：

   ```rust
   // 示例代码
   uniform_list("entries", 50, cx.processor(...))
       .track_scroll(&self.scroll_handle)
       .with_decoration(highlight_entity)
       .h_full()
   ```

3. 运行示例，上下滚动列表。

**需要观察的现象**：高亮条随内容一起滚动（因为 `top` 加了 `scroll_offset.y`），滚出视口后高亮消失（`visible_range` 不再包含选中行），滚回来重新出现。

**预期结果**：高亮精确覆盖第 7 行整行。若发现高亮「贴」在某处不动，多半是忘了加 `scroll_offset.y`——这是最常见错误。完整效果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么装饰的 `compute` 要传入 `item_height` 和 `item_count`，而不是让装饰自己去测量？

**答案**：装饰的定位完全依赖列表的几何事实，而这些事实是列表在 prepaint 里刚测出来的（行高来自 `measure_item`，总数来自构造参数）。传参既避免了重复测量，也保证装饰与条目使用**同一帧**的数值——如果装饰自己测，两次测量的时机不同可能出现行高不一致，高亮就会错位。`item_count` 则用于 y_flipped 场景下行号镜像等计算。

**练习 2**：overscroll 钳制代码同时写了 `shared_scroll_offset.borrow_mut().y` 和本地 `scroll_offset.y`（第 402-405 行），为什么两处都要写？

**答案**：`shared_scroll_offset` 是 `Interactivity` 持有的跨帧共享偏移（`Rc<RefCell<Point<Pixels>>>`），滚轮监听器在它上面累计增量，只写本地变量的话下一次滚轮滚动会从「滚过头的老值」继续累加，弹回越界；本地 `scroll_offset` 则是本帧后续计算（deferred 滚动、可见区间、条目排布）的直接输入，只写共享格的话本帧仍用越界值排布。两处同步才能既修正历史又管好当下。

**练习 3**：对比 `uniform_list` 与朴素 `div().flex_col().children(一万行)`：滚动一帧内，两者各自要布局多少个元素？

**答案**：朴素 div 每帧布局全部一万行（Taffy 全量参与）；uniform_list 布局「可见行数 + 2 个测量条目」（request_layout 与 prepaint 各测一次），与总行数无关。这正是模块文档「只测第一个元素、其余排成一条线」的含义——滚动性能与数据规模解耦。

## 5. 综合实践

**任务：十万行目录 + 跳转到指定行 + 虚拟化验证**。把本讲三个模块串起来：大数据量（4.1）、程序化滚动（4.2）、边界与验证（4.3）。

要求实现：

1. 列表数据 100_000 行（行内容用行号生成即可，如 `Line #123456`），行高固定（给条目 `div().h(px(24.0))` 最稳，避免字体测量差异导致行高不齐）。
2. 视图持有 `UniformListScrollHandle`，列表 `.track_scroll(&handle)`。
3. 顶部一行控件：一个显示「当前可见区间 / 总行数 / 可见行数」的标签，加一个「跳到第 99999 行」按钮（`scroll_to_item_strict(99999, ScrollStrategy::Bottom)`）和一个「回顶部」按钮（`scroll_to_item(0, ScrollStrategy::Top)`）。
4. 渲染回调里把 `range.clone()` 存进视图字段（data_table 第 433 行的做法），标签即实时反映虚拟化状态。

骨架参考（示例代码，基于 examples/uniform_list.rs 扩写）：

```rust
// 示例代码
struct HugeList {
    total: usize,                       // 100_000
    visible_range: Range<usize>,        // 渲染回调每帧更新
    scroll_handle: UniformListScrollHandle,
}

impl Render for HugeList {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        div().size_full().flex().flex_col().child(
            div().flex().flex_row().gap_2().child(format!(
                "total {}, visible {:?} ({} rows)",
                self.total,
                self.visible_range,
                self.visible_range.len()
            ))
            .child(
                div().id("jump")
                    .child("跳到 99999")
                    .on_click(cx.listener(|this, _, _, _| {
                        this.scroll_handle
                            .scroll_to_item_strict(99999, ScrollStrategy::Bottom);
                    })),
            ),
        )
        .child(
            uniform_list(
                "lines",
                self.total,
                cx.processor(|this, range: Range<usize>, _, _| {
                    this.visible_range = range.clone();
                    range.map(|ix| {
                        div().id(ix).h(px(24.0)).child(format!("Line #{ix}"))
                    }).collect()
                }),
            )
            .track_scroll(&self.scroll_handle)
            .flex_1(),
        )
    }
}
```

**验收标准（三个观察点）**：

1. **虚拟化验证**：标签里「visible rows」恒为几十（视口高 ÷ 24 + 1 左右），无论怎么滚都不增长——证明十万行数据从未变成十万行元素。这就是本讲实践任务要的「验证内存中不会实例化全部行」。
2. **程序化滚动**：点「跳到 99999」后列表滚到底部，可见区间形如 `999xx..100000`；点「回顶部」回到 `0..N`。若按钮无效，第一嫌疑是忘了 `.track_scroll`（deferred 请求没有列表来兑现）。
3. **overscroll**：跳到底后继续向下滚动，列表不应出现空白拖尾——第 402-405 行的钳制在起作用。

注意 `flex_1()` 给列表分配剩余高度，满足「固定（或最大）高度容器」的前提；若外层不约束高度，Auto 尺寸模式下列表可能塌缩为 0 高。运行效果与具体可见行数**待本地验证**。

## 6. 本讲小结

- **等高虚拟化的本质是算术代替布局**：`uniform_list` 每帧只测量 1 个条目（`measure_item`），总高 = 行高 × 行数，第 ix 行的位置与布局约束全部由乘法与加法得出，绕过 Taffy 子树布局，滚动性能与数据规模解耦。
- **可见区间 = `floor` 到 `ceil` 的除法**：`first = ⌊(s - pad)/h⌋`、`last = ⌈(s + V)/h⌉`，渲染回调只为该区间构建元素，半露的行由 `ContentMask` 裁剪。
- **`UniformListScrollHandle` 是「记录意图、下帧兑现」**：`scroll_to_item` 只写入 `deferred_scroll_to_item`，下一次 prepaint 由列表按 `ScrollStrategy`（Top/Center/Bottom/Nearest，strict 与否、可带 offset）换算成像素偏移，同帧立即生效。
- **`track_scroll` 一根线接两头**：底层 `base_handle` 交给 `Interactivity` 驱动滚轮滚动，整个句柄留给列表兑现程序化滚动并记录 `last_item_size`；不挂句柄则两者皆失效。
- **`cx.processor` 是列表回调访问视图状态的标准适配器**：把 `(&mut T, E, &mut Window, &mut Context<T>)` 闭包适配成框架需要的 `(E, &mut Window, &mut App)`，与 `cx.listener` 同一思想。
- **边界三件套**：overscroll 双写钳制（共享偏移 + 本帧偏移）、ContentMask 裁剪、`UniformListDecoration` 协议让跨行视觉元素（选中高亮等）跟随滚动。

## 7. 下一步学习建议

本讲解决了「等高」场景，下一步自然是**变高**场景：

- **u6-l2（list 元素）**：行高不一（聊天消息、折叠面板）时，`uniform_list` 的「测一行乘 N」不再成立。`src/elements/list.rs` 的 `ListState` 会缓存已测量条目的高度、对未渲染区域做尺寸估算——那是虚拟化更通用也更复杂的形态。阅读时带着一个问题：等高版哪些设计被保留了？（提示：`track_scroll`、`ListSizingBehavior`、可见区间回调签名都是两族共享的。）
- **实践路线**：把本讲综合实践的行高改成随内容变化（长文本换行），亲手观察等高假设崩溃的现象（行重叠/空洞），再带着这个痛感去读 list.rs。
- **顺带一读**：Zed 编辑器本体（仓库 `crates/` 下）大量使用 uniform_list 渲染补全列表、项目面板等，可作为真实规模的使用范例检索 `uniform_list(` 的调用点。
