# 拖放与手势（u5-l6）

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `on_drag` / `on_drop` / `can_drop` / `drag_over` 组合出「窗口内元素间拖放」的完整交互。
2. 说出一次内部拖拽的完整生命周期：按下 → 越过 2 像素阈值 → 构造拖动视图 → `AnyDrag` 挂到 `App` 上 → 每帧跟随鼠标 → 松手命中则投递 / 未命中则取消。
3. 理解操作系统文件拖入窗口的事件负载结构（`FileDropEvent` 五个变体与 `ExternalPaths`），以及 GPUI 如何把「文件拖放」翻译成「内部拖拽 + 合成鼠标事件」。
4. 理解「内部拖拽升级为平台原生拖拽」的机制（`ExternalDragPayload` 与 `PlatformOwnedDrag` 状态机）。
5. 了解 `gestures` 模块对触摸手势的抽象：手势竞技场思想、`OngoingScroll` 的主轴锁定算法、`GestureTuning` 手感常量与 `PlatformGestures` 平台钩子。

## 2. 前置知识

本讲建立在 u5-l1、u5-l2 之上，先回顾三个关键认知：

- **监听器在 paint 阶段注册、只活一帧**（u5-l2）。`on_drag` / `on_drop` 本质上也是鼠标事件监听器，同样由 `Interactivity::paint_mouse_listeners` 在每帧绘制时写入窗口，帧结束后失效。拖拽的「跨帧状态」不存放在监听器里，而是存放在 `App` 的 `active_drag` 字段和元素的跨帧状态表里。
- **hitbox 与两阶段派发**（u5-l1）。鼠标事件先做命中测试，再按 Capture 正序、Bubble 逆序派发。`on_drop` 的判定条件「拖拽物悬停在本元素上方」就是 `hitbox.is_hovered(window)`。
- **实体与视图**（u2-l2、u3-l1）。拖拽时跟随鼠标的那个「幽灵视图」是一个普通实体（实现了 `Render`），由框架在每帧绘制时额外画在所有内容之上。

另外一个反复出现的 Rust 技巧是**用 `TypeId` 做类型擦除路由**：拖拽的值被装进 `Arc<dyn Any>`，投递时用 `TypeId::of::<T>()` 比对来找到匹配的 `on_drop` 监听器——和 u5-l3 里 Action 注册表、u2-l3 里实体事件的路由方式同构。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/elements/div.rs` | 拖拽 API 的声明侧（`on_drag` / `on_drop` / `can_drop` / `drag_over` / `on_drag_move` / `external_drag_payload`）与执行侧（`paint_mouse_listeners` 中启动拖拽、分发 drop） |
| `src/interactive.rs` | 事件类型的定义地：`ExternalPaths`、`ExternalDragPayload`、`FileDragPaths`、`FileDropEvent`、`PlatformInput` |
| `src/app.rs` | `AnyDrag`（活动拖拽的全局状态）、`PlatformOwnedDrag` 状态机、`has_active_drag` / `stop_active_drag` 等查询与控制方法 |
| `src/window.rs` | 拖动视图的每帧绘制、`FileDropEvent` 到内部事件的翻译、拖出窗口时的平台升级（`promote_external_drag_to_platform`），以及一套完整的文件拖拽测试 |
| `src/gestures.rs` | 触摸手势识别词汇：`OngoingScroll`、`GestureTuning`、`GestureKinds`、`LongPressEvent`、`PlatformGestures` |
| `src/platform.rs` | `PlatformWindow` 上的 `can_start_external_drag` / `start_external_drag` 默认实现 |
| `src/platform/test/window.rs` | 测试平台对 `start_external_drag` 的模拟实现 |
| `examples/drag_drop.rs` | 官方拖放示例：三个可拖色块 + 一个放置目标 |

## 4. 核心概念与源码讲解

### 4.1 元素间拖放：on_drag / on_drop

#### 4.1.1 概念说明

GPUI 的拖放是**值语义**的：你把一个任意 `T: 'static` 的值（拖拽负载）交给某个元素当「拖拽源」，再用 `on_drop::<T>` 在另一个元素上声明「我能接收 T 类型的投放」。源和目标之间不直接通信，全部经由 `App` 上一个全局唯一的 `active_drag: Option<AnyDrag>` 中转。

这套设计解决了三个问题：

1. **解耦**：拖拽源不需要知道谁会接收，接收方也不需要知道拖拽从哪开始——只要类型匹配。
2. **类型安全**：投递时按 `TypeId` 匹配，`Vec<u32>` 拖不到 `String` 的放置目标上。
3. **统一内部与外部**：后面会看到，从操作系统拖文件进窗口，也会被包装成同一种 `AnyDrag`，所以接收侧 API 只有一套 `on_drop`。

#### 4.1.2 核心流程

一次完整的窗口内拖放：

```text
1. 鼠标左键按下
   元素把 MouseDownEvent 存进跨帧状态 pending_mouse_down，并 window.refresh()
2. 鼠标移动，位移超过 DRAG_THRESHOLD(2px)
   ├── 调用 on_drag 注册的构造闭包，创建「拖动视图」实体
   ├── 组装 AnyDrag { view, value, cursor_offset, cursor_style, ... }
   ├── 挂到 cx.active_drag
   └── window.refresh()，此后的每一帧都会额外绘制拖动视图
3. 拖动中（每一帧）
   ├── Window::draw 用 mouse_position - cursor_offset 作为根位置
   │   重新布局、绘制拖动视图 → 幽灵视图跟随鼠标
   └── 路径上的元素可根据 drag_over 样式高亮
4. 松开鼠标（MouseUpEvent，Bubble 阶段）
   ├── 命中某个带 on_drop::<T> 且 hitbox.is_hovered 的元素
   │   └── can_drop 谓词通过 → 调用监听器、消费 active_drag、stop_propagation
   └── 没有任何接收者 → 框架直接丢弃 active_drag（拖拽取消）
```

注意第 2 步的阈值判断和第 4 步的类型匹配，是拖放与普通 click 的分水岭：按下后没超过 2 像素就松手，走的是 u5-l2 讲过的 click 合成；超过了，click 状态被清空，进入拖拽轨道。

#### 4.1.3 源码精读

**声明侧：on_drag 需要一个值和一个视图构造器。**

[examples/drag_drop.rs:L87-L105](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/drag_drop.rs#L87-L105) 是官方示例里三个可拖色块的写法：`.id(("item", ix))` 让元素成为有状态元素（这是 `on_drag` 的隐含要求，因为 `pending_mouse_down` 要跨帧存放），`.on_drag(drag_info, |info, position, _, cx| cx.new(|_| info.position(position)))` 把 `DragInfo` 作为负载，并当场创建一个新实体作为拖动视图。

对应的 trait 方法定义在：

[src/elements/div.rs:L1558-L1570](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L1558-L1570) —— `StatefulInteractiveElement::on_drag` 的流式 API，签名里三个泛型约束值得注意：`T: 'static`（要做类型擦除）、`W: 'static + Render`（拖动视图必须是可渲染实体）、`Self: Sized`（只在具体元素上可用）。

[src/elements/div.rs:L589-L615](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L589-L615) —— 命令式等价物 `Interactivity::on_drag`，把值装进 `Arc`，把构造闭包装箱存进 `self.drag_listener`。文档注释特意说明：这个 API 同时也是「拖拽开始」回调——配合 `on_drag_move` 使用时，构造闭包就是你的 drag start 钩子。同一元素调用两次 `on_drag` 会触发 `debug_assert!` 报错。

**执行侧一：拖拽何时启动。**

[src/elements/div.rs:L2842-L2885](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L2842-L2885) 是整个机制的心脏，注册在 `MouseMoveEvent` 上的监听器：

- `pending_mouse_down` 里有按下的记录（本元素此前被按下过）；
- 当前没有活动拖拽（`!cx.has_active_drag()`）；
- 位移的模长超过 `DRAG_THRESHOLD`（[src/elements/div.rs:L48](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L48) 定义的常量 `2.`，与 u5-l2 讲过的 click 拖拽取消阈值是同一个值）；
- 按的是左键。

四条全满足时：清空 click 状态（这次交互不再是点击）、计算 `cursor_offset = event.position - hitbox.origin`（按下点相对元素原点的偏移，用来让拖动视图和 grab 点对齐）、调用构造闭包建视图、组装 `AnyDrag` 挂到 `cx.active_drag`、`stop_propagation()` 防止多个重叠元素同时开拖。

`AnyDrag` 的结构在 [src/app.rs:L2944-L2963](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2944-L2963)：`view`（跟随鼠标的视图）、`value: Arc<dyn Any>`（拖拽负载）、`cursor_offset`、`cursor_style`，以及拖出窗口时才用到的 `external_payload_source`。存放位置是 [src/app.rs:L688](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L688) 的 `pub(crate) active_drag: Option<AnyDrag>`——注意它在 `App` 而不是 `Window` 上，所以同一个值理论上可以拖着跨过多个窗口。

**执行侧二：拖动视图为什么跟手。**

[src/window.rs:L3142-L3147](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L3142-L3147)：每帧 `Window::draw` 在 prepaint 完根元素后，若存在活动拖拽，就把拖动视图当作一个「根元素」用 `prepaint_as_root(offset, AvailableSpace::min_size(), ...)` 单独布局，`offset = self.mouse_position() - active_drag.cursor_offset`。鼠标一动窗口就重绘（下面会看到触发点），于是每帧重算 offset，视图就跟手了。

[src/window.rs:L3163-L3169](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L3163-L3169)：绘制顺序上，prompt（对话框）> 拖动视图 > tooltip，三者互斥、都画在整棵元素树与 deferred 层之后——这就是拖动视图永远浮在最上层的原因。

示例里拖动视图自身的画法在 [examples/drag_drop.rs:L31-L52](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/drag_drop.rs#L31-L52)：外层 div 用 `pl`/`pt` 做偏移、内层画半透明色块加 `shadow_md`，制造「浮起的幽灵卡片」效果。

**执行侧三：drop 的投递与取消。**

[src/elements/div.rs:L2774-L2804](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L2774-L2804)：`on_drop` 注册的监听器挂在 `MouseUpEvent` 上，Bubble 阶段触发。条件是「存在活动拖拽 + 本元素 hitbox 被悬停 + 负载的 `type_id()` 与 `on_drop::<T>` 登记的类型一致」。命中后：`cx.active_drag.take()` 消费掉拖拽（一次投放只投给一个接收者）、可选地先过 `can_drop` 谓词、调用监听器拿到 `&T`、`window.refresh()`、`stop_propagation()`。声明侧 API 在 [src/elements/div.rs:L540-L560](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L540-L560)（`on_drop` 与 `can_drop`）。

[src/window.rs:L5220-L5230](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L5220-L5230)：事件派发完之后框架兜底——如果这是 MouseMove 且拖拽仍在，就 `refresh()` 让拖动视图跟进；如果是 MouseUp 且拖拽**仍然**在（说明没有任何 on_drop 消费它，比如松在了空白处），直接 `cx.active_drag = None` 取消并重绘。

示例的放置目标在 [examples/drag_drop.rs:L107-L122](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/drag_drop.rs#L107-L122)：`.on_drop(cx.listener(|this, info: &DragInfo, _, _| { this.drop_on = Some(*info); }))`，用 `cx.listener` 把回调绑回 `DragDrop` 实体，落子后改状态触发重绘，边框颜色随之变成被投条目的颜色。

#### 4.1.4 代码实践

**实践目标**：亲手跑通官方示例，并用日志验证 4.1.2 描述的生命周期。

**操作步骤**：

1. 在 `crates/gpui` 目录运行示例：

   ```bash
   cargo run -p gpui --example drag_drop
   ```

2. 按住任一色块拖动：先原地不动，再快速移动超过 2 像素，观察「幽灵卡片」出现的瞬间。
3. 分别拖到虚线框内松手、拖到窗口空白处松手，对比边框颜色变化。
4. 把窗口缩小到放不下三个色块，从右侧第一个色块开始向左拖，体会「同一个值可以从任何源拖到任何目标」。
5. （选做）在示例里加一行打印：把 `on_drop` 的监听器临时改成

   ```rust
   .on_drop(cx.listener(|this, info: &DragInfo, _, cx| {
       println!("dropped item {} at {:?}", info.ix, info.position);
       this.drop_on = Some(*info);
       cx.notify();
   }))
   ```

   （示例代码，改动只为观察。注意 `cx.listener` 本身**不会**自动 `cx.notify()`——这里界面仍会刷新，是因为 drop 投递管线在监听器返回后统一调用了 `window.refresh()`，见下一节源码 L2797。）

**需要观察的现象**：拖动过程中鼠标样式变化（示例源元素设置了 `.cursor_move()`）；松手在目标内时打印发生一次、边框变色；松手在目标外时无打印、界面恢复原状。

**预期结果**：三个色块都可拖，放置目标边框变成最后投放条目的颜色，说明 `DragInfo` 值被完整投递。窗口内拖拽不需要任何平台配置。运行效果如与描述不符，请以本地实际为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `on_drag` 要求元素先 `.id()`？不加会怎样？

答案：`on_drag` 的启动逻辑依赖元素的跨帧状态 `pending_mouse_down`——按下事件发生在一帧，位移超阈值的判定发生在之后的某帧，中间必须有人记住「这个元素被按下过」。跨帧元素状态以 `(GlobalElementId, TypeId)` 为键存放（u5-l2），没有 `.id()` 就没有 `GlobalElementId`，状态无处安放。事实上 `on_drag` 定义在 `StatefulInteractiveElement` 上，缺 `.id()` 时元素根本拿不到这个方法，编译期即报错。

**练习 2**：两个重叠的放置目标都注册了 `on_drop::<DragInfo>`，投放会给谁？

答案：给视觉上层的那个。drop 监听器在 Bubble 阶段派发，而 Bubble 阶段按注册顺序的**逆序**遍历（见 [src/window.rs:L5207-L5216](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L5207-L5216)），监听器又是在 paint 阶段按绘制顺序注册的，后绘制（视觉靠前）的元素先收到；命中后 `stop_propagation()` 终止派发，所以只有最上层接收一次。

**练习 3**：如何让一个放置目标「只接收偶数编号的条目」？

答案：在该元素上追加 `.can_drop(|value: &dyn Any, _, _| { value.downcast_ref::<DragInfo>().is_some_and(|info| info.ix % 2 == 0) })`。`can_drop` 谓词在投递前以 `&dyn Any` 形式拿到负载（[src/elements/div.rs:L2790-L2793](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L2790-L2793)），返回 false 则本次不投递，拖拽继续有效（可以再去找别的目标）。它同时会关闭 `drag_over` 高亮。

### 4.2 拖拽反馈进阶：on_drag_move、drag_over 样式与拖拽控制

#### 4.2.1 概念说明

拖放体验好不好，一半取决于反馈：拖到哪了、哪里能放、光标什么形状。GPUI 提供三个层次的反馈 API：

- **`on_drag_move::<T>`**：拖拽物每次移动都回调，且不限于拖出元素自身范围——适合实现插入指示线、悬停预览这类「不 conforming 拖放模式」的交互，也可以用来做调整大小（resize）这类非拖放语义的拖拽。
- **`drag_over::<T>` / `group_drag_over::<T>`**：声明式样式反馈，拖拽物悬停到本元素（或本分组）上方时自动应用一段 `StyleRefinement` 补丁，与 hover/active 样式同一套 refine 合成机制（u3-l3）。
- **`App::stop_active_drag` / `App::set_active_drag_cursor_style`**：程序化控制——比如按 Esc 取消拖拽、根据悬停位置动态切换光标形状（复制/移动/禁用）。

#### 4.2.2 核心流程

```text
拖动中每一帧:
  MouseMoveEvent(Capture 阶段)
    └── 所有 on_drag_move::<T> 监听器被调用（只要 active_drag.value 是 T 类型）
        └── 回调里可读 event.position、bounds，通过 event.drag(cx) 拿到 &T

样式合成阶段（compute_style）:
  若 active_drag 存在且值类型为 S:
    ├── can_drop 谓词通过
    ├── drag_over::<S> 且 hitbox.is_hovered → refine 样式补丁
    ├── group_drag_over::<S> 且分组 hitbox 被悬停 → refine 分组补丁
    └── style.mouse_cursor = drag.cursor_style（拖拽光标覆盖元素自身光标）
```

#### 4.2.3 源码精读

[src/elements/div.rs:L62-L86](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L62-L86)：`DragMoveEvent<T>` 的定义。它包装了原始 `MouseMoveEvent`、当前元素 `bounds`、被拖的值（`dragged_item: Arc<dyn Any>`）。`drag(cx)` 方法从 `cx.active_drag` 里 downcast 出 `&T`——注意它只在活动拖拽确实是 T 类型时有效，否则 panic（文档写明该事件「only valid when the stored active drag is of the same type」）。

[src/elements/div.rs:L327-L358](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L327-L358)：`on_drag_move` 的实现。关键点有三：它在 **Capture 阶段**触发（早于所有 Bubble 监听器，包括 drop 判定）；**不做命中测试**（`hitbox` 只用来取 `bounds`，无论鼠标在哪都回调，文档注释明说「inside or outside of this element」）；以 `TypeId::of::<T>()` 过滤活动拖拽的类型。

[src/elements/div.rs:L1130-L1163](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L1130-L1163)：`drag_over` / `group_drag_over` 的声明侧——把 `(TypeId::of::<S>(), 样式构造闭包)` 推进 `drag_over_styles`。闭包能拿到被拖的 `&S`，所以高亮样式可以依赖负载内容。

[src/elements/div.rs:L3340-L3369](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L3340-L3369)：应用侧。样式合成时若存在活动拖拽：先过 `can_drop`，再逐个比对 `drag_over_styles` 里登记的类型，类型匹配且 `hitbox.is_hovered(window)` 就 `style.refine(...)`；分组版本查的是分组 hitbox（u5-l2 的 `group()` 机制）。最后一行 `style.mouse_cursor = drag.cursor_style` 很有意思——拖拽期间，拖拽源在 `on_drag` 元素上设置的鼠标样式（示例里的 `.cursor_move()`）会**覆盖**沿途所有元素自己的光标。

[src/app.rs:L2499-L2524](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2499-L2524) 与 [src/app.rs:L2583-L2596](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2583-L2596)：程序化控制面。`has_active_drag()` 查询；`stop_active_drag(window)` 强制取消并刷新（实现「Esc 取消拖拽」就靠它）；`set_active_drag_cursor_style` 运行中改光标（比如拖到「禁止区域」时换成禁止符号）。

#### 4.2.4 代码实践

**实践目标**：给 drag_drop 示例补上「拖到放置目标上方时目标高亮 + 拖动中打印轨迹」的反馈。

**操作步骤**：

1. 复制 `examples/drag_drop.rs` 为 `examples/my_drag_drop.rs`，并在 `Cargo.toml` 的 `[[example]]` 列表里加一行（可参照相邻 example 条目的格式；若不确定格式，直接在原示例上改也可以）。
2. 给放置目标加 drag_over 样式（示例代码）：

   ```rust
   .drag_over::<DragInfo>(|style, info, _, _| {
       style.border_color(info.color).bg(info.color.opacity(0.1))
   })
   ```

3. 给放置目标加轨迹打印（示例代码）：

   ```rust
   .on_drag_move(|event: &DragMoveEvent<DragInfo>, _, cx| {
       let info = event.drag(cx);
       println!("dragging item {} over this element", info.ix);
   })
   ```

4. `cargo run -p gpui --example my_drag_drop`，重复 4.1.4 的拖动操作。

**需要观察的现象**：拖动色块接近放置目标时，目标边框与背景立即变为该色块颜色的高亮（比 4.1 的「投放后才变色」反馈更快）；终端持续输出 dragging 日志。

**预期结果**：高亮随拖拽物进入/离开目标区域而出现/消失；日志只在悬停于该元素期间打印（`on_drag_move` 注册在该元素上，但鼠标不必在元素内——注意本例中拖动经过窗口任何位置都可能触发，因为 `on_drag_move` 不做命中过滤，请以实际输出为准）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`on_drag_move` 与 `on_mouse_move` 都响应鼠标移动，核心差异是什么？

答案：三点。①`on_mouse_move` 在 Bubble 阶段且要求 `hitbox.is_hovered`（只在本元素上方触发）；`on_drag_move` 在 Capture 阶段且**不要求悬停**，只要活动拖拽的类型匹配、无论指针在哪都回调。②`on_drag_move` 携带类型化的拖拽负载（`event.drag(cx)` 拿 `&T`）。③语义不同：前者是普通悬停反馈，后者专为「正在进行的拖拽」服务。

**练习 2**：想让「拖到窗口某区域时显示禁止光标」，怎么做？

答案：在该区域的 `on_drag_move`（或任何拖拽期间的回调）里调用 `cx.set_active_drag_cursor_style(CursorStyle::OperationNotAllowed, window)`——它会写入 `AnyDrag.cursor_style`，而样式合成时 `style.mouse_cursor = drag.cursor_style` 会覆盖沿途元素自己的光标（[src/elements/div.rs:L3366](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L3366)）。「禁止」对应 `CursorStyle::OperationNotAllowed`（[src/platform.rs:L2287-L2289](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform.rs#L2287-L2289)），同一枚举里还有拖拽专用的 `DragLink`、`DragCopy`。离开区域时再设回 `CursorStyle::Arrow` 或目标形状。

**练习 3**：为什么 `drag_over` 的样式闭包能拿到 `&S`，而 `hover` 样式闭包不能拿到任何负载？

答案：hover 只依赖「鼠标在不在上面」这一事实，没有附加数据；而 drag_over 补丁登记时绑定了负载类型 `S`，应用时框架手里正好有 `active_drag.value: Arc<dyn Any>`，类型匹配即可 downcast 传出（[src/elements/div.rs:L3358-L3362](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L3358-L3362)），于是「拖一个红色条目」和「拖一个蓝色条目」可以让目标高亮出不同颜色。

### 4.3 系统文件拖入：FileDropEvent 与 ExternalPaths

#### 4.3.1 概念说明

从操作系统文件管理器拖文件进窗口，是另一种拖放：拖拽源不在你的应用里，平台（macOS 的 AppKit、Linux 的 Wayland/X11、Windows）全程掌控拖拽会话，GPUI 只收到通知。这类事件统一为 `FileDropEvent` 枚举的五个变体，负载是 `ExternalPaths`（一组 `PathBuf`）。

GPUI 的设计妙处在于：**文件拖入不发明新 API，而是翻译成内部拖拽**。文件一进入窗口，框架就构造一个值为 `ExternalPaths` 的 `AnyDrag`，并把后续的平台通知翻译成合成的 `MouseMoveEvent` / `MouseUpEvent`。于是接收文件只需要一行 `.on_drop::<ExternalPaths>(...)`——和窗口内拖放完全同一套代码路径，命中测试、`can_drop`、`drag_over::<ExternalPaths>` 全部免费获得。

#### 4.3.2 核心流程

```text
平台事件                     GPUI 内部翻译（window.rs dispatch_event）
─────────────────────────────────────────────────────────────
FileDropEvent::Entered  →  若无活动拖拽: active_drag = AnyDrag {
    {position, paths}          value  = Arc::new(paths.clone()),
                               view   = cx.new(|_| paths),   // ExternalPaths 实现 Render
                               ... }
                           合成 MouseMove{position, pressed_button: Left}
FileDropEvent::Pending  →  合成 MouseMove{position, pressed_button: Left}
    {position}
FileDropEvent::Submit   →  cx.activate(true)
    {position}              合成 MouseUp{Left, position, click_count: 1}
                               → 走普通 drop 投递，on_drop::<ExternalPaths> 收到值
FileDropEvent::Exited   →  清空/归还 active_drag，原样转发 Exited 事件
FileDropEvent::Ended    →  结束平台会话状态，原样转发 Ended 事件
```

`ExternalPaths` 的视图实现是 `Empty`——什么都不画，因为拖文件时**平台自己会画文件图标**跟随光标，应用再画就重复了。

#### 4.3.3 源码精读

[src/interactive.rs:L683-L692](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/interactive.rs#L683-L692)：`ExternalPaths(pub SmallVec<[PathBuf; 2]>)`，`paths()` 取切片。它就是「文件拖入」时的拖拽值类型，也是你 `on_drop` 上要写的类型参数。

[src/interactive.rs:L719-L724](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/interactive.rs#L719-L724)：`impl Render for ExternalPaths` 返回 `Empty`，注释直言「平台会为被拖的文件渲染图标」。

[src/interactive.rs:L726-L758](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/interactive.rs#L726-L758)：`FileDropEvent` 五变体定义——`Entered`（进入窗口，带路径）、`Pending`（窗口内移动）、`Submit`（松手投放）、`Exited`（拖出窗口或取消）、`Ended`（平台会话结束）。它实现了 `InputEvent` 与 `MouseEvent`，因此既能装进 `PlatformInput`（[src/interactive.rs:L783-L784](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/interactive.rs#L783-L784)），也能走鼠标事件的派发管线。

[src/window.rs:L5072-L5123](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L5072-L5123)：翻译逻辑全貌，对应上面流程表的每一行。几个细节：

- `Entered` 分支先试 `cx.restore_platform_drag(source_window)`（4.4 会讲：如果是本窗口早先拖出去、被平台接管的拖拽回来了，恢复原拖拽而不是新建）；确实没有活动拖拽才构造新的 `AnyDrag`，`cursor_offset` 直接用进入位置。
- `Submit` 会 `cx.activate(true)` 把窗口带到前台，再合成 `MouseUp`——合成事件带 `click_count: 1`、左键，与真实松手别无二致，所以 4.1 的 drop 投递管线无需任何改动就能接收。
- `Exited` / `Ended` 原样转发 `PlatformInput::FileDrop`，让注册了 `on_mouse_event::<FileDropEvent>` 的元素有机会清理状态（比如收起高亮）。

#### 4.3.4 代码实践

**实践目标**：让 drag_drop 示例同时接收「从文件管理器拖进来的文件」，打印全部路径。

**操作步骤**：

1. 在 4.2.4 基础上（或直接改原示例），给放置目标追加一个 `on_drop`（同一元素可以注册多个不同类型的 drop 监听器，它们按类型各管各的）：

   ```rust
   .on_drop(cx.listener(|this, paths: &ExternalPaths, _, _| {
       for path in paths.paths() {
           println!("received file: {}", path.display());
       }
       this.last_dropped_file = paths.paths().first().cloned();
   }))
   ```

   （示例代码；需在 `DragDrop` 结构体上加 `last_dropped_file: Option<PathBuf>` 字段，并在渲染里显示它。）

2. `cargo run -p gpui --example drag_drop`。
3. 从系统的文件管理器（Linux 上 Nautilus/Dolphin，macOS 上 Finder）拖任意文件到窗口内的放置目标上松手。

**需要观察的现象**：拖动文件经过窗口时，光标旁跟着平台绘制的文件缩略图标（不是你画的）；悬停在放置目标上方时，若你注册了 `drag_over::<ExternalPaths>` 会出现高亮；松手后终端打印每个文件的完整路径。

**预期结果**：`paths()` 返回所有被拖入文件的路径；拖到目标外松手则什么都不打印（拖拽被框架取消）。文件拖入行为依赖平台与窗口系统（Wayland/X11/macOS 表现可能略有差异），具体以本地为准。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ExternalPaths` 的 `render` 返回 `Empty`？如果返回一个色块会怎样？

答案：因为文件拖入时平台绘制自己的拖拽预览（文件图标跟随光标），这是操作系统会话的一部分，应用无法也不应接管。返回 `Empty` 表示「应用侧的拖动视图不画任何东西」；如果画一个色块，屏幕上会出现「平台图标 + 你的色块」双重预览，且色块位置由 GPUI 每帧重算，与平台图标并不同步。

**练习 2**：`FileDropEvent::Submit` 被翻译成合成的 `MouseUpEvent` 之后，接下来发生什么？

答案：与真实鼠标松手完全相同的 drop 投递（4.1.3 执行侧三）：对上一帧渲染结果做命中测试，Bubble 逆序找 `hitbox.is_hovered` 且类型匹配 `ExternalPaths` 的 `on_drop` 监听器，`can_drop` 通过则调用之并消费 `active_drag`；没有任何接收者则框架把 `active_drag` 清空。区别只是这次 `active_drag` 的值来自平台。

**练习 3**：如何在拖动文件经过（但还没松手）时就知道拖的是什么文件？

答案：注册 `.drag_over::<ExternalPaths>(|style, paths, _, _| ...)`——样式闭包能拿到 `&ExternalPaths`，可以把文件名写进高亮样式（例如 `style.border_color(...)` 之外再在元素本体用 `cx.has_active_drag()` 配合 `on_drag_move` 读 `event.drag::<ExternalPaths>(cx)` 展示文件名）；或者监听 `FileDropEvent` 本身：`window.on_mouse_event(|event: &FileDropEvent, phase, window, cx| ...)`，`Entered` 变体带完整 `paths`。

### 4.4 拖出窗口：ExternalDragPayload 与平台接管

#### 4.4.1 概念说明

反过来，把应用内的东西拖**出**窗口、变成操作系统级的拖拽（比如把一个「文件」从你的应用拖到桌面），需要平台配合：一旦指针越过窗口边界，GPUI 就把拖拽「移交」给平台，自己退居幕后；如果用户又拖回本窗口，再把拖拽「接回来」。这套移交/接回由 `App` 里的一个小状态机 `PlatformOwnedDrag` 管理。

移交时交给平台的数据是 `ExternalDragPayload`，目前只有一个变体 `Files(FileDragPaths)`——真实磁盘路径。这不是随便把 `DragInfo` 之类任意值扔给系统：操作系统文件拖放只认路径。

#### 4.4.2 核心流程

```text
窗口内拖拽中，指针移出视口（MouseMove 且左键按住）:
  promote_external_drag_to_platform:
    ├── platform_window.can_start_external_drag()?     // 平台是否支持
    ├── 取出 active_drag.external_payload_source       // 只能取一次
    ├── resolver(&T) → Option<ExternalDragPayload>     // 惰性求值
    ├── platform_window.start_external_drag(&payload)  // 平台接管
    └── hand_active_drag_to_platform: active_drag 挂起进 PlatformOwnedDrag::Suspended

拖回本窗口:
  FileDropEvent::Entered → restore_platform_drag → active_drag 恢复，状态变 RestoredInSourceWindow
  再次拖出 → hand_restored_drag_to_platform → 重新挂起
  FileDropEvent::Ended → end_platform_drag → 全部清理，active_drag 置空
```

`external_payload_source` 被 `take()` 意味着「每次拖拽手势至多移交一次」；resolver 返回 `None` 则表示这次拖拽不支持外带，继续当普通窗口内拖拽处理。

#### 4.4.3 源码精读

[src/interactive.rs:L694-L717](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/interactive.rs#L694-L717)：`ExternalDragPayload::Files(FileDragPaths)`。`FileDragPaths` 是 `(PathBuf, bool)` 的列表，布尔标记是否目录——文档注释解释了为什么让调用方提供目录信息：避免平台开始拖拽时再去查文件系统元数据。

[src/elements/div.rs:L617-L643](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L617-L643)：`external_drag_payload` 的声明侧。约束严格：必须先调用过 `on_drag`、类型参数 `T` 必须与 `on_drag` 的值同类型、每个元素至多一次（三个 `debug_assert!` 各管一条）。它不会立刻求值，而是把 resolver 装箱存起来，等真正要移交时才调用。

[src/window.rs:L5151-L5179](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L5151-L5179)：`promote_external_drag_to_platform`，在每次输入事件派发完之后被调用（[src/window.rs:L5134-L5136](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L5134-L5136)，注释说明必须在 move 派发之后：移交后手势归平台，这是拖拽监听器看到指针离开、重置状态的最后机会）。判定链：是 MouseMove 且左键按住 → 位置**不在**视口内 → 平台允许 → 取 payload source → 求值 → `start_external_drag` 成功则挂起内部拖拽并刷新。

[src/app.rs:L667-L677](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L667-L677)：`PlatformOwnedDrag` 状态机的两个状态。`Suspended(AnyDrag)`——拖拽在平台手里，GPUI 停画拖动视图；`RestoredInSourceWindow`——拖回源窗口了（此标记可以比 active_drag 活得久，因为源窗口内投放会先消费 active_drag，而 AppKit 的拖拽会话要晚一点才结束，靠 `FileDropEvent::Ended` 清理）。

[src/app.rs:L2526-L2581](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2526-L2581)：状态机的四个转移函数 `hand_active_drag_to_platform` / `restore_platform_drag` / `hand_restored_drag_to_platform` / `end_platform_drag`，全部按 `source_window` 过滤——只有源窗口有权操作这次拖拽。

[src/platform.rs:L908-L913](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform.rs#L908-L913)：`PlatformWindow` 的两个默认实现都返回 false / 空操作——即默认「本平台不支持外带拖拽」，`promote` 链在第一关就退出，拖拽保持窗口内语义。这展示了 GPUI 平台抽象的常见模式：新能力给保守默认值，各平台按支持程度覆写。

[src/window.rs:L7157-L7186](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L7157-L7186)：一套现成的用法范本——测试视图 `FileDragView` 把 `PathBuf` 当拖拽值，`on_drag` 创建空视图、`external_drag_payload` 把路径包装成 `FileDragPaths`（标记为目录）、`on_drag_move` 记录轨迹、`on_drop` 记录投放。配套测试 `file_drag_is_promoted_once_and_restored_in_source_window`（[src/window.rs:L7188-L7189](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L7188-L7189) 起）完整演练了移交-恢复-结束循环。

[src/platform/test/window.rs:L404-L416](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/test/window.rs#L404-L416)：测试平台的 `start_external_drag` 把路径记进 `external_drag_files` 并返回可配置的结果——这就是无 GUI 环境下测拖出的诀窍。

#### 4.4.4 代码实践

**实践目标**（源码阅读型）：跟随测试理解「拖出 → 平台接管 → 拖回 → 结束」的完整循环，并跑通它。

**操作步骤**：

1. 阅读 [src/window.rs:L7157-L7186](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L7157-L7186) 的 `FileDragView`，画出它与 4.4.2 流程的对应关系。
2. 继续往下读 `start_drag` 辅助函数与 `file_drag_is_promoted_once_and_restored_in_source_window` 测试体（[src/window.rs:L7196-L7204](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L7196-L7204) 起），注意测试如何用 `window.dispatch_event` 喂入合成的鼠标事件与 `FileDropEvent`。
3. 运行该测试并观察：

   ```bash
   cargo test -p gpui file_drag_is_promoted_once_and_restored_in_source_window
   ```

4. （选做）在真实窗口里验证：把 4.4.3 范本的 `on_drag` + `external_drag_payload` 抄进你 4.2.4 的示例（值类型换成 `PathBuf`，指向磁盘上真实存在的文件），拖出窗口边界——在支持的平台（如 macOS）上会看到拖拽预览从你的幽灵卡片变成系统的文件图标。

**需要观察的现象**：测试通过；测试输出中能看到移交只发生一次（`external_payload_source` 被 take 后为空），拖回源窗口时 `on_drag_move` 重新开始记录，`Ended` 之后 `active_drag` 为空。

**预期结果**：`cargo test` 绿色通过。桌面平台上的真实拖出行为取决于平台实现（Linux 上 X11/Wayland 支持程度不同），`can_start_external_drag` 默认 false 的平台会保持窗口内拖拽——这本身就是一个值得验证的现象。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`external_drag_payload` 的 resolver 为什么是「惰性」的（拖出时才调用），而不是 `on_drag` 时就把 payload 准备好？

答案：两个原因。①大多数拖拽根本不会拖出窗口，提前构造 `FileDragPaths`（可能要收集路径、查目录元数据）是纯浪费；②拖拽开始时应用状态可能与拖出时不同（例如用户拖着走了一会儿，目标路径才确定下来），惰性求值保证交给平台的是最新数据。源码里它被包成 `ExternalDragPayloadSource` 存进 `AnyDrag`，`promote` 时 `take()` 并调用恰好一次。

**练习 2**：拖出窗口后，`App::active_drag` 是空的吗？GPUI 还记得这次拖拽吗？

答案：`active_drag` 是空的（`hand_active_drag_to_platform` 把它 take 走了），但 GPUI 没忘记：整个 `AnyDrag` 被挂进 `PlatformOwnedDrag { state: Suspended(drag) }`（[src/app.rs:L2526-L2535](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2526-L2535)）。指针拖回源窗口时 `restore_platform_drag` 再把它放回 `active_drag`。所以拖出→拖回→投放的完整链路里，最初的拖拽值（连同视图）始终存活。

**练习 3**：为什么需要 `RestoredInSourceWindow` 这个看似多余的状态？

答案：源码注释（[src/app.rs:L674-L676](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L674-L676)）解释了时序问题：在源窗口内投放时，`active_drag` 会被 drop 投递先消费掉，而平台（AppKit）的拖拽会话要到稍后才发 `Ended`。这个标记让「会话已回到源窗口」这一事实独立于 `active_drag` 存活，等 `Ended` 到达时 `end_platform_drag` 才把整个 `PlatformOwnedDrag` 清理干净，中途 `stop_active_drag` 也能正确联动。

### 4.5 手势识别：gestures 模块

#### 4.5.1 概念说明

`gestures.rs` 处理的是触摸屏（以及触控板捏合）手势，与前面的鼠标拖放是两个世界，但设计哲学一脉相承：**不发明新事件，把识别出的手势映射到已有语义事件上**。模块开头的文档（[src/gestures.rs:L1-L11](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gestures.rs#L1-L11)）说得很清楚：GPUI 在核心里维护一个可移植的「手势竞技场」，从原始 `TouchEvent` 识别手势；识别器相互竞争，赢家认领触摸、输家被取消；识别结果尽量通过现有事件浮出——点按变成 `ClickEvent`（触摸来源）、平移变成携带 `TouchPhase` 的 `ScrollWheelEvent`、捏合变成 `PinchEvent`。收益是「为 `on_click` 和滚动容器写的组件在移动端原样可用」。

目前模块内已落地的核心是 `OngoingScroll`（平移手势的主轴锁定），其余是为竞技场准备的手感常量（`GestureTuning`）、能力声明（`GestureKinds`）、事件类型（`LongPressEvent`）与平台钩子（`PlatformGestures`）。

#### 4.5.2 核心流程

`OngoingScroll::filter_at` 解决触摸滚动最恼人的问题：用户想纵向滚列表，手指却不可能走出完美竖线，若不处理，列表会左右轻微抖动。算法是「主轴锁定 + 定向解锁」：

```text
输入: 本帧增量 delta, 触摸阶段 touch_phase, 当前时间 now

1. Ended/Cancelled → 清空状态（手势结束）
2. 增量为零 → 仅在 Started 时清空状态，直接返回
3. 判断是否新手势: touch_phase == Started
                 或距上个事件 ≥ SCROLL_EVENT_SEPARATION(28ms, 兼容只发 Moved 的平台)
   新手势 → 锁定主轴: |x| <= |y| 取 Vertical，否则 Horizontal
   旧手势 → 尝试解锁: 若另一轴显著占优（见下）则置 axis = None
4. 按轴过滤: Vertical → delta.x = 0；Horizontal → delta.y = 0
```

解锁条件（[src/gestures.rs:L36-L37](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gestures.rs#L36-L37) 的两个常量）：另一轴增量超过 \( \max(|x|, |y|) \geq 6\,\text{px} \) 的下限，且比值达到主轴的 1.9 倍，即

\[
\delta_{\text{other}} \geq 1.9 \times \delta_{\text{main}} \quad\text{且}\quad \delta_{\text{other}} \geq 6\,\text{px}
\]

用乘法而非减法，使判定在不同滚动速度下都成比例成立。

#### 4.5.3 源码精读

[src/gestures.rs:L20-L86](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gestures.rs#L20-L86)：`OngoingScroll` 全文。`filter` 是公开入口（用 `Instant::now()`），`filter_at` 接受注入的时间供测试用。状态只有两个字段：`last_event: Option<Instant>` 与 `axis: Option<Axis>`——`None` 表示自由滚动（两轴都放行）。

[src/gestures.rs:L91-L123](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gestures.rs#L91-L123)：`GestureTuning` 手感常量及默认值（iOS 风格）：`touch_slop` 8px（移动多远算拖动而非点按）、`multi_tap_interval` 400ms 与 `multi_tap_slop` 16px（双击判定窗口）、`long_press_duration` 500ms、`momentum_decay_per_ms` 0.998（甩动后的动量衰减，注释提到 UIScrollView 同款数值）、`min_fling_velocity` 50px/s。

[src/gestures.rs:L125-L159](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gestures.rs#L125-L159)：`GestureKinds` 四个布尔（tap / long_press / pan / pinch）加 `NONE` / `ALL` 两个常量。它是平台向核心「报账」用的：声明哪些手势平台原生识别，剩下的交给 GPUI 的可移植识别器。

[src/gestures.rs:L161-L172](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gestures.rs#L161-L172)：`LongPressEvent`（长按，移动端的上下文菜单触发）。文档说明：裸长按会作为 `long_press: true` 的 `ClickEvent` 投给 aux-click 监听器；这个原始事件类型留给需要手势本身的元素（例如「长按启动拖拽」），注册 API 随手势竞技场一起交付。

[src/gestures.rs:L174-L194](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gestures.rs#L174-L194)：`PlatformGestures` trait，两个带默认实现的方法（`tuning` 返回默认手感、`native_recognizers` 返回 `NONE`），`NullPlatformGestures` 是给桌面平台与测试的空实现——又是「保守默认 + 按需覆写」模式。

**消费点在哪？** `OngoingScroll` 不是死代码：每个开启了 `overflow_scroll` 的 div 都会在布局阶段把一个 `OngoingScroll` 存进跨帧元素状态（[src/elements/div.rs:L2166-L2182](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L2166-L2182)，滚动句柄场景在 L2162-L2165）。滚动事件处理时，若元素声明了 `.restrict_scroll_to_axis()`（[src/elements/div.rs:L1479-L1484](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L1479-L1484)）且增量是精确像素（触摸/触控板来源），就调用 `ongoing_scroll.borrow_mut().filter(&mut delta, event.touch_phase)`（[src/elements/div.rs:L3207-L3216](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L3207-L3216)）再做滚动——这就是触摸屏上列表「只朝一个方向滚」的实现。

`gestures` 模块经 [src/gpui.rs:L29](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gpui.rs#L29) 挂载、[src/gpui.rs:L107](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gpui.rs#L107) 的 `pub use gestures::*` 扁平导出，所以直接写 `gpui::OngoingScroll` 即可。

#### 4.5.4 代码实践

**实践目标**：用模块自带的单元测试验证主轴锁定算法，并亲手改参数观察行为边界。

**操作步骤**：

1. 跑 gestures 的全部测试：

   ```bash
   cargo test -p gpui gestures::
   ```

2. 精读两个代表性测试：[src/gestures.rs:L201-L218](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gestures.rs#L201-L218)（锁轴：初始增量 (10,2) 锁 Horizontal，后续 (3,2) 的 x 分量保留、y 归零）与 [src/gestures.rs:L221-L235](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gestures.rs#L221-L235)（解锁：随后 (2,10) 的 y 达到 x 的 5 倍 ≥1.9 倍阈值，axis 置 None，两轴都放行）。
3. 做一个思想实验并在测试里验证（临时改测试断言，观察完还原）：把连续增量换成 (10, 15)——比值 1.5 < 1.9，轴**不**应解锁，delta.y 仍被归零。
4. （选做）真实设备验证：在有触摸屏的机器上跑任一带 `.overflow_y_scroll()` + `.restrict_scroll_to_axis()` 的列表示例，用手指斜向滑动，观察列表只在初始主轴方向滚动；中途明显改变方向（快速大幅横向甩）才会切换。

**需要观察的现象**：步骤 1 全绿；步骤 3 中 (10,15) 不解锁（15 < 10×1.9=19），证实 1.9 倍是硬阈值；步骤 4 中轻微斜滑不抖动、大幅换向能切换。

**预期结果**：算法行为与 4.5.2 的规则逐条对应。触摸屏行为依赖硬件与平台，无触摸设备时以单元测试为准。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：手势竞技场「识别器竞争、赢家认领、输家取消」解决什么问题？

答案：多点触控下同一串触摸可能同时满足多个手势的判据（例如单指移动既可能是平移也可能是滑动手势的开头）。若各识别器独立判断，会同时触发多个语义事件，用户意图被放大成多个动作。竞技场让识别器在判定完成前先「竞争」，最先确认意图者认领触摸，其余被取消——一次触摸只产出一个手势。（这与浏览器触摸事件模型的 `preventDefault` 之争、Android 的事件拦截是同一类问题。）

**练习 2**：`SCROLL_EVENT_SEPARATION` 超时（28ms）为什么存在？

答案：有些平台只发 `TouchPhase::Moved`、不发 `Started`（源码注释明说这是超时回退存在的原因，见 [src/gestures.rs:L28-L30](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gestures.rs#L28-L30)）。没有 Started 就无法靠阶段划分手势边界，于是用「两次事件间隔 ≥28ms」认定新手势开始，重新锁轴。测试 `ongoing_scroll_starts_new_gesture_at_timeout_boundary`（[src/gestures.rs:L237-L252](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gestures.rs#L237-L252)）专门验证了这个边界。

**练习 3**：桌面应用开发者需要关心 `PlatformGestures` 吗？

答案：通常不用。桌面平台用 `NullPlatformGestures`（无原生识别器、默认手感），触摸事件或者不发生，或者由平台翻译成鼠标/滚轮事件（u5-l1 的 `PlatformInput`）。只有两种情况需要碰它：①为新的移动平台实现 GPUI 后端时，用 `native_recognizers` 上报平台原生手势、用 `tuning` 提供系统级手感常量；②你的组件需要区分「触摸点按」与「鼠标点击」时，直接消费带 `TouchPhase` 的 `ScrollWheelEvent` 或 `ClickEvent` 的触摸变体，而不是依赖本 trait。

## 5. 综合实践

**任务：两栏看板 + 系统文件接收器**（综合 4.1–4.3 全部知识点）。

要求：

1. **数据建模**：一个 `Kanban` 根实体，持有 `todo: Vec<Card>` 与 `done: Vec<Card>`；`Card { id: usize, title: SharedString, color: Hsla }` 实现 `Copy`，作为拖拽负载类型（对照 4.1 的 `DragInfo`）。
2. **左栏（待办）**：每张卡片一个 `.id(("card", card.id))` 的 div，`on_drag(card, ...)` 创建跟随视图（参照 [examples/drag_drop.rs:L31-L52](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/drag_drop.rs#L31-L52) 的幽灵卡片画法）。
3. **右栏（已完成）**：`.on_drop(cx.listener(...))` 接收 `Card`，把卡片从 `todo` 搬进 `done`；两栏都加 `.drag_over::<Card>(...)` 高亮（4.2）；右栏再加 `.can_drop(...)` 拒绝已存在的 id。
4. **系统文件**：右栏再注册 `.on_drop::<ExternalPaths>`（4.3），把每个文件路径变成一张新卡片（标题为文件名）；外层窗口容器打印 `FileDropEvent::Exited` 用于清理「正在拖文件」的提示状态。
5. **验证清单**：
   - 卡片从左栏拖到右栏：右栏出现、左栏消失；
   - 拖到两栏之外松手：无变化（拖拽取消）；
   - 拖动过程中两栏分别出现/消失 drag_over 高亮；
   - 从文件管理器拖入 3 个文件：右栏新增 3 张卡片，标题为文件名；
   - 同一张卡片反复横跳（左→右→左，需要左栏也加 on_drop）不丢数据。

提示：不要为「把卡片渲染成拖动视图」另建复杂状态——`on_drag` 的构造闭包里 `cx.new` 一个临时实体即可，投放时真正落库的是闭包外面传进来的 `Card` 值。

## 6. 本讲小结

- **拖放是值语义的**：`on_drag` 交出 `T`，`on_drop::<T>` 按TypeId 接收，中转站是 `App` 上全局唯一的 `active_drag: Option<AnyDrag>`；源与目标彻底解耦。
- **启动有阈值、投递靠命中**：按下存 `pending_mouse_down`（所以拖拽源必须有 `.id()`），移动超过 2px 才开拖；松手时在 Bubble 逆序派发里找「悬停 + 类型匹配 + `can_drop` 通过」的接收者，投给视觉最上层的一个；没人接就整体取消。
- **拖动视图跟手的原理**：每帧 `Window::draw` 把它当第二个根元素，以 `mouse_position - cursor_offset` 为原点重新布局绘制，画在整棵树之后。
- **文件拖入 = 翻译成内部拖拽**：`FileDropEvent::Entered` 构造值为 `ExternalPaths` 的 `AnyDrag`（其视图 Render 为 Empty，图标由平台画），`Pending`/`Submit` 合成 MouseMove/MouseUp，因此 `on_drop::<ExternalPaths>` 一行接入。
- **拖出 = 移交平台**：指针出视口且平台支持时，`external_drag_payload` 的惰性 resolver 产出 `ExternalDragPayload::Files(FileDragPaths)` 交给 `start_external_drag`，内部拖拽挂起进 `PlatformOwnedDrag` 状态机，拖回可恢复、`Ended` 才清理。
- **gestures 的哲学是复用语义事件**：手势竞技场从原始 `TouchEvent` 识别出 tap/pan/pinch 后映射为 `ClickEvent`/`ScrollWheelEvent`/`PinchEvent`；已落地的 `OngoingScroll` 用「主轴锁定（Started 或 28ms 间隔重锁）+ 1.9 倍且 ≥6px 定向解锁」过滤触摸滚动增量，消费点是 `restrict_scroll_to_axis` 的滚动容器。

## 7. 下一步学习建议

本讲补齐了交互机制单元（第 5 单元）的最后一块拼图。接下来：

- **进入第 6 单元「高级 UI 模式」**：建议先读 u6-l1（uniform_list 虚拟化列表）——把本讲的看板换成上千条目时，就需要虚拟化；拖拽与虚拟列表结合（拖动一条目到另一个分组）是 Zed 编辑器里真实存在的模式。
- **u6-l3（deferred 与 anchored）**：拖动过程中常需要「跟随鼠标的提示层」，`deferred` 提供比拖动视图更通式的悬浮层机制，可对照本讲的拖动视图绘制位置（[src/window.rs:L3163-L3169](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L3163-L3169)）理解两层悬浮方案的取舍。
- **想深入平台侧**：`start_external_drag` 的真实实现不在 gpui crate 内，而在平台 crate 里——macOS 在 `crates/gpui_macos/src/window.rs`（L2009-L2013），Linux 仅 Wayland 在 `crates/gpui_linux/src/linux/wayland/window.rs`（L1777-L1781），其余平台落到 [src/platform.rs:L908-L913](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform.rs#L908-L913) 的默认 false。对照着读，能看到一个平台能力如何从 trait 声明一路接到操作系统 API——这为第 7 单元 u7-l1 的 Platform 抽象做好铺垫。
- **想看真实用法**：在 Zed 主仓库（`crates/` 下其余 crate）全局搜索 `on_drag(` 与 `on_drop::<ExternalPaths>`，观察项目面板、标签页等真实组件如何组合本讲 API。
