# Element trait 三阶段生命周期

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐方法说出 `Element` trait 的三阶段——`request_layout`、`prepaint`、`paint`——各自的职责、入参与出参，以及 `RequestLayoutState` / `PrepaintState` 两个关联类型如何在阶段之间传递状态。
2. 理解「元素树每帧重建、但状态可以跨帧存续」这一 GPUI 核心设计：`ElementId` 如何升级为 `GlobalElementId`，`with_element_state` 如何按 `(GlobalElementId, TypeId)` 存取跨帧状态，未被访问的状态如何在帧结束时被回收。
3. 理解元素 arena（bump 分配器）如何让「每帧创建并丢弃数万个元素」变得廉价，以及 `ArenaBox` 如何用 `valid` 标志防止 use-after-clear。
4. 独立实现一个带 `prepaint` 状态的最小自定义元素 `FixedBox`：在 `request_layout` 向 Taffy 申报固定尺寸，在 `prepaint` 计算居中 bounds，在 `paint` 用 `window.paint_quad` 画纯色矩形。

本讲是第 4 单元「元素机制与绘制管线」的入口：u3 系列讲的是「用什么元素搭界面」（声明式），本讲开始拆「元素本身如何工作」（命令式），后续 u4-l2 讲 Taffy 布局引擎、u4-l3 讲窗口绘制管线、u4-l5 讲自定义绘制实战。

## 2. 前置知识

本讲默认你已完成 u3 系列，以下概念会被直接使用：

- **元素树每帧重建**（u3-l1）：根视图每帧调用 `Render::render` 生成一棵全新的元素树，帧结束后整棵树连同注册的回调一起被丢弃；应用状态则存放在跨帧存活的实体（`Entity<T>`）中。本讲要回答的问题正是：既然元素每帧都死，元素自己的状态放在哪？
- **视图与 ViewElement**（u3-l1）：视图是实现了 `Render` 的实体；`ViewElement` 以实体 id 作为元素 id，为子树提供独立的元素 id 命名空间。本讲的 `GlobalElementId` 会沿着这条 id 链构造。
- **Style 与 StyleRefinement**（u3-l3）：`Style` 是全字段有确定值的完整样式，`StyleRefinement` 是全 `Option` 的补丁。`request_layout` 阶段交给 Taffy 的正是一份合成后的 `Style`。
- **像素类型**（u3-l4）：`Pixels` 是逻辑像素，`Bounds<Pixels>` = origin + size，原点在左上、y 轴向下；`Bounds::center()` 返回中心点。
- **canvas 元素**（u3-l5）：你已见过 canvas 这个「命令式绘制逃生舱」，本讲将把它当作自定义元素的最佳范文逐行精读。

两个本讲新引入的基础概念：

- **Taffy**：GPUI 内置的 Rust 版 flexbox/grid 布局引擎。元素在 `request_layout` 阶段把自己的 `Style` 和孩子的 `LayoutId` 报给 Taffy，Taffy 算出每个节点的位置与尺寸。本讲只用它的接口，细节留给 u4-l2。
- **bump 分配器（arena）**：一块连续内存，分配只是移动指针，释放时整块一起归还。适合「大量短命对象同生共死」的场景——元素树正是如此。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/element.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs) | 本讲主战场：`Element` trait 定义、`Drawable` 状态机、`GlobalElementId`、`IntoElement`、`AnyElement`、`Empty`。 |
| [src/elements/canvas.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/canvas.rs) | 仅 96 行的最小自定义元素范本：两个 `FnOnce` 回调分别映射 prepaint/paint，是本讲实践 `FixedBox` 的参照。 |
| [src/arena.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/arena.rs) | 元素 arena 的完整实现：`Arena`、`Chunk`、`ArenaBox`，以及解释嵌套绘制为何要延迟 clear 的 scope 机制。 |
| [src/window.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs) | 三阶段依赖的 `Window` API（`request_layout`/`compute_layout`/`layout_bounds`/`paint_quad`）、跨帧状态表 `element_states` 与 `with_element_state`、`ElementId` 枚举、线程局部元素 arena 的挂载点。 |
| [src/elements/animation.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/animation.rs) | 弹簧动画元素：在 `request_layout` 里用 `with_element_state` 维护跨帧弹簧物理状态，是「元素每帧重建但状态存续」的官方实战范例。 |
| [examples/painting.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/painting.rs) | canvas 元素的完整使用示例，本讲实践的运行载体。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：4.1 Element trait 总览与三阶段状态机；4.2 三阶段逐个精读；4.3 ElementId/GlobalElementId 与跨帧状态；4.4 元素 arena。

### 4.1 Element trait：三阶段生命周期总览

#### 4.1.1 概念说明

`Element` 是 GPUI 的低层命令式 API，负责「布局 + 绘制」窗口里的一切内容。模块文档开宗明义：

> Elements are the workhorses of GPUI. They are responsible for laying out and painting all of the contents of a window.

它解决的问题是：声明式 UI（div + Render）虽然好用，但当你需要自定义布局算法、虚拟化十万行列表、或者渲染一个代码编辑器时，需要亲手接管「申报尺寸 → 确定位置 → 提交绘制」这条流水线。`Element` trait 就是这条流水线的接口约定。

三个关键设计决定：

1. **三阶段生命周期**：元素被绘制要依次走过 `request_layout`（申报样式与孩子）→ `prepaint`（拿到最终 bounds，登记 hitbox、焦点等「绘制前置物」）→ `paint`（真正提交绘制指令）。每个阶段都能拿到前一个阶段产生的状态。
2. **帧内状态用关联类型传递**：`RequestLayoutState` 与 `PrepaintState` 是两个关联类型，让元素在同一次绘制的三个阶段之间传递任意 Rust 值，且完全类型安全。
3. **每帧重建**：元素树在下一帧开始前被整体丢弃、回调全部注销，下一帧由 `render()` 重新构造。所以元素自身**不是**持久状态的家——持久状态要么放实体（u2-l2），要么用本讲 4.3 的元素状态机制按 id 跨帧续命。

与 `IntoElement` 的关系：`Element: 'static + IntoElement`，`IntoElement` 是「能转换成元素」的更弱约定（比如 `&str` 可以 `into_element` 成文本元素），`Element` 才拥有三阶段方法。

#### 4.1.2 核心流程

一次窗口绘制中，某个元素的生命周期是一个严格有序的状态机：

```text
Start ──request_layout()──▶ RequestLayout ──(框架在根部 compute_layout)──▶ LayoutComputed
                                   │                                              │
                                   └──────────────── prepaint() ──────────────────┘
                                                          │
                                                        Prepaint
                                                          │
                                                       paint()
                                                          │
                                                        Painted
```

阶段职责速查表：

| 阶段 | 元素要做什么 | 拿到的输入 | 产出的状态 | 允许调用的关键 Window API |
| --- | --- | --- | --- | --- |
| `request_layout` | 把合成后的 `Style` 与孩子的 `LayoutId` 报给 Taffy | `Option<&GlobalElementId>` | `(LayoutId, RequestLayoutState)` | `window.request_layout` / `window.request_measured_layout` |
| `prepaint` | 拿到本元素最终 `Bounds`，登记 hitbox、计算绘制所需的中间量 | `bounds: Bounds<Pixels>` + `RequestLayoutState` | `PrepaintState` | `window.insert_hitbox`、`window.with_element_state` |
| `paint` | 提交绘制指令（quad/path/sprite）、注册鼠标键盘监听 | `bounds` + 两个状态的可变引用 | （无返回值） | `window.paint_quad`、`window.paint_path`、`window.on_mouse_event` |

两个容易误解的点，先在这里纠正：

- **三阶段是「全树分层推进」，不是「逐元素走完三步再轮到下一个」**。整棵树先完成 `request_layout`（父元素在自己的 `request_layout` 里递归调用孩子的 `request_layout` 收集 `LayoutId`），随后框架在根部调用一次 `compute_layout` 让 Taffy 算出所有节点的几何，接着整棵树走 `prepaint`，最后整棵树走 `paint`。驱动细节在 u4-l3 精读。
- **窗口的 `DrawPhase` 只区分 `Prepaint` 和 `Paint` 两档**（外加 `Focus`/`None`）：`request_layout` 阶段在窗口看来也算 `Prepaint`。框架用 debug_assert 防止你在错误阶段调用错误的 API。

#### 4.1.3 源码精读

先看 trait 本体。[src/element.rs:47-58](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L47-L58) 定义了 trait 与两个关联类型：`RequestLayoutState` 是 `request_layout` 的返回状态、随后以可变引用传给 `prepaint` 和 `paint`；`PrepaintState` 是 `prepaint` 的返回状态、随后以可变引用传给 `paint`。二者都要求 `'static`，因此可以装 `AnyElement`、`Style` 等任意 owned 数据。

[src/element.rs:71-104](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L71-L104) 是三个生命周期方法的完整签名，注意每个方法的前两个参数都是 `Option<&GlobalElementId>` 与 `Option<&InspectorElementId>`——id 只在元素通过 `id()` 方法申报了身份时才非 `None`。[src/element.rs:60-69](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L60-L69) 中，`id()` 的文档说明了唯一性约束：id 必须在「第一个带 id 的祖先元素」的孩子中唯一；`source_location()` 则用于 inspector 定位源码。

[src/element.rs:106-136](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L106-L136) 是无障碍相关的一组带默认实现的方法（`a11y_role`/`write_a11y_info`/`a11y_synthetic_children`），默认不参与无障碍树，u6-l8 再展开。[src/element.rs:139-141](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L139-L141) 的 `into_any` 把元素装箱成类型擦除的 `AnyElement`。

接口的调用顺序不是靠文档约定，而是靠状态机硬性保证。[src/element.rs:260-286](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L260-L286) 的 `ElementDrawPhase` 枚举就是这张状态机：`Drawable<E>` 包装任意元素，`phase` 字段记录当前所处阶段，每次 `mem::take` 消费旧状态、写入新状态。三个推进方法在不满足前置条件时直接 panic：

- [src/element.rs:297-342](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L297-L342)（`Drawable::request_layout`）：从 `Start` 出发才合法，否则 `"must call request_layout only once"`；它先把自己的 id 压入 `window.element_id_stack` 构造 `GlobalElementId`（4.3 详述），再调用元素的同名方法。
- [src/element.rs:344-455](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L344-L455)（`Drawable::prepaint`）：要求先 `request_layout`，否则 `"must call request_layout before prepaint"`；它用 `window.layout_bounds(layout_id)` 取回 Taffy 算好的 bounds 传给元素，并在前后压入/弹出派发树节点（`dispatch_tree.push_node`/`pop_node`）——这是 u5 键位派发的挂载点。
- [src/element.rs:457-497](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L457-L497)（`Drawable::paint`）：要求先 `prepaint`，否则 `"must call prepaint before paint"`；它调用 `set_active_node` 恢复 prepaint 时压入的派发节点，paint 结束后返回 `(RequestLayoutState, PrepaintState)` 供调用方（如渲染缓存）复用。

[src/element.rs:499-550](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L499-L550) 的 `layout_as_root` 是窗口根视图专用的入口：先 `request_layout`，再用给定的可用空间 `compute_layout`，返回根尺寸；若可用空间没变则跳过重算——这是 u4-l3 绘制管线的起点之一。

模块级文档 [src/element.rs:1-32](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L1-L32) 值得完整读一遍，它确认了两件事：元素树在下一帧开始前连同注册的回调一起被整体丢弃；官方建议优先用内置元素和 `RenderOnce` 组件，只有在需要自定义布局/绘制时才实现自己的元素。

#### 4.1.4 代码实践

用 painting 示例直接观察三阶段的调用顺序。

1. **实践目标**：亲眼确认 `request_layout → prepaint → paint` 的执行顺序，以及「元素每帧重建、回调每帧重新执行」。
2. **操作步骤**：
   1. 打开 [examples/painting.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/painting.rs)，找到第 356 行附近的 `canvas(...)` 调用。
   2. 在prepaint 回调（当前的 `move |_, _, _| {}`）里加一行 `eprintln!("[prepaint]");`，在 paint 回调体第一行加 `eprintln!("[paint]");`。
   3. 运行：`cargo run -p gpui --example painting`（在本仓库根目录执行；首次编译较久）。
   4. 在窗口里按住鼠标拖动画一条线（这会触发 `cx.notify()` 引发重绘）。
3. **需要观察的现象**：终端里 `[prepaint]` 与 `[paint]` 成对出现；每次拖动都会新增一对——说明每帧都会重新走一遍 prepaint 与 paint。
4. **预期结果**：两行日志严格成对且 prepaint 在前；不操作窗口时（理想情况下）没有新日志，因为 GPUI 只在需要时才重绘。
5. 运行结果与编译时间取决于本地环境，具体输出「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RequestLayoutState` 和 `PrepaintState` 要设计成关联类型，而不是让 trait 方法直接返回 `Box<dyn Any>`？

**参考答案**：关联类型让状态在编译期就确定具体类型，`prepaint`/`paint` 拿到的是 `&mut Self::RequestLayoutState` 这样的强类型引用，无运行时向下转型开销、无类型不匹配的 panic；同时状态可以是 `Sized` 任意值（如 canvas 的 `Style`、动画元素的 `AnyElement`）。`AnyElement` 只在「孩子类型各异」的容器场景（div 的 children）才用类型擦除，那是显式且局部的设计选择。

**练习 2**：如果在一个元素的 `paint` 方法里调用 `self.request_layout`（即直接调用 `Element::request_layout` 而非 `window.request_layout`），会发生什么？

**参考答案**：`Drawable` 状态机此时已处于 `Prepaint`/`Painted` 分支，`mem::take` 匹配不到 `Start`，直接 panic 并报 `"must call request_layout only once"`。这正是状态机的意义：把「必须按顺序调用」从文档约定升级为运行时保证。

**练习 3**：`Element` 与 `IntoElement` 两个 trait 的关系是什么？为什么 `Element: IntoElement` 而不是反过来？

**参考答案**：`IntoElement`（[src/element.rs:144-157](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L144-L157)）表示「能转换成一个元素」，只有一个 `into_element()` 方法，让 `&str`、`SharedString` 等轻量类型可以出现在 `child()` 里；`Element` 在此之上增加三阶段生命周期与关联状态。所有元素都能「转换成自己」（恒等转换），所以 `Element: IntoElement`；反过来 `IntoElement` 的实现者可能只是数据（如字符串），没有生命周期可言。

### 4.2 三阶段逐个精读：request_layout、prepaint、paint

#### 4.2.1 概念说明

上一模块看了状态机骨架，本模块回答每个阶段「具体做什么、能调用什么」。

- **request_layout——申报，不计算**。此阶段元素只知道自己的 `Style` 和孩子是谁，还不知道自己的位置和大小。它做两件事：为每个孩子（递归地）取得 `LayoutId`；把「自己的样式 + 孩子的 id 列表」报给 Taffy，换回自己的 `LayoutId`。真正的几何计算由框架稍后在根部统一执行。
- **prepaint——拿到答案，做绘制准备**。框架已经 `compute_layout` 完毕，元素通过 `window.layout_bounds(layout_id)` 拿到自己在窗口坐标系中的 `Bounds<Pixels>`。此阶段适合：登记 hitbox（供鼠标命中测试）、计算绘制所需的中间量（如居中子矩形）、读写跨帧元素状态、申请自动滚动。
- **paint——提交绘制指令**。向当前帧的 `Scene` 添加图元（quad、path、sprite），并注册鼠标/键盘监听（div 系元素的监听正是在 paint 阶段注册的，u5-l2 详述）。paint 拿到两个状态的可变引用，因此可以消费 prepaint 的计算结果。

为什么要把 prepaint 和 paint 分成两次全树遍历？因为 hitbox、焦点等「绘制前置物」必须在任何元素开始 paint 之前全部就位——比如后绘制的兄弟元素需要知道前面元素的 hitbox 才能正确处理层叠遮挡。分成两遍保证所有元素的准备工作先整体完成。

#### 4.2.2 核心流程

以 canvas 元素为例的状态流（T 是用户 prepaint 回调的返回值）：

```text
request_layout:  StyleRefinement ──refine──▶ Style ──▶ window.request_layout(style, []) ──▶ (LayoutId, Style)
                                                                                │
prepaint:  bounds（框架算好传入）+ prepaint 回调(bounds, window, cx) ──▶ Option<T>          │
                                                                                │
paint:  style.paint(bounds, ...) 先画背景，再 paint 回调(bounds, T, window, cx) ────────────┘
```

三阶段与 Taffy 的时序关系：

```text
全树 request_layout（构造 Taffy 节点树）
        │
根部 compute_layout(available_space)      ← Taffy 在此刻算出全部几何
        │
全树 prepaint（此时 layout_bounds 才有值）
        │
全树 paint
```

#### 4.2.3 源码精读

**先看 Window 侧的三个关键 API。** [src/window.rs:4615-4640](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L4615-L4640) 的 `Window::request_layout` 接收合成后的 `Style` 与孩子 `LayoutId` 集合，交给布局引擎（`self.layout_engine`，即 Taffy 封装）建节点，返回新的 `LayoutId`。注意它开头调用 `self.invalidator.debug_assert_prepaint()`——布局申报只允许在 request_layout/prepaint 阶段调用。[src/window.rs:4642-4663](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L4642-L4663) 的 `request_measured_layout` 是变体：接受一个测量闭包，在布局时用任意逻辑决定尺寸（文本测量内部就用它）。[src/window.rs:4665-4681](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L4665-L4681) 的 `compute_layout` 由框架在根部调用，让 Taffy 在给定可用空间内解出整棵树的几何。[src/window.rs:4683-4700](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L4683-L4700) 的 `layout_bounds` 把 `LayoutId` 翻译成窗口坐标系里的 `Bounds<Pixels>`（含像素对齐与元素偏移修正）——`Drawable::prepaint` 传给元素的 `bounds` 参数正是来自这里。

**paint 阶段的绘制出口。** [src/window.rs:4079](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L4079) 的 `Window::paint_quad` 把一个 `PaintQuad`（纯色/渐变矩形，可带圆角与边框）加进当前帧场景；[src/window.rs:6809-6824](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L6809-L6824) 的自由函数 `quad(...)` 是构造 `PaintQuad` 的便捷构造器，painting 示例里 `window.paint_quad(quad(bounds, px(0.), color, px(0.), transparent, Default::default()))` 一行就是「画一个无边框纯色矩形」。

**再看 canvas 元素如何把三阶段填满。** [src/elements/canvas.rs:8-27](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/canvas.rs#L8-L27)：构造函数 `canvas(prepaint, paint)` 接收两个 `FnOnce` 回调，分别签名为 `(Bounds, &mut Window, &mut App) -> T` 与 `(Bounds, T, &mut Window, &mut App)`；两个字段都是 `Option<Box<...>>`，用 `Some` 包裹是为了后面用 `take()` 拿走所有权。

[src/elements/canvas.rs:37-39](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/canvas.rs#L37-L39) 声明关联类型：`RequestLayoutState = Style`（保存合成后的完整样式，paint 时要用它画背景）、`PrepaintState = Option<T>`（用户 prepaint 回调的返回值，`Option` 是为了能被 `take`）。

- [src/elements/canvas.rs:49-60](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/canvas.rs#L49-L60)（request_layout）：`Style::default()` 出发 `refine(&self.style)` 合成链式样式补丁（u3-l3 的知识在这里落地），然后 `window.request_layout(style.clone(), [], cx)`——孩子为空，因为 canvas 是叶子元素。样式连同 `LayoutId` 一起作为状态返回。
- [src/elements/canvas.rs:62-72](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/canvas.rs#L62-L72)（prepaint）：框架已把 `bounds` 传进来，这里直接执行用户 prepaint 回调并把返回值 `T` 存入 `PrepaintState`。
- [src/elements/canvas.rs:74-88](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/canvas.rs#L74-L88)（paint）：先 `take` 出 prepaint 结果与 paint 回调，`style.paint(bounds, window, cx, |window, cx| ...)` 用状态里的 `Style` 画背景（背景/边框等视觉样式由它统一处理），回调里再执行用户 paint 闭包。两个 `take().unwrap()` 体现 `FnOnce` 的单次语义——回调被消费后元素即失效，反正下一帧会重建新元素。

[src/elements/canvas.rs:91-95](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/canvas.rs#L91-L95) 为 canvas 实现 `Styled`，所以它也能 `.size_full()`、`.bg(...)`——自定义元素照样可以接入 Tailwind 风格样式链。

[src/window.rs:4702-4719](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L4702-L4719) 的 `insert_hitbox` 是 prepaint 阶段另一件常事：把 bounds 连同 `HitboxBehavior` 登记到 `next_frame.hitboxes`，返回可在 paint 与事件回调中查询的 `Hitbox`。canvas 没有用它，但 div、list 等交互元素都靠它做命中测试（u5-l2）。

#### 4.2.4 代码实践

实现 `FixedBox` 的三阶段骨架（本讲综合实践的第 1 步，先在本地任一示例文件里试验）。

1. **实践目标**：写出第一个自定义元素：向 Taffy 申报固定尺寸、在 prepaint 计算居中内接矩形、在 paint 用 `paint_quad` 画出来。
2. **操作步骤**：
   1. 复制 `examples/hello_world.rs` 为 `examples/fixed_box.rs`（本仓库 gpui 的 Cargo.toml 未关闭 example 自动发现，新文件一般会被自动识别；若 `cargo run -p gpui --example fixed_box` 找不到 target，则仿照 [Cargo.toml:177-179](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/Cargo.toml#L177-L179) 补一段 `[[example]]`）。
   2. 在文件中加入如下代码（**示例代码**，非项目原有代码）：

   ```rust
   use gpui::{
       App, Bounds, Context, Element, ElementId, GlobalElementId, IntoElement, LayoutId, Pixels,
       Render, Size, Style, Window, WindowOptions, div, point, prelude::*, px, quad, rgb, size,
   };
   use gpui_platform::application;

   /// 一个固定尺寸的自定义元素：申报 240x160，内部画一个居中的 80x40 矩形
   pub struct FixedBox {
       id: ElementId,
       box_size: Size<Pixels>,
   }

   impl FixedBox {
       pub fn new(id: impl Into<ElementId>, box_size: Size<Pixels>) -> Self {
           Self { id: id.into(), box_size }
       }
   }

   impl IntoElement for FixedBox {
       type Element = Self;
       fn into_element(self) -> Self { self }
   }

   impl Element for FixedBox {
       type RequestLayoutState = ();
       type PrepaintState = Bounds<Pixels>; // 居中内接矩形，paint 时使用

       fn id(&self) -> Option<ElementId> {
           Some(self.id.clone())
       }

       fn source_location(&self) -> Option<&'static core::panic::Location<'static>> {
           None
       }

       fn request_layout(
           &mut self,
           _id: Option<&GlobalElementId>,
           _inspector_id: Option<&gpui::InspectorElementId>,
           window: &mut Window,
           cx: &mut App,
       ) -> (LayoutId, Self::RequestLayoutState) {
           // 阶段 1：向 Taffy 申报「我要一个固定尺寸的节点」
           let style = Style {
               size: size(self.box_size.width.into(), self.box_size.height.into()),
               ..Default::default()
           };
           let layout_id = window.request_layout(style, [], cx);
           (layout_id, ())
       }

       fn prepaint(
           &mut self,
           _id: Option<&GlobalElementId>,
           _inspector_id: Option<&gpui::InspectorElementId>,
           bounds: Bounds<Pixels>,
           _request_layout: &mut Self::RequestLayoutState,
           _window: &mut Window,
           _cx: &mut App,
       ) -> Self::PrepaintState {
           // 阶段 2：框架已算好 bounds，计算居中的 80x40 内接矩形
           let inner = size(px(80.), px(40.));
           let center = bounds.center();
           let origin = center - point(inner.width * 0.5, inner.height * 0.5);
           Bounds { origin, size: inner }
       }

       fn paint(
           &mut self,
           _id: Option<&GlobalElementId>,
           _inspector_id: Option<&gpui::InspectorElementId>,
           _bounds: Bounds<Pixels>,
           _request_layout: &mut Self::RequestLayoutState,
           prepaint: &mut Self::PrepaintState,
           window: &mut Window,
           _cx: &mut App,
       ) {
           // 阶段 3：消费 prepaint 结果，提交绘制
           window.paint_quad(quad(
               *prepaint,
               px(0.),
               rgb(0x2E7D32),
               px(0.),
               gpui::transparent_black(),
               Default::default(),
           ));
       }
   }

   struct FixedBoxView;

   impl Render for FixedBoxView {
       fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
           div()
               .flex()
               .flex_col()
               .size_full()
               .items_center()
               .justify_center()
               .bg(rgb(0x1E1E1E))
               .child(FixedBox::new("fixed-box", size(px(240.), px(160.))))
       }
   }

   fn main() {
       application().run(|cx| {
           cx.open_window(WindowOptions::default(), |_window, cx| {
               cx.new(|_cx| FixedBoxView)
           })
           .unwrap();
           cx.activate(true);
       });
   }
   ```

   3. 运行：`cargo run -p gpui --example fixed_box`。
3. **需要观察的现象**：深色窗口中央出现一个 240x160 的区域，其中间画着一个 80x40 的绿色矩形；矩形严格居中。
4. **预期结果**：绿色矩形位于申报尺寸的正中；缩放窗口时布局由外层 div 的 flex 居心重新计算，FixedBox 依旧申报固定 240x160。若把 `.items_center().justify_center()` 删掉，FixedBox 会移到左上角——证明它的位置完全由 Taffy 布局决定，元素只负责申报尺寸。编译与显示效果「待本地验证」。
5. 注意 `Size<Pixels>` 到 `Size<Length>` 的转换用了 `Pixels → Length` 的 `From` 实现（[src/geometry.rs:3755](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/geometry.rs#L3755)）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FixedBox::prepaint` 不自己调用 `window.layout_bounds`，而是使用框架传入的 `bounds` 参数？

**参考答案**：两件事都有人做：`Drawable::prepaint`（[src/element.rs:364](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L364)）在调用元素方法前已经 `window.layout_bounds(layout_id)` 并把结果作为 `bounds` 参数传入。使用参数是官方约定（canvas、div 都这么写），省一次查询也避免了「忘记查询导致 bounds 错误」的坑。元素自己查询 `layout_bounds` 是给「需要在 prepaint 里查孩子 bounds」的场景预留的。

**练习 2**：canvas 的 `PrepaintState` 为什么是 `Option<T>` 而不是直接 `T`？

**参考答案**：因为 paint 阶段要用 `take()` 把值从状态里搬走交给用户回调（`FnOnce` 需要值所有权），搬走后状态位置必须仍有一个合法值，`Option<T>` 用 `None` 填坑。这是 Rust 所有权语义下「跨阶段传递单次消费值」的常用手法，你的 `FixedBox` 中 `PrepaintState = Bounds<Pixels>` 之所以不需要 `Option`，是因为 paint 只复制（`*prepaint`）而不消费它。

**练习 3**：如果元素的 paint 阶段才申请 hitbox（调用 `insert_hitbox`），会怎样？

**参考答案**：会触发 debug 断言失败。[src/window.rs:4706](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L4706) 的文档与 `debug_assert_prepaint`（[src/window.rs:263-268](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L263-L268)）要求它只能在 prepaint 阶段调用——hitbox 必须在任何人开始 paint 前全部登记完毕，否则层叠遮挡判断不完整。release 构建下断言被剥离，但行为已不可靠，属于未定义使用方式。

### 4.3 ElementId、GlobalElementId 与跨帧元素状态

#### 4.3.1 概念说明

元素每帧重建，但有些状态「太简单、太量大」，不值得每个都建一个实体来存——比如「这个 div 是否处于 hover」。GPUI 为此提供第三种状态存储：**元素状态（element state）**，按 `(GlobalElementId, TypeId)` 为键存在窗口里，只要元素在连续帧中以相同 id 出现，状态就自动延续；哪一帧没被访问，帧结束就被回收。

三个角色分工：

- **`ElementId`**：元素自己申报的「本地名」，可以是整数、字符串、UUID、焦点句柄、代码位置等十种形态。
- **`GlobalElementId`**：本地名不够——两个不同列表的 `Item(0)` 会撞车。GPUI 在绘制时维护一个 `element_id_stack`，每个带 id 的元素进入时压栈、退出时弹栈，`GlobalElementId` 就是整条栈路径的快照（`Arc<[ElementId]>`），用「路径」保证全局唯一。u3-l1 讲过 `ViewElement` 以实体 id 作为元素 id，等于为每个视图的子树开启了一条独立的路径命名空间。
- **`with_element_state`**：存取接口。传 id 与闭包，闭包收到 `Option<S>`（首帧为 `None`），返回新状态；框架负责把状态搬到下一帧。

#### 4.3.2 核心流程

```text
帧 N：元素 X（id = "box"）prepaint/paint
        │ window.with_element_state(global_id, |state: Option<S>, window| ...)
        │ 查 next_frame.element_states，未命中则查 rendered_frame（上一帧）.element_states
        │ 闭包处理 Some(旧状态) 或 None，返回新状态 → 写回 next_frame.element_states
        ▼
帧结束：NextFrame::finish —— 只有本帧被访问过的 key 才从上一帧搬进新帧
        ▼
帧 N+1：元素 X 再次以相同 GlobalElementId 出现 → 取到 Some(状态)，延续
        元素 X 消失（不再渲染）→ 状态无人认领 → 被丢弃
```

状态键的构成：

\[ \text{key} = (\text{GlobalElementId},\ \text{TypeId::of::<S>()}) \]

同一个元素的同一份 id 下可以并存多种类型的状态（例如 div 同时存交互状态 `InteractiveElementState`），互不冲突；反过来，同一 id 下对同一类型重入调用会被检测并 panic（防止把状态借出两次）。

#### 4.3.3 源码精读

**ElementId 的十种形态。** [src/window.rs:6588-6614](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L6588-L6614) 定义了枚举：`View(EntityId)`（视图元素专用）、`Integer`（列表下标）、`Name`（字符串名）、`FocusHandle`（焦点句柄）、`NamedInteger`（如 `"item-3"`）、`CodeLocation`（源码位置，`use_state` 默认用它）等。`usize`/`i32`/`SharedString` 都有 `From` 转换（[src/window.rs:6654-6669](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L6654-L6669)），所以 `FixedBox::new("fixed-box", ...)` 里 `impl Into<ElementId>` 能直接吃 `&str`。

**GlobalElementId 是路径快照。** [src/element.rs:211-213](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L211-L213) 定义 `GlobalElementId(pub(crate) Arc<[ElementId]>)`；其 `Display` 实现（[src/element.rs:215-225](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L215-L225)）用点号连接各级 id（如 `view-12.list.item-3`），调试日志里看到的就是它。构造现场在 [src/element.rs:297-342](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L297-L342)：`Drawable::request_layout` 把 `self.element.id()` 压入 `window.element_id_stack`，随后 `GlobalElementId(Arc::from(&*window.element_id_stack))` 复制整条栈作为全局 id，方法返回前弹栈。prepaint/paint 阶段（[src/element.rs:359-362](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L359-L362)、[src/element.rs:472-475](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L472-L475)）会重新压栈并用 `debug_assert_eq!` 校验路径与 request_layout 时一致——三阶段拿到的必须是同一个 id。

**状态的存取。** [src/window.rs:3766-3843](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L3766-L3843) 的 `with_element_state` 是核心：用 `(global_id.clone(), TypeId::of::<S>())` 作 key（[src/window.rs:953](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L953) 的 `element_states: FxHashMap<(GlobalElementId, TypeId), ElementStateBox>` 就是状态表本体）；先查当前帧 `next_frame`，未命中再查上一帧 `rendered_frame`（[src/window.rs:3783-3787](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L3783-L3787)）；闭包拿到 `Option<S>` 后返回 `(结果, 新状态)` 写回 `next_frame`。对同一 key 重入的调用会在 [src/window.rs:3817-3819](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L3817-L3819) 处 panic（`"reentrant call to with_element_state..."`），因为状态此刻正被借出。id 可能缺失的场景用 [src/window.rs:3845](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L3845) 起的 `with_optional_element_state`（div、img、text 都用它）。

**回收时机。** [src/window.rs:1095-1105](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L1095-L1105) 的 `NextFrame::finish` 只遍历 `accessed_element_states`（本帧实际访问过的 key），把上一帧对应状态搬进新帧——没有被动过的状态就此蒸发。这就是「连续帧渲染才存活」的准确含义，无需手动清理。

**官方实战范例：弹簧动画。** [src/elements/animation.rs:242-283](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/animation.rs#L242-L283)：`SpringAnimationElement` 在 `request_layout` 里调用 `window.with_element_state(global_id.unwrap(), ...)`，`SpringElementState`（位置、速度、目标、上次更新时间，[src/elements/animation.rs:242-249](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/animation.rs#L242-L249)）跨帧延续，每帧根据流逝时间积分一次弹簧物理。元素每帧都在重建，但物理模拟从未中断——这是本模块最好的心智图像。u6-l4 会完整拆它。

顺带一提 [src/window.rs:3748-3764](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L3748-L3764) 的 `use_state`：它是 `with_element_state` 的糖，把状态升级为一个 `Entity<S>`（首帧初始化，后续直接复用实体），适合状态本身需要被其他实体观察的场景；默认用调用处源码位置当 id，列表等需要区分实例的场合应改用 `use_keyed_state`。

#### 4.3.4 代码实践

给 `FixedBox` 加跨帧帧计数器。

1. **实践目标**：验证「元素每帧重建，但按 id 存的状态跨帧存续」；并验证「元素不再渲染时状态被回收」。
2. **操作步骤**：
   1. 在 4.2.4 的 `FixedBox::prepaint` 开头加入（**示例代码**）：

   ```rust
   let frame_count = window.with_element_state(
       _id.unwrap(),
       |state: Option<u32>, _window| {
           let next = state.unwrap_or(0) + 1;
           (next, next) // (返回给调用方的结果, 存回状态表的新状态)
       },
   );
   eprintln!("FixedBox 第 {} 帧渲染", frame_count);
   ```

   2. 重新运行示例，观察终端输出几秒。
   3. 再做一个小实验：把 `FixedBoxView::render` 里的 `.child(FixedBox::new(...))` 换成两个不同 id 的 `FixedBox`（如 `"box-a"`、`"box-b"`），观察计数是否各自独立从 1 开始。
3. **需要观察的现象**：帧号持续递增而不是停在 1——虽然每帧都是新建的 `FixedBox` 结构体，状态却接续上了；两个不同 id 的计数互不干扰。GPUI 有空闲节流，未操作窗口时计数增长会放缓或暂停，属正常现象。
4. **预期结果**：计数从 1 开始单调递增；换 id 后重新从 1 开始（新 key 首帧为 `None`）。「待本地验证」。
5. 注意 `with_element_state` 允许在 request_layout、prepaint、paint 三个阶段调用（[src/window.rs:271-279](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L271-L279) 的断言文案写明了这一点），放在 prepaint 只是本例的选择。

#### 4.3.5 小练习与答案

**练习 1**：在一个渲染 10000 行的 `uniform_list` 里，每行的元素都用 `ElementId::Integer(ix)` 作 id。为什么不会和其他列表的 `Integer(ix)` 冲突？

**参考答案**：`GlobalElementId` 是完整路径而不只是本地名：列表自身的 id、再到 `ViewElement` 的实体 id（`ElementId::View(EntityId)`）都在路径里。两个列表只要祖先链不同（不同实体或不同名字），`Arc<[ElementId]>` 整体就不同。这正是「id 只需在第一个带 id 的祖先内唯一」这条规则（[src/element.rs:63-65](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L63-L65)）的含义。

**练习 2**：元素状态和实体（`Entity<T>`）都能跨帧存活，什么时候选哪个？

**参考答案**：元素状态适合「与元素渲染位置绑定、随元素消失即可丢弃」的短期状态（hover、动画进度、滚动位置缓存），生命周期由框架自动管理、无人访问即回收；实体适合「应用语义状态」（数据模型），需要被多处引用、观察（`observe`/`subscribe`）、跨窗口共享。误用元素状态存应用数据会导致元素一不渲染数据就丢；误用实体存 hover 之类碎状态则会造成实体数量膨胀与泄漏排查困难（u2-l2 的经验）。

**练习 3**：同一帧内两次调用 `with_element_state` 且 id 与类型都相同，会发生什么？为什么这样设计？

**参考答案**：第二次调用 panic（[src/window.rs:3817-3819](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L3817-L3819)）。因为状态被取出的瞬间它不在表里，若允许重入，第二次会拿到 `None` 当成「首帧初始化」，闭包返回后把第一次的状态整个覆盖丢失。宁可 panic 也不静默丢数据，这与 GPUI「杜绝可变别名」的整体哲学一致（u2-l2 的租约模型同款思路）。

### 4.4 元素 arena：每帧元素的生与死

#### 4.4.1 概念说明

「每帧重建整棵元素树」意味着每帧要分配并随后销毁成千上万个元素（每个 div、每段文本都是）。如果走标准库的逐个 `Box` 分配，分配器碎片与开销都会很可观。GPUI 的答案是**元素 arena**：一个线程局部的 bump 分配器，帧开始时整块可用，帧结束 `clear` 一次性归还所有内存。

- **分配即移动指针**：`alloc` 在当前 chunk 里对齐、划出一段内存、写入值，O(1) 且无 per-allocation 元数据。
- **释放即失效标志**：`clear` 把所有 chunk 的偏移指针拨回起点，并翻转 `valid` 标志；所有还捏在外面的 `ArenaBox` 再被解引用就 panic——用最便宜的机制防 use-after-clear。
- **析构不省略**：bump 分配器通常的痛点是「不跑析构」，arena 用一个 `elements: Vec<ArenaElement>` 记录每个分配的 drop 函数指针，clear 时逐个调用，`DropGuard` 这类有副作用的对象也能正确析构。

`AnyElement`（div 的孩子、列表的行的最终归宿）就住在这个 arena 里：`AnyElement(ArenaBox<dyn ElementObject>)`。所以「元素树每帧被丢弃」的物理实现就是一次 `arena.clear()`。

#### 4.4.2 核心流程

一次分配的旅程：

```text
AnyElement::new(element)
  └─ with_element_arena(|arena| arena.alloc(|| Drawable::new(element)))
       └─ Chunk::allocate(layout)          // 当前 chunk 装得下：对齐、划内存、移指针
            └─ 装不下 → 追加新 chunk（容量翻倍式增长）→ 再试
       └─ ptr::write(ptr, f())             // 原地构造
       └─ elements.push({value, drop_fn})  // 登记析构函数
       └─ 返回 ArenaBox { ptr, valid }     // 带失效标志的裸指针包装
```

帧结束：

```text
arena.clear()
  ├─ scope_depth == 0 → force_clear():
  │     valid 置 false（所有旧 ArenaBox 立即失效）
  │     elements.clear()（逐个调用 drop）
  │     各 chunk 偏移指针 reset 回起点
  └─ scope_depth > 0 → 推迟（嵌套绘制未结束，外层还要用这块内存）
```

chunk 增长遵循：

\[ \text{capacity} = \text{chunks.len()} \times \text{chunk\_size} \]

单个 chunk 装不下某次分配时直接 panic（`chunk_size` 配置过小的场景），默认 chunk 大小为 1MB（[src/window.rs:315-316](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L315-L316) 的 `Arena::new(1024 * 1024)`）。

#### 4.4.3 源码精读

**Arena 的骨架。** [src/arena.rs:81-88](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/arena.rs#L81-L88)：`chunks: Vec<Chunk>` 是一组连续内存块，`elements: Vec<ArenaElement>` 登记析构（[src/arena.rs:10-20](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/arena.rs#L10-L20) 的 `ArenaElement` 就是 `裸指针 + drop 函数指针`），`valid: Rc<Cell<bool>>` 是所有 `ArenaBox` 共享的失效标志，`scope_depth` 记录嵌套绘制深度。

**chunk 的 bump 逻辑。** [src/arena.rs:55-74](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/arena.rs#L55-L74) 的 `Chunk::allocate`：先用整数地址做对齐与越界检查（注释特意解释了为什么必须在指针运算前完成检查——越过分配末尾构造指针本身就是 UB），装得下就移动 `offset` 并返回指针，装不下返回 `None` 交给上层开新 chunk。[src/arena.rs:76-79](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/arena.rs#L76-L79) 的 `reset` 只把 offset 拨回起点——内存不还给系统，下一帧接着用。

**分配主路径。** [src/arena.rs:160-210](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/arena.rs#L160-L210) 的 `Arena::alloc`：当前 chunk 失败就 `current_chunk_index += 1`、必要时 `chunks.push(Chunk::new(chunk_size))`（并打 trace 日志 `"increased element arena capacity to ..."`），仍失败才 panic；成功后 `ptr::write` 原地构造、登记 drop、返回 `ArenaBox`。闭包式的 `alloc(|| value)` 保证值直接在 arena 内存上构造，无额外拷贝。

**失效防护。** [src/arena.rs:213-234](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/arena.rs#L213-L234)：`ArenaBox` 持裸指针与 `valid` 的 `Rc` 克隆，`Deref`/`DerefMut`（[src/arena.rs:236-252](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/arena.rs#L236-L252)）先 `validate()`。[src/arena.rs:150-158](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/arena.rs#L150-L158) 的 `force_clear` 先把旧 `valid` 置 false 再换一个新的——从此任何仍指向旧代的 `ArenaBox` 解引用即 panic，测试 `test_arena_use_after_clear`（[src/arena.rs:328-336](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/arena.rs#L328-L336)）断言了这条 panic 消息。

**为什么 clear 可能被推迟。** [src/arena.rs:113-148](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/arena.rs#L113-L148)：绘制过程中可能发生嵌套绘制（典型：Windows 平台的消息泵在 draw 期间重入窗口过程；或在 draw 里打开新窗口）。内层绘制结束时若立即 clear，会把外层还在引用的内存一起释放。于是 `begin_scope`/`end_scope` 维护深度计数，`clear` 发现 `scope_depth > 0` 就只打一条 debug 日志推迟，由最外层绘制的 clear 一并回收；不配对的 `end_scope` 直接 panic（[src/arena.rs:126-131](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/arena.rs#L126-L131) 的注释解释了为什么必须响亮失败）。测试 `test_clear_deferred_while_scope_active`（[src/arena.rs:338-377](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/arena.rs#L338-L377)）完整演绎了这个场景。

**挂载点。** [src/window.rs:315-321](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L315-L321)：线程局部 `ELEMENT_ARENA` 默认 1MB chunk；`CURRENT_ELEMENT_ARENA` 在每次窗口 draw 时指向当前 App 自己的 arena，让多个测试 App 的 arena 互不污染。[src/window.rs:338-350](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L338-L350) 的 `with_element_arena` 是统一入口。元素侧的消费者在 [src/element.rs:587-599](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L587-L599)：`AnyElement::new` 把 `Drawable` 分配进 arena 并擦除为 `dyn ElementObject`。

**测试是最好的文档。** [src/arena.rs:261-293](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/arena.rs#L261-L293) 的 `test_arena` 覆盖分配、clear 后复用、Drop 被调用三个关键行为；`test_arena_grow`（[src/arena.rs:295-307](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/arena.rs#L295-L307)）验证 chunk 增长（8 字节 chunk 放两个 u64 后容量 16）。

#### 4.4.4 代码实践

运行 arena 的单元测试并观察容量增长日志。

1. **实践目标**：确认 arena 的分配/回收/析构行为有测试背书；直观感受 chunk 增长机制。
2. **操作步骤**：
   1. 在仓库根目录运行：`cargo test -p gpui arena`。
   2. 阅读输出的测试名，与 4.4.3 提到的六个测试一一对应：`test_arena`、`test_arena_grow`、`test_arena_alignment`、`test_arena_use_after_clear`、`test_clear_deferred_while_scope_active`、`test_unbalanced_end_scope_panics`。
   3. 想看容量增长日志的话，把日志级别开到 trace 再跑一个复杂示例，例如 `RUST_LOG=trace cargo run -p gpui --example painting 2>&1 | grep "element arena"`（在 examples 里加大量 div 会让这条日志更容易出现）。
3. **需要观察的现象**：六个测试全部通过；trace 日志中出现 `increased element arena capacity to ...kb`，且数值是 1024 的整数倍（chunk 按需追加）。
4. **预期结果**：测试全绿；日志行按 chunk 追加节奏出现。测试运行结果「待本地验证」。
5. 这一实践是纯验证型的：arena 是 `pub(crate)` 内部设施，示例代码无法直接触碰它，阅读测试是理解它的唯一（也是足够好的）窗口。

#### 4.4.5 小练习与答案

**练习 1**：bump 分配器通常「不跑析构」，arena 是怎么解决 `Drop` 的？为什么必须解决？

**参考答案**：`alloc` 把每个分配的 `(指针, drop 函数指针)` 追加进 `elements`（[src/arena.rs:201-204](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/arena.rs#L201-L204)），`force_clear` 先 `elements.clear()` 逐个调用 drop 再重置 chunk。必须解决，因为元素里普遍持有需要析构的资源：`Canvas` 的 `Box<dyn FnOnce>`、div 的 `Vec<AnyElement>` 孩子、各元素的 `SharedString`/`Arc` 引用计数——不跑析构就是内存与引用计数泄漏。

**练习 2**：`ArenaBox` 为什么用 `Rc<Cell<bool>>` 而不是 `Cell<bool>` 或 `Arc<AtomicBool>`？

**参考答案**：`Cell<bool>` 无法共享——arena 与每个 `ArenaBox` 必须指向同一个标志，失效才能同时可见；`Rc<Cell<bool>>` 单线程共享可变，正合适。arena 与元素树全部活动在前台单线程（u2-l5 的结论），不需要 `Arc<AtomicBool>` 的原子开销；clear 时直接把整个标志换成新的 `Rc`（[src/arena.rs:151-152](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/arena.rs#L151-L152)），旧标志永久停留在 false，这代 `ArenaBox` 从此不可用。

**练习 3**：为什么元素用 arena 分配，而实体（`Entity<T>`）用 `slotmap` 的 `EntityMap`（u2-l2）？

**参考答案**：两者的生命周期模式完全不同。元素是「同生共死的短命对象」——一帧内大量创建、帧末整体消亡，bump 分配器 O(1) 分配 + 整块回收 + 内存复用（不还给系统）把这个模式优化到极致。实体是「独立生命周期的长寿对象」——需要按 id 稳定寻址、引用计数、弱引用、观察者挂钩，slotmap 提供 key 稳定性且支持单个删除。用错方向都会灾难：给元素用 slotmap 会回到逐个分配/释放的老路；给实体用 arena 则无法表达「单个实体先于 others 释放」。

## 5. 综合实践

**任务：把 `FixedBox` 做成一个带跨帧状态、可交互的完整自定义元素。**

综合运用本讲四个模块的知识，把 4.2.4 的骨架扩展成一个完整示例（**示例代码**，非项目原有代码）：

1. **基础骨架**（4.2）：按 4.2.4 实现 `FixedBox` 的三阶段——`request_layout` 申报固定尺寸、`prepaint` 计算居中内接矩形、`paint` 用 `window.paint_quad` 绘制。
2. **跨帧状态**（4.3）：按 4.3.4 加入 `with_element_state` 帧计数器；再进一步，让计数器驱动视觉——例如颜色透明度随 `frame_count` 的正弦轻微起伏（`gpui::Hsla` 的 `l` 字段），形成一个不依赖动画系统的「呼吸」效果。
3. **事件监听**（预习 u5）：在 paint 阶段调用 `window.on_mouse_event`，或在视图层给包裹 `FixedBox` 的 div 挂 `.on_click(cx.listener(...))`，点击时通过 `cx.notify()` 触发重绘，观察帧计数随之跳动。
4. **观察 arena 的影响**（4.4）：在视图中同时渲染 1 个与 100 个（用 `.children((0..100).map(|i| FixedBox::new(format!("box-{i}"), size(px(60.), px(40.))))））`，配合 `RUST_LOG=trace` 观察元素 arena 容量增长日志；体会「每帧创建上百个元素」在 arena 下毫无压力。
5. **验证要点**：
   - 每个元素的帧计数独立递增（id 不同 → `GlobalElementId` 不同 → 状态隔离）；
   - 缩放窗口时内接矩形始终居中（bounds 由 Taffy 每帧重算）；
   - 全部日志成对出现 prepaint 在 paint 前（若你保留了 4.1.4 的日志）。

预期完成标志：你能不看讲义，从零写出这个元素的三阶段实现，并向别人解释清楚「元素每帧重建，状态为什么没丢」。运行表现「待本地验证」。

## 6. 本讲小结

- `Element` trait 用 `request_layout → prepaint → paint` 三阶段刻画元素的绘制生命周期，`Drawable` 的 `ElementDrawPhase` 状态机（Start → RequestLayout → LayoutComputed → Prepaint → Painted）以 panic 硬性保证调用顺序；三阶段是全树分层推进，窗口的 `DrawPhase` 只区分 Prepaint/Paint 两档。
- `request_layout` 只申报：把合成后的 `Style` 与孩子 `LayoutId` 交给 Taffy（`Window::request_layout`）；几何由根部一次 `compute_layout` 统一解出；`prepaint` 拿到 `window.layout_bounds` 算好的 `Bounds` 做绘制准备（hitbox、中间量、跨帧状态）；`paint` 通过 `window.paint_quad`/`paint_path` 提交图元。`RequestLayoutState` 与 `PrepaintState` 两个关联类型在阶段间类型安全地传递状态。
- 元素每帧重建，跨帧状态按 `(GlobalElementId, TypeId)` 存在窗口的 `element_states` 表中：`GlobalElementId` 是 `element_id_stack` 路径快照（`Arc<[ElementId]>`），`with_element_state` 负责存取，`NextFrame::finish` 只搬走本帧访问过的状态——无人认领即回收。
- 元素树住在线程局部的 bump 分配 arena 里（默认 1MB chunk，按需增长）：分配即移指针、登记 drop；`clear` 翻转 `valid` 标志使所有旧 `ArenaBox` 失效并整块回收；嵌套绘制用 `begin_scope`/`end_scope` 推迟 clear 防止外层 use-after-free。
- canvas 元素是理解三阶段的最小范本：`RequestLayoutState = Style`、`PrepaintState = Option<T>`，两个 `FnOnce` 回调分别映射 prepaint/paint，`.take().unwrap()` 体现单帧单次语义。
- 选型顺序依旧：内置元素 → canvas（临时绘制）→ 自定义 `Element`（自定义布局/编辑器级组件），自定义元素也应实现 `Styled` 接入样式链。

## 7. 下一步学习建议

- **u4-l2（Taffy 布局引擎集成）**：本讲只用了 `window.request_layout` 的接口形态，下一讲拆 `Style` 如何映射为 taffy 节点、`AvailableSpace` 如何逐层传递、`request_measured_layout` 的测量闭包何时被调用，并排解「布局不生效」类问题。
- **u4-l3（窗口绘制管线）**：把本讲的状态机放回全景——`cx.notify()` 如何标脏窗口、`Window::draw` 如何驱动「全树 request_layout → compute_layout → 全树 prepaint → 全树 paint」、元素 arena 与渲染缓存在帧边界如何交接。
- **u4-l5（自定义 Element 实战：命令式绘制）**：以 painting 示例为模板写一个不依赖 div 的纯命令式组件（模拟时钟），覆盖 `ContentMask` 裁剪与 `on_next_frame` 自驱重绘。
- **顺带阅读**：[src/elements/animation.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/animation.rs)（`with_element_state` 的官方实战）、[src/elements/svg.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/svg.rs)（一个中等复杂度的完整 Element 实现，含 Transformation 只影响绘制的处理）。
