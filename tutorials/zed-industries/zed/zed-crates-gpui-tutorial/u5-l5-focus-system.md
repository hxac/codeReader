# 焦点系统：FocusHandle 与键盘导航

## 1. 本讲目标

u5-l4 结尾我们留下了一个关键结论：**按键动作的派发路径由焦点决定，而不是由鼠标命中测试决定**。本讲就来解剖「焦点」本身。它是什么？存在哪里？怎么移动？Tab 键导航是怎么实现的？读完本讲，你应该能够：

1. 解释 `FocusHandle` 与 `FocusMap` 的存储模型：为什么焦点句柄是「只有 ID 的引用计数句柄」，窗口里那个 `Option<FocusId>` 又是什么；
2. 区分接入焦点的两条路径——实体视图侧的 `Focusable` trait 与元素侧的 `track_focus` / `tab_index`——并说清各自适合什么场景；
3. 讲出 Tab 导航的完整链路：`actions!` 定义动作 → `focus_next`/`focus_prev` → `TabStopMap` 的路径排序查询，包括 `tab_group` 的嵌套语义；
4. 理解焦点包含关系（containment）：焦点是一条路径而非单点，`contains_focused` / `within_focused` / `on_focus_in` / `on_focus_out` 都建立在上一帧的 dispatch 树上；
5. 掌握 `focus` / `in_focus` / `focus_visible` 三种焦点样式的生效条件，特别是 `focus_visible` 如何用「输入模态」区分鼠标聚焦与键盘聚焦。

## 2. 前置知识

- **实体与所有权模型**（u2-l2）：`Entity<T>` 只是「EntityId + 类型标签」的句柄，数据存在 `App` 内的 `EntityMap`（slotmap）里。本讲的 `FocusHandle` 是同一设计哲学的微缩版，对照着读会非常轻松。
- **元素三阶段**（u4-l1）：元素每帧依次走过 `request_layout` → `prepaint` → `paint`。焦点句柄的登记发生在特定阶段：布局期创建、prepaint 期挂到派发树、paint 期写入 Tab 顺序表。
- **Interactivity 与元素状态**（u5-l2）：div 的全部交互配置汇聚于 `Interactivity` 结构体；跨帧状态以 `(GlobalElementId, TypeId)` 为键存于窗口元素状态表。本讲的「自动焦点句柄」就存在这里。
- **Action 体系**（u5-l3）与**键位派发链路**（u5-l4）：`actions!` 宏、`cx.bind_keys`、`on_action(cx.listener(...))` 的标准写法，以及 DispatchTree 是每帧重建的平行树。

先建立一个心智模型：**GPUI 的焦点 = 每窗口最多一个的「当前焦点 ID」+ App 内一张全局句柄表**。键盘派发（u5-l4）、Tab 导航（本讲）、IME 文本输入（u5-l1 的 `InputHandler`）全部构建在这套基础设施上。还有一个容易忽略的事实：**GPUI 没有内置的 Tab 键行为**——Tab 导航是应用自己定义动作、调用 `window.focus_next(cx)` 拼装出来的，本讲会用官方示例验证这一点。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/window.rs`（约 7500 行） | 窗口运行时 | `FocusId`/`FocusMap`/`FocusRef`/`FocusHandle`/`WeakFocusHandle`/`Focusable` 的定义（300-720 行一带）、`Window` 的 `focused`/`focus`/`blur`/`focus_next`/`focus_prev`、`set_focus_handle`、`with_tab_group`、帧末焦点路径 diff、`on_focus_in`/`on_focus_out`、输入模态跟踪 |
| `src/tab_stop.rs`（约 620 行） | Tab 顺序数据结构 | `TabStopMap`/`TabStopNode`/`TabStopPath`、`insert`/`begin_group`/`end_group`/`next`/`prev`/`replay`，以及文末的顺序测试 |
| `src/key_dispatch.rs` | 每帧重建的派发树 | `set_focus_id`、`focus_contains`、`focus_path`、`focusable_node_id` —— containment 查询的真正实现 |
| `src/elements/div.rs` | div 与交互机制 | `InteractiveElement` 的 `track_focus`/`tab_stop`/`tab_index`/`tab_group` 与 `focus`/`in_focus`/`focus_visible` 样式方法、焦点句柄在三个阶段的登记、`compute_style_internal` 的焦点样式合成、键盘激活（enter/space 触发 click） |
| `examples/tab_stop.rs` | Tab 导航官方示例 | 动作定义、句柄的 tab_index/tab_stop 配置、tab group 嵌套 |
| `examples/focus_visible.rs` | focus-visible 官方示例 | 三种焦点样式叠加对比（该文件未在 Cargo.toml 的 `[[example]]` 中显式注册，靠 Cargo 自动发现，运行方式见 4.4.4） |
| `examples/input.rs` | 文本输入官方示例 | 两个 `Focusable` 实现、`track_focus` 与 `handle_input` 的配合、光标只在聚焦时绘制 |

## 4. 核心概念与源码讲解

### 4.1 FocusHandle 与 FocusMap：焦点存在哪里

#### 4.1.1 概念说明

「焦点」要回答的问题是：**键盘事件应该派发给谁**。GPUI 的答案分两层存储：

- **窗口层**：每个 `Window` 持有一个 `focus: Option<FocusId>`——整个窗口同一时刻最多只有一个聚焦元素，没聚焦就是 `None`；
- **App 层**：所有窗口共享一张 `FocusMap`，里面存放每个焦点的元数据与引用计数。`FocusHandle` 只包含一个 `FocusId` 和指向这张表的 `Arc`，**不包含任何数据指针**——这和 `Entity<T>` 只是「EntityId + 类型标签」的设计一模一样（u2-l2）。

为什么句柄里还带着 `tab_index` 和 `tab_stop` 两个字段？因为 Tab 导航需要为每个焦点附加「排序键」与「是否可被 Tab 停留」的元数据（4.3 节），GPUI 干脆把它们挂在句柄上，并且链式设置时会**同时写进 FocusMap 里的元数据**，这样别的持有者也能读到最新值。

此外还有 `WeakFocusHandle`——不阻止焦点被释放的弱引用，用于 `FocusOutEvent` 等回调里避免延长生命周期，对应关系如同 `WeakEntity<T>` 之于 `Entity<T>`（u2-l2）。

#### 4.1.2 核心流程

一次焦点的完整生命周期：

```
创建    cx.focus_handle()
          → FocusHandle::new(&App.focus_handles)
          → SlotMap 插入 FocusRef { ref_count: 1, tab_index: 0, tab_stop: false }
          → 返回 { id, tab_index, tab_stop, handles: Arc<FocusMap> }

克隆    handle.clone() → for_id(id) → 原子地把 ref_count 从非 0 加一
释放    handle.drop()  → ref_count 减一（减到 0 后 for_id 返回 None，条目等待 slotmap 回收）

聚焦    handle.focus(window, cx)   → Window::focus：
          ① 若 focus_enabled == false 或已聚焦同一 id：直接返回
          ② window.focus = Some(id)
          ③ focus_generation 加一（使未完成的键盘激活失效，见 4.4）
          ④ 清空 pending 多键序列（u5-l4）
          ⑤ defer 一个 pending_input_changed 通知
          ⑥ self.refresh() —— 强制下一帧忽略缓存重绘（u4-l3）

失焦    window.blur()  → focus = None（同样 refresh）
禁用    window.disable_focus() → blur 且 focus_enabled = false，之后任何 focus 调用都是空操作
```

注意 `Window::focus` 的一个细节：**聚焦会调用 `self.refresh()`**。这不是偶然——焦点样式（`.focus(...)` 等）是否生效取决于重绘，而普通 `cx.notify()` 可能命中视图缓存（u3-l1），refresh 才保证焦点变化必然反映到画面。

#### 4.1.3 源码精读

先看存储层。`FocusMap` 是带读写锁的 slotmap，`FocusRef` 里除了引用计数还有 tab 元数据：

[window.rs:488-493](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L488-L493) —— 定义 `FocusMap`（`RwLock<SlotMap<FocusId, FocusRef>>`）与 `FocusRef`（原子引用计数 + `tab_index` + `tab_stop`）。`FocusId` 由上面的 `slotmaps::new_key_type!` 宏生成（324-327 行），与 `EntityId` 同源。

再看句柄本体：

[window.rs:526-533](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L526-L533) —— `FocusHandle` 结构体：`id`（FocusId）、`handles`（指向全局表的 Arc）、`tab_index`、`tab_stop`。文档注释一句话点题：「用于跟踪和操纵窗口中被聚焦元素的句柄」。

[window.rs:541-569](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L541-L569) —— `FocusHandle::new` 向 slotmap 插入初始 `FocusRef`；`for_id` 按 id 取回句柄，用 `atomic_incr_if_not_zero` 保证计数已归零的焦点不会被复活（这个辅助函数也解释了为什么 `Clone` 不会 panic）。

[window.rs:571-589](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L571-L589) —— `tab_index`/`tab_stop` 两个链式设置方法：既改自己身上的字段，也写回 `FocusMap` 里的 `FocusRef`，保证表内元数据与句柄副本一致。

[window.rs:599-635](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L599-L635) —— `focus()`（委托给 `Window::focus`）、`is_focused()`、`contains_focused()`、`within_focused()`、`contains()`（containment 查询，4.4 节展开）以及 `dispatch_action()`——最后这个方法说明句柄还能直接「向挂载它的元素派发动作」，实现原理是查派发树上该焦点对应的节点（u5-l4 的 `focusable_node_id`）。

[window.rs:638-661](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L638-L661) —— `Clone` 走 `for_id` 原子加计数；`PartialEq` 只比较 `id`；`Drop` 把 `ref_count` 减一。三个 trait impl 合起来就是一套手写的引用计数。

[window.rs:663-676](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L663-L676) —— `WeakFocusHandle`：只存 `id` 和 `Weak<FocusMap>`，`upgrade()` 失败即代表焦点（或整个 App）已释放。

窗口侧的状态与操作：

[window.rs:1191-1195](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L1191-L1195) —— `Window` 的焦点三字段：`focus: Option<FocusId>`（窗口级单值）、`focus_enabled`（`disable_focus` 的开关）、`focus_generation`（每次焦点移动加一的代数计数器，注释写明用途：让「待完成的键盘激活状态」在焦点变化时失效）。

[window.rs:2029-2033](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L2029-L2033) —— `Window::focused`：把窗口记录的 `FocusId` 经 `for_id` 还原成强句柄；计数已归零则返回 `None`。

[window.rs:2046-2087](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L2046-L2087) —— `focus`（含 4.1.2 流程里的六个步骤，注释解释了为什么用 `cx.defer` 推迟通知——避免重入实体更新）、`blur`、`disable_focus`。

[app.rs:2658-2663](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app.rs#L2658-L2663) —— `App::focus_handle()`：用户创建句柄的唯一入口（`cx.focus_handle()` 在任何上下文里最终都走到这里，因为 `Context<T>` 会 Deref 到 `App`）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「窗口只存一个 FocusId，句柄只是 ID」以及 `focus`/`blur`/`focused` 的行为。

**操作步骤**（源码阅读 + 小实验）：

1. 复制官方示例作为自己的实验场（避免改动源码）：

   ```bash
   cp examples/tab_stop.rs examples/my_focus_probe.rs
   cargo run -p gpui --example my_focus_probe
   ```

   Cargo 会自动发现 `examples/` 目录下新增的文件，无需注册 `[[example]]`（待本地验证）。

2. 在 `Example::new` 里，把 `window.focus(&focus_handle, cx);` 之后追加两行打印，并在 `items` 里挑第二个句柄聚焦：

   ```rust
   // 示例代码：追加到 Example::new 末尾
   window.focus(&items[1], cx); // items[1].tab_index(2) 是第一个 index=2 的句柄
   eprintln!("focused after new: {:?}", window.focused(cx));
   ```

3. 在 `on_tab` 回调里追加打印，观察每次按 Tab 后窗口记录的焦点：

   ```rust
   // 示例代码：on_tab 开头
   eprintln!("before focus_next: {:?}", window.focused(cx));
   ```

4. 临时给根节点加一个点击回调调用 `window.blur()`，观察画面上焦点高亮的消失。

**需要观察的现象**：

- `focused` 打印出的是 `Some(FocusHandle(FocusId(2)))` 这类 Debug 输出（`FocusHandle` 的 Debug 只打 id，见 535-539 行）；
- 启动时焦点在 `items[1]`（后一次 `focus` 覆盖了前一次 `focus_handle`），证明窗口里只有一个焦点槽位；
- 按一次 Tab 后 `focused` 从 `items[1]` 变成 tab 顺序中的下一个（结合 4.3 的顺序规则预测是哪一个）；
- `blur()` 之后 `focused` 打印 `None`。

**预期结果**：窗口级单焦点 + 句柄只是 ID 的模型得到验证。若打印行为与预测不符，优先检查该句柄是否真的出现在了画面上（不在画面上的句柄仍可被 `focus`，但不在 Tab 顺序里）。运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `FocusHandle::clone` 不用 `Arc::clone` 风格的直接复制，而要走一遍 `for_id` 的原子计数？

**答案**：句柄的核心字段 `FocusId` 指向 slotmap 里的 `FocusRef`，其 `ref_count` 是手写的原子引用计数。直接按位复制会绕过计数，导致 `Drop` 多减、焦点被提前回收。`for_id` 里的 `atomic_incr_if_not_zero` 还顺带处理了竞态：计数已归零的焦点无法被复活，`clone` 因此返回 `Option` 并在内部 `unwrap`——此时表内条目必然仍被当前句柄持有。

**练习 2**：`Window::focus` 为什么要调用 `self.refresh()` 而不是 `cx.notify()`？

**答案**：焦点变化必然影响画面（焦点样式、光标绘制等），而 `cx.notify()` 只是标脏视图，可能命中 `Entity::cached` 的渲染缓存（u3-l1：缓存命中时跳过 render）。`refresh()` 置 `refreshing` 标志，下一帧绕过所有缓存强制整树重绘（u4-l3），保证焦点状态与画面严格一致。

**练习 3**：两个不同窗口里的元素可以同时「被聚焦」吗？

**答案**：可以。`focus: Option<FocusId>` 是 `Window` 的字段，每个窗口各有一份；`FocusMap` 虽是 App 级共享的，但它只存句柄元数据，不存「谁聚焦」。这也是为什么 `is_focused` 必须传入 `&Window`——同一个句柄在 A 窗口聚焦、在 B 窗口没有。

### 4.2 Focusable 与 track_focus：焦点如何接到元素与视图

#### 4.2.1 概念说明

句柄造出来之后，还得**挂到元素树上**才有意义：键盘派发要沿「焦点节点 → 根」的路径走（u5-l4），Tab 导航要按元素出现顺序排序。GPUI 提供两条接入路径：

- **实体视图侧（推荐给「视图」用）**：在视图结构体里存一个 `focus_handle` 字段，`render()` 里用 `.track_focus(&self.focus_handle(cx))` 挂到 div 上；同时实现 `Focusable` trait，把句柄暴露出去，**使用者**就能一句 `cx.focus_view(&view, window)` 聚焦你的视图。`Focusable` 的文档注释写得很直白：「让视图的使用者可以轻松聚焦它」。
- **纯元素侧**：只写 `.tab_index(n)`，div 会在布局阶段从元素状态里取（或新建）一个句柄，随元素存活。适合按钮这类「不需要外部主动聚焦」的静态部件。

两条路径最终汇合在同一处：prepaint 阶段调用 `Window::set_focus_handle`，把 `FocusId` 写进**下一帧派发树的当前节点**。u5-l4 讲过 DispatchTree 每帧重建，焦点挂载也一样——每帧重新申报。

#### 4.2.2 核心流程

```
实体路径：
  struct TextInput { focus_handle: FocusHandle, ... }   // 实体跨帧持有
    ├─ impl Focusable → 暴露句柄（cx.focus_view 走这里）
    └─ render() → div().track_focus(&handle)
         ├─ request_layout：Interactivity.focusable = true，记录 tracked_focus_handle
         ├─ prepaint：window.set_focus_handle(handle)
         │    → next_frame.dispatch_tree.set_focus_id(id)
         │    → 节点记 focus_id，focusable_node_ids[id] = node_id（u5-l4 的索引）
         └─ paint：window.next_frame.tab_stops.insert(handle)（4.3 节）

纯元素路径：
  div().tab_index(3)
    └─ request_layout：元素状态里 get_or_insert_with(cx.focus_handle())
         → 句柄随 (GlobalElementId, TypeId) 键的元素状态跨帧存活（u5-l2）

IME 输入挂接：
  paint 阶段 window.handle_input(&focus_handle, input_handler)
    → 仅当 focus_handle.is_focused(window) 才注册平台输入处理器
```

#### 4.2.3 源码精读

先看契约：

[window.rs:698-709](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L698-L709) —— `Focusable` trait 只有一个方法 `focus_handle(&self, cx: &App) -> FocusHandle`；紧随其后的 blanket impl 让 `Entity<V: Focusable>` 自动实现 `Focusable`（读内部状态拿句柄），所以任何地方都能直接 `entity.focus_handle(cx)`。紧接其后的 `ManagedView: Focusable + EventEmitter<DismissEvent> + Render`（713-715 行）说明模态框、弹出菜单这类「由别的视图管理生命周期」的视图也以 `Focusable` 为前提。

[context.rs:286-289](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app/context.rs#L286-L289) —— `Context::focus_view`：`window.focus(&view.focus_handle(self), self)`，正是「使用者一句话聚焦你的视图」的落地。

再看元素侧的挂载方法（都在 `InteractiveElement` trait 上）：

[div.rs:749-790](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs#L749-L790) —— 四个方法一次看全：`track_focus`（写入外部句柄，置 `focusable = true`）、`tab_stop(bool)`（是否允许 Tab 停留，注释给了容器用法示例）、`tab_index(isize)`（设排序键并默认成为 tab stop）、`tab_group()`（把 div 声明为 tab 组，未设 index 时默认 0，4.3 节展开）。注意 `tab_index` 也会置 `focusable = true`——这就是「纯元素路径」的入口。

[div.rs:2141-2160](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs#L2141-L2160) —— 布局阶段的自动句柄逻辑，注释讲得很清楚：**如果 focusable 但没有显式 tracked handle，就从元素状态里取，取不到就 `cx.focus_handle()` 新建一个存进去**。元素状态随元素 id 跨帧存活（u5-l2），所以纯元素句柄在元素存在的帧之间是稳定的。`tab_stop`/`tab_index` 在这里应用到句柄上。

[div.rs:2216-2235](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs#L2216-L2235) —— prepaint 阶段：`window.set_focus_handle(focus_handle, cx)`；顺带在无障碍树激活时调用 `set_focusable` / `set_focus`（u6-l8 的伏笔）。

[window.rs:4764-4774](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L4764-L4774) —— `Window::set_focus_handle`：若该句柄恰好是当前焦点，同步记到 `next_frame.focus`（帧末 diff 靠它，见 4.4）；随后把 FocusId 挂到派发树当前节点。

[key_dispatch.rs:220-224](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L220-L224) —— `DispatchTree::set_focus_id`：节点记 `focus_id` 并维护 `focusable_node_ids: HashMap<FocusId, DispatchNodeId>` 反查索引。u5-l4 里「按焦点找派发路径」就是查这张表。

最后看 `input.rs` 示例里两条路径如何协作：

[input.rs:623-639](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/input.rs#L623-L639) —— `TextInput` 与 `InputExample` 两个 `Focusable` 实现：都是把实体字段 clone 出来，教科书式的样板。

[input.rs:585-590](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/input.rs#L585-L590) —— `TextInput::render` 里 `.key_context("TextInput").track_focus(&self.focus_handle(cx))`：焦点 + 键位上下文（u5-l4）一起挂，这是输入类视图的标准开头。

[input.rs:552-576](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/input.rs#L552-L576) —— 自定义 `TextElement::paint` 的两个焦点用法：`window.handle_input(&focus_handle, ...)` 注册 IME 处理器；`focus_handle.is_focused(window)` 决定光标 quad 是否绘制。

[window.rs:4827-4841](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L4827-L4841) —— `Window::handle_input`：**只有聚焦的句柄才能注册平台输入处理器**——这就是「点别处后打字没反应」的机制根源。

#### 4.2.4 代码实践

**实践目标**：跟踪一条完整链路，体会「实体持有句柄 → track_focus → 派发树节点 → IME/光标」每一环的分工。

**操作步骤**（源码阅读型）：

1. 打开 `examples/input.rs`，从 `InputExample::new`（约 720-740 行，两处 `focus_handle: cx.focus_handle()`）开始，画出下面这条链路的每一跳，并标注代码行号：

   ```
   cx.focus_handle() 创建
     → TextInput::render 的 .track_focus(...)
     → Window::set_focus_handle（div.rs prepaint）
     → DispatchTree::set_focus_id（key_dispatch.rs）
     → TextElement::paint 的 window.handle_input(...)（仅聚焦时注册）
     → is_focused 决定光标绘制
   ```

2. 阅读测试反推行为：`examples/testing.rs` 或 `examples/input.rs` 中聚焦输入框后敲键的路径，结合 u5-l4 的四阶段派发，确认「输入框未聚焦时 Backspace 动作不会派发给它」。

3. 小改造实验：复制 `input.rs` 为 `examples/my_input.rs`，给 `TextInput::render` 的外层 div 追加 `.in_focus(|s| s.outline_1().outline_color(gpui::red()))`（示例代码），运行后用鼠标点输入框，观察红色轮廓在聚焦时出现。

**需要观察的现象**：第 3 步中，红色 outline 在输入框聚焦时出现、失焦时消失；文字光标同样只在聚焦时可见（input.rs:572 的条件）。

**预期结果**：链路每一跳都能在源码中指认；`in_focus` 样式生效（其条件 `within_focused` 见 4.4）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：什么情况下应该把 `FocusHandle` 存在实体里，什么时候交给元素状态自动管理？

**答案**：需要在渲染之外引用这个焦点时（别的视图要聚焦它、`cx.on_focus` 订阅它、IME 要用它、程序化 `focus()` 它），存实体并实现 `Focusable`；如果只是页面上一个可 Tab 到的静态按钮，`.tab_index(n)` 让元素状态自动管理即可，省一个字段。代价对比：实体句柄跨帧绝对稳定；元素状态句柄随元素 id 存活，元素从树上消失太久后句柄会被回收。

**练习 2**：`cx.focus_view(&entity, window)` 和 `entity.focus_handle(cx).focus(window, cx)` 有区别吗？

**答案**：没有实质区别。前者（context.rs:286-289）就是后者的语法糖——先经 blanket impl 拿句柄，再调 `Window::focus`。`FocusHandle::focus`（window.rs:599-602）同样只是委托 `window.focus(self, cx)`。

**练习 3**：为什么 `window.handle_input` 只在句柄聚焦时才注册输入处理器，而不是注册时过滤？

**答案**：注册发生在 paint 阶段、每帧重申报（u5-l2 的「监听器只活一帧」原则）。若窗口里每个输入元素都注册，平台层就要自己判断路由；按焦点过滤后，`next_frame.input_handlers` 里最多只有当前聚焦元素的处理器，平台直接取用（window.rs:2910-2918 帧末取最后一个注册者），职责更简单。

### 4.3 tab_stop 与 TabStopMap：Tab 导航的完整链路

#### 4.3.1 概念说明

浏览器里按 Tab 会自动跳到下一个可聚焦元素，GPUI **没有**这个内置行为。`examples/tab_stop.rs` 展示了标准做法：

1. `actions!(example, [Tab, TabPrev])` 定义两个动作；
2. `cx.bind_keys([KeyBinding::new("tab", Tab, None), KeyBinding::new("shift-tab", TabPrev, None)])` 绑定物理按键；
3. `on_action` 里分别调用 `window.focus_next(cx)` / `window.focus_prev(cx)`。

也就是说，**Tab 导航 = 两个普通动作 + 窗口提供的顺序查询**。真正的数据结构是 `TabStopMap`：每帧 paint 阶段，所有被 track 的焦点句柄按出现顺序登记进来，组织成一棵用 `SumTree` 存储的有序集合，查询时用游标找「当前焦点的下一个 / 上一个 tab stop」。

它要解决的核心问题是**排序**。排序键是：

\[
\text{key} = (\text{path},\ \text{insertion\_index})
\]

其中 `path` 是从最外层 tab group 到自身的 `tab_index` 序列（字典序比较），`insertion_index` 是本帧登记顺序。同层元素按 `tab_index` 排；`tab_index` 相同按登记顺序排；组内元素的 path 前缀是组的 index，所以**调整一个组的 index 就能整体移动一组控件的 Tab 位置，而不必重编全应用所有编号**——这正是 HTML `tabindex` + 容器分组的思路。

#### 4.3.2 核心流程

一帧之内的登记（paint 阶段，div.rs）：

```
div().tab_group().tab_index(6)      // 组节点：with_tab_group(Some(6)) 压栈
  └─ div().tab_index(1)             // 子节点 path = [6, 1]
     paint 时：window.next_frame.tab_stops.insert(handle)
              path = current_path + handle.tab_index
组闭合：with_tab_group 出栈（end_group 弹掉 6）
```

查询（`focus_next` 为例）：

```
focus_next(cx)
  → rendered_frame.tab_stops.next(self.focus.as_ref())
     ├─ 无焦点：取 order 里第一个 tab_stop 节点
     ├─ 有焦点：找到该焦点的节点，游标 seek 后 next()
     │    └─ 循环跳过 tab_stop == false 的节点（组容器、纯 track_focus 元素）
     └─ 走到末尾：回到开头（循环导航）
  → window.focus(&handle, cx)   // 4.1 的聚焦流程
```

两个易忽略点：

- **所有被 track 的句柄都会被登记**（包括 `tab_stop(false)` 的），它们参与排序但不作为停留目标——这样才能「从非停留节点出发找下一个」；
- **缓存视图复用时**，被跳过 render 的子树不会重新走 paint，所以 `Window` 会把该子树此前的登记操作**重放**（`replay`）到新帧的 `TabStopMap`，保证顺序完整（window.rs:3486-3489，配合 u3-l1 的渲染缓存）。

#### 4.3.3 源码精读

[tab_stop.rs:11-16](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/tab_stop.rs#L11-L16) —— `TabStopMap` 的四个存储：`current_path`（当前 group 栈）、`insertion_history`（登记历史，供 replay）、`by_id`（FocusId → 节点）、`order`（SumTree 有序集合）。文件顶部的 `pub(crate)` 说明它完全是窗口内部机制，用户 API 只有 div 方法与 `focus_next`/`focus_prev`。

[tab_stop.rs:36-63](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/tab_stop.rs#L36-L63) —— `TabStopPath`（`SmallVec<[isize; 6]>`，前 5 层栈上分配）与 `TabStopNode`（path + 插入序 + 是否 tab stop）；手写的 `Ord` 正是排序键 \((\text{path},\ \text{insertion\_index})\) 的实现。

[tab_stop.rs:77-101](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/tab_stop.rs#L77-L101) —— `insert`（登记句柄：path 压入自身 tab_index，节点写入 by_id 与 order）、`begin_group`/`end_group`（group 栈的压栈与弹栈，同时记录进 insertion_history）。

[tab_stop.rs:111-146](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/tab_stop.rs#L111-L146) —— `next`：三分支处理（无焦点取首个、有焦点找后继、后继为空回绕到首个），`next_inner` 用 SumTree 游标 `seek` + 循环 `next()` 跳过 `tab_stop == false` 的节点。`prev`（148-183 行）完全对称。

[tab_stop.rs:185-193](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/tab_stop.rs#L185-L193) —— `replay`：按 insertion_history 重放全部操作，供缓存复用的子树恢复 Tab 登记。

[window.rs:3902-3913](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L3902-L3913) —— `Window::with_tab_group`：有 index 就 `begin_group` → 执行闭包 → `end_group`；没有就直接执行。div 的 paint 用它包裹子树绘制。

[div.rs:2416-2439](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs#L2416-L2439) —— 登记现场。2428-2439 的注释值得整段读：**容器自身的句柄必须登记在 `with_tab_group` 内部**，否则容器的 path 会比自己的孩子们浅，兄弟组并列时所有容器都会排到所有条目前面，`focus_next` 就会从容器跳到「全窗口第一个条目」而不是「本组第一个条目」。这是本模块最精妙的一处排序细节。

[window.rs:2089-2109](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L2089-L2109) —— `focus_next`/`focus_prev`：查 `rendered_frame.tab_stops`（注意是**上一帧**的数据——Tab 顺序只在 paint 时才更新），拿到句柄后走 4.1 的 `focus` 流程。

[tab_stop.rs:322-364](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/tab_stop.rs#L322-L364) —— 单元测试 `test_tab_handles` 是理解排序规则的最快途径：7 个句柄（含一个非 tab stop、两个重复 index），期望顺序是 `[0, 5, 1, 2, 6]`——index 0 的两个按登记序排前，index 1 的两个随后，非 stop 的被跳过，index 2 的最后一个殿后；末尾还断言了 next 从最后回绕到第一、prev 从第一回绕到最后。

示例侧的标准写法：

[examples/tab_stop.rs:18-45](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/tab_stop.rs#L18-L45) —— `Example::new` 里五个句柄的 index/stop 配置（第四个既无 index 也非 stop）；`on_tab`/`on_tab_prev` 各调一次 `window.focus_next/focus_prev`。

[examples/tab_stop.rs:83-109](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/tab_stop.rs#L83-L109) —— 列表项的渲染：`.track_focus(&item_handle)` + 用 `item_handle.tab_stop && item_handle.is_focused(window)` 手动加高亮（因为没有用 `.focus()` 样式方法）；非 stop 项降透明度提示。

[examples/tab_stop.rs:136-157](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/tab_stop.rs#L136-L157) —— 两个 tab group：容器 `.tab_index(6/7).tab_group().tab_stop(false)`，三个子按钮 index 1/2/3——于是整组在全局排在 index 6/7 的位置，组内顺序独立。

[examples/tab_stop.rs:183-202](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/tab_stop.rs#L183-L202) —— `bind_keys` 绑定 `tab`/`shift-tab`（`shift-tab` 是 u5-l1 讲过的 Keystroke 修饰键写法）。

#### 4.3.4 代码实践

**实践目标**：通过三个受控改造，验证排序键 \((\text{path},\ \text{insertion\_index})\) 的三条规则。

**操作步骤**：

1. 运行原始示例，按 Tab 走一圈记录顺序（预期：items[0](1) → items[1](2) → items[2](2) → items[4](2) → el1(4) → el2(5) → 组1 的 [6,1][6,2][6,3] → 组2 的 [7,1][7,2][7,3] → 回绕；items[3] 被 跳过）。**待本地验证**。

2. 改造 A（同 index 按登记序）：把 `items` 中第四个句柄改成 `.tab_index(2).tab_stop(true)`（示例代码）。预测它将插在 items[2] 与 items[4] 之间（登记序在它们之间），运行验证。

3. 改造 B（组整体移动）：把 `group-2` 容器的 `.tab_index(7)` 改成 `.tab_index(0)`（示例代码）。预测组 2 的三个按钮会整体跳到最前面（path 首元素 0 最小），组内顺序不变，运行验证。

4. 改造 C（关掉 stop）：给 `el1` 按钮追加 `.tab_stop(false)`（示例代码）。预测 Tab 会直接从 items[4] 跳到 el2，el1 只能靠点击聚焦。

**需要观察的现象**：每次改造后，按 Tab / Shift-Tab 的停留顺序变化且仅限预测范围；被 `tab_stop(false)` 标记的元素高亮状态只能通过点击（改造 C 里 el1 仍可点击）进入。

**预期结果**：三条规则（path 字典序、同 path 按登记序、非 stop 被跳过）逐一得到验证。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `focus_next` 查询的是 `rendered_frame.tab_stops` 而不是 `next_frame.tab_stops`？

**答案**：按键发生在两帧之间，此刻正在生效的元素树是上一帧渲染完成的 `rendered_frame`；`next_frame` 只在 `draw` 过程中逐步填充，帧末才交换（u4-l3）。Tab 顺序由上一帧的画面结构决定，查 rendered_frame 才与用户看到的焦点位置一致。

**练习 2**：一个 `tab_group` 容器自己 `tab_stop(false)`，这不会让它的孩子们也被跳过吗？

**答案**：不会。`tab_stop` 是**单节点属性**，登记时逐节点存进 `TabStopNode.tab_stop`（tab_stop.rs:85）；查询时只跳过当前经过的节点本身。容器不作为停留点，但它的孩子们照常参与排序与停留。`div.rs:758-767` 的文档注释描述的正是这个用法：聚焦容器后调 `focus_next` 即落入组内第一个条目。

**练习 3**：如果两个兄弟元素的 `tab_index` 相同且登记顺序相同（不可能，但假设编译期不可见的动态列表里出现），顺序由什么决定？

**答案**：由 `insertion_history` 的实际登记顺序决定，即 paint 阶段元素树的中序遍历顺序——视觉上靠前的元素先登记。`TabStopNode` 的 `Ord` 实现（tab_stop.rs:52-58）在 path 相同时比较 `node_insertion_index`，测试 `test_sibling_nested_groups_out_of_order`（600-614 行）还验证了 path 无序登记时最终顺序仍按排序键收敛。

### 4.4 焦点包含关系（containment）与 focus_visible：容器焦点与键盘指示

#### 4.4.1 概念说明

到此为止焦点看起来是个单点，但实际上 GPUI 在派发树里维护的是**一条路径**：从根到聚焦节点的所有「可聚焦祖先」。这就是 **containment（包含关系）**：容器可以问「焦点现在在我内部吗」，子元素可以问「我在被聚焦的容器里吗」。三个查询 API：

| API | 语义 | 典型用法 |
| --- | --- | --- |
| `handle.contains(other)` | other 在我的子树内（或就是我） | 判断两个焦点的层级关系 |
| `handle.contains_focused()` | 焦点在我内部**或**就是我 | 容器高亮「我这块区域正被使用」 |
| `handle.within_focused()` | 我在焦点元素内部**或**就是我 | 子元素随容器聚焦亮起（`.in_focus` 样式） |

焦点**移动**则是事件：帧末比较前后两帧的焦点路径，得出 `WindowFocusEvent`，`is_focus_in` / `is_focus_out` 按 FocusId 判断；路径从非空变空触发 focus-lost。订阅入口有元素窗口级的 `window.on_focus_in` / `on_focus_out`，也有实体上下文级的 `cx.on_focus` / `on_focus_in` / `on_focus_lost` / `on_focus_out`（返回 `Subscription`，u2-l3 的订阅生命周期规则适用）。

最后是 **focus_visible**：CSS 的 `:focus-visible` 语义——同一个焦点，鼠标点出来的不该画焦点圈（画面已有反馈），Tab 聚焦出来的要画（键盘用户需要知道位置）。GPUI 用「输入模态」实现：`Window` 记录最近一次输入来自键盘、鼠标还是触摸，`focus_visible` 样式只在「聚焦 && 模态为键盘」时叠加。

#### 4.4.2 核心流程

containment 查询的数据来源是上一帧的派发树：

```
handle.contains(other)
  → rendered_frame.dispatch_tree.focus_contains(my_id, other_id)
     → 分别找到两个焦点的节点
     → 从 other 节点沿 parent 指针上溯，途经 my 节点即包含
```

帧末的焦点路径 diff（`Window::draw` 尾部）：

```
draw 结束时：
  next_frame.finish(...)                  # 新帧完成
  previous_focus_path = 旧帧.focus_path()
  swap(rendered_frame, next_frame)        # 新帧生效
  current_focus_path  = 新帧.focus_path()
  若前后路径不同：
    ├─ 旧非空 → 新为空：触发 focus_lost_listeners（焦点整体丢失，
    │            典型场景：被聚焦元素从树上消失）
    └─ 构造 WindowFocusEvent { previous, current }
       → 逐个调用 focus_listeners
          （on_focus_in / on_focus_out 的包装在这里判断 is_focus_in/out）
```

输入模态的更新（`dispatch_event` 入口）：

```
KeyDown            → InputModality::Keyboard
MouseMove/MouseDown → InputModality::Mouse
Touch              → InputModality::Touch
其他                → 保持不变
模态变化 → self.refresh()（让 focus_visible 样式立即切换）
```

焦点样式合成顺序（`compute_style_internal`，接 u3-l3/u5-l2 的 base→focus→hover→active 大顺序）：

```
base_style
  → in_focus 样式      （within_focused：位于被聚焦容器内）
  → focus 样式         （is_focused：无论来源）
  → focus_visible 样式 （is_focused 且 last_input_was_keyboard）
  → （后续 hover/active……）
```

键盘激活（聚焦的按钮按 enter/space 触发 click）用 `focus_generation` 防 ABA：

```
keydown(enter/space，无修饰键)
  → 记录 pending = Some(当前 focus_generation)
keyup(enter/space)
  → 若 pending == 当前 generation（中途没移动焦点）且中间没按其他键
     → 合成 ClickEvent::Keyboard { button: Enter/Space }
  → 否则取消
```

这里若只记「哪个句柄按下的」，焦点移走又移回来（ABA）会误触发；记**代数**则只要焦点动过一次（generation 必然 +1）就作废。

#### 4.4.3 源码精读

containment 的查询入口与实现：

[window.rs:495-523](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L495-L523) —— `FocusId` 的四个查询方法。注意 `contains` 的实现：完全委托给 `window.rendered_frame.dispatch_tree.focus_contains`——**包含关系是上一帧几何结构的事实**，不是实时计算。

[key_dispatch.rs:346-360](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L346-L360) —— `focus_contains`：从 child 节点沿 `parent` 上溯找 parent 节点，遇不到就到根为止。

[key_dispatch.rs:574-586](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L574-L586) —— `focus_path(focus_id)`：从焦点节点上溯收集所有带 `focus_id` 的祖先并反转（根在前）。「焦点是路径」的本体实现。`Frame::focus_path`（[window.rs:1105-1109](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L1105-L1109)）包装它。

帧末 diff 与监听器派发：

[window.rs:2922-2964](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L2922-L2964) —— `draw` 尾部：`finish` → 取旧路径 → swap 帧 → 取新路径 → 空变空判定 focus-lost（并记录 `focus_lost_path` 供 `focus_lost_restore_target` 恢复最近可聚焦祖先，见 2038-2044 行）→ 构造 `WindowFocusEvent` 派发监听器。窗口激活状态（`window_active`）也参与：非激活窗口的路径按空处理，避免后台窗口报 focus 事件。

[window.rs:303-316](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L303-L316) —— `WindowFocusEvent` 与 `is_focus_in` / `is_focus_out`：判断依据是「该 FocusId 是否只在旧/新路径中出现」，天然覆盖祖先节点（焦点从容器 A 的子 X 移到子 Y，A 不触发 in/out，X 触发 out、Y 触发 in）。

[window.rs:4905-4952](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L4905-L4952) —— `on_focus_in` / `on_focus_out`：把用户回调包成 focus listener（`on_focus_out` 的 `FocusOutEvent.blurred` 用 `WeakFocusHandle`，避免强持已失焦元素）；`cx.defer(activate)` 保证注册当帧就生效。实体侧的对应入口 `Context::on_focus` 系列在 [context.rs:547-660](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app/context.rs#L547-L660)。

[window.rs:7480-7510](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L7480-L7510) —— 测试 `test_focus_moved_by_focus_listener_is_dispatched`：聚焦 A 的监听器里再把焦点转给 B（「dock 把焦点转给活动面板」的场景），断言 B 的监听器立刻收到通知——用一个真实测试验证了焦点监听器里移动焦点是安全的。

focus_visible 与输入模态：

[window.rs:2836-2840](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L2836-L2840) —— `last_input_was_keyboard`：文档注释直接点明用途（focus-visible 样式只在键盘导航时显示焦点指示）。

[window.rs:5012-5025](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5012-L5025) —— `dispatch_event` 入口的模态跟踪：KeyDown → Keyboard，MouseMove/MouseDown → Mouse，Touch → Touch，其余不变；**模态变化即 `refresh()`**——这一步必不可少，否则从鼠标切到键盘时画面上的 focus圈 不会出现。注释还提到键盘模态下会抑制 hover 高亮（鼠标悬停在某个条目上时用键盘导航不该亮它）。

[div.rs:1211-1241](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs#L1211-L1241) —— `focus` / `in_focus` / `focus_visible` 三个样式方法的定义，`focus_visible` 的文档注释明确对标 CSS `:focus-visible`。

[div.rs:3280-3299](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs#L3280-L3299) —— 合成现场：三段 `if` 分别对应 `within_focused` → `is_focused` → `is_focused && last_input_was_keyboard`，依次 `refine`。前提都是元素有 `tracked_focus_handle`——**焦点样式只对 track 过焦点的元素生效**。

[div.rs:2887-2949](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs#L2887-L2949) —— 键盘激活：聚焦元素上 enter/space 的 keydown 记录 `window.focus_generation`，keyup 时 generation 未变才合成 `ClickEvent::Keyboard`；中途按下其他键则作废（2943-2948 行）。这段只在 `is_focused` 时注册（2887 行的 `if is_focused`），即**键盘激活是聚焦元素的特权**。

[examples/focus_visible.rs:19-32](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/focus_visible.rs#L19-L32) —— 三个句柄只差样式用法：按钮 1 只 `.focus`，按钮 2 只 `.focus_visible`，按钮 3 两者都加。

[examples/focus_visible.rs:125-160](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/focus_visible.rs#L125-L160) —— 按钮 1（黄框，任何聚焦都显示）与按钮 2（绿框，仅键盘聚焦）的对比写法；按钮 3（162-192 行）两者叠加。

#### 4.4.4 代码实践

**实践目标**：直观区分 `.focus` / `.focus_visible` / `.in_focus` 三种样式的触发条件，并验证「焦点是路径」。

**操作步骤**：

1. 运行 focus-visible 示例。该文件未在 Cargo.toml 中显式注册 `[[example]]`，依赖 Cargo 对 `examples/` 目录的自动发现（u1-l4 讲过构建配置与文档要交叉验证，这里是「文件存在但未显式注册」的例子）：

   ```bash
   cargo run -p gpui --example focus_visible
   ```

   **待本地验证**（若自动发现未生效，可临时在 Cargo.toml 加一段 `[[example]]`，或复制该文件为已注册示例的同名变体——但不要提交这个改动）。

2. 用鼠标点击按钮 2：无绿框（模态是 Mouse）；按 Tab 走到按钮 2：绿框出现（模态是 Keyboard）。点击按钮 3：黄框；Tab 到按钮 3：黄 + 绿框。

3. 嵌套实验（示例代码）：复制示例为 `examples/my_containment.rs`，把三个按钮包进一个容器，容器聚焦、内层装饰 div track 一个**从不聚焦的子句柄**并加 `.in_focus`：

   ```rust
   // 示例代码：容器聚焦时，内部句柄 within_focused 为真 → in_focus 样式生效
   let container_handle = cx.focus_handle();
   let inner_handle = cx.focus_handle(); // 永不直接聚焦
   // render:
   div()
       .track_focus(&container_handle)
       .child(div().track_focus(&inner_handle).in_focus(|s| s.bg(gpui::yellow())))
       /* ...children... */
   ```

   聚焦 `container_handle`（或其任何后代）时内层变黄；聚焦外部元素时恢复。

**需要观察的现象**：点击与 Tab 对按钮 2 的视觉反馈不同；步骤 3 中内层黄色背景随「焦点是否位于容器子树内」切换，而 `inner_handle` 自身从未被聚焦。

**预期结果**：`focus` 无条件、`focus_visible` 看输入模态、`in_focus` 看包含关系，三者互不冲突且可叠加。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：焦点从容器 A 内的子元素 X 直接移到容器 B（A、B 互不包含），会触发哪些 in/out 事件？

**答案**：以 FocusId 为粒度看路径差集：X 与 A 触发 focus_out（在旧路径不在新路径），B 触发 focus_in。A 作为 X 的祖先天然包含在差集里——这正是 `is_focus_out` 按「路径成员资格」而不是「叶子是否相同」判断的价值：容器不用自己轮询，订阅 `on_focus_out(A)` 就能在焦点离开整块区域时收到通知（下拉菜单失焦关闭就是这么做的）。

**练习 2**：`contains_focused` 和 `within_focused` 换成互为对方的实现会怎样？用一个 UI 场景说明。

**答案**：会得到完全相反的高亮：侧边栏容器应该用 `contains_focused`（「我的某个后代被聚焦」→ 侧边栏高亮）；若误用 `within_focused`（「我在被聚焦元素的子树里」），只有当焦点跑到侧边栏的某个**祖先**上时才亮。同理 `.in_focus` 样式对应 `within_focused`，是给「位于聚焦容器内部的子元素」用的。

**练习 3**：为什么键盘激活（enter/space → click）要记录 `focus_generation` 而不是「按下时的焦点句柄」？

**答案**：div.rs:2893-2896 的注释直接回答：记句柄存在 ABA 问题——keydown 在句柄 A 上，焦点移到 B 又移回 A，keyup 到来时句柄仍「匹配」，会误触发一次 click。`focus_generation` 每次焦点移动都加一（window.rs:2053），只要焦点动过（哪怕移回来），generation 就不同，激活自动作废。

## 5. 综合实践

把本讲全部知识点串成一个**三输入卡表单**：三个卡片视图各自实现 `Focusable`，支持 Tab/Shift-Tab 循环切换，被聚焦的卡片显示 focus ring，且用 `focus_visible` 区分「鼠标点击聚焦」与「键盘 Tab 聚焦」。

**需求清单**：

1. 三张卡片是三个实体视图（`FormCard`），句柄存在实体里（4.2 实体路径）；
2. 卡片按 `tab_index` 1/2/3 参与 Tab 导航（4.3）；
3. Tab / Shift-Tab 用 `actions!` + `bind_keys` + `focus_next`/`focus_prev` 实现（4.3）；
4. 鼠标点卡片也能聚焦（`FocusHandle::focus`，4.1）；
5. 键盘聚焦显示蓝色 focus ring（`focus_visible`），任何方式聚焦显示浅色背景（`focus`，4.4）；
6. 状态栏实时显示当前聚焦的是哪张卡（`window.focused` + containment 判断）。

**参考实现**（示例代码，放入 `examples/my_form.rs` 后 `cargo run -p gpui --example my_form` 运行，**待本地验证**）：

```rust
#![cfg_attr(target_family = "wasm", no_main)]

use gpui::{
    App, Bounds, Context, FocusHandle, KeyBinding, SharedString, Window, WindowBounds,
    WindowOptions, actions, div, prelude::*, px, size,
};
use gpui_platform::application;

actions!(example, [Tab, TabPrev]);

struct FormCard {
    focus_handle: FocusHandle,
    title: &'static str,
}

impl FormCard {
    fn new(title: &'static str, tab_index: isize, cx: &mut Context<Self>) -> Self {
        Self {
            focus_handle: cx.focus_handle().tab_index(tab_index).tab_stop(true),
            title,
        }
    }
}

impl Render for FormCard {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        div()
            .id("card") // on_click 需要状态化元素（u5-l2）
            .track_focus(&self.focus_handle)
            .on_click(cx.listener(|this, _, window, cx| {
                this.focus_handle.focus(window, cx); // 鼠标点击聚焦
            }))
            .h_12()
            .w_full()
            .flex()
            .items_center()
            .px_4()
            .rounded_md()
            .border_1()
            .border_color(gpui::black())
            // 任何来源的聚焦：浅色背景
            .focus(|style| style.bg(gpui::rgb(0xeef2ff)))
            // 仅键盘聚焦：蓝色 focus ring
            .focus_visible(|style| style.border_2().border_color(gpui::blue()))
            .child(self.title)
    }
}

impl gpui::Focusable for FormCard {
    fn focus_handle(&self, _: &App) -> FocusHandle {
        self.focus_handle.clone()
    }
}

struct Form {
    cards: Vec<gpui::Entity<FormCard>>,
    root_handle: FocusHandle,
    message: SharedString,
}

impl Form {
    fn new(window: &mut Window, cx: &mut Context<Self>) -> Self {
        let cards = [
            cx.new(|cx| FormCard::new("Name", 1, cx)),
            cx.new(|cx| FormCard::new("Email", 2, cx)),
            cx.new(|cx| FormCard::new("Phone", 3, cx)),
        ]
        .into();

        let root_handle = cx.focus_handle();
        // 聚焦根容器（非 tab stop）：Tab 会从这里落到第一个 tab stop（4.3 的容器语义）
        window.focus(&root_handle, cx);

        Self {
            cards,
            root_handle,
            message: "Press Tab / Shift-Tab, or click a card.".into(),
        }
    }

    fn on_tab(&mut self, _: &Tab, window: &mut Window, cx: &mut Context<Self>) {
        window.focus_next(cx);
        self.report_focus(window, cx);
    }

    fn on_tab_prev(&mut self, _: &TabPrev, window: &mut Window, cx: &mut Context<Self>) {
        window.focus_prev(cx);
        self.report_focus(window, cx);
    }

    fn report_focus(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        let focused = window.focused(cx);
        self.message = match focused {
            Some(handle) => {
                let name = self
                    .cards
                    .iter()
                    .find(|card| card.focus_handle(cx) == handle)
                    .map(|card| card.read(cx).title)
                    .unwrap_or("root");
                format!("Focused: {}", name).into()
            }
            None => "Nothing focused".into(),
        };
        cx.notify();
    }
}

impl Render for Form {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        div()
            .id("app")
            .track_focus(&self.root_handle)
            .on_action(cx.listener(Self::on_tab))
            .on_action(cx.listener(Self::on_tab_prev))
            .on_mouse_down(gpui::MouseButton::Left, {
                // 点击卡片外部区域后，点击卡片才需要重新聚焦；
                // 这里顺便演示 cx.on_focus_out：焦点离开根容器时更新状态栏
                let root = self.root_handle.clone();
                cx.listener(move |this, _, window, cx| {
                    if !root.contains_focused(window, cx) {
                        this.message = "Focus left the form".into();
                        cx.notify();
                    }
                })
            })
            .size_full()
            .flex()
            .flex_col()
            .p_8()
            .gap_3()
            .bg(gpui::rgb(0xf8fafc))
            .child(self.message.clone())
            .children(self.cards.clone())
    }
}

fn run_example() {
    application().run(|cx: &mut App| {
        cx.bind_keys([
            KeyBinding::new("tab", Tab, None),
            KeyBinding::new("shift-tab", TabPrev, None),
        ]);

        let bounds = Bounds::centered(None, size(px(600.), px(400.0)), cx);
        cx.open_window(
            WindowOptions {
                window_bounds: Some(WindowBounds::Windowed(bounds)),
                ..Default::default()
            },
            |window, cx| cx.new(|cx| Form::new(window, cx)),
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

**验收要点**：

1. 启动后状态栏显示 `Focused: root`（根容器聚焦，卡片都无 ring）；
2. 按 Tab：依次停留 Name → Email → Phone → 回绕到 Name，每张卡出现蓝 ring + 浅色背景（键盘模态下两种样式同时生效）；
3. 用鼠标点击 Email：只有浅色背景、**没有**蓝 ring（模态是 Mouse）——这是本实践的核心验收点；
4. 点击窗口空白处后 Tab 一次：焦点回到 Name（`focus_next` 从根容器出发落到第一个 stop，对应 4.3 的容器语义）；
5. 综合覆盖：实体句柄 + `Focusable`（4.1/4.2）、Tab 导航（4.3）、`focus`/`focus_visible`/containment（4.4）。

以上运行现象**待本地验证**；若 Tab 无响应，按 u5-l4 的六步排查（焦点位置 → 上下文链 → 绑定注册）逐一检查。

## 6. 本讲小结

- **焦点 = 窗口级单值 + App 级句柄表**：`Window.focus: Option<FocusId>` 是唯一的「谁被聚焦」事实；`FocusHandle` 是只含 ID 的引用计数句柄，`FocusMap`（slotmap）存元数据（含 `tab_index`/`tab_stop`），设计上就是 `Entity`/`EntityMap` 的微缩复刻。
- **接入焦点的两条路径**：实体视图存字段 + `Focusable` trait（供外部 `focus_view`），或纯元素 `.tab_index(n)` 让元素状态自动持有句柄；两者最终都在 prepaint 阶段经 `set_focus_handle` 把 FocusId 挂到派发树节点，每帧重新申报。
- **Tab 导航不是内置行为**：应用定义 `Tab`/`TabPrev` 动作并绑定按键，`focus_next`/`focus_prev` 查询上一帧 paint 阶段登记的 `TabStopMap`——排序键是 `(tab group 路径, tab_index, 登记序)` 的字典序，`tab_group` 让一组控件可以整体参与排序而组内编号独立。
- **焦点是路径而非单点**：containment 查询（`contains`/`contains_focused`/`within_focused`）沿上一帧派发树的 parent 链上溯；帧末 diff 前后焦点路径派发 `WindowFocusEvent`，`on_focus_in`/`on_focus_out` 按路径成员资格判定，容器因此无需轮询。
- **三种焦点样式的条件各不相同**：`.focus`（`is_focused`，任何来源）、`.in_focus`（`within_focused`，位于聚焦容器内）、`.focus_visible`（`is_focused` 且输入模态为键盘）；模态由 `dispatch_event` 按事件类型更新，模态切换会触发 refresh。
- **聚焦还解锁键盘激活**：聚焦的 stateful div 上按 enter/space 会合成 `ClickEvent::Keyboard`，配对 keydown/keyup 用 `focus_generation`（每次焦点移动加一）防止「焦点移走又移回」的 ABA 误触发。

## 7. 下一步学习建议

- **下一讲 u5-l6（拖放与手势）**将继续交互机制的收尾部分：`on_drag`/`on_drop` 与命中盒的配合、系统文件拖入的 `FileDropEvent`。你会发现拖拽状态与焦点一样走「每帧申报」的路线。
- **回看 u5-l4**：现在再读 DispatchTree 的 `focusable_node_ids` 索引与 `dispatch_path`，焦点一侧的地基已经补全，四阶段派发的全链路应当完全通了。
- **u7-l4（测试）**会大量用到本讲内容：`simulate_keystrokes` 派发按键时走的就是真实焦点路径，`cx.on_focus` 系列的订阅如何在测试中断言，以及 window.rs:7480 那类焦点测试的写法。
- **延伸阅读**：`src/elements/list.rs` 开头（约 270 行的 `focus_handle` 方法）展示了虚拟化列表如何对外暴露「滚动容器」的焦点句柄——`list` 内部条目的焦点处理是本讲知识在大规模列表上的实战（u6-l2 详解）；`src/window/prompts.rs:182-186` 则是 `Focusable` 在原生对话框回退实现中的最小样板。
