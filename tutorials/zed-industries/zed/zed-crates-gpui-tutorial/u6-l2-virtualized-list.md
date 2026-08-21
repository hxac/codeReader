# 变高度虚拟列表：list 元素

## 1. 本讲目标

上一讲（u6-l1）的 `uniform_list` 用「测一行、乘行数」的算术换来了十万行列表的流畅渲染，但它的前提是**所有条目等高**。一旦条目高度随内容变化——聊天气泡随文本换行、编辑器段落随折叠展开、搜索结果随匹配数伸缩——算术就失效了，必须逐条测量、逐条缓存。

本讲深入 `src/elements/list.rs`（约 3000 行，其中前 ~1717 行为实现，其余是测试），这是 Zed 编辑器里项目面板、设置页、AI 对话流等所有「变高列表」的共同底座。学完后你应该能：

1. 说清 `ListState` 的「测量-缓存-复用」循环：为什么可视区外的条目不许悄悄变高、变了为什么要 `splice` / `remeasure` 通知；
2. 解释向上滚动与跳到底部时，未渲染区域的高度如何被「按需测量」而不是「事先全测」；
3. 会用 `ListScrollEvent`、贴底（`ListAlignment::Bottom`）、follow-tail（`FollowMode::Tail`）和滚动条辅助 API 处理滚动交互；
4. 面对「等高 or 变高」的列表需求，能在 `uniform_list` 与 `list` 之间做出有依据的选型。

## 2. 前置知识

本讲假设你已读过以下内容（不熟悉的概念点击可回看前置讲义）：

- **u6-l1 uniform_list**：虚拟化的基本思想——只为可视区间的行号构建元素，行高恒定时总高 = 行高 × 行数，`ContentMask` 裁剪半露的行。
- **u4-l1 元素三阶段**：`request_layout`（向 Taffy 申报）→ `prepaint`（拿到最终 bounds、登记 hitbox）→ `paint`（提交绘制、注册监听器）。`list` 是一个手写实现 `Element` 的自定义元素，不走 Taffy 排孩子。
- **u5-l1 / u5-l2 输入事件**：滚轮事件 `ScrollWheelEvent`、`ScrollDelta::pixel_delta(line_height)` 的换算、hitbox 的 `should_handle_scroll` 判定、监听器只在 paint 阶段注册且只活一帧。
- **SumTree（本讲新概念）**：`sum_tree` crate 提供的 B 树（默认分支因子 6），每个节点缓存其子树所有条目 summary 的「和」。GPUI 用它存测量缓存：既能在 \( O(\log n) \) 内按「第几条」（Count）或「累计高度多少像素」（Height）定位，又能随手取到全表总高——这正是滚动条和钳制计算需要的。

一个关键直觉先立住：**元素树每帧重建**（立即模式），所以「缓存」缓的不是元素，而是每个条目**上次测得的尺寸**。`list` 每帧只为可视区（加上下 overdraw）重新构建元素并测量，区外条目只留下一个数字。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/elements/list.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs) | 本讲主角：`list()` 构造函数、`List` 元素、`ListState` 状态、SumTree 测量缓存、滚动链路与全部测试 |
| [examples/list_example.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/list_example.rs) | 可运行示例：Bottom 对齐 + `measure_all` + 手写滚动条，条目高度按 `index % 5` 变化 |
| [examples/data_table.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/data_table.rs) | 选型对照：一万行股票表格用的是 `uniform_list`（等高），`Rc<Quote>` 共享数据 + `track_scroll` |
| [src/elements/mod.rs:L23](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/mod.rs#L23) | `pub use list::*;` —— `gpui::list` / `gpui::ListState` 等从这里重导出 |
| crates/sum_tree/src/sum_tree.rs | `Item` / `Summary` / `Dimension` trait 定义，理解缓存结构的背景材料 |

## 4. 核心概念与源码讲解

### 4.1 List 与 ListState：变高列表的总体设计

#### 4.1.1 概念说明

先看 `list.rs` 开头的模块文档，它就是整个元素的**使用契约**：

> 客户端必须保证可视区之外的元素不会改变高度；如果你的元素确实会变高，请通过 `ListState::splice` 或 `ListState::reset` 通知列表元素。为了减少重渲染，这个元素的状态以「侵入式」存放在你自己的视图上，这样你的代码可以直接与列表元素的缓存状态协同。如果所有元素等高，请用 `UniformList`。

[src/elements/list.rs:L1-L8](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1-L8) —— 上述契约的原文（英文）。

为什么有这条契约？因为列表把「区外条目的高度」当作**已知缓存**参与滚动计算。如果区外某条目自己长高了而列表不知情，总高、滚动条、锚点位置就全错了。所以变更必须走 `splice`（增删条目）或 `remeasure`（同一条目内容变高）显式申报。

「侵入式状态」是 `list` 与众不同的第二点。回忆 u4-l1：元素的跨帧状态通常以 `(GlobalElementId, TypeId)` 为键存在窗口的 `element_states` 表里。但 `List` 的 `id()` 返回 `None`（[src/elements/list.rs:L1444-L1446](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1444-L1446)），它不把状态放进窗口，而是要求**你的视图持有一个 `ListState`**，每帧 render 时把它的克隆传进来。`ListState` 内部是 `Rc<RefCell<StateInner>>`，克隆只是引用计数加一，元素与你的代码共享同一份缓存——你的视图可以直接调 `list_state.scroll_to_end()`、读 `logical_scroll_top()`，这就是「协同」的含义。

第三点是 `ListAlignment`：`Top` 是普通列表（默认锚在顶端），`Bottom` 是聊天记录式（默认锚在底端）。这个枚举决定了 `logical_scroll_top` 为空时的默认锚点，是聊天 UI 几乎零成本贴底的关键（4.4 详述）。

#### 4.1.2 核心流程

一帧之内，`list` 的工作流程概览：

```text
你的视图 render()
  └─ list(list_state.clone(), render_item)   // 只是把回调装箱，不做任何测量
       │
       ├─ request_layout：向 Taffy 申报自身（Auto：吃父容器给的尺寸；
       │                    Infer：先跑一遍 layout_items 算总高，再申报）
       ├─ prepaint：拿到最终 bounds
       │    ├─ 若宽度变了 → 全部缓存失效（重置为 Unmeasured）
       │    └─ layout_items：从滚动锚点向下测量到「可视高 + overdraw」，
       │         不满一屏则向上补；缓存写回 SumTree
       └─ paint：注册滚轮监听器（先于孩子，让孩子的 bubble 处理器先跑），
            然后在 ContentMask 内逐条 paint 可视条目
```

滚动发生时（滚轮 / 代码调用）改的是 `StateInner::logical_scroll_top`，然后 `cx.notify(宿主视图)` 触发下一帧，下一帧 `layout_items` 按新锚点重新选窗。

#### 4.1.3 源码精读

**构造函数与元素结构**。[src/elements/list.rs:L24-L42](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L24-L42)：`list(state, render_item)` 返回 `List`，它只装四样东西——状态、渲染回调（`Box<dyn FnMut(usize, &mut Window, &mut App) -> AnyElement>`，注意回调拿到的是 `&mut App` 而不是 `Context<T>`，所以通常用 `cx.processor` 适配回视图方法）、样式补丁、尺寸策略。真正的测量不发生在构造时。

**ListState 与 StateInner**。[src/elements/list.rs:L52-L76](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L52-L76)：`ListState` 是 `Rc<RefCell<StateInner>>` 的透明包装；`StateInner` 的字段就是理解本讲的目录：

- `items: SumTree<ListItem>` —— 测量缓存（4.2）；
- `logical_scroll_top: Option<ListOffset>` —— 滚动锚点，`None` 表示「贴自然锚点」（Top 贴顶 / Bottom 贴底）；
- `alignment` / `overdraw` —— 对齐方向与上下超额测量区；
- `pending_scroll` / `follow_state` —— 重测后的滚动补偿与贴底跟随（4.3 / 4.4）；
- `scroll_handler` / `scrollbar_drag_start_height` —— 滚动回调与拖动滚动条期间的高度冻结。

**构造与初始 splice**。[src/elements/list.rs:L307-L331](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L307-L331)：`ListState::new(item_count, alignment, overdraw)` 建好空状态后立刻 `splice(0..0, item_count)`，把 `item_count` 个 `Unmeasured` 条目放进缓存树——所以新列表「知道有 100 条」但「不知道每条多高」。

**对齐枚举**。[src/elements/list.rs:L162-L169](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L162-L169)：`Top`（普通列表）/ `Bottom`（聊天日志）。

**滚动事件类型**。[src/elements/list.rs:L171-L184](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L171-L184)：`ListScrollEvent` 把滚动翻译成「条目语义」：`visible_range`（当前可见的行号区间）、`count`（总条数）、`is_scrolled`（是否离开自然锚点）、`is_following_tail`（是否正在贴底跟随）。4.4 详述。

#### 4.1.4 代码实践

**实践目标**：跑通官方示例，直观感受「变高 + 贴底」两个特性。

1. 操作步骤：在 zed 仓库根目录执行 `cargo run -p gpui --example list_example`；
2. 需要观察的现象：示例打开时直接显示**列表底部**（Bottom 对齐的自然锚点）；每行高度按 `index % 5` 在 30~70px 间变化（[examples/list_example.rs:L97](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/list_example.rs#L97)）；用滚轮上翻时顶部状态行的 `offset / max / fraction` 实时变化，`fraction` 是滚动条拇指位置比例（正常 ≤ 1.0，示例专门用它检测一个历史 bug）；
3. 预期结果：变高条目滚动时无闪烁、无跳变——这就是 `overdraw px(500.)` + `measure_all()`（[examples/list_example.rs:L19](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/list_example.rs#L19)）的效果；
4. 若本地无法编译运行（缺平台依赖），此步「待本地验证」，可改为阅读源码。

#### 4.1.5 小练习与答案

1. **练习**：把示例中的 `ListAlignment::Bottom` 改成 `Top`，首帧显示哪里？为什么？
   **答案**：显示第 0 条。`logical_scroll_top` 为 `None` 时，`Top` 对齐的默认锚点是 `ListOffset { item_ix: 0, offset_in_item: 0 }`（[src/elements/list.rs:L962-L974](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L962-L974)）。
2. **练习**：`overdraw` 参数调大到 `px(2000.)` 会有什么代价与收益？
   **答案**：收益是快速滚动时上下预备区更大、更不容易看到空白；代价是每帧要测量/复用更多条目（窗口为可视高加两侧 overdraw，[src/elements/list.rs:L308-L313](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L308-L313) 的文档注释）。Zed 里聊天类取 2048px、选择器取 1000px 左右。
3. **练习**：为什么 `List` 的 `id()` 返回 `None` 却仍能保存跨帧状态？
   **答案**：因为状态不在窗口的 element_states 表里，而在调用方视图持有的 `ListState`（`Rc` 共享）中——这就是模块文档说的「侵入式存储」。

### 4.2 测量缓存：ListItem、ListItemSummary 与失效机制

#### 4.2.1 概念说明

缓存的数据结构是一棵 `SumTree<ListItem>`。每个 `ListItem` 只有两个变体：

- `Unmeasured { size_hint, focus_handle }` —— 从未（或不再）被测量过；`size_hint` 是可选的估计高度，参与总高估算但不参与布局；
- `Measured { size, focus_handle }` —— 上次真实测得的尺寸。

每条目贡献一个 `ListItemSummary`，字段包括 `count`、`rendered_count`、`unrendered_count`、`height`（总高）、`has_focus_handles`、`has_unknown_height`。SumTree 的每个子树节点缓存其下所有 summary 的和，于是：

- 全表总高 = 根 summary 的 `height` —— 滚动条与滚动钳制直接可用；
- 「第 i 条之前累计多高」= 用 `Count(i)` 作维度 seek，起点 summary 即答案 —— \( O(\log n) \)；
- 「累计高度 y 像素处是第几条」= 用 `Height(y)` 作维度 seek —— 这正是像素滚动偏移 ⇄ 条目锚点互换的核心。

`Unmeasured` 条目对 `height` 的贡献是 `size_hint` 的高度、没有 hint 则为 0，并把 `has_unknown_height` 置真（[src/elements/list.rs:L1646-L1657](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1646-L1657)）。所以「未测量区域的总高」天然只是估计——这就是本讲标题里「未渲染区域的尺寸估计」的机制基础。

失效（缓存何时作废）有三条路径：

1. **宽度变化**：prepaint 里发现 `bounds.size.width` 与上一帧不同 → 整棵树重置为 `Unmeasured`（连 size_hint 也清空，[src/elements/list.rs:L1543-L1558](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1543-L1558)）。原因很直白：文本换行数取决于宽度，宽度一变所有高度都不可信。
2. **数据增删**：`splice` / `splice_focusable` 把 `old_range` 替换成 n 个新的 `Unmeasured` 条目（[src/elements/list.rs:L501-L549](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L501-L549)）。它还会顺手修正滚动锚点：锚点条目被删则吸到 `old_range.start` 顶部；删除发生在锚点之前则平移索引号（`item_ix - 旧长度 + 新长度`）。
3. **同条目变高**：`remeasure_items(range)` 把区间重置为 `Unmeasured` 但**保留 size_hint**，并登记一个 `PendingScroll` 补偿（4.3 详述）；全量 `remeasure()` 用比例锚点（[src/elements/list.rs:L396-L414](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L396-L414)），适合字号变化这类「整体缩放」场景。

#### 4.2.2 核心流程

SumTree 的读写模式（seek 一律对数时间）：

```text
写入：layout_items 每帧末把「本次测量窗口」的 Measured 条目
      切片拼回树（slice 前缀 + extend 测量结果 + append 后缀）
读取：
  seek(Count(i),  Bias::Right) → 定位第 i 条（边界偏右）
  seek(Height(y), Bias::Left)  → 定位累计高度 y 处
  summary().height             → 全表总高（滚动条）
  cursor.start().height        → 光标前的累计高度（锚点换算）
```

估计与真实的换算关系：

\[ H_{\text{总}} = \sum_{\text{measured}} h_i + \sum_{\text{unmeasured}} \text{hint}_i \quad (\text{无 hint 记 } 0) \]

滚动范围据此钳制：`scroll_max = max(0, H_总 + padding − 视口高)`。随着滚动推进，被真实测量的条目越来越多，`H_总` 逐渐收敛到真值——滚动条会「越来越准」，这是变高虚拟列表的固有特征。

#### 4.2.3 源码精读

**ListItem 与 summary 结构**。[src/elements/list.rs:L244-L299](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L244-L299)：两个变体、`size()` / `size_hint()` / `focus_handle()` 三个访问器，以及 `ListItemSummary` 的六个字段。注意 `focus_handle` 在失效时**始终被保留**（`Unmeasured` 也带着它），这是「焦点条目滚出屏幕仍可交互」的数据基础（4.3）。

**summary 的加法与维度**。[src/elements/list.rs:L1638-L1685](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1638-L1685) 定义了 `Item for ListItem`（每种变体如何产出 summary）与 `add_summary`（字段逐个累加，`has_focus_handles` / `has_unknown_height` 用或）。[src/elements/list.rs:L1687-L1717](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1687-L1717) 定义 `Count` / `Height` 两个 Dimension 与对应 SeekTarget——这就是「按条目数寻址」和「按像素高度寻址」两种坐标系的实现，全文所有 `cursor.seek(&Height(y), ..)` 都依赖它。

**measure_all 与 with_uniform_item_height**。[src/elements/list.rs:L333-L350](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L333-L350)：两条让滚动条「第一帧就准」的路线。`measure_all()` 首帧渲染前把所有条目真实测一遍（大列表首帧可能很贵，文档明说）；`with_uniform_item_height(px)` 只给所有 `Unmeasured` 条目填一个统一 hint（[src/elements/list.rs:L377-L394](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L377-L394)），滚动条先按估计画、随真实测量逐帧收敛——表格类「行高大致均匀」的场景推荐后者。

**splice 的锚点修正**。[src/elements/list.rs:L501-L549](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L501-L549)：除替换条目外，末尾 12 行专门修 `logical_scroll_top`：锚点条目落在被删区间 → 吸到区间起点且偏移清零；删除整体在锚点之前 → 索引平移。聊天「加载更早的历史」（顶部 splice）不掉位置，靠的就是这段。

#### 4.2.4 代码实践

**实践目标**：亲眼验证「宽度变化 → 缓存整体失效」。

1. 操作步骤：运行 `list_example`（它 `measure_all()` 过，总高是精确值），观察顶部状态行的 `max` 值；然后横向拖拽改变窗口宽度；
2. 需要观察的现象：宽度一变，`max` 数值变化后仍稳定（不会跳到只剩已测条目的假值）；
3. 预期结果：宽度变化触发整树失效（[src/elements/list.rs:L1543-L1558](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1543-L1558)），`measure_all` 的 `Measure(false)` 也随之 reset（[src/elements/list.rs:L207-L214](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L207-L214)），下一帧重新全量测量，`max` 恢复精确。对应的回归测试是 [src/elements/list.rs:L2066-L2099](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L2066-L2099)（`test_measure_all_after_width_change`，断言换宽后 `max_offset_for_scrollbar().y` 仍是 300px）；
4. 待本地验证。

#### 4.2.5 小练习与答案

1. **练习**：一个 `Unmeasured` 且无 `size_hint` 的条目，对 summary 的 `height` 与 `has_unknown_height` 各贡献什么？
   **答案**：`height` 贡献 `px(0.)`，`has_unknown_height` 贡献 `true`（[src/elements/list.rs:L1646-L1657](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1646-L1657)）。`is_scrolled_to_end()` 遇到 `has_unknown_height` 会返回 `None` 表示「无法判断」（[src/elements/list.rs:L484-L499](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L484-L499)）。
2. **练习**：为什么失效时要保留 `focus_handle`？
   **答案**：焦点句柄与高度无关，且列表需要凭它检测「被聚焦的条目是否在屏外」从而继续渲染它以维持键盘交互（[src/elements/list.rs:L1220-L1242](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1220-L1242)）。
3. **练习**：`measure_all()` 和 `with_uniform_item_height(px(30.))` 各自的第一帧成本与滚动条精度如何取舍？
   **答案**：`measure_all` 首帧全量真实测量（贵、但滚动条即刻精确）；`with_uniform_item_height` 零测量成本、滚动条先按 30px/条估算后逐帧收敛（便宜、首帧近似）——见 [src/elements/list.rs:L333-L350](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L333-L350) 两条 API 的文档注释。

### 4.3 测量-缓存-复用循环：layout_items 逐段精读

#### 4.3.1 概念说明

`StateInner::layout_items`（约 220 行）是整个元素的心脏。它每帧回答一个问题：**「从滚动锚点出发，哪些条目需要真实构建元素、哪些用缓存数字就够了？」**

规则可以概括成四条：

1. **向下走**：从锚点条目开始向下，直到累计「可视高度」达到 `视口高 + overdraw` 为止。可视区内的条目**每帧都重新** `render_item` + `layout_as_root`（元素树本来就是每帧重建的）；尾部 overdraw 区里的条目若已有缓存则只取数字、不建元素。
2. **不满一屏就向上补**：如果锚点下方的内容填不满视口（例如跳到列表末尾、或数据变少），从锚点向上 `cursor.prev()` 逐条渲染直到填满，然后**重算锚点**。`Top` 对齐把锚点上推并存储；`Bottom` 对齐则把 `logical_scroll_top` 置回 `None`——重新贴底。
3. **向上预备**：锚点上方再测 `overdraw` 高度的条目（leading overdraw），只测量不绘制，供上滚时即时可用。
4. **焦点保活**：若本帧可视条目中没有包含焦点的，找到持有焦点的屏外条目额外渲染一条，让键盘交互不断线。

「未渲染区域的尺寸估计」就发生在第 2 条：跳到底部时下方全是 `Unmeasured`，循环会**边走边测**——每个未测条目真实构建一次元素拿高度，直到填满视口。代价与未测条目数成正比，但只需覆盖一屏加 overdraw，而不是全表。

另一个精妙机制是 `PendingScroll`（重测补偿）：`remeasure_items` 把锚点条目标记重测时，先记下「锚点在该条目内的位置」——像素偏移（Absolute，看内容时保持文字不跳）或比例偏移（Proportional，整体缩放时保持相对位置）；下一帧该条目测出新高度后，按记录恢复锚点（[src/elements/list.rs:L78-L109](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L78-L109)）。若用户在重测生效前又滚动，`rebase_pending_scroll` 会把补偿重新锚到新位置，避免「滚了又被弹回去」（[src/elements/list.rs:L844-L876](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L844-L876)）。

#### 4.3.2 核心流程

`layout_items` 的伪代码（对照源码四段）：

```text
输入: 视口高 H, overdraw O, 锚点 scroll_top{item_ix, offset_in_item}
若 follow-tail 激活: scroll_top = 末尾（item_ix = 总条数, offset = 0）

# ① 向下测量（L1060-L1124）
cursor = 缓存树.seek(Count(scroll_top.item_ix))
rendered_height = padding.top
循环 cursor 向下:
    visible_height = rendered_height - scroll_top.offset_in_item
    若 visible_height >= H + O: 跳出
    若 visible_height < H 或无缓存:          # 可视区内每帧重建；overdraw 区只在没缓存时测
        element = render_item(ix)
        size = element.layout_as_root(...)
        若 ix == 0 且有 pending_scroll: 按补偿恢复 offset_in_item
        若 visible_height < H: 进 item_layouts（要绘制）
    rendered_height += size.height
    写入 measured_items

# ② 不满一屏向上补（L1127-L1176）
若 rendered_height - offset < H:
    cursor.prev() 逐条渲染直到填满
    重算 scroll_top:
      Top    → offset = max(0, rendered_height - H)，存 Some
      Bottom → 存 None（重新贴底）

# ③ 向上预备（L1178-L1198）
从锚点向上再累计 O 高度（有缓存用缓存，没缓存现测），只进缓存不进绘制列表

# ④ 写回（L1200-L1206）
measured_range 之外的旧条目原样保留，区间内换成新测结果 → 新 SumTree

# ⑤ 收尾（L1208-L1242）
若 follow-tail 已停且滚动回到距底 1px 内 → 重新 start_following
若焦点不在可视条目中 → 找到持焦点的屏外条目补渲染一条
```

#### 4.3.3 源码精读

**向下测量循环**。[src/elements/list.rs:L1060-L1124](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1060-L1124)：`cursor.seek(&Count(scroll_top.item_ix), Bias::Right)` 后逐条枚举；L1071-L1074 是「复用 or 重建」的判定——`if visible_height < available_height || size.is_none()` 才调 `render_item`；L1082-L1103 在 `ix == 0` 时消费 `PendingScroll`（Absolute 取 `min(新高度)`，Proportional 取 `fraction × 新高度`）；L1105-L1114 只有真正可视的条目进入 `item_layouts`，overdraw 条目只测不画。

**向上补齐与贴底**。[src/elements/list.rs:L1127-L1176](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1127-L1176)：`while rendered_height < available_height` 里 `cursor.prev()` 向上渲染；随后按对齐方向分派——`Bottom` 分支把 `logical_scroll_top` 置 `None`（L1168-L1174），这就是「跳到末尾后新消息持续到达仍贴底」的实现基础。

**leading overdraw 与树写回**。[src/elements/list.rs:L1178-L1206](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1178-L1206)：向上预备循环只写 `measured_items`；随后 `slice + extend + append` 把测量窗口拼回整树——窗口外的缓存（包括从未见过的 `Unmeasured`）原样保留。

**follow-tail 再挂挡与焦点保活**。[src/elements/list.rs:L1208-L1242](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1208-L1242)：停跟后只要 `滚动偏移 + 视口高 ≥ 总高 − 1px` 就重新开始跟随；末段用 `filter` 光标只遍历 `has_focus_handles` 的条目，找到含焦点者补进绘制列表。

**prepaint 编排与子元素 autoscroll**。[src/elements/list.rs:L1251-L1276](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1251-L1276)：`prepaint_items` 在 `window.transact` 里先处理 `Measure` 行为的全量测量，再调 `layout_items`，然后以 `bounds.origin + padding.top − offset_in_item` 为起点、在 `ContentMask` 内逐条 `prepaint_at`。若某个孩子（如获得焦点的输入框）请求了 autoscroll，函数返回 `Err(新锚点)`，外层 [prepaint（L1563-L1572）](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1563-L1572) 设好锚点后**再跑一遍**（第二遍关闭 autoscroll）。上边界越界的请求还会向上走进更早的条目保证偏移非负（[src/elements/list.rs:L1292-L1325](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1292-L1325)，对应测试 [L1732-L1783](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1732-L1783)）。

**request_layout 的两种尺寸策略**。[src/elements/list.rs:L1452-L1524](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1452-L1524)：默认 `Auto` —— 像普通 div 一样吃父容器分配的尺寸；`Infer` —— 列表按内容推算自身高度（先跑一遍 `layout_items` 拿总高，再经 `request_measured_layout` 闭包上报 `min(总高, 可用高)`），用于把列表嵌进别人的滚动容器时。注意 `Infer` 还会设 `overflow.y = Scroll`（L1462）。

#### 4.3.4 代码实践

**实践目标**：通过阅读一个测试，验证锚点换算与「不满一屏向上补」。

1. 操作步骤：阅读 [src/elements/list.rs:L1828-L1871](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1828-L1871)（`test_scroll_by_positive_and_negative_distance`）：5 条 × 20px、视口 100px 的列表，先 `scroll_by(px(30.))` 再 `scroll_by(px(-30.))`；
2. 需要观察的现象：+30px 后锚点是 `item_ix: 1, offset_in_item: 10px`（20px 一条，30px 落在第二条内部 10px 处）；−30px 后回到 `item_ix: 0, offset_in_item: 0px`；
3. 预期结果：像素偏移 ⇄ 条目锚点的互换精确无误，这正是 4.2 里 `Count`/`Height` 双维度 seek 的行为；随后运行 `cargo test -p gpui --element list` 之外的正确命令：`cargo test -p gpui list`（跑该文件全部测试）验证通过；
4. 待本地验证（测试运行结果）。

#### 4.3.5 小练习与答案

1. **练习**：可视区内**已有缓存**的条目，下一帧还会调用 `render_item` 吗？
   **答案**：会。判定是 `visible_height < available_height || size.is_none()`（[src/elements/list.rs:L1074](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1074)），可视区内前半恒真——元素树每帧重建，缓存省的是 overdraw 区的构建，不是可视区的。
2. **练习**：`splice(2..5, 1)`（把第 2~4 条换成 1 条新条目）后，原本锚在 `item_ix: 10` 的位置变成多少？
   **答案**：`10 − (5 − 2) + 1 = 8`，由 [src/elements/list.rs:L545-L547](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L545-L547) 的平移公式计算；若锚点条目本身在被删区间内则吸到 `item_ix: 2, offset: 0`。
3. **练习**：一个流式输出的 AI 回答条目每秒变高一次，应该调哪个 API？为什么不能不管它？
   **答案**：`remeasure_items(ix..ix+1)`（或对该条目 splice）。不管它就违反了模块契约——区外/未重测条目的高度被当作已知，总高与滚动条会错；`remeasure_items` 还会登记 Absolute 型 `PendingScroll` 让可视文字不跳动（[src/elements/list.rs:L405-L414](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L405-L414) 的文档注释举的正是 streaming text 例子）。

### 4.4 滚动链路：滚轮、scroll()、ListScrollEvent 与贴底

#### 4.4.1 概念说明

滚动有两条入口：**用户滚轮**（paint 阶段注册的监听器）与**程序滚动**（`scroll_to` / `scroll_by` / `scroll_to_end` / `set_follow_mode` 等）。两条路最终都归结为改 `logical_scroll_top`（像素偏移先经 `Height` 维度换算成条目锚点）并 `cx.notify` 宿主视图。

`logical_scroll_top = None` 不是「没有位置」，而是「贴自然锚点」：`Top` 贴第 0 条、`Bottom` 贴末尾（[src/elements/list.rs:L962-L974](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L962-L974)）。`Bottom` 列表因此在用户滚到底时把锚点清空、自动恢复贴底——聊天记录「回到底部就继续跟随」零成本获得。

Follow-tail（`FollowMode::Tail`）是显式的贴底跟随状态机：跟随中每帧锚定末尾；用户上滚即 `stop_following`；滚回距底 1px 内自动 `start_following`（4.3 的 ⑤）。它与 `Bottom` 对齐的隐式贴底互为补充，适合 `Top` 对齐但又要跟随尾部增长的场景（如日志流）。

关于学习目标里提到的 overscroll 与 sticky：

- **overscroll（越界）**：`scroll()` 把目标偏移钳制在 \([0, \text{scroll\_max}]\) 内（`scroll_max = max(0, 总高 + padding − 视口高)`），列表不会滚出内容边界；拖动滚动条期间用 `scrollbar_drag_started` 冻结高度基准，防止拖动中内容增长导致拇指跳变。
- **sticky（吸附表头之类）**：`list.rs` **没有**内置 sticky 机制。Zed 的做法是把固定表头渲染在列表之外的普通 div 里（如 `data_table` 示例的表头行），或用 u6-l3 将讲的 `deferred`/`anchored` 浮层实现。不要指望 `list` 提供 `position: sticky`。

#### 4.4.2 核心流程

一次滚轮滚动的旅程：

```text
滚轮 → 平台翻译为 ScrollWheelEvent → 窗口 hitbox 命中测试
  → Capture 正序 / Bubble 逆序两阶段派发（u5-l1）
  → list 在 paint 阶段注册的监听器（先于孩子注册 ⇒ bubble 阶段孩子先跑）
     ├─ 孩子 stop_propagation? → 列表不动（嵌套滚动容器场景）
     └─ 否则 delta.coalesce(...).pixel_delta(px(20.)) → StateInner::scroll()
          ├─ reset 期间直接丢弃（高度未知，L909-L911）
          ├─ 像素钳制 [0, scroll_max]；Height 维度换算回 ListOffset
          ├─ Bottom 且到底 → logical_scroll_top = None（贴底）
          ├─ 上滚 → follow-tail stop_following
          ├─ 触发 scroll_handler(ListScrollEvent{ visible_range, count, ... })
          └─ cx.notify(宿主视图) → 下一帧 layout_items 按新锚点选窗
```

#### 4.4.3 源码精读

**paint：监听器注册顺序的讲究**。[src/elements/list.rs:L1579-L1621](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1579-L1621)：列表自己的滚轮监听器**先于**孩子 paint 注册（L1601 在 L1616 之前），而 bubble 阶段按注册的**逆序**回调——于是孩子的 `on_scroll_wheel` + `stop_propagation` 能先拦下事件（嵌套滚动），与 div 滚动容器的语义一致；L1591-L1595 的注释原文解释了这一点。delta 先 `coalesce` 再 `pixel_delta(px(20.))`（Lines 滚轮按 20px/行换算），命中判定用 `hitbox_id.should_handle_scroll`（u5-l2 讲过为什么不用 `is_hovered`）。最后在 `ContentMask` 内逐条 paint（L1616-L1620）。

**scroll()：钳制、贴底与事件**。[src/elements/list.rs:L898-L960](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L898-L960)：`new_scroll_top = clamp(当前像素偏移 − delta.y, 0, scroll_max)`；`Bottom` 对齐且恰好到底 → 锚点清空（L920-L922）；换算用 `items.find(.., &Height(new_scroll_top), Bias::Right)`（L924-L930）；随后 `rebase_pending_scroll` 防止重测补偿覆盖用户滚动；`delta.y > 0`（上滚）停止跟随；最后构造 `ListScrollEvent` 回调 + `cx.notify`（L942-L959）。`visible_range` 由 [src/elements/list.rs:L886-L896](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L886-L896) 用两次 seek 算出：锚点向下到 `锚点像素 + 视口高` 处的前一条。

**程序滚动 API**。[src/elements/list.rs:L559-L707](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L559-L707)：`scroll_by`（相对像素）、`scroll_to`（绝对锚点，越界钳到末尾）、`scroll_to_reveal_item`（让某条完整可见——目标在上方就对齐其顶部，在下方就从底部倒推，L689-L703）、`scroll_to_end`（锚到 `item_ix = 总数`，文档注明即使末条还在增长也总是显示其底部——流式输出场景专用，L597-L611）。

**follow-tail**。[src/elements/list.rs:L613-L657](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L613-L657)：`set_follow_mode(Tail)` 立即锚到末尾并开始跟随；`pause_following_tail` 冻结当前位置但保持 Tail 模式（文档举的例子：放大图表导致条目变高时不要吸走）；`is_following_tail` 供 UI 查询。再挂挡逻辑在 layout 尾部（4.3 已读）。

**滚动条辅助 API**。[src/elements/list.rs:L739-L801](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L739-L801)：GPUI 不内置滚动条，`list` 提供四个钩子让你自绘——`max_offset_for_scrollbar()`（最大偏移，拖动期间冻结在拖动开始时的高度）、`scroll_px_offset_for_scrollbar()`（当前偏移，y 为负）、`viewport_bounds()`、`set_offset_from_scrollbar(point)`（把拖动的拇指位置换算回锚点，[src/elements/list.rs:L1371-L1418](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1371-L1418)，拖到冻结末端还会自动恢复贴底跟随）。`list_example` 的手写滚动条（[examples/list_example.rs:L26-L48](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/list_example.rs#L26-L48)）就是用这组 API 算拇指高度与位置的。

**视口查询**。[src/elements/list.rs:L803-L841](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L803-L841)：`item_is_above_viewport` / `item_is_below_viewport` 返回 `Option<bool>`（布局不足时 `None`），Zed 用它做「滚出视口的行不渲染某些重内容」这类优化。

#### 4.4.4 代码实践

**实践目标**：用 `set_scroll_handler` 观察滚动事件与可见区间。

在 `list_example` 的 `BottomListDemo::new()` 里加一行（示例代码，修改请在自己的工作副本进行）：

```rust
// 示例代码：注册滚动回调，打印每次滚动后的可见区间
list_state.set_scroll_handler(|event, _window, _cx| {
    println!(
        "visible: {:?}, count: {}, is_scrolled: {}, following_tail: {}",
        event.visible_range, event.count, event.is_scrolled, event.is_following_tail
    );
});
```

1. 操作步骤：加完后 `cargo run -p gpui --example list_example`，上下滚动若干次；
2. 需要观察的现象：`visible_range` 随滚动平移；`is_scrolled` 在滚离底端时变 `true`，滚回最底时变 `false`（锚点被清空回贴底）；
3. 预期结果：与 4.4.2 的链路一致——每次滚轮事件触发一次 handler，随后一帧重绘；
4. 待本地验证。

#### 4.4.5 小练习与答案

1. **练习**：为什么列表的滚轮监听器要在孩子 paint **之前**注册？
   **答案**：bubble 阶段按注册逆序回调，先注册意味着后执行；孩子（如内嵌滚动区）先拿到事件并可 `stop_propagation` 阻止列表滚动（[src/elements/list.rs:L1591-L1602](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1591-L1602) 注释，行为由 [L1873-L1925](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L1873-L1925) 的测试锁定）。
2. **练习**：`ListAlignment::Bottom` 且用户滚到最底时 `logical_scroll_top` 是什么？此时追加新消息列表会动吗？
   **答案**：`None`（[src/elements/list.rs:L920-L922](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L920-L922)），即贴自然锚点（末尾）；追加消息后下一帧从末尾向上填满视口（4.3 步骤②），列表保持贴底。
3. **练习**：拖动滚动条期间条目被测量导致总高变化，拇指为什么不会跳？
   **答案**：`scrollbar_drag_started` 记录拖动开始时的总高（[src/elements/list.rs:L739-L753](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/list.rs#L739-L753)），`max_offset_for_scrollbar` 与 `set_offset_from_scrollbar` 在拖动期间都用这份冻结高度（L878-L884、L1377-L1383），拖完 `scrollbar_drag_ended` 解冻。

### 4.5 API 全景与选型：uniform_list 还是 list

#### 4.5.1 概念说明

| 维度 | `uniform_list`（u6-l1） | `list`（本讲） |
| --- | --- | --- |
| 行高 | 必须等高 | 任意、可变（变更需申报） |
| 测量成本 | 每帧测 1 条推全局 | 每帧测「视口 + 2×overdraw」区，缓存区外 |
| 状态宿主 | `UniformListScrollHandle`（内含 `ScrollHandle`） | `ListState`（侵入式，放在你的视图上） |
| 元素 id | 构造时显式传入（`uniform_list("id", ..)`） | `id()` 为 `None`，状态不进窗口表 |
| 增删数据 | 改 `item_count` 即可 | 必须 `splice` / `reset` 申报 |
| 行高变化 | 不支持 | `remeasure` / `remeasure_items` + 滚动补偿 |
| 贴底 / 跟尾 | 无专门支持 | `ListAlignment::Bottom` / `FollowMode::Tail` |
| 滚动定位 | `scroll_to_item(ix)`（按行号） | `scroll_to_reveal_item` / `scroll_to` / `scroll_by` / `scroll_to_end`（像素 + 条目双坐标） |

选型口诀：**等高用 uniform_list（便宜、简单），变高才上 list（贵一点，但什么都能装）**。Zed 自己的用法印证了这一点：

- 等高表格：`data_table` 示例一万行用 `uniform_list` + `track_scroll` + `Rc<Quote>` 共享数据（[examples/data_table.rs:L429-L444](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/data_table.rs#L429-L444)）；
- AI 对话流（条目高度随流式输出变化）：`agent_ui` 的 conversation_view 用 `ListState::new(0, Top, px(2048.))`（[crates/agent_ui/src/conversation_view.rs:L1267](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/agent_ui/src/conversation_view.rs#L1267)）；
- 日志流（贴底跟随）：`acp_tools` 用 `ListAlignment::Bottom + px(2048.)`（[crates/acp_tools/src/acp_tools.rs:L178](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/acp_tools/src/acp_tools.rs#L178)）；
- 选择器列表（大致等高但留变化余地）：`picker` 用 `Top + px(1000.)`；设置页要精确滚动条，加 `measure_all()`。

#### 4.5.2 核心流程

决策树：

```text
条目高度恒定吗？
 ├─ 是 → uniform_list（行数多时收益最大）
 └─ 否 → 条目会被增删吗？
      ├─ 会 → list + splice / splice_focusable
      └─ 只变高 → list + remeasure_items
          └─ 还需要贴底/跟尾？→ Bottom 对齐 或 FollowMode::Tail
              └─ 滚动条要求首帧精确？→ measure_all() 或 with_uniform_item_height(h)
```

#### 4.5.3 源码精读

**uniform_list 侧的对照**。[examples/data_table.rs:L429-L444](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/data_table.rs#L429-L444)：`uniform_list("items", count, cx.processor(...))` 的回调签名是 `Range<usize>`（整段区间一次给足，因为等高所以算术可算），配合 `.track_scroll(&self.scroll_handle)`；行组件 `TableRow` 是 `#[derive(IntoElement)]` 的 `RenderOnce`（[examples/data_table.rs:L140-L253](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/data_table.rs#L140-L253)），数据用 `Rc<Quote>` 共享避免克隆——这套「实体存数据 + 虚拟化渲染 + 组件做行」的分层正是 u3-l6 讲过的模式。

**list 侧的对照**。[examples/list_example.rs:L96-L115](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/list_example.rs#L96-L115)：`list(state.clone(), |index, _window, _cx| {...})` 的回调签名是**单个行号**，逐条按需调用；行高 `30 + (index % 5) * 10` 各不相同。

#### 4.5.4 代码实践

**实践目标**：做一次真实的选型判断并写出理由。

1. 操作步骤：为以下三个需求各选一个元素并写下一句理由——(a) 十万行等高日志查看器；(b) IM 聊天记录（气泡高度随文本、需贴底）；(c) 设置页的搜索结果（几十条、高度不一、需要精确滚动条）；
2. 需要观察的现象：无（纯设计练习）；
3. 预期结果：(a) `uniform_list`——等高，算术虚拟化最便宜；(b) `list` + `ListAlignment::Bottom`（+ 必要时 `FollowMode::Tail`）；(c) `list` + `measure_all()`（条目少，全量测量换取首帧精确滚动条，settings_ui 正是这么用的）。

#### 4.5.5 小练习与答案

1. **练习**：把等高的 `data_table` 硬改成 `list` 有什么损失？
   **答案**：每帧要多测/复用「视口 + 2×overdraw」个条目并维护 SumTree，而 `uniform_list` 只测一条；还要自己管理 splice 与锚点——等高场景纯属白付成本。
2. **练习**：`list` 的 `render_item` 回调拿到的是 `&mut App`，想在回调里读写自己的视图怎么办？
   **答案**：用 `cx.processor(|this, ix, window, cx| ...)` 把「视图方法」适配成回调（u6-l1 已见过同款用法，定义在 [src/app/context.rs:L264-L272](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app/context.rs#L264-L272)），它内部自动做 `entity.update`。

## 5. 综合实践

**任务**：用 `list` 实现一个聊天记录列表——每条消息高度随内容变化（长文本自动换行），按钮发送新消息时自动贴底，并能随时「回到底部」。这覆盖本讲全部三个最小模块：ListState 与 SumTree 缓存（4.1/4.2）、测量-缓存-复用与未测量区域处理（4.3）、贴底与滚动（4.4）。

以下是完整示例代码（示例代码，非仓库原有内容）。在 crates/gpui 下新建 `examples/chat_list.rs`，并在 `Cargo.toml` 的 `[[example]]` 区仿照 [Cargo.toml:L245-L246](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/Cargo.toml#L245-L246) 增加一段 `name = "chat_list"` / `path = "examples/chat_list.rs"`（gpui 的示例都是显式声明的，不加不会被 cargo 发现）：

```rust
#![cfg_attr(target_family = "wasm", no_main)]

use gpui::{
    App, Bounds, Context, ListAlignment, ListState, Render, SharedString, Window, WindowBounds,
    WindowOptions, div, list, prelude::*, px, rgb, size,
};
use gpui_platform::application;

struct ChatDemo {
    messages: Vec<SharedString>,
    list_state: ListState,
}

impl ChatDemo {
    fn new() -> Self {
        Self {
            messages: Vec::new(),
            // Bottom 对齐：自然锚点就是末尾，聊天记录的标准配置
            list_state: ListState::new(0, ListAlignment::Bottom, px(500.)),
        }
    }

    /// 追加消息的三步曲：改数据 → splice 申报 → 贴底
    fn append(&mut self, long: bool, cx: &mut Context<Self>) {
        let at = self.messages.len();
        let text = if long {
            format!("消息 #{at}：{}", "这是一段会自动换行的长文本，".repeat(1 + at % 6))
        } else {
            format!("消息 #{at}：在吗？")
        };
        self.messages.push(text.into());
        // 契约（4.1）：数据变了必须申报，末尾多了一个未测量条目
        self.list_state.splice(at..at, 1);
        // 未测量区域的尺寸估计（4.3）：锚到 item_ix = 总数，
        // 下一帧 layout_items 从末尾向上边测边填满视口
        self.list_state.scroll_to_end();
        cx.notify();
    }
}

impl Render for ChatDemo {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let top = self.list_state.logical_scroll_top();
        div()
            .size_full()
            .flex()
            .flex_col()
            .p_4()
            .gap_2()
            .bg(rgb(0xFFFFFF))
            .child(format!(
                "共 {} 条 | 锚点：第 {} 项 +{:.0}px",
                self.messages.len(),
                top.item_ix,
                top.offset_in_item
            ))
            .child(
                div()
                    .flex()
                    .flex_row()
                    .gap_2()
                    .child(
                        div()
                            .id("short")
                            .px_3()
                            .py_1()
                            .rounded_md()
                            .bg(rgb(0x0080FF))
                            .text_color(rgb(0xFFFFFF))
                            .text_sm()
                            .child("发短消息")
                            .on_click(cx.listener(|this, _, _, cx| this.append(false, cx))),
                    )
                    .child(
                        div()
                            .id("long")
                            .px_3()
                            .py_1()
                            .rounded_md()
                            .bg(rgb(0x0066CC))
                            .text_color(rgb(0xFFFFFF))
                            .text_sm()
                            .child("发长消息")
                            .on_click(cx.listener(|this, _, _, cx| this.append(true, cx))),
                    )
                    .child(
                        div()
                            .id("bottom")
                            .px_3()
                            .py_1()
                            .rounded_md()
                            .bg(rgb(0x888888))
                            .text_color(rgb(0xFFFFFF))
                            .text_sm()
                            .child("回到底部")
                            .on_click(cx.listener(|this, _, _, cx| {
                                this.list_state.scroll_to_end();
                                cx.notify();
                            })),
                    ),
            )
            .child(
                // render_item 用 cx.processor 适配回视图方法（4.5 练习 2）
                list(self.list_state.clone(), cx.processor(|this, ix: usize, _, _| {
                    let text: SharedString = this
                        .messages
                        .get(ix)
                        .cloned()
                        .unwrap_or_else(|| "（超出范围）".into());
                    div()
                        .w_full()
                        .px_3()
                        .py_2()
                        .rounded_md()
                        .bg(if ix % 2 == 0 { rgb(0xF3F4F6) } else { rgb(0xE8EAED) })
                        .text_sm()
                        .child(text)
                        .into_any()
                }))
                .flex_1(),
            )
    }
}

fn run_example() {
    application().run(|cx: &mut App| {
        let bounds = Bounds::centered(None, size(px(400.), px(600.)), cx);
        cx.open_window(
            WindowOptions {
                focus: true,
                window_bounds: Some(WindowBounds::Windowed(bounds)),
                ..Default::default()
            },
            |_, cx| cx.new(|_| ChatDemo::new()),
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

运行与观察（`cargo run -p gpui --example chat_list`）：

1. 连点「发长消息」十余次：长文本换行导致每条高度都不同（变高），列表始终贴底——`scroll_to_end` 把锚点设为 `item_ix = 总数`，未测量的新条目在下一帧由「向上补齐」循环边测边填（4.3 步骤②）；
2. 用滚轮向上翻几屏，观察状态行「锚点：第 N 项」变小；此时再点「发短消息」，因为 `append` 里有 `scroll_to_end`，列表会跳回底部——**把 `scroll_to_end()` 那行注释掉**再试：用户上翻后新消息不再拽走视口，这正是聊天产品「仅在读到底部时才自动跟随」的前半实现；
3. 更进一步：把「是否跟随」改成条件式——只在 `self.list_state.is_scrolled_to_end().unwrap_or(true)` 时才 `scroll_to_end()`，行为即与 `FollowMode::Tail` 的再挂挡语义对齐（4.4）；
4. 试着在 `new()` 里加 `.with_uniform_item_height(px(40.))`（或换成 `Top` 对齐），对比滚动条/首帧锚点行为差异。

以上运行结果「待本地验证」；若编译报错，优先检查 `Cargo.toml` 的 `[[example]]` 是否已声明。

## 6. 本讲小结

- `list` 是 GPUI 的变高虚拟列表：状态以 `ListState`（`Rc<RefCell<StateInner>>`）**侵入式**存在你的视图上，与元素每帧共享同一份缓存；其契约是「区外条目不许擅自变高，变更须 `splice` / `remeasure` 申报」。
- 测量缓存是一棵 `SumTree<ListItem>`：`Measured` 存真实尺寸、`Unmeasured` 只存可选 hint；summary 提供总高与 `Count`/`Height` 双维度 \( O(\log n) \) 寻址，未测量区域的总高只是估计，随滚动逐帧收敛。
- `layout_items` 每帧只构建「视口 + 2×overdraw」内的元素：向下测到超额、不满一屏向上补（`Bottom` 对齐此时把锚点清空回贴底）、窗口外缓存原样保留；焦点条目滚出屏幕会被额外渲染以保住键盘交互。
- 滚动有两条入口（滚轮监听器 / 程序 API），最终都改 `logical_scroll_top` 并 notify 宿主视图；`logical_scroll_top = None` 意为贴自然锚点（Top 贴顶 / Bottom 贴底），`FollowMode::Tail` 提供显式的「跟随-停止-再挂挡」状态机。
- `ListScrollEvent` 把滚动翻译成条目语义（`visible_range`/`count`/`is_scrolled`/`is_following_tail`）；滚动条不内置，由 `max_offset_for_scrollbar` 等四个钩子自绘，拖动期间冻结高度基准防跳变。
- 选型：等高用 `uniform_list`（一行测量推全局），变高才用 `list`（逐项测量 + 缓存 + 申报变更）；Zed 的对话流、日志流、选择器、设置页分别给出了四种参数搭配范例。

## 7. 下一步学习建议

- **u6-l3（deferred 与 anchored）**：变高列表之上做悬浮层——下拉菜单、自动补全弹出层如何锚定到列表条目并在边缘翻转，聊天输入框的 @ 提及菜单就是 `list` + `anchored` 的组合。
- **u7-l4（测试 GPUI 应用）**：本讲多次引用 `list.rs` 内置测试（如 `test_scroll_by_positive_and_negative_distance`、`test_child_scroll_handler_can_stop_list_scroll`），它们是 `TestAppContext` + `simulate_event` 的绝佳样本；学完后可以为综合实践的 ChatDemo 写「发消息后 `logical_scroll_top` 贴底」的断言。
- **读 Zed 真实调用方**：`crates/agent_ui/src/conversation_view.rs`（Top + 2048 overdraw，流式条目如何调 `remeasure_items`）与 `crates/picker/src/picker.rs`（选择器如何混用 `UniformListScrollHandle` 与 `ListState`），检验你能否独立读懂它们的 splice 时机。
- **回看 u6-l1**：带着本讲的「测量-缓存-复用」视角重读 `uniform_list.rs`，体会「算术替代测量」到底省掉了哪些环节。
