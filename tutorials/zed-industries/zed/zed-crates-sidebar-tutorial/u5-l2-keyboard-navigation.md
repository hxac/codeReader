# 键盘导航与确认：选择、展开与折叠

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `SelectNext` / `SelectPrevious` / `SelectFirst` / `SelectLast` 四个导航动作对 `selection` 字段的精确推进规则，包括从 `None` 起步、到头回绕、到顶清空这三种边界行为。
- 区分 `Confirm` 在三种行类型（项目分组头 / 线程 / 终端）上的分流逻辑：分组头上是折叠切换，线程与终端上是激活。
- 理解 `SelectChild`（右方向键）/ `SelectParent`（左方向键）这对「树形导航」动作，特别是「在子行上按左键会收起父分组并把选择迁移到父分组头」这一交互。
- 掌握 `neighboring_activatable_entry` 如何在删除/归档/关闭某个条目之前，预先挑出「下一个该激活的邻居」。
- 用自己的话回答：**`selection` 的合法取值范围到底由谁维护？**（这是本讲代码实践的落脚点，答案可能和你直觉不同。）

## 2. 前置知识

在进入源码之前，先回顾几个本讲要反复用到的概念。它们大多在前面几讲建立过，这里只做最小回顾。

### 2.1 selection 是「键盘焦点下标」，不是「当前条目」

`Sidebar` 结构体中的 `selection: Option<usize>`（[sidebar.rs:745](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L745)）是**键盘选中行在 `contents.entries` 里的下标**。它和全局高亮的 `active_entry`（活跃线程/终端）是完全不同的两个概念——u2-l3 已详细辨析过：

| | `selection` | `active_entry` |
|---|---|---|
| 含义 | 键盘焦点所在行 | 当前真正打开的条目 |
| 类型 | `Option<usize>`（下标） | `Option<ActiveEntry>`（身份） |
| 谁写 | 导航动作、各种清空点 | 面板事件同步、用户激活 |
| 何时可见 | 仅当侧边栏持有焦点 | 任何时候（背景高亮） |

渲染端的判定只有一行（[sidebar.rs:2173-2175](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2173-L2175)）：

```rust
let is_focused = self.focus_handle.is_focused(window);
// is_selected means the keyboard selector is here.
let is_selected = is_focused && self.selection == Some(ix);
```

也就是说：焦点不在侧边栏时，即使 `selection` 有值也不画选中样式。这解释了为什么很多代码路径可以「懒得清空」selection。

### 2.2 列表是「分组头 + 行」交替的一维数组

`contents.entries` 是一个扁平的 `Vec<ListEntry>`，结构大致是：

```
[ProjectHeader(A), Thread(a1), Terminal(a2), ProjectHeader(B), Thread(b1), ...]
```

折叠分组 A 后，A 的子行从数组里**物理消失**（重建时不再压入），只剩 `ProjectHeader(A)`。所以「下标导航」天然就是树形导航——折叠改变了数组长度，也就改变了下标语义。这一点是理解本讲所有行为的关键前提（折叠与重建的关系见 u3-l2、u4-l2）。

### 2.3 动作来自 `menu` 命名空间

本讲的导航动作不是 sidebar crate 自己定义的，而是从 `menu` crate 借来的（[sidebar.rs:38-39](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L38-L39)）：

```rust
use menu::{
    Cancel, Confirm, SelectChild, SelectFirst, SelectLast, SelectNext, SelectParent, SelectPrevious,
```

由于 `dispatch_context` 给根容器加了 `"ThreadsSidebar"` 和 `"menu"` 两个上下文词（u5-l1 讲过），Zed 的默认键位表里两套绑定都能命中：

- 全局 `menu` 上下文绑定（[default-macos.json:7-21](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/assets/keymaps/default-macos.json#L7-L21)）：`down`/`ctrl-n`/`tab` → `menu::SelectNext`，`up`/`ctrl-p` → `menu::SelectPrevious`，`pageup`/`cmd-up` → `SelectFirst`，`pagedown`/`cmd-down` → `SelectLast`，`enter` → `Confirm`。
- `ThreadsSidebar` 专属绑定（[default-macos.json:788-811](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/assets/keymaps/default-macos.json#L788-L811)）：`left` → `menu::SelectParent`，`right` → `menu::SelectChild`，`enter` 与 `space` → `menu::Confirm`（`space` 仅在 `not_searching` 时生效）。

### 2.4 折叠状态住在 MultiWorkspace，不在 Sidebar

分组是否折叠存在宿主 `MultiWorkspace` 的 `ProjectGroupState.expanded` 里，`Sidebar` 只有一对薄门面（u4-l2 讲过）：

- `is_group_collapsed(key)`：现查宿主（[sidebar.rs:930-939](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L930-L939)）
- `set_group_expanded(key, expanded)`：写宿主并触发持久化（[sidebar.rs:941-950](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L941-L950)）

改完折叠后**必须调用 `update_entries(cx)` 重建列表**，子行才会真正从数组里消失/回来。本讲的折叠动作全都遵守这个「改状态 → 重建」的两拍节奏。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [crates/sidebar/src/sidebar.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs) | 本讲主战场：导航、确认、折叠动作与邻居查找全部在此（约 8200 行） |
| [crates/sidebar/src/sidebar_tests.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs) | 七个 `test_keyboard_*` 测试，是本讲行为的「规格说明书」 |
| [assets/keymaps/default-macos.json](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/assets/keymaps/default-macos.json) | 默认键位表，把物理按键映射到 `menu::*` 动作 |

sidebar.rs 内本讲涉及的关键片段（按行号排序）：

| 行号 | 内容 |
|---|---|
| 407-431 | `ActivatableEntry` 枚举与 `from_list_entry` 转换 |
| 930-950 | `is_group_collapsed` / `set_group_expanded` 门面 |
| 2149-2162 | `select_first_entry`（搜索刷新后的「选中第一个可激活行」） |
| 3229-3238 | `toggle_collapse` |
| 3445-3466 | `editor_move_down` / `editor_move_up`（搜索框内的导航转发） |
| 3468-3516 | `select_next` / `select_previous` / `select_first` / `select_last` |
| 3518-3562 | `confirm` |
| 4250-4304 | `expand_selected_entry` / `collapse_selected_entry` |
| 4306-4367 | `toggle_selected_fold` / `fold_all` / `unfold_all` |
| 4386-4423 | `neighboring_activatable_entry` |
| 4425 起 | `activate_entry`（邻居的消费者） |
| 5637-5686 | `archive_selected_thread` / `rename_selected_thread`（同为 selection 的读取方） |
| 7778-7789 | render 根容器上的动作注册 |

## 4. 核心概念与源码讲解

### 4.1 模块一：select_next / select_previous —— 下标推进与三种边界

#### 4.1.1 概念说明

这是一对最基础也最容易被轻视的函数。它们回答的问题是：**给定当前 `selection`（可能是 `None`，也可能是过期下标），按一次「下/上」之后 `selection` 应该变成什么？**

设计上有两个关键决策值得注意：

1. **`None` 是合法起点，且两个方向起点不同**。焦点刚落到侧边栏时 `selection` 是 `None`（`focus_in` 不再设置默认选中，见测试 `test_keyboard_focus_in_does_not_set_selection`）。从 `None` 按「下」从头开始（下标 0），从 `None` 按「上」从尾开始（最后一个下标）——这是「从任意端进入列表」的自然交互。
2. **底部回绕，顶部清空**。到底再按「下」会绕回 0（循环列表）；到顶（下标 0）再按「上」则把 `selection` 置 `None` 并**把焦点交还给搜索框**。这个不对称是有意的：向下是浏览，向上是「退出列表、回到输入」。

#### 4.1.2 核心流程

`select_next` 的决策表（`len` 为 `contents.entries.len()`）：

```
当前 selection        | 条件            | 结果
---------------------+-----------------+------------------
Some(ix), ix+1 < len |                 | Some(ix + 1)
Some(_)              | len > 0         | Some(0)   ← 到底回绕
None                 | len > 0         | Some(0)   ← 从头进入
其余（len == 0）      |                 | 直接 return（列表为空，什么都不做）
```

`select_previous` 的决策表：

```
当前 selection | 结果
---------------+------------------------------------------------------------
Some(0)        | selection = None，焦点移交 filter_editor，cx.notify()
Some(ix)       | Some(ix - 1)，滚动到 ix-1
None, len > 0  | Some(len - 1)，滚动到最后一行  ← 从尾部进入
None, len == 0 | 什么都不做
```

注意一个细节：`select_next` 的第一条分支只看 `ix + 1 < len`，**没有检查 `ix < len`**。如果 `selection` 因列表缩短而指向了 `len` 之外（后面 4.1.4 会解释这如何可能发生），第一条分支不命中，落入第二条「到底回绕」→ 归一到 0。也就是说，`select_next` 顺带把过期下标「洗」回合法范围。`select_previous` 同理：过期下标 `ix` 会走 `Some(ix) => Some(ix - 1)`，一步步减回合法区。

#### 4.1.3 源码精读

四个函数全部集中在 [sidebar.rs:3468-3516](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3468-L3516)：

```rust
fn select_next(&mut self, _: &SelectNext, _window: &mut Window, cx: &mut Context<Self>) {
    let next = match self.selection {
        Some(ix) if ix + 1 < self.contents.entries.len() => ix + 1,
        Some(_) if !self.contents.entries.is_empty() => 0,
        None if !self.contents.entries.is_empty() => 0,
        _ => return,
    };
    self.selection = Some(next);
    self.list_state.scroll_to_reveal_item(next);
    cx.notify();
}
```

这段代码（[sidebar.rs:3468-3478](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3468-L3478)）就是上面决策表的直译：三行 match 臂分别对应「正常下移」「到底回绕」「从 None 进入」，空列表直接返回。每次移动后调用 `list_state.scroll_to_reveal_item(next)` 让虚拟列表滚动到新选中行可见，再 `cx.notify()` 触发重渲染。

```rust
fn select_previous(&mut self, _: &SelectPrevious, window: &mut Window, cx: &mut Context<Self>) {
    match self.selection {
        Some(0) => {
            self.selection = None;
            self.filter_editor.focus_handle(cx).focus(window, cx);
            cx.notify();
        }
        Some(ix) => {
            self.selection = Some(ix - 1);
            self.list_state.scroll_to_reveal_item(ix - 1);
            cx.notify();
        }
        None if !self.contents.entries.is_empty() => {
            let last = self.contents.entries.len() - 1;
            self.selection = Some(last);
            self.list_state.scroll_to_reveal_item(last);
            cx.notify();
        }
        None => {}
    }
}
```

[sidebar.rs:3480-3500](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3480-L3500)。注意它是四个函数里唯一用到 `window` 的——因为「到顶清空」那条分支要把焦点交还给搜索框（`filter_editor.focus_handle(cx).focus(window, cx)`），这是唯一产生焦点迁移的导航分支。

```rust
fn select_first(&mut self, _: &SelectFirst, _window: &mut Window, cx: &mut Context<Self>) {
    if !self.contents.entries.is_empty() {
        self.selection = Some(0);
        self.list_state.scroll_to_reveal_item(0);
        cx.notify();
    }
}

fn select_last(&mut self, _: &SelectLast, _window: &mut Window, cx: &mut Context<Self>) {
    if let Some(last) = self.contents.entries.len().checked_sub(1) {
        self.selection = Some(last);
        self.list_state.scroll_to_reveal_item(last);
        cx.notify();
    }
}
```

[sidebar.rs:3502-3516](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3502-L3516)。这两个是纯跳转：`SelectFirst` 直接落 0，`SelectLast` 用 `checked_sub(1)` 优雅处理空列表（空表时 `checked_sub` 返回 `None`，整个 `if let` 不执行——比 `len() - 1` 的 usize 下溢 panic 安全）。

还有一组容易漏掉的「影子导航」——当焦点在**搜索框**里时按上下键，`editor::MoveDown` / `MoveUp` 会被转发到列表导航（[sidebar.rs:3445-3457](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3445-L3457)）：

```rust
fn editor_move_down(&mut self, _: &MoveDown, window: &mut Window, cx: &mut Context<Self>) {
    self.select_next(&SelectNext, window, cx);
    if self.selection.is_some() {
        self.focus_handle.focus(window, cx);
    }
}
```

在搜索框里按下方向键 → 列表选中第一项 → 焦点从编辑器切到侧边栏本体。此后键盘事件走的就是 `ThreadsSidebar` 上下文而非编辑器上下文，`down/up` 由全局 `menu` 绑定接管。「在搜索框打字 → 按下进入列表」的完整链路就是这两行。

#### 4.1.4 代码实践

**实践目标**：用测试固化对四种边界行为的理解，并亲手验证「过期下标」会被导航洗回合法范围。

**操作步骤**（在 Zed 仓库根目录执行）：

1. 运行 `cargo test -p sidebar --lib test_keyboard_select_next_and_previous`，对照 [sidebar_tests.rs:1357-1414](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L1357-L1414) 阅读断言。该测试播种 3 个线程后列表为 `[header, thread3, thread2, thread1]`（4 项，新线程排前），依次断言：`SelectNext` 四次从 `None` 走到 `Some(3)`；第五次回绕到 `Some(0)`；`SelectPrevious` 三次退回 `Some(0)`；再按一次变 `None`。
2. 运行 `cargo test -p sidebar --lib test_keyboard_select_first_and_last`（[sidebar_tests.rs:1416-1436](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L1416-L1436)）与 `test_keyboard_navigation_on_empty_list`（[sidebar_tests.rs:1617-1649](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L1617-L1649)，只有分组头一项的场景）。
3. **本地验证过期下标**（需要改测试文件，做完还原）：复制 `test_keyboard_select_next_and_previous` 为一个新测试，在 `focus_sidebar` 之后直接注入 `sidebar.update_in(cx, |s, _, _| { s.selection = Some(99); });`（越过 `entries.len()`），再 `dispatch_action(SelectNext)`，断言 `selection == Some(0)`。

**需要观察的现象**：第 3 步中，指向 99 的「野下标」既没有 panic，也没有停留在 99——`select_next` 的第二条 match 臂把它当作「到底」，回绕到 0。

**预期结果**：三个现成测试全部通过（它们是 CI 的一部分）；第 3 步的自制测试也通过，证明导航函数自带「过期值归一化」。

（第 3 步为待本地验证项：本讲义未运行该命令，请以实际输出为准；如果你不方便改测试文件，仅阅读 `select_next` 的 match 结构推导出同样结论也可。）

#### 4.1.5 小练习与答案

**练习 1**：列表共 5 项，`selection = Some(4)`（最后一项）。连续按两次「下」，再按一次「上」，`selection` 最终是什么？焦点在哪？

<details>
<summary>参考答案</summary>

第一次「下」：`ix + 1 = 5` 不小于 `len = 5`，命中回绕臂 → `Some(0)`。第二次「下」：`0 + 1 < 5` → `Some(1)`。再「上」：`Some(1)` 走 `Some(ix)` 臂 → `Some(0)`。焦点仍在侧边栏本体（只有 `Some(0)` 时按「上」才会交还焦点给搜索框，而此时没再按一次）。
</details>

**练习 2**：为什么 `select_last` 用 `checked_sub(1)` 而不是 `entries.len() - 1`？

<details>
<summary>参考答案</summary>

`entries.len()` 是 `usize`，空列表时 `0 - 1` 会因下溢而 panic。`checked_sub(1)` 在空列表时返回 `None`，让 `if let` 分支整体跳过，函数安全地变成空操作。这也符合项目规范「避免可能 panic 的操作」。
</details>

**练习 3**：`editor_move_down` 在转发给 `select_next` 之后为什么还要 `focus_handle.focus(window, cx)`，而 `select_next` 自己不做这件事？

<details>
<summary>参考答案</summary>

`select_next` 是纯粹的「移动下标」，不管理焦点，`SelectNext` 在侧边栏已持焦时也会触发，此时再 focus 是多余的。而 `editor_move_down` 的语义是「从搜索框进入列表」：焦点原本在编辑器里，如果不把焦点切到侧边栏本体，后续的 `left/right/enter` 仍会被编辑器消费，永远到不了列表的动作处理器。`if self.selection.is_some()` 的守卫则保证空列表时不白白抢焦点。
</details>

### 4.2 模块二：confirm —— 一个动作，三种行为

#### 4.2.1 概念说明

`Confirm`（回车 / 空格）是列表交互的「万能执行键」。侧边栏是异构列表——行可能是分组头、线程、终端——所以 `confirm` 的第一件事就是**按行类型分流**：

| 行类型 | Confirm 的行为 |
|---|---|
| `ProjectHeader` | 切换该分组的折叠状态（`toggle_collapse`） |
| `Thread` 且 workspace 为 `Open` | 本地激活该线程（`activate_thread`） |
| `Thread` 且 workspace 为 `Closed` | 先打开工作区再激活（`open_workspace_and_activate_thread`） |
| `Terminal` | 激活该终端（`activate_terminal_entry`） |

另有一个藏在最前面的第四种行为：**如果正在进行行内重命名，回车先提交重命名**（u5-l4 会展开重命名状态机，这里只需知道 `finish_thread_rename` 返回 `true` 表示「消费了这次回车」，函数立即返回）。

#### 4.2.2 核心流程

```
confirm(Confirm)
  ├─ finish_thread_rename()?  ── 正在重命名 → 提交重命名并返回
  ├─ selection 为 None？       ── 直接返回（无可确认项）
  ├─ entries.get(ix) 越界？    ── 直接返回（防御过期下标）
  └─ match 行类型
       ├─ ProjectHeader{key}  → toggle_collapse(key) → update_entries
       ├─ Thread{metadata, workspace}
       │    ├─ Open(workspace)  → activate_thread(metadata, workspace, false)
       │    └─ Closed{folder_paths, project_group_key}
       │                        → open_workspace_and_activate_thread(...)
       └─ Terminal{metadata, workspace} → activate_terminal_entry(...)
```

`toggle_collapse` 本身只有三行（[sidebar.rs:3229-3238](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3229-L3238)）：读当前折叠状态 → 写反值（顺带触发宿主持久化）→ `update_entries` 重建列表。**确认即切换**（toggle），不是「展开」也不是「收起」，所以测试里连按两次回车会先收起再展开。

#### 4.2.3 源码精读

完整函数在 [sidebar.rs:3518-3562](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3518-L3562)：

```rust
fn confirm(&mut self, _: &Confirm, window: &mut Window, cx: &mut Context<Self>) {
    if self.finish_thread_rename(window, cx) {
        return;
    }

    let Some(ix) = self.selection else { return };
    let Some(entry) = self.contents.entries.get(ix) else {
        return;
    };

    match entry {
        ListEntry::ProjectHeader { key, .. } => {
            let key = key.clone();
            self.toggle_collapse(&key, window, cx);
        }
        ListEntry::Thread(thread) => {
            let metadata = thread.metadata.clone();
            match &thread.workspace {
                ThreadEntryWorkspace::Open(workspace) => {
                    let workspace = workspace.clone();
                    self.activate_thread(metadata, &workspace, false, window, cx);
                }
                ThreadEntryWorkspace::Closed {
                    folder_paths,
                    project_group_key,
                } => {
                    let folder_paths = folder_paths.clone();
                    let project_group_key = project_group_key.clone();
                    self.open_workspace_and_activate_thread(
                        metadata, folder_paths, &project_group_key, window, cx,
                    );
                }
            }
        }
        ListEntry::Terminal(terminal) => {
            let metadata = terminal.metadata.clone();
            let workspace = terminal.workspace.clone();
            self.activate_terminal_entry(metadata, workspace, false, window, cx);
        }
    }
}
```

逐段说明：

- 前两行（[sidebar.rs:3519-3521](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3519-L3521)）：重命名优先。`finish_thread_rename` 只有在 `renaming_thread_id` 存在时才返回 `true` 并消费这次回车。
- [sidebar.rs:3523-3526](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3523-L3526)：双重守卫——`selection` 为 `None` 直接返回；`entries.get(ix)` 用 `Option` 模式而非索引，越界（列表缩短导致的过期下标）时安静返回，不 panic。
- 分组头分支（[sidebar.rs:3529-3532](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3529-L3532)）：`key.clone()` 是为了把所有权从借用的 `entry` 里拿出来——后面 `toggle_collapse(&mut self)` 需要 `&mut self`，与继续借用 `self.contents` 冲突。
- 线程分支（[sidebar.rs:3533-3555](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3533-L3555)）：按 `ThreadEntryWorkspace` 的 Open/Closed 两形态（u2-l2 讲过）再分一层。`Open` 直接本地激活；`Closed` 携带 `folder_paths` 与 `project_group_key` 两种「重开身份材料」走「先开工作区再激活」的慢路径。激活的完整决策树是 u6-l1 的主题，本讲只需记住 confirm 是它的入口之一。

#### 4.2.4 代码实践

**实践目标**：通过 `test_keyboard_confirm_on_project_header_toggles_collapse` 验证「分组头上的回车 = 折叠切换」，并理解测试里的视觉编码。

**操作步骤**：

1. 运行 `cargo test -p sidebar --lib test_keyboard_confirm_on_project_header_toggles_collapse`。
2. 对照 [sidebar_tests.rs:1470-1520](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L1470-L1520)。测试先用 `visible_entries_as_strings`（[sidebar_tests.rs:547-604](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L547-L604)）断言初始状态是 `["v [my-project]", "  Thread 1"]`——`v` 表示展开、`>` 表示折叠、`  <== selected` 标记选中行；然后手动把 `selection` 设为 `Some(0)`（分组头），`dispatch_action(Confirm)` 后断言只剩 `["> [my-project]  <== selected"]`；再按一次 `Confirm`，断言恢复两项。
3. 注意 `dispatch_action` 之后都有一次 `cx.run_until_parked()`——因为 `toggle_collapse` 内部的 `update_entries` 与重建虽是同步的，但后续可能排入异步任务，测试统一泵到收敛再断言。

**需要观察的现象**：两次回车后列表长度在 1 和 2 之间振荡，选中标记始终停留在下标 0 的分组头上。

**预期结果**：测试通过。折叠后 `Thread 1` 行消失——它被重建管线物理移出了 `entries`，而不是被 CSS 隐藏。

#### 4.2.5 小练习与答案

**练习 1**：在分组头上按回车时，`selection` 会移动吗？

<details>
<summary>参考答案</summary>

不会。`toggle_collapse` 只改折叠状态并重建列表，不触碰 `selection`。由于折叠只删除分组头**之后**的子行、分组头本身留在原下标（u4-l2 的「先记下标再压头」规则），选中下标在重建前后仍然指向同一个分组头——这正是测试断言里 `<== selected` 标记位置不变的原因。
</details>

**练习 2**：如果 `selection` 指向的行在一次重建后被删除了（比如别的窗口归档了该线程），此时按回车会发生什么？

<details>
<summary>参考答案</summary>

什么都不会发生。`self.contents.entries.get(ix)` 对越界下标返回 `None`，`let ... else { return }` 安静退出。这是「无集中钳制」策略的另一半：读取方全部用 `get` 防御，过期下标不会 panic，只是无效。下一次方向键导航会把下标洗回合法范围（见 4.1）。
</details>

**练习 3**：为什么 `confirm` 里每个分支都要先 `clone()` 再调用 `&mut self` 的方法？

<details>
<summary>参考答案</summary>

`entry` 是从 `self.contents.entries` 借用来的，而 `toggle_collapse` / `activate_thread` 等方法需要 `&mut self`，Rust 的借用规则不允许在持有 `&self.contents` 借用的同时再可变借用整个 `self`。先 clone 出 `key` / `metadata` / `workspace` 这些小体积身份数据，就能在调用前结束对 entries 的借用。
</details>

### 4.3 模块三：expand_selected_entry / collapse_selected_entry —— 树形导航与「收起到父级」

#### 4.3.1 概念说明

左/右方向键（`menu::SelectParent` / `menu::SelectChild`）把扁平列表当作树来导航。它们与 `Confirm` 的折叠切换共享同一套折叠机制，但语义更细：

- **`SelectChild`（右）**：选中分组头且该组**已折叠** → 展开它；选中分组头且该组**已展开** → 选中移到下一行（钻入组内第一个子行）。对线程/终端行按右键是空操作。
- **`SelectParent`（左）**：选中分组头且该组**已展开** → 收起它；选中分组头且已折叠 → 空操作（不能再往左了）；选中**子行**（线程/终端）→ **收起它所属的分组，并把选择迁移到该分组头**。

第三个行为是本模块的精髓：「在子行上按左键」不是简单地把光标上移一行，而是**收起到父级**（collapse-to-parent）。用户在树形列表里按左键的心智预期是「把这个层级收起来、回到父节点」，而不是「光标左移」——因为这是一维列表，根本没有「左」可言。

配套还有三个动作：

- `toggle_selected_fold`（编辑器的 `ToggleFold`，通常无默认键位）：不论选中的是头还是子行，找到所属分组头并切换其折叠；收起时额外把选择移到分组头。
- `fold_all` / `unfold_all`（`editor::actions::FoldAll` / `UnfoldAll`）：一次性收起/展开全部分组，直接委托宿主的 `set_all_groups_expanded`。

#### 4.3.2 核心流程

`SelectChild`（右键）：

```
selection → entries.get(ix)
  ├─ ProjectHeader{key}
  │    ├─ 组已折叠 → set_group_expanded(true) + update_entries（selection 不动）
  │    └─ 组已展开且 ix+1 < len → selection = ix+1 + 滚动（钻入）
  └─ 其他（线程/终端/越界） → 空操作
```

`SelectParent`（左键）：

```
selection → entries.get(ix)
  ├─ ProjectHeader{key} 且未折叠 → set_group_expanded(false) + update_entries
  ├─ Thread/Terminal → 从 ix-1 往前找第一个 ProjectHeader：
  │      找到 i → selection = Some(i)（选择迁移到父头）
  │             + set_group_expanded(key_i, false) + update_entries
  │      找不到（理论上不会，首行必是头）→ 什么都不做
  └─ 越界 → 空操作
```

「往前扫描找头」的 `(0..ix).rev()` 循环是树形结构在一维数组上的经典投影：**某行的父分组头就是它左边最近的那个 `ProjectHeader`**。

#### 4.3.3 源码精读

[sidebar.rs:4250-4272](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4250-L4272) 是 `expand_selected_entry`：

```rust
fn expand_selected_entry(
    &mut self,
    _: &SelectChild,
    _window: &mut Window,
    cx: &mut Context<Self>,
) {
    let Some(ix) = self.selection else { return };

    match self.contents.entries.get(ix) {
        Some(ListEntry::ProjectHeader { key, .. }) => {
            let key = key.clone();
            if self.is_group_collapsed(&key, cx) {
                self.set_group_expanded(&key, true, cx);
                self.update_entries(cx);
            } else if ix + 1 < self.contents.entries.len() {
                self.selection = Some(ix + 1);
                self.list_state.scroll_to_reveal_item(ix + 1);
                cx.notify();
            }
        }
        _ => {}
    }
}
```

对分组头的两分支：折叠则展开（走「改状态 → 重建」两拍），已展开则下移一格钻入。注意「展开」分支**不动 selection**——分组头还在原下标，展开只是让子行重新出现在它后面。

[sidebar.rs:4274-4304](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4274-L4304) 是 `collapse_selected_entry`，重点看子行分支：

```rust
Some(ListEntry::Thread(_) | ListEntry::Terminal(_)) => {
    for i in (0..ix).rev() {
        if let Some(ListEntry::ProjectHeader { key, .. }) = self.contents.entries.get(i)
        {
            let key = key.clone();
            self.selection = Some(i);
            self.set_group_expanded(&key, false, cx);
            self.update_entries(cx);
            break;
        }
    }
}
```

三件事的顺序值得玩味：先把 `selection` 设为父头下标 `i`，再收起，再重建。因为收起会**删除从 `i+1` 到组尾的所有行**，而 `i` 本身不受影响，所以先设后收是安全的——重建后 `Some(i)` 仍指向这个分组头。反过来（先收起再找头）就要在新数组上重新定位了。

`toggle_selected_fold`（[sidebar.rs:4306-4339](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4306-L4339)）复用了同一段「往前找头」逻辑，但用 `find` 写得更函数式：

```rust
let header_ix = match self.contents.entries.get(ix) {
    Some(ListEntry::ProjectHeader { .. }) => Some(ix),
    Some(ListEntry::Thread(_) | ListEntry::Terminal(_)) => (0..ix).rev().find(|&i| {
        matches!(
            self.contents.entries.get(i),
            Some(ListEntry::ProjectHeader { .. })
        )
    }),
    None => None,
};
```

随后的切换有个细节：**展开时不移动 selection，收起时把 selection 拉到分组头**（[sidebar.rs:4330-4335](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4330-L4335)）。收起后子行消失，若选择还停在已被删除的子行下标上就成了野下标——这里选择主动迁移，而不是等导航来洗。

`fold_all` / `unfold_all`（[sidebar.rs:4341-4367](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4341-L4367)）是最粗粒度的版本，直接对宿主整体设值：

```rust
fn fold_all(&mut self, _: &editor::actions::FoldAll, _window: &mut Window, cx: &mut Context<Self>) {
    if let Some(mw) = self.multi_workspace.upgrade() {
        mw.update(cx, |mw, _cx| {
            mw.set_all_groups_expanded(false);
        });
    }
    self.update_entries(cx);
}
```

还是熟悉的两拍：改宿主状态 + 重建。注意它**不处理 selection**——全收起后若选择原本停在子行上，就会变成野下标，交给 4.1 的导航归一化兜底。

#### 4.3.4 代码实践

**实践目标**：验证「收起到父级」的选择迁移，并观察「已展开的头上按右键 = 钻入」。

**操作步骤**：

1. 运行 `cargo test -p sidebar --lib test_keyboard_collapse_from_child_selects_parent`，对照 [sidebar_tests.rs:1577-1615](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L1577-L1615)。流程：聚焦侧边栏（`selection` 为 `None`）→ 两次 `SelectNext` 走到 `Some(1)`（线程行）→ 一次 `SelectParent` → 断言 `selection == Some(0)` 且可见行只剩 `["> [my-project]  <== selected"]`。
2. 运行 `cargo test -p sidebar --lib test_keyboard_expand_and_collapse_selected_entry`，对照 [sidebar_tests.rs:1522-1575](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L1522-L1575)。注意最后一个断言（[sidebar_tests.rs:1573-1574](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L1573-L1574)）：在已展开的分组头上再按一次 `SelectChild`，`selection` 从 `Some(0)` 变成 `Some(1)`——右键的「钻入」分支，且这条断言后面没有 `run_until_parked`，因为钻入只动下标不重建。
3. 把第 1 步测试里 `SelectParent` 之前的两次 `SelectNext` 改成一次（选中分组头），运行并观察结果——此时左键走的是「收起已展开的头」分支，行为相同但路径不同。

**需要观察的现象**：第 1 步中「按左」之后**两个**状态同时变化：`selection` 从子行下标跳到父头下标，可见行从 2 项变 1 项。

**预期结果**：两个测试均通过。第 3 步的修改后测试同样通过（1 次 `SelectNext` 选中头，左键收起，断言依旧成立）——这恰好说明两条分支殊途同归。

#### 4.3.5 小练习与答案

**练习 1**：列表为 `[Header(A), T1, T2, Header(B), T3]`，`selection = Some(3)`（Header B）。按一次左键，再按一次右键，最后 `selection` 是什么？

<details>
<summary>参考答案</summary>

`Some(3)` 处是已展开的 Header B → 左键收起 B，selection 不动（仍在 3）。此时列表变 `[Header(A), T1, T2, Header(B)]`。右键时 B 已折叠 → 展开分支，selection 仍为 `Some(3)`，列表恢复 5 项。
</details>

**练习 2**：`collapse_selected_entry` 的子行分支为什么用 `for i in (0..ix).rev()` 加 `break`，而不是像 `toggle_selected_fold` 那样用 `(0..ix).rev().find(...)`？

<details>
<summary>参考答案</summary>

两者语义完全等价，只是写法差异：`collapse_selected_entry` 在循环体里找到了头之后要连续做「设 selection、收起、重建、break」四件事，用 `for + break` 更直接；`toggle_selected_fold` 只需要先求出头的下标（`find` 返回 `Option<usize>`），后续动作依赖这个中间值再统一处理。前者是「找到即做事」，后者是「先定位后行动」。
</details>

**练习 3**：`fold_all` 收起全部后，如果 `selection` 原本指向某个子行，会发生什么？用户会看到选中标记跳到哪里？

<details>
<summary>参考答案</summary>

`fold_all` 不迁移 selection，该下标成为野下标（指向已不存在的行）。渲染端 `render_list_entry` 对 `entries.get(ix)` 为 `None` 的行返回空 `div`，且选中标记需要 `selection == Some(ix)` 命中现存行——所以用户看不到任何选中标记。下一次按上下键时，导航函数把这个过期值洗回合法范围（`select_next` 回绕到 0，`select_previous` 逐步递减）。这是「懒清理 + 防御读取」策略在全收起场景的体现。
</details>

### 4.4 模块四：neighboring_activatable_entry —— 删除前先找好接班人

#### 4.4.1 概念说明

前三个模块都在讲「用户主动导航」。最后这个函数解决相反方向的问题：**某个条目即将消失（被归档、被关闭、草稿被删除）时，谁是下一个该被激活的条目？**

试想用户归档了当前活跃的线程：这条行马上要从列表消失，如果什么都不做，界面会突兀地「悬空」。侧边栏的策略是在删除**之前**先算出一个「邻居」（neighbor），删除完成后再激活它，让焦点/活跃状态平滑转移到相邻条目。

`neighboring_activatable_entry(current_position)` 的查找规则体现了两条偏好：

1. **同组优先**：先在「当前条目所在的项目分组」内找；只有整组再无可激活条目时，才扩大到全列表。
2. **向下优先，向上兜底**：组内先从当前位置往下找，往下没有再从当前位置往上找。

而「可激活」（activatable）排除了 `ProjectHeader`——分组头不是激活目标，只有线程和终端行才算。这个过滤由 `ActivatableEntry::from_list_entry` 完成（[sidebar.rs:418-431](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L418-L431)）：

```rust
impl ActivatableEntry {
    fn from_list_entry(entry: &ListEntry) -> Option<Self> {
        match entry {
            ListEntry::Thread(thread) => Some(Self::Thread {
                metadata: thread.metadata.clone(),
            }),
            ListEntry::Terminal(terminal) => Some(Self::Terminal {
                metadata: terminal.metadata.clone(),
                workspace: terminal.workspace.clone(),
            }),
            ListEntry::ProjectHeader { .. } => None,
        }
    }
}
```

`ActivatableEntry` 本体（[sidebar.rs:407-416](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L407-L416)）只携带激活所需的最小身份数据：线程只要 `metadata`（激活时现查 workspace），终端还要带上 `workspace`（终端激活路径直接需要它）。

#### 4.4.2 核心流程

```
neighboring_activatable_entry(current_position):
  1. section_start = current_position 左边最近的 ProjectHeader 的下标 + 1
     （左边没有头 → 0）
  2. section_end   = current_position 右边第一个 ProjectHeader 的下标
     （右边没有头 → entries.len()）
  3. 依次尝试两个区间 [(section_start, section_end), (0, len)]：
       after  = entries[position+1 .. end]      ← 下半区
       before = entries[start .. position]      ← 上半区
       在 after.chain(before.rev()) 里找第一个能转成 ActivatableEntry 的行
       （即：先从紧邻的下一行往下扫，扫不到再从紧邻的上一行往上扫）
  4. 两个区间都找不到 → None（整个列表再无线程/终端）
```

把它写成公式：设当前位置为 \(p\)，候选集合为 \(\mathcal{C}\)（第一轮取本组内除自身外的可激活行，第二轮取全列表），被选中的是到 \(p\) 「距离」最小的候选

\[
\arg\min_{j \in \mathcal{C}} |j - p|, \quad \text{同等距离时} \; j > p \; \text{（下方）胜出}
\]

「向下优先、向上兜底、距离最近、平局下方胜」——这正是 `after.iter().chain(before.iter().rev())` 这一拼接顺序的数学含义：`after` 按下标升序提供下半区，`before.rev()` 按下标降序提供上半区，`find_map` 取第一个可激活者，恰好就是距离最近的那个。

#### 4.4.3 源码精读

[sidebar.rs:4386-4423](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4386-L4423)，带一段说明查找策略的 doc comment：

```rust
/// Find the entry to select after the entry at `current_position` is
/// removed: the nearest activatable entry in the same project section,
/// below first, then above. Only when that section has no other
/// activatable entry, the nearest one in the whole list.
fn neighboring_activatable_entry(&self, current_position: usize) -> Option<ActivatableEntry> {
    let entries = &self.contents.entries;
    let is_header = |entry: &ListEntry| matches!(entry, ListEntry::ProjectHeader { .. });

    let section_start = entries
        .get(..current_position)?
        .iter()
        .rposition(is_header)
        .map_or(0, |header| header + 1);
    let section_end = entries
        .get(current_position + 1..)?
        .iter()
        .position(is_header)
        .map_or(entries.len(), |offset| current_position + 1 + offset);

    for (start, end) in [(section_start, section_end), (0, entries.len())] {
        let Some(before) = entries.get(start..current_position) else {
            continue;
        };
        let Some(after) = entries.get(current_position + 1..end) else {
            continue;
        };

        let Some(entry) = after
            .iter()
            .chain(before.iter().rev())
            .find_map(ActivatableEntry::from_list_entry)
        else {
            continue;
        };
        return Some(entry);
    }
    None
}
```

几个精读要点：

- **切分区间**（前两个计算）：`rposition(is_header)` 在当前位置左侧找最近的头，`+1` 跳过头本身得到组内首行；`position(is_header)` 在右侧找第一个头得到组尾。两个 `map_or` 分别处理「左边没有头」（本组就是列表开头）与「右边没有头」（本组延伸到列表末尾）。
- **`entries.get(..current_position)?`**：如果 `current_position` 越界（条目已不在列表中），`get` 返回 `None`，`?` 直接让整个函数返回 `None`——又是防御式读取。
- **两轮循环**：`for (start, end) in [(section, section), (0, len)]` 用数组字面量表达了「先本组、后全表」的降级顺序。每轮里 `before`/`after` 的 `get` 若切不出合法区间则 `continue` 到下一轮。
- **拼接顺序**：`after.iter().chain(before.iter().rev())` ——先自上而下扫下半区，再自下而上扫上半区，实现「向下优先、距离最近」。

三个调用方分别在关闭终端（[sidebar.rs:4993-5004](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4993-L5004)）、归档线程（[sidebar.rs:5343-5351](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5343-L5351)）、删除草稿（[sidebar.rs:6725-6734](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6725-L6734)）之前，先用 `iter().position(...)` 定位即将消失的行，再求邻居。算出的 `neighbor` 作为参数传给 `close_terminal_entry` 等收尾函数，最终由 `activate_entry`（[sidebar.rs:4425 起](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4425)）消费：线程走「写入 `active_entry` + 激活工作区 + 加载线程」，终端直接调 `activate_terminal_entry`。

#### 4.4.4 代码实践

**实践目标**：用纸笔推演 `neighboring_activatable_entry`，再用测试验证「关闭终端后激活邻居」的完整链路。

**操作步骤**：

1. **手工推演**（无需运行）：设 `entries = [H0, T1, M2, T3, H4, T5]`（H = 分组头，T = 线程，M = 终端），分别对 `current_position = 2` 和 `current_position = 3` 求邻居，写下结果后再对照源码验证。
2. 运行 `cargo test -p sidebar --lib test_terminal_close_event_activates_neighbor`，观察「面板发出关闭请求 → 侧边栏关闭终端 → 激活相邻条目」的断言（该测试的逐行拆解是 u6-l2 的实践任务，本讲只需跑通并确认它存在、通过）。
3. 阅读三个调用点上下文（上面源码精读列出的三处），确认它们都遵循「先 `position` 定位、再求邻居、最后把邻居传给收尾函数」的三段式。

**需要观察的现象**：第 1 步中 `current_position = 2` 的邻居是 `T3`（下半区紧邻即可激活行）；`current_position = 3` 时下半区只有 `H4`（头，不可激活），第一轮区间内向下找不到，退到上半区最近的 `M2`。

**预期结果**：手工结果与源码逻辑一致；第 2 步测试通过。

（第 2 步为待本地验证项：本讲义未运行该测试，请以实际输出为准。）

#### 4.4.5 小练习与答案

**练习 1**：`entries = [H0, T1, T2]`，求 `current_position = 1` 的邻居。如果整组只有这一个线程（`entries = [H0, T1]`）呢？

<details>
<summary>参考答案</summary>

第一问：`section = (1, 3)`，after = `[T2]` → 命中 `T2`。第二问：第一轮区间 `(1, 2)` 的 after 与 before 都空，`continue` 到第二轮 `(0, 2)`：after 空、before = `[H0]` 不可激活，`find_map` 返回 `None`，整体返回 `None`——列表里再无备选，调用方据此跳过激活。
</details>

**练习 2**：为什么这个函数只排除 `ProjectHeader`，而不排除草稿（draft）或已完成的线程？

<details>
<summary>参考答案</summary>

因为「可激活」的判定只关乎**这一行能不能被打开**，不关乎它的业务状态。草稿可以打开（继续编辑），已完成线程也可以打开（查看历史）——`from_list_entry` 对所有 `Thread`/`Terminal` 行都返回 `Some`。状态过滤（如归档时跳过运行中的线程）是调用方 `archive_selected_thread`（[sidebar.rs:5637-5669](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5637-L5669)）等其他逻辑的职责，邻居查找保持单一职责。
</details>

**练习 3**：`section_start` 的计算为什么是 `rposition(is_header).map_or(0, |header| header + 1)` 里的 `header + 1`，而 `section_end` 不需要 `- 1`？

<details>
<summary>参考答案</summary>

`section_start` 求的是组内**第一个子行的下标**：左边最近的头在 `header`，子行从 `header + 1` 开始。`section_end` 求的是组内子行**结束边界（开区间右端）**：右侧第一个头本身就占据其下标，子行恰好到它为止，`position` 返回的 offset 加回 `current_position + 1` 后天然就是开区间右端，无需再减。一个求「闭转开要跳过头」，一个「头的位置天然就是边界」，`+1` 与不减正是一体两面。
</details>

## 5. 综合实践

把本讲四个模块串起来：**给「键盘导航的完整循环」写一份行为规格，并用一个自制测试验证其中最容易被忽略的一条**。

任务步骤：

1. **整理动作清单**。从 render 根容器的动作注册（[sidebar.rs:7778-7789](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7778-L7789)）出发，列出本讲涉及的 12 个动作处理器（`select_next`、`select_previous`、`editor_move_down`、`editor_move_up`、`select_first`、`select_last`、`confirm`、`expand_selected_entry`、`collapse_selected_entry`、`toggle_selected_fold`、`fold_all`、`unfold_all`），标注每个的默认键位（查 [default-macos.json:7-21](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/assets/keymaps/default-macos.json#L7-L21) 与 [default-macos.json:788-811](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/assets/keymaps/default-macos.json#L788-L811)）和「是否触碰 selection / 是否触发重建」两个维度。
2. **回答核心问题**：写一段 200 字左右的说明——「`selection` 的合法取值范围由谁维护？」提示：答案不是某个函数，而是一组策略的叠加——各转换点清空为 `None`（输入搜索词、激活工作区、聚焦过滤框等）、所有读取方用 `entries.get()` 防御（confirm、expand/collapse、render_list_entry）、导航动作顺带把野下标洗回合法范围（4.1.2）、渲染端「有焦点且下标命中现存行」才画选中标记（[sidebar.rs:2173-2175](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2173-L2175)）。
3. **写一个回归测试**（可选，需改测试文件后还原）：仿照 `test_keyboard_collapse_from_child_selects_parent` 的结构，编写「`fold_all` 之后 selection 成为野下标，下一次 `SelectNext` 回绕到 0」的测试：播种 2 个线程 → 聚焦 → 导航到 `Some(1)` → `dispatch_action(FoldAll)`（需从 `editor::actions` 引入 `FoldAll`）→ `run_until_parked` → 断言可见行只剩一个头且无选中标记 → `dispatch_action(SelectNext)` → 断言 `selection == Some(0)`。
4. 运行 `cargo test -p sidebar --lib test_keyboard`，确认本讲全部键盘测试（含你的新测试）一起通过。

**预期结果**：你将得到一张「动作 × 键位 × 副作用」对照表、一段关于 selection 维护策略的分析，以及（若完成第 3 步）一个验证「懒清理」策略的测试。若第 3 步断言与实际行为不符，回头检查 `fold_all` 是否真的不迁移 selection（4.3.3 的源码是你的依据）。

## 6. 本讲小结

- **导航四动作**：`select_next` 底部回绕到 0、`select_previous` 顶部清空 selection 并把焦点交还搜索框、`select_first`/`select_last` 纯跳转（空列表安全）；从 `None` 起，「下」进首行、「上」进末行。
- **selection 没有集中钳制者**：重建不会主动修正过期下标。合法性由四道防线共同维护——各转换点清空为 `None`、所有读取方用 `entries.get()` 防御、导航动作顺带把野下标洗回合法范围、渲染端「有焦点且下标命中现存行」才画选中标记。
- **confirm 是类型分流的**：分组头上切换折叠（`toggle_collapse` = 改宿主状态 + 重建两拍），线程按 workspace 的 Open/Closed 分本地激活与「先开工作区再激活」，终端直接激活；最前面还有一条「重命名中先提交重命名」的隐藏分支。
- **左/右键是树形导航**：右键在折叠头上展开、在展开头上钻入下一行；左键在展开头上收起、在子行上**收起到父级**——收起前先把 selection 迁移到父头下标，因为父头下标在重建前后不变。
- **折叠状态住在 MultiWorkspace**：`is_group_collapsed`/`set_group_expanded` 只是薄门面，改完必须 `update_entries` 重建，子行的消失/回归是数组级的，不是视觉隐藏。
- **删除前先找接班人**：`neighboring_activatable_entry` 按「同组优先、向下优先、距离最近、平局下方胜」挑选下一个激活目标，排除分组头；三个调用方（关终端、归档线程、删草稿）都遵循「定位 → 求邻居 → 收尾时激活邻居」三段式。

## 7. 下一步学习建议

- **u5-l3（过滤搜索）**：本讲多次出现「selection 被清空 / `select_first_entry` 被调用」，下一讲把这条线补全——过滤编辑器的 `BufferEdited` 订阅如何在键入时清空 selection（[sidebar.rs:834-843](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L834-L843)），带 `select_first_after_update` 的重建如何选中第一个匹配行，以及 `select_first_entry`（[sidebar.rs:2149-2162](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2149-L2162)）为何优先选「第一个线程/终端行」而不是分组头。
- **u5-l4（行内重命名）**：本讲 `confirm` 开头的 `finish_thread_rename` 分支将在那里展开成完整的状态机（`renaming_thread_id`、`suppress_next_rename_edit` 等字段的迁移）。
- **u6-l1（线程激活全链路）**：`confirm` 的 Thread 分支只是入口，`activate_thread` 的三条路径（本窗口、跨窗口、先开工作区）与 `restoring_tasks` 防重入是下一单元的主题；`neighboring_activatable_entry` 的消费方 `activate_entry` 也在那里完整展开。
- 若想巩固「防御式读取」的味道，可以通读 `selection` 在 sidebar.rs 的全部 40 余处出现（用 `grep -n "selection" crates/sidebar/src/sidebar.rs`），给每一处分类：写入合法值 / 清空 / 防御读取 / 渲染判定。
