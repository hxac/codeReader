# 菜单、工作区标签与默认分支预取（u4-l4）

## 1. 本讲目标

学完本讲，你应该能够：

1. 推演 `workspace_menu_worktree_labels` 在任意工作区布局（单根、多根、含 linked worktree、无 git 仓库）下产出的菜单标签。
2. 读懂项目分组头尾部两颗按钮的完整行为：「+」按钮的 `New Thread In…` 菜单与 `Create New Worktree…` 子菜单，以及「⋯」省略号菜单的全部条目与出现条件。
3. 说清 `DefaultBranchCache` 三种状态（缺失、`Pending`、`Resolved`）的迁移时机，以及为什么要在 `update_entries` 里预热它。
4. 理解侧边栏如何「不自己造轮子」：创建 git worktree 委托给 `git_ui_core::worktree_service`，建立远程连接委托给 `remote_connection`，打开工作区委托给 `MultiWorkspace::find_or_create_workspace`。

本讲是渲染层（单元四）的收官：u4-l1 看整体骨架，u4-l2 看分组头与粘性头部，u4-l3 看线程行/终端行，本讲看挂在分组头尾部的那两颗按钮，以及它们背后跨越 crate 边界的调用。

## 2. 前置知识

- **工作区（Workspace）与项目分组（ProjectGroupKey）**：一个 `MultiWorkspace` 窗口里可以同时打开多个项目；`ProjectGroupKey`（主 worktree 路径 + 远程主机）把「同一个仓库的所有工作区」归成一个分组，侧边栏每个分组渲染一个分组头（见 u2-l2）。
- **git worktree**：一个 git 仓库可以同时检出多个工作目录（主 worktree + 若干 linked worktree），各自停在不同分支上。Zed 把每个 worktree 当作一个可打开的工作区，`git worktree list` 会把主检出称为 `main`。
- **远程跟踪分支**：形如 `origin/main` 的分支引用。git 底层返回的可能是 `refs/remotes/origin/main`，`RemoteBranchName::parse` 负责剥掉前缀并按第一个 `/` 拆成 remote 名（`origin`）与分支名（`main`）。
- **菜单的三种构建件**（ui crate）：
  - `PopoverMenu`：挂在按钮上的弹出菜单容器，通过 `PopoverMenuHandle` 可以在代码里开关它；
  - `ContextMenu`：条目式菜单构建器，`ContextMenu::build` 在每次确认后自动关闭，而 `ContextMenu::build_persistent` 构建的菜单在确认后**保持打开**，适合「连续操作」类菜单；
  - `menu::SecondaryConfirm`：菜单的「次级确认」动作，配合条目右侧的修饰键提示（如 ⌥-click）使用。
- **同步渲染与异步 I/O 的矛盾**：菜单内容在打开瞬间（一个同步的 `menu(...)` 闭包）就要拼出完整条目；而「远程默认分支是什么」需要一次异步 git 查询。本讲的 `DefaultBranchCache` 就是解决这个矛盾的手段——把 I/O 挪到菜单路径之外。

另外请回忆 u3-l2 建立的架构约束：侧边栏的一切可见状态都由 `update_entries` → `rebuild_contents` 从当前世界状态全量重推导。本讲的预取缓存是这条教义下的一个有趣的「边界案例」，我们会在 4.3 讨论它为什么成立。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `crates/sidebar/src/sidebar.rs` | 本讲主战场：标签函数、两颗按钮的渲染、预取逻辑、两个委托适配器全部在此 |
| `crates/git_ui_core/src/worktree_service.rs` | 提供 `RemoteBranchName`、`worktree_create_targets`（选项推导）与 `handle_create_worktree`（真正建 worktree） |
| `crates/zed_actions/src/lib.rs` | 定义 `CreateWorktree` 动作与 `NewWorktreeBranchTarget` 枚举，是 sidebar 与 git_ui_core 之间的公共词汇 |
| `crates/workspace/src/multi_workspace.rs` | 宿主：`workspaces_for_project_group`、分组排序/移除、`find_or_create_workspace`（接收 `connect_remote` 回调） |
| `crates/workspace/src/workspace.rs` | `Workspace::root_paths`：一个工作区的全部根路径 |
| `crates/project/src/git_store.rs` | `Repository::default_branch`（异步 git 查询）与 `linked_worktree_short_name`（linked worktree 短名推导） |

## 4. 核心概念与源码讲解

### 4.1 workspace_menu_worktree_labels：一个工作区在菜单里叫什么

#### 4.1.1 概念说明

侧边栏的菜单经常需要列出「这个分组里的每个工作区」——比如 `New Thread In…` 菜单要让你选在哪个工作区里新建线程，`Open Worktrees` 一节要让你切换/关闭某个工作区。此时每个工作区需要一个简短、可区分的名字。

难点在于：

- 一个工作区可能有**多个根路径**（多根工作区），每个根都要单独展示；
- 一个根可能是 git worktree（主检出或 linked 检出），也可能只是个普通文件夹；
- 多根时必须显示文件夹名才能区分，单根时文件夹名是冗余的。

`workspace_menu_worktree_labels` 是一个**自由函数**（不挂在 `Sidebar` 上，因为它只依赖工作区自身状态，可以在任意 `&App` 上下文里调用），输入一个工作区，输出它的每个根路径的标签 `Vec<WorkspaceMenuWorktreeLabel>`。

#### 4.1.2 核心流程

```
取 root_paths（工作区全部根路径）
show_folder_name = 根路径数 > 1
一次性收集 project 所有 repository 的快照
对每个 root_path：
    folder_name = 路径最后一段
    在快照里找 work_directory_abs_path == root_path 的那个仓库
    ├─ 找到（是 git worktree）：
    │     worktree_name = linked ? linked_worktree_short_name(主仓路径, 本路径) : "main"
    │     show_folder_name ?
    │       → 主名 = folder_name，次名 = worktree_name（渲染为 "zed / main"）
    │       否则 → 主名 = worktree_name（渲染为 "main"），无次名
    │     icon = GitWorktree
    └─ 没找到（普通文件夹）：
          主名 = folder_name，无次名，无 icon
```

`linked_worktree_short_name` 的推导规则（见源码精读第 3 条）：linked 目录名 ≠ 仓库名时直接用目录名（如 `zed-review-x`）；撞名时（两个都叫 `zed`）退而取**父目录**名（如 `/tmp/zed` → `tmp`）。

#### 4.1.3 源码精读

1. **标签的数据结构与渲染**：`WorkspaceMenuWorktreeLabel` 只有三个字段——可选图标、主名、可选次名。`render()` 把它们拼成一行：图标（弱化色）+ 主名（超长截断）+ 可选的 `/ 分隔 + 次名`。见 [sidebar.rs:579-600](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L579-L600)。

2. **主函数**：先取 `root_paths` 并算出 `show_folder_name`，再一次性收集仓库快照（避免在循环里反复 `read(cx)`）；随后对每个根路径做「找快照 → 分支命名」。见 [sidebar.rs:602-662](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L602-L662)。注意匹配方式是拿根路径与每个快照的 `work_directory_abs_path` 做**全等比较**，不做前缀或归一化匹配。

3. **linked worktree 短名**：`project::linked_worktree_short_name` 定义在 git_store。主仓路径与 linked 路径相同返回 `None`；否则优先用 linked 的目录名，目录名与主仓项目名撞车时用父目录名兜底。见 [git_store.rs:10627-10647](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/project/src/git_store.rs#L10627-L10647)，在 [sidebar.rs:629-638](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L629-L638) 被调用，推不出短名时回落到 `folder_name`。

4. **`Workspace::root_paths`**：返回工作区全部根路径（多根工作区有多个），是整个函数的输入。见 [workspace.rs:7136](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/workspace.rs#L7136)。

一个容易惊讶的细节：**单根 git 工作区的标签是 `"main"` 而不是仓库名**。这是刻意的工作区中心（worktree-centric）命名——与 `git worktree list` 把主检出称为 main 的习惯一致；只有多根时才把文件夹名放到主位（`zed / main`）、单根无仓库时才直接显示文件夹名。

#### 4.1.4 代码实践

1. **实践目标**：能对任意目录布局手工推出菜单标签。
2. **操作步骤**：
   - 假设一个工作区有两个根：`/dev/zed`（zed 主仓的主 worktree）和 `/dev/zed-review-x`（linked worktree，目录名与仓库名不同）；
   - 再假设另一个工作区单根 `/notes`（无 git 仓库）；
   - 按 4.1.2 的流程分别推演两个工作区的标签列表。
3. **需要观察的现象**：对照 [sidebar.rs:616-660](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L616-L660) 逐行核对：`show_folder_name` 的取值如何改变主/次名的分配、无快照时 icon 为何是 `None`。
4. **预期结果**：第一个工作区（两根，都在 git 下）→ `[zed / main, zed / zed-review-x]`；若把它改成单根 `/dev/zed` → `[main]`；第二个工作区 → `[notes]`（无图标）。若把 linked worktree 改成 `/tmp/zed`（目录名撞车）→ 短名退为 `tmp`。**待本地验证**（可在本地打开对应布局后查看分组头菜单确认）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `repository_snapshots` 要在循环外一次性收集，而不是每个根路径现查？

**答案**：每个根的标签计算只需要读快照的只读信息（`work_directory_abs_path`、`is_linked_worktree`、`main_worktree_abs_path`）。循环外收集一次快照向量，循环内只做内存查找，避免在 `map` 闭包里反复走 `project.read(cx)` / `repo.read(cx)` 的实体读取；这也让函数保持纯读（`&App`），不产生任何订阅或通知。

**练习 2**：一个工作区有三个根，其中两个是 git worktree、一个是普通文件夹，`show_folder_name` 是什么？普通文件夹那一项的次名是什么？

**答案**：`root_paths.len() == 3 > 1`，所以 `show_folder_name = true`；git 项渲染为 `文件夹名 / worktree名`。普通文件夹那一项走 else 分支，永远只有主名（文件夹名）、次名为 `None`、无图标——「/ 次名」只对 git worktree 有意义。

**练习 3**：主 worktree（非 linked）在多根布局下的 `worktree_name` 是什么？这个字符串来自哪里？

**答案**：是字面量 `"main"`（[sidebar.rs:637](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L637)），是 UI 写死的展示词，不是从 git 查询来的；linked 分支才会去问 `linked_worktree_short_name`。

### 4.2 分组头尾部的两颗按钮：render_new_thread_button 与 render_project_header_ellipsis_menu

#### 4.2.1 概念说明

u4-l2 讲过 `render_project_header` 的左半区（分组名、状态徽标、折叠箭头）。本讲看它的右半区：两个悬停时才浮现的图标按钮——「+」（新建线程）和「⋯」（更多操作）。挂接点在 [sidebar.rs:2414-2432](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2414-L2432)。

两者的共同骨架：

- 都是 `PopoverMenu`（弹出菜单），触发按钮在菜单未展开时 `visible_on_hover`（悬停分组头才出现），展开期间保持可见；
- 弹出句柄都存在按下标索引的 `HashMap` 字段里（[sidebar.rs:775-778](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L775-L778)），这样**右键分组头也能开关省略号菜单**（[sidebar.rs:2433-2443](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2433-L2443) 直接 `menu_handle.toggle`）；
- 菜单内容全部在**打开时刻现算**（`menu(...)` 闭包每次展开都会执行），不缓存条目——这与「全量重推导」的教义一脉相承。

两者的关键差异：「+」按钮先按「组内有无打开的工作区」分流，空组直接点击建线程、不弹菜单；「⋯」菜单用 `build_persistent` 构建（确认后不关闭）。

#### 4.2.2 核心流程

**「+」按钮（`render_new_thread_button`）**：

```
取组内打开的工作区 open_workspaces
├─ 空：渲染纯 IconButton
│    点击 → set_group_expanded(true) → selection = None
│         → workspace_for_group 有结果？ create_new_entry
│           否则 open_workspace_and_create_entry(LastCreatedKind)
└─ 非空：渲染 PopoverMenu，菜单内容：
     header("New Thread In…")
     每个打开的工作区一条 custom_entry：
        行内容 = 该工作区的 labels（4.1）用 "•" 连接；活跃工作区打 ✓
        点击 → set_group_expanded(true) → selection = None → create_new_entry(&workspace)
     base_workspace = 活跃工作区（若属于本组）否则第一个
     creation_blocked = 无 base 或 (is_via_collab 或 仓库列表为空)
     若 !creation_blocked：separator + submenu("Create New Worktree…")
        选项 = worktree_create_targets(多仓库?, 默认分支缓存, 当前分支)
        每个选项 → 条目 "Based on {branch_label}" → create_worktree_in_workspace
```

**「⋯」按钮（`render_project_header_ellipsis_menu`）**：

```
on_open：project_header_menu_ix = Some(ix)（记录展开行，关闭时清零）
菜单（build_persistent + 末尾槽位挂 menu::SecondaryConfirm）：
  1. "Open Project in New Window"   —— 仅当本地分组且分组数 ≥ 2
  2. "Focus Project" / "Focus Last Project"（带 ⌥-click 提示）—— 活跃时禁用
  3. 分隔线 + "Open Worktrees" 头 + 每个打开的工作区一行（✓ 或悬停 ✕）
  4. 分隔线 + "Move Up" / "Move Down" —— 仅当分组数 ≥ 2，边界处禁用
  5. 分隔线 + "Remove"
Dismiss 事件 → project_header_menu_ix = None
```

#### 4.2.3 源码精读

1. **「+」按钮的空组分支**：没有打开的工作区时只是一个带提示的按钮，点击后先确保分组展开、清空键盘选中，再「有工作区就建、没有就先开工作区再建」。见 [sidebar.rs:2505-2532](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2505-L2532)。`open_workspace_and_create_entry` 的内部走 `MultiWorkspace::find_or_create_workspace`（u6-l3 会展开）。

2. **`New Thread In…` 主体**：菜单闭包里现算两个东西——组内打开的工作区列表，和每个工作区的标签（复用 4.1 的函数，见 [sidebar.rs:2564-2567](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2564-L2567)）。每个工作区一条 `custom_entry`：渲染函数负责行外观（labels 用 `•` 连接、活跃工作区打 ✓），处理器负责副作用（展开分组、清空 selection、`create_new_entry`）。见 [sidebar.rs:2573-2621](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2573-L2621)。

3. **`Create New Worktree…` 子菜单的门槛**：`base_workspace` 优先取「当前活跃工作区（且属于本组）」，否则取组内第一个；`creation_blocked` 判定 `is_via_collab()`（访客会话）或仓库列表为空——注释明确说明这是镜像 worktree picker 的 `creation_blocked_reason`，否则子菜单会展开成空的。见 [sidebar.rs:2623-2637](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2623-L2637)。

4. **子菜单的选项与缓存读取**：子菜单闭包同步读取三个输入——是否多仓库、当前活跃分支名、以及从 `worktree_default_branches` 缓存取默认分支。注意读取处的匹配：**只有 `Resolved` 才解包，缺失或 `Pending` 一律当 `None`**（[sidebar.rs:2652-2662](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2652-L2662)）。选项由 `worktree_create_targets` 推导（4.4 详解），每个选项渲染成 `"Based on origin/main"` 这样的条目，点击调 `create_worktree_in_workspace`。见 [sidebar.rs:2664-2687](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2664-L2687)。

5. **省略号菜单的骨架**：触发按钮是 `IconButton(Ellipsis)`，未展开时悬停可见（展开时靠 `is_deployed()` 保持可见，见 [sidebar.rs:2793-2808](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2793-L2808)）；`on_open` 把 `project_header_menu_ix` 记为当前行（[sidebar.rs:2809-2818](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2809-L2818)），菜单的 `DismissEvent` 订阅再把它清零（[sidebar.rs:3120-3130](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3120-L3130)）。菜单闭包先现算：组内打开的工作区、本组在分组序列中的位置（用于 Move Up/Down 的可用性，注释强调「在打开时刻计算以反映最新分组顺序」）、活跃工作区与标签。见 [sidebar.rs:2824-2855](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2824-L2855)。

6. **`build_persistent` 与次级确认**：菜单用 `ContextMenu::build_persistent` 构建（确认后不关闭），并把 `menu::SecondaryConfirm` 挂到末尾槽位（end slot）——这是「Focus Project ⌥-click」这类次级交互的载体。见 [sidebar.rs:2857-2859](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2857-L2859)。

7. **五个条目区块**：
   - 「Open Project in New Window」：仅当 `project_group_key.host().is_none()`（本地分组）且 `project_group_keys().len() >= 2` 时出现（条件在 [sidebar.rs:2785-2789](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2785-L2789) 计算），挂 `workspace::MoveProjectToNewWindow` 动作，处理器调 `MultiWorkspace::open_project_group_in_new_window`（[sidebar.rs:2862-2884](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2862-L2884)）。
   - 「Focus Project / Focus Last Project」：`custom_entry`，标题取决于组内有无线程；右侧渲染次键修饰提示；`is_active` 时禁用（`.selectable(!is_active)` 且处理器直接 return）；处理器分流「有打开工作区 → 激活；没有 → `open_workspace_for_group`」，并清空 selection 与 active_entry（[sidebar.rs:2886-2945](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2886-L2945)）。
   - 「Open Worktrees」：组内每个打开的工作区一行（标签复用 4.1）；活跃工作区显示 ✓，非活跃的悬停显示 ✕ 关闭按钮——✕ 调 `MultiWorkspace::remove([workspace], RemovalIntent::CloseProject)` 并手动向菜单发 `DismissEvent` 收起；行点击则 `multi_workspace.activate(...)` 并收起菜单（[sidebar.rs:2947-3063](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2947-L3063)）。
   - 「Move Up / Move Down」：仅当分组数 ≥ 2；挂从 workspace crate 导入的 `MoveProjectUp` / `MoveProjectDown` 动作（[sidebar.rs:65-70](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L65-L70)），到边界时禁用；处理器调 `move_project_group_up/down` 后手动发 `DismissEvent`（[sidebar.rs:3065-3104](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3065-L3104)）。
   - 「Remove」：调 `MultiWorkspace::remove_project_group` 并收起菜单（[sidebar.rs:3106-3117](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3106-L3117)）。

宿主侧的对应方法都在 `MultiWorkspace` 上：`project_group_keys`（[multi_workspace.rs:848](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L848)）、`workspaces_for_project_group`（[multi_workspace.rs:938](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L938)）、`move_project_group_up/down`（[multi_workspace.rs:898](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L898) / [multi_workspace.rs:916](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L916)）、`remove_project_group`（[multi_workspace.rs:951](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L951)）、`open_project_group_in_new_window`（[multi_workspace.rs:1025](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L1025)）。

#### 4.2.4 代码实践（本讲代码实践任务 · 第二部分）

1. **实践目标**：给 `render_project_header_ellipsis_menu` 的菜单项列一张完整清单（标题 + 触发的动作/效果），作为后续维护的「规格文档」。
2. **操作步骤**：
   - 通读 [sidebar.rs:2857-3118](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2857-L3118)，对每个条目记录四列：条目标题、出现/禁用条件、绑定的动作（若有）、点击后的实际效果（调了宿主的哪个方法）；
   - 特别标注哪些条目是「确认后菜单保持打开」（`build_persistent` 的默认行为），哪些手动发 `DismissEvent` 收起；
   - 顺手为 `New Thread In…` 菜单（[sidebar.rs:2573-2692](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2573-L2692)）做一张同构的表。
3. **需要观察的现象**：条件计算（`show_multi_project_entries`、`show_reorder_entries`、`can_move_up/down`、`creation_blocked`）都发生在菜单闭包里，即每次打开重新求值。
4. **预期结果**：省略号菜单的参考清单如下（你的表应与之等价）：

| 条目 | 条件 | 绑定动作 | 效果 |
| --- | --- | --- | --- |
| Open Project in New Window | 本地分组且分组数 ≥ 2 | `workspace::MoveProjectToNewWindow` | `open_project_group_in_new_window`，弹出新窗口 |
| Focus Project / Focus Last Project | 恒出现；本组活跃时禁用 | 无（custom handler，配合 `menu::SecondaryConfirm` 次级确认） | 有打开工作区 → `activate_workspace`；无 → `open_workspace_for_group`；均清空 selection/active_entry |
| "Open Worktrees" 头 + 每工作区一行 | 组内 ≥ 1 个打开的工作区 | 无（custom） | 行点击 → `multi_workspace.activate` + 收起菜单；✕ → `remove(..., RemovalIntent::CloseProject)` + 收起 |
| Move Up / Move Down | 分组数 ≥ 2；首/尾分组分别禁用 | `MoveProjectUp` / `MoveProjectDown` | `move_project_group_up/down` + 收起菜单 |
| Remove | 恒出现 | 无（custom） | `remove_project_group` + 收起菜单 |

   由于整张菜单是 `build_persistent`，不手动发 `DismissEvent` 的确认（如 Focus 条目）执行后菜单保持打开。此结论为源码推演结果，**待本地验证**（`sidebar_tests.rs` 中没有覆盖这些菜单的测试——用 `ellipsis`、`default_branch`、`worktree_create` 等关键词检索均无命中）。

#### 4.2.5 小练习与答案

**练习 1**：为什么「⋯」菜单用 `build_persistent` 而 `New Thread In…` 菜单用 `ContextMenu::build`？

**答案**：省略号菜单里有一组「连续操作」条目——Move Up/Move Down 排序。用户往往要连按几次才能把分组挪到目标位置，每次确认后自动关闭会很折磨；`build_persistent` 让菜单保持打开，由需要关闭的条目（Remove、工作区切换/关闭）手动发 `DismissEvent`。`New Thread In…` 是一次性的选择动作（选个工作区建线程），选完就该关闭，用默认的 `build` 即可。

**练习 2**：右键分组头为什么能打开省略号菜单？这条链路上哪个字段是关键？

**答案**：`render_project_header` 在整行上注册了 `on_mouse_down(Right)`，取出 `project_header_menu_handles.get(&ix)` 的 `PopoverMenuHandle` 并 `toggle`（[sidebar.rs:2433-2443](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2433-L2443)）。关键在于创建菜单时用 `.with_handle(menu_handle)` 把同一个句柄注入 `PopoverMenu`（[sidebar.rs:2801-2802](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2801-L2802)），句柄存在 `Sidebar` 的按下标索引的 HashMap 里（[sidebar.rs:776](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L776)）。

**练习 3**：`project_header_menu_ix` 字段被哪些代码读写？它的取值表达了什么？

**答案**：只在三处出现：字段声明（[sidebar.rs:778](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L778)）、初始化为 `None`（sidebar.rs:916）、菜单 `on_open` 时写入 `Some(ix)`（[sidebar.rs:2809-2818](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2809-L2818)）以及 `DismissEvent` 时清回 `None`（[sidebar.rs:3120-3130](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3120-L3130)）。它记录「当前展开的省略号菜单在第几行」；而「展开时按钮保持可见」这件事其实不靠它，靠的是 `menu_handle.is_deployed()`（[sidebar.rs:2799-2807](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2799-L2807)）。

### 4.3 prefetch_worktree_default_branches 与 DefaultBranchCache：把 git I/O 挪出菜单路径

#### 4.3.1 概念说明

`Create New Worktree…` 子菜单想提供「基于远程默认分支建 worktree」的选项，而「默认分支是什么」需要一次异步 git 查询（本地走 git 后台作业，远程走 RPC）。矛盾在于：菜单闭包是同步的。

解法是**预热缓存**：`Sidebar` 上有一个以 `ProjectGroupKey` 为键的字段 `worktree_default_branches: HashMap<ProjectGroupKey, DefaultBranchCache>`（[sidebar.rs:779](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L779)，构造时为空表 [sidebar.rs:917](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L917)）。每次 `update_entries` 的**最后一步**都会调用 `prefetch_worktree_default_branches`（[sidebar.rs:2005-2012](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2005-L2012)），把所有分组提前查好；子菜单打开时只做一次同步的 HashMap 查找。

`DefaultBranchCache` 只有两个变体（[sidebar.rs:701-706](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L701-L706)），配合「键不存在」，构成事实上的三态：

- **缺失**（键不在 HashMap）：还没查过，**或者**查询条件尚不满足（仓库还没加载完）而刻意不写入，等下次重建重试；
- **`Pending`**：查询已发出、结果未回；
- **`Resolved(Option<RemoteBranchName>)`**：终态。`Resolved(None)` 表示「确认没有（或拿不到）默认分支」——**同样不再重试**。

这是「全量重推导」教义下的合法例外：默认分支不随侧边栏可见状态变化，把它记成字段不是「增量协调状态」，而是一次性事实的缓存；它不影响 `contents` 的任何内容。

#### 4.3.2 核心流程

```
update_entries（u3-l2 五步）末尾
  └─ prefetch_worktree_default_branches
       从 contents.entries 收集所有 ProjectHeader 的 key
       对每个 key：
         已有缓存（任何状态）→ 跳过
         workspaces_for_project_group(key).first() 不存在（组内无打开工作区）→ 跳过
         否则 prefetch_worktree_default_branch(key, base)：
           已有缓存 → return（防重入）
           active_repository() 为 None → return（不写缓存 → 下次重建重试）
           request = repository.default_branch(true)   // 发出异步 git 作业
           插入 Pending                                 ← 写入点 1（同步）
           spawn 任务：
             await request → 解析 RemoteBranchName::parse
             插入 Resolved(parsed) + cx.notify()          ← 写入点 2（异步回调）
```

三条状态迁移边：

1. **缺失 → Pending**：满足「分组头在列 + 组内有打开工作区 + 活跃仓库存在」时，先插入 `Pending` 再发查询（同步侧）；
2. **Pending → Resolved**：后台任务 await 完成后写入（可能是 `Resolved(Some("origin/main"))`，也可能因查询失败/返回空/名字解析不出 `remote/branch` 结构而是 `Resolved(None)`）；
3. **缺失 → 缺失（重试）**：`active_repository()` 为 `None` 时刻意**不插入**任何键，下一次 `update_entries` 会再次尝试——仓库加载完成后即可补上。

注意**没有任何**从 `Resolved` 出发的边，也没有淘汰逻辑：缓存在 `Sidebar` 实体的整个生命周期内只增不减。

#### 4.3.3 源码精读

1. **`DefaultBranchCache` 与设计注释**：枚举上方的注释直接点明动机——「per-project-group 的远程默认分支缓存，用来填充 "Create New Worktree" 子菜单，避免菜单打开期间做 git I/O」。见 [sidebar.rs:701-706](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L701-L706)。

2. **收集器 `prefetch_worktree_default_branches`**：从当前 `contents.entries` 里过滤出所有 `ProjectHeader` 的 key（复用重建结果，不重算分组）；已有任何缓存态的键直接跳过；取组内第一个工作区去查。注释解释了为什么任取一个就行：同一仓库的所有 worktree 共享同一个默认分支。见 [sidebar.rs:2706-2737](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2706-L2737)。同时这也意味着**组内没有打开工作区的分组（比如全部关闭的历史分组）永远不会被预热**——`first()` 返回 `None` 后 `continue`。

3. **单个键的预热 `prefetch_worktree_default_branch`**：
   - 防重入守卫与「无仓库不落键」的重试策略（注释原话：no-repository case is deliberately not inserted so it retries on a later rebuild once the repository has finished loading），见 [sidebar.rs:2745-2753](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2745-L2753)；
   - 写入点 1——先发查询再插 `Pending`：`repository.default_branch(true)` 返回一个 oneshot receiver（git 作业在后台线程执行），见 [sidebar.rs:2754-2756](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2754-L2756)。`true` 表示结果要带 remote 名（即 `origin/main` 而非裸 `main`），这是 [git_store.rs:9329-9332](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/project/src/git_store.rs#L9329-L9332) 的 `include_remote_name` 参数；
   - 写入点 2——异步回调写入终态：`cx.spawn` 的任务 await 查询结果，`RemoteBranchName::parse` 把 `refs/remotes/origin/main` 或 `origin/main` 解析成结构化名字，插入 `Resolved` 并 `cx.notify()` 触发重渲染（此刻菜单里的子菜单下次展开就能拿到结果）。见 [sidebar.rs:2758-2769](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2758-L2769)，任务 `.detach()` 独立运行。

4. **读取侧**：子菜单只认 `Resolved`（4.2.3 第 4 条），缺失与 `Pending` 等价于「不知道默认分支」，此时 `worktree_create_targets` 退化为只提供 `CurrentBranch` 选项——所以即便预热没来得及完成，菜单依然可用，只是少一个选项。这是典型的「缓存降级」设计。

#### 4.3.4 代码实践（本讲代码实践任务 · 第一部分）

1. **实践目标**：完整追踪 `prefetch_worktree_default_branches` → `prefetch_worktree_default_branch` → `worktree_default_branches` 的写入路径，并给出三态迁移时刻表。
2. **操作步骤**：
   - 从 [sidebar.rs:2012](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2012)（`update_entries` 的调用点）出发，依次通读 [sidebar.rs:2710-2737](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2710-L2737) 与 [sidebar.rs:2739-2770](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2739-L2770)；
   - 标出 `worktree_default_branches` 的**全部**写入点（提示：只有两处 `insert`，一处同步一处异步）与全部读取点（收集器的 `contains_key` 与子菜单的 `get`）；
   - 画一张状态机图：三个状态、三条边（缺失→Pending、Pending→Resolved(Some)/Resolved(None)、缺失→缺失重试），每条边标注触发条件与代码行号；
   - 回答：为什么 `Resolved(None)` 不像「无仓库」那样保持缺失以待重试？（提示：想想无网络/无远程的笔记本场景，重试每次重建都会发起一次注定失败的 git 查询。）
3. **需要观察的现象**：（可选，本地实验）在 [sidebar.rs:2755](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2755) 之前临时加一行 `log::info!("prefetch default branch for {:?}", key);`，用 `RUST_LOG=sidebar=info cargo run -p zed` 运行，观察每个分组只打印一次、全部关闭工作区的分组不打印。
4. **预期结果**：状态机与 4.3.2 的一致；两个 `insert` 分别在 [sidebar.rs:2755-2756](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2755-L2756)（Pending）与 [sidebar.rs:2761-2765](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2761-L2765)（Resolved）。`Resolved(None)` 不重试的理由：它已经是一次**真实查询的结论**（查到了空/失败/名字不合法），重试大概率还是失败，反而每次重建都白付一次 git I/O；而「缺失（无仓库）」是**前提未就绪**，未来大概率能成功，值得重试。日志实验**待本地验证**，做完记得还原源码。

#### 4.3.5 小练习与答案

**练习 1**：如果把预热从 `update_entries` 末尾挪到「菜单 `on_open` 回调」里，会发生什么？

**答案**：`on_open` 仍然早于用户点开子菜单，理论上还有一小段时间可供查询完成。但 `update_entries` 每次重建都会执行，预热窗口远大于一次悬停；更重要的是菜单读取侧只认 `Resolved`，若查询未完成，用户点开子菜单时 `worktree_create_targets` 只会给出 `CurrentBranch` 一个选项，随后结果到达还得靠再次展开才能看到——把预热放在重建末尾能让缓存在用户想到要点菜单之前就绪。

**练习 2**：同一分组先打开、再全部关闭工作区，缓存键还在吗？之后该分组头还会出现在 `contents.entries` 里吗？

**答案**：键还在——没有任何删除路径。分组头仍会出现：u2-l2 讲过组内工作区全部关闭后 `ProjectGroupKey` 仍保留、侧边栏渲染「已关闭」分组头。因此收集器仍会遍历到这个 key，但 `contains_key` 命中（此前已 `Resolved`）直接跳过；即便当初没预热成功，`workspaces_for_project_group(key).first()` 为 `None` 也会 `continue`——关闭态分组永远不会发起查询。

**练习 3**：`RemoteBranchName::parse("main")` 返回什么？这会让缓存进入哪个状态？

**答案**：`None`——`"main"` 没有斜杠，`split_once('/')` 失败（见 [worktree_service.rs:43-53](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/git_ui_core/src/worktree_service.rs#L43-L53)）。缓存会进入 `Resolved(None)`：查询成功返回了裸分支名（比如某个没有远程的本地仓库），但解析不出 remote/branch 结构，视为「没有可用的远程默认分支」，是终态。

### 4.4 create_worktree_in_workspace 与 connect_remote：两个薄委托适配器

#### 4.4.1 概念说明

本讲涉及的两个「重活」都不是 sidebar 自己干的：

- **建 git worktree**（含 fetch 远程、命名、打开新工作区、恢复布局）是 `git_ui_core::worktree_service` 的职责，worktree picker 也用同一套；
- **建立 SSH 远程连接**（含认证 UI）是 `remote_connection` crate 的职责。

sidebar 对它们各写了一个**薄适配器**：`create_worktree_in_workspace` 和 `connect_remote`。适配器的价值在于把「别人的 API 形状」翻译成「本 crate 调用点需要的形状」，并集中持有这类跨 crate 约定——调用点不用重复写参数组装。

两个适配器之间还有一层公共词汇：`zed_actions` crate 定义的 `CreateWorktree` 动作与 `NewWorktreeBranchTarget` 枚举。sidebar 把用户意图编码成这两个类型，git_ui_core 负责解释执行——这样「侧边栏子菜单」与「worktree picker」天然共享同一套行为（`create_worktree_in_workspace` 的注释原话：Mirrors the behavior of the worktree picker's "Create new worktree" entries）。

#### 4.4.2 核心流程

**选项推导（git_ui_core 侧，`worktree_create_targets`）**：

```
输入：是否多仓库 / 默认分支(Option) / 当前分支名(Option<&str>)
多仓库            → [CurrentBranch]                （每仓库分支不同，无法统一默认分支）
无默认分支         → [CurrentBranch]
否则              → [DefaultBranch(默认分支)]
                    且 当前分支 ≠ 默认分支名 时追加 [CurrentBranch]
```

条目标签由 `branch_label` 给出：`DefaultBranch` 显示 `origin/main` 式全名；`CurrentBranch` 在多仓库时显示 `"current branches"`，否则显示当前分支名、兜底 `"HEAD"`。

**建 worktree（sidebar 适配器）**：

```
create_worktree_in_workspace(workspace, branch_target)
  └─ workspace.update：
       focused_dock = workspace.focused_dock_position(window, cx)
       git_ui_core::worktree_service::handle_create_worktree(
            workspace, &CreateWorktree { worktree_name: None, branch_target },
            window, focused_dock, cx)
```

**远程连接（sidebar 适配器）**：

```
connect_remote(modal_workspace, connection_options)
  └─ remote_connection::connect_with_modal(...)   // 带认证模态框的 SSH 连接
调用方（如 open_workspace_for_group）把它作为回调传给
MultiWorkspace::find_or_create_workspace —— 后者在需要新远程工作区时才调用它
```

#### 4.4.3 源码精读

1. **`create_worktree_in_workspace`**：整体就是一次 `workspace.update` 加一次转发。`worktree_name: None` 表示「名字由服务方决定」（创建时生成）；`focused_dock` 是当前聚焦的 dock 位置，作为兜底传下去（新工作区恢复布局时用）。见 [sidebar.rs:708-728](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L708-L728)。

2. **服务方入口 `handle_create_worktree`**：doc 注释说明它「通用处理 `CreateWorktree` 动作，不含任何 agent panel 逻辑；创建 worktree、打开工作区、恢复布局与文件；错误经 toast 呈现，返回的工作区句柄被丢弃」——sidebar 正是不需要拿到句柄的场景。见 [worktree_service.rs:688-706](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/git_ui_core/src/worktree_service.rs#L688-L706)。

3. **选项词汇 `WorktreeCreateTarget` 与 `NewWorktreeBranchTarget`**：前者是 git_ui_core 的 UI 层选项（`CurrentBranch` / `DefaultBranch(RemoteBranchName)`），后者是 `zed_actions` 的动作参数（`CurrentBranch` / `RemoteBranch { remote_name, branch_name }`）；`branch_target()` 负责两者间的翻译。见 [worktree_service.rs:63-98](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/git_ui_core/src/worktree_service.rs#L63-L98)。

4. **推导规则 `worktree_create_targets`**：见 [worktree_service.rs:103-121](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/git_ui_core/src/worktree_service.rs#L103-L121)，doc 注释点明「优先远程默认分支，与当前分支相同时只给当前分支」。`RemoteBranchName` 的解析与显示见 [worktree_service.rs:37-58](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/git_ui_core/src/worktree_service.rs#L37-L58)。

5. **`connect_remote` 适配器**：doc 注释直说它的用途——「适合作为 `MultiWorkspace::find_or_create_workspace` 的 `connect_remote` 参数」，实现只有一行 `connect_with_modal`。见 [sidebar.rs:688-699](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L688-L699)。典型调用方是 `open_workspace_for_group`：把 `connect_remote` 作为闭包传入（[sidebar.rs:1263-1275](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1263-L1275)），任务结束后再统一 `dismiss_connection_modal`（[sidebar.rs:1277-1283](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1277-L1283)）。整个文件里这样的传参点共 5 处（sidebar.rs:1268、1307、3981、4614、4929）。

6. **宿主侧的回调契约**：`find_or_create_workspace` 的 doc 注释解释了为什么用回调注入而不是直接依赖——连接 UI（密码提示等）由调用方负责，宿主只在「确实需要新建远程工作区」时才调用它。见 [multi_workspace.rs:1085-1134](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L1085-L1134)。

#### 4.4.4 代码实践

1. **实践目标**：画出「子菜单点击 → worktree 真正建立」的完整调用链，并标注每次 crate 边界穿越。
2. **操作步骤**：
   - 从 [sidebar.rs:2679-2686](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2679-L2686)（子菜单条目的处理器）出发，依次经过 `create_worktree_in_workspace` → `handle_create_worktree`，记下每步所在的 crate；
   - 再追踪 `branch_target()` 的翻译：`WorktreeCreateTarget`（git_ui_core）如何变成 `NewWorktreeBranchTarget`（zed_actions）再变成 `CreateWorktree` 动作载荷；
   - 用同样方法整理 `connect_remote` 链：`open_workspace_for_group`（sidebar）→ `find_or_create_workspace`（workspace）→ `connect_remote`（sidebar）→ `connect_with_modal`（remote_connection），注意这条链 **离开又回到** sidebar。
3. **需要观察的现象**：sidebar 自始至终没有任何 git 命令执行、socket 连接代码；它只组装意图（动作类型）与编排顺序。
4. **预期结果**：两条链的边界地图——
   - worktree 链：`sidebar`（条目处理器）→ `sidebar`（适配器）→ `git_ui_core`（服务方，词汇借道 `zed_actions`）；
   - 远程链：`sidebar` → `workspace`（宿主）→ 回调回 `sidebar`（适配器）→ `remote_connection`（服务方）。
   此为源码推演结果，无需运行即可确认（调用关系完全静态可追）。

#### 4.4.5 小练习与答案

**练习 1**：`worktree_create_targets(true, Some(RemoteBranchName::origin_main), Some("dev"))` 返回什么？菜单上会显示几个条目？

**答案**：多仓库为 `true` 时直接返回 `[CurrentBranch]`（[worktree_service.rs:108-110](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/git_ui_core/src/worktree_service.rs#L108-L110)），默认分支参数被忽略——多个仓库各有各的默认分支，没法统一。菜单显示一个条目，标签是 `"current branches"`（`branch_label` 对多仓库的 `CurrentBranch` 特判）。

**练习 2**：为什么 `handle_create_worktree` 要同时存在一个「丢弃句柄、错误走 toast」版本和一个返回 `Task<Result<...>>` 的 `create_worktree_workspace` 版本？

**答案**：两类调用方的需求不同。像侧边栏菜单这样「用户点了就完事」的场景只关心「尽力创建、出错有人告诉我」，错误呈现统一交给 toast，句柄无用；而像 `create_thread` agent 工具那样的调用方（[worktree_service.rs:708-735](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/git_ui_core/src/worktree_service.rs#L708-L735) 的 doc 注释）需要拿到新工作区句柄往里建线程、并自己处理错误。sidebar 选前者，正说明它是纯编排者。

**练习 3**：`create_worktree_in_workspace` 里为什么要先算 `focused_dock` 再传入？

**答案**：`handle_create_worktree` 的签名需要一个 `fallback_focused_dock`（新工作区恢复布局时决定把面板放哪个 dock 的兜底值）。这个信息属于**源工作区当前的 UI 状态**，只有调用方（拿着 `workspace` 实体和 `window` 的 sidebar 适配器）能问到；服务方无法从动作载荷里得知，所以由适配器在转发前查询一次。

## 5. 综合实践

把本讲四个模块串成一份**「分组头菜单规格文档」**，作为你自己的学习产出：

1. **缓存状态机图**：完成 4.3.4 的状态机（三个状态、三条边，每条边标注触发条件与代码行号），并补一句「为什么 `worktree_default_branches` 是全量重推导教义的合法例外而不是违规」。
2. **两张菜单清单**：完成 4.2.4 的省略号菜单条目表，再仿照它为 `New Thread In…` 菜单做一张同构的表（条目、条件、动作、效果），其中 `Create New Worktree…` 子菜单要列出 `worktree_create_targets` 三种输入组合下的条目集合。
3. **委托地图**：完成 4.4.4 的两条调用链笔记，标注每一步跨越的 crate 边界，并回答：如果未来要让「基于指定远程建 worktree」支持 SSH 远程分组，`creation_blocked` 判定需要动吗？（提示：看 [sidebar.rs:2632-2635](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2632-L2635) 挡掉了什么。）
4. **（可选，本地实验）**：运行 `cargo run -p zed`，打开一个含多 worktree 的 git 项目，逐项核对你两张表里的条目与出现条件；再按 4.3.4 的日志实验验证「每分组恰好预热一次、失败不重试」。此步**待本地验证**，改过源码记得还原。

需要说明：`sidebar_tests.rs` 中没有直接覆盖这两个菜单或预取逻辑的测试（用 `ellipsis`、`default_branch`、`worktree_create`、`prefetch` 等关键词检索均无命中），所以本讲的验证只能靠源码推演 + 本地 UI 实验——这也提示这两块是潜在的测试补强点。

## 6. 本讲小结

- `workspace_menu_worktree_labels` 在菜单打开时现算每个工作区的标签：多根才显示文件夹名，git worktree 显示「主名 / worktree 名」（linked 用短名、主仓用字面量 `main`），无仓库降级为纯文件夹名、无图标。
- 「+」按钮按「组内有无打开工作区」分流：空组直接新建；非空弹 `New Thread In…` 菜单（每工作区一条 + 可选 `Create New Worktree…` 子菜单），子菜单选项由 `worktree_create_targets` 统一推导，与 worktree picker 共用同一套规则。
- `DefaultBranchCache` 用「缺失 / Pending / Resolved」三态把异步 git 查询挪出菜单路径：`update_entries` 末尾预热、菜单同步读缓存；无仓库时刻意保持缺失以便重试，`Resolved(None)` 是不重试的终态；缓存按 `ProjectGroupKey` 存、只增不减。
- 省略号菜单用 `build_persistent`（确认后不关）支撑 Move Up/Down 连续排序，其余条目（Remove、工作区切换/关闭）手动发 `DismissEvent` 收起；条目数据全部在打开时刻现算，菜单句柄按下标存表所以右键分组头也能开关它。
- 侧边栏对跨 crate 能力只写薄适配器：`create_worktree_in_workspace` 转发给 `git_ui_core` 的 `handle_create_worktree`（词汇借道 `zed_actions` 的 `CreateWorktree`/`NewWorktreeBranchTarget`），`connect_remote` 把「弹连接模态框」作为回调注入 `MultiWorkspace::find_or_create_workspace`——自己不实现 git 操作也不碰连接池。

## 7. 下一步学习建议

本讲结束了单元四（渲染层）。下一讲 u5-l1《动作注册与键位上下文》将从「菜单里看到的动作」进入「动作系统本身」：本讲条目上挂的 `MoveProjectUp`、`workspace::MoveProjectToNewWindow`、`menu::SecondaryConfirm` 都会回到 `gpui::actions!`、`dispatch_context` 与键位分发的框架下重新理解。建议带着一个问题去读：菜单条目的 `.action(...)` 挂的动作与 `.handler(...)` 注册的处理器是两套东西，它们各自在什么时机被触发？

若想继续深挖本讲的支线，推荐顺读 `crates/git_ui_core/src/worktree_service.rs` 中 `create_worktree_workspace_inner` 的实现（fetch 远程、错误 toast、布局恢复都在那里），以及 u8-l2 会讲到的归档流水线——那里同样大量出现「侧边栏只编排、服务方干活」的模式。
