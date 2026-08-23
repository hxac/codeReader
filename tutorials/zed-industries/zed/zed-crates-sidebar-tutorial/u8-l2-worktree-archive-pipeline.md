# u8-l2 工作树归档流水线

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清「归档一个线程」在 sidebar crate 里远不止把数据库里的 `archived` 翻成 `true`——它可能连带**删除空草稿、移除整个工作区、关闭混合工作区里的编辑器条目、把 linked worktree 的 git 状态存档、最后从磁盘上删掉整个工作树目录**。
2. 读懂归档「根」（`RootPlan`）的推导过程：什么样的目录才有资格被归档（Zed 创建的、位于托管目录内的 linked worktree），以及谁会「阻塞」一次归档（`thread_blocks_worktree_archive`）。
3. 理解整条流水线里严格的**先后顺序约束**：为什么根推导必须发生在工作区移除之前、为什么空草稿删除发生在工作区移除之后、为什么归档磁盘任务要在 `store.archive` 之前启动。
4. 区分两种归档任务：绑定线程的 `start_archive_worktree_task`（可回滚、可取消、失败自动反归档）与脱离线程的 `start_detached_archive_worktree_task`（终端与草稿专用，失败只记日志）。
5. 掌握 `archive_worktree_roots` 的「持久化 → 删除 → 逐级回滚」事务结构。

本讲是 sidebar crate 生命周期逻辑的巅峰，也是前几讲知识（`ThreadEntryWorkspace::Closed`、`PathList`、`ProjectGroupKey`、`remove_workspaces_then`、草稿体系）的总汇流点。

## 2. 前置知识

### 2.1 git linked worktree 与主仓库

一个 git 仓库可以有多个「工作树」（worktree）：

- **主工作树**（main worktree）：你 `git clone` 下来的那个目录，`.git` 是一个真实的目录。
- **linked worktree**：用 `git worktree add` 额外挂出来的目录，它的 `.git` 只是一个**文件**，内容形如 `gitdir: /主仓库/.git/worktrees/名字`，指回主仓库的元数据。

git 拒绝删除主工作树，所以本流水线只处理 linked worktree。Zed 自己会为 Agent 线程创建 linked worktree，放在由 `git.worktree_directory` 设置决定的**托管目录**里（默认在仓库旁边的 `../worktrees` 一带，见 [sidebar.rs](../crates/sidebar/src/sidebar.rs) 引用的 `worktrees_base_for_repo`，位于 [thread_worktree_archive.rs:86-93](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L86-L93)）。用户手动创建、或放在托管目录之外的 worktree **一律不动**。

### 2.2 归档要解决的矛盾：删目录 vs 不丢代码

把 worktree 目录删掉是容易的，难的是**删了之后还能恢复**。解决方案分三层：

1. **WIP 提交对**：归档时在 worktree 上创建两个**游离提交**（detached commit，不移动任何分支）——一个捕获暂存区状态（parent 是 HEAD），一个捕获全部文件含未跟踪状态（parent 是暂存提交）。
2. **数据库记录**：把两个提交的 SHA、分支名、主仓库路径写进 `ThreadMetadataStore` 的 `archived_git_worktree` 表，并把所有引用该 worktree 的线程链接到这条记录。
3. **防 GC 引用**：在主仓库上创建 `refs/archived-worktrees/{id}` 指向未暂存提交。没有这个 ref，`git gc` 迟早把游离提交收走，恢复时会静默失败。

u8-l1 讲过的归档视图（ThreadsArchiveView）的「恢复」功能，正是靠这三层记录反向执行：重建 worktree、切回分支、用 `read-tree` 还原暂存/未暂存状态。

### 2.3 复习：本讲直接用到的旧概念

| 概念 | 出处 | 本讲用途 |
|---|---|---|
| `ThreadEntryWorkspace::Closed { folder_paths, project_group_key }` | u2-l2 | 已关闭线程的归档需要先「补开」工作区 |
| `PathList` | u2-l2 | 线程的 folder 路径集合，逐路径推导归档根 |
| `ProjectGroupKey` | u2-l2 | 判断线程的 folder 是否等于分组主路径（相等则不是 linked worktree 场景） |
| `remove_workspaces_then` | u6-l2 | 把「移除工作区」的异步流程与后续副作用串联起来 |
| 草稿 = `session_id` 为空的线程行 | u6-l3 | 空草稿是唯一「不阻塞归档」的其他线程 |
| `restoring_tasks` / 任务 drop 即取消 | u6-l1 / u8-l1 | 同款任务生命周期管理思想 |

### 2.4 「in-flight 任务 + 取消信号」模式

`start_archive_worktree_task` 返回一个二元组 `(Task<()>, async_channel::Sender<()>)`：

- `Task<()>`：后台磁盘归档任务。**谁持有它，谁就能通过 drop 取消它**。
- `Sender<()>`：取消信号线。`async_channel` 的语义是：**当所有 Sender 被 drop 时，Receiver 的 `is_closed()` 变为 true**。后台任务在每个关键步骤前检查这个标志，发现关闭就走回滚路径。

这个二元组会被存进 `ThreadMetadataStore` 的 `in_flight_archives: HashMap<ThreadId, (Task<()>, Sender<()>)>`（[thread_metadata_store.rs:509](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_metadata_store.rs#L509)）：用户反归档时 drop 它们即可中断进行中的磁盘删除——这就是 u8-l1 里「用户发起恢复可取消任务」的底层机制。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [crates/sidebar/src/sidebar.rs](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs) | 主战场：归档判定、编排、级联清理全部在此 |
| [crates/agent_ui/src/thread_worktree_archive.rs](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs) | 磁盘层：`RootPlan` 推导、git 状态持久化、worktree 删除与回滚 |
| [crates/agent_ui/src/thread_metadata_store.rs](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_metadata_store.rs) | `archive`/`unarchive`/`cleanup_completed_archive` 与 in-flight 任务表 |
| [crates/agent_ui/src/draft_prompt_store.rs](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/draft_prompt_store.rs) | `draft_has_user_content`：判断草稿是否有用户内容（阻塞判据之一） |
| [crates/sidebar/src/sidebar_tests.rs](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs) | 两个关键测试锁定整条流水线的行为契约 |

sidebar.rs 内的关键函数（全部为 `impl Sidebar` 私有方法）速查：

| 函数 | 行号 | 一句话职责 |
|---|---|---|
| `archive_selected_thread` | 5637 | 动作入口：按行类型分流 |
| `archive_thread` | 5248 | 线程归档主编排（本讲主角） |
| `archive_and_activate` | 5436 | 落锤：标记归档 + 交接活跃条目 |
| `should_load_closed_workspace_for_archive` | 4635 | 已关闭工作区是否需要先补开 |
| `open_workspace_for_archive` | 4906 | 补开工作区后递归重入归档 |
| `roots_to_archive_for_paths` | 4713 | 推导要删盘的 worktree 根列表 |
| `thread_blocks_worktree_archive` | 4862 | 单线程是否阻塞归档 |
| `linked_worktree_workspace_to_remove` | 4751 | 归档后是否要移除整个工作区 |
| `close_items_for_archived_worktrees` | 5170 | 混合工作区里关条目 / 纯归档工作区整删 |
| `delete_empty_drafts_for_archive_*` | 4807-4860 | 删除空草稿元数据 |
| `remove_workspaces_then` | 5128 | 异步移除工作区后再执行收尾（u6-l2 已讲） |
| `start_archive_worktree_task` | 5511 | 启动绑定线程的磁盘归档任务 |
| `start_detached_archive_worktree_task` | 5548 | 启动脱离线程的磁盘归档任务 |
| `archive_worktree_roots` | 5573 | 磁盘归档的事务体（persist → remove → rollback） |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：

1. 入口与主编排：`archive_selected_thread` → `archive_thread`
2. 归档根的推导：`roots_to_archive_for_paths` 与 `build_root_plan`
3. 阻塞判据与三道防线：`thread_blocks_worktree_archive` 等
4. 级联清理：空草稿删除、工作区移除与混合工作区关条目
5. 磁盘事务与两种任务形态：`archive_worktree_roots` 家族与 `archive_and_activate`

---

### 4.1 入口与主编排：archive_selected_thread → archive_thread

#### 4.1.1 概念说明

用户在侧边栏选中一行后按归档键（或点行尾的归档按钮），触发 `ArchiveSelectedThread` 动作。入口函数先做**行类型分流**：

- 分组头 / 无选中：无事可做。
- 线程行且状态为 `Running` / `WaitingForConfirmation`：直接返回——正在干活的线程不许归档。
- 线程行且是**草稿**：走 u6-l3 讲过的 `remove_draft`（草稿没有「归档」语义，只有删除；但其内部同样复用本讲的流水线，见 4.4）。
- 线程行且有 `session_id`：走本讲主角 `archive_thread`。
- 终端行：走 u6-l2 讲过的 `close_terminal`。

#### 4.1.2 核心流程

`archive_thread(session_id)` 的主干可以概括为六步伪代码：

```text
archive_thread(session_id):
    1. 收集身份材料：
       metadata   ← ThreadMetadataStore.entry_by_session(session_id)
       thread_id  ← metadata.thread_id（数据库为权威，行数据兜底）
       folder_paths ← metadata.folder_paths()（三级兜底：store → 行 → 活跃工作区根路径）

    2. 若行的工作区是 Closed 且 should_load_closed_workspace_for_archive(...)：
       open_workspace_for_archive(folder_paths, group_key,
           then = |this| { this.update_entries(); this.archive_thread(session_id) })
       return    # 异步补开后递归重入，本次调用到此为止

    3. roots_to_archive ← roots_to_archive_for_paths(folder_paths, …, except=thread_id)
       # ↑ 必须在移除任何工作区之前完成（见源码注释）

    4. neighbor ← neighboring_activatable_entry(当前行下标)   # 接班条目
       workspace_to_remove ← linked_worktree_workspace_to_remove(folder_paths, …)

    5. workspaces_to_remove ← [workspace_to_remove?]
       close_item_tasks ← close_items_for_archived_worktrees(roots, &mut workspaces_to_remove)

    6. remove_workspaces_then(workspaces_to_remove, close_item_tasks, finish = |this| {
           若确实移除了工作区: delete_empty_drafts_for_archive_paths(folder_paths)
           in_flight ← start_archive_worktree_task(thread_id, roots)   # 内部还会删一轮空草稿
           archive_and_activate(session_id, thread_id, neighbor, …, in_flight)
       })
```

注意第 2 步的**递归结构**：补开工作区是异步的，完成后在回调里重新调用 `archive_thread` 自己——此时行的工作区已是 `Open`，跳过第 2 步继续往下走。这和 u6-l1 里 `activate_thread` 对 Closed 行的「先开工作区再递归收敛」是同一个模式。

#### 4.1.3 源码精读

动作入口与状态守卫——[sidebar.rs:5637-5660](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5637-L5660)：先取 `selection`；线程行里 `Running | WaitingForConfirmation` 状态直接 `return`（正在运行的线程不可归档）；`draft.is_some()` 走 `remove_draft`；否则取 `session_id` 进入 `archive_thread`。该动作经 `.on_action(cx.listener(Self::archive_selected_thread))` 注册在渲染根容器上（[sidebar.rs:7791](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7791)），线程行尾部的归档按钮（Archive 图标 IconButton）最终也落到同一动作（[sidebar.rs:6278-6293](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L6278-L6293)）。

身份材料的三级兜底——[sidebar.rs:5254-5293](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5254-L5293)：`thread_id` 优先从全局 Store 按 `session_id` 查（数据库是权威），查不到再从当前列表行取；`folder_paths` 依次尝试 metadata → 行数据 → 活跃工作区的 `root_paths`。这是因为归档可能发生在行数据尚未刷新到最新状态的瞬间。

Closed 行的补开分支——[sidebar.rs:5295-5323](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5295-L5323)：条件是「行的工作区为 Closed」且 `should_load_closed_workspace_for_archive` 通过（判定见 4.3.3）。回调里先 `update_entries` 让新开的工作区进入列表世界，再**递归调用** `archive_thread`。

根推导必须前置的注释——[sidebar.rs:5325-5341](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5325-L5341)：源码注释明确写着「这必须发生在我们从 MultiWorkspace 移除任何工作区之前，因为 `build_root_plan` 需要当前打开的工作区来找到受影响的 project 与 repository 句柄」——**实体一旦被移除销毁，就再也查不回来了**，这是整条流水线最关键的顺序约束。

编排收尾——[sidebar.rs:5343-5417](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5343-L5417)：依次算接班条目 `neighbor`（u5-l2 讲过的「同组优先、向下优先」规则）、算待移除工作区、收集混合工作区关条目任务，最后把所有副作用押进 `remove_workspaces_then` 的 `finish` 闭包。`finish` 里的顺序是：删空草稿（仅当工作区真的被移除了）→ 启动磁盘归档任务 → `archive_and_activate` 落锤。

#### 4.1.4 代码实践

**实践目标**：用肉眼走通 `archive_thread` 的调用图，确认「副作用全部押后」的结构。

**操作步骤**：

1. 打开 [sidebar.rs:5248-5418](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5248-L5418)，用三种颜色分别标注：a) 纯读取（`read`/`find`/`position`）；b) 同步预计算（`roots_to_archive_for_paths`、`linked_worktree_workspace_to_remove`、`close_items_for_archived_worktrees` 的分类部分）；c) 副作用（`remove_workspaces_then` 闭包内的四行）。
2. 统计：从函数入口到 `remove_workspaces_then` 调用之前，有几次 `update`/写操作？（预期答案：0 次，除了 Closed 分支里的 `open_workspace_for_archive`。）
3. 在本地给 `finish` 闭包内的三步各加一条 `log::info!`（练习用，不提交），运行 `cargo test -p sidebar --lib test_archive_selected_thread_archives_closed_linked_worktree`，观察日志顺序。

**需要观察的现象**：三条日志按「删空草稿 → 启动归档任务 → archive_and_activate」顺序出现，且都出现在工作区移除完成之后。

**预期结果**：确认本函数是「先算后动」的纯编排层；若第 3 步本地未运行，标注**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `archive_selected_thread` 对 `Running` 状态的线程直接返回，而不是排队等它结束再归档？

**答案**：运行中的线程持有活跃的 agent 会话，归档语义（后续可能删盘）与会话的写入需求直接冲突；排队还会引入「归档请求过期」问题（用户可能改主意）。Zed 选择最简单的契约：想归档先等它停下来。

**练习 2**：`archive_thread` 里 `thread_folder_paths` 为什么要做三级兜底？只信 `metadata` 不行吗？

**答案**：`archive_thread` 的入口之一是 Closed 行补开工作区后的**递归重入**，另一个入口是行尾按钮点击。列表行数据是上一次 `rebuild_contents` 的快照，可能与 Store 有瞬时不一致；而 metadata 理论上总在，但防御性编程要求在它意外缺失时（如竞态下被并发归档）仍能从行数据或活跃工作区拿到路径材料。三级兜底保证归档判定尽量不因数据缺口而静默失效。

**练习 3**：递归重入 `archive_thread` 时，为什么回调里要先 `update_entries`？

**答案**：补开的工作区携带新的 `Entity<Workspace>` 与 git 仓库句柄；`roots_to_archive_for_paths` 和 `linked_worktree_workspace_to_remove` 都依赖「当前打开的工作区集合」做现查。先 `update_entries` 让列表世界（含行的 `workspace` 字段从 Closed 变 Open）与新现实同步，后续判定才成立。

---

### 4.2 归档根的推导：roots_to_archive_for_paths 与 build_root_plan

#### 4.2.1 概念说明

「归档根」= 归档这个线程后**可以从磁盘删除的 worktree 目录**。sidebar 侧的 `roots_to_archive_for_paths` 是过滤器，agent_ui 侧的 `build_root_plan` 是资格判定器，两者合作产出 `Vec<RootPlan>`。

`RootPlan` 是一张**提前抓拍的快照**：因为后续的 persist（git 操作）和 remove（删盘）都是异步的，而且中间隔着工作区移除——那时 project / repository 实体可能已销毁——所以必须在一切开始前，把所需句柄与路径全部同步收集进一个纯数据结构。

#### 4.2.2 核心流程

sidebar 侧过滤逻辑（伪代码）：

```text
roots_to_archive_for_paths(folder_paths, remote, except_thread_id, except_terminal_id):
    workspaces ← archive_workspaces()        # 本窗口 + 全局所有窗口的工作区
    对 folder_paths.ordered_paths() 中的每个路径 p:
        plan ← build_root_plan(p, remote, workspaces)      # 资格判定，None 则跳过
        若 p 仍被其他「阻塞型」未归档线程引用 → 丢弃 plan
        若 p 仍被其他终端引用（except_terminal_id 除外）→ 丢弃 plan
        收集 plan
```

资格判定 `build_root_plan` 的四道门槛（任一不过即 `None`）：

1. 至少一个打开的 Project 加载了该路径作为可见 worktree（`affected_projects` 非空）；
2. 能找到一个 `Repository` 实体，其快照表明该路径是 **linked worktree**（主工作树直接出局）；
3. 路径位于全局 `git.worktree_directory` 设置推导出的**托管目录**内；
4. 该 worktree 在 `created_worktrees` 注册表中有 Zed 记录（`recorded_created_at`），即确实是 **Zed 创建的**。

形式化地，记 \( U(p) \) 为引用路径 \( p \) 的未归档线程集合，\( t_0 \) 为本次归档对象，则：

\[ \text{root}(p) \text{ 可归档} \iff \text{build\_root\_plan}(p) \neq \varnothing \;\wedge\; B(p) = \varnothing \;\wedge\; \neg \text{terminal\_refs}(p) \]

其中 \( B(p) = \{ t \in U(p) \setminus \{t_0\} \mid \text{blocks}(t) \} \)，`blocks` 的定义见 4.3。

#### 4.2.3 源码精读

`RootPlan` 结构体——[thread_worktree_archive.rs:31-61](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L31-L61)：七个字段各司其职——`root_path`（要删的目录）、`main_repo_path`（主仓库，用于建防 GC ref 和 `git worktree remove`）、`affected_projects`（所有加载了该 worktree 的 Project 及其 WorktreeId，删除时要逐个释放）、`worktree_repo`（Repository 实体句柄，persist 时跑 git 命令）、`branch_name`（恢复时切回分支，None 表示 detached HEAD）、`remote_connection`（远程场景建临时 project 用）、`recorded_created_at`（创建时间戳，删除前复核「还是不是 Zed 创建的那个目录」）。结构体上方的文档注释（[thread_worktree_archive.rs:21-30](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L21-L30)）点明了「所有字段都在 worktree 尚未卸载时同步收集，因为工作区移除会拆掉 project 与 repository 实体」。

sidebar 侧的过滤器——[sidebar.rs:4713-4749](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4713-L4749)：`filter_map` 里调 `build_root_plan` 做资格判定；第一个 `filter` 排除仍被其他阻塞型未归ived线程引用的根（`path_is_referenced_by_unarchived_threads_for_archive`，阻塞谓词就是 4.3 的 `thread_blocks_worktree_archive`）；第二个 `filter` 排除仍被终端引用的根。工作区集合来自 `archive_workspaces`——[sidebar.rs:4692-4695](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4692-L4695)，它委托 [thread_worktree_archive.rs:928-947](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L928-L947) 的 `workspaces_for_archive`：本窗口 MultiWorkspace 的工作区，**并上其他所有窗口的工作区**——因为同一 worktree 可能同时挂在多个窗口的 project 里，任何一个没释放都会让 `git worktree remove` 失败（`AffectedProject` 的文档注释，[thread_worktree_archive.rs:63-75](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L63-L75)，专门解释了这一点）。

资格判定的四道门槛——[thread_worktree_archive.rs:112-206](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L112-L206)：先按远程身份过滤并收集 `affected_projects`（[L127-L146](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L127-L146)，本地 `/project` 与远程 `/project` 不会混淆）；空则返回 None；再在各 project 的仓库里找 `is_linked_worktree() && work_directory_abs_path == path` 的 Repository（[L150-L173](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L150-L173)）——注释强调「只有 linked worktree 能被 `git worktree remove` 删，主工作树必须留下」；然后检查路径前缀是否落在托管目录内（[L178-L181](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L178-L181)，注意读的是**全局**设置而非项目局部覆盖，因为 Zed 创建 worktree 时用的就是全局值）；最后查 `created_worktrees` 注册表拿 `recorded_created_at`（[L189-L190](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L189-L190)）——目录在托管目录内但没注册记录的，是用户手动建的，同样不动。

#### 4.2.4 代码实践

**实践目标**：验证四道资格门槛的行为边界。

**操作步骤**：

1. 阅读 agent_ui 内的五个 `build_root_plan` 单测（它们就住在 [thread_worktree_archive.rs:994-1369](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L994-L1369) 的 `#[cfg(test)]` 模块里）：
   - `test_build_root_plan_returns_none_for_main_worktree`
   - `test_build_root_plan_returns_some_for_linked_worktree`
   - `test_build_root_plan_returns_none_for_external_linked_worktree`
   - `test_build_root_plan_returns_none_for_unrecorded_linked_worktree_in_managed_directory`
   - `test_build_root_plan_with_custom_worktree_directory`
2. 为每个测试写一行结论：「哪个门槛把它拦下/放行」。
3. 运行：`cargo test -p agent_ui --lib thread_worktree_archive::tests::test_build_root_plan`（在仓库根目录执行）。

**需要观察的现象**：五个测试全部通过；`returns_none_for_unrecorded_...` 证明「在托管目录内但非 Zed 创建」也会被拦。

**预期结果**：得到一张「门槛 × 测试」对照表。运行结果**待本地验证**（取决于本机是否能编译 agent_ui 全量依赖）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `affected_projects` 是列表而不是单个 Project？

**答案**：同一个 worktree 路径可以被多个窗口/工作区的 project 同时加载（比如用户开了两个窗口都包含这个 linked worktree）。删除目录前必须让**每一个**持有它的 project 都调用 `remove_worktree` 并等待释放，否则文件系统句柄未放行，`git worktree remove` 会失败。

**练习 2**：把第 3 道门槛（托管目录前缀检查）去掉会怎样？

**答案**：第 3、4 道门槛是互为补充的双保险，各管一半：第 3 道约束「路径形态」（必须在托管目录内），第 4 道约束「创建者身份」（必须在注册表里有 Zed 的记录）。只留第 4 道会有漏洞——用户可以在托管目录布局下**手动**建 worktree（注册表不认识它，第 4 道本可拦下），但如果用户后来改了 `git.worktree_directory` 设置，旧的托管目录就不再受第 3 道管辖，此时路径形态检查反而成了唯一防线；反过来，只留第 3 道也拦不住「注册表里有记录、但设置已改指向别处」的旧 worktree（`test_build_root_plan_with_custom_worktree_directory` 专门验证了这种情况）。sidebar_tests.rs 的第二个关键测试（见 4.5.4）则验证「托管目录外的 worktree 即使被 project 加载也不产生根」。三处测试合起来把两道门槛的行为边界锁死了。

**练习 3**：`roots_to_archive_for_paths` 的两个 `except` 参数（`except_thread_id` / `except_terminal_id`）分别服务于哪两个调用场景？

**答案**：`except_thread_id` 服务于线程归档/草稿删除——被归档的线程自己当然引用这个路径，不能让它「阻塞自己」；`except_terminal_id` 服务于终端关闭——被关闭的终端同样引用路径。归档线程时传 `None` 给终端参数（不豁免任何终端），关闭终端时反过来。

---

### 4.3 阻塞判据与三道防线：thread_blocks_worktree_archive

#### 4.3.1 概念说明

「我想归档线程 A，顺便删掉它的 worktree」——前提是**没有别人还需要这个目录**。「别人」分三类：

1. 其他未归档的非草稿线程（一定阻塞）；
2. 其他草稿线程——仅当它**有用户内容**时阻塞（空草稿不算数，直接删掉就行）；
3. 终端（引用该路径的终端元数据，一定阻塞）。

`thread_blocks_worktree_archive` 是单个线程的阻塞谓词，被三个消费方复用，构成本讲所说的「三道防线」：

| 防线 | 函数 | 问题 |
|---|---|---|
| 防线一 | `should_load_closed_workspace_for_archive`（[sidebar.rs:4635](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4635)） | Closed 行归档前，值不值得补开一个工作区？ |
| 防线二 | `roots_to_archive_for_paths`（[sidebar.rs:4713](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4713)） | 这个目录到底删不删盘？ |
| 防线三 | `linked_worktree_workspace_to_remove`（[sidebar.rs:4751](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4751)） | 归档后要不要把整个工作区从窗口里移除？ |

#### 4.3.2 核心流程

阻塞谓词的形式化定义：

\[ \text{blocks}(t) = \neg t.\text{is\_draft}() \;\vee\; \text{draft\_has\_user\_content}(t) \]

翻译成人话：**非草稿必阻塞；草稿只在有用户内容时阻塞**。「有用户内容」的判定要查活跃面板的内存态草稿编辑器 + kvp 持久层（`draft_prompt_store`），所以谓词需要传入当前工作区列表。

三道防线的判定流程：

```text
防线一（是否补开工作区）:
    folder_paths 为空 或 等于分组主路径（说明不是 linked worktree 场景）→ 不补开
    任一路径被阻塞型未归ived线程引用 → 不补开
    任一路径被终端引用 → 不补开
    否则 → 补开

防线三（是否移除工作区）:
    folder_paths 为空 → 不移除
    count_threads_blocking_worktree_archive(folder_paths, except) > 0 → 不移除
    找不到对应工作区 → 不移除
    工作区还有终端元数据（except 除外）→ 不移除
    有归档根：工作区的全部可见 worktree 都在归档路径集内 → 移除整个工作区
    无归档根：分组键路径 ≠ folder_paths（即这是个 linked-worktree 专属工作区）→ 移除

（防线二已在 4.2 讲过）
```

#### 4.3.3 源码精读

阻塞谓词本体——[sidebar.rs:4862-4876](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4862-L4876)：`!thread.is_draft()` 直接返回 true；草稿则委托 [draft_prompt_store.rs:60-71](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/draft_prompt_store.rs#L60-L71) 的 `draft_has_user_content`——遍历各工作区的 AgentPanel，找内存中的草稿块，落不到内存再查持久层。这个谓词被 `path_is_referenced_by_unarchived_threads_for_archive`（[sidebar.rs:4676-4690](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4676-L4690)）与 `count_threads_blocking_worktree_archive`（[sidebar.rs:4697-4711](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4697-L4711)）包装成集合查询。

防线一——[sidebar.rs:4635-4674](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4635-L4674)：第一道短路是 `folder_paths == project_group_key.path_list()`——线程的 folder 就是分组主路径时（普通线程，住在主仓库里），补开工作区毫无意义，归档判定不需要 git 信息。之后依次做线程引用与终端引用检查，全部干净才返回 true。它的调用点有三处：线程归档（[sidebar.rs:5302](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5302)）、终端关闭（[sidebar.rs:4961](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4961)）、草稿删除（[sidebar.rs:6672](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L6672)）——三条生命周期路径共用同一个「要不要先补开」判定。

补开的实现——[sidebar.rs:4906-4948](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4906-L4948)：函数上的文档注释说明动机——「Closed linked-worktree 条目需要一个打开的工作区，归档根规划才能在删除 worktree 前检查仓库」。实现上调用宿主的 `find_or_create_workspace`（`OpenMode::Add`），拿到工作区后先 `wait_for_archive_workspace_metadata`（[sidebar.rs:4878-4904](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4878-L4904)）：等 worktree 扫描完成、再等每个 git 仓库的 barrier——**git 元数据没就绪就推导根，会得出错误结论**。就绪后才执行调用方传入的 `then` 回调。

防线三——[sidebar.rs:4751-4805](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4751-L4805)：注意两个分支的差异——有归档根时（[L4785-L4801](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4785-L4801)），要求工作区的**全部**可见 worktree 都在归档路径集合内才移除（混入了别的目录就只关条目、不删工作区，交给 4.4 的 `close_items_for_archived_worktrees`）；无归档根时（[L4803-L4804](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4803-L4804)），退而求其次：只要分组键不等于 folder_paths（说明这个工作区是为 linked worktree 而存在的）就移除。

#### 4.3.4 代码实践

**实践目标**：用假想数据演算三道防线。

**操作步骤**：

1. 构造如下假想场景（纸面推演）：
   - 主仓库 `/project`，linked worktree `/worktrees/project/feature-a/project`（Zed 创建、在托管目录内）；
   - 线程 A（要归档，`session_id` 有值，folder = worktree 路径，行状态 Closed）；
   - 线程 B（非草稿，folder 同上，未归档）；
   - 空草稿 D（folder 同上，无用户内容）；
   - 终端 T（引用 worktree 路径）。
2. 分别对「只有 A」「A + B」「A + D」「A + T」四种世界状态，填写下表：

| 世界状态 | 防线一（补开？） | 防线二（roots？） | 防线三（移除工作区？） |
|---|---|---|---|
| 只有 A | 是 | 有 | 是 |
| A + B | ？ | ？ | ？ |
| A + D | ？ | ？ | ？ |
| A + T | ？ | ？ | ？ |

3. 对照源码验证你的答案。

**需要观察的现象 / 预期结果**：A + B 与 A + T 时三道防线全部亮红灯（B 非草稿阻塞、T 是终端引用），归档退化为「只归档线程本身、目录与工作区保留」；A + D 时空草稿不阻塞任何防线（它在 4.4 被直接删除）。这是纸面推演，结论可从源码直接读出，无需本地运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么「空草稿不阻塞」？直接让它阻塞、留在列表里不行吗？

**答案**：空草稿没有任何用户投入的内容（连 kvp 持久层都没有记录，测试里有断言 `read(empty_draft_id) is none`）。如果它阻塞归档，用户会面对「一个永远空着的草稿行挡住了整个 worktree 的清理」。设计选择是：归档时顺手把同路径的空草稿元数据**删掉**（见 4.4），代价是这些草稿不可恢复——但它们本来就是空的，无可恢复。

**练习 2**：防线一里 `folder_paths == project_group_key.path_list()` 时返回 false 的深层原因是什么？

**答案**：补开工作区的唯一目的是让 `build_root_plan` 能检查 git 仓库结构。folder 等于分组主路径意味着线程住在**主仓库**里，而主工作树永远不会产生归档根（4.2 门槛 2），所以补开是纯浪费——还会闪一个工作区出来打扰用户。

**练习 3**：`open_workspace_for_archive` 为什么要 `dismiss_connection_modal`？

**答案**：补开工作区可能触发远程连接流程并弹出连接模态框；归档是后台性质的清理动作，不应把「正在连接远程机器」的模态 UI 留在屏幕上。连接完成后主动关掉它（[sidebar.rs:4940](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4940)）。

---

### 4.4 级联清理：空草稿删除、工作区移除与混合工作区关条目

#### 4.4.1 概念说明

归档的「级联」指三件按顺序发生的清理：

1. **删空草稿**：把同路径的空草稿元数据从 Store 里物理删除（注意：是 `delete_all`，不是 `archive`——空草稿不留尸体）。
2. **移除工作区**：当一个工作区存在的唯一理由就是那个要归档的 linked worktree 时，把整个工作区从 MultiWorkspace 里移除（`RemovalIntent::KeepProject`，保留 project 以便快速重开）。
3. **混合工作区关条目**：工作区里混着「要归档的 worktree」和「正常 worktree」时，不移除工作区，只关掉指向归档 worktree 的编辑器条目（保持用户布局）。

顺序约束的核心：**1 和 2 的先后由「谁依赖谁」决定**——空草稿删除放在工作区移除**之后**（`remove_workspaces_then` 的 finish 闭包里），因为「草稿是否为空」的判定（`draft_has_user_content`）要查工作区面板的内存态，工作区还在时查得最准；同时 `start_archive_worktree_task` 开头还会再删一轮（按 roots 路径），双保险覆盖「工作区没被移除但 roots 非空」的场景。

#### 4.4.2 核心流程

```text
archive_thread 的收尾编排:
    remove_workspaces_then(workspaces_to_remove, close_item_tasks):
        异步移除工作区（RemovalIntent::KeepProject）
        用户取消 → 整条链终止（finish 不执行）
        await 全部关条目任务（错误只记日志）
        finish():
            若移除了工作区: delete_empty_drafts_for_archive_paths(folder_paths)
            in_flight ← start_archive_worktree_task(thread_id, roots)
                        # 内部第一步: delete_empty_drafts_for_archive_roots(roots)
            archive_and_activate(...)   # 见 4.5

close_items_for_archived_worktrees(roots, &mut workspaces_to_remove):
    archive_paths ← roots 的根路径集合
    对宿主的每个工作区（跳过已列入移除名单的）:
        archived_ids ← 该工作区可见 worktree 中落在 archive_paths 里的 id
        无 → 跳过
        全部可见 worktree 都命中 → 整个工作区加入移除名单
        部分命中 → 记为「混合」，对每个 pane 关掉 project_path 落在
                   archived_ids 里的编辑器条目（SaveIntent::Close）
    返回关条目任务列表
```

#### 4.4.3 源码精读

空草稿删除的三个重载——[sidebar.rs:4807-4860](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4807-L4860)：`_roots` 版从 `RootPlan` 取路径，`_paths` 版从 `PathList` 取路径，两者都汇入 `delete_empty_drafts_for_archive_targets`（[L4834-L4860](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4834-L4860)）。核心查询是 `unarchived_draft_ids_matching`：草稿的路径匹配任一 target 且远程身份一致、**并且不满足阻塞谓词**（即确实是空草稿）才入选；随后一次性 `store.delete_all`。注意这里复用了 `thread_blocks_worktree_archive`——「阻塞归档的」被留下，「不阻塞的（空草稿）」被删除，同一个谓词一正一反两种用法。

混合工作区分流——[sidebar.rs:5170-5246](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5170-L5246)：遍历宿主全部工作区，对每个工作区计算「可见 worktree 中有多少落在归档路径集内」；`visible_worktrees.len() == archived_worktree_ids.len()` 时整区移除（[L5212-L5216](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5212-L5216)），否则进混合名单。混合处理在 [L5220-L5243](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5220-L5243)：遍历 pane，挑出 `project_path(cx)` 的 `worktree_id` 命中归档集合的条目，调 `pane.close_items(window, cx, SaveIntent::Close, …)`——**只关条目、不动布局**。`archive_thread` 上方的注释（[sidebar.rs:5368-5374](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5368-L5374)）解释了设计意图：关闭条目让 `Entity<Worktree>` 句柄自然释放，从而后续删盘可行，但用户的工作区布局（其他目录、打开的其他文件）原封不动。

异步串联——[sidebar.rs:5128-5168](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5128-L5168)：`remove_workspaces_then`（u6-l2 已精读，这里看归档视角）：无事可做时**同步**执行 finish（[L5136-L5139](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5136-L5139)）；有移除时先 `multi_workspace.remove(..., RemovalIntent::KeepProject, ...)`，其返回任务若 resolve 为 `false`（用户在确认对话框上取消）则**整条链终止**——finish 里的归档落锤、磁盘任务都不会发生，这正是「用户取消则不归档」的实现点。之后逐个 await 关条目任务（错误 `log_err`，不中断），最后执行 finish。

草稿删除复用同一条流水线——[sidebar.rs:6656-6788](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L6656-L6788)：`remove_draft` 的结构与 `archive_thread` 几乎镜像（Closed 先补开、算 roots、算工作区移除、`remove_workspaces_then` 押后副作用），差别在最后一步调用的是 `remove_draft_entry`，其中磁盘归档走**分离版**任务（[sidebar.rs:6830](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L6830)）——草稿没有「归档记录」可绑定 in-flight 任务。终端关闭同理（`close_terminal_entry` 在 [sidebar.rs:5104](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5104) 调分离版）。

#### 4.4.4 代码实践

**实践目标**：确认「移除工作区 → 删空草稿 → 启动磁盘任务 → 落锤归档」的顺序在测试里可观察。

**操作步骤**：

1. 打开 [sidebar_tests.rs:3692-3732](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3692-L3732)，注意测试在 `dispatch_action(ArchiveSelectedThread)` 后跑了 **8 轮** `run_until_parked`（[L3688-L3690](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3688-L3690)）——一轮不够，因为整条链有多个异步跳板：补开工作区 → 等扫描/git barrier → 递归重入 → 移除工作区 → 磁盘任务。
2. 数一数链上有几个 `await` 边界（提示：`open_workspace_for_archive` 的 spawn、`wait_for_archive_workspace_metadata` 的两处、`remove_workspaces_then` 的 spawn、`archive_worktree_roots` 内部多个）。
3. 本地把 8 轮改成 4 轮，运行 `cargo test -p sidebar --lib test_archive_selected_thread_archives_closed_linked_worktree`，观察是否失败在「目录还在」或「线程未归档」断言上；改回 8 轮。

**需要观察的现象**：4 轮时断言可能间歇性失败（异步链未走完），8 轮时稳定通过——这就是作者写 8 轮的原因。

**预期结果**：顺序约束与异步跳板数量对得上。本地运行结果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`delete_empty_drafts_for_archive_targets` 里为什么要在谓词里排除「阻塞型」草稿？

**答案**：`unarchived_draft_ids_matching` 的过滤条件是「匹配路径 **且** `!thread_blocks_worktree_archive(...)`」。有用户内容的草稿是阻塞型的（会阻止 worktree 归档），它不该被删除——用户写了一半的提示词必须保留。这个 `!` 精确地把删除范围限定在空草稿。

**练习 2**：为什么混合工作区只关条目而不移除工作区？移除再让用户重开不行吗？

**答案**：混合意味着工作区里还有**不在归档范围**的 worktree（用户自己的目录、正在编辑的文件）。移除整个工作区会摧毁这些状态；只关命中条目既释放了 `Entity<Worktree>` 句柄（删盘的前提），又保住了布局。设计原则：清理动作的影响半径必须严格限定在「确属被归档线程所有」的资源。

**练习 3**：`remove_workspaces_then` 里用户取消移除后，为什么连 `archive_and_activate` 也不执行（线程压根没被标记归档）？

**答案**：移除工作区被用户否决说明用户不希望这个清理发生；若仍标记归档并删盘，会出现「列表里线程消失了、目录也被删了，但工作区还原封不动开着」的中间状态，且不可回退（磁盘删除不可逆）。把整个 finish 闭包押在移除成功之后，是「要么全做、要么全不做」的原子性选择——反正线程还在，用户随时可以重新发起归档。

---

### 4.5 磁盘事务与两种任务形态：archive_worktree_roots 家族与 archive_and_activate

#### 4.5.1 概念说明

磁盘归档是一个**手写事务**：对每个 root 依次「persist（保状态）→ remove（删目录）」，任一步失败就把已完成的 persist 按**逆序**回滚；每个关键步骤前检查取消信号，发现取消同样逆序回滚并返回 `Cancelled`。

两种任务形态的分工：

| | `start_archive_worktree_task` | `start_detached_archive_worktree_task` |
|---|---|---|
| 调用场景 | 归档**线程**（有归档记录可挂靠） | 关闭**终端** / 删除**草稿**（元数据被直接 delete，无挂靠点） |
| 返回值 | `Option<(Task, Sender)>`，二元组存入 Store 的 `in_flight_archives` | 无（内部 `detach`） |
| 成功 | `store.cleanup_completed_archive(thread_id)` 清掉挂靠 | 什么都不做 |
| 失败 | `store.unarchive(thread_id)` **自动反归档**（线程复活） | 只 `log::error!` |
| 取消 | 用户 unarchive 时 drop Sender → 任务回滚 | 无人持有 Sender，实质不可取消 |

`archive_and_activate` 则是归档的「落锤」：把线程标记为 archived、把 in-flight 任务挂进 Store、清理活跃条目并交接给邻居。它上方的文档注释（[sidebar.rs:5420-5435](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5420-L5435)）解释了一个微妙问题：激活邻居时**必须同时激活邻居的工作区**，否则 `rebuild_contents` 会从「当前活跃工作区」推导出一个带 worktree 徽标的虚假「+ New Thread」草稿行，把刚删的 worktree 又钉在列表里，阻碍磁盘清理。

#### 4.5.2 核心流程

磁盘事务（单 root，多 root 逐个重复）：

```text
archive_worktree_roots(roots, cancel_rx):
    completed_persists ← []
    对每个 root:
        若 cancel_rx 已关闭 → 逆序回滚 completed_persists；返回 Cancelled
        id ← persist_worktree_state(root)          # 失败 → 逆序回滚；返回 Err
        completed_persists.push((id, root))
        若 cancel_rx 已关闭 → 逆序回滚（含刚 push 的）；返回 Cancelled
        remove_root(root)                           # 失败 → 回滚当前 + 逆序回滚其余；返回 Err
    返回 Success

persist_worktree_state(root):
    读 HEAD SHA
    create_archive_checkpoint() → (staged_commit, unstaged_commit)   # 两个游离 WIP 提交
    store.create_archived_worktree(...) → DB 行 id
    把所有引用该路径的线程 link 到该行（失败 → 删行 + Err）
    主仓库 update_ref("refs/archived-worktrees/{id}", unstaged_commit)  # 防 GC，致命步骤
    返回 id

remove_root(root):
    verify_created_by_zed(root)   # 创建时间戳不匹配 → 拒删 + Err
    对每个 affected_project: remove_worktree + 等待释放
    主仓库 remove_worktree(root_path, force=true)   # 实际删目录
    成功 → forget_created_worktree（清注册表）
    失败 → rollback_root：把 worktree 重新 add 回各 project
```

回滚的对称性：`rollback_persist`（[thread_worktree_archive.rs:613-637](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L613-L637)）删 ref + 删 DB 行即可——WIP 提交本来就是游离的，ref 一删 git gc 自会收走，无需 git reset。

#### 4.5.3 源码精读

绑定版任务——[sidebar.rs:5511-5546](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5511-L5546)：roots 为空返回 None（调用方据此走纯元数据归档）；先删空草稿；建 channel；spawn 的任务里对三种结局分流——Success 清挂靠（`cleanup_completed_archive`，[thread_metadata_store.rs:879-881](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_metadata_store.rs#L879-L881)）、Cancelled 静默、Err 记日志并 `unarchive`（[thread_metadata_store.rs:873-877](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_metadata_store.rs#L873-L877)，内部 drop Sender 的注释点明了取消机制）。返回的二元组在 `archive_thread` 的 finish 里被先算出、再传给 `archive_and_activate` ——**先启动任务、后标记归档**，保证 Store 挂靠发生在 `store.archive` 内部（[thread_metadata_store.rs:858-871](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_metadata_store.rs#L858-L871)：`update_archived(true)` → 存 job → 发 `ThreadArchived` 事件）。

分离版任务——[sidebar.rs:5548-5571](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5548-L5571)：结构相同但没有 thread_id 可挂靠，`drop(cancel_tx)` 显式放在任务末尾（关闭信号线只用于结构对称），失败仅记一条带 `after closing sidebar item` 后缀的错误日志。**没有自动反归档**——因为终端/草稿的元数据已经被 delete，不存在「复活」一说。

事务体——[sidebar.rs:5573-5622](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5573-L5622)：`completed_persists` 记录已成功的 (DB行id, root)；persist 前后各查一次取消；`remove_root` 失败时先把**同路径**的最后一条 persist 弹出回滚（[L5607-L5613](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5607-L5613)），再逆序回滚其余。结果枚举 `ArchiveWorktreeOutcome { Success, Cancelled }` 定义在 [sidebar.rs:137-140](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L137-L140)。

persist 的 git 细节——[thread_worktree_archive.rs:499-607](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L499-L607)：`create_archive_checkpoint` 产出两个提交；DB 落行后**链接所有引用该 worktree 的线程**（[L550-L585](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L550-L585)，链接失败时删行回滚）；最后在主仓库上 `update_ref`（[L587-L601](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L587-L601)），注释直言这是致命步骤——没有 ref，`git gc` 终将收走 WIP 提交，恢复会静默失败。主仓库句柄来自 `find_or_create_repository`（[thread_worktree_archive.rs:356-471](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L356-L471)）：先在所有打开的工作区里找活体 Repository（远程身份必须匹配），找不到就**临时造一个 Project**（本地 `Project::local` / 远程 `Project::remote`）只为拿到 Repository 句柄，用完即弃——因为 `GitStore` 被 `Project` 独占，这是当前架构下的无奈之举（源码注释里标了 Future improvement）。

remove 的安全阀——[thread_worktree_archive.rs:216-253](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L216-L253) 与 [L269-L303](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L269-L303)：删除前 `verify_created_by_zed` 复核磁盘上 git 元数据目录的创建时间是否仍等于注册表记录值——不相等说明目录被外部删过又重建，Zed 拒绝删除、清掉过期记录并报错（这是防「删错别人目录」的最后一道闸）；随后逐 project 释放 worktree、等释放完成，再由主仓库执行 `remove_worktree(path, force=true)`（远程场景经 headless server 的 RPC 执行）；失败时 `rollback_root` 把 worktree 重新 add 回各 project（[L475-L482](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L475-L482)）；成功则 `forget_created_worktree` 清注册表。

落锤——[sidebar.rs:5436-5509](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5436-L5509)：`store.archive(thread_id, in_flight_archive)` 标记归档并挂任务；若被归档线程正是活跃条目则清空 `active_entry` 并尝试激活邻居（`activate_entry` 内部会切工作区，兑现文档注释里的承诺）；若不是活跃条目，还要清理「停在已归档线程上的面板」（[L5462-L5488](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5462-L5488)，防止用户切回该工作区时看到僵尸线程）；邻居激活失败则 `clear_base_view` 留下一个空分组。

#### 4.5.4 代码实践

**实践目标**：把两个关键测试读成「断言链 → 实现函数」的映射表（本讲综合实践的前半部分在此预演）。

**操作步骤**：

1. 精读对照测试——[sidebar_tests.rs:3735-3854](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3735-L3854)（`test_archive_selected_thread_deletes_empty_draft_when_linked_worktree_has_no_archive_root`）：场景是 `/external-worktree` 在托管目录**外**，`build_root_plan` 返回 None。
2. 回答：这个测试里 `workspace_for_paths(...).is_none()` 的断言为什么不是平凡成立（提示：归档前 `should_load_closed_workspace_for_archive` 通过 → `open_workspace_for_archive` 先补开了一个工作区，防线三的无根分支 `group_key.path_list() != folder_paths` 又把它移除了）。
3. 运行：`cargo test -p sidebar --lib test_archive_selected_thread_deletes_empty_draft`。

**需要观察的现象**：测试通过；磁盘断言 `fs.is_dir("/external-worktree")` 仍为 true（外部目录不删）。

**预期结果**：理解「无归档根」场景下流水线的退化行为：照样补开工作区、照样移除工作区、照样删空草稿，唯独不删目录。运行结果**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `start_archive_worktree_task` 要在 `archive_and_activate` **之前**调用（而不是之后）？

**答案**：`archive_and_activate` 内部的 `store.archive(thread_id, in_flight_archive)` 是「标记归档 + 挂靠任务」的原子操作，二元组必须先准备好。更重要的是顺序语义：若先标记归档再启动任务，标记与挂靠之间存在空窗——用户此刻反归档会找不到可取消的任务，磁盘删除照跑不误。先启动后标记，保证任何时刻 Store 里的归档线程要么没有磁盘任务、要么任务已在挂靠表里。

**练习 2**：磁盘归档失败后线程自动复活（`unarchive`），但此时**工作区已经移除了**——这算不算状态不一致？

**答案**：算轻微不一致，但可接受且有界：线程复活后重新出现在列表里（Closed 形态，folder 路径还在），用户可以重新打开工作区；worktree 目录因回滚而完好。设计上优先保护**数据不丢**（git 状态、线程元数据），工作区布局属于可重建的易失状态。

**练习 3**：分离版任务为什么不能像绑定版那样自动恢复？假设强行给终端关闭也加「失败自动恢复元数据」会怎样？

**答案**：终端/草稿走的是 `store.delete`（真删），磁盘任务启动时元数据已不存在，「恢复」没有落点。若强行恢复，就需要把 delete 推迟到磁盘任务成功之后——那关闭终端的 UI 反馈会被拖到 git 操作完成，且失败时要处理「已从面板移除但元数据又回来了」的更复杂不一致。权衡之下：终端/草稿的 worktree 磁盘状态仍然被 persist 保住（WIP 提交 + ref 都在，只是没有线程链接指向……实际上 persist 内部会把引用该路径的线程链接到记录），只是不自动反悔。

---

## 5. 综合实践

这是本讲的收官任务，对应大纲指定的实践：**精读 `test_archive_selected_thread_archives_closed_linked_worktree`，建立「断言 ↔ 实现」映射，并画出完整判定流程图。**

### 5.1 测试场景还原

测试位于 [sidebar_tests.rs:3568-3733](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3568-L3733)。准备阶段用 FakeFs 搭了一个逼真的 git worktree 世界：

- 主仓库 `/project`，其 `.git` 下有 `worktrees/feature-a/{commondir, HEAD}`——模拟真实 git 的 worktree 元数据布局（[L3572-3586](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3572-L3586)）；
- linked worktree 目录 `/worktrees/project/feature-a/project`，其 `.git` 是**文件**，内容 `gitdir: /project/.git/worktrees/feature-a`（[L3587-3594](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3587-L3594)）；
- `add_linked_worktree_for_repo` 让假 git 层认识这个 linked worktree，`record_zed_created_worktree` 往注册表里登记「Zed 创建」——**这道登记是后面根推导放行的关键**；
- 主 project 只打开 `/project`，所以 worktree 线程行是 **Closed** 形态；
- 三条元数据：worktree 线程（folder=worktree 路径，main=/project）、主仓库线程、同路径空草稿（并断言 kvp 层无内容，[L3652-3657](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3652-L3657)）。

触发：`focus_sidebar` → 设 `selection` → `dispatch_action(ArchiveSelectedThread)` → 8 轮 `run_until_parked`。

### 5.2 断言链 ↔ 实现函数映射表（参考答案）

| # | 断言（行号） | 验证的行为 | 背后的实现函数 |
|---|---|---|---|
| 0 | `[L3669-3681]` 行是 `ThreadEntryWorkspace::Closed` 且 folder 正确 | 前置：worktree 线程确实以 Closed 形态出现在列表 | `rebuild_contents` 的四路查询（u3-l4） |
| 1 | `[L3692-3702]` `thread.archived == Some(true)` | 线程被标记归档且未被自动反归档（磁盘任务成功） | `archive_and_activate` → `ThreadMetadataStore::archive`；成功路径 `cleanup_completed_archive` |
| 2 | `[L3703-3712]` 空草稿元数据已删 | 空草稿在删盘前被物理删除 | `start_archive_worktree_task` 开头的 `delete_empty_drafts_for_archive_roots` → `delete_empty_drafts_for_archive_targets` → `store.delete_all` |
| 3 | `[L3713-3720]` `workspace_for_paths(worktree路径)` 为 None | 临时补开的 linked worktree 工作区被移除 | `should_load_closed_workspace_for_archive`（决定补开）→ `open_workspace_for_archive`（补开+递归重入）→ `linked_worktree_workspace_to_remove`（决定移除）→ `remove_workspaces_then`（执行移除） |
| 4 | `[L3721-3727]` 工作区计数 == 1 | 只剩主仓库工作区 | 同上（`RemovalIntent::KeepProject` 移除，主工作区不受影响） |
| 5 | `[L3728-3732]` `!fs.is_dir(worktree路径)` | 目录真的从磁盘消失 | `archive_worktree_roots` → `persist_worktree_state`（WIP 提交+DB+ref）→ `remove_root`（释放句柄 + `git worktree remove`） |

（行号均指 sidebar_tests.rs，如 [L3692-3702](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3692-L3702)。）

### 5.3 流程图：归档选中线程的完整判定

请自己动手画一遍，下面是参考版（`→` 为同步流，`⇒` 为异步跳板）：

```text
ArchiveSelectedThread
    │
    ├─ 行是终端？ ──────────→ close_terminal（u6-l2，走分离版磁盘任务）
    ├─ 行是草稿？ ──────────→ remove_draft（镜像结构，走分离版磁盘任务）
    ├─ 状态 Running/Waiting → 拒绝
    ▼
archive_thread
    │
    ├─ 行是 Closed 且 should_load_closed_workspace_for_archive？
    │      │是 ⇒ open_workspace_for_archive ⇒ 等扫描+git barrier
    │      │      ⇒ update_entries ⇒ 递归 archive_thread（此时已 Open）
    │      │否 ↓
    ▼
roots_to_archive_for_paths          ←【必须在移除任何工作区之前】
    │   每路径过四道门槛（linked？托管目录内？Zed 创建？有 project 加载？）
    │   再滤掉「被其他阻塞型线程引用」「被终端引用」
    │
    ├─ roots 为空？
    │      ├─ 是：只归档线程元数据；工作区若属 linked-worktree 专属仍移除（无根分支）；
    │      │      空草稿经 delete_empty_drafts_for_archive_paths 删除；目录保留
    │      └─ 否 ↓
    ▼
linked_worktree_workspace_to_remove + close_items_for_archived_worktrees
    │   工作区全部可见 worktree 都命中 → 整区移除
    │   部分命中 → 只关条目（保布局）
    ▼
remove_workspaces_then ⇒（用户取消 → 全链终止）
    finish:
        ① removed? → delete_empty_drafts_for_archive_paths
        ② in_flight ← start_archive_worktree_task
              内部: delete_empty_drafts_for_archive_roots
              ⇒ archive_worktree_roots（事务: persist→remove，逐级回滚）
                    成功 → cleanup_completed_archive
                    失败 → unarchive（线程复活）
        ③ archive_and_activate
              store.archive(标记+挂靠) → 清 active_entry → 激活邻居（连带切工作区）
                                                └─ 无邻居 → clear_base_view
```

三个关键判定口诀：

- **何时只归档线程**：roots 为空 且 folder 等于分组主路径（普通线程），或路径仍被其他阻塞型线程/终端引用——只翻 `archived` 标志，世界原样。
- **何时连带归档 linked worktree（删盘）**：folder 是 Zed 创建、位于托管目录的 linked worktree，且除被归档线程外只剩空草稿引用、无终端引用——persist 保状态后 `git worktree remove`。
- **何时删除空草稿**：两种时机都发生在工作区处置之后——工作区被移除时按 folder 路径删（`_paths` 版），磁盘任务启动时按 roots 再删一轮（`_roots` 版）；谓词统一为「匹配路径 且 不阻塞（即无用户内容）」。

### 5.4 动手验证

1. 运行两个测试（仓库根目录）：

   ```bash
   cargo test -p sidebar --lib test_archive_selected_thread_archives_closed_linked_worktree
   cargo test -p sidebar --lib test_archive_selected_thread_deletes_empty_draft_when_linked_worktree_has_no_archive_root
   ```

2. 对照 5.2 的映射表，在源码里给每个断言找到唯一对应的实现函数；有对不上的，回到 4.x 对应小节重读。
3. 进阶（可选）：模仿这两个测试，写一个「归档时存在另一个非草稿线程」的测试，断言目录**保留**、工作区**不移除**、但线程本身仍被归档——这正好覆盖 4.3.4 表格中「A + B」那一行。断言怎么写可直接复用现成测试的套路（`fs.is_dir`、`workspace_for_paths`、`entry_by_session(...).archived`）。

预期结果：两个现有测试通过；进阶测试如果写了，验证「阻塞型线程让删盘与移区双双退避」。本地运行结果**待本地验证**。

## 6. 本讲小结

- **归档是流水线不是开关**：`ArchiveSelectedThread` → 行类型分流 → （Closed 行先补开工作区并递归重入）→ 推导归档根 → 判定工作区移除 → `remove_workspaces_then` 押后全部副作用 → 删空草稿 → 启动磁盘任务 → `archive_and_activate` 落锤。
- **根推导必须前置**：`build_root_plan` 依赖活体 project/repository 实体，工作区移除后这些实体即销毁，故 `roots_to_archive_for_paths` 永远先于 `remove_workspaces_then` 执行。
- **资格与阻塞双闸**：一个目录可删盘，当且仅当它是 Zed 创建、位于托管目录、被打开 project 加载的 linked worktree（四道门槛），且不被其他阻塞型线程（非草稿或有内容的草稿）与终端引用（`thread_blocks_worktree_archive` 谓词）。
- **清理有半径**：纯归档工作区整区移除（`RemovalIntent::KeepProject`），混合工作区只关命中条目保布局；空草稿在处置后按路径/按根删两轮；用户取消移除则整条链终止、线程不归档。
- **磁盘操作是手写事务**：persist（WIP 提交对 + DB 行 + 线程链接 + 防 GC ref）→ remove（创建时间复核 + 逐 project 释放 + `git worktree remove --force`），任一步失败或收到取消信号即逆序回滚。
- **两种任务形态对齐两种生命周期**：线程归档用绑定版（任务挂靠 Store，失败自动 `unarchive`，用户恢复可取消）；终端关闭与草稿删除用分离版（元数据已真删，失败只记日志）。

## 7. 下一步学习建议

- **下一讲 u8-l3**「序列化与恢复：WorkspaceSidebar 契约」：从磁盘与生命周期转向持久化——`SerializedSidebar` 如何把宽度与视图形态写入宿主，`WorkspaceSidebar` trait 如何解耦 workspace crate 与 sidebar crate。
- **反向读恢复链**：本讲的 persist 三层记录（WIP 提交、DB 行、防 GC ref）的消费方是 [thread_worktree_archive.rs:645-805](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L645-L805) 的 `restore_worktree_via_git`——对照 u8-l1 的 `open_thread_from_archive`，把「归档 → 恢复」闭环读完。
- **读 `remove_root` 的单测族**：[thread_worktree_archive.rs:1372-1746](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_worktree_archive.rs#L1372-L1746) 四个测试分别锁定「目录被删」「目录已消失仍成功」「外部重建则拒删」「删目录失败则回滚」，是理解安全阀设计的最佳材料。
- **追踪 `ThreadArchived` 事件的下游**：`store.archive` 发出的 `ThreadMetadataStoreEvent::ThreadArchived` 会驱动哪些订阅者刷新（提示：回看 u3-l1 的订阅网络），把事件侧的响应链补全。
