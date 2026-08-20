# 窗口绘制管线：从 refresh 到 draw

## 1. 本讲目标

学完本讲，你应该能够：

1. 完整描述从 `cx.notify()` 到屏幕像素更新的整条链路：谁把窗口标脏、谁唤醒平台、平台在哪个回调里调用 `Window::draw`、画完的 `Scene` 交给谁。
2. 读懂 `Window::draw` 与 `draw_roots` 如何驱动整棵元素树走完 request_layout → prepaint → paint 三阶段，并理解 `next_frame` / `rendered_frame` 双缓冲与视图缓存复用的配合。
3. 解释元素 arena 的「每帧 bump 分配、帧末整体回收」策略，以及 `ArenaBox` 如何用有效标志防止 use-after-clear。
4. 区分三种触发重绘的方式——`cx.notify()`、`window.refresh()`、`cx.refresh_windows()`——它们在「是否绕过视图缓存」上的差异，并用日志验证。
5. 说出 `bounds_tree`（R-tree 变体）在 `Scene` 中的职责：为相互重叠的图元赋予 z-order，使绘制批次可以按层叠关系排序。
6. 识别提交 `1861e58f98` 在本讲的源码区域里埋下的 `profiler` 可观测性钩子（`window_id`、`record_frame_pending`、`foreground_turn`、present 计时、`kind_name`）——它们不改变调度语义，但会反复出现在你读到的代码里。

## 2. 前置知识

本讲建立在你已掌握的概念之上，先快速对齐词汇：

- **三阶段生命周期（u4-l1）**：每个元素依次走过 `request_layout`（向 Taffy 申报样式与孩子）、`prepaint`（拿到最终 bounds、登记 hitbox）、`paint`（用 `paint_quad` 等提交绘制）。本讲回答的问题是：**谁**在**什么时机**驱动整棵树走这三步。
- **实体与 notify（u2 系列）**：`Entity<T>` 是状态句柄；`cx.notify()` 声明「我的状态变了，请重绘」。u3-l1 讲过 notify 会把窗口标脏，本讲把这条链路一字不落地追到平台事件循环。
- **视图缓存（u3-l1/u3-l6）**：只有用 `entity.cached(style)` 显式包装的内嵌视图才有渲染缓存——prepaint 时若 bounds、content_mask、text_style 都没变、视图不脏、且窗口不在 refresh，就直接「重放」上一帧录制的区间，跳过 render。窗口根视图没有这层包装，每帧必然 render。
- **单前台线程与 RefCell 借用（u2-l1）**：所有更新都发生在主线程，`App` 被 `AppCell`（`RefCell`）包裹。绘制期间再借用 App 会 panic，这个约束解释了本讲多处「防御性」代码。
- **Scene 与图元（u4-l4 预告）**：paint 阶段产出的不是像素，而是 `Quad`、`Path`、`Shadow` 等图元，攒进 `Scene`；帧末交给平台渲染器。本讲的 `bounds_tree` 就挂在 `Scene` 内部。
- **profiler feature**：gpui 有一个可选的 `profiler` feature，编译进前后台日志与卡顿检测等观测设施。本讲的多个源码位置带有 `#[cfg(feature = "profiler")]` 埋点——默认构建里它们不存在，读代码时可以先跳过；u7-l6 会专门拆解这套子系统。

一个贯穿全讲的直觉：**GPUI 的每帧是「全量重建、局部复用」**。元素树每帧从根重建（立即模式），但重建的成本被三样东西压住——Taffy 布局树每帧重算但只算一次、缓存命中的视图子树直接重放录制区间、元素本体住进 bump arena 让「每帧重建」几乎只是指针推进。

## 3. 本讲源码地图

| 文件 | 行数规模 | 本讲关注点 |
| --- | --- | --- |
| `src/window.rs` | 约 7500 行 | `WindowInvalidator`（标脏）、`Window` 结构、`refresh`、`draw`、`draw_roots`、双 `Frame`、`on_request_frame` 帧回调、`ElementArenaScope`/`ArenaClearNeeded`；以及提交 `1861e58f98` 埋入的 profiler 钩子 |
| `src/app.rs` | 约 3100 行 | `App::notify`、`record_entities_accessed`（倒排表）、`open_window` 的首帧绘制、`flush_effects` 中测试平台的同步绘制、`refresh_windows`；profiler 构建下的 `foreground_journal` 字段与访问器 |
| `src/arena.rs` | 约 400 行 | `Arena`/`Chunk`/`ArenaBox`：bump 分配、scope 推迟清除、有效标志 |
| `src/bounds_tree.rs` | 约 470 行 | `BoundsTree`：R-tree 变体，为重叠 bounds 赋 z-order |
| `src/scene.rs` | 约 300 行 | `Scene` 如何用 `BoundsTree` 计算 `order` 并按 order 排序批次 |
| `src/view.rs` | 约 500 行 | `ViewElement` 的缓存命中/未命中分支（连接 u3-l1 与本讲） |

## 4. 核心概念与源码讲解

### 4.1 Window：一帧所需的全部状态

#### 4.1.1 概念说明

`Window` 是 GPUI 中「一个窗口在一帧内需要的一切」的聚合体。它不是 UI 框架里常见的「控件容器」——元素树每帧重建，根本不存放在 Window 里。Window 存放的是**驱动重建的机器**：布局引擎、双缓冲的两个帧、各种栈（元素 id、文本样式、内容遮罩）、失效标记，以及与平台窗口（`Box<dyn PlatformWindow>`）的连接。

理解 Window 的关键，是把它看成三块：

1. **平台连接**：`platform_window`、`sprite_atlas`、`scale_factor`、`viewport_size`。
2. **帧机器**：`layout_engine`（Taffy）、`rendered_frame` / `next_frame`（双缓冲）、`dirty_views`、`invalidator`。
3. **绘制期栈**：`element_id_stack`、`text_style_stack`、`content_mask_stack`、`rendered_entity_stack` 等——只在 draw 期间有意义，帧首帧尾必须为空（`draw` 里有 `debug_assert!` 检查）。

#### 4.1.2 核心流程

`Window` 的生命周期与窗口事件循环同构：

```text
Window::new（创建 + 注册平台回调，含 on_request_frame）
    ↓
App::open_window 内部：装载根视图 → 立刻 draw 一次（保证窗口至少画过一帧）
    ↓
之后每帧：平台发来帧请求 → on_request_frame 回调 → 视情况 Window::draw + present
    ↓
窗口关闭：removed = true → App 移除
```

#### 4.1.3 源码精读

先看结构体全貌（节选关键字段，注释为讲解所加）：

[window.rs:1132-1209](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L1132-L1209) —— `Window` 结构体定义：`platform_window` 是平台窗口的 trait 对象；`layout_engine` 持有 Taffy；`root: Option<AnyView>` 是窗口的根视图（类型擦除）；`rendered_frame` 与 `next_frame` 是双缓冲的两个 `Frame`；`dirty_views` 是本帧需要重渲染的实体集合；`refreshing` 标记「本帧无视缓存」。

其中 `Frame` 是一帧产出的所有副产品的集合：

[window.rs:966-987](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L966-L987) —— `Frame` 结构体：`scene`（要交给渲染器的图元）、`hitboxes`（命中盒）、`dispatch_tree`（动作派发树）、`mouse_listeners`（鼠标监听器）、`element_states`（跨帧元素状态，u4-l1 讲过以 `(GlobalElementId, TypeId)` 为键）、`deferred_draws`（deferred/anchored 浮层）、`input_handlers`（输入法句柄）、`tab_stops`（Tab 导航表）。

注意 `mouse_listeners`、`hitboxes` 也都在 Frame 里——这解释了 u5 会讲到的现象：**事件监听是 paint 阶段注册、随帧过期重建的**，它们不是持久的对象。

再看 `refresh`，本讲标题里的第一个关键词：

[window.rs:2017-2022](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L2017-L2022) —— `Window::refresh`：仅当当前不在绘制中（`not_drawing()`）才生效；置 `refreshing = true` 并把 invalidator 标脏。`refreshing` 会让所有缓存视图在本帧强制重新 render（见 4.3.3）。

与之相对的 App 级版本：

[app.rs:1056-1058](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1056-L1058) —— `App::refresh_windows`：把 `Effect::RefreshWindows` 压入效果队列。

[app.rs:1781-1788](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1781-L1788) —— `apply_refresh_effect`：效果冲刷时对**所有**窗口置 `refreshing = true` 并标脏——这就是「全局忽略缓存重绘」的实现，主题切换之类的场景用它。

#### 4.1.4 代码实践

**实践目标**：把 `Window` 的约 70 个字段按「平台连接 / 帧机器 / 绘制期栈」三类手工分组，验证你对结构的理解。

**操作步骤**：

1. 打开 [window.rs:1132-1209](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L1132-L1209)，把每个字段抄进三栏笔记。
2. 用 grep 验证两个猜想：
   - `rendered_entity_stack` 只在 draw 期间被压入弹出——搜索 `rendered_entity_stack`，确认 `draw` 开头与结尾各有一次 `debug_assert!` 为空检查（[window.rs:2868](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L2868) 与 [window.rs:2966](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L2966)）。
   - `element_states` 真正的家在 `Frame` 而不是 `Window` 上（u4-l1 的跨帧元素状态存在这里，帧间靠 `Frame::finish` 搬运）。

**需要观察的现象 / 预期结果**：分组完成后你会发现「绘制期栈」类字段全部是 `Vec`/`SmallVec` 且名字带 `stack`，它们是三阶段遍历的上下文通道；「帧机器」类字段在 `draw` 里被读写。这是纯阅读实践，无运行结果。

#### 4.1.5 小练习与答案

**练习 1**：`Window` 里的 `root: Option<AnyView>` 为什么是 `AnyView` 而不是泛型 `Entity<T>`？

**答案**：窗口需要在不关心具体类型的情况下持有任意类型的根视图（`App::open_window` 是泛型入口，但 `App.windows` 里存的是类型擦除的窗口集合）。`AnyView` 是「`AnyEntity` + 一个 render 函数指针」（u3-l1），`draw_roots` 只需调用 `into_any_element()` 就能把它变成元素，无需知道 T 是什么。

**练习 2**：为什么 `mouse_listeners` 的类型是 `Vec<Option<AnyMouseListener>>` 而不是 `Vec<AnyMouseListener>`？

**答案**：因为缓存复用。`reuse_paint` 重放上一帧的鼠标监听器时用 `.take()` 把它们从 `rendered_frame` **搬走**（移动语义），搬过的槽位变成 `None`，但 Vec 长度不变——这样本帧新注册的监听器仍落在相同的索引区间，缓存里记录的 `paint_range` 索引才不会错位（[window.rs:3474-3479](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L3474-L3479)）。同理 `input_handlers` 也是 `Vec<Option<...>>`。

### 4.2 重绘调度：从 cx.notify() 到平台帧请求

#### 4.2.1 概念说明

GPUI 是按需绘制（demand-driven）的：没有变化就没有帧。把「变化」变成「一帧」的工作由三方协作完成：

- **`App::notify`**：查倒排表「哪些窗口正在显示这个实体」，通知它们的失效器。
- **`WindowInvalidator`**：每个窗口一个的标脏器，持有 `dirty` 标志、`dirty_views` 集合、当前 `DrawPhase`，以及能唤醒平台帧源的 `platform_waker`；profiler 构建下还携带 `window_id`，用于把标脏事件归属到具体窗口并上报前台日志。
- **平台帧回调 `on_request_frame`**：注册在平台窗口上，平台每次「愿意画一帧」时调用它；它检查 dirty 决定是否真的 `draw`。

`WindowInvalidator` 被设计成 `Rc<RefCell<...>>` 的可克隆句柄，因为 `App` 的倒排表（实体 → 显示它的窗口）也要持有它——notify 发生时窗口可能不在被更新状态，必须经由共享句柄访问。

#### 4.2.2 核心流程

```text
cx.notify()（某实体的更新闭包内）
    ↓
App::notify(entity_id)
    ├─ 查 window_invalidators_by_entity[entity_id]
    ├─ 用 tracked_entities 过滤出「此刻真的在显示该实体」的窗口
    ├─ 无活跃窗口 → 只入队 Effect::Notify（观察者路径，u2-l3）
    └─ 有活跃窗口 → 对每个 invalidator 调 invalidate_view
              ↓
WindowInvalidator::invalidate_view
    ├─ dirty_views.insert(entity_id)            ← 记下「谁脏了」
    ├─ draw_phase == None（不在绘制中）:
    │     dirty = true（首次变脏时调 platform_waker 唤醒平台）
    │     （profiler 构建：首次变脏时上报 journal::record_frame_pending）
    │     入队 Effect::Notify（观察者仍要收到通知）
    └─ draw_phase != None（绘制中）: 只登记，不标脏 —— 防止当前帧自我触发新帧
              ↓
平台被唤醒 → 下一帧回调 on_request_frame
    ├─ invalidator.is_dirty() == true → Window::draw + present
    └─ is_dirty() == false → 不画（或只 present 未提交的内容）
```

#### 4.2.3 源码精读

先看 `App::notify`——链路的起点：

[app.rs:2666-2697](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L2666-L2697) —— `App::notify`：取走该实体名下的失效器列表，用 `tracked_entities`（窗口 → 本帧访问过的实体，帧末由 `record_entities_accessed` 重建）过滤出仍在显示它的窗口，逐个调用 `invalidate_view`。注释点明倒排表是单调增长的，必须过滤才能把失效范围收紧到「现在真的在显示它」的窗口。

再看失效器本体：

[window.rs:119-129](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L119-L129) —— `WindowInvalidatorInner`：`dirty`（窗口要不要重画）、`draw_phase`（当前处于哪阶段）、`dirty_views`（哪些实体脏了）、`platform_waker`（唤醒平台帧源的闭包）。注意第一个字段 `window_id` 是 `#[cfg(feature = "profiler")]` 的——提交 `1861e58f98` 给它补上了窗口身份，使标脏事件能被归到具体窗口上报给前台日志（u7-l6）。

[window.rs:149-163](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L149-L163) —— `WindowInvalidator::new`：现在接收 `window_id` 参数（profiler 构建下存入 inner，非 profiler 构建用 `#[allow(unused_variables)]` 吞掉）。`Window::new` 处以 `WindowInvalidator::new(handle.window_id())` 传入（[window.rs:1421](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L1421)）。

[window.rs:165-190](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L165-L190) —— `invalidate_view`：先无条件登记 `dirty_views`，再分支——不在绘制中则标脏并唤醒平台；在绘制中直接返回。实体 id 留在 `dirty_views` 里，等下一次 draw 开头被消费（见 4.3.3 的 `invalidate_entities`）。profiler 构建下还有一步：`record_frame_dirty` 返回本帧首个脏时间戳，若本次调用让窗口「由干净变脏」，就在释放 RefCell 借用后调用 `profiler::journal::record_frame_pending(window_id, dirty_at)`——把「这帧是因为谁、什么时候开始待画」记入前台工作日志。

[window.rs:196-216](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L196-L216) —— `set_dirty`：供 resize、focus 等窗口级变化使用——这类变化没有具体实体，直接把整窗标脏。同样在 profiler 构建下，由干净变脏时会带着时间戳上报 `record_frame_pending`。

[window.rs:231-236](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L231-L236) —— `wake_platform`：有些平台对空闲窗口会停止请求帧，必须显式唤醒帧源，下一帧回调才会到来。

最后是帧回调的主体（节选主干）：

[window.rs:1533-1543](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L1533-L1543) —— `Window::new` 里向平台窗口注册 `on_request_frame` 回调；`invalidator`、`active`、`needs_present`、`next_frame_callbacks` 都以 `Rc` 克隆的方式捕获进闭包，回调可在无窗口借用的情况下检查它们。回调第一行是 profiler 构建下的 `let _foreground_turn = profiler::journal::foreground_turn();`——一个 RAII 守卫，把「这一次前台 turn」标记进日志流，drop 时封口（细节在 u7-l6）。

[window.rs:1544-1563](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L1544-L1563) —— 重入防护：若本线程已有 draw 在栈上（如 Windows 窗口过程嵌套泵消息时又来了帧请求），直接跳过并记下 `force_render`，等当前 draw 展开后再补一帧。这是单前台线程 + `RefCell` 借用模型的直接后果——嵌套 draw 会撞上 App 的可变借用而 panic。

[window.rs:1632-1651](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L1632-L1651) —— 帧回调的心脏：`if invalidator.is_dirty() || force_render` 才 `window.draw(cx)` + `window.present()`；`force_render` 时先 `window.refresh()` 绕过视图缓存（注释举例：GPU 设备恢复后不能重放过期的图集引用）。否则若只是有内容未提交（`needs_present`）就仅 `present`。

[window.rs:1659-1666](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L1659-L1666) —— 帧尾再武装：若 draw 过程中窗口又被标脏、或还有 `on_next_frame` 回调待跑，显式唤醒平台，保证帧流不断。

还有一个常被忽略的首帧细节：

[app.rs:1264-1269](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1264-L1269) —— `open_window` 在返回前**同步 draw 一次**：注释解释在 Windows 上经常输给 `on_request_frame` 的竞态，返回一个从未渲染过的窗口会导致 `DispatchTree::root_node_id` 断言失败。所以「窗口至少画过一帧」是被构造函数保证的不变量。

**可观测性埋点速览（profiler feature 专用）**：本讲的调度链路上，提交 `1861e58f98` 共埋了五类钩子，全部包在 `#[cfg(feature = "profiler")]` 里，默认构建完全不参与编译，调度语义零变化——

| 埋点 | 位置 | 作用 |
| --- | --- | --- |
| `window_id` 字段 | [window.rs:120-121](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L120-L121) | 让失效器知道自己是哪个窗口，上报时带上身份 |
| `record_frame_pending` | [window.rs:178-181](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L178-L181) | 窗口「由干净变脏」的那一刻记入前台日志 |
| `foreground_turn` 守卫 | [window.rs:1542-1543](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L1542-L1543) | 把一次帧回调标记为一个前台活动区间 |
| present 起止计时 | [window.rs:3017-3028](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L3017-L3028) | 记录提交帧消耗的墙钟时间（见 4.3.3） |
| 输入类别标注 | [window.rs:5010-5011](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L5010-L5011) | `dispatch_event` 用新增的 `PlatformInput::kind_name()` 给日志标注 `"key_down"`、`"scroll_wheel"` 等输入类别 |

这些钩子写入的 `ForegroundJournal` 挂在 `App` 上（profiler 构建下 `App` 有 `foreground_journal` 字段与 `foreground_journal()` 访问器，[app.rs:692-693](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L692-L693) 与 [app.rs:1929-1934](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1929-L1934)）。本讲只需认得它们、读代码时能一眼跳过；整套子系统在 u7-l6 拆解。

#### 4.2.4 代码实践

**实践目标**：用日志直观看到「帧不是凭空来的，是 notify 标脏 + 平台回调送来的」。

**操作步骤**：

1. 复制 `examples/opacity.rs` 为 `examples/frame_trace.rs`（或直接修改后还原）。
2. 在 `render` 开头加一行 `eprintln!("render: opacity={}", self.opacity);`。
3. 运行 `cargo run -p gpui --example opacity`，观察点击面板后的日志流。
4. 把 [opacity.rs:69](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/opacity.rs#L69) 的 `window.request_animation_frame()` 注释掉再运行。

**需要观察的现象**：

- 第 3 步：动画期间每帧一条日志，透明度从 0 递增到 1 后日志停止。
- 第 4 步：只剩一两条日志——`start_animation` 里 `cx.notify()` 触发的那一帧之后，没有任何东西继续标脏，帧流停止。

`request_animation_frame` 的实现正是「用下一帧回调反向 notify 当前视图」：

[window.rs:2371-2374](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L2371-L2374) —— `request_animation_frame`：登记一个 `on_next_frame` 回调，回调里 `cx.notify(entity)`。帧回调（[window.rs:1614-1623](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L1614-L1623)）会在 draw 前统一跑掉这些回调——于是 notify → 标脏 → 本帧就画，形成连续帧流。

**预期结果**：动画驱动 = `cx.notify()` 驱动，二者是同一条调度链路；删掉动画帧请求后应用静止即零帧。具体日志条数与刷新率相关，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `invalidate_view` 在绘制中（`draw_phase != None`）不把窗口标脏？脏实体岂不是丢了？

**答案**：绘制中再标脏会让「当前帧」触发对下一帧的请求，极易形成帧内自激循环。实体 id 在阶段检查**之前**已插入 `dirty_views`（[window.rs:168](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L168)），不会丢——下一次 `draw` 开头的 `invalidate_entities` 会把它并入窗口的 dirty 集合。持续动画场景则由 `on_next_frame` 回调在帧尾维持帧流。

**练习 2**：`cx.notify()` 一个没有任何窗口显示的实体，会发生什么？

**答案**：`App::notify` 过滤后 `live_invalidators` 为空，走 [app.rs:2687-2691](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L2687-L2691) 的分支：入队 `Effect::Notify`，观察者（`cx.observe`）仍会被调用，但不会有任何窗口被标脏、不会有帧。notify 的「重绘」语义只对正在被显示的实体成立。

**练习 3**：`record_frame_pending` 为什么放在 `drop(inner)` 之后调用，而不是在 RefCell 借用期间直接调？

**答案**：看 [window.rs:175-181](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L175-L181)：`window_id` 与 `dirty_at` 都是先拷贝出来的值，`drop(inner)` 归还失效器的借用后才进入 journal 上报。这样被上报路径触发的任何代码（journal 内部同样可能经由 App 访问失效器）不会再撞上这笔已持有的 `borrow_mut`——这正是 u2-l1 讲过的单线程重入防御习惯。

### 4.3 Window::draw 与 draw_roots：一帧的完整流水线

#### 4.3.1 概念说明

`Window::draw` 是本讲的标题角色：它不画任何东西，而是**编排**一次完整的三阶段遍历，并把产出的 `Frame` 与上一帧交换。`draw_roots` 则是编排中的核心乐章——从根元素出发走完 prepaint（内含 request_layout，u4-l1 讲过布局申报发生在 prepaint 之前的请求阶段）与 paint。

双缓冲是理解 draw 的钥匙：`rendered_frame` 是「现在屏幕上/用于命中测试与事件派发的上一帧」，`next_frame` 是「正在构建的本帧」。构建完成后二者交换。**缓存复用正是靠读取 `rendered_frame`、写入 `next_frame` 实现的**。

#### 4.3.2 核心流程

`Window::draw`（[window.rs:2854-2991](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L2854-L2991)）按顺序做八件事：

```text
1. 进入元素 arena 作用域（ElementArenaScope::enter，见 4.4）
2. invalidate_entities：把 invalidator.dirty_views 排干，并入 window.dirty_views
   （mark_view_dirty 沿上一帧 dispatch_tree 把祖先视图也标脏）
3. set_dirty(false)：清「需要重画」标志，本帧开始
4. draw_roots（除非 cx.mode.skip_drawing()）：
     a. 阶段置 Prepaint
     b. 根元素 request_layout → stretch_auto_size_to_fill → prepaint_as_root
        （request_layout 阶段各视图的 render() 在这里被调用）
     c. prepaint_deferred_draws：多轮处理 deferred 浮层（u6-l3 的基础）
     d. prompt / 拖拽 / tooltip 之一做 prepaint
     e. mouse_hit_test = next_frame.hit_test(鼠标位置)   ← 命中测试发生在 paint 前！
     f. 阶段置 Paint；根元素 paint；paint_deferred_draws；prompt/拖拽/tooltip paint
5. dirty_views.clear()（脏集合只活一帧）
6. 收尾：Taffy 树 clear、text_system.finish_frame、
   next_frame.finish(&mut rendered_frame)（搬运被访问过的 element_states）
7. 阶段置 Focus；交换 rendered_frame ↔ next_frame；next_frame.clear()
   焦点路径变化则派发 focus 事件
8. record_entities_accessed（重建「窗口↔实体」倒排表）、reset_cursor_style、
   refreshing = false、needs_present = true、退出 arena 作用域返回清除凭证
```

随后 `present`（[window.rs:3016-3031](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L3016-L3031)）把 `rendered_frame.scene` 交给 `platform_window.draw`，由平台渲染器变成像素。

#### 4.3.3 源码精读

**draw 的骨架**：

[window.rs:2862-2869](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L2862-L2869) —— 进入 per-App 元素 arena 作用域；`invalidate_entities` 消化失效器积压的脏视图；`set_dirty(false)` 表示「脏债已还，本帧开始」。

[window.rs:2889-2902](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L2889-L2902) —— `cx.mode.skip_drawing()` 为真（如 headless 测试模式）时跳过绘制本体；画完后 `dirty_views.clear()`——脏集合的消费窗口就是这一帧。

[window.rs:2920-2928](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L2920-L2928) —— 帧收尾四连：Taffy 布局树整树丢弃（每帧重建，u4-l2）；文本系统收帧（字形布局缓存交接）；`next_frame.finish(&mut rendered_frame)`；**交换双缓冲**。`DrawPhase::Focus` 之后 `rendered_frame` 已是新帧。

[window.rs:2966-2978](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L2966-L2978) —— `record_entities_accessed` 重建倒排表；`refreshing = false` 结束本帧的「无视缓存」状态；若焦点监听器又移动了焦点，补一次 `refresh`；`needs_present = true` 交给 present。

**invalidate_entities 与祖先传播**：

[window.rs:3007-3013](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L3007-L3013) —— `invalidate_entities`：取走失效器的 `dirty_views` 逐个 `mark_view_dirty` 再放回空集。

[window.rs:1940-1952](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L1940-L1952) —— `mark_view_dirty`：沿 `rendered_frame.dispatch_tree.view_path_reversed(view_id)` 从该视图向根遍历，把沿途所有视图插进 `dirty_views`——**子视图变脏会连坐祖先**，因为父视图的 render 输出可能包含子视图。遇到已在集合里的就提前 break（其祖先必然已插入）。

**draw_roots 的三阶段驱动**：

[window.rs:3112-3122](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L3112-L3122) —— 根元素三步走：`request_layout`（整棵树自底向上向 Taffy 申报，此时非缓存视图的 render 被调用）、`stretch_auto_size_to_fill`（窗口根像 web 的根元素一样默认撑满视口）、`prepaint_as_root`（触发整棵树的 prepaint，Taffy 一次性求解布局）。

[window.rs:3152-3169](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L3152-L3169) —— 关键时序：先基于**刚构建的** `next_frame.hitboxes` 做一次命中测试存为 `mouse_hit_test`，然后才进入 Paint 阶段开始 paint。命中盒在 prepaint 登记、paint 前即可查询——hover 样式正是靠它生效的。随后根元素 paint、浮层 paint。

**present 与提交计时**：

[window.rs:3016-3031](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L3016-L3031) —— `present`：把 `rendered_frame.scene` 交给 `platform_window.draw` 后清掉 `needs_present`。注意其中的 profiler 埋点：进入时同样持有 `foreground_turn` 守卫（present 也算一段前台工作），`present_start`/`Instant::now()` 夹住平台提交调用，把起止时间连同窗口是否活跃、有无待跑帧回调一起交给 `window_profiler.record_present(...)`——这是 `1861e58f98` 对 `record_present` 签名的扩展（从单一时间点改为起止区间），供 u7-l6 的卡顿检测计算 dirty-to-present 延迟。顺带一提，`present_if_needed` 的可见范围也从 `bench` 扩为 `any(bench, all(test, profiler))`（[window.rs:3033-3043](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L3033-L3043)），让 profiler 下的测试能像 benchmark 一样显式提交帧。

**视图缓存的命中与未命中**（连接 u3-l1）：

[view.rs:380-401](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/view.rs#L380-L401) —— `ViewElement::prepaint` 的缓存判定：`bounds`、`content_mask`、`text_style` 三者与缓存键一致，且实体不在 `dirty_views`，且 `!window.refreshing`——四个条件全满足则 `reuse_prepaint` 重放上一帧录制的 prepaint 区间并补记访问过的实体，**render 不被调用**。

[view.rs:403-418](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/view.rs#L403-L418) —— 未命中分支：临时把 `window.refreshing` 置真（阻止缓存的子视图在测量期间被错误复用）、调用 `view.render(...)` 重建元素、`layout_as_root` + `prepaint_at`，并用 `detect_accessed_entities` 记录本次渲染读过的实体（供倒排表与下一轮缓存判定用）。

[view.rs:470-476](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/view.rs#L470-L476) —— paint 侧的对应分支：prepaint 产出了新元素就真画；否则 `reuse_paint` 重放录制区间。

[window.rs:3461-3497](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L3461-L3497) —— `reuse_paint`：把 `rendered_frame` 里对应区间的鼠标监听器、输入句柄、光标样式、Tab 停靠点、文本行布局、**以及 Scene 的绘制操作**（`scene.replay`，见 4.5.3）整体搬到 `next_frame`。缓存复用不是「跳过绘制」而是「重放录制」——监听器和图元一个不少，只是不用重新计算。

**倒排表的重建**：

[app.rs:1111-1138](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1111-L1138) —— `record_entities_accessed`：帧末，把本帧（含重放补记）访问过的实体登记为「该窗口正在显示」，并从旧集合里摘除不再显示的实体。4.2 的 notify 过滤正是查这张表。

**测试环境下的同步绘制**：

[app.rs:1694-1706](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1694-L1706) —— `flush_effects` 尾声：在 `test`/`test-support`/`bench` 构建下，效果队列清空后对所有脏窗口直接 `window.draw(cx)`。测试没有平台帧循环，这一段就是「假帧源」——这也是 u7-l4 `run_until_parked` 能让断言看到 UI 状态的原因。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：用日志区分三种重绘触发方式的实际工作量——`cx.notify()`（子视图脏，缓存视图不重渲染）vs `window.refresh()`（全窗口绕过缓存）。

**操作步骤**：

1. 新建 `examples/render_count.rs`（下面的完整示例为**示例代码**，基于 [hello_world.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/hello_world.rs) 与 [opacity.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/opacity.rs) 的骨架改写）：

```rust
#![cfg_attr(target_family = "wasm", no_main)]

use gpui::{
    App, Bounds, ClickEvent, Context, Entity, StyleRefinement, Window, WindowBounds,
    WindowOptions, div, prelude::*, px, rgb, size,
};
use gpui_platform::application;

/// 一个可被缓存的子视图：每次 render 都打印日志。
struct Child {
    label: &'static str,
    renders: usize,
}

impl Render for Child {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        self.renders += 1;
        eprintln!("[child {}] render #{}", self.label, self.renders);
        div()
            .flex_1()
            .h_16()
            .bg(rgb(0x505050))
            .text_color(rgb(0xffffff))
            .child(format!("{}: #{}", self.label, self.renders))
    }
}

struct Root {
    cached_child: Entity<Child>,
    root_renders: usize,
}

impl Root {
    fn notify_cached_child(&mut self, _: &ClickEvent, _: &mut Window, cx: &mut Context<Self>) {
        self.cached_child.update(cx, |_, cx| cx.notify());
    }

    fn refresh_window(&mut self, _: &ClickEvent, _: &mut Window, cx: &mut Context<Self>) {
        cx.refresh_windows(); // 或 window.refresh()，效果相同：置 refreshing + 标脏
    }
}

impl Render for Root {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        self.root_renders += 1;
        eprintln!("[root] render #{}", self.root_renders);
        div()
            .flex()
            .flex_col()
            .gap_2()
            .p_4()
            .size(px(500.))
            .text_color(rgb(0xffffff))
            .child(
                div()
                    .id("notify-child")
                    .bg(rgb(0x2b6cb0))
                    .on_click(cx.listener(Self::notify_cached_child))
                    .child("点我：notify 缓存子视图"),
            )
            .child(
                div()
                    .id("refresh-window")
                    .bg(rgb(0x9b2c2c))
                    .on_click(cx.listener(Self::refresh_window))
                    .child("点我：refresh 整个窗口"),
            )
            // 关键对照：同一实体，用 .cached() 包装，启用渲染缓存。
            .child(self.cached_child.clone().cached(StyleRefinement::default().size_full()))
    }
}

fn run_example() {
    application().run(|cx: &mut App| {
        let bounds = Bounds::centered(None, size(px(500.), px(500.)), cx);
        cx.open_window(
            WindowOptions {
                window_bounds: Some(WindowBounds::Windowed(bounds)),
                ..Default::default()
            },
            |_, cx| {
                cx.new(|cx| Root {
                    cached_child: cx.new(|_| Child { label: "cached", renders: 0 }),
                    root_renders: 0,
                })
            },
        )
        .unwrap();
        cx.activate(true);
    });
}

#[cfg(not(target_family = "wasm"))]
fn main() {
    run_example();
}
```

2. 若新建文件，需要在 `Cargo.toml` 的 `[[example]]` 清单里补一项（参照既有示例的声明方式），或直接把代码覆盖到一个不用的示例里运行。
3. 运行 `cargo run -p gpui --example render_count`，依次点击两个按钮各一次，观察终端日志。

**需要观察的现象与预期结果**（矩阵）：

| 操作 | root render | cached child render | 原因 |
| --- | --- | --- | --- |
| 点「notify 缓存子视图」 | +1 | +1 | 子视图在 `dirty_views` 中，缓存失效（[view.rs:390](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/view.rs#L390) 命中否定条件） |
| 点「refresh 整个窗口」 | +1 | +1 | `refreshing = true` 无视缓存（[view.rs:391](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/view.rs#L391)） |

真正的差异要加一个「notify 别人」的对照：再放一个**未缓存**的普通子视图 `.child(plain_child.clone())`，点「notify 缓存子视图」时它会随根一起每帧重渲染，而缓存子视图的日志在「notify 根/其他视图」时**不增长**（缓存命中走 `reuse_paint`）。注意：窗口根视图没有缓存包装，任何一帧它必然 render——所以 root 的日志计数实际等于「画了多少帧」。具体数字**待本地验证**。

4. 思考题自测：把 `.cached(...)` 换成普通 `.child(self.cached_child.clone())`，两个按钮的日志矩阵会变成什么样？（答案见练习 1。）

#### 4.3.5 小练习与答案

**练习 1**：去掉 `.cached()` 包装后，「notify 缓存子视图」按钮点击时日志会怎样？

**答案**：root 与 child 各 +1（和原来一样）；但任何其他原因引发的帧（如 notify 根、focus 变化）也会让 child +1——普通 `ViewElement` 走 [view.rs:332-341](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/view.rs#L332-L341) 的非缓存分支，`request_layout` 阶段每帧调用 `render`。缓存 opt-in 的意义就在于此：`.cached()` 让子树获得「没脏就不重算」的能力。

**练习 2**：为什么 `mouse_hit_test` 要在 Paint 阶段开始**前**计算，而不是等整帧画完？

**答案**：因为 hover 样式（`.hover:bg(...)`）在 paint 阶段就要判断「鼠标是否悬停在本命中盒上」，判断依据是 `mouse_hit_test` 的结果；而命中盒在 prepaint 已经登记进 `next_frame.hitboxes`。把命中测试插在两个阶段之间（[window.rs:3152](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L3152)），本帧的 hover 状态用的就是本帧的布局结果，不会落后一帧。

**练习 3**：`dirty_views` 为什么在 `draw` 末尾 clear，而不是 draw 开头？

**答案**：draw 开头要先 `invalidate_entities` 把失效器积压的脏视图**并入**它（并入时还要沿 dispatch_tree 连坐祖先），随后本帧的缓存判定（[view.rs:390](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/view.rs#L390)）要查询它，所以必须画完（[window.rs:2902](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L2902)）才能清。脏集合的生命周期恰是一帧：帧首汇聚、帧中消费、帧尾清空。

### 4.4 元素 arena：每帧 bump 分配与安全回收

#### 4.4.1 概念说明

元素树每帧重建意味着：每帧要分配成千上万个 `Div`、`StyledText`、`AnyElement` 等临时对象，帧末全部丢弃。用普通堆分配（每对象一次 `malloc`/`free`）代价太高。GPUI 的答案是**区域分配器（bump allocator / arena）**：向系统申请大块内存（1MB 的 chunk），对象在这块内存上指针递增式地「堆叠」，帧末不做逐个 free，而是把偏移量归零——一次 O(1) 的操作「释放」了全部对象。

代价是：arena 里的对象必须由 arena 统一析构（arena 记录每个对象的 drop 函数），且 clear 之后不允许再访问旧指针。为了在开发期抓住这类 bug，`ArenaBox` 携带一个共享的 `valid` 标志，clear 时翻转，之后任何解引用都会 panic。

#### 4.4.2 核心流程

```text
App 启动：App.element_arena = Arena::new(1024 * 1024)   ← 每个 App 一块（测试隔离）
每帧 draw：
    ElementArenaScope::enter(&cx.element_arena)
      → arena.begin_scope()（scope_depth += 1）
      → CURRENT_ELEMENT_ARENA 指向这块 arena（线程局部）
    draw_roots：元素构造时 alloc → ArenaBox<T>（bump 推进）
    arena_scope.exit(&cx.element_arena) → 返回 ArenaClearNeeded 凭证
    调用方（帧回调/测试/首帧）执行 arena_clear_needed.clear(cx)
      → scope_depth == 0 ? force_clear() : 推迟（外层 draw 还在引用内存）
force_clear：
    valid 标志翻 false（旧 ArenaBox 全部失效）
    逐个 drop 已登记的对象 → chunk 偏移归零（内存保留复用，容量只增不减）
```

#### 4.4.3 源码精读

[arena.rs:81-88](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/arena.rs#L81-L88) —— `Arena` 结构：`chunks`（大块内存）、`elements`（drop 函数登记表）、`valid`（代际标志）、`current_chunk_index`、`scope_depth`。

[app.rs:721](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L721) 与 [app.rs:869](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L869) —— arena 挂在 `App` 上（`element_arena: RefCell<Arena>`，初始 1MB）。**每 App 一块**而不是全局一块，是为了多个测试 App 互不污染。

[arena.rs:160-210](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/arena.rs#L160-L210) —— `alloc`：当前 chunk 装得下就 bump 推进；装不下就翻到下一 chunk（不够则再分配一块并打 trace 日志）；比 chunk 还大的对象直接 panic。分配后把 `(指针, drop 函数)` 登记进 `elements`，返回携带 `valid` 标志的 `ArenaBox`。对齐通过 `align_offset` 整数运算保证（溢出安全）。

[arena.rs:133-158](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/arena.rs#L133-L158) —— `clear` 与 `force_clear`：有活跃 scope 时**推迟**清除（嵌套 draw 的内存外层还在用）；真正清除时翻转 valid 标志、逐个 drop、chunk 偏移归零、回到第一个 chunk。容量不缩减——下一帧直接复用，这正是「每帧分配回收」的低成本来源。

[arena.rs:213-234](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/arena.rs#L213-L234) —— `ArenaBox` 与 `validate`：解引用前断言 valid——「attempted to dereference an ArenaRef after its Arena was cleared」。元素代码若把 `ArenaBox` 存进跨帧状态，开发期立刻暴露。

[window.rs:329-367](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L329-L367) —— 两层线程局部：兜底的 `ELEMENT_ARENA`（无 App 上下文时用）与 `CURRENT_ELEMENT_ARENA`（指向当前 App 的 arena）。`with_element_arena` 是元素分配的统一入口——优先当前 App 的 arena，保证测试隔离。中间夹着的 `draw_in_progress()`（[window.rs:350-352](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L350-L352)）直接检查这个指针是否被设置，正是 4.2.3 帧回调里重入防护的判定依据。

[window.rs:369-452](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L369-L452) —— `ElementArenaScope`：进入时 `begin_scope` 并记录先前的 arena 指针；`exit` 返回清除凭证；**Drop 里做清理**——即使 draw 中途 panic 回栈，scope 深度也能被正确减回（注释详细解释了若只在 exit 清理，panic 会导致深度永久抬高、之后每次 clear 都被无限推迟、内存无界泄漏）。

[window.rs:454-484](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L454-L484) —— `ArenaClearNeeded`：`#[must_use]` 的清除凭证，只能由 `ElementArenaScope::exit` 产生；`clear(cx)` 断言传入的是同一个 App（清错 App 的 arena 会释放别的 draw 还在引用的内存）。draw 返回它、调用方负责 clear 的设计，把「何时清」的决策权交给驱动帧的一方。

#### 4.4.4 代码实践

**实践目标**：观察 arena 的容量增长与「容量只增不减」特性。

**操作步骤**：

1. 运行一个元素较多的示例并打开 trace 日志：`RUST_LOG=trace cargo run -p gpui --example uniform_list`，观察终端中形如 `increased element arena capacity to ...kb` 的行（对应 [arena.rs:183-186](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/arena.rs#L183-L186)）。
2. 反复滚动列表，注意容量日志是否在滚动过程中持续增长（帧间复用，不再增长），还是每帧都增长。
3. 阅读单元测试 [arena.rs:339-377](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/arena.rs#L339-L377)（`test_clear_deferred_while_scope_active`）：注意嵌套 scope 中 clear 被推迟、外层结束时才真正 drop 的断言顺序。

**需要观察的现象 / 预期结果**：初始 1024kb；只有当某帧元素总量超过当前容量时才追加 chunk 并打一条日志；稳态后（界面不再变复杂）不再出现容量日志——bump 分配的「回收」只是偏移归零，不产生任何分配器噪声。trace 日志量较大，建议配合终端过滤。具体数值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Arena::clear` 不能在 `scope_depth > 0` 时执行 `force_clear`？

**答案**：嵌套 draw（如 Windows 窗口过程中重入的帧请求、或 draw 中开新窗口）共享同一块 arena。内层 draw 结束时，外层 draw 的元素还活在 arena 里且仍会被解引用；此时 force_clear 会翻转 valid 标志并 drop 这些对象，外层随后访问就是 use-after-free。所以 clear 被推迟到最外层 scope 结束（见 `test_clear_deferred_while_scope_active` 的断言）。

**练习 2**：`ArenaBox<T>` 与 `Box<T>` 的本质区别是什么？

**答案**：`Box` 拥有独立堆内存、drop 时释放自身内存；`ArenaBox` 只是一个指向 arena 内存的裸指针 + 代际 valid 标志，**不拥有内存**，drop 也不做任何事（析构由 arena 在 clear 时统一执行）。它的价值是廉价的分配（bump 推进）加上代际检查（catch use-after-clear）。

**练习 3**：如果某个元素想把 `ArenaBox` 存进自己的跨帧元素状态（`with_element_state`），会发生什么？

**答案**：下一帧 arena clear 后 valid 翻 false，再解引用就 panic「attempted to dereference an ArenaRef after its Arena was cleared」（[arena.rs:329-336](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/arena.rs#L329-L336) 的测试演示了这一点）。跨帧状态必须拥有数据（`Box`/`Rc`/值类型），arena 只服务「本帧的元素树」。

### 4.5 bounds_tree：为 Scene 图元赋予 z-order

#### 4.5.1 概念说明

paint 阶段各元素向 `Scene` 提交图元的顺序是「元素树遍历序」，但**层叠顺序**不等于遍历序：绝对定位的浮层、deferred 元素后画却要在最上面，两个不重叠的元素彼此无关，重叠的元素必须区分谁压谁。GPU 渲染器希望每个图元批次按 z-order 排序后一次画完。

GPUI 的做法：`Scene` 内部维护一棵 `BoundsTree`（R-tree 变体），每插入一个图元就查询「与它相交的所有已插入图元中最大 order 是多少」，新 order = 最大相交 order + 1。不相交的图元可以复用同一个 order 值——排序键保持小整数，排序代价低。u6-l3 将讲的 deferred 浮层，正是在 paint 顺序上后处理，靠这套 order 机制获得正确的层叠。

#### 4.5.2 核心流程

插入图元时：

\[ \text{order}(b_{new}) = 1 + \max\{\, \text{order}(b) \mid b \in T,\ b \cap b_{new} \neq \varnothing \,\} \]

（空集时 max 取 0。）直觉：**只有空间上压到别人的图元才需要更高的序号**。两串互不重叠的图元各自都是 order=1，排序后仍保持各自的相对插入序（稳定排序下）。

查询最大相交 order 的复杂度：R-tree 剪枝搜索，最坏 \( O(n) \)，但常见情形（新图元与当前最大 order 的叶子相交）命中 O(1) 快路径——树里缓存了「全局最大 order 的叶子」。

#### 4.5.3 源码精读

[bounds_tree.rs:9-35](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/bounds_tree.rs#L9-L35) —— `BoundsTree`：文档注释直说它是「为重叠 UI 元素分配 z-order 而设计的 R-tree 变体」，优化点包括 O(1) 快路径（缓存 max_leaf）、较高的分支因子（`MAX_CHILDREN = 12`，树更矮、cache miss 更少）、基于 `max_order` 元数据的激进剪枝。

[bounds_tree.rs:120-136](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/bounds_tree.rs#L120-L136) —— `insert`：`find_max_ordering` 求最大相交 order，加一作为新 order，插入叶子并维护 `max_leaf`。注意每个内部节点都缓存子树的 `max_order`，children 数组维持「最大 order 的孩子在末尾」的不变量，搜索时优先弹出。

[bounds_tree.rs:139-193](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/bounds_tree.rs#L139-L193) —— `find_max_ordering`：先试快路径（查询框与全局最大 order 的叶子相交则直接返回）；否则沿树搜索，用「子树 max_order 不超过当前结果就跳过」「空间不相交就跳过」双重剪枝。

[scene.rs:87-101](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/scene.rs#L87-L101) —— `Scene::insert_primitive`：图元 bounds 先与 content_mask 求交（完全被裁掉的直接丢弃），order 取自当前图层栈或 `primitive_bounds.insert(clipped_bounds)`，写进图元的 `order` 字段后放入对应批次 Vec。

[scene.rs:151-163](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/scene.rs#L151-L163) —— `Scene::finish`：帧末按 order 对 shadow/quad/path/sprite 各批次排序——这就是 `next_frame.finish` 调用链（[window.rs:2922](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L2922)）里 scene 的收尾动作，draw 与 present 之间完成。

[scene.rs:141-149](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/scene.rs#L141-L149) —— `Scene::replay`：缓存复用的另一半——`reuse_paint` 重放的图元不是拷贝旧数据，而是把上一帧的绘制**操作序列**（`PaintOperation`）重新走一遍 `insert_primitive`，order 由本帧的 bounds_tree 重新赋予。这保证了「重放 + 新画」混合出一帧时层叠关系仍然正确。

#### 4.5.4 代码实践

**实践目标**：通过单元测试理解 order 分配规则，不运行 GUI。

**操作步骤**：

1. 阅读 [bounds_tree.rs:380-435](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/bounds_tree.rs#L380-L435) 的 `test_insert`：三个 10×10 的 bounds 依次从 (0,0)、(5,5)、(10,10) 起插入。
2. 先在纸上预测每次 `insert` 的返回值，再对照断言。
3. 运行 `cargo test -p gpui bounds_tree`（纯逻辑测试，不需要窗口系统）。
4. 再看 [bounds_tree.rs:437-471](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/bounds_tree.rs#L437-L471) 的 `test_random_iterations`：用 1000 个随机种子、每次最多 100 个随机 AABB，与暴力计算的期望 order 逐一比对。

**需要观察的现象 / 预期结果**：`test_insert` 的断言依次是 1、2、3（三者两两相交，order 递增），随后三个互不相交/部分相交的 bounds 返回 1、1、2（bounds4/5 与谁都不相交回到 1，bounds6 压到 bounds4 得 2）。`test_random_iterations` 通过即证明 order 公式与暴力法等价。此实践可在本仓库直接运行验证；若环境受限则**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：三个两两重叠的图元 A、B、C 按 A、B、C 顺序插入，各得多少 order？若 A 与 B 不相交、B 与 C 不相交、A 与 C 相交呢？

**答案**：第一种：A=1、B=2、C=3（每个都压到前面所有图元）。第二种：A=1；B 与 A 不交得 1；C 与 A 相交（也与 B 相交？不，题设 B、C 不相交）——C 只与 A 相交，max=1，得 2。可见 order 只编码「必须分层」的约束，不编码插入序。

**练习 2**：`Scene::replay` 为什么不直接复制上一帧排序好的批次，而要重放操作、重新走 bounds_tree？

**答案**：因为本帧的场景是「复用区间 + 新画区间」交错拼接的：某个缓存的子视图重放图元时，窗口里可能新增了与它重叠的浮层。重新插入让每个图元的 order 在**本帧的空间关系**下计算，`finish` 再统一排序，混合场景的层叠才正确。直接复制会带着上一帧的 order 参与本帧排序，重叠关系可能已经变了。

**练习 3**：`MAX_CHILDREN = 12` 的取舍是什么？

**答案**：分支因子越大树越矮（比较次数少、指针跳转少），但每个内部节点扫描孩子列表的线性开销变大。注释给出的取舍是「更矮的树 = 更少 cache miss，但每个节点工作更多」——12 是针对这种「小规模、频繁插入、按 max_order 剪枝」负载的经验值。

## 5. 综合实践

**任务：做一个「帧率与渲染计数器」调试小工具。** 在 4.3.4 的 `render_count` 示例基础上扩展：

1. **帧计数**：在根视图 `render` 里每次递增并显示 `frames: N`——由于根视图无缓存，它的 render 次数就等于窗口实际绘制的帧数（本讲验证过的结论）。
2. **缓存命中率**：给缓存子视图维护 `renders` 计数，界面上显示 `cached child renders: M`；`M / N` 即缓存未命中率。
3. **动画对照**：加一个复选/按钮，按下时在 render 里调用 `window.request_animation_frame()`（参照 [opacity.rs:63-71](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/opacity.rs#L63-L71) 的写法）：观察帧计数每秒稳定增长约等于显示器刷新率的量，而缓存子视图计数**不变**（动画只 notify 根视图，缓存子视图不脏、缓存键未变，一直命中）。
4. **强制失效**：再点一次「refresh 整个窗口」，两个计数同时 +1，且 `refreshing` 绕过缓存（[view.rs:391](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/view.rs#L391)）。

完成后，你手上就有了一个可以回答「这次改动到底触发了多少渲染工作量」的仪器——把它保留下来，后续学习 u6-l1 虚拟化列表、u6-l4 动画时都用得上。运行结果与具体计数**待本地验证**。

## 6. 本讲小结

- **调度链路**：`cx.notify()` → `App::notify` 查倒排表过滤出正在显示该实体的窗口 → `WindowInvalidator::invalidate_view` 登记 `dirty_views` 并标脏、唤醒平台 → 平台帧回调 `on_request_frame` 检查 dirty → `Window::draw` + `present`。绘制期间的 notify 只登记不标脏，实体留待下一帧消费。
- **draw 的编排**：进入 arena 作用域 → 汇聚脏视图（沿 dispatch_tree 连坐祖先）→ `draw_roots` 驱动整树三阶段（prepaint 后、paint 前完成命中测试）→ 双缓冲交换 `rendered_frame`/`next_frame` → 重建倒排表 → 返回 arena 清除凭证。窗口在 `open_window` 时被保证至少画过一帧。
- **缓存复用**：只有 `.cached()` 包装的视图才有渲染缓存；命中条件是 bounds/content_mask/text_style 不变 + 不在 `dirty_views` + 不在 refresh。复用不是跳过，而是 `reuse_prepaint`/`reuse_paint` 重放录制区间——监听器、图元一个不少。`window.refresh()` / `cx.refresh_windows()` 置 `refreshing` 绕过全部缓存。
- **元素 arena**：每 App 一块 1MB 起、容量只增不减的 bump arena；帧末整体回收（valid 翻转 + 偏移归零），嵌套 draw 靠 scope 深度推迟清除；`ArenaBox` 的代际检查在开发期拦截 use-after-clear。
- **bounds_tree**：挂在 `Scene` 里的 R-tree 变体，按「最大相交 order + 1」为图元赋 z-order，不相交图元复用 order；`finish` 按 order 排序各图元批次，`replay` 重放操作让缓存区间与新画区间的层叠保持正确。
- **测试即帧源**：`flush_effects` 在 test/bench 构建下对脏窗口同步 draw——没有平台帧循环的环境里，效果冲刷就是帧调度。
- **profiler 埋点不改变语义**：提交 `1861e58f98` 在标脏（`window_id` + `record_frame_pending`）、帧回调与 present（`foreground_turn` 守卫、提交起止计时）、输入派发（`kind_name`）各处埋了 `cfg(feature = "profiler")` 钩子，写入挂在 `App` 上的前台工作日志——默认构建里它们不存在，主链路一个分支都没多。

## 7. 下一步学习建议

- **u4-l4（Scene 与绘制原语）**：本讲只把 Scene 当「图元收集器」，下一讲深入 `Quad`/`Path`/`Shadow`/sprite 的字段与批处理，理解 order 排好之后渲染器怎么画。
- **u4-l5（自定义 Element 实战）**：带着本讲的阶段时序（hitbox 在 prepaint 登记、命中测试在 paint 前完成）去写一个自定义元素，很多「为什么这段代码必须在 prepaint 里」的困惑会自然消解。
- **u6-l3（deferred 与 anchored）**：本讲看到了 `prepaint_deferred_draws`/`paint_deferred_draws` 的多轮循环与 `Frame` 的 `deferred_draws` 字段，届时回来重读 [window.rs:3271-3378](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/window.rs#L3271-L3378) 会有完整图景。
- **u7-l4（测试 GPUI 应用）**：本讲的 `flush_effects` 同步绘制、`simulate_next_frame`、`skip_drawing` 都是测试设施的伏笔。
- **u7-l6（前台工作日志与卡顿检测）**：本讲 4.2.3 那张埋点速览表里的每个钩子——`ForegroundJournal`、`foreground_turn`、`record_frame_pending`、present 计时、`kind_name`——都将在那一讲串成完整的可观测性链路。
- **延伸阅读**：`src/platform/test/window.rs` 中测试平台对 `on_request_frame` 的实现，对照 4.2 的生产路径，体会「平台帧源」这一抽象的两端。
