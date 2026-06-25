# Tree：树形组件

## 1. 本讲目标

本讲精读 `gpui-component` 的 `Tree` 树形组件，读完你应当能够：

- 理解 `Tree` 如何把用户写的「嵌套树」**扁平化**为一维列表交给虚拟滚动渲染；
- 掌握 `TreeState` 状态引擎如何管理「选中、展开/折叠、键盘导航」并发出 `TreeEvent`；
- 弄清 `IndexPath` 这个坐标类型在库里的真实角色，以及它和 `Tree` 的扁平索引之间的关系；
- 学会处理节点的点击、选中、右键菜单，并写出一个最小可运行的文件目录树。

本讲是「高性能数据展示」单元的第三讲，承接 [u7-l1 虚拟化原理](./u7-l1-virtual-list-and-list.md) 的「虚拟化 + 状态实体 + 无状态外壳」范式，并继续使用该范式来理解 `Tree`。

## 2. 前置知识

在进入源码前，先用通俗语言对齐几个概念：

- **树形数据**：一个节点（node）可以挂多个子节点（children），子节点又能继续挂子节点，形成层级。文件目录、书的章节、评论楼层都是树。每个节点有一个「深度」（depth）：根节点深度为 0，它的直接子节点深度为 1，以此类推。
- **扁平化（flatten）**：把一棵嵌套的树「按深度优先、展开的才显示」的顺序摊平成一个一维数组。例如 `src/`（展开）下有 `lib.rs` 和 `ui/`（展开），扁平化后就是 `[src, lib.rs, ui, ...]`。`Tree` 内部存的就是这种扁平数组。
- **虚拟滚动**：详见 u7-l1。核心是「只渲染当前可见的那几十行」。`Tree` 直接复用 GPUI 内置的 `uniform_list`（等高虚拟列表）来渲染扁平后的条目。
- **状态实体 + 无状态外壳**：这是 gpui-component 的核心设计范式（见 u7-l1 的 `List`/`ListState`、u4-l4 的 `Input`/`InputState`）。`Tree` 同样是「有状态 `TreeState` 实体 + 无状态 `Tree` 外壳」。
- **`EventEmitter`**：状态实体可以实现该 trait 对外「广播」事件（见 u4-l3 的 `SliderState`、u5-l4 的 `PopupMenu`）。`TreeState` 会广播 `Expanded`/`Collapsed` 事件。
- **`key_context` + `actions`**：组件给自己打一个上下文标签（如 `"Tree"`），再在该上下文里绑定快捷键到特定 `Action`（见 u5-l4 菜单的 `key_context("PopupMenu")`）。`Tree` 用这种方式实现方向键导航。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/ui/src/tree.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs) | `Tree` 组件全部实现：数据模型 `TreeItem`/`TreeEntry`、状态引擎 `TreeState`、无状态外壳 `Tree`、键盘绑定与单元测试 |
| [crates/ui/src/index_path.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/index_path.rs) | `IndexPath { section, row, column }` 坐标类型，是 `List` 体系用的定位坐标 |
| [crates/ui/src/list/list_item.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list_item.rs) | `ListItem` —— `Tree` 渲染每一行的基本单元（带选中态、禁用态、点击回调） |
| [crates/ui/src/actions.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/actions.rs) | 定义 `SelectUp/SelectDown/SelectLeft/SelectRight/Confirm` 等选择类 Action |
| [crates/story/src/stories/tree_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/tree_story.rs) | 完整的真实示例：递归扫描磁盘目录、构建文件树、挂右键菜单与重命名 Action |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **数据模型**：`TreeItem` 嵌套树与 `TreeEntry` 扁平化；
2. **状态引擎**：`TreeState` 的选中、展开/折叠与 `TreeEvent`；
3. **键盘导航与渲染**：`key_context` + `actions`、`uniform_list` 渲染、右键菜单；
4. **定位坐标**：`IndexPath` 与 `Tree` 的扁平索引关系。

### 4.1 数据模型：TreeItem 嵌套树与 TreeEntry 扁平化

#### 4.1.1 概念说明

用户使用 `Tree` 时，描述的是一棵**自然的嵌套树**：每个 `TreeItem` 持有 `children: Vec<TreeItem>`，子节点又能继续嵌套。这种写法很直观，但**不适合直接喂给虚拟滚动**——虚拟列表需要的是一个一维数组（第 0 行、第 1 行……）。

因此 `Tree` 内部做了一个翻译：把嵌套树**按「深度优先 + 只展开已展开的节点」的顺序扁平化**成一维的 `Vec<TreeEntry>`，每个 `TreeEntry` 额外记录自己所在的 `depth`（用于前端缩进）。这样一来：

- 嵌套结构由用户用 `TreeItem::child(...)` 表达；
- 渲染时用扁平的 `Vec<TreeEntry>` 喂给 `uniform_list`；
- 缩进靠 `entry.depth()` 在渲染闭包里计算（如 `pl(px(16.) * entry.depth())`）。

注意「展开/折叠」本质上不是增删节点，而是**改变扁平化结果**：折叠一个文件夹，就把它的子节点从扁平数组里「摘掉」；展开，就重新「插回来」。

#### 4.1.2 核心流程

初始构建流程（`items` 调用时）：

```text
TreeState::items(items)
  └─ for 每个 root item: add_entry(item, depth=0)
       ├─ 把 item 包成 TreeEntry{ item, depth } push 进 entries
       └─ 若 item.is_expanded():
            └─ for 每个 child: add_entry(child, depth+1)   // 递归
```

展开/折叠后重建流程（`rebuild_entries`）：

```text
rebuild_entries()
  ├─ 从当前 entries 里筛出 depth==0 的 root items（保留它们最新的展开态）
  ├─ entries.clear()
  └─ for 每个 root item: add_entry(item, depth=0)   // 用最新展开态重新扁平化
```

关键点：`add_entry` 只会递归进入 `is_expanded()` 为真的节点，所以折叠的子树不会出现在扁平数组里，自然也不会被渲染。

#### 4.1.3 源码精读

**嵌套节点 `TreeItem`**：用户构造树的基本积木。注意它的展开/禁用态放在 `Rc<RefCell<TreeItemState>>` 里——这是为了能在不持有 `&mut` 的情况下、从扁平数组里的克隆副本反向改写「同一份」展开态（因为 `TreeItem` 是 `Clone` 的，扁平数组里存的是克隆副本，但内部 `state` 是 `Rc` 共享的）。

[crates/ui/src/tree.rs:62-69](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L62-L69) 定义了 `TreeItem` 的字段：`id`（唯一标识，常存全路径）、`label`（显示文本）、`children`、共享的 `state`。

[crates/ui/src/tree.rs:134-168](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L134-L168) 是构造与链式配置：`new(id, label)` 建节点，`child`/`children` 挂子节点，`expanded(true)` 设默认展开，`disabled(true)` 设禁用。这些都是 builder 风格——返回 `Self`，可链式调用。

**扁平条目 `TreeEntry`**：扁平化后的数组元素，比 `TreeItem` 多了一个 `depth`。

[crates/ui/src/tree.rs:71-112](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L71-L112) 给出 `TreeEntry` 及其只读访问器：`item()` 取源节点、`depth()` 取深度、`is_root()`（depth==0）、`is_folder()`（有子节点）、`is_expanded()`、`is_disabled()`。

**扁平化的核心 `add_entry`**：

[crates/ui/src/tree.rs:326-336](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L326-L336) —— 先把自己 push 进数组，再**仅当自己已展开**时递归处理子节点，且子节点 `depth + 1`。这是整个扁平化的引擎。

**重建 `rebuild_entries`**：

[crates/ui/src/tree.rs:360-371](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L360-L371) —— 展开态变化后调用。它先筛出根节点（`is_root()`），清空数组，再用 `add_entry` 重新铺平。因为根节点本身以及它们的 `state`（`Rc` 共享）带着最新展开态，所以重建后扁平数组会正确反映新的「展开快照」。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：亲眼确认「扁平化只包含已展开的子树，且深度随层级递增」。
2. **操作步骤**：打开 [crates/ui/src/tree.rs 的 `test_tree_entry` 测试](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L664-L733)，阅读它构造的树（`src` 展开 → `ui` 展开 → 3 个文件；另有 `Cargo.toml`、禁用的 `Cargo.lock`、`README.md`）。
3. **需要观察的现象**：测试用 `assert_entries` 把 `entries` 按 `"    ".repeat(depth) + label` 打印出来，并断言成如下缩进树：

   ```text
   src
       ui
           button.rs
           icon.rs
           mod.rs
       lib.rs
   Cargo.toml
   Cargo.lock
   README.md
   ```

   注意 `Cargo.lock` 虽被 `disabled(true)`，仍出现在扁平数组里（禁用只影响交互，不影响扁平化）。测试随后调用 `state.toggle_expand(1, cx)`（折叠 `ui`），并断言 `ui` 的 3 个子文件从数组里消失。
4. **预期结果**：折叠后断言成：

   ```text
   src
       ui
       lib.rs
   Cargo.toml
   Cargo.lock
   README.md
   ```

5. 运行命令为 `cargo test -p gpui-component tree::tests::test_tree_entry --features git`（实际能否运行依赖本机 GPUI 测试环境，**待本地验证**）。即使不运行，对照源码也能确认上述扁平化逻辑。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 `TreeItem` 有子节点但 `expanded(false)`（默认），它的子节点会进入扁平数组吗？
**答案**：不会。`add_entry` 只在 `item.is_expanded()` 为真时才递归处理子节点（见 [tree.rs:331-335](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L331-L335)）。

**练习 2**：为什么 `TreeItem::state` 用 `Rc<RefCell<TreeItemState>>` 而不是普通字段？
**答案**：因为 `TreeItem` 是 `Clone` 的，扁平数组 `entries` 里存的是克隆副本。用 `Rc` 共享 `state` 后，对克隆副本修改 `expanded` 会作用到「同一份」状态上，`rebuild_entries` 重新铺平时才能读到最新的展开态。

---

### 4.2 状态引擎：TreeState、选中、展开/折叠与 TreeEvent

#### 4.2.1 概念说明

`TreeState` 是 `Tree` 的「大脑」，是一个有状态的 `Entity`（GPUI 实体），实现 `Render`（自己能渲染）和 `EventEmitter<TreeEvent>`（能广播事件）。它持有：

- `entries: Vec<TreeEntry>` —— 扁平化后的可见节点；
- `selected_ix: Option<usize>` —— 当前选中节点在扁平数组里的下标；
- `right_clicked_ix: Option<usize>` —— 右键点击的下标（用于右键菜单高亮）；
- `scroll_handle` —— 虚拟滚动控制柄；
- `render_item` —— 用户传入的「渲染每一行」闭包；
- `context_menu_builder` —— 可选的右键菜单构建闭包。

它解决的问题是：把「选中、展开、键盘导航、滚动、右键」这些跨帧状态集中托管，让无状态外壳 `Tree` 只负责把这些配置一次性下发。

注意选中机制：`Tree` 默认是**单选**（一个 `Option<usize>`），选中下标是**扁平数组的下标**，不是层级路径。展开/折叠会引起扁平数组重排，所以同一个逻辑节点的下标会随展开状态变化——这也是后面 `IndexPath` 一节要讨论的「下标不稳定」问题。

#### 4.2.2 核心流程

**点击一个节点**（鼠标左键）：

```text
TreeState::render 里的 div.on_mouse_down(Left)
  └─ on_entry_click(ix)
       ├─ selected_ix = Some(ix)         // 选中
       ├─ toggle_expand(ix)              // 若是文件夹则切换展开
       │    ├─ 翻转 state.expanded
       │    ├─ cx.emit(Expanded 或 Collapsed(id))
       │    └─ rebuild_entries()
       └─ cx.notify()                    // 触发重渲染
```

**展开/折叠事件**：`toggle_expand` 会发出 `TreeEvent::Expanded(id)` 或 `TreeEvent::Collapsed(id)`，payload 是**节点的 `id`（SharedString）而非下标**——因为下标不稳定，而 `id` 是用户给的稳定标识。

**按 `id` 选中（自动展开祖先）**：`set_selected_item(Some(&item))` 时，若目标节点不在当前扁平数组里（说明祖先被折叠了），会调用 `expand_ancestors` 沿路径逐层展开，并发出对应的 `Expanded` 事件。

#### 4.2.3 源码精读

**`TreeState` 字段与事件 trait**：

[crates/ui/src/tree.rs:203-216](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L203-L216) 定义结构体，并 `impl EventEmitter<TreeEvent> for TreeState {}` 声明它会广播 `TreeEvent`。

**事件类型 `TreeEvent`**：

[crates/ui/src/tree.rs:114-121](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L114-L121) —— 两个变体 `Expanded(SharedString)` 与 `Collapsed(SharedString)`，携带节点 `id`。

**展开/折叠核心 `toggle_expand`**：

[crates/ui/src/tree.rs:338-358](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L338-L358) —— 先判断是文件夹（`is_folder()`）才处理；翻转 `expanded`；按新状态 `cx.emit` 对应事件；最后 `rebuild_entries()` 重铺扁平数组。注意 payload 用的是 `entry.item.id.clone()`，而非下标 `ix`。

**点击处理 `on_entry_click`**：

[crates/ui/src/tree.rs:439-443](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L439-L443) —— 同时做两件事：设选中、切换展开（非文件夹时 `toggle_expand` 内部会直接返回，见 [tree.rs:342-344](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L342-L344)）。

**按 `id` 选中并展开祖先 `set_selected_item` / `expand_ancestors`**：

[crates/ui/src/tree.rs:266-285](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L266-L285) 与 [crates/ui/src/tree.rs:302-324](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L302-L324) —— 目标不在扁平数组里时，先用 `find_ancestors` 找出从根到目标的祖先链，逐层（`.rev()` 从最外层开始）展开并发出 `Expanded` 事件，最后 `rebuild_entries()`。`test_set_selected_item_emits_expanded_events_for_hidden_ancestors`（[tree.rs:815-839](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L815-L839)）验证了这一点。

#### 4.2.4 代码实践（源码阅读型 + 可选运行）

1. **实践目标**：确认 `toggle_expand` 发出的事件携带的是 `id` 而非下标。
2. **操作步骤**：阅读 [crates/ui/src/tree.rs 的 `test_event_carries_item_id`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L795-L813)。它构造 `src → ui → button.rs`，对下标 1（即 `ui`）调用 `toggle_expand`，然后断言收到的事件是 `TreeEvent::Expanded("src/ui")`。
3. **需要观察的现象**：尽管操作的是下标 `1`，事件 payload 却是 `"src/ui"`（该节点的 `id`）。
4. **预期结果**：断言通过，证明事件以稳定 `id` 为载体。
5. 想在真实应用里订阅事件，可仿照测试里的 `TestCollector`：用 `cx.subscribe(&state, move |_, _, ev: &TreeEvent, _| { ... })` 收集事件（[tree.rs:625-637](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L625-L637)）。**待本地验证**运行行为。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TreeEvent` 携带 `SharedString`（id）而不是 `usize`（下标）？
**答案**：因为展开/折叠会引起扁平数组重排，下标会变化、不稳定；而 `id` 是用户在 `TreeItem::new(id, label)` 时给的稳定标识，适合作为事件 payload。

**练习 2**：调用 `set_items` 重设整棵树时，会发出 `Expanded`/`Collapsed` 事件吗？
**答案**：不会。`set_items` 直接清空并重新 `add_entry`，不发事件（见 [tree.rs:243-252](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L243-L252)）。`test_set_items_does_not_emit_expansion_events`（[tree.rs:769-793](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L769-L793)）专门验证这一点——只有用户交互产生的展开/折叠才发事件。

---

### 4.3 键盘导航与渲染：key_context + actions、uniform_list、右键菜单

#### 4.3.1 概念说明

`Tree` 提供完整的键盘导航：上下移动选中、左右折叠/展开、回车切换。这套能力不是手写按键监听，而是用 GPUI 的 **`key_context` + `actions` 体系**：

- 组件给自己打一个上下文标签 `"Tree"`（`key_context(CONTEXT)`）；
- 在 `init` 里把 `up/down/left/right` 物理键绑定到 `SelectUp/SelectDown/SelectLeft/SelectRight` Action，且**仅在该上下文生效**；
- 在 `Tree` 外壳上用 `.on_action(...)` 把这些 Action 路由到 `TreeState` 的处理方法。

只有当焦点落在 `Tree` 上（即当前 `key_context` 包含 `"Tree"`）时，方向键才会被翻译成对应 Action。这与 u5-l4 菜单的 `key_context("PopupMenu")` 是同一套机制。

渲染层面，`TreeState::render` 用 GPUI 内置的 `uniform_list`（等高虚拟列表）渲染扁平 `entries`，每一行调用用户的 `render_item` 闭包生成一个 `ListItem`，外层再包一个 `div` 挂鼠标事件与右键菜单。

#### 4.3.2 核心流程

键盘导航路由：

```text
用户按 ↓  ──(key_context="Tree")──>  SelectDown Action
   └─ Tree::render 里 .on_action(on_action_down)
        └─ TreeState::on_action_down
             ├─ selected_ix 下移（到底回绕到 0）
             ├─ scroll_handle.scroll_to_item(selected_ix, Top)
             └─ cx.notify()
```

各 Action 的语义：

| 键 | Action | 行为 |
| --- | --- | --- |
| `↑` | `SelectUp` | 选中上移（到顶回绕到末尾） |
| `↓` | `SelectDown` | 选中下移（到底回绕到 0） |
| `←` | `SelectLeft` | 当前是已展开的文件夹 → 折叠它 |
| `→` | `SelectRight` | 当前是未展开的文件夹 → 展开它 |
| `Enter` | `Confirm` | 当前是文件夹 → 切换展开/折叠 |

注意左右键的语义是「有条件折叠/展开」：`←` 只在「文件夹且已展开」时折叠，`→` 只在「文件夹且未展开」时展开，否则不动（不做「跳到父节点」）。

渲染流程：

```text
Tree::render (无状态外壳)
  ├─ 把 render_item / context_menu_builder 写入 state
  ├─ div().key_context("Tree").track_focus(focus_handle)
  │     .on_action(on_action_up/down/left/right/confirm)
  │     .child(self.state)            // 把 state 当子视图渲染
  │     .vertical_scrollbar(scroll_handle)
  └─ TreeState::render
       └─ div().context_menu(...).child(
            uniform_list("entries", entries.len(), |visible_range| {
                for ix in visible_range {
                    let item = render_item(ix, entry, selected, ...);
                    div().id(ix)
                        .child(item.disabled(..).selected(selected).secondary_selected(right_clicked))
                        .on_mouse_down(Left, on_entry_click(ix))
                        .on_mouse_down(Right, right_clicked_ix = Some(ix))
                }
            }).track_scroll(scroll_handle)
        )
```

#### 4.3.3 源码精读

**键盘绑定 `init`**：

[crates/ui/src/tree.rs:18-26](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L18-L26) —— 在 `"Tree"` 上下文里把四个方向键绑定到对应 Action（这些 Action 定义在 [actions.rs:11](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/actions.rs#L11)，`Confirm` 定义在 [actions.rs:6](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/actions.rs#L6)）。这个 `init` 由库的 `gpui_component::init(cx)` 统一调用（详见 u1-l4）。

**Action 处理方法**：

[crates/ui/src/tree.rs:388-437](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L388-L437) 给出 `on_action_left/right/up/down`。注意 `on_action_up`/`on_action_down` 在移动选中后还调用 `scroll_handle.scroll_to_item` 让目标行滚入视口（[tree.rs:420-421](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L420-L421) 与 [tree.rs:434-435](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L434-L435)）。

[crates/ui/src/tree.rs:377-386](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L377-L386) 是 `on_action_confirm`（回车切换）。

**`TreeState::render` 的虚拟列表与事件挂载**：

[crates/ui/src/tree.rs:446-530](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L446-L530)。其中 [tree.rs:483-529](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L483-L529) 是关键：

- 用 `uniform_list("entries", self.entries.len(), cx.processor(...))` 只渲染可见区间的行（虚拟化）；
- 每行用 `(render_item)(ix, entry, selected, window, cx)` 生成 `ListItem`，再调 `.disabled(..).selected(selected).secondary_selected(right_clicked)` 设状态；
- 外层 `div().id(ix)` 在**节点未禁用**时挂 `on_mouse_down(Left)` → `on_entry_click(ix)` 与 `on_mouse_down(Right)` → 记录 `right_clicked_ix`（[tree.rs:500-516](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L500-L516)）；
- 整个容器挂 `.context_menu(...)`，当存在 `context_menu_builder` 且有 `right_clicked_ix` 时，构建并返回右键菜单（[tree.rs:455-482](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L455-L482)）。右键菜单的机制本身见 u5-l4。

**无状态外壳 `Tree::render`**：

[crates/ui/src/tree.rs:583-607](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L583-L607) —— 每帧先把 `render_item` 与 `context_menu_builder` 写入 state（[tree.rs:588-591](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L588-L591)），再 `.key_context(CONTEXT).track_focus(...).on_action(... ×5).child(self.state).vertical_scrollbar(...)`。`.child(self.state)` 把 `TreeState` 这个 `Entity` 当子视图渲染（GPUI 会调它的 `Render::render`）。

#### 4.3.4 代码实践（运行型，可选）

1. **实践目标**：体验键盘导航与右键菜单。
2. **操作步骤**：运行 Story Gallery（`cargo run`），在左侧找到 **Tree** 故事并打开；用鼠标点击树中任意文件夹节点，观察它展开/折叠；点击一个节点后，按 `↑/↓` 移动选中、按 `←/→` 折叠/展开、按 `Enter` 切换；在节点上**右键**，观察弹出的「Open / Rename / Delete」菜单。
3. **需要观察的现象**：方向键移动会自动把目标行滚入视口；右键菜单只对文件显示「Open」（对文件夹不显示），其构建逻辑见 [tree_story.rs:265-271](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/tree_story.rs#L265-L271)。
4. **预期结果**：键盘与右键交互均符合上表语义。
5. 受限于本机图形环境，运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么方向键只在 `Tree` 获得焦点时才生效？
**答案**：因为键绑定带 `Some(CONTEXT)`（[tree.rs:20-25](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L20-L25)），而 `Tree::render` 给容器打了 `.key_context(CONTEXT)`（[tree.rs:595](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L595)）。只有焦点落在该容器、上下文匹配时，物理键才会被翻译成对应 Action。

**练习 2**：`←` 键对一个「未展开的文件夹」会做什么？
**答案**：什么也不做。`on_action_left` 只在「文件夹且已展开」时调用 `toggle_expand` 折叠它（[tree.rs:388-397](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L388-L397)）。它不会「跳到父节点」。

---

### 4.4 定位坐标：IndexPath 与 Tree 的扁平索引关系

#### 4.4.1 概念说明

本讲主题提到「`IndexPath` 定位机制」，需要澄清一个容易误解的点：**`Tree` 组件内部并不直接使用 `IndexPath`**，而是用扁平数组的下标 `usize`（`selected_ix: Option<usize>`）。`IndexPath` 是库中 `List`/`ListDelegate` 体系（见 u7-l1）使用的坐标类型，这里讲它是因为：

1. 它是 gpui-component 里**通用的「定位坐标」概念**，理解它能帮你把 `Tree`、`List`、`Select` 等组件的定位方式统一起来；
2. `Tree` 的扁平下标与 `IndexPath` 存在清晰的**等价关系**，明白这层关系，你就能在「扁平下标」与「层级路径」之间换算。

`IndexPath` 是一个三元组 `{ section, row, column }`：

- `section`：分段下标（`List` 支持把数据分组，每组是一个 section）；
- `row`：段内行号；
- `column`：列号（用于表格类场景）。

`Tree` 没有分段、没有列，所以它的每一个扁平条目等价于 `IndexPath { section: 0, row: 扁平下标, column: 0 }`。

#### 4.4.2 核心流程

两种定位方式的对照：

| 方式 | 表示 | 是否稳定 | 用在哪 |
| --- | --- | --- | --- |
| 扁平下标 | `usize`（如 `selected_ix`） | **不稳定**（展开/折叠会重排） | `Tree` 内部选中、点击、键盘导航 |
| `IndexPath` | `{ section, row, column }` | 段内行号同样随展开变化 | `List`/`ListDelegate` 体系 |
| 层级路径 | `[子下标, 子下标, ...]`（从根到节点） | **稳定**（与展开状态无关） | 概念上定位「树里的某个节点」 |

换算关系（概念，非库内置）：

\[ \text{扁平下标 } n \;\longleftrightarrow\; \text{IndexPath}\{ \text{section}:0,\ \text{row}:n,\ \text{column}:0 \} \]

而要从扁平下标还原「层级路径」，可以利用 `TreeEntry::depth()`：从当前条目沿扁平数组向前回溯，每遇到 `depth` 减 1 的条目，就是上一层祖先。设某节点深度为 \(d\)，则其层级路径长度为 \(d+1\)：

\[ \text{path}(n) = [\,\text{root\_idx},\ \ldots,\ \text{parent\_idx},\ n\,] \]

#### 4.4.3 源码精读

**`IndexPath` 定义**：

[crates/ui/src/index_path.rs:8-16](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/index_path.rs#L8-L16) —— 三个 `usize` 字段 `section / row / column`，派生了 `Debug/Clone/Copy/Default/PartialEq/Eq`，是值类型、可拷贝。

**构造与链式 setter**：

[crates/ui/src/index_path.rs:34-63](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/index_path.rs#L34-L63) —— `IndexPath::new(row)` 默认 `section=0, column=0`；`.section(s)/.row(r)/.column(c)` 是 builder 风格 setter。

**转 `ElementId`**：

[crates/ui/src/index_path.rs:18-22](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/index_path.rs#L18-L22) —— `impl From<IndexPath> for ElementId`，把它格式化成 `"index-path(section,row,column)"` 字符串。这样 `IndexPath` 可直接当作 GPUI 元素的稳定 `id`（用于 `div().id(...)`、`ListItem::new(...)` 等），保证同一坐标每次渲染得到同一 `id`。`test_into_element_id`（[index_path.rs:75-80](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/index_path.rs#L75-L80)）验证了该格式。

**`eq_row` 忽略列**：

[crates/ui/src/index_path.rs:65-68](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/index_path.rs#L65-L68) —— 只比较 `section` 与 `row`、忽略 `column`，用于「同一行不同列」也算同一位置的判定。

#### 4.4.4 代码实践（源码阅读 + 编写型）

1. **实践目标**：把 `Tree` 的扁平下标换算成「层级路径」，加深对两种定位方式的理解。
2. **操作步骤**：基于 `TreeState::entries`（每个 `TreeEntry` 有 `depth()`），写一个工具函数：给定扁平下标 `n`，沿数组向前回溯收集路径。**示例代码**（非项目原有代码）：

   ```rust
   // 示例代码：把扁平下标转成从根到该节点的下标路径
   fn flat_index_to_path(depths: &[usize], n: usize) -> Vec<usize> {
       // 从 n 向前找深度依次递减的祖先
       let mut path = vec![n];
       let mut target_depth = depths[n];
       let mut i = n;
       while target_depth > 0 && i > 0 {
           i -= 1;
           if depths[i] + 1 == target_depth {
               path.push(i);
               target_depth = depths[i];
           }
       }
       path.reverse(); // 从根到节点
       path
   }
   ```

3. **需要观察的现象**：对照 4.1.4 的扁平结果（`[src(0), ui(1), button.rs(2), icon.rs(3), mod.rs(4), lib.rs(5), ...]`，深度为 `[0,1,2,2,2,1,...]`），对 `n=2`（`button.rs`）调用该函数。
4. **预期结果**：返回 `[0, 1, 2]`，即「根 `src` → `ui` → `button.rs`」的下标路径，长度 = depth(2)+1 = 3。
5. 注意此为**概念性示例代码**，库内并未提供该函数；如需在程序里稳定定位节点，推荐用 `TreeItem::id`（稳定字符串），而非下标或路径。**待本地验证**实际取值。

#### 4.4.5 小练习与答案

**练习 1**：`Tree` 内部用 `selected_ix: Option<usize>` 而不用 `IndexPath`，主要原因是什么？
**答案**：`Tree` 把树扁平成单一一维数组，没有分段（section 恒为 0）、没有列（column 恒为 0），只需一个 `usize` 行号即可定位；用 `IndexPath` 会带上两个永远为 0 的冗余字段，所以直接用扁平下标更简洁。

**练习 2**：`IndexPath` 实现了 `From<IndexPath> for ElementId`，这有什么用？
**答案**：让 `IndexPath` 可直接作为 GPUI 元素的稳定 `id`（如 `ListItem::new(index_path)`）。同一坐标每次渲染都生成相同的 `"index-path(s,r,c)"` 字符串，保证 GPUI 在 diff 时能正确复用元素、保留动画与状态（见 [index_path.rs:18-22](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/index_path.rs#L18-L22)）。

---

### 4.5 渲染外壳与渲染闭包：Tree 无状态组件与 render_item 模式

（此模块把「无状态外壳」与「用户渲染闭包」这两个易踩坑的点单独讲清，作为前四个模块的收口。）

#### 4.5.1 概念说明

`Tree` 是一个**无状态 `RenderOnce` 组件**（派生 `IntoElement`），它只持有 `state: Entity<TreeState>`、用户的 `render_item` 闭包、可选的 `context_menu_builder`，以及样式。它的职责非常薄：

- 每帧把 `render_item` 与 `context_menu_builder` **下发**给 `TreeState`；
- 给容器打 `key_context`、挂焦点、注册 5 个 Action、挂滚动条；
- 把 `TreeState` 当子视图渲染。

真正「画树」的是 `TreeState::render`（见 4.3）。这种「外壳薄、状态实体厚」的分工，与 u7-l1 的 `List`/`ListState` 完全一致。

用户侧最关键的是 **`render_item` 闭包**。它的签名是：

```rust
Fn(usize, &TreeEntry, bool, &mut Window, &mut App) -> ListItem + 'static
//     ix    entry     selected                返回这一行的渲染
```

闭包收到「行号、条目、是否选中」三个信息，返回一个 `ListItem`。常见用法是：根据 `entry.is_folder()` / `entry.is_expanded()` 选图标，用 `entry.depth()` 算缩进，把 `entry.item().label` 作为文本。

#### 4.5.2 核心流程

`tree()` 构造与渲染：

```text
tree(&state, |ix, entry, selected, window, cx| { ... ListItem ... })
  └─ Tree::new(state, render_item)        // 存闭包到 Rc<dyn Fn>
       └─ Tree::render (每帧)
            ├─ state.render_item = self.render_item            // 下发
            ├─ state.context_menu_builder = self.context_menu_builder
            └─ div().key_context("Tree").track_focus().on_action(×5)
                  .child(self.state).vertical_scrollbar()
```

#### 4.5.3 源码精读

**构造函数 `tree()` 与 `Tree::new`**：

[crates/ui/src/tree.rs:50-55](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L50-L55) 是便捷构造函数；[crates/ui/src/tree.rs:546-559](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L546-L559) 是 `Tree::new`，把闭包包成 `Rc<dyn Fn ...>` 存储，并生成一个基于 `entity_id` 的稳定 `id`（`format!("tree-{}", state.entity_id())`，[tree.rs:551](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L551)）。

**`ListItem::new` 接受任意可转 `ElementId` 的参数**：

[crates/ui/src/list/list_item.rs:43-61](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list_item.rs#L43-L61) —— `ListItem::new(id: impl Into<ElementId>)`。`render_item` 里常写 `ListItem::new(ix)`（`usize` 可转 `ElementId`），它就是这一行的稳定 id。

**真实示例 `tree_story.rs` 的渲染闭包**：

[crates/story/src/stories/tree_story.rs:229-264](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/tree_story.rs#L229-L264) —— 这是最佳范本，值得逐行读：

- [tree_story.rs:234-240](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/tree_story.rs#L234-L240)：按「非文件夹 → `File`；文件夹展开 → `FolderOpen`；文件夹折叠 → `Folder`」选图标；
- [tree_story.rs:246](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/tree_story.rs#L246)：`pl(px(16.) * entry.depth() + px(12.))` 按深度缩进；
- [tree_story.rs:253-261](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/tree_story.rs#L253-L261)：用 `on_click` 打印点击项的 `label` 与 `id`。

> **关键模式（容易踩坑）**：注意整个闭包体被包在 `view.update(cx, |_, cx| { ... ListItem ... })` 里（[tree_story.rs:232-263](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/tree_story.rs#L232-L263)）。原因是 `render_item` 闭包拿到的是 `&mut App`，而 `cx.listener(...)`（用于把 `on_click` 回调到外层 `TreeStory` 视图）只能在 `&mut Context<TreeStory>` 上调用。`view.update(cx, ...)` 把 `&mut App` 升级为 `Context<Self>`，且其返回值就是内层闭包的返回值（即构造好的 `ListItem`），从而既能在闭包里用 `cx.listener`、又能正常返回 `ListItem`。

#### 4.5.4 代码实践（编写型）

1. **实践目标**：手写一个最小的 `render_item` 闭包，理解三个参数与 `view.update` 模式。
2. **操作步骤**：在一个自定义视图里（参考 u1-l4 的最小应用骨架）放一个 `Tree`，写一个只显示标签、按深度缩进、点击打印 `id` 的闭包。**示例代码**（基于 tree_story 精简，非完整可编译文件）：

   ```rust
   // 示例代码：最小 render_item 闭包
   let view = cx.entity();
   tree(&self.tree_state, move |ix, entry, _selected, _window, cx| {
       view.update(cx, |_, cx| {
           let item = entry.item();
           ListItem::new(ix)
               .pl(px(16.) * entry.depth() + px(12.))
               .child(item.label.clone())
               .on_click(cx.listener({
                   let id = item.id.clone();
                   move |_, _, _, _| println!("Clicked: {}", id)
               }))
       })
   })
   ```

3. **需要观察的现象**：节点按深度缩进；点击任意节点在控制台打印其 `id`。
4. **预期结果**：缩进与点击打印均正常。注意：若不包 `view.update`，`cx.listener` 会因 `cx` 是 `&mut App` 而无法编译。
5. 完整可运行版本见 4.1 节引用的 `test_tree_entry` 与第 5 节综合实践。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`Tree` 为什么是 `RenderOnce`（无状态）而不是 `Render`（有状态视图）？
**答案**：因为跨帧状态（选中、展开、滚动）全部由 `TreeState` 这个独立实体持有；`Tree` 本身每帧只是把 `render_item`/`context_menu_builder` 下发给 state 并组装容器，不需要自己持状态，所以做成无状态 `RenderOnce` 更轻（见 [tree.rs:583-607](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tree.rs#L583-L607)）。这与库的统一范式一致（u7-l1）。

**练习 2**：`ListItem::new(ix)` 里的 `ix`（`usize`）起了什么作用？
**答案**：作为这一行的稳定 `ElementId`（`usize → ElementId`）。GPUI 在每帧 diff 时用它判断「这是同一行」，从而复用元素、保留过渡动画与内部状态（见 [list_item.rs:44-48](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list_item.rs#L44-L48)）。

---

## 5. 综合实践

把本讲知识串起来：**用 `Tree` 渲染一个至少 3 层的静态文件目录树，实现展开/折叠，点击节点打印其 `id`，并挂一个右键菜单**。

参考 [crates/story/src/stories/tree_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/tree_story.rs)，按以下步骤实现一个精简版（**示例代码**，需放入一个 `Render` 视图的 `render` 中，并提前在 `cx.new` 里建好 `tree_state`）：

1. **建状态与数据**：构造一棵 3 层的树（根目录 → 子目录 → 文件），至少把第一层目录设为 `expanded(true)`，让初始就能看到第 2、3 层。

   ```rust
   // 示例代码
   let tree_state = cx.new(|cx| {
       TreeState::new(cx).items(vec![
           TreeItem::new("project", "project").expanded(true).child(
               TreeItem::new("project/src", "src").expanded(true)
                   .child(TreeItem::new("project/src/main.rs", "main.rs"))
                   .child(TreeItem::new("project/src/lib.rs", "lib.rs")),
           ),
           TreeItem::new("project/Cargo.toml", "Cargo.toml"),
       ])
   });
   ```

2. **渲染树**：用 `tree(&state, render_item)`，闭包内按 `is_folder()/is_expanded()` 选 `Folder/FolderOpen/File` 图标，按 `entry.depth()` 缩进，`on_click` 打印 `id`（用 4.5.4 的 `view.update` 模式）。
3. **挂右键菜单**：链式调用 `.context_menu(|_ix, entry, menu, _window, _cx| menu.menu("Open", Box::new(...)).separator().menu("Delete", Box::new(...)))`，仿 [tree_story.rs:265-271](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/tree_story.rs#L265-L271)。
4. **验证清单**：
   - 启动后能看到展开的 `project/src` 下的两个文件（说明 4.1 扁平化生效）；
   - 点击 `src` 能折叠/展开（说明 4.2 `toggle_expand` 生效）；
   - 选中一个节点后按 `↓/↑/←/→/Enter` 表现符合 4.3 的语义表；
   - 点击节点控制台打印其 `id`（说明 4.5 `render_item` + `view.update` 模式生效）；
   - 右键出现菜单（说明 4.3 `context_menu` 生效）。
5. **进阶**：订阅 `TreeEvent`（4.2.4 的 `cx.subscribe`），在折叠/展开时打印事件携带的 `id`，验证「payload 是 id 不是下标」。
6. 运行结果**待本地验证**（依赖本机 GPUI 图形环境）。

## 6. 本讲小结

- `Tree` 采用「**嵌套 `TreeItem` 描述 + 内部扁平化为 `Vec<TreeEntry>`**」的双层模型，扁平化只纳入已展开的子树，缩进靠 `entry.depth()`。
- 状态全部集中在 **`TreeState`** 这个 `EventEmitter<TreeEvent>` 实体里：选中用扁平下标 `selected_ix`，展开/折叠通过翻转 `state.expanded` + `rebuild_entries()` 重铺数组，并向外广播 `Expanded/Collapsed(id)` 事件（payload 是稳定 `id` 而非下标）。
- 键盘导航用 **`key_context("Tree")` + `actions`** 体系：`init` 绑定方向键到 `SelectUp/Down/Left/Right/Confirm`，外壳用 `.on_action(...)` 路由；只有焦点落在 `Tree` 上时才生效。
- 渲染用 GPUI 内置的 **`uniform_list`** 做等高虚拟化，每行调用户的 `render_item` 闭包生成 `ListItem`；右键菜单复用 u5-l4 的 `context_menu` 机制。
- **`IndexPath { section, row, column }`** 是库通用坐标（`List` 体系用），`Tree` 内部并不直接用它，而用扁平 `usize`；二者等价关系为 `IndexPath{0, n, 0}`，且都「随展开状态变化而不稳定」，程序内稳定定位应优先用 `TreeItem::id`。
- 渲染闭包里要把 `on_click` 回调到外层视图时，需用 **`view.update(cx, |_, cx| { ... ListItem ... })`** 把 `&mut App` 升级为 `Context<T>` 才能用 `cx.listener`。

## 7. 下一步学习建议

- **横向对比虚拟化组件**：回到 u7-l1 重读 `List`/`ListState`/`ListDelegate`，对比 `Tree` 与 `List` 在「状态实体 + 无状态外壳 + 虚拟化」上的同构与差异（`List` 用 `IndexPath` 分段，`Tree` 用扁平下标）。
- **深入 `ListItem`**：阅读 [crates/ui/src/list/list_item.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/list/list_item.rs)，掌握 `Selectable`/`Disableable` trait 与 `selected/secondary_selected/confirmed` 等多态样式，理解 `Tree` 每一行的能力来源。
- **可搜索/多选树**：仿照 [docs/docs/components/tree.md](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/docs/docs/components/tree.md) 的「Search and Filter」「Multi-Select Tree」示例，结合 u5-l5 的 `SearchableListState` 实现带搜索过滤的树。
- **进入富文本单元**：`Tree` 常作为文件浏览器出现在编辑器侧栏，下一步可进入 u8（Text/Markdown/HTML 渲染）与 u9（代码编辑器 + LSP），把「点击树节点 → 打开文件 → 编辑器渲染」的完整链路打通。
