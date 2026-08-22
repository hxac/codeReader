# 动作注册与键位上下文

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `gpui::actions!` 宏生成了什么、动作命名空间如何决定它在 keymap JSON 里的全名、以及动作上的 doc comment 会展示给谁看。
2. 逐一说清 [render()](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7760-L7903) 根容器上 25 个 `.on_action` 各自对应的动作全名、处理方法与来源（本 crate 定义 / 借用自 menu、editor、zed_actions、agent_ui、workspace）。
3. 读懂 `dispatch_context` 构造的 `KeyContext`（`os` / `ThreadsSidebar` / `menu` / `searching|editing|not_searching`），并解释一条键位绑定如何按「焦点层级栈」的深度决定优先级。
4. 理解 `focus_in` 与 `prepare_for_focus` 这两条「进入侧边栏」路径如何清理 `selection`（承接 u2-l3：selection 是易失交互状态）。
5. 亲手为自己的 Zed 写一条 `agents_sidebar::NewThreadInGroup` 的键位绑定并验证触发。

## 2. 前置知识

本讲假设你已学完 u4-l1（渲染主骨架）。需要用到的概念：

- **Action（动作）**：Zed 里几乎不存在「按键 → 回调」的直连。键盘交互被拆成两层解耦：**keymap JSON 把按键绑定到动作名**（如 `"cmd-n": "agents_sidebar::NewThreadInGroup"`），**元素树上的处理器监听动作类型**（`.on_action(...)`）。动作本身是一个全局注册表里的类型，命令面板、键位提示、代码里的 `window.dispatch_action` 都用同一个名字调用它，这就是为什么改键不需要改代码。
- **KeyContext（键位上下文）**：一条绑定是否生效不只看按键，还要看「焦点现在停在 UI 的哪一层」。gpui 在派发按键时，会从窗口根到焦点元素把沿途每层元素声明的 `KeyContext` 收集成一个**上下文栈**，绑定按匹配深度排序——越深（越靠近焦点）越优先。
- **`cx.listener`**：回顾 u4-l1——`.on_action(cx.listener(Self::select_next))` 中的 [listener](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/app/context.rs#L262-L272) 把「实体方法」包装成「先 `update` 实体再回调」的处理器，处理器里借到的就是 `&mut Sidebar`。
- **selection 的易失性**：回顾 u2-l3——`selection: Option<usize>` 是键盘焦点所在的行下标，点击列表、输入搜索词都会清空它。本讲 4.4 节会看到「焦点切入侧边栏」这第三种清理时机。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [sidebar.rs:L86-L102](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L86-L102) | 本 crate 自己定义的两组动作：`agents_sidebar::NewThreadInGroup`、`agents_sidebar::ToggleThreadHistory` 与 `dev::DumpWorkspaceInfo` |
| [sidebar.rs:L3240-L3264](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3240-L3264) | `dispatch_context`：渲染期现算 `KeyContext` |
| [sidebar.rs:L3266-L3279](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3266-L3279) | `focus_in`：焦点落入侧边栏自身句柄时的转发逻辑 |
| [sidebar.rs:L7699-L7702](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7699-L7702) | `prepare_for_focus`：外部切入前的选中态清理 |
| [sidebar.rs:L7774-L7804](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7774-L7804) | `render()` 根容器：`.key_context` + `.track_focus` + 25 个 `.on_action` |
| [gpui/src/action.rs:L23-L40](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/action.rs#L23-L40) | `actions!` 宏本体 |
| [gpui/src/keymap/context.rs:L29-L47](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/keymap/context.rs#L29-L47) | `KeyContext` 与 `new_with_defaults`（自动带上 `os` 键值对） |
| [gpui/src/keymap.rs:L150-L164](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/keymap.rs#L150-L164) | 绑定优先级规则：深度优先，同深度后加者优先（用户 keymap 覆盖默认） |
| [gpui/src/elements/div.rs:L1030-L1034](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/elements/div.rs#L1030-L1034) | `capture_action`：在正常派发之前截获动作 |
| [menu/src/menu.rs:L12-L37](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/menu/src/menu.rs#L12-L37) | `menu` 命名空间动作族（本 crate 借用其中 8 个） |
| [zed_actions/src/lib.rs:L919-L940](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/zed_actions/src/lib.rs#L919-L940) | 同为 `agents_sidebar` 命名空间的 `FocusSidebarFilter`、`ToggleThreadSwitcher` |
| [workspace/src/multi_workspace.rs:L122-L161](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L122-L161) | `Sidebar` trait：`prepare_for_focus` 的契约与调用方 |
| [assets/keymaps/default-macos.json:L788-L811](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/assets/keymaps/default-macos.json#L788-L811) | `ThreadsSidebar` 两个绑定块：默认键位如何引用这些动作 |

## 4. 核心概念与源码讲解

### 4.1 gpui::actions!：动作的定义与命名空间

#### 4.1.1 概念说明

动作是 gpui 键盘交互的最小货币。一个动作要可用，需要三件事：

1. **定义**：用 `actions!` 宏（无数据的单位动作）或 `#[derive(Action)]`（带字段的动作）声明一个类型；
2. **绑定**（可选）：在 keymap JSON 里把按键映射到动作全名 `命名空间::Name`；
3. **处理**：在元素树上用 `.on_action` / `.capture_action` 注册处理器（见 4.2）。

命名空间决定动作的字符串全名，是 keymap JSON、命令面板引用动作的唯一凭据。**命名空间不等于 crate**：多个 crate 可以向同一个命名空间贡献动作，本 crate 的 `agents_sidebar::NewThreadInGroup` 与 zed_actions crate 的 `agents_sidebar::FocusSidebarFilter` 就同居一个命名空间——只要全名不撞车（撞车会在 `App` 创建时 panic，见 [action.rs:L53](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/action.rs#L53)）。

#### 4.1.2 核心流程

`actions!(namespace, [A, B])` 对每个名字展开为（见宏本体 [action.rs:L23-L40](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/action.rs#L23-L40)）：

```rust
#[derive(Clone, PartialEq, Default, Debug, gpui::Action)]
#[action(namespace = namespace)]
/// doc comment 原样保留
pub struct A;   // 单位结构体：无参数、无状态
```

也就是「一个派生了 `Action` 的 pub 单位结构体 + 固定命名空间」。doc comment 会被保留并展示给用户（命令面板、键位提示浮层）。

#### 4.1.3 源码精读

本 crate 定义了两组动作：

- [sidebar.rs:L86-L94](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L86-L94) —— 在 `agents_sidebar` 命名空间下声明 `NewThreadInGroup`（在选中/活跃项目分组里新建线程）与 `ToggleThreadHistory`（线程列表 ↔ 历史归档）。doc comment 写得面向最终用户。
- [sidebar.rs:L96-L102](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L96-L102) —— 在 `dev` 命名空间下声明 `DumpWorkspaceInfo`（把多工作区状态倾倒进缓冲区的调试动作；它与 zed_actions 里 `dev` 命名空间的其他调试动作共享前缀）。

对照「同命名空间、跨 crate」的两个邻居：

- [zed_actions/src/lib.rs:L919-L940](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/zed_actions/src/lib.rs#L919-L940) —— `agents_sidebar` 模块里，`ToggleThreadSwitcher` 是**带字段的动作**（`#[derive(Action)]` + `select_last: bool`），所以 keymap 里可以写成 `["agents_sidebar::ToggleThreadSwitcher", { "select_last": true }]` 传参；`FocusSidebarFilter` 则是普通单位动作。
- [agent_ui/src/agent_ui.rs:L213-L233](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_ui.rs#L213-L233) —— `agent` 命名空间下的 `ArchiveSelectedThread`、`RenameSelectedThread` 等，本 crate 只借用不定义。

本 crate 实际用到的动作 import 汇总在 [sidebar.rs:L38-L40](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L38-L40)（`menu` 族）与 [sidebar.rs:L73-L76](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L73-L76)（`zed_actions` 的 editor 与 agents_sidebar 模块）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「命名空间跨 crate 共享」与「动作全名唯一」。

1. 在仓库根目录执行 `rg -n "actions!\(" crates/sidebar/src crates/zed_actions/src crates/menu/src`，找出三处命名空间声明；
2. 再执行 `rg -n "agents_sidebar::" assets/keymaps/`，数一数默认键位里引用了多少个 `agents_sidebar` 动作；
3. 对照上一步结果反推：`agents_sidebar` 命名空间下的动作分别定义在哪几个 crate？

**需要观察的现象**：`agents_sidebar::` 前缀的动作在 keymap 里出现多处（如 `NewThreadInGroup`、`FocusSidebarFilter`、`ToggleThreadHistory`、`ToggleThreadSwitcher`），但它们的 `actions!`/`derive(Action)` 定义分散在 sidebar 与 zed_actions 两个 crate。

**预期结果**：`NewThreadInGroup`、`ToggleThreadHistory` 定义在 [sidebar.rs:L86-L94](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L86-L94)；`FocusSidebarFilter`、`ToggleThreadSwitcher` 定义在 [zed_actions/src/lib.rs:L919-L940](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/zed_actions/src/lib.rs#L919-L940)。命令输出以本地为准（本讲未代跑）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `NewThreadInGroup` 要定义在 sidebar crate，而 `ToggleThreadSwitcher` 定义在 zed_actions crate？

**参考答案**：动作放哪由「谁需要按名字引用它」决定。`NewThreadInGroup` 的语义与处理逻辑都完全属于侧边栏，别处不会派发它；而 `ToggleThreadSwitcher` 需要被全局（如 MultiWorkspace 层、其他面板）按名字派发，还带 `select_last` 参数要在 keymap JSON 里反序列化，所以放在专门承载跨 crate 动作定义的 zed_actions 里，避免反向依赖。

**练习 2**：动作上的 `/// Creates a new thread in ...` doc comment 会被谁看到？

**参考答案**：最终用户。这类注释会出现在命令面板条目与键位提示（tooltip）里；同时也是对开发者的文档。这就是为什么它们用面向用户的祈使句写法。

**练习 3**：如果两个 crate 都用 `actions!(agents_sidebar, [Foo])` 定义 `Foo`，会发生什么？

**参考答案**：同名同命名空间的动作重复注册，会在 `App` 创建时 panic（[action.rs:L53](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/action.rs#L53) 明确记载）。全局注册表靠全名唯一性工作。

### 4.2 render() 根容器上的 .on_action 全家福

#### 4.2.1 概念说明

定义动作只是「挂号」，处理动作才是「接诊」。处理器注册在元素树上：按键发生后，gpui 先用上下文栈匹配出动作（4.3 节），再把动作**沿焦点路径派发**——从焦点元素向祖先冒泡，沿途任何注册了该动作类型的元素都能处理或放行。侧边栏把 25 个处理器集中挂在 render() 的根容器上（u4-l1 讲过这个容器同时挂了 `.key_context` 与 `.track_focus`，三者是同一套键盘机制的三个部件）。

动作来源分三类：本 crate 定义（2 个）、从 `menu` crate 借用的通用「菜单导航」动作族（8 个）、从 editor / zed_actions / agent_ui / workspace 借用的语义动作（其余）。

#### 4.2.2 核心流程

```text
按键
 → gpui 收集上下文栈（窗口根 … 侧边栏容器 … 焦点元素）
 → keymap 按「深度优先、后加者优先」选出动作（4.3 节）
 → 动作沿焦点路径派发：
     捕获阶段（capture_action，如重命名编辑器的 Newline）
     → 目标元素
     → 冒泡阶段（on_action）：焦点元素 → … → 侧边栏根容器 → …
 → 根容器上 25 个 .on_action 之一命中（cx.listener 先 update 实体再调方法）
```

#### 4.2.3 源码精读

注册点全貌（根容器，紧跟 `.key_context` 与 `.track_focus` 之后）：

- [sidebar.rs:L7774-L7804](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7774-L7804) —— `v_flex().id("workspace-sidebar").key_context(...).track_focus(...)` 之后连续 25 行 `.on_action(cx.listener(...))`：24 个实体方法 + 1 个内联闭包（`OpenRecent`）。

逐项对照表（这是本讲的「家谱」，也是综合实践的参考答案）：

| 注册行 | 处理方法（定义行） | 动作全名 | 来源 | 行为一句话 |
| --- | --- | --- | --- | --- |
| [L7778](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7778) | `select_next`（[L3468](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3468)） | `menu::SelectNext` | menu | 选中下一行（到尾回卷） |
| [L7779](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7779) | `select_previous`（[L3480](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3480)） | `menu::SelectPrevious` | menu | 选中上一行 |
| [L7780](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7780) | `editor_move_down`（[L3445](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3445)） | `editor::MoveDown` | zed_actions | 转调 `select_next` 并把焦点收回列表 |
| [L7781](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7781) | `editor_move_up`（[L3452](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3452)） | `editor::MoveUp` | zed_actions | 转调 `select_previous` 并收回焦点 |
| [L7782](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7782) | `select_first`（[L3502](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3502)） | `menu::SelectFirst` | menu | 选中第一行 |
| [L7783](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7783) | `select_last`（[L3510](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3510)） | `menu::SelectLast` | menu | 选中最后一行 |
| [L7784](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7784) | `confirm`（[L3518](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3518)） | `menu::Confirm` | menu | 分组头→切换折叠；线程/终端→激活 |
| [L7785](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7785) | `expand_selected_entry`（[L4250](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4250)） | `menu::SelectChild` | menu | 展开选中分组或下移一行 |
| [L7786](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7786) | `collapse_selected_entry`（[L4274](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4274)） | `menu::SelectParent` | menu | 折叠分组/收起到父级 |
| [L7787](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7787) | `toggle_selected_fold`（[L4306](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4306)） | `editor::ToggleFold` | editor | 切换所在分组折叠 |
| [L7788](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7788) | `fold_all`（[L4341](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4341)） | `editor::FoldAll` | editor | 折叠全部分组 |
| [L7789](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7789) | `unfold_all`（[L4355](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4355)） | `editor::UnfoldAll` | editor | 展开全部分组 |
| [L7790](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7790) | `cancel`（[L3281](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3281)） | `menu::Cancel` | menu | 退出重命名/清空搜索/退回搜索框 |
| [L7791](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7791) | `archive_selected_thread`（[L5637](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5637)） | `agent::ArchiveSelectedThread` | agent_ui | 归档选中线程 |
| [L7792](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7792) | `rename_selected_thread`（[L5671](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5671)） | `agent::RenameSelectedThread` | agent_ui | 开始行内重命名 |
| [L7793](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7793) | `new_thread_in_group`（[L6611](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6611)） | `agents_sidebar::NewThreadInGroup` | **本 crate** | 在选中/活跃分组新建线程 |
| [L7794](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7794) | `new_terminal_thread`（[L6635](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6635)） | `agent::NewTerminalThread` | agent_ui | 新建终端线程 |
| [L7795](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7795) | `toggle_archive`（[L7519](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7519)） | `agents_sidebar::ToggleThreadHistory` | **本 crate** | 线程列表 ↔ 历史归档 |
| [L7796](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7796) | `focus_sidebar_filter`（[L3313](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3313)） | `agents_sidebar::FocusSidebarFilter` | zed_actions | 清空选中并聚焦搜索框 |
| [L7797](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7797) | `on_toggle_thread_switcher`（[L5865](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5865)） | `agents_sidebar::ToggleThreadSwitcher` | zed_actions | 呼出/关闭线程切换器 |
| [L7798](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7798) | `on_next_project`（[L7057](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7057)） | `multi_workspace::NextProject` | workspace | 激活下一项目分组 |
| [L7799](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7799) | `on_previous_project`（[L7061](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7061)） | `multi_workspace::PreviousProject` | workspace | 激活上一项目分组 |
| [L7800](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7800) | `on_next_thread`（[L7138](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7138)） | `multi_workspace::NextThread` | workspace | 按侧边栏顺序激活下一线程 |
| [L7801](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7801) | `on_previous_thread`（[L7142](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7142)） | `multi_workspace::PreviousThread` | workspace | 激活上一线程 |
| [L7802](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7802) | 内联闭包 | `projects::OpenRecent` | zed_actions | 切换最近项目弹窗 |

三个值得驻足的细节：

1. **借用的 `menu` 动作族**：[menu.rs:L12-L37](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/menu/src/menu.rs#L12-L37) 定义了 11 个 `menu::` 动作，侧边栏借用其中 8 个（`SelectNext/SelectPrevious/SelectFirst/SelectLast/Confirm/SelectChild/SelectParent/Cancel`，import 见 [sidebar.rs:L38-L40](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L38-L40)），未借用的 `SecondaryConfirm`、`Restart`、`EndSlot` 不参与。好处：任何为「菜单式列表」写的通用键位都能直接作用在侧边栏上。
2. **方向键双轨**：为什么除了 `menu::SelectNext` 还要挂 `editor::MoveDown`？看 [sidebar.rs:L3445-L3450](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3445-L3450)：`editor_move_down` 转调 `select_next` 后**把焦点交给列表自身**。搜索框（一个 Editor）聚焦时按方向键派发的是 `editor::MoveDown`，这条处理器让「在搜索框里按 ↓ → 列表选中下一行 → 焦点移出搜索框」一气呵成。
3. **嵌套注册与 `capture_action`**：根容器不是唯一注册点。行内重命名时，标题槽上的子容器注册了三个动作（[sidebar.rs:L6196-L6216](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6196-L6216)）：`editor::Newline` 用 `capture_action`——[div.rs:L1030-L1034](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/elements/div.rs#L1030-L1034) 说明它在正常派发之前截获，否则编辑器会自己消费回车（插入换行）；`menu::Confirm` 与 `editor::Cancel` 用普通 `on_action` 等冒泡到达。三者都汇到 `finish_thread_rename`。另见 [sidebar.rs:L6641](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6641) 的 `cx.stop_propagation()`：`new_terminal_thread` 处理完主动截停，防止同一个 `agent::NewTerminalThread` 冒泡到外层面板被二次处理。

#### 4.2.4 代码实践

**实践目标**：不依赖本讲表格，独立枚举侧边栏的全部动作注册点。

1. 操作步骤：在 `crates/sidebar` 下执行 `rg -n "\.on_action\(|\.capture_action\(" src/`；
2. 对每个命中行，跳转到对应的处理方法签名，读出它的第二个参数类型（如 `_: &Confirm`）；
3. 再沿文件头部的 `use` 语句确定该类型的来源模块，拼出动作全名。

**需要观察的现象**：除根容器 25 行外，还有重命名标题槽的 3 行嵌套注册（L6202、L6207、L6210）。

**预期结果**：与本讲 4.2.3 的表格一致（含 `OpenRecent` 闭包一项）。若发现多出的注册点，说明源码已演进，请以本地源码为准。

#### 4.2.5 小练习与答案

**练习 1**：侧边栏为什么复用 `menu::SelectNext` 而不自己定义 `agents_sidebar::SelectNext`？

**参考答案**：复用让侧边栏免费接入所有为 `menu` 上下文写的通用绑定（见 4.3 节 `add("menu")` 与 [default-macos.json:L48-L54](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/assets/keymaps/default-macos.json#L48-L54) 的 left/right 绑定），也让「列表类 UI 的方向键行为一致」这一产品约定由一处键位承载。只有侧边栏独有的语义（如在分组里新建线程）才值得新造动作。

**练习 2**：`editor::MoveDown` 与 `editor::ToggleFold` 的全名前缀相同，但来自不同 crate。请指出各自的定义处。

**参考答案**：`MoveDown` 定义在 [zed_actions/src/lib.rs:L229-L242](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/zed_actions/src/lib.rs#L229-L242) 的 `editor` 模块（命名空间 `editor`）；`ToggleFold` 等折叠动作定义在 editor crate 的 `actions` 模块（侧边栏以 `editor::actions::ToggleFold` 路径引用，见 [sidebar.rs:L4306-L4311](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4306-L4311)）。命名空间是字符串前缀，crate 是物理边界，两者只是约定对齐。

**练习 3**：如果删掉根容器上的 `.on_action(cx.listener(Self::cancel))`，按 Escape 会发生什么？

**参考答案**：`menu::Cancel` 没有处理器消费，动作沿焦点路径继续冒泡，交给外层（如工作区/面板）的 Escape 处理；侧边栏的「清空搜索词 → 退出重命名 → 退回搜索框」阶梯（[sidebar.rs:L3281-L3311](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3281-L3311)）整体失效。

### 4.3 dispatch_context：一次渲染期的焦点快照

#### 4.3.1 概念说明

`KeyContext` 是键位匹配的「词表」：一组标识符（如 `ThreadsSidebar`、`menu`）和键值对（如 `os = macos`）。绑定 JSON 里的 `"context": "ThreadsSidebar && not_searching"` 就是在问：「当前焦点路径上，是否有一层的词表同时含这两个标识符？」

侧边栏的词表不是静态的——它每次 `render()` 都由 `dispatch_context` **现场重算**，因为词表的第三个维度（searching / editing / not_searching）取决于「此刻焦点在哪」，而焦点变化会触发重渲染，词表随之刷新。这又是一次「不存中间状态、每次从世界现推」哲学（u3-l2）的体现。

#### 4.3.2 核心流程

```text
dispatch_context(window, cx):
    ctx = KeyContext::new_with_defaults()      # 自动带 os = macos|linux|windows
    ctx.add("ThreadsSidebar")                  # 本体标识符：keymap 的锚点
    ctx.add("menu")                            # 认领 menu 动作族的通用绑定
    if 搜索框聚焦(列表视图或归档视图): identifier = "searching"
    else if 重命名编辑器聚焦:           identifier = "editing"
    else:                               identifier = "not_searching"
    ctx.add(identifier)
    return ctx                                 # 挂到根容器 div 上
```

这个三层词表 `os / ThreadsSidebar+menu / searching|editing|not_searching` 与根容器的 `.key_context(...)`（[sidebar.rs:L7776](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7776)）配合：`.key_context` 把词表挂到带 `id` 的元素上（[div.rs:L792-L803](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/elements/div.rs#L792-L803)），`.track_focus` 声明「焦点可停在我这棵子树里」——只有焦点进入子树，这层词表才会出现在上下文栈上。

#### 4.3.3 源码精读

- [sidebar.rs:L3240-L3264](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3240-L3264) —— `dispatch_context` 全文。先 `new_with_defaults`（[context.rs:L29-L47](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/keymap/context.rs#L29-L47)，按编译目标写入 `os` 键值对），再依次 `add` 三个维度。`add` 有去重语义（[context.rs:L116-L123](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/keymap/context.rs#L116-L123)）。三态判定的依据是三个 `is_focused` 现查：过滤编辑器（列表视图或归档视图的搜索框，L3245、L3252）与重命名编辑器（L3247-L3250）。
- **为什么 `add("menu")`**：keymap 里存在面向所有菜单式列表的通用块 [default-macos.json:L48-L54](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/assets/keymaps/default-macos.json#L48-L54)（`"context": "menu"` 把 left/right 绑到 `menu::SelectParent`/`menu::SelectChild`）。侧边栏把自己的词表加上 `menu`，就自动继承这类通用绑定，不必逐键重写。
- **侧边栏专属绑定**：[default-macos.json:L788-L803](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/assets/keymaps/default-macos.json#L788-L803) 是 `"context": "ThreadsSidebar"` 块——`cmd-n` → `NewThreadInGroup`、`enter` → `menu::Confirm`、`ctrl-tab` → `ToggleThreadSwitcher` 等；[L804-L811](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/assets/keymaps/default-macos.json#L804-L811) 是更窄的 `"ThreadsSidebar && not_searching"` 块——`space` 也当 `Confirm`、`shift-r` 触发重命名。`not_searching` 的含义：焦点在搜索框里时，space 要用来输入空格，绝不能当「确认」。
- **优先级规则**：[keymap.rs:L150-L164](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/keymap.rs#L150-L164) 写明：绑定按匹配**深度**排序（Editor 层胜过 Pane 层、胜过 Workspace 层），同深度后加入者优先——用户 keymap 在默认 keymap 之后加载，所以用户绑定天然覆盖默认绑定。`&&` 中各项要落在同一层词表里匹配（[context.rs:L259-L268](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/keymap/context.rs#L259-L268) 的 `depth_of` 从最深前缀向下尝试），而 `>` 运算符表达「父层 > 子层」的跨层关系。

#### 4.3.4 代码实践

**实践目标**：用「上下文栈」推演两组按键的行为差异（纯源码推理练习）。

1. 操作步骤：先读 [dispatch_context](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3240-L3264)，再回答：
   - 焦点在**搜索框**里时按 `space`，会发生什么？
   - 焦点在**列表**（`not_searching`）时按 `space`，会发生什么？
   - 焦点在**重命名编辑器**里时，词表是哪三个标识符？此时 `space` 的行为又如何？
2. 需要观察的现象（推理依据）：三种情形下 `identifier` 分别取 `searching` / `not_searching` / `editing`，其中只有 `not_searching` 能让 [L804-L811](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/assets/keymaps/default-macos.json#L804-L811) 的 `space` 绑定匹配成功；而搜索框与重命名编辑器各自是更深的 `Editor` 层，普通字符键会先被编辑器消费。
3. 预期结果：搜索框中 `space` 输入空格（列表同时被过滤刷新，承接 u5-l3 将讲的链路）；列表中 `space` 派发 `menu::Confirm`（展开/折叠分组或激活线程）；重命名编辑器中 `space` 输入空格，回车走 [L6202-L6206](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6202-L6206) 的 `capture_action` 提交重命名。具体手感「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`dispatch_context` 为什么挂在 render 里每帧现算，而不是构造时算一次存起来？

**参考答案**：词表的第三维取决于「此刻焦点在哪个编辑器」，焦点随时变化；gpui 的机制是状态变化 → `cx.notify()` → 重新 render → 新词表。存一份旧词表就引入了「增量协调状态」，违反 u3-l2 讲过的全量重推导约束。

**练习 2**：`"context": "ThreadsSidebar && not_searching"` 与 `"context": "ThreadsSidebar"` 两个块中同按键的绑定，哪个优先？

**参考答案**：两者都在侧边栏这一层词表上匹配（`&&` 各项须同层），深度相同；同为默认 keymap 时按加载顺序、条件更具体的块并无机制性加成——真正保证行为正确的是两个块的按键集合不重叠（`space`、`shift-r` 只出现在 `not_searching` 块）。用户 keymap 因为加载在默认之后，同深度时覆盖默认。

**练习 3**：`ThreadSwitcher` 模态打开时有自己独立的上下文块（[default-macos.json:L812-L818](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/assets/keymaps/default-macos.json#L812-L818)）。为什么 `ctrl-tab` 在两处都要绑定？

**参考答案**：模态打开后焦点移入切换器，上下文栈的最深层变成 `ThreadSwitcher` 而非 `ThreadsSidebar`；若只在侧边栏块绑定，按住 ctrl 再按 tab 的「连续循环」就会在模态里断掉。两处各绑一次，保证呼出与继续循环都有效（详见 u7-l1）。

### 4.4 focus_in 与 prepare_for_focus：切入侧边栏时的选中态清理

#### 4.4.1 概念说明

「用户进入侧边栏」有两条不同的路径，分别由两个函数把关，都在清理 `selection`：

- **焦点落入**：焦点移到侧边栏自身的 focus_handle 上（比如从搜索框按 Escape 退回、或代码里调用 `focus`）→ 构造期注册的 **`focus_in`** 回调触发，它决定焦点最终停在哪个子元素上；
- **外部切入**：用户按 `multi_workspace::ToggleWorkspaceSidebar` / `FocusWorkspaceSidebar` 从编辑器等区域进入 → 宿主 MultiWorkspace 先调用侧边栏的 **`prepare_for_focus`**，再执行聚焦。

两者分工：`prepare_for_focus` 负责「切入前清场」（把残留的键盘选中态抹掉），`focus_in` 负责「切入后引导」（没有选中就把焦点引到搜索框——侧边栏的产品默认是「打开即搜索」）。

#### 4.4.2 核心流程

```text
外部切入（以 toggle_sidebar 为例，multi_workspace.rs:L436-L451）:
    toggle_sidebar()
      → prepare_for_focus()      # Sidebar：selection = None; cx.notify()
      → sidebar.focus(window)    # 聚焦侧边栏句柄
      → 焦点进入 → focus_in() 被回调触发
          若聚焦的恰是侧边栏自身句柄:
              归档视图且无选中   → 聚焦归档视图的过滤编辑器
              列表视图且无选中   → 聚焦 filter_editor（搜索框）

焦点落入（无外部切入，例如 Escape 从搜索框退回）:
    focus_handle 获得焦点 → focus_in()
          selection 为 Some → 不动（保留键盘选中态）
```

#### 4.4.3 源码精读

- **注册点**：[sidebar.rs:L800-L802](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L800-L802) —— `Sidebar::new` 里 `cx.on_focus_in(&focus_handle, window, Self::focus_in)`（[window.rs:L4908-L4918](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/window.rs#L4908-L4918) 的 `on_focus_in` 在焦点进入给定句柄时触发回调），订阅照例 `.detach()`（u1-l3 的约定：构造期订阅一律分离）。
- **`focus_in` 本体**：[sidebar.rs:L3266-L3279](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3266-L3279) —— 第一道守卫 `!self.focus_handle.is_focused(window)` 直接返回：只有聚焦的**恰好是侧边栏自身句柄**（而不是搜索框、重命名编辑器等子元素）才继续。之后分两个视图：归档视图下若归档列表没有选中，把焦点交给它的过滤编辑器；线程列表视图下若 `selection` 为 `None`，把焦点交给 `filter_editor`。注意它**不修改** `selection`——只做焦点引导。
- **`prepare_for_focus` 本体**：[sidebar.rs:L7699-L7702](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7699-L7702) —— 只有两行：清空 `selection`、`cx.notify()`。它是 workspace crate 定义的 `Sidebar` trait 的方法，契约写在 trait 的 doc comment 里：「Makes focus reset back to the search editor upon toggling the sidebar from outside」（[multi_workspace.rs:L131-L132](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L131-L132)）。
- **调用方**：宿主在两处三个分支调用它——`toggle_sidebar`（[multi_workspace.rs:L436-L451](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L436-L451)，侧边栏从关到开的分支）与 `focus_sidebar`（[L463-L491](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L463-L491)，侧边栏已开但焦点在外、以及从关到开两个分支）。每次都是「先 `prepare_for_focus` 清场，再 `focus` 聚焦」的固定顺序，随后 `focus_in` 在焦点回调里接力。宿主经由对象安全的 `SidebarHandle` 转发（trait 定义 [L163-L181](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L163-L181)，实现 [L210-L212](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L210-L212)），因为宿主持有的是 `Box<dyn SidebarHandle>`（u1-l1 讲过的装配方式）。
- **为什么必须清理**：承接 u2-l3——`selection` 是键盘焦点下标，只在「侧边栏持有焦点」期间有意义。用户离开侧边栏去编辑器干活，列表上残留的高亮选中既无意义又会在下次切入时误导（一按方向键就「跳」到旧位置）。所以切入前一律归零，配合 `focus_in` 把默认焦点放到搜索框。

#### 4.4.4 代码实践

**实践目标**：跑通「外部切入 → 清场 → 引导」的完整链路（源码跟踪 + 本地观察）。

1. 操作步骤（源码跟踪）：
   - 从 `multi_workspace::FocusWorkspaceSidebar` 动作开始，找到 MultiWorkspace 渲染树上的 `.on_action` 处理器，确认它调用 `focus_sidebar`；
   - 沿 `focus_sidebar` → `sidebar.prepare_for_focus`（经 `SidebarHandle` 转发）→ `sidebar.focus` → `focus_in` 画出调用时序；
   - 标注每一步落在哪个文件哪一行。
2. 操作步骤（本地观察，可选）：
   - 运行 `cargo run -p zed` 打开 Zed，展开侧边栏并用方向键选中一行；
   - 点击编辑器把焦点移走，再按 ToggleWorkspaceSidebar 切回侧边栏；
   - 观察之前的选中高亮是否消失、光标是否落在搜索框。
3. 需要观察的现象：切入后列表无选中高亮，搜索框获得焦点（placeholder「Search threads…」可见光标）。
4. 预期结果：与 4.4.3 的链路推断一致；本地手感「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`prepare_for_focus` 为什么要定义在 workspace crate 的 `Sidebar` trait 上，而不是侧边栏自己监听某个事件？

**参考答案**：因为调用时机由**宿主**决定——「切入前清场」必须发生在宿主聚焦侧边栏**之前**，这是一个由外向内的主动调用，不是侧边栏能自我感知的事件。放在 trait 上（[multi_workspace.rs:L122-L161](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L122-L161)）让宿主对「任意侧边栏实现」（现在只有线程侧边栏，将来可能有别的）都能用统一节奏编排。

**练习 2**：`focus_in` 开头的 `if !self.focus_handle.is_focused(window) { return; }` 防的是什么？

**参考答案**：`on_focus_in` 的回调在焦点进入该句柄（含从子树外移入）时触发，但触发时真正聚焦的可能是搜索框等**子元素**。守卫确保只有「聚焦的是侧边栏自身句柄」时才执行引导逻辑，避免焦点在子元素之间正常移动时被错误地改道。

**练习 3**：`prepare_for_focus` 清空 `selection` 后为什么要 `cx.notify()`？

**参考答案**：`selection` 参与行渲染（键盘选中的边框高亮，见 u4-l3 的 `focused` 参数）。状态变了必须把实体标记为脏，gpui 才会重新 render，高亮才会消失——这是 gpui「状态变更 → notify → 重投影」的基本纪律（u4-l1）。

## 5. 综合实践

把本讲三块知识串成一个任务：**给 `NewThreadInGroup` 换一个自己的快捷键，并解释它为什么只在特定焦点状态下生效**。

**第一步：枚举动作注册表（对应 4.2）**

在 `crates/sidebar` 下执行：

```bash
rg -n "\.on_action\(cx\.listener" src/sidebar.rs
```

把输出与 4.2.3 的 25 行表格对照；再对其中任意 3 个不熟悉的方法打开定义处，读出参数里的动作类型并拼出全名（用文件头部 [L38-L76](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L38-L76) 的 `use` 判断来源 crate）。

**第二步：推断 menu:: 动作的委派（对应 4.3）**

回答：keymap 里 `"context": "menu"` 的通用绑定（left/right → `menu::SelectParent`/`SelectChild`，[default-macos.json:L48-L54](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/assets/keymaps/default-macos.json#L48-L54)）为什么会作用到侧边栏？哪些 `menu::` 动作在侧边栏有处理器、哪些没有？

参考答案：因为 [dispatch_context](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3240-L3264) 给词表 `add("menu")`；有处理器的是 8 个：`SelectNext`、`SelectPrevious`、`SelectFirst`、`SelectLast`、`Confirm`、`SelectChild`、`SelectParent`、`Cancel`（见 4.2.3 表格与 [sidebar.rs:L38-L40](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L38-L40) 的 import）；`SecondaryConfirm`、`Restart`、`EndSlot` 没有注册，即便绑了键也会原样冒泡。

**第三步：写一条自己的键位绑定并触发（对应 4.1 + 4.3）**

1. 运行 `cargo run -p zed` 启动本地构建的 Zed，执行命令面板里的 `zed: open keymap`（或直接编辑 `~/.config/zed/keymap.json`）；
2. 加入以下代码（示例配置，故意选一个与默认 `cmd-n` 不冲突的键；Windows/Linux 上请把 `ctrl-alt-n` 换成你的平台可用组合）：

   ```json
   [
     {
       "context": "ThreadsSidebar",
       "bindings": {
         "ctrl-alt-n": "agents_sidebar::NewThreadInGroup"
       }
     }
   ]
   ```

3. 展开侧边栏（ToggleWorkspaceSidebar），先用方向键在某个项目分组头上制造一个 `selection`，再按 `ctrl-alt-n`；
4. 观察要点：新线程是否创建在「选中的分组」里、该分组是否被自动展开、`selection` 是否被清空——这三点正是 [new_thread_in_group](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6611-L6633) 的行为（`selected_group_key` 优先，展开分组，`selection = None`）；没有选中时则落到活跃工作区分组（L6630-L6632）；
5. 对照实验：把焦点移到编辑器（侧边栏外）再按同一个键，确认绑定**不**生效——因为上下文栈里已没有 `ThreadsSidebar` 层；
6. 验证完删除或保留该绑定均可。

预期结果：第 4 步三个观察点全部命中、第 5 步不触发，即键位—上下文—处理器三层链路打通。按键组合在不同平台/窗口管理器下的可用性「待本地验证」。

## 6. 本讲小结

- 动作是键盘交互的货币：`gpui::actions!` 生成带命名空间的单位动作类型，keymap JSON 用 `命名空间::Name` 引用；命名空间跨 crate 共享，全名重复会在 `App` 创建时 panic。
- 侧边栏把 25 个处理器集中在 render() 根容器上：2 个本 crate 动作、8 个借用的 `menu::` 导航族，其余借自 editor / zed_actions / agent_ui / workspace；嵌套元素（重命名标题槽）用 `capture_action` 在编辑器消费回车之前截获动作。
- `dispatch_context` 每帧现算三层词表：`os`（平台）+ `ThreadsSidebar` 与 `menu`（身份与通用绑定入口）+ `searching | editing | not_searching`（焦点三态）；绑定按上下文栈深度取优先级，深层（焦点侧）与后加载（用户 keymap）胜出。
- `add("menu")` 是复用键位的关键：为所有菜单式列表写的通用绑定（left/right）自动对侧边栏生效。
- 进入侧边栏的两条清理路径：外部切入时宿主先调 `prepare_for_focus` 清空 `selection` 再聚焦；焦点落到侧边栏自身句柄时 `focus_in` 把默认焦点引导到搜索框——「打开即搜索」且不残留旧选中态。

## 7. 下一步学习建议

- 下一讲 **u5-l2 键盘导航与确认**：深入 `select_next` / `select_previous` 的下标推进与钳制规则、`confirm` 在三种行类型上的分流、展开/折叠导航中「收起到父级」的选择迁移——本讲的动作表正是那张地图的入口。
- 顺带阅读 [assets/keymaps/vim.json](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/assets/keymaps/vim.json#L1238-L1260) 中 `ThreadsSidebar` 相关的两个块，看模态编辑模式如何用 `>` 跨层谓词叠加在侧边栏上下文之上。
- 若想彻底吃透派发机制，可接着读 [gpui/src/keymap.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/keymap.rs#L150-L164) 的优先级实现与 [gpui/src/keymap/context.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/keymap/context.rs#L220-L248) 的谓词语法文档（`&&`、`||`、`!`、`>`、`==`）。
