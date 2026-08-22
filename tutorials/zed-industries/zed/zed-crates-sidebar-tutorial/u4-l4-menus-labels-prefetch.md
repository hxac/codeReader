# 菜单、工作区标签与默认分支预取（u4-l4）

## 1. 本讲目标

学完本讲，你应该能够：

1. 推演 `workspace_menu_worktree_labels` 在任意工作区布局（单根、多根、含 linked worktree、无 git 仓库）下产出的菜单标签。
2. 读懂项目分组头右上角两个按钮的完整行为：「+」按钮的 `New Thread In…` 菜单与 `Create New Worktree…` 子菜单，「⋯」省略号菜单的全部条目。
3. 说清 `DefaultBranchCache` 三种状态（缺失、`Pending`、`Resolved`）的迁移时机，以及为什么要在 `update_entries` 里预热它。
4. 理解侧边栏如何「不自己造轮子」：创建 git worktree 委托给 `git_ui_core::worktree_service`，建立远程连接委托给 `remote_connection`，打开工作区委托给 `MultiWorkspace::find_or_create_workspace`。

本讲是渲染层（单元四）的收官：u4-l1 看整体骨架，u4-l2 看分组头与粘性头部，u4-l3 看线程行/终端行，本讲看挂在分组头尾部的那两颗按钮，以及它们背后跨越 crate 边界的调用。

## 2. 前置知识

- **工作区（Workspace）与项目分组（ProjectGroupKey）**：一个 `MultiWorkspace` 窗口里可以同时打开多个项目；`ProjectGroupKey`（主 worktree 路径 + 远程主机）把「同一个仓库的所有工作区」归成一个分组，侧边栏每个分组渲染一个分组头（见 u2-l2）。
- **git worktree**：一个 git 仓库可以同时检出多个工作目录（main worktree + 若干 linked worktree），各自停在不同分支上。Zed 把每个 worktree 当作一个可打开的工作区。
- **远程默认分支**：形如 `origin/main` 的远程跟踪分支。`RemoteBranchName::parse` 会把它拆成 remote 名（`origin`）和分支名（`main`）。
- **菜单的三种构建件**（ui crate）：
  - `PopoverMenu`：挂在按钮上的弹出菜单容器，通过 `PopoverMenuHandle` 可以在代码里开关它；
  - `ContextMenu`：条目式菜单构建器，`ContextMenu::build` 在每次确认后自动关闭，而 `ContextMenu::build_persistent` 构建的菜单在确认后**保持打开**，适合「连续操作」类菜单；
  - `menu::SecondaryConfirm`：菜单的「次级确认」动作，配合条目右侧的修饰键提示（如 ⌥-click）使用。
- **同步渲染 vs 异步 I/O 的矛盾**：菜单内容在打开瞬间（一个同步的 `menu(...)` 闭包）就要拼出完整条目；而「远程默认分支是什么」需要一次异步 git 查询。本讲的 `DefaultBranchCache` 就是解决这个矛盾的手段。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `crates/sidebar/src/sidebar.rs` | 本讲主战场：标签函数、两个按钮的渲染、预取逻辑、两个委托适配器全部在此 |
| `crates/git_ui_core/src/worktree_service.rs` | 提供 `RemoteBranchName`、`worktree_create_targets`（选项推导）与 `handle_create_worktree`（真正建 worktree） |
| `crates/zed_actions/src/lib.rs` | 定义 `NewWorktreeBranchTarget` 与 `CreateWorktree` 动作，是 sidebar 与 git_ui_core 之间的公共词汇 |
| `crates/workspace/src/multi_workspace.rs` | 宿主：`find_or_create_workspace`（接收 `connect_remote` 回调）、分组排序/移除、`MoveProjectToNewWindow` 动作 |
| `crates/project/src/git_store.rs` | `Repository::default_branch`（异步 git 查询）与 `linked_worktree_short_name`（短名推导） |
| `crates/remote_connection/src/remote_connection.rs` | `connect_with_modal`：带模态 UI 的 SSH 连接建立 |
| `crates/ui/src/components/context_menu.rs` | `ContextMenu::build_persistent` 与 `end_slot_action` 的语义出处 |

## 4. 核心概念与源码讲解

### 4.1 workspace_menu_worktree_labels：一个工作区在菜单里叫什么

#### 4.1.1 概念说明

一个项目分组里可能同时开着多个工作区（比如 main worktree 加一个 linked worktree）。当菜单需要列出「在哪个工作区里新建线程」或「打开哪个 worktree」时，就必须给每个工作区一个**人类可读且不歧义**的名字。

`workspace_menu_worktree_labels` 是一个自由函数：输入一个工作区实体，输出它的标签列表（一个工作区可能有多个根路径，因此是 `Vec`）。它不缓存、不持久化，每次菜单打开时现算——这符合本 crate「能从当前世界状态算出来的就不存字段」的教义（见 u3-l2）。

标签分主名（primary）与次名（secondary）两部分，渲染成 `主名 / 次名` 的形式；多个标签之间再用 `•` 连接（由调用方负责，见 4.2/4.4）。

#### 4.1.2 核心流程

```
对工作区的每个根路径 root_path（来自 workspace.root_paths()）：
    folder_name  = root_path 的最后一段文件名
    在仓库快照里找 work_directory_abs_path == root_path 的那个 git 仓库
    ├─ 找到了（这是个 git worktree）：
    │     worktree_name =
    │         若是 linked worktree：linked_worktree_short_name(主仓库路径, root_path)
    │                             （失败则回退 folder_name）
    │         否则：固定为 "main"
    │     若工作区是多根（root_paths.len() > 1）：
    │         标签 = [GitWorktree 图标] folder_name / worktree_name
    │     否则：
    │         标签 = [GitWorktree 图标] worktree_name
    └─ 没找到（普通文件夹，无 git 仓库）：
          标签 = folder_name（无图标、无次名）
```

其中 `linked_worktree_short_name` 的规则：若 linked worktree 的目录名与主仓库名不同，直接用目录名；相同（比如两个都叫 `zed`），则取 linked 路径**父目录**的名字来消歧。

#### 4.1.3 源码精读

先看标签的数据结构与渲染——[sidebar.rs:579-600](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L579-L600)：`WorkspaceMenuWorktreeLabel` 持有可选图标、主名、可选次名；`render()` 把它们拼成一行，主次名都带 `truncate()`（超宽截断），次名前加一个半透明的 `/` 分隔。

再看推导逻辑——[sidebar.rs:602-662](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L602-L662)：

- [sidebar.rs:606-607](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L606-L607)：`root_paths` 与 `show_folder_name = root_paths.len() > 1`——**只有多根工作区才显示文件夹名**，单根时直接显示 worktree 名，避免冗余。
- [sidebar.rs:609-614](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L609-L614)：先把所有仓库快照收集出来，再对每个根路径做一次线性匹配（[sidebar.rs:624-626](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L624-L626)，按 `work_directory_abs_path` 相等查找）。
- [sidebar.rs:628-652](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L628-L652)：命中仓库时按 linked/main 推导 `worktree_name`，再按 `show_folder_name` 决定主次名的摆放。
- [sidebar.rs:653-659](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L653-L659)：未命中仓库的根路径降级为「无图标 + 文件夹名」。

两个外部支撑点：

- `root_paths` 的定义——[workspace.rs:7136-7142](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/workspace/src/workspace.rs#L7136-L7142)：取所有**可见** worktree 的绝对路径。注意是 `visible_worktrees`，被隐藏的系统 worktree 不会出现在标签里。
- 短名推导——[git_store.rs:10627-10647](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/project/src/git_store.rs#L10627-L10647)：`linked_worktree_short_name` 在两条路径相同时返回 `None`（调用方回退 `folder_name`），目录名撞车时取父目录名。

#### 4.1.4 代码实践

**实践目标**：不运行程序，纯靠读代码推演标签输出，再找机会在真实 UI 里验证。

**操作步骤**：

1. 在纸上画三种工作区布局：
   - A：单根 `/home/me/zed`（main worktree，git 仓库）；
   - B：多根 `/home/me/zed` + `/tmp/zed-feature`（后者是前者的 linked worktree）；
   - C：单根 `/home/me/notes`（无 git 仓库的普通文件夹）。
2. 对每种布局，按 4.1.2 的伪代码逐步写出 `workspace_menu_worktree_labels` 的返回值（图标、primary、secondary）。
3. 若本机能运行 Zed（`cargo run -p zed`），打开对应布局的侧边栏分组头菜单，对照你的推演结果。

**需要观察的现象**：B 布局下两条标签长什么样？`zed / main` 与 `zed / zed-feature`？还是别的形式？

**预期结果**（按代码推演）：

- A：`[GitWorktree] main`（单根不显示文件夹名）；
- B：`[GitWorktree] zed / main` 与 `[GitWorktree] zed / zed-feature`——linked worktree 的短名来自 `linked_worktree_short_name`，其目录名 `zed-feature` 与主仓库名 `zed` 不同，直接用目录名（即路径最后一段 `file_name()`，见 [sidebar.rs:620-623](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L620-L623) 与 [git_store.rs:10635-10638](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/project/src/git_store.rs#L10635-L10638)）；
- C：`notes`（无图标、无次名）。

真实 UI 显示的短名细节**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `show_folder_name` 的判定是 `root_paths.len() > 1` 而不是「总是显示」？

**答案**：单根工作区里，文件夹名与分组名高度重复（分组标签本身就来自主 worktree 路径），再显示一遍 `zed / main` 是冗余；多根时必须用文件夹名区分各个根，所以主名换成文件夹名、worktree 名退到次名位置。这是一个「信息密度随上下文调整」的小设计。

**练习 2**：一个 linked worktree 的目录名恰好与主仓库名相同（如主 `/repos/zed`、linked `/backups/zed`），标签的次名是什么？

**答案**：`linked_worktree_short_name` 发现目录名（`zed`）等于项目名（`zed`），于是取 linked 路径父目录的名字 `backups` 作为短名（[git_store.rs:10637-10645](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/project/src/git_store.rs#L10637-L10645)）。若父目录也取不到（路径是根等情况），返回 `None`，调用方回退用 `folder_name`——此时次名与主名相同，但仍能渲染。

**练习 3**：这个函数为什么是自由函数（关联在文件顶层）而不是 `Sidebar` 的方法？

**答案**：它只依赖「工作区实体 + App 上下文」，不读 `Sidebar` 的任何字段。放在自由函数上明确了它无状态、可独立测试、可被任意菜单复用——事实上 `render_new_thread_button` 和 `render_project_header_ellipsis_menu` 两处都在调用它。

### 4.2 render_new_thread_button：「New Thread In…」与「Create New Worktree…」

#### 4.2.1 概念说明

u4-l2 讲过，分组头右侧有两颗悬停可见的按钮（[sidebar.rs:2419-2428](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2419-L2428)）：「+」新建、「⋯」菜单。本模块拆「+」按钮。

它的行为按「分组里有没有打开的工作区」分两支：

- **没有**（分组下的工作区全关了，只剩 `Closed` 行）：点一下直接走「找/开工作区 → 新建条目」的快捷路径；
- **有**：弹出 `New Thread In…` 菜单，让用户选择在哪个工作区新建；满足条件时还附带一个 `Create New Worktree…` 子菜单。

#### 4.2.2 核心流程

```
render_new_thread_button(ix, key, group_name):
    menu_handle = project_header_new_thread_menu_handles[ix]   # 渲染期懒初始化
    open_workspaces = MultiWorkspace.workspaces_for_project_group(key)

    if open_workspaces 为空:
        返回普通 IconButton(+):
            tooltip = "Start New Agent Thread"
            点击 → set_group_expanded(true) → selection = None
                    → workspace_for_group 有则 create_new_entry
                      否则 open_workspace_and_create_entry(LastCreatedKind)
    else:
        返回 PopoverMenu:
            menu 闭包（打开时执行，同步）:
                header("New Thread In…")
                对每个 open_workspace:
                    custom_entry( 标签 = workspace_menu_worktree_labels(...) 用 • 连接,
                                  活跃工作区加 ✓,
                                  选中 → 展开分组 + create_new_entry(workspace) )
                base_workspace = 活跃工作区(若在组内) 否则第一个
                creation_blocked = 无 base 或 (经 collab 或 没有任何 git 仓库)
                if base 存在且未 blocked:
                    separator + submenu("Create New Worktree…"):
                        default_branch = worktree_default_branches[key]  # 只认 Resolved
                        targets = worktree_create_targets(多仓?, default_branch, 当前分支)
                        对每个 target: entry("Based on {label}") → create_worktree_in_workspace
```

#### 4.2.3 源码精读

- 句柄懒初始化与按钮外观——[sidebar.rs:2488-2503](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2488-L2503)：句柄从 `project_header_new_thread_menu_handles` 按 header 下标取（该映射在渲染分组头条目时用 `entry(ix).or_default()` 懒建，见 [sidebar.rs:2196-2199](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2196-L2199)）；菜单未打开时按钮 `visible_on_hover`，打开时常显。
- 空分组快捷路径——[sidebar.rs:2505-2532](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2505-L2532)：`workspaces_for_project_group` 为空时直接建按钮，点击先展开分组、清空 `selection`，再尝试就地新建或先开工作区（`open_workspace_and_create_entry` 走 4.5 讲的 `find_or_create_workspace` 链路）。
- 菜单闭包内计算标签——[sidebar.rs:2564-2567](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2564-L2567)：注意标签是在**菜单打开那一刻**的 `menu(...)` 闭包里现算的，不是按钮渲染时算好存起来的。
- 每个工作区一条目——[sidebar.rs:2573-2621](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2573-L2621)：`custom_entry` 的第一个闭包渲染行（多个标签用 `•` 以 25% 透明度连接，活跃工作区尾部加 ✓），第二个闭包是选中处理（展开分组、清 selection、`create_new_entry`）。
- base 工作区与阻断判定——[sidebar.rs:2623-2635](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2623-L2635)：优先用活跃工作区（若它属于本组），否则用组内第一个；`creation_blocked` 模仿 worktree picker 的 `creation_blocked_reason`——经 collab 共享或没有任何 git 仓库时不出子菜单，否则子菜单展开会是一片空白。
- 子菜单与预取结果的消费——[sidebar.rs:2637-2692](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2637-L2692)：`default_branch` 只从缓存里读 `Resolved` 变体（[sidebar.rs:2652-2662](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2652-L2662)），`Pending`/缺失一律当 `None`；随后交给 `worktree_create_targets` 推导选项，每个选项一条 `Based on {label}` 条目，点击调用 `create_worktree_in_workspace`（见 4.5）。

选项推导函数在 git_ui_core 里——[worktree_service.rs:103-121](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/git_ui_core/src/worktree_service.rs#L103-L121)：

```rust
pub fn worktree_create_targets(
    has_multiple_repositories: bool,
    default_branch: Option<RemoteBranchName>,
    current_branch_name: Option<&str>,
) -> Vec<WorktreeCreateTarget> {
    if has_multiple_repositories {
        return vec![WorktreeCreateTarget::CurrentBranch];
    }
    let Some(default_branch) = default_branch else {
        return vec![WorktreeCreateTarget::CurrentBranch];
    };
    let is_different =
        current_branch_name.is_none_or(|current| current != default_branch.branch_name);
    let mut targets = vec![WorktreeCreateTarget::DefaultBranch(default_branch)];
    if is_different {
        targets.push(WorktreeCreateTarget::CurrentBranch);
    }
    targets
}
```

规则归纳：多仓库 → 只提供「当前分支」（各仓库当前分支可能不同，无法统一指向默认分支）；无默认分支信息 → 同样只提供「当前分支」；否则首选「默认分支」，且当前分支与默认不同（或未知）时追加「当前分支」。doc comment 明说这是为了让 worktree picker 和侧边栏菜单**保持同一套选项**。

`WorktreeCreateTarget::branch_label`（[worktree_service.rs:82-97](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/git_ui_core/src/worktree_service.rs#L82-L97)）决定条目文案：默认分支显示 `origin/main` 形式的 `display_name()`；当前分支在多仓库下显示 `current branches`，单仓库显示分支名、取不到时兜底 `HEAD`。

#### 4.2.4 代码实践

**实践目标**：把「+」按钮的行为整理成一张可核对的场景表。

**操作步骤**：

1. 准备一张三列表格：场景 / 菜单内容 / 每个条目点击后发生什么。
2. 填入以下场景（纯源码阅读，不必运行）：
   - 分组下无打开工作区；
   - 组内 1 个工作区、单仓库、缓存 `Resolved(Some(origin/main))`、当前分支 `main`；
   - 同上但当前分支 `feature-x`；
   - 组内 1 个工作区、缓存仍是 `Pending`；
   - 组内 1 个工作区、仓库列表为空（刚打开还没扫到 git 仓库）。
3. 每一行都标注对应源码行号（分支条件在哪一行判定）。

**需要观察的现象**：场景 3 与场景 2 的子菜单条目数量差异；场景 4 的子菜单退化成什么。

**预期结果**：

- 场景 1：无菜单，按钮直接新建；
- 场景 2：子菜单只有一条 `Based on origin/main`（当前分支 == 默认分支，`is_different` 为 false，不追加）；
- 场景 3：两条——`Based on origin/main` 与 `Based on feature-x`；
- 场景 4：`default_branch` 读到 `None` → 只有 `Based on HEAD` 或当前分支一条（`worktree_create_targets` 第二个分支）；
- 场景 5：`repositories(cx).is_empty()` → `creation_blocked` 为真，**整个子菜单不出现**。

以上均为代码推演结果，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么「在哪个工作区新建线程」的判断要用 `workspaces_for_project_group(key)` 而不是「分组是否在 entries 里有 Open 行」？

**答案**：工作区的开/闭判定权威在宿主 `MultiWorkspace`（u2-l2 讲过 `Open`/`Closed` 是重建时以 `PathList` 现查推导的）。`workspaces_for_project_group` 直接问宿主，避免依赖上一次重建的快照（`self.contents`），菜单打开时读到的永远是最新状态。

**练习 2**：子菜单里的条目点击后菜单会关闭吗？分组头会怎样？

**答案**：`create_worktree_in_workspace` 在 `workspace.update` 里执行 `handle_create_worktree`，它最终会创建并打开一个新工作区；新建的工作区落入同一 `ProjectGroupKey` 分组，触发侧边栏刷新。这个子菜单用的是普通 `submenu` 条目，确认后随外层菜单关闭（`PopoverMenu` 默认行为）。（侧边栏列表如何把新工作区收进分组，复习 u3-l4 的分组遍历。）

**练习 3**：`creation_blocked` 为什么把 `project.is_via_collab()` 也算进去？

**答案**：经 collab 共享的项目，其 git 操作要在宿主机器上执行且未必有完整本地仓库上下文，贸然提供「建 worktree」会得到一个必然失败的入口。侧边栏在这里刻意与 worktree picker 的 `creation_blocked_reason` 保持一致（注释见 [sidebar.rs:2629-2631](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2629-L2631)），保证两个入口的可用性判定不会打架。

### 4.3 prefetch_worktree_default_branches：DefaultBranchCache 的预热

#### 4.3.1 概念说明

4.2 里子菜单的同步闭包要读「默认分支」，而查默认分支是一次异步 git I/O（本地走 git 后台线程，远程走协议请求）。矛盾的解法是**把 I/O 挪到菜单路径之外**：每次 `update_entries` 重建完列表后，顺手为每个分组预热默认分支，存进 `worktree_default_branches: HashMap<ProjectGroupKey, DefaultBranchCache>`。等用户真的打开子菜单时，答案已经在内存里，同步读取即可。

`DefaultBranchCache` 只有两个变体，加上「键不存在」（缺失），构成三种状态：

```rust
// 按项目分组缓存的远程默认分支，用来在菜单打开期间不做 git I/O
// 就能填充 "Create New Worktree" 子菜单。
enum DefaultBranchCache {
    Pending,
    Resolved(Option<RemoteBranchName>),
}
```

注意 `Resolved` 包的是 `Option<RemoteBranchName>`：**查询失败也是一种终态**（解析不出远程名/分支名、查询出错，都落成 `Resolved(None)`），不会退回 `Pending`。

这是全量重推导教义的一个「合法例外」：教义说「能从当前世界状态算出的不存字段」，但默认分支**无法**脱离 git I/O 从世界状态算出，所以它属于「记忆/缓存」一类，与 `live_thread_statuses` 同性质（u1-l3 的字段分类）。

#### 4.3.2 核心流程

状态机（`worktree_default_branches[key]`）：

```
            ┌──────────────────────────────────────────────┐
            │  缺失（键不存在）                              │
            │  · 分组没有打开的工作区（continue 跳过）        │
            │  · active_repository 为 None（仓库未加载完，    │
            │    刻意不插入，等下一轮重建重试）               │
            └───────────────┬──────────────────────────────┘
                            │ prefetch_worktree_default_branch：
                            │ 拿到 active_repository 之后
                            │ ① repository.default_branch(true) 发出查询
                            │ ② insert(key, Pending)
                            ▼
                     ┌─────────────┐
                     │   Pending   │  查询在途；菜单此刻读到的是 None
                     └──────┬──────┘
                            │ cx.spawn 的任务完成：
                            │ request.await → parse → insert(key, Resolved(parsed))
                            │ 并 cx.notify() 触发重渲染
                            ▼
                ┌───────────────────────────────┐
                │ Resolved(Option<RemoteBranchName>) │  终态；菜单读到 Some 时
                │                              │    才会出现 "Based on origin/main"
                └───────────────────────────────┘    条目；None 则只剩当前分支
```

要点：

- **没有任何淘汰路径**。全文件对 `worktree_default_branches` 只有 `contains_key`/`get`/`insert`，没有 `remove`/`retain`。分组被移除后残留的缓存条目无害（查找按键进行，量级是分组数）。
- **键是 `ProjectGroupKey` 而不是工作区**。注释说明：同一仓库的各 worktree 共享同一个默认分支，取组内任意工作区查询结果都一样，所以取 `first()`。
- `default_branch(true)` 的 `true` 是 `include_remote_name`，返回形如 `origin/main` 的完整串，正好能被 `RemoteBranchName::parse` 拆开。

#### 4.3.3 源码精读

- 枚举定义与意图注释——[sidebar.rs:701-706](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L701-L706)；字段声明——[sidebar.rs:779](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L779)。
- 挂进重建管线的位置——[sidebar.rs:2012](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2012)：`update_entries` 五步（重建、草稿时间、草稿订阅、差异应用、预取）的最后一步。也就是说**任何一次刷新都会顺带补齐新分组的预热**。
- 批量入口——[sidebar.rs:2706-2737](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2706-L2737)：

```rust
fn prefetch_worktree_default_branches(&mut self, cx: &mut Context<Self>) {
    let Some(multi_workspace) = self.multi_workspace.upgrade() else { return; };
    let keys: Vec<ProjectGroupKey> = self.contents.entries.iter()
        .filter_map(|entry| match entry {
            ListEntry::ProjectHeader { key, .. } => Some(key.clone()),
            _ => None,
        })
        .collect();
    for key in keys {
        if self.worktree_default_branches.contains_key(&key) { continue; }
        let Some(base) = multi_workspace.read(cx)
            .workspaces_for_project_group(&key, cx).first().cloned()
        else { continue; };
        self.prefetch_worktree_default_branch(&key, &base, cx);
    }
}
```

  候选键来自**本次刚重建出来的** `contents.entries` 里的分组头；已缓存（Pending 或 Resolved）的跳过；组内无打开工作区的跳过（这类分组本就没有子菜单）。

- 单键预热——[sidebar.rs:2739-2770](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2739-L2770)：

```rust
// 键已存在说明该分组已在等待或已出结果。无仓库的情况刻意不插入，
// 这样稍后的重建会在仓库加载完成后重试。
if self.worktree_default_branches.contains_key(key) { return; }
let Some(repository) = workspace.read(cx).project().read(cx).active_repository(cx) else {
    return;
};
let request = repository.update(cx, |repository, _| repository.default_branch(true));
self.worktree_default_branches.insert(key.clone(), DefaultBranchCache::Pending);
let key = key.clone();
cx.spawn(async move |this, cx| {
    let default_branch = request.await.ok().and_then(Result::ok).flatten();
    let parsed = default_branch.as_deref().and_then(RemoteBranchName::parse);
    this.update(cx, |sidebar, cx| {
        sidebar.worktree_default_branches.insert(key, DefaultBranchCache::Resolved(parsed));
        cx.notify();
    }).ok();
}).detach();
```

  读注释与代码要抓三个细节：(1) 查询任务在插入 `Pending` **之前**就已发出（`default_branch(true)` 立刻把 job 派给后台），但两者在同一次同步 update 里完成，中间不会插入重建；(2) `request.await.ok().and_then(Result::ok).flatten()` 把「任务被取消 / 查询出错 / 结果为空」三层失败全部压平成 `None`，最终统一落到 `Resolved(None)`——错误不重试、不报警，是「尽力预热」的定位；(3) `cx.notify()` 让缓存就位后 UI 重渲染，而已打开的菜单因为是 `menu(...)` 惰性闭包构建，下次展开子菜单自然会读到新值。

- 查询的另一端——[git_store.rs:9329-9352](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/project/src/git_store.rs#L9329-L9352)：`Repository::default_branch(include_remote_name)` 返回 oneshot receiver；本地仓库走 git 后端，远程仓库走 `GetDefaultBranch` 协议请求。也就是说这个缓存在远程项目上还能省一次网络往返。
- 解析——[worktree_service.rs:43-53](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/git_ui_core/src/worktree_service.rs#L43-L53)：剥掉可选的 `refs/remotes/` 前缀后按第一个 `/` 切成 remote 名与分支名，任一段为空即失败。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：完整追踪 `prefetch_worktree_default_branches → prefetch_worktree_default_branch → worktree_default_branches` 的写入路径，画出状态迁移图。

**操作步骤**：

1. 从 [sidebar.rs:2012](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2012) 出发，确认预取在 `update_entries` 里的时序位置（在 `apply_list_state_diff` 之后、通知宿主之前）。
2. 通读 [sidebar.rs:2710-2737](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2710-L2737) 与 [sidebar.rs:2739-2770](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2739-L2770)，在代码旁用铅笔记下每个 `insert` / `contains_key` / `get` 的作用。
3. 画出 4.3.2 的状态机，并**给每条边标注触发它的代码行号**：
   - 缺失 → `Pending`：[sidebar.rs:2755-2756](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2755-L2756)；
   - `Pending` → `Resolved`：[sidebar.rs:2761-2766](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2761-L2766)；
   - 保持缺失（两个早退分支）：[sidebar.rs:2727-2734](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2727-L2734)（组内无打开工作区）与 [sidebar.rs:2751-2753](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2751-L2753)（无活跃仓库）。
4. 回答两个追问（答案写在你的笔记里）：
   - 为什么「无仓库」刻意不插入 `Pending`？（对照 [sidebar.rs:2745-2747](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2745-L2747) 的注释。）
   - 缓存会随分组移除而清理吗？（用 Grep 验证 `worktree_default_branches` 上没有任何 `remove`/`retain` 调用。）

**需要观察的现象**（若做本地实验）：在 [sidebar.rs:2755](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2755) 前临时加一条 `log::info!`，运行 Zed 打开一个 git 项目，应看到每个分组恰好一条日志；打开远端较慢的仓库时，先开子菜单只有当前分支条目，稍后重开才出现默认分支条目。

**预期结果**：状态机与边标注如 4.3.2 图；追问答案——不插入是为了给「仓库尚未加载完」留重试机会（一旦插入 `Pending` 就永远不会重试）；缓存不清理，属写 monotonic 的按键缓存。本地实验部分**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果用户在 `Pending` 状态下打开子菜单，会看到什么？之后缓存就位，子菜单会自己刷新吗？

**答案**：`Pending` 被读作 `None`（[sidebar.rs:2654-2659](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2654-L2659) 的 `_ => None`），`worktree_create_targets` 退化成只有当前分支一条。已构建的菜单条目不会热更新，但缓存写入时的 `cx.notify()` 会触发重渲染，且菜单内容闭包是打开时执行的——**关掉再开**就能看到默认分支条目。

**练习 2**：把这个缓存键从 `ProjectGroupKey` 换成「每个工作区实体各存一份」会有什么问题？

**答案**：同一分组的多个 worktree 共享同一默认分支，按工作区存会发出重复 git 查询（组内 N 个工作区 N 次查询），而且没有任何收益。按分组键存正好利用了 u2-l2 的分组语义：主 worktree 与 linked worktree 天然同键。

**练习 3**：`Resolved(None)` 与「缺失」在菜单上的表现一样（都只剩当前分支），那区分它们还有什么意义？

**答案**：意义在**是否重试**。缺失会在后续每次 `update_entries` 里重试（比如仓库马上要加载完了）；`Resolved(None)` 是终态，避免对「确实查不到默认分支」的仓库（无远程、 detached 等）反复发查询。这是用状态区分「还没问」和「问过了、没有」的经典手法。

### 4.4 render_project_header_ellipsis_menu：省略号菜单全景

#### 4.4.1 概念说明

「⋯」按钮（以及在整个分组头上**右键**，见 [sidebar.rs:2433-2443](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2433-L2443)，右键直接 toggle 同一个菜单句柄）打开的是分组级操作菜单。它管理的是「这个项目分组」本身：聚焦、开新窗口、打开/关闭组内工作区、排序、移除。

两个结构性选择值得注意：

1. 用 `ContextMenu::build_persistent` 而非 `build`——确认一条条目后菜单**不自动关闭**（语义见 [context_menu.rs:358-362](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/context_menu.rs#L358-L362)）。这对「Move Up/Move Down 连续排序」是必要的；也因此其他条目的处理器里要**手动** `cx.emit(DismissEvent)` 来关菜单。
2. 菜单数据在打开时现算（ reorder 状态、工作区列表、标签），保证反映最新的分组顺序——见 [sidebar.rs:2830-2838](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2830-L2838) 的注释。

#### 4.4.2 核心流程

```
打开菜单（按钮点击 或 分组头右键）:
    on_open: project_header_menu_ix = Some(ix)          # 记录哪个头的菜单开着
    menu 闭包:
        open_workspaces = workspaces_for_project_group(key)
        (group_index, total_groups) = 在 project_group_keys() 里的位置与总数
        构建条目（见下表）
    关闭（DismissEvent 订阅）: project_header_menu_ix = None
```

条目清单（按出现顺序）：

| # | 条目 | 出现条件 | 关联动作 | 点击效果 |
| --- | --- | --- | --- | --- |
| 0 | （end-slot 注册） | 总是 | `menu::SecondaryConfirm` | 为菜单接上次级确认交互（配合条目里的 ⌥-click 提示） |
| 1 | `Open Project in New Window` | 本地分组且窗口内 ≥2 个分组 | `workspace::MoveProjectToNewWindow` | `open_project_group_in_new_window(key)`，detach |
| 2 | `Focus Project` / `Focus Last Project`（按 `has_threads` 选词） | 总是（当前活跃分组时置灰不可选） | 无（custom entry，右侧渲染修饰键 + `-click` 提示） | 有工作区则 `activate_workspace`，否则 `open_workspace_for_group`；清空 selection 与 active_entry |
| 3 | 分隔线 + `Open Worktrees` 头 + 每个工作区一行 | 组内有打开的工作区 | 无（custom entry） | 行主体：标签列表（`•` 连接，活跃加 ✓）；点行 = `multi_workspace.activate(workspace)` 后手动关菜单；非活跃行尾部悬停 ✕（tooltip `Close Worktree`）= `multi_workspace.remove([ws], RemovalIntent::CloseProject)` 后关菜单 |
| 4 | `Move Up` | 窗口内 ≥2 个分组 | `MoveProjectUp` | `move_project_group_up(key)`，**不关菜单**（可连按） |
| 5 | `Move Down` | 同上 | `MoveProjectDown` | `move_project_group_down(key)`，不关菜单 |
| 6 | 分隔线 + `Remove` | 总是 | 无 | `remove_project_group(key)`，手动关菜单 |

条件变量：`show_multi_project_entries = host 为 None && 分组数 ≥ 2`；`show_reorder_entries = 分组数 ≥ 2`；`can_move_up = index > 0`；`can_move_down = index + 1 < total`。

#### 4.4.3 源码精读

- 入参与两个条件——[sidebar.rs:2772-2799](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2772-L2799)：函数收到 header 下标 `ix`、分组键、`is_active`、`has_threads`（后两者来自 `ListEntry::ProjectHeader` 的重建结果，u2-l1）；`show_multi_project_entries` 要求**本地**分组（远程分组开新窗口没有意义）。
- 触发器与打开记录——[sidebar.rs:2801-2818](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2801-L2818)：`PopoverMenu::with_handle` 绑定到 `project_header_menu_handles[ix]`（这就是右键能 toggle 它的原因）；`on_open` 把 `project_header_menu_ix` 置为 `Some(ix)`。该字段目前在本文件中只有写入（打开置位、关闭清零，[sidebar.rs:3120-3130](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L3120-L3130)），按钮的「菜单开着时常显」效果实际由 `menu_handle.is_deployed()` 驱动（[sidebar.rs:2799](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2799)、[sidebar.rs:2807](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2807)）。
- 打开时刻的数据采集——[sidebar.rs:2824-2855](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2824-L2855)：工作区列表、分组序号、活跃工作区、每个工作区的标签，全部在 `menu(...)` 闭包里现算；reorder 相关注释点名「在打开时刻计算以反映最新分组顺序」。
- `build_persistent` 与 end-slot——[sidebar.rs:2857-2861](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2857-L2861)：`end_slot_action(Box::new(menu::SecondaryConfirm))` 把次级确认动作接到菜单的 end-slot 处理上（该动作由 ui 的 `ContextMenu` 消费，见 [context_menu.rs:2344-2346](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/context_menu.rs#L2344-L2346)）。
- `Open Project in New Window`——[sidebar.rs:2862-2884](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2862-L2884)：条目上挂了 `workspace::MoveProjectToNewWindow` 动作（动作定义在 [multi_workspace.rs:60](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/workspace/src/multi_workspace.rs#L60)，这让它能显示键位提示），处理器调宿主的 `open_project_group_in_new_window` 并 `detach_and_log_err`。
- `Focus Project` 自定义条目——[sidebar.rs:2886-2945](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2886-L2945)：文案按 `has_threads` 二选一（有历史线程叫 `Focus Last Project`）；右侧用 `render_modifiers(&Modifiers::secondary_key(), ...)` 渲染平台相关的修饰键提示加 `-click`；`.selectable(!is_active)` 让当前活跃分组的这条置灰，处理器里 `if is_active { return; }` 双保险。
- `Open Worktrees` 区块——[sidebar.rs:2947-3063](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2947-L3063)：每行一个工作区，标签渲染与 4.1 一致；非活跃行尾部有悬停可见的 ✕（[sidebar.rs:3005-3041](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L3005-L3041)），点击先 `stop_propagation` 再调 `multi_workspace.remove(..., RemovalIntent::CloseProject, ...)` 并发出 `DismissEvent` 关菜单；行主体点击（[sidebar.rs:3044-3058](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L3044-L3058)）调 `multi_workspace.activate` 后同样手动关菜单。
- `Move Up` / `Move Down`——[sidebar.rs:3065-3104](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L3065-L3104)：`ContextMenuEntry::new(...).action(...).disabled(...)` 挂动作与禁用态；处理器调宿主排序方法后**不**发 `DismissEvent`——这就是选 `build_persistent` 的原因，用户可以连按把分组一路挪到目标位置。
- `Remove` 与收尾——[sidebar.rs:3106-3117](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L3106-L3117)：调 `remove_project_group` 后关菜单；[sidebar.rs:3120-3130](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L3120-L3130) 通过 `window.subscribe` 订阅菜单实体的 `DismissEvent`，把 `project_header_menu_ix` 清回 `None`。

#### 4.4.4 代码实践

**实践目标**：亲手产出上面 4.4.2 的菜单项清单（规格任务的后半部分），并验证条件分支。

**操作步骤**：

1. 只读源码，不看 4.4.2 的表，自己从 [sidebar.rs:2857](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2857) 读到 [sidebar.rs:3117](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L3117)，按出现顺序记录：条目标题、出现条件、绑定的动作（`action(...)` 参数或无）、处理器最终调用的宿主方法、确认后菜单是否保持打开。
2. 用三个假想窗口状态核对条件列：单分组窗口、双分组本地窗口、单分组远程（SSH）窗口——逐个判断条目 1 和 4/5 是否出现。
3. 把你的表与 4.4.2 对照，标出遗漏或理解偏差。

**需要观察的现象**：`build_persistent` 带来的行为差异——排序条目连按时菜单不关，而 Remove/工作区行点击后菜单立刻关。

**预期结果**：与 4.4.2 的表一致；单分组窗口没有条目 1/4/5（两个条件都要求 ≥2 分组）；远程分组即使多分组也没有条目 1（`host().is_none()` 不满足）。交互细节（连按不关菜单）**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Move Up/Down` 之后菜单不关，而 `Remove` 之后要手动关？

**答案**：排序是典型的「可能连续重复」操作（把分组从第 5 挪到第 1 要按 4 次 Move Up），每次都关菜单会很折磨；`Remove` 则是一次性动作，且移除后分组已不存在，菜单继续开着没有对象可言。代码上前者不发 `DismissEvent`（依赖 `build_persistent` 的不关闭语义），后者显式发出（[sidebar.rs:3116](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L3116)）。

**练习 2**：`Focus Project` 条目为什么同时用 `.selectable(!is_active)`（不可选）和处理器里的 `if is_active { return; }`（早退）两道防线？

**答案**：`.selectable(false)` 是 UI 层置灰（标签用 `Color::Disabled` 渲染，键盘/鼠标都无法确认），但菜单的确认路径不止一条（主确认、次级确认 `SecondaryConfirm` 等），处理器内的早退保证即使从某条未置灰的路径触发也不会执行任何激活逻辑。防御性双保险在菜单这种多入口组件里很常见。

**练习 3**：`Open Worktrees` 行里的关闭按钮（✕）为什么先 `cx.stop_propagation()` 和 `window.prevent_default()`？

**答案**：✕ 按钮嵌在整行可点击的 custom entry 内部。不阻止传播的话，点 ✕ 会同时触发外层行的「激活该工作区」处理器——关掉一个工作区的瞬间又把它激活。`stop_propagation` 切断事件冒泡，`prevent_default` 阻止默认行为（这里是防止 PopoverMenu 把点击当作「点在菜单外」而提前收起菜单等默认处理），确保只有关闭逻辑执行。

### 4.5 create_worktree_in_workspace 与 connect_remote：把工作交给正确的 crate

#### 4.5.1 概念说明

本讲到这里出现了两类「侧边栏不该自己做」的事：

1. **创建 git worktree**——涉及 git 子进程、fetch 远程、信任传播、打开新工作区、恢复布局，这些全部住在 `git_ui_core::worktree_service`。
2. **建立 SSH 远程连接**——涉及连接池、密码/认证模态框，住在 `remote_connection` crate，且连接 UI 需要挂在一个具体的 workspace 上。

侧边栏对这两件事各写了一个薄适配函数，把「Zed 侧的入口参数」翻译成「服务方要的参数」。这正是 u1-l1 说的分层原则的落地：sidebar 组合别人，不重复实现。

#### 4.5.2 核心流程

**创建 worktree**（从 4.2 的子菜单条目进入）：

```
点击 "Based on origin/main"
  → create_worktree_in_workspace(workspace, branch_target)
      → workspace.update:
          focused_dock = workspace.focused_dock_position()      # 保底 dock 位置
          git_ui_core::worktree_service::handle_create_worktree(
              workspace, &CreateWorktree { worktree_name: None, branch_target },
              window, focused_dock, cx )
  → handle_create_worktree 内部：创建 worktree 工作区、打开、恢复布局，
    错误经 toast 呈现（任务 detach_and_log_err）
```

其中 `branch_target` 是 `WorktreeCreateTarget::branch_target()` 把展示用的 target 翻译成动作参数 `NewWorktreeBranchTarget`（`CurrentBranch` / `RemoteBranch { remote_name, branch_name }`，见 [worktree_service.rs:69-80](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/git_ui_core/src/worktree_service.rs#L69-L80)）。

**远程连接**（从「打开一个关闭的远程分组」进入）：

```
用户点击远程分组的 Focus Project / 新建线程 等
  → open_workspace_for_group / open_workspace_and_create_entry（sidebar 侧）
      → multi_workspace.find_or_create_workspace(paths, host, ...,
            connect_remote = |options, window, cx|
                connect_remote(active_workspace, options, window, cx), ...)
      → 宿主发现没有现成工作区且 host 存在
          → 调用传入的 connect_remote(options, ...)
              → remote_connection::connect_with_modal(modal_workspace, options, ...)
                  · 已有活动连接：直接复用连接池
                  · 否则：在 modal_workspace 上弹 RemoteConnectionModal，返回连接 Task
          → 连接成功后创建远程 Project 与 Workspace
  → sidebar 侧的 spawn 任务完成后 dismiss_connection_modal 收掉模态框
```

关键点：`find_or_create_workspace` 把「连接 UI 长什么样」设计成**回调参数**，由调用方注入。宿主不该知道模态框，sidebar（调用方）不该自己管连接池——回调节点让两边解耦。

#### 4.5.3 源码精读

- `connect_remote` 适配器——[sidebar.rs:688-699](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L688-L699)：doc comment 直说它「适合作为 `MultiWorkspace::find_or_create_workspace` 的 `connect_remote` 参数」。注意第一个参数 `modal_workspace`：连接模态框要挂在**当前活跃的**工作区上（远处那个还没建出来）。
- `create_worktree_in_workspace` 适配器——[sidebar.rs:708-728](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L708-L728)：注释说明它镜像 worktree picker 的「Create new worktree」行为；`worktree_name: None` 表示让 Zed 随机生成新 worktree 名（字段语义见 [zed_actions/src/lib.rs:321-325](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/zed_actions/src/lib.rs#L321-L325) 的 `CreateWorktree`）。
- 公共词汇 `NewWorktreeBranchTarget`——[zed_actions/src/lib.rs:298-314](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/zed_actions/src/lib.rs#L298-L314)：三个变体描述「新 worktree 基于哪个 ref」；doc 强调**总是以 detached HEAD 状态创建**，要分支得进 worktree 后自己建。
- 服务端实现——[worktree_service.rs:683-706](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/git_ui_core/src/worktree_service.rs#L683-L706)：`handle_create_worktree` 是「不牵涉 agent 面板」的通用入口，错误经 toast 呈现、返回的工作区句柄被丢弃；同文件还有给 agent 工具用的 `create_worktree_workspace`（返回 `Task`，拿得到新工作区句柄）——两者的分工写在 doc comment 里，可顺带对照。
- 宿主的回调契约——[multi_workspace.rs:1098-1134](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/workspace/src/multi_workspace.rs#L1098-L1134)：签名里 `connect_remote: impl FnOnce(RemoteConnectionOptions, &mut Window, &mut Context<Self>) -> Task<Result<Option<Entity<RemoteClient>>>>`；doc（[multi_workspace.rs:1090-1097](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/workspace/src/multi_workspace.rs#L1090-L1097)）写明该闭包「负责所有用户可见的连接 UI（如密码提示）」，返回 `None` 表示用户取消。本地项目（`host` 为 `None`）根本不会调它（[multi_workspace.rs:1120-1130](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/workspace/src/multi_workspace.rs#L1120-L1130) 走本地分支）。
- sidebar 侧的注入点——`connect_remote` 在本文件共出现 5 处实参传递：[sidebar.rs:1264-1275](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1264-L1275)（`open_workspace_for_group`）、[sidebar.rs:1302-1314](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1302-L1314)（`open_workspace_and_create_entry`），以及另外三个打开分组的工作区并接续操作的场景（[sidebar.rs:3977](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L3977)、[sidebar.rs:4610](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L4610)、[sidebar.rs:4925](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L4925)，分别用于激活已关闭条目、归档后重开等）。收尾的 `dismiss_connection_modal` 见 [sidebar.rs:1277-1283](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1277-L1283)：连接任务结束后（无论成败）把模态框收掉。
- 连接服务本体——[remote_connection.rs:560-576](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/remote_connection/src/remote_connection.rs#L560-L576)：`connect_with_modal` 先查 `has_active_connection`，有则 `connect_reusing_pool`（老连接直接复用，连模态框都不弹）；没有才在给定工作区上 `toggle_modal` 弹出 `RemoteConnectionModal`。

#### 4.5.4 代码实践

**实践目标**：沿一条完整链路走一遍「委托」，体会回调参数如何解耦两个 crate。

**操作步骤**：

1. 场景设定：一个 SSH 远程项目分组，其下工作区全部关闭，侧边栏只剩 `Closed` 行。用户打开省略号菜单点 `Focus Project`。
2. 从 [sidebar.rs:2919-2943](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2919-L2943) 的处理器出发：`workspace_for_group` 找不到 → `open_workspace_for_group`。
3. 逐步追进 [sidebar.rs:1247-1284](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1247-L1284) → [multi_workspace.rs:1115-1140](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/workspace/src/multi_workspace.rs#L1115-L1140) → [remote_connection.rs:560-576](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/remote_connection/src/remote_connection.rs#L560-L576)，记录每一跳的文件与行号。
4. 回答：这条链路上，谁负责弹密码框？谁负责连接池？谁负责创建远程 Project？侧边栏自己做了什么？

**需要观察的现象**：若在本地用 SSH 项目实验（`cargo run -p zed` 后连一个远程目录并关闭其全部工作区再点 Focus Project），应看到连接模态框只在「没有活动连接」时出现。

**预期结果**：侧边栏只做了三件事——决定「现在要打开这个分组」、提供 `connect_remote` 回调（把当前活跃工作区作为模态框宿主）、在任务结束后收掉模态框；弹密码框与连接池归 `remote_connection`，建远程 Project 与 Workspace 归 `MultiWorkspace`。实验部分**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`find_or_create_workspace` 为什么不自己 import `remote_connection` 弹模态框，而要接收一个回调？

**答案**：workspace crate 若直接依赖连接 UI，就得知道「模态框该挂在哪个工作区上」这类**调用方才知道**的上下文，而且测试宿主时还得连带起一套远程连接栈。回调把「何时连接」留在宿主、「如何呈现」交给调用方，`connect_remote` 的 doc comment 明说闭包负责全部用户可见的连接 UI。这是控制反转在实体 API 上的典型应用。

**练习 2**：`create_worktree_in_workspace` 里为什么要先取 `focused_dock_position` 再传给 `handle_create_worktree`？

**答案**：`handle_create_worktree` 通用入口的签名接收 `fallback_focused_dock`（保底聚焦 dock 位置）。在 workspace 上下文里取一次当前聚焦 dock 传进去，让新工作区打开后的焦点落位有依据；侧边栏作为调用方只是搬运这个信息，不参与决策。这属于「适配器只做参数翻译，不做策略」的自觉。

**练习 3**：如果 `connect_with_modal` 返回的 Task 解析为 `Ok(None)`，链路会发生什么？

**答案**：`None` 约定为「用户取消了连接」（契约见 [multi_workspace.rs:1093-1097](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/workspace/src/multi_workspace.rs#L1093-L1097)）。宿主的后续 spawn 任务会把它转成 `Err(anyhow!("Remote connection was cancelled"))`，sidebar 侧 `detach_and_log_err` 记录日志，不创建任何工作区；`dismiss_connection_modal` 照常把模态框收掉。侧边栏不需要为「取消」写任何特判——错误一路传播即可。

## 5. 综合实践

把本讲四个模块串成一份**「分组头菜单规格文档」**，作为你自己的学习产出：

1. **缓存生命周期图**：完成 4.3.4 的状态机（含每条边的代码行号），并补一句「为什么这是全量重推导教义的合法例外」。
2. **菜单清单**：完成 4.4.4 的省略号菜单条目表，再仿照它为 4.2 的 `New Thread In…` 菜单做一张同构的表（条目、条件、动作、效果）。
3. **委托地图**：完成 4.5.4 的调用链笔记，标注每一步跨越的 crate 边界（sidebar → workspace → remote_connection；sidebar → git_ui_core → zed_actions 词汇）。
4. **（可选，本地实验）**：运行 `cargo run -p zed`，打开一个多 worktree 的 git 项目，逐项核对你两张表里的条目与出现条件；在 [sidebar.rs:2755](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2755) 前临时加 `log::info!("prefetch default branch for {:?}", key)` 并用 `RUST_LOG=sidebar=info cargo run -p zed` 观察，验证「每分组恰好预热一次、失败不重试」。（此步**待本地验证**，做完记得还原。）

需要说明：`sidebar_tests.rs` 中没有直接覆盖这两个菜单或预取逻辑的测试（用 Grep 搜 `ellipsis`、`default_branch`、`worktree_create` 均无命中），所以本讲的验证只能靠源码推演 + 本地 UI 实验，这也提示这两块是潜在的测试补强点。

## 6. 本讲小结

- `workspace_menu_worktree_labels` 在菜单打开时现算每个工作区的标签：多根才显示文件夹名，git worktree 显示 `主名 / worktree 名`（linked 用短名、主仓用 `main`），无仓库降级为纯文件夹名。
- 「+」按钮按「组内有无打开工作区」分两支：空组直接新建；非空弹 `New Thread In…` 菜单（每工作区一条 + 可选 `Create New Worktree…` 子菜单），子菜单选项由 `worktree_create_targets` 统一推导，与 worktree picker 共用同一套规则。
- `DefaultBranchCache` 用「缺失 / Pending / Resolved」三态把异步 git 查询挪出菜单路径：`update_entries` 末尾预热，菜单同步读缓存；无仓库时刻意保持缺失以便重试，`Resolved(None)` 是不重试的终态；缓存按 `ProjectGroupKey` 存、永不淘汰。
- 省略号菜单用 `build_persistent`（确认后不关）支撑 `Move Up/Down` 连续排序，其余条目（Remove、工作区行、✕ 关闭）手动发 `DismissEvent` 收起；条目数据全部在打开时刻现算。
- 侧边栏对跨 crate 能力只写薄适配器：`create_worktree_in_workspace` 转发给 `git_ui_core` 的 `handle_create_worktree`，`connect_remote` 把「弹连接模态框」作为回调注入 `MultiWorkspace::find_or_create_workspace`——自己不实现 git 操作也不碰连接池。

## 7. 下一步学习建议

本讲结束了单元四（渲染层）。下一讲 u5-l1《动作注册与键位上下文》将从「菜单里看到的动作」进入「动作系统本身」：本讲条目上挂的 `MoveProjectUp`、`workspace::MoveProjectToNewWindow`、`menu::SecondaryConfirm` 都会回到 `gpui::actions!`、`dispatch_context` 与键位分发的框架下重新理解。建议带着一个问题去读：省略号菜单里 `.action(...)` 挂的动作和处理器是两套东西，它们各自在什么时机被触发？

若想继续深挖本讲的支线，推荐顺读 `crates/git_ui_core/src/worktree_service.rs` 中 `create_worktree_workspace_inner` 的实现（fetch 远程、信任传播、布局恢复都在那里），以及 u8-l2 会讲到的归档流水线——那里同样大量出现「侧边栏只编排、服务方干活」的模式。
