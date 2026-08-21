# 悬浮层与锚定定位：deferred 与 anchored

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释「悬浮层问题」的本质——为什么下拉菜单、tooltip、右键菜单这类 UI 不能靠普通的元素嵌套实现。
2. 读懂 `Deferred` 元素：它如何让子元素**布局留在原地、绘制延迟到整棵树之后**，以及 `priority` 如何决定多个浮层的层叠顺序。
3. 读懂 `Anchored` 元素：锚点（`Anchor`）如何换算坐标、窗口边界溢出时「翻转锚点」与「吸附窗口边」两种策略如何工作。
4. 独立实现一个带遮挡检测、点击外部关闭、Esc 关闭的 popover / 下拉菜单。

本讲属于「高级 UI 模式」单元，承接 u4-l3 讲过的窗口绘制管线（`draw` 如何驱动整棵元素树走完三阶段），把视角收窄到其中一条特殊支线：**延迟绘制（deferred draw）**。

## 2. 前置知识

本讲默认你已从前面各讲获得以下认知，这里只做一句话唤醒，不再展开：

- **元素三阶段**（u4-l1）：每个元素依次走过 `request_layout`（向 Taffy 申报尺寸）、`prepaint`（拿到最终 `Bounds`、登记 hitbox 与跨帧状态）、`paint`（用 `paint_quad` 等提交绘制）。三阶段是**全树分层推进**的：整棵树先做完 prepaint，再统一进入 paint。
- **绘制顺序**（u4-l3）：`Scene` 里的 `BoundsTree` 按空间相交关系为图元赋 z-order，但根本上，**后 paint 的内容画在先 paint 的内容之上**——元素树的后序遍历顺序就是默认层叠顺序。
- **几何类型**（u3-l4）：`Bounds<Pixels>` = `origin + size`，原点在窗口左上角，坐标单位是逻辑像素。
- **交互监听**（u5-l1 / u5-l2）：监听器在 paint 阶段注册、只活一帧；鼠标事件按 Capture 正序、Bubble 逆序两阶段派发，基于上一帧的 hitbox 命中测试。
- **焦点与按键**（u5-l5 / u5-l4）：键盘事件的派发路径由**焦点**决定，`on_key_down` 只有挂在焦点路径上的元素才会收到。

一个直觉性的问题引出本讲：元素树是「先父子声明、后序绘制」的结构。一个下拉菜单在元素树上住在「按钮」里面，可按钮后面还有大段正文、图形要画——按默认顺序，菜单会被后画的内容盖住，还会被父容器的 `ContentMask`（比如滚动裁剪）裁掉。**悬浮层要做的，就是把「住在树里」和「画在最上」这两件矛盾的事拆开。**

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/elements/deferred.rs` | `Deferred` 元素（约 200 行，含测试） | 三阶段如何「搬走」子元素 |
| `src/elements/anchored.rs` | `Anchored` 元素（约 400 行，含测试） | 定位算法与防溢出 |
| `src/window.rs`（节选） | 窗口运行时 | `DeferredDraw` 结构、`Window::defer_draw`、`prepaint_deferred_draws` / `paint_deferred_draws` |
| `src/geometry.rs`（节选） | 几何基础类型 | `Anchor` 枚举、`Bounds::from_anchor_and_size` |
| `examples/popover.rs` | 官方 popover 示例 | 一级/二级浮层、点击外部关闭 |
| `src/elements/div.rs`（节选） | div 交互 API | `on_mouse_down_out`、`on_key_down` |

## 4. 核心概念与源码讲解

### 4.1 Deferred：布局留在原地，绘制搬到最后

#### 4.1.1 概念说明

`Deferred` 解决的问题是：**一个子元素在布局上属于当前子树（要参与测量、占据位置），但在绘制上必须浮到整个窗口最上层。**

如果没有它，你要么把菜单提升到根视图去渲染（状态与视图结构被迫拆散，组件化崩坏），要么忍受菜单被兄弟内容覆盖、被滚动容器裁剪。`Deferred` 的契约是：

- `request_layout` 照常申报孩子——孩子的 LayoutId 正常挂进 Taffy 树，父容器照常为它预留空间；
- `prepaint` 时把孩子**整体移交**给窗口的延迟绘制队列，自己变成空壳；
- `paint` 什么都不做——孩子的 paint 由窗口在「整棵树画完之后」统一补上。

#### 4.1.2 核心流程

```text
div 按钮
 └─ Deferred
     └─ 浮层内容

帧内执行顺序：
1. request_layout：浮层内容正常申报布局（Deferred 只是透传）
2. 根树 prepaint：走到 Deferred.prepaint 时
   ├── 取出孩子（take，Deferred 从此为空壳）
   └── window.defer_draw(孩子, 当前 element_offset, priority)
3. 根树 prepaint 结束 → 窗口统一 prepaint 所有 deferred_draws（可能多轮，支持嵌套）
4. 根树 paint：Deferred.paint 为空操作
5. 根树 paint 结束 → 窗口统一 paint 所有 deferred_draws（按 priority 升序）
```

#### 4.1.3 源码精读

构造函数只是把孩子擦除为 `AnyElement` 并初始化优先级为 0：

[src/elements/deferred.rs:7-19](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/deferred.rs#L7-L19) —— `deferred(child)` 构造 `Deferred { child: Some(...), priority: 0 }`；结构体只有这两个字段，它是一个纯粹的「转发 + 搬运」元素。

布局阶段原样透传，孩子的 `LayoutId` 直接作为自己的 `LayoutId` 返回，Taffy 树里根本没有「Deferred」这个节点：

[src/elements/deferred.rs:43-52](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/deferred.rs#L43-L52) —— `request_layout` 里 `self.child.as_mut().unwrap().request_layout(window, cx)`，孩子照常参与布局测量。

关键的搬运发生在 prepaint：

[src/elements/deferred.rs:54-66](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/deferred.rs#L54-L66) —— `prepaint` 用 `take()` 把孩子拿走（注意是拿走所有权，不是借用），记录当前的 `window.element_offset()`（父链上累计的位移，比如滚动偏移），然后调用 `window.defer_draw(child, element_offset, self.priority, None)` 把孩子连同上下文一起押进窗口队列。

[src/elements/deferred.rs:68-78](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/deferred.rs#L68-L78) —— `paint` 是**空的**。这是整个机制的点睛之笔：Deferred 自己不画任何东西，孩子的绘制被彻底移交。

`priority` 决定多个延迟绘制之间的层叠关系，值越大越靠近观察者：

[src/elements/deferred.rs:21-29](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/deferred.rs#L21-L29) —— `with_priority` 链式设置优先级（另有语义相同的 `priority` 方法，见 [src/elements/deferred.rs:89-96](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/deferred.rs#L89-L96)）。

文件末尾的回归测试展示了「嵌套 deferred」——popover 里再开 popover，内层 deferred 是在处理外层 deferred 的 round 中新压进队列的：

[src/elements/deferred.rs:98-132](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/deferred.rs#L98-L132) —— 测试视图 `PanelView`：外层 `deferred(anchored()...).with_priority(1)`，其孩子内容里又嵌了一个 `deferred(...).with_priority(2)`。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认「没有 deferred，浮层会被后画的内容盖住」。
2. **操作步骤**：
   1. 打开 `examples/popover.rs`，找到默认打开的浮层（第 98-111 行附近的 `deferred(anchored()...)`）。
   2. 把 `deferred(...)` 这层包装暂时去掉，让 `anchored().anchor(Anchor::TopLeft)...` 直接作为 `button("popover0")` 的 child（`anchored()` 本身实现了 `IntoElement`，可以直接作 child）。
   3. 重新运行 `cargo run -p gpui --example popover`。
3. **需要观察的现象**：浮层不再浮在四条彩色横线之上，而是被后绘制的内容遮挡或裁剪；同时它的布局位置也随元素树位置变化。
4. **预期结果**：恢复 `deferred(...)` 包装后浮层回到最上层。改完记得还原（这只是观察实验）。
5. 若你的运行环境没有图形窗口，此项「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`Deferred::request_layout` 为什么不能像 `prepaint` 那样把孩子 `take()` 走？

**答案**：布局是全树自底向上构建 Taffy 节点的阶段，孩子必须在此阶段正常申报自己的 Style 与孩子节点，父容器才能为它预留空间、测量尺寸。如果布局阶段就搬走孩子，浮层内容将没有 LayoutId，后续 `Anchored` 也拿不到孩子的测量尺寸（`layout_bounds`），整个定位算法无从谈起。搬走只应发生在「布局已完成、prepaint 掌握了上下文」之后。

**练习 2**：`Deferred` 的 `id()` 返回 `None`（[src/elements/deferred.rs:35-37](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/deferred.rs#L35-L37)），这意味着它能否持有跨帧元素状态？

**答案**：不能。u4-l1 讲过，跨帧元素状态以 `(GlobalElementId, TypeId)` 为键存储，没有 id 的元素无法定位自己的状态。`Deferred` 是无状态搬运器，所有状态都应放在它包装的孩子里（这也是为什么 popover 示例中的浮层面板都有 `.id(...)`）。

### 4.2 层叠绘制：窗口如何处理 deferred_draws

#### 4.2.1 概念说明

`Deferred` 只是把孩子押进了队列，真正让浮层「浮起来」的是 `Window` 的两条批处理流水线：`prepaint_deferred_draws` 与 `paint_deferred_draws`。理解它们就理解了 GPUI 的完整层叠顺序。

#### 4.2.2 核心流程

一帧之内，`Window::draw` 的执行顺序是（对照 u4-l3 的绘制管线）：

```text
1. 根元素树 request_layout → Taffy 计算
2. 根元素树 prepaint        ← Deferred.prepaint 在这里押队
3. prepaint_deferred_draws  ← 多轮处理延迟队列（round 1, round 2, ...）
4. 命中测试（基于上一帧 hitbox 的逻辑在本帧 prepaint 后刷新）
5. 根元素树 paint
6. paint_deferred_draws     ← 按 priority 升序补画所有浮层
7. prompt（模态对话框）→ 拖拽视图 → tooltip   ← 比浮层更顶层
```

于是窗口内容自底向上的层叠顺序为：

**普通元素树内容 < deferred 浮层（priority 升序）< 模态 prompt < 拖拽预览 < tooltip**

#### 4.2.3 源码精读

押队时窗口对当前上下文做了一次完整快照：

[src/window.rs:3923-3945](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L3923-L3945) —— `Window::defer_draw` 是 `pub` 方法（自定义元素也能用），断言只能在 prepaint 阶段调用，随后 push 一个 `DeferredDraw`：记录当前视图（`current_view`）、派发树父节点（`parent_node`）、元素 id 栈、文本样式栈、rem 尺寸、内容蒙版与优先级。这些快照保证了浮层脱离原位置绘制后，**文本样式、键位上下文、事件派发路径仍然与它在树中的出身一致**。

`DeferredDraw` 的完整字段：

[src/window.rs:952-964](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L952-L964) —— 注意 `prepaint_range` / `paint_range` 两个区间字段：它们把这次延迟绘制在 hitbox、派发树、场景等数组里占据的区间记录下来，供 u4-l3 讲过的 `.cached()` 视图在下一帧 `reuse_prepaint` / `reuse_paint` 时整段重放。文件内嵌的回归测试 [src/elements/deferred.rs:148-155](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/deferred.rs#L148-L155) 正是为「缓存视图 + 嵌套 deferred」索引错位崩溃而写的。

延迟队列挂在 `Frame`（每帧的渲染快照）上：

[src/window.rs:966-987](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L966-L987) —— `Frame` 结构体的 `deferred_draws: Vec<DeferredDraw>` 字段（第 976 行），与 hitboxes、mouse_listeners、dispatch_tree 并列，都是「帧结束即整体重建」的立即模式数据。

prepaint 阶段的批处理是**多轮**的：

[src/window.rs:3271-3339](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L3271-L3339) —— `prepaint_deferred_draws` 用 `round_start..round_end` 切轮：先处理当前队列里所有条目（按 `priority` 排序），这些条目的 prepaint 又可能压入新的 deferred（嵌套浮层），于是进入下一轮，直到某轮没有新增为止；嵌套深度有硬上限 10 层（第 3292 行的断言），防止无限循环。处理每条时，先恢复它快照的 element_id_stack / text_style_stack / 派发树父节点，再在 `with_rendered_view` + `with_rem_size` + `with_absolute_element_offset` 三个包装器里对元素执行 prepaint。

paint 阶段则一次性统一排序：

[src/window.rs:3341-3378](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L3341-L3378) —— `paint_deferred_draws` 把所有条目（含嵌套轮次压入的）按 `priority` 升序排序后逐个 paint，同样恢复各自的上下文栈并套上记录的 `content_mask`。排序函数在 [src/window.rs:3380-3385](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L3380-L3385)。

这两条流水线在 `draw` 中的挂载点：

[src/window.rs:3115-3169](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L3115-L3169) —— 第 3122 行根树 `prepaint_as_root` 之后紧接着第 3127 行 `prepaint_deferred_draws`；第 3156 行根树 paint 之后是第 3161 行 `paint_deferred_draws`；再往后才是 prompt / 拖拽 / tooltip（第 3163-3169 行），印证了上面的层叠顺序。

#### 4.2.4 代码实践

1. **实践目标**：验证 priority 决定浮层层叠顺序。
2. **操作步骤**：
   1. 运行 `cargo run -p gpui --example popover`，先点开一级浮层，再点其中的 "Child Popover" 打开二级浮层。
   2. 观察二级（蓝色边框）浮层与一级浮层互相重叠的部分谁在上面。
   3. 修改示例中二级浮层的 `.priority(2)` 为 `.priority(0)`（或交换两级优先级），重新运行重复上述操作。
3. **需要观察的现象**：priority 大的浮层画在 priority 小的浮层之上；交换数值后层叠关系反转。
4. **预期结果**：与 [examples/popover.rs:59-75](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/popover.rs#L59-L75) 中二级浮层 `priority(2)`、一级浮层 `priority(1)` 的设置一致——子浮层永远盖住父浮层。
5. 无图形环境则「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `prepaint_deferred_draws` 需要多轮循环，而 `paint_deferred_draws` 只需一趟？

**答案**：嵌套浮层（popover 里的 popover）是在处理外层 deferred 的 prepaint 过程中才压入队列的——第 N 轮处理会为第 N+1 轮生产新条目，所以 prepaint 必须循环到队列不再增长。而 paint 之前所有轮次的条目都已入队，一趟按 priority 全局排序绘制即可。

**练习 2**：`DeferredDraw` 为什么要快照 `text_style_stack` 和 `parent_node`（派发树节点）？

**答案**：浮层脱离原位置、在整树之后绘制，但语义上它仍属于出身的那个子树。快照 `text_style_stack` 让浮层里的文字继承「按下那个按钮处」的文本样式而不是窗口根的默认样式；快照 `parent_node` 让浮层内注册的 `on_click`、`on_action`、`key_context` 仍挂在原子树的派发树节点上——u5-l4 讲过，键位匹配沿焦点到根的派发路径收集 KeyContext，浮层的上下文事实必须跟着它走。

### 4.3 Anchored：锚定定位与防溢出

#### 4.3.1 概念说明

`Deferred` 只解决「画在第几层」，不解决「画在哪里」。`Anchored` 解决位置问题：**以某个参考点为锚，按选定的角贴靠摆放浮层，并在快要溢出窗口时自动调整**。

它的设计目标是「avoid overflowing the window bounds」（避免溢出窗口边界）——典型如下拉菜单贴着按钮下沿展开，按钮贴近窗口底部时自动改为向上展开。

两个核心概念：

- **锚点 `Anchor`**：九宫格式的参考方位（四角 + 四边中点），声明「浮层的哪个角贴在参考点上」。
- **适配模式 `AnchoredFitMode`**：溢出时的两种补救策略——换一个对侧锚点（`SwitchAnchor`，默认），或直接吸附到窗口边缘（`SnapToWindow` / `SnapToWindowWithMargin`）。

#### 4.3.2 核心流程

`Anchored::prepaint` 的定位算法：

```text
输入：
  anchor            锚点（浮层的哪个角贴参考点）
  anchor_position   参考点（窗口坐标，缺省用当前 bounds.origin）
  children_bounds   孩子们的测量尺寸（取并集）
  offset            附加偏移（如 PopoverMenu 的间距）
  viewport_size     窗口可视区尺寸

1. desired = Bounds::from_anchor_and_size(anchor, 参考点 + offset, 尺寸)
      —— 按锚点把"参考点"换算成浮层左上角：
         以 TopRight 为例 x' = x - w（参考点是浮层右上角）
2. 若 fit_mode == SwitchAnchor：
      若 desired 水平溢出窗口 → 尝试换成水平对侧锚点（翻转后不溢出才采纳）
      若 desired 垂直溢出窗口 → 尝试换成垂直对侧锚点（同上）
3. 边缘吸附（所有 fit_mode 都做，margin 版加上边距与 client_inset）：
      若右缘越界  x' -= (right - limit_right + margin_right)
      若左缘越界  x'  = limit_left + margin_left
      垂直方向同理
4. offset = desired.origin - bounds.origin（取整）
   window.with_element_offset(offset, 孩子们 prepaint)
      —— 通过"元素位移栈"平移孩子，而非改 Taffy 布局
```

边缘吸附的数学即：

\[ x' = x - \big(\text{right}(B) - \text{right}(L) + m\big) \quad \text{当 } \text{right}(B) > \text{right}(L) \]

其中 \(B\) 是浮层当前期望边界，\(L\) 是窗口可视区边界，\(m\) 是右边距。

#### 4.3.3 源码精读

锚点枚举与换算函数住在 geometry.rs：

[src/geometry.rs:2163-2182](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/geometry.rs#L2163-L2182) —— `Anchor` 的八个方位。

[src/geometry.rs:837-868](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/geometry.rs#L837-L868) —— `Bounds::from_anchor_and_size`：把「参考点 + 锚点」换算成浮层左上角。锚点 TopLeft 时参考点就是左上角原样使用；TopRight 时参考点被解释为浮层**右上角**，故 \( x' = x - w \)；BottomCenter 则 \( x' = x - w/2,\ y' = y - h \)，以此类推。

[src/geometry.rs:2217-2240](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/geometry.rs#L2217-L2240) —— `Anchor::other_side_along(axis)`：沿指定轴取对侧锚点（TopLeft 的水平对侧是 TopRight），这是「翻转」的原子操作。

`Anchored` 的字段与默认值：

[src/elements/anchored.rs:16-36](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/anchored.rs#L16-L36) —— 默认 `Anchor::TopLeft`、`SwitchAnchor` 适配、`Window` 定位模式、无参考点（用当前 bounds 原点）、无偏移。配置方法集中且短：[src/elements/anchored.rs:38-78](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/anchored.rs#L38-L78) 依次是 `anchor`、`position`（显式给窗口坐标参考点）、`offset`（锚定后再偏移）、`position_mode`（Window / Local 两种坐标系）、`snap_to_window`、`snap_to_window_with_margin`。

布局阶段用 `position: Absolute` 让浮层**脱离常规流**：

[src/elements/anchored.rs:98-120](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/anchored.rs#L98-L120) —— `request_layout` 先正常申报所有孩子（拿到各自 LayoutId，存进 `AnchoredState`），再以 `Position::Absolute` + `Display::Flex` 的 Style 申报自身。绝对定位意味着它不挤占父容器的空间、不影响兄弟布局——这是浮层的第二个必要条件（第一个是 deferred 的层级）。

两种定位模式的坐标解释：

[src/elements/anchored.rs:252-289](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/anchored.rs#L252-L289) —— `AnchoredPositionMode::Window` 把 `position()` 给的点解释为窗口坐标（缺省用 `bounds.origin`，即「不设 position 就贴在它所在的树位置」）；`Local` 解释为相对父元素的坐标（`bounds.origin + anchor_position + offset`）。

定位主体在 prepaint：

[src/elements/anchored.rs:131-148](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/anchored.rs#L131-L148) —— 先把所有孩子的布局边界取**并集**得到整体尺寸（支持多个 child），再调 `get_position_and_bounds` 得到期望边界 `desired` 与参考点 `origin`。

[src/elements/anchored.rs:150-180](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/anchored.rs#L150-L180) —— `SwitchAnchor` 翻转逻辑：先查水平方向（左溢或右溢），尝试换成水平对侧锚点重算，**只有翻转后不再溢出才采纳**；再查垂直方向同理。两轴独立处理，所以四个角都能翻到。

[src/elements/anchored.rs:182-205](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/anchored.rs#L182-L205) —— 边缘吸附：注意这段对**所有** fit_mode 生效（`SnapToWindow` 只是跳过了前面的锚点翻转），margin 版额外加上用户边距与 `client_inset`（平台窗口的客户区内缩，如系统标题栏区域）。右溢就左移差值，左溢就钉在左边界——浮层比窗口还宽时表现为靠左对齐。

最后通过元素位移栈完成移动：

[src/elements/anchored.rs:207-215](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/anchored.rs#L207-L215) —— 计算 `desired.origin - bounds.origin` 并取整（避免亚像素模糊），然后在 `window.with_element_offset(offset, ...)` 里给孩子们做 prepaint。

[src/window.rs:3572-3602](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L3572-L3602) —— `with_element_offset` 把位移压入 `element_offset_stack`，这就是 u6-l1 讲滚动时提过的同一套机制：**位移是绘制期的平移，不回写 Taffy 布局**。一个重要推论：放在滚动容器里的 anchored 浮层，其参考点若来自 `bounds.origin`，会随内容滚动而移动；若显式给窗口坐标 `position(...)`，则固定在窗口某处不随滚动变化——文件内嵌测试 `test_anchored_position_when_scrolled`（[src/elements/anchored.rs:349-378](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/anchored.rs#L349-L378)）验证的正是滚动 1000px 后菜单仍钉在窗口坐标 (100,100)。

paint 阶段只做透传：

[src/elements/anchored.rs:217-230](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/anchored.rs#L217-L230) —— 逐个 `child.paint`，定位已在 prepaint 完成。

#### 4.3.4 代码实践

1. **实践目标**：用源码内嵌测试验证定位与吸附算法，不依赖图形环境。
2. **操作步骤**：
   1. 运行 `cargo test -p gpui --lib elements::anchored`。
   2. 阅读三个测试（[src/elements/anchored.rs:329-398](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/anchored.rs#L329-L398)）：`test_anchored_position_without_scroll` 断言 200x300 菜单放在 (100,100)；`test_anchored_position_when_scrolled` 断言滚动后仍在 (100,100)；`test_anchored_snaps_to_window` 把参考点设为 (100,500)，窗口高 600，断言菜单原点被吸附到 (100,300)。
   3. 自己算一遍第三个测试：500 + 300 = 800 > 600，底部溢出 200px，吸附后 y' = 800 - 200 = 600？但断言是 300——因为 `position(100,500)` 配 `Anchor::TopLeft` 时 desired 是 (100,500)-(300,800)，垂直溢出触发 `other_side_along(Vertical)` 翻转……注意该测试用的是 `snap_to_window()`，翻转被跳过，直接走吸附：y -= (800 - 600) = 200，即 y' = 300。两种路径殊途同归，确认你算得出 300。
3. **需要观察的现象**：三个测试全部通过；手算与断言一致。
4. **预期结果**：`(100, 300)` 正是 `desired.bottom() - limits.bottom()` 的差值左移结果。
5. 测试运行结果以本地为准；如无法编译运行则「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`Anchored` 为什么要在 `request_layout` 里用 `Position::Absolute`，而普通 div 不用？

**答案**：浮层不能挤占布局空间——下拉菜单展开时不该把按钮下方的正文推开。绝对定位让 Taffy 把它从常规流里拿出来，父容器与兄弟的布局完全不受影响；它的最终位置由 prepaint 的锚定算法决定，而不是 Taffy 的 flexbox 求解。

**练习 2**：`SwitchAnchor` 与 `SnapToWindow` 各适合什么场景？

**答案**：`SwitchAnchor`（默认）适合「贴边翻转」类 UI：菜单贴按钮下沿，按钮在底部就翻到上方展开，保持完整大小、位置跟随锚点。`SnapToWindow`/`WithMargin` 适合「宁可靠边也不能出界」的大浮层：内容比剩余空间还大时翻转也救不了，只能贴住窗口边并留边距（如全屏化的选择列表）。注意源码里吸附逻辑对所有模式兜底生效——翻转失败最终也会被吸附回来。

**练习 3**：`Anchored` 的 `offset` 与 `position` 有什么区别？

**答案**：`position` 设定**参考点**在哪（锚点贴靠的那个点），`offset` 是参考点确定后的**附加微调**（[src/elements/anchored.rs:53-56](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/anchored.rs#L53-L56) 的文档点名 PopoverMenu 场景：锚到元素后再往下让出一点间距）。两者在 `get_position_and_bounds` 里叠加：`anchor_position + offset`。

### 4.4 popover 模式：deferred + anchored + 交互收口

#### 4.4.1 概念说明

把 4.1-4.3 串起来，一个完整的 popover 由四件事组成：

1. **状态开关**：一个 `bool`（或更复杂的枚举）挂在宿主视图上，决定浮层是否渲染——`.when(self.open, |this| this.child(deferred(...)))`。
2. **层级与定位**：`deferred(anchored()...)` 组合，`priority` 拉开层叠。
3. **点击外部关闭**：`on_mouse_down_out`——鼠标按下发生在本元素 bounds **之外**时的 Capture 阶段回调。
4. **键盘关闭（Esc）**：两种路线——给浮层 `track_focus` 并聚焦后用 `on_key_down` 检查 escape 键；或按 u5-l3/u5-l4 定义 `Dismiss` action + 键位绑定 + `on_action`。注意 GPUI **没有**元素级 `on_blur` 方法，等效能力是 `window.on_focus_out(handle, cx, ...)`（[src/window.rs:4928-4934](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L4928-L4934)），它以 `Subscription` 形式存活、在 paint 阶段之外的窗口层注册，适合「焦点离开浮层就关」的需求。

#### 4.4.2 核心流程

```text
用户点按钮
  → cx.listener 改 this.open = true; cx.notify()
  → 下一帧 render 里 .when(self.open, ...) 挂上 deferred(anchored().child(菜单面板))
  → 浮层绘制在整树最上层，锚定在按钮旁
用户点浮层外
  → MouseDown 事件 Capture 阶段先于目标元素到达
  → 菜单面板的 on_mouse_down_out 触发（命中不在面板 bounds 内）
  → this.open = false; cx.notify() → 下一帧浮层消失
用户按 Esc
  → 键盘事件沿焦点路径派发（浮层需 track_focus 且持有焦点）
  → on_key_down 匹配 keystroke.key == "escape" → 关闭
```

#### 4.4.3 源码精读

示例的状态与两个复用组件：

[examples/popover.rs:10-42](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/popover.rs#L10-L42) —— `HelloWorld` 持有 `open` / `secondary_open` 两个开关；`button()` 与 `popover()` 两个辅助函数搭出按钮和白色圆角阴影面板的样式。

二级浮层（嵌套 deferred 的活样本）：

[examples/popover.rs:44-79](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/popover.rs#L44-L79) —— `render_secondary_popover`：按钮点击置 `secondary_open = true`；`.when` 条件挂上 `deferred(anchored()...).priority(2)`，面板用 `on_mouse_down_out` 关闭自己。这个内层 deferred 是在处理外层浮层的 prepaint 轮次中才入队的，正对应 4.2 讲的多轮机制。

主渲染里三个值得咀嚼的细节：

[examples/popover.rs:98-111](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/popover.rs#L98-L111) —— 默认打开的浮层 `priority(0)`，`anchored().anchor(Anchor::TopLeft).snap_to_window_with_margin(px(8.))`，贴按钮左上角、留 8px 边距。

[examples/popover.rs:112-153](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/popover.rs#L112-L153) —— 点击打开的一级浮层 `priority(1)`。第 135 行的 `.when(!self.secondary_open, ...)` 很精妙：**二级浮层打开期间，一级浮层不再挂 `on_mouse_down_out`**——否则点击二级浮层内容（它也在一级面板 bounds 之外）会把一级误关掉。第 143-144 行的注释「Here we need render popover after the content to ensure it will be on top layer」提示了 child 顺序对层叠的影响：嵌套浮层的宿主按钮要放在内容之后声明。

[examples/popover.rs:155-165](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/popover.rs#L155-L165) —— 四条彩色横线画在按钮行**之后**：专门用来肉眼验证浮层确实盖在后绘制的内容之上（去掉 deferred 立刻被盖住，见 4.1.4 实践）。

点击外部关闭的底层定义：

[src/elements/div.rs:932-943](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L932-L943) —— `on_mouse_down_out` 的文档：任意按键、Capture 阶段、鼠标在**本元素 bounds 之外**按下时触发。Capture 阶段先于 Bubble，所以「点到外面别处」在事件到达那个「别处」之前就能先把浮层收掉。

Esc 监听的入口：

[src/elements/div.rs:1070-1080](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/elements/div.rs#L1070-L1080) —— `on_key_down`：Bubble 阶段的按键回调。结合 u5-l5：元素需要 `.id(...)` + `track_focus(&handle)` 进入焦点路径，打开浮层时 `window.focus(&handle)`，这里的回调才会被触发。

#### 4.4.4 代码实践

1. **实践目标**：为一级浮层补上 Esc 关闭。
2. **操作步骤**：
   1. 给 `HelloWorld` 加一个 `focus_handle: FocusHandle` 字段，在 `cx.new` 闭包里用 `cx.focus_handle()` 创建。
   2. 给一级浮层面板加 `.id("popover-1").track_focus(&self.focus_handle)`，并在 `on_click` 打开浮层时（`this.open = true` 处）同时 `window.focus(&this.focus_handle)`（listener 签名里有 `window` 参数）。
   3. 面板上挂 `.on_key_down(cx.listener(|this, event: &KeyDownEvent, _, cx| { if event.keystroke.key == "escape" { this.open = false; cx.notify(); } }))`。
   4. 运行示例，点开浮层后按 Esc。
3. **需要观察的现象**：Esc 关闭浮层；点击浮层内部不关闭；点击外部仍经 `on_mouse_down_out` 关闭。
4. **预期结果**：两条关闭路径独立生效。若 Esc 无反应，排查点按 u5-l4 的排查清单：多半是浮层没进焦点路径（没 track_focus 或没 focus）。
5. 无图形环境则「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `on_mouse_down_out` 用的是 Capture（捕获）阶段而不是 Bubble（冒泡）阶段？

**答案**：鼠标事件先 Capture（根→叶）再 Bubble（叶→根）。点击浮层外的某个按钮时，若「外部点击」监听走 Bubble，那个按钮自己的 `on_click` 会先执行，随后才轮到浮层关闭——顺序上尚可，但若目标元素在 Bubble 中 `stop_propagation`（u5-l1），浮层就永远收不到通知。Capture 阶段在最前面，浮层必然第一时间得知「按下点不在我的 bounds 里」，不依赖任何目标元素的行为。

**练习 2**：把浮层内容渲染在 `.child(...)` 的第一个位置而不是最后一个（对照 [examples/popover.rs:143-147](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/examples/popover.rs#L143-L147) 的注释），会发生什么？

**答案**：child 声明顺序决定元素树遍历顺序，也决定同一轮 deferred 的入队先后。嵌套浮层的宿主按钮放在内容之前，会让二级 deferred 在队列中先于「后声明的兄弟内容」处理；由于二级浮层 priority 更高（2 > 1），最终层叠仍正确，但同优先级浮层之间的顺序就会随声明顺序颠倒。惯例是**后声明的画在上面**，所以「内容在前、嵌套浮层宿主在后」是稳妥写法。

**练习 3**：`on_mouse_down_out` 挂在浮层面板上而不是宿主按钮上，为什么？

**答案**：判定标准是「按下点是否在**本元素** bounds 之外」。浮层打开后，「点外部」是相对浮层面板而言的（点浮层内部属于正常交互，不该关闭）。挂在按钮上会把「点击浮层自身」也判为外部点击，浮层一打开就被误关。

## 5. 综合实践：自定义下拉菜单

把本讲四件事（层级、定位、外部点击、键盘关闭）拼成一个可运行的下拉菜单。以下代码均为**示例代码**（基于 `examples/popover.rs` 改写，非项目原有代码）。

**任务**：一排按钮各带一个下拉菜单；菜单贴按钮下沿展开；按钮贴近窗口底边时菜单自动翻到按钮上方；点击菜单外部、按 Esc、选中某项均关闭菜单。

**步骤**：

1. **建文件**：复制 `examples/popover.rs` 为 `examples/dropdown.rs`，并在 `Cargo.toml` 的 examples 段仿照 [Cargo.toml:167-169](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/Cargo.toml#L167-L169) 的格式新增：

   ```toml
   [[example]]
   name = "dropdown"
   path = "examples/dropdown.rs"
   ```

   （更省事的做法是直接在 `popover.rs` 上改，观察完还原。）

2. **状态建模**：视图持有 `open: bool`、`focus_handle: FocusHandle`（`cx.focus_handle()` 创建）、以及选项列表。

3. **渲染骨架**（核心结构，省略样式细节）：

   ```rust
   // 示例代码
   fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
       div()
           .size_full()
           .flex()
           .flex_col()
           .justify_end()          // 把按钮压到窗口底部，制造“必须翻转”的场景
           .items_center()
           .p_8()
           .child(
               div()
                   .id("trigger")
                   .child("Choose ▾")
                   .on_click(cx.listener(|this, _, window, cx| {
                       this.open = !this.open;
                       if this.open {
                           window.focus(&this.focus_handle);
                       }
                       cx.notify();
                   }))
                   .when(self.open, |this| {
                       this.child(
                           deferred(
                               anchored()
                                   // 贴按钮左下沿：参考点取按钮左上角，用 TopLeft + 垂直翻转，
                                   // 或直接 Anchor::BottomLeft + 向下 offset
                                   .anchor(Anchor::BottomLeft)
                                   .snap_to_window_with_margin(px(8.))
                                   .child(self.render_menu(window, cx)),
                           )
                           .priority(1),
                       )
                   }),
           )
   }

   fn render_menu(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
       div()
           .id("dropdown-menu")
           .track_focus(&self.focus_handle)
           .w_48()
           .rounded_md()
           .border_1()
           .bg(gpui::white())
           .shadow_lg()
           .on_mouse_down_out(cx.listener(|this, _, _, cx| {
               this.open = false;
               cx.notify();
           }))
           .on_key_down(cx.listener(|this, event: &KeyDownEvent, _, cx| {
               if event.keystroke.key == "escape" {
                   this.open = false;
                   cx.notify();
               }
           }))
           .children(self.items.iter().enumerate().map(|(ix, item)| {
               div()
                   .id(("item", ix))       // ElementId 必须唯一，防状态串扰（u5-l2）
                   .px_3()
                   .py_1()
                   .child(item.clone())
                   .on_click(cx.listener(move |this, _, _, cx| {
                       this.selected = Some(ix);
                       this.open = false;
                       cx.notify();
                   }))
           }))
   }
   ```

4. **运行**：`cargo run -p gpui --example dropdown`。

**需要观察的现象**：

1. 菜单默认出现在按钮下方（`justify_end` 把按钮压到底部后，下方空间不足，`snap_to_window` / 锚点策略把菜单调整到不越界的位置——试着把 `justify_end` 换成 `justify_start` 对比按钮在顶部与底部时菜单的方向差异）。
2. 点击菜单选项：菜单关闭且选择被记录。
3. 点击窗口空白处：菜单关闭。
4. 菜单打开时按 Esc：菜单关闭。
5. 窗口缩到很小时菜单不会超出窗口边界（`snap_to_window_with_margin(px(8.))` 兜底）。

**预期结果**：以上五点全部成立。若 Esc 无效，检查菜单是否 `track_focus` 且打开时 `window.focus`；若点击选项把菜单直接关掉却没记录选择，检查选项的 `.id` 是否唯一；若菜单被窗口底部裁掉，确认 `snap_to_window_with_margin` 在链上。

本实践涉及编译运行，具体效果**待本地验证**。

## 6. 本讲小结

- **悬浮层问题**的实质是「住在树里」与「画在最上」的矛盾；GPUI 用两个正交元素拆解它：`Deferred` 管层级，`Anchored` 管位置。
- `Deferred` 在 prepaint 时用 `window.defer_draw` 把孩子**连同样式栈、元素 id 栈、派发树父节点的完整快照**押入 `Frame.deferred_draws` 队列，自己的 paint 为空；孩子的布局照常参与 Taffy 树。
- 窗口在根树之后分两条流水线批处理延迟队列：`prepaint_deferred_draws` 多轮循环支持嵌套（深度上限 10），`paint_deferred_draws` 按 `priority` 升序统一绘制；完整层叠顺序是**普通树 < deferred（priority 升序）< prompt < 拖拽 < tooltip**。
- `Anchored` 以绝对定位申报布局（不挤占父空间），prepaint 时按 `Anchor` 锚点换算期望边界，溢出窗口时先尝试**换对侧锚点**（`SwitchAnchor`），再做**边缘吸附**（所有模式兜底），最终通过 `with_element_offset` 位移栈平移孩子——位移不回写 Taffy，所以窗口坐标定位的浮层不随滚动移动。
- popover 的标准配方：`bool` 开关 + `.when` 条件挂载 + `deferred(anchored()...)` + 面板上的 `on_mouse_down_out`（Capture 阶段外部点击关闭）+ `track_focus` + `on_key_down`（Esc 关闭）；嵌套浮层靠更高的 `priority` 与多轮 prepaint 机制自然层叠。
- `DeferredDraw` 记录的 `prepaint_range` / `paint_range` 让 `.cached()` 视图重放浮层区间——立即模式的浮层也能享受保留模式的缓存复用。

## 7. 下一步学习建议

- **下一讲 u6-l4（动画系统）**：学会用 `.with_animation` 给浮层的出现/消失加过渡，弹簧物理（`spring.rs`）是 GPUI 动画的核心。
- **对照真实工程用法**：Zed 主仓库 `crates/ui` 里的 `PopoverMenu` / `ContextMenu` 是本讲模式的工业化版本（状态机 + anchored + 焦点管理），建议在读完本讲后去对照 `PopoverMenu` 的 open/close 处理与本讲 4.4 的差异。
- **回看源码**：带着本讲结论重读 [src/window.rs:3271-3339](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/window.rs#L3271-L3339) 的多轮循环注释——它解释了为什么必须**原地处理**而非搬出再放回（prepaint_index 快照与 `reuse_prepaint` 切片的一致性），这是连接 u4-l3 渲染缓存的最后一环。
- 若想加深锚定直觉，可修改 `test_anchored_snaps_to_window` 的参考点与窗口尺寸，手算期望边界后改断言验证（改完还原）。
