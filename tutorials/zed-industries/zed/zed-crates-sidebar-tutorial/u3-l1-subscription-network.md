# 事件订阅网络：谁在通知侧边栏刷新

## 1. 本讲目标

学完本讲，你应该能够：

1. 完整列出触发侧边栏刷新的**全部事件源**，并说明每个事件源注册在哪里、触发什么行为。
2. 跟踪 `ProjectEvent::WorktreePathsChanged` → `move_entry_paths` → 两个元数据存储的 `change_worktree_paths` 这条**写路径**，写出行数据路径迁移的时序说明。
3. 解释 `refresh_draft_editor_observations` 为什么必须在**每次** `update_entries` 之后重连订阅，而不是像其他订阅那样注册一次就不管。
4. 理解这套订阅网络如何与「全量重推导」架构约束（u1-l3 已建立）互相配合：订阅只负责「喊一声该刷新了」，不负责搬运增量状态。

本讲是 u1-l3（构造期一级订阅）的下游：u1-l3 讲了 `Sidebar::new` 里注册了哪些订阅，本讲深入其中的二级订阅函数，弄清每一类事件到底从哪来、到哪去。

## 2. 前置知识

阅读本讲前，你需要理解以下 gpui 概念（u1-l2、u1-l3 已铺垫，这里按本讲用法再确认一遍）：

- **实体与事件（Entity / EventEmitter）**：每个 `Entity<T>` 都是独立的状态单元。`T` 实现 `impl EventEmitter<E> for T {}` 后，就可以在更新实体时用 `cx.emit(event)` 发出事件；其他实体用 `cx.subscribe(&entity, |this, source, event, cx| ...)` 注册回调，收到事件。**订阅回调只在事件发出时触发**，适合「发生了某件具体的事」。
- **observe**：`cx.observe(&entity, |this, entity, cx| ...)` 则不同——只要那个实体调用了 `cx.notify()`（即「我的状态变了，请重新渲染我」），观察回调就会被调用。它不知道*具体*变了什么，只知道*变了*。适合粗粒度的「数据变了，重新查一遍」。
- **Subscription 的生命周期**：`cx.subscribe` / `cx.observe` 返回一个 `Subscription` 值，**它被 drop 时回调自动注销**。所以有两种持有策略：调用 `.detach()` 让它活到实体死亡为止（适合一次性接线），或把它存进结构体字段（适合需要主动断开/重连的场景）。本讲会同时见到这两种用法，这正是理解 4.5 节的关键。
- **去抖合并**：多个事件源可能在同一轮事件派发里连续触发刷新。`schedule_update_entries` 用一个 `update_task: Option<Task<()>>` 字段把一段时间窗口内的多次刷新请求合并成一次 `update_entries`（见 4.1.3）。
- **弱引用防环**：`WeakEntity<T>` 不增加实体引用计数。订阅闭包如果强持有被订阅实体，会和「实体持有订阅」互相锁死形成引用环，导致内存永不释放。侧边栏大量使用 `downgrade()` + `upgrade()` 判空。

还需要两个背景事实（u2-l1、u2-l2 已建立）：

- 侧边栏的每一行（线程/终端）都来自两个**全局元数据存储**：`ThreadMetadataStore` 与 `TerminalThreadMetadataStore`。行数据的「身份」里包含工作区路径（`folder_paths` 用于工作区匹配，`main_worktree_paths` 用于分组键 `ProjectGroupKey`）。
- 侧边栏的架构约束：任何变化最终都经 `update_entries` → `rebuild_contents` 从当前世界状态**全量重推导**列表，禁止在事件之间维护增量协调状态。

## 3. 本讲源码地图

| 文件 | 本讲涉及内容 |
| --- | --- |
| [crates/sidebar/src/sidebar.rs](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs) | 本讲主战场：`Sidebar::new` 的一级订阅、`subscribe_to_workspace`、`move_entry_paths`、`subscribe_to_agent_panel`、`observe_docks`、`schedule_update_entries`、`update_entries`、`refresh_draft_editor_observations` |
| [crates/project/src/project.rs](https://github.com/zed-industries-zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project/src/project.rs) | `ProjectEvent` 枚举（含 `WorktreePathsChanged`）与它的发出点 `emit_group_key_changed_if_needed` |
| [crates/project/src/worktree_store.rs](https://github.com/zed-industries-zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project/src/worktree_store.rs) | `WorktreePaths` 类型：`ordered_pairs` / `add_path` / `remove_folder_path` |
| [crates/agent_ui/src/thread_metadata_store.rs](https://github.com/zed-industries-zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/agent_ui/src/thread_metadata_store.rs) | `ThreadMetadataStore::change_worktree_paths`：线程行数据如何被改键并重建索引 |
| [crates/agent_ui/src/terminal_thread_metadata_store.rs](https://github.com/zed-industries-zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/agent_ui/src/terminal_thread_metadata_store.rs) | `TerminalThreadMetadataStore::change_worktree_paths`：终端侧的对称实现 |
| [crates/agent_ui/src/agent_panel.rs](https://github.com/zed-industries-zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/agent_ui/src/agent_panel.rs) | `AgentPanelEvent` 枚举与 `conversation_views()` |
| [crates/sidebar/src/sidebar_tests.rs](https://github.com/zed-industries-zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar_tests.rs) | 两个路径迁移测试，本讲代码实践的主要依据 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：先看订阅网络全景（4.1），再依次精读三个订阅/观察函数（4.2、4.4、4.5），以及夹在中间的路径迁移写路径（4.3）。

### 4.1 订阅网络全景：所有事件都汇入同一条漏斗

#### 4.1.1 概念说明

侧边栏自己**不生产任何数据**：线程列表来自两个元数据存储，工作区分组来自 `MultiWorkspace`，活跃条目来自 `AgentPanel`。它是纯响应式组件——对外部世界的每一类变化注册一个「耳朵」，耳朵听到任何动静，只做一件事：调用 `schedule_update_entries`，请求一次全量重推导。

这个设计直接对应结构体上的文档注释（u1-l3 引用过，这里再看一遍，因为它是本讲所有内容的总纲）：

[crates/sidebar/src/sidebar.rs:L730-L733](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L730-L733) —— `Sidebar` 结构体的文档注释明确要求：每次变化都经 `update_entries` → `rebuild_contents` 从零重推导整个条目列表，**禁止**添加增量或跨事件协调状态；凡是能从当前世界状态算出来的东西，都必须在重建时现算。

订阅网络的职责因此被严格限定：**只报告「世界变了」，不搬运「变了什么」**。唯一的例外是 `move_entry_paths`（4.3）——它不是给侧边栏自己搬运状态，而是把路径变更写回元数据存储，属于「修正数据源的键」，我们到 4.3 再展开。

#### 4.1.2 核心流程

所有事件源汇入同一条漏斗：

```text
事件源（一级订阅，Sidebar::new 直接注册）
├─ MultiWorkspaceEvent          ─┐
├─ filter_editor 的 BufferEdited  │
├─ ThreadMetadataStore observe   ├─→ schedule_update_entries(select_first?)
├─ TerminalThreadMetadataStore   │        │
│   observe                      ─┘        ↓
│                                    update_task（合并窗口）
事件源（二级订阅，随工作区/面板动态注册）      ↓
├─ ProjectEvent（每工作区）          update_entries
├─ GitStoreEvent（每工作区）             ├─ rebuild_contents        全量重推导行
├─ workspace::Event::PanelAdded        ├─ refresh_refilled_draft_times
├─ AgentPanelEvent（每面板）            ├─ refresh_draft_editor_observations ← 4.5
├─ dock observe（每工作区）             ├─ apply_list_state_diff   保留测量值
└─ 草稿编辑器 + ConversationView       ├─ prefetch_worktree_default_branches
    （每次 update_entries 后重连）      └─ cx.notify
```

其中一级订阅管「世界结构」（工作区增删、活跃切换、存储变化），二级订阅管「内容细节」（worktree、git、面板）。u1-l3 已经建立了这个两级划分，本讲的 4.2–4.5 就是把二级订阅逐个拆开。

#### 4.1.3 源码精读

一级订阅的注册集中在 `Sidebar::new`。先是宿主 `MultiWorkspace` 的四类事件：

[crates/sidebar/src/sidebar.rs:L813-L832](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L813-L832) —— 订阅宿主的 `MultiWorkspaceEvent`：`ActiveWorkspaceChanged` 先同步活跃条目、替换面板上已归档的线程，再请求刷新；`WorkspaceAdded` 是二级订阅的入口——新工作区出现时立刻调用 `subscribe_to_workspace` 接上它的全部事件源（4.2）；`WorkspaceRemoved` 与 `ProjectGroupsChanged` 只需刷新。

接着是过滤器编辑器和两个全局存储的 observe：

[crates/sidebar/src/sidebar.rs:L834-L843](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L834-L843) —— 过滤器编辑器每被编辑一次就请求刷新；查询非空时顺带清空 `selection`（u2-l3 讲过：键盘选中态是易失的）并传 `select_first_after_update = true`，让刷新后自动选中第一条匹配结果。

[crates/sidebar/src/sidebar.rs:L854-L865](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L854-L865) —— 对两个全局元数据存储的 `observe`。这是侧边栏最粗粒度也最常用的刷新通道：任何线程/终端元数据的增删改（保存、改标题、归档……）都会让存储调用 `cx.notify()`，从而触发这里。注意它**不区分**变了什么——反正重建是全量的，不需要知道。

构造尾巴上的 `defer_in` 负责补订「构造时已经存在」的工作区：

[crates/sidebar/src/sidebar.rs:L878-L887](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L878-L887) —— 把宿主降级为弱引用后延迟到构造完成，再遍历当前已有的工作区逐个 `subscribe_to_workspace`，最后做第一次刷新。`MultiWorkspaceEvent::WorkspaceAdded` 只能覆盖**未来**新增的工作区，这个 defer 补上了**过去**的。

漏斗本身的合并逻辑：

[crates/sidebar/src/sidebar.rs:L1974-L1990](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1974-L1990) —— `schedule_update_entries`：如果已有一个未执行的刷新任务（`update_task.is_some()`）且这次不需要 `select_first_after_update`，就直接返回（合并）；否则 spawn 一个任务，任务体里先把 `update_task` 置回 `None` 再执行 `update_entries`。这样从「请求刷新」到「任务真正跑起来」之间的所有重复请求都被吞掉，一轮事件风暴只做一次全量重建。`select_first_after_update = true` 的请求不会被合并掉，因为它携带了「刷新后选中第一条」的语义，不能丢。

汇总成一张总表（本讲最重要的产出，建议对照源码逐行核实）：

| # | 事件源 | 注册位置 | 事件 | 行为 |
| --- | --- | --- | --- | --- |
| 1 | `MultiWorkspace` | `Sidebar::new` | `ActiveWorkspaceChanged` | 同步活跃条目 + 替换归档面板线程 + 请求刷新 |
| 2 | `MultiWorkspace` | `Sidebar::new` | `WorkspaceAdded` | **`subscribe_to_workspace`** + 请求刷新 |
| 3 | `MultiWorkspace` | `Sidebar::new` | `WorkspaceRemoved` / `ProjectGroupsChanged` | 请求刷新 |
| 4 | `filter_editor` | `Sidebar::new` | `EditorEvent::BufferEdited` | 清空选中（查询非空时）+ 请求刷新（带 select_first） |
| 5 | `ThreadMetadataStore` | `Sidebar::new` | `observe`（任何 notify） | 请求刷新 |
| 6 | `TerminalThreadMetadataStore` | `Sidebar::new` | `observe`（任何 notify） | 请求刷新 |
| 7 | `Project`（每工作区） | `subscribe_to_workspace` | `WorktreeAdded` / `WorktreeRemoved` / `WorktreeOrderChanged` | 请求刷新 |
| 8 | `Project`（每工作区） | `subscribe_to_workspace` | `WorktreePathsChanged` | **`move_entry_paths`**（4.3）+ 请求刷新 |
| 9 | `GitStore`（每工作区） | `subscribe_to_workspace` | `RepositoryUpdated(GitWorktreeListChanged \| HeadChanged)` | 请求刷新 |
| 10 | `Workspace`（每工作区） | `subscribe_to_workspace` | `Event::PanelAdded(AgentPanel)` | **`subscribe_to_agent_panel`**（4.4）+ 请求刷新 |
| 11 | 各 dock（每工作区） | `observe_docks` | `observe`（任何 notify） | 仅活跃工作区时 `cx.notify()`（纯视觉刷新，不重建） |
| 12 | `AgentPanel`（每面板） | `subscribe_to_agent_panel` | `ActiveViewChanged` / `ActiveViewFocused` / `EntryChanged` | `sync_active_entry_from_panel` + 请求刷新 |
| 13 | `AgentPanel`（每面板） | `subscribe_to_agent_panel` | `TerminalCloseRequested` | `close_terminal` |
| 14 | `AgentPanel`（每面板） | `subscribe_to_agent_panel` | `ThreadInteracted` | `record_thread_interacted` + 请求刷新 |
| 15 | 草稿消息编辑器（每草稿） | `refresh_draft_editor_observations` | `MessageEditorEvent::Edited` | 请求刷新 |
| 16 | `ConversationView`（每草稿） | `refresh_draft_editor_observations` | `StateChange` | 请求刷新 |

#### 4.1.4 代码实践

**实践目标**：不运行任何代码，纯靠阅读，验证上表 16 行里每一行的「注册位置」都真实存在。

**操作步骤**：

1. 打开 [crates/sidebar/src/sidebar.rs:L813](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L813) 起，逐个找到表中 #1–#6 的注册代码。
2. 跳到 `subscribe_to_workspace`（L958）找到 #7–#10，跳到 `observe_docks`（L1223）找到 #11，跳到 `subscribe_to_agent_panel`（L1090）找到 #12–#14，跳到 `refresh_draft_editor_observations`（L2113）找到 #15–#16。
3. 对每一行在源码里用行号标注，做成你自己的「事件源卡片」。
4. 统计：16 行里有几处最终调用的是 `schedule_update_entries`，几处不是？（答案在 4.1.5。）

**需要观察的现象**：你会注意到除了 #11（dock observe 只 `cx.notify()`）和 #13（关闭终端走专门链路）之外，几乎所有耳朵的响应都是同一个函数调用——这就是漏斗的形状。

**预期结果**：完成一张带行号的注记表。本实践为源码阅读型，无需运行，「待本地验证」的部分仅指行号可能随后续提交漂移。

#### 4.1.5 小练习与答案

**练习 1**：为什么 #5、#6 用 `observe` 而不用 `subscribe` + 具体事件？

**参考答案**：两个存储的变化种类很多（保存、改标题、归档、更新时间戳、改路径……），而侧边栏的响应是全量重建，根本不关心*具体*变了什么，只关心*是否*变了。`observe` 恰好是「任何 `cx.notify()` 都叫醒我」的粗粒度通道，与全量重建的语义完全匹配，还避免了在存储侧为每种变化定义事件的维护成本。

**练习 2**：`schedule_update_entries(false, cx)` 连续被调用 10 次，`update_entries` 会执行几次？如果中间夹了一次 `schedule_update_entries(true, cx)` 呢？

**参考答案**：前者的典型情况是 1 次：第一个请求 spawn 了任务并把 `update_task` 设为 `Some`，后续 9 次因 `update_task.is_some() && !select_first_after_update` 直接返回；任务执行时先置回 `None` 再重建。但如果任务已经开始执行（`update_task` 已置 `None`），后续请求会再排一个新任务，所以严格说是「合并窗口内的请求合并为一次」。夹入一次 `true` 时它不会被合并掉（条件里 `!select_first_after_update` 使其绕过提前返回），会替换掉 pending 的任务并保证重建后执行 `select_first_entry`。

**练习 3**：表中 #11（dock observe）为什么只调 `cx.notify()` 而不 `schedule_update_entries`？

**参考答案**：dock 的变化（展开/收起、尺寸、可见性）不影响列表的**内容**——行集合、分组、标题都不变，只影响侧边栏自身的**渲染**（比如 dock 展开时的视觉状态）。`cx.notify()` 只触发重新 `render`，跳过整条重建管线，开销小得多。这也再次体现分工：内容变化走重建，纯视觉变化走 notify。

### 4.2 subscribe_to_workspace：给每个工作区接上三类事件源

#### 4.2.1 概念说明

一个 `MultiWorkspace` 可以容纳多个 `Workspace`（u1-l1 建立的认知），每个 `Workspace` 各有自己的 `Project`、`GitStore`、面板和 dock。`subscribe_to_workspace` 就是「把一个工作区的全部事件源接到侧边栏上」的接线函数，在两个时机被调用：

- `MultiWorkspaceEvent::WorkspaceAdded`（[sidebar.rs:L822-L825](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L822-L825)）——新工作区加入时；
- 构造尾巴的 `defer_in`（[sidebar.rs:L878-L887](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L878-L887)）——侧边栏创建时宿主里已有的工作区。

它注册的订阅都 `.detach()`——只要被订阅的实体活着回调就有效，工作区销毁时实体死亡、订阅随之失效，不需要侧边栏手动清理。

#### 4.2.2 核心流程

```text
subscribe_to_workspace(workspace)
│
├─ 防线：project.is_via_collab() → 直接返回
│   （collab 访客端的项目状态由远端主机驱动，本地不订阅、不迁移）
│
├─ ① cx.subscribe_in(&project)      → ProjectEvent 三类
│     WorktreeAdded / WorktreeRemoved / WorktreeOrderChanged → 请求刷新
│     WorktreePathsChanged { old_worktree_paths }           → move_entry_paths + 请求刷新
│
├─ ② cx.subscribe_in(&git_store)    → GitStoreEvent
│     RepositoryUpdated(_, GitWorktreeListChanged | HeadChanged) → 请求刷新
│
├─ ③ cx.subscribe_in(workspace)     → workspace::Event
│     PanelAdded(view) 且 view 可下转型为 AgentPanel
│       → subscribe_to_agent_panel + 请求刷新
│
├─ ④ self.observe_docks(workspace)  → 每个 dock 一条 observe（见 4.4）
│
└─ ⑤ 若 workspace 里已存在 AgentPanel 面板
      → 立刻 subscribe_to_agent_panel（补上面板先于侧边栏存在的情形）
```

①②③ 三类事件源分别回答三个问题：「这个工作区有哪些文件夹」「git 仓库结构什么样」「有哪些面板」。

#### 4.2.3 源码精读

先看整体与 collab 防线：

[crates/sidebar/src/sidebar.rs:L958-L967](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L958-L967) —— 函数开头取工作区的 `Project` 实体，若 `is_via_collab()` 为真直接返回。collab 访客（guest）端的项目状态由主机推送，路径不归本地管，订阅并迁移本地存储里的路径反而是错的——4.3 的实践里有一个专门的回归测试验证这条防线。

第一类：`ProjectEvent`。

[crates/sidebar/src/sidebar.rs:L969-L985](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L969-L985) —— 对 `Project` 的订阅。注意 `match` 只列出了四个变体，其余 `_ => {}` 一律忽略：worktree 的增删与重排只需刷新；唯独 `WorktreePathsChanged` 要先调用 `move_entry_paths`（4.3）再刷新——因为存储里的行数据还挂在旧路径键上，不先搬走，重建就会「丢行」。`ProjectEvent` 的类型别名导入见 [sidebar.rs:L42](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L42)（`use project::{... Event as ProjectEvent, ...}`）。

`WorktreePathsChanged` 事件长什么样、由谁发出：

[crates/project/src/project.rs:L371-L373](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project/src/project.rs#L371-L373) —— `ProjectEvent::WorktreePathsChanged` 只携带一个字段：变更**前**的完整 `WorktreePaths` 快照。新的路径集合可以从 `project.worktree_paths(cx)` 现查（4.3 会用到这一点），旧集合则必须随事件带上，否则无法做差。

[crates/project/src/project.rs:L2469-L2476](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project/src/project.rs#L2469-L2476) —— 发出点是 `emit_group_key_changed_if_needed`：`Project` 把当前 `worktree_paths` 与自己缓存的 `last_worktree_paths` 比较，不同则以旧值发出该事件并更新缓存。也就是说这是一个**变更检测后的转发**，只有路径集合真正变化时才有事件。

[crates/project/src/project.rs:L3931-L3960](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project/src/project.rs#L3931-L3960) —— `WorktreeStore` 事件的处理中，`WorktreeAdded`、`WorktreeRemoved`、`WorktreeUpdatedRootRepoCommonDir` 三处会调用 `emit_group_key_changed_if_needed`。这也解释了为什么 `ProjectEvent::WorktreeAdded` 和 `WorktreePathsChanged` 常常成对出现：加一个文件夹会先发出 `WorktreeAdded`（触发侧边栏刷新），紧接着路径比较发现变了再发出 `WorktreePathsChanged`（触发迁移 + 再刷新）。

第二类：`GitStoreEvent`。

[crates/sidebar/src/sidebar.rs:L987-L1005](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L987-L1005) —— 订阅工作区的 `GitStore`，只关心 `RepositoryUpdated` 且内层事件是 `GitWorktreeListChanged` 或 `HeadChanged` 的情况。这两个事件影响的是渲染层的两个数据：linked worktree 清单（u2-l2 讲过它决定 `Closed` 条目候选）和分支名标签——都不影响存储里的行数据身份，所以刷新即可，无需搬运。

第三类：`workspace::Event::PanelAdded`。

[crates/sidebar/src/sidebar.rs:L1007-L1019](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1007-L1019) —— 订阅工作区实体本身，等 `PanelAdded` 事件。面板在 workspace 里以类型擦除的形式存放（`view` 是可下转型的句柄），所以这里要 `downcast::<AgentPanel>()`：下转型成功说明加入的是 Agent 面板，于是调用 `subscribe_to_agent_panel` 接上它的事件（4.4）。面板是惰性创建的——用户第一次打开 Agent 面板时才会 `PanelAdded`，所以这条订阅必须常驻。

收尾两步：

[crates/sidebar/src/sidebar.rs:L1021-L1025](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1021-L1025) —— 调用 `observe_docks`（4.4.3 精读），然后检查该工作区**当前**是否已有 `AgentPanel`：有就直接订阅。这覆盖「面板先于侧边栏存在」的时序——`PanelAdded` 事件在侧边栏构造前就发完了，只能靠主动查询补订。

#### 4.2.4 代码实践

**实践目标**：弄清「`ProjectEvent` 有几十个变体，为什么侧边栏只处理四个」。

**操作步骤**：

1. 打开 [crates/project/src/project.rs:L340](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project/src/project.rs#L340) 附近的 `Event` 枚举（`ProjectEvent` 的本体），快速浏览全部变体。
2. 对照 [sidebar.rs:L972-L983](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L972-L983) 中被处理的四个，把其余变体按「是否可能影响侧边栏列表内容」分成两类。
3. 对每个「看起来可能影响」的变体（比如 `WorktreeUpdatedEntries`、`DiskBasedDiagnosticsStarted`），给出一个理由说明为什么它其实不影响：列表行的内容来自元数据存储 + git 快照 + 面板状态，不来自文件树条目或诊断。

**需要观察的现象**：你会发现绝大多数 `ProjectEvent` 变体描述的是**编辑器世界**的事（缓冲区、诊断、语言服务器），而侧边栏只关心**工作区结构**（路径集合）——两个世界几乎不相交。

**预期结果**：一张「忽略理由」清单。这是源码阅读型实践，结论无需运行验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `subscribe_to_workspace` 里对 `Project` 的订阅用 `cx.subscribe_in`（带 `window`），而 `Sidebar::new` 里对两个元数据存储用 `cx.observe`（不带 `window`）？

**参考答案**：`subscribe_in` 提供窗口上下文，供需要操作窗口的回调使用（本函数内的回调其实没用到 `_window`，但同一套接线习惯保持一致；4.4 里 `subscribe_to_agent_panel` 的 `TerminalCloseRequested` 分支真正需要 `window`）。`observe` 是粗粒度通知，回调里只做 `schedule_update_entries`，不需要窗口。

**练习 2**：如果删掉 L1023-L1025 的「补订已有面板」会发生什么？

**参考答案**：当侧边栏创建时某个工作区的 Agent 面板已经存在（面板先开、侧边栏后建，或面板状态从持久化恢复），`PanelAdded` 事件早已发过，侧边栏将永远收不到该面板的 `AgentPanelEvent`——活跃条目不跟随面板切换、终端关闭请求无人响应。这正是「事件只在未来有效，现存状态要主动查询」这一订阅模型通则的体现，与 `defer_in` 补订已有工作区是同一个道理。

**练习 3**：`WorktreeAdded`（刷新）和 `WorktreePathsChanged`（迁移 + 刷新）几乎总是先后到来，两次刷新会不会造成两次全量重建？

**参考答案**：通常会合并为一次：两个事件在同一轮事件派发里先后到达，第一个请求 spawn 的 `update_task` 还未执行（合并窗口未关闭），第二个请求因 `update_task.is_some() && !select_first_after_update` 被吞掉；`move_entry_paths` 写存储触发的 store observe 又是同窗口内的第三次请求，同样被吞。最终只做一次读取新路径集合的重建。

### 4.3 move_entry_paths：把路径变更写回两个元数据存储

#### 4.3.1 概念说明

这是整个订阅网络里**唯一一处写外部数据**的地方，也是理解「侧边栏为什么要订阅 `WorktreePathsChanged`」的关键。

背景：线程/终端的元数据行用「路径」当工作区身份——`folder_paths`（`PathList`）用于 `threads_by_paths` 索引和工作区匹配，`main_worktree_paths` 用于分组键 `ProjectGroupKey`（u2-l1、u2-l2）。这些行**持久化在数据库里**，其中大量是「历史线程」：没有在任何面板里打开，纯粹躺在存储中。

问题：用户往工作区里加一个文件夹（比如 `/project-a` 变成 `/project-a + /project-b`），工作区的路径集合变了，但存储里的历史行还挂在旧键 `[/project-a]` 上。若不处理，重建时按新键查询查不到它们——历史线程会凭空从侧边栏消失。

解法：`move_entry_paths` 在收到 `WorktreePathsChanged` 时，把旧键下的所有行**搬**到新键上。它是订阅网络里唯一知道「旧路径集合」（事件携带）又能触达两个全局存储的角色，所以由侧边栏承担这次搬运。

#### 4.3.2 核心流程

算法是一次朴素的集合差分。设旧路径对的有序序列为 \( O \)，新序列为 \( N \)（`WorktreePaths::ordered_pairs` 返回 `(main_path, folder_path)` 对）：

\[ \text{added} = \{(m, f) \in N : (m, f) \notin O\}, \qquad \text{removedFolders} = \{f : f \in \text{folders}(O),\ f \notin \text{folders}(N)\} \]

然后对两个存储中「挂在旧 folder 集合下的行」统一应用变更：加入所有新对、删除所有被移除的 folder。完整时序：

```text
用户向工作区添加文件夹 /project-b
│
├─ Project::find_or_create_worktree → WorktreeStore 发出 WorktreeAdded
│    ├─ Project 转发 ProjectEvent::WorktreeAdded ──────────────┐
│    └─ emit_group_key_changed_if_needed：路径集合与缓存不同     │
│         → cx.emit(WorktreePathsChanged { old: {/project-a} }) │
│                                                              │
├─ 侧边栏的 project 订阅回调（sidebar.rs:972）◄─────────────────┘
│    ├─ WorktreeAdded 分支        → schedule_update_entries（请求①）
│    └─ WorktreePathsChanged 分支
│         ├─ move_entry_paths(project, old_paths)
│         │    ├─ new_paths = project.worktree_paths(cx) = {/project-a,/project-b}
│         │    ├─ added_pairs  = [(/project-b, /project-b)]        （差集）
│         │    ├─ removed      = []                                （差集）
│         │    ├─ apply_path_changes 闭包 = 「add_path(/project-b,/project-b)」
│         │    ├─ ThreadMetadataStore.change_worktree_paths(old=[/project-a], …)
│         │    │    └─ 命中历史线程 → 改键 → 重建索引 → cx.notify
│         │    └─ TerminalThreadMetadataStore.change_worktree_paths(…)  （对称）
│         │         └─ 命中终端元数据 → 改键 → cx.notify ──→ observe 触发请求②
│         └─ schedule_update_entries（请求③）
│
└─ 合并窗口内 ①②③ → 一次 update_entries
     └─ rebuild_contents 用新键 [/project-a, /project-b] 查询
          → 历史线程出现在新分组头下，列表收敛
```

注意收敛性：`move_entry_paths` 写存储 → 存储 notify → 侧边栏的 observe 触发刷新 → 重建按**新**键查询 → 新键下数据正确 → 重建不再引发路径写入。回环只转一圈。

#### 4.3.3 源码精读

先看差分计算：

[crates/sidebar/src/sidebar.rs:L1028-L1061](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1028-L1061) —— 函数开头重复一遍 collab 防线（因为订阅注册时项目可能还是本地模式，之后才转入 collab，见 4.3.4 的回归测试）；然后从 `project.worktree_paths(cx)` 读**新**路径集合，与事件携带的旧集合做差：`added_pairs` 是「新集合里有、旧集合里没有的 `(main, folder)` 对」；`removed_folder_paths` 是「旧 folder 列表里有、新列表里没有的路径」。两者都为空则直接返回——路径集合没实质变化时不写存储、不触发 observe。

再看变更的应用方式：

[crates/sidebar/src/sidebar.rs:L1063-L1071](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1063-L1071) —— 构造 `apply_path_changes` 闭包：对一份 `WorktreePaths` 依次 `add_path`（新对）和 `remove_folder_path`（被移除的 folder）。闭包被传给**两个**存储，同一份变更逻辑复用两次。`remote_connection` 一并取出，用于把搬运限定在同一远端身份的行上。

最后是对两个存储的调用：

[crates/sidebar/src/sidebar.rs:L1072-L1087](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1072-L1087) —— 分别 `update` 两个全局存储，调用它们的 `change_worktree_paths`，三要素：旧 folder 路径列表（定位要搬的行）、远端身份（过滤）、变更闭包（怎么搬）。

被调用的存储侧实现（线程版）：

[crates/agent_ui/src/thread_metadata_store.rs:L978-L1003](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/agent_ui/src/thread_metadata_store.rs#L978-L1003) —— `ThreadMetadataStore::change_worktree_paths`：用旧 folder `PathList` 查 `threads_by_paths` 索引拿到候选 `ThreadId` 集合，过滤掉**已归档**的行（归档行不参与迁移）和远端身份不匹配的行，然后交给 `mutate_thread_paths` 改键并重建 `threads_by_paths` / `threads_by_main_paths` 两个索引。

终端版是更薄的对称实现：

[crates/agent_ui/src/terminal_thread_metadata_store.rs:L267-L302](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/agent_ui/src/terminal_thread_metadata_store.rs#L267-L302) —— `TerminalThreadMetadataStore::change_worktree_paths`：同样按 `terminals_by_paths` 定位、按远端身份过滤（终端没有归档概念，所以没有 archived 过滤），对每条元数据应用闭包后经 `save_internal` 重写索引，最后 `cx.notify()`——这个 notify 正是触发侧边栏 observe（总表 #6）的那一下。

变更闭包所用的 `WorktreePaths` 方法：

[crates/project/src/worktree_store.rs:L46-L49](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project/src/worktree_store.rs#L46-L49) —— `WorktreePaths` 的本体：两条平行的 `PathList`（folder 路径与 main 路径），linked worktree 时两者不同，普通 worktree 时相同。

[crates/project/src/worktree_store.rs:L102-L106](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project/src/worktree_store.rs#L102-L106) —— `ordered_pairs`：按插入顺序 zip 出 `(main, folder)` 对，是差分计算的比较单位。

[crates/project/src/worktree_store.rs:L111-L126](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project/src/worktree_store.rs#L111-L126) 与 [crates/project/src/worktree_store.rs:L142-L150](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project/src/worktree_store.rs#L142-L150) —— `add_path`（已存在同对则无操作，否则重建两条列表）与 `remove_folder_path`（按 folder 过滤后重建两条列表）。两者都通过「收集—过滤—重建 `PathList`」实现，保证排序一致。

#### 4.3.4 代码实践

**实践目标**：按规格完成两件事——(a) 写出路径迁移的时序说明；(b) 用两个现成测试验证理解。

**操作步骤**：

1. 以 4.3.2 的时序图为底稿，逐箭头填上真实行号：事件发出点（[project.rs:L2469](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/project/src/project.rs#L2469)）、订阅回调分支（[sidebar.rs:L978-L981](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L978-L981)）、差分（L1041-L1057）、闭包（L1064-L1071）、两次 `change_worktree_paths`（L1072-L1087）、存储侧改键（thread_metadata_store.rs:L978、terminal_thread_metadata_store.rs:L267）。
2. 在仓库根目录运行迁移测试：
   ```bash
   cargo test -p sidebar --lib test_non_archive_thread_paths_migrate_on_worktree_add_and_remove
   ```
   阅读该测试（[sidebar_tests.rs:L11993-L12112](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar_tests.rs#L11993-L12112)）：它先播种两条**纯历史**线程（不经面板、直接写存储）在键 `[/project-a]` 下，然后 `find_or_create_worktree("/project-b")`，断言旧键下 0 条、新键 `[/project-a, /project-b]` 下 2 条；再 `remove_worktree`，断言迁移回旧键。整个过程就是 4.3.2 时序的自动化版本。
3. 再运行 collab 防线测试：
   ```bash
   cargo test -p sidebar --lib test_collab_guest_move_thread_paths_is_noop
   ```
   阅读 [sidebar_tests.rs:L14765-L14833](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar_tests.rs#L14765-L14833)：本地模式建好订阅后把项目标记为 collab，再加 worktree——断言线程路径**保持不变**。这条测试锁定了 `move_entry_paths` 开头 `is_via_collab` 防线的必要性（订阅注册时项目还是本地的，防线必须设在**处理时**而非注册时）。
4. 回答规格中的问题：为什么迁移要同时调用两个存储？——因为线程行和终端行分属两个独立的存储与索引体系，没有统一的「条目存储」抽象，只能各自搬运。

**需要观察的现象**：测试输出中两个用例均通过；第一个用例若没有 `move_entry_paths`，断言「旧键 0 条 / 新键 2 条」会失败为「旧键 2 条 / 新键 0 条」（历史线程滞留旧键）。

**预期结果**：一份带行号的时序说明 + 两条绿色测试记录。命令输出**待本地验证**（本讲义未代跑）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `move_entry_paths` 用事件携带的 `old_worktree_paths` 定位旧行，而不是自己记住「上次的路径集合」？

**参考答案**：两点。其一，「上次的路径集合」属于跨事件协调状态——侧边栏还要维护它、在合适的时机更新它，恰恰是结构体文档注释（L730-L733）禁止的那类增量状态；事件携带旧快照让侧边栏可以无状态地做差分。其二，`Project` 自己已经缓存了 `last_worktree_paths` 用于变更检测（project.rs:L2469-L2476），侧边栏再记一份是重复且可能失同步的。

**练习 2**：差分为什么分别用「对」和「folder」两个粒度——added 用 `(main, folder)` 对，removed 只用 folder？

**参考答案**：added 需要完整对，因为往 `WorktreePaths` 里 `add_path` 要同时提供 main 与 folder（linked worktree 两者不同）；removed 只需要 folder，因为 `remove_folder_path` 按 folder 过滤即可连带删掉对应的 main。这是对 `WorktreePaths` 两个 API 形状的最小适配。

**练习 3**：`change_worktree_paths` 会过滤掉**已归档**的线程（thread_metadata_store.rs:L990-L997），为什么归档行不该迁移？

**参考答案**：归档线程不在侧边栏常规列表中显示、不参与按工作区键的常规查询匹配；它们的历史路径保持原样（归档视图按归档时间等组织）。若也迁移，等于让一次临时的文件夹增删改写了归档记录的归属。而终端存储没有归档概念，所以没有这层过滤——两个存储的过滤差异正好映照它们数据模型的差异。

### 4.4 subscribe_to_agent_panel 与 observe_docks：面板与 dock 事件

#### 4.4.1 概念说明

`AgentPanel` 是线程/终端真正被打开和显示的地方，因此它是 `active_entry`（u2-l3 讲过：全局高亮的活跃条目）的**权威来源**。侧边栏对每个工作区的每个 `AgentPanel` 注册一条订阅，处理面板的六类事件。

`observe_docks` 则是纯视觉通道：dock 的开合状态会影响侧边栏的渲染（例如左侧 dock 展开时的样式），但完全不影响列表内容，所以只 `cx.notify()` 不重建。

#### 4.4.2 核心流程

```text
subscribe_to_agent_panel(workspace, agent_panel)
│
├─ workspace 降级为 WeakEntity（防引用环）
│
└─ cx.subscribe_in(agent_panel) → AgentPanelEvent
     ├─ ActiveViewChanged | ActiveViewFocused | EntryChanged
     │      → sync_active_entry_from_panel（u2-l3 精读过：带活跃面板防线
     │        与 pending_thread_activation 防抖）
     │      → schedule_update_entries
     ├─ TerminalCloseRequested { metadata }
     │      → upgrade 工作区 → close_terminal（终端关闭链路，u6-l2 展开）
     └─ ThreadInteracted { thread_id }
            → record_thread_interacted（记录交互时间戳）
            → schedule_update_entries
```

#### 4.4.3 源码精读

[crates/sidebar/src/sidebar.rs:L1090-L1121](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1090-L1121) —— 函数本体。第一行就把 `workspace` 降级为弱引用再移进闭包：订阅闭包会随面板实体存活，若它强持有工作区、而工作区又（经由面板/dock）间接持有侧边栏，就会形成引用环。前三个事件变体合并处理——它们都意味着「面板当前展示的东西变了」，所以同步活跃条目后统一刷新；`TerminalCloseRequested` 分支里先 `upgrade()` 判空再用 `ThreadEntryWorkspace::Open(workspace)` 包装（u2-l2 讲过的 Open 形态）；`ThreadInteracted` 记录交互时间戳（`record_thread_interacted` 定义于 [sidebar.rs:L5696](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L5696)，与用户显式访问时间戳 `record_thread_access` 是两个入口，u7-l2 会展开）。

事件的定义处：

[crates/agent_ui/src/agent_panel.rs:L4935-L4941](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/agent_ui/src/agent_panel.rs#L4935-L4941) —— `AgentPanelEvent` 共五个变体，全部被侧边栏消费，无一浪费：三个「视图变化」类 + 一个「请求关闭终端」+ 一个「线程被交互」。

dock 观察：

[crates/sidebar/src/sidebar.rs:L1223-L1245](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1223-L1245) —— `observe_docks`：取出工作区全部 dock，每个 dock 挂一条 `observe`。回调里先 `upgrade` 工作区弱引用，再用 `is_active_workspace` 判断是否当前活跃工作区——只有活跃工作区的 dock 变化才值得让侧边栏重新渲染，其他工作区的 dock 动静与当前展示无关。注意这里没有任何 `schedule_update_entries`：dock 状态不在重建管线的输入集合里。

#### 4.4.4 代码实践

**实践目标**：核实「面板事件 → 侧边栏行为」的映射表，并理解弱引用在事件链中的位置。

**操作步骤**：

1. 对照 [sidebar.rs:L1101-L1118](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1101-L1118) 的五个 match 臂与 [agent_panel.rs:L4935-L4941](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/agent_ui/src/agent_panel.rs#L4935-L4941) 的五个变体，画一张一一映射表。
2. 用编辑器跳转（或 `grep -n "fn close_terminal"`）找到 `close_terminal`（[sidebar.rs:L4950](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L4950)）和 `record_thread_interacted`（[sidebar.rs:L5696](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L5696)），浏览各自前 20 行，记下它们各自触发了哪些后续动作（u6-l2 与 u7-l2 会精读，这里只需目录级了解）。
3. 数一数从 `subscribe_to_agent_panel` 到 `close_terminal` 的调用链上有几处 `downgrade`/`upgrade`，说明各自防的是什么环。

**需要观察的现象**：`close_terminal` 的调用点不止订阅回调一处（键盘/菜单也会关终端），说明「面板请求关闭」只是入口之一。

**预期结果**：一张五行的「事件 → 行为 → 后续函数行号」表。源码阅读型实践，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：`ActiveViewChanged`、`ActiveViewFocused`、`EntryChanged` 三个变体的处理完全相同，为什么不合并成一个事件？

**参考答案**：它们在面板侧的语义不同（视图切换完成 / 视图获得焦点 / 某个条目内容更新），面板的其他订阅方（不止侧边栏）可能需要区分。侧边栏恰好对三者响应一致，是「消费方裁剪」而非「生产方合并」——事件模型里生产方保持语义完整，消费方按需 match。

**练习 2**：`observe_docks` 对每个工作区注册时，dock 列表是注册那一刻的快照。若工作区后来新增了 dock，会怎样？

**参考答案**：新 dock 不会被观察到——`subscribe_to_workspace` 在工作区加入侧边栏时执行一次 `observe_docks`，之后新增的 dock 没有对应 observe。这是一个可接受的精度损失：dock 变化只影响视觉（回调仅 `cx.notify()`），而 gpui 的渲染是全量重绘，即使错过某次 dock notify，下一次任何 notify 都会带上最新 dock 状态。对比 4.5 的草稿编辑器订阅（错过会导致**内容**过时），这里错过只可能延迟一次视觉更新。

### 4.5 refresh_draft_editor_observations：每次重建后重连的观察链

#### 4.5.1 概念说明

草稿（draft）是「已创建但还没发送第一条消息」的线程。草稿行在侧边栏里的**标题**直接取自草稿消息编辑器的当前内容（用户输入几个字，行标题就跟着变）。要实现这一点，侧边栏必须订阅每个草稿的消息编辑器。

但这条订阅与前面所有订阅有一个本质区别：**它的订阅对象集合本身是动态发现的，且订阅目标实体会被替换**。因此它不能「注册一次就 detach」，而必须在每次 `update_entries` 之后清空重连——这也是为什么 `Sidebar` 结构体要专门有一个 `_draft_editor_observations: Vec<gpui::Subscription>` 字段（[sidebar.rs:L781](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L781)），而不是像其他订阅那样 `.detach()` 完事。

#### 4.5.2 核心流程

它在 `update_entries` 管线中的位置（第三步）：

[crates/sidebar/src/sidebar.rs:L2005-L2012](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2005-L2012) —— `update_entries` 依次执行：`rebuild_contents`（全量重推导行）→ `refresh_refilled_draft_times`（草屑「空→有内容」时间戳刷新，u6-l3 展开）→ **`refresh_draft_editor_observations`**（本节）→ `apply_list_state_diff`（保留测量值，u3-l3 展开）→ `prefetch_worktree_default_branches`（u4-l4 展开）。重连被排在重建之后，因为「当前有哪些草稿」正是重建的产物之一。

函数自身的流程：

```text
refresh_draft_editor_observations
│
├─ ① _draft_editor_observations.clear()   ← drop 全部旧 Subscription，注销旧回调
│
├─ ② 收集所有草稿会话视图：
│      workspaces → 各自的 AgentPanel → panel.conversation_views()
│      （= 活跃会话视图 + 全部保留的线程视图）
│
└─ ③ 对每个会话视图 cv 挂两条订阅（不 detach，存进 Vec）：
       a. cv 当前线程视图的消息编辑器
            MessageEditorEvent::Edited → schedule_update_entries
       b. cv 实体本身
            StateChange（任意状态变化）→ schedule_update_entries
```

#### 4.5.3 源码精读

[crates/sidebar/src/sidebar.rs:L2111-L2124](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2111-L2124) —— 函数开头：`clear()` 掉旧观察（`Subscription` 被 drop 即注销），然后从 `MultiWorkspace` 出发收集草稿会话视图：遍历工作区 → 取各自的 `AgentPanel` → `panel.read(cx).conversation_views()`。`conversation_views()` 的定义在 [agent_panel.rs:L4070-L4076](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/agent_ui/src/agent_panel.rs#L4070-L4076)——「活跃会话视图 + `retained_threads` 里保留的全部线程视图」，即**当前面板可能展示的会话全集**。这个集合没有专门的事件通知（「新草稿出现了」不构成一个事件），只能每次现查——这是「必须重连」的第一个原因。

[crates/sidebar/src/sidebar.rs:L2126-L2146](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2126-L2146) —— 对每个会话视图挂两条订阅。第一条挂在 `thread_view.read(cx).message_editor` 上：用户在草稿编辑器里每敲一下，`MessageEditorEvent::Edited`（枚举定义见 [message_editor.rs:L221-L227](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/agent_ui/src/message_editor.rs#L221-L227)，`Edited` 是其中之一）就触发一次刷新，重建时草稿行标题随内容更新。第二条挂在会话视图**实体本身**：源码里的注释（L2137-L2139）直接说明动机——会话视图在生命周期切换（Loading → Connected）期间会**替换**它的消息编辑器实体，旧编辑器上的订阅从此指向一个不再被使用的实体，只有订阅会话视图的 `StateChange`（[conversation_view.rs:L516](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/agent_ui/src/conversation_view.rs#L516) 定义的单元结构体事件）才能在编辑器被替换时再触发刷新、让下一轮重连挂上**新**编辑器。这是「必须重连」的第二个原因。

注意这两条订阅都**没有** `.detach()`，而是被 push 进 `_draft_editor_observations`：它们的生命周期必须由侧边栏主动管理（下次重建时 `clear()`），这正是 2 节所说「两种 Subscription 持有策略」的对照样本。

#### 4.5.4 代码实践

**实践目标**：回答规格中的问题——`refresh_draft_editor_observations` 为什么要在**每次** `update_entries` 后重连，而不是一次性注册。

**操作步骤**：

1. 先写下你的初始假设（很多人会猜「性能原因」——注意性能不是主因）。
2. 在源码中找三个证据：
   - 证据 A（订阅对象集合动态）：`conversation_views()`（[agent_panel.rs:L4070-L4076](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/agent_ui/src/agent_panel.rs#L4070-L4076)）返回的集合随面板状态变化，而侧边栏没有「草稿集合变了」的事件可订阅。
   - 证据 B（订阅目标实体被替换）：[sidebar.rs:L2137-L2139](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2137-L2139) 的注释——Loading → Connected 切换会替换消息编辑器实体，挂在旧实体上的订阅会变「聋」。
   - 证据 C（持有方式）：订阅被存进 `Vec<Subscription>` 字段（L2129、L2140 的 push）而非 detach，说明作者预期它们要被成批撤销。
3. 用一段文字组织答案：三条理由 + 「为什么每次重建都重连是安全的」（重连只是重建管线的固定一步，成本是 O(当前会话数) 的订阅登记，且与全量重推导架构一致——不记忆哪些已订阅，就不会失同步）。

**需要观察的现象**：证据 B 的注释原文就在代码里，是作者对这一设计最直接的自述。

**预期结果**：一段 5–8 句的说明文字。源码阅读型实践，无需运行。

#### 4.5.5 小练习与答案

**练习 1**：如果把 `refresh_draft_editor_observations` 里的两条订阅也 `.detach()`，会发生什么？

**参考答案**：短期看似正常，随后出现两类泄漏式 bug。其一，每次重建都新增一批订阅且永不注销，旧回调堆积（旧编辑器被敲一下也会触发刷新，虽然结果正确但回调数量无限增长）；其二，`clear()` 语义消失，函数从「重连」退化成「追加」，与「订阅目标实体可能已被替换」的现实冲突。`detach` 适合订阅对象**终生不变**的接线（如 `Sidebar::new` 里那些），动态集合必须用可撤销的持有方式。

**练习 2**：用户在草稿编辑器里敲一个字母，从按键到行标题更新，中间经过哪些环节？

**参考答案**：编辑器发出 `MessageEditorEvent::Edited` → 侧边栏的草稿编辑器订阅回调（L2131-L2134）→ `schedule_update_entries(false, cx)` →（合并窗口后）`update_entries` → `rebuild_contents` 从线程视图现读草稿内容生成新标题 → `refresh_draft_editor_observations` 重连（内容变了但编辑器实体没变，重连结果等价）→ `apply_list_state_diff` 保留测量 → `cx.notify()` 重新渲染。注意标题不是被「推送」到行的——行标题永远在重建时从源头现读。

**练习 3**：为什么第二条订阅挂在 `ConversationView` 上而不是 `ThreadView` 上？

**参考答案**：因为被替换的**编辑器**由线程视图持有，而「发生了生命周期切换、该重查了」这个信号由会话视图的状态机发出（`StateChange` 是 `ConversationView` 的事件，[conversation_view.rs:L516](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/agent_ui/src/conversation_view.rs#L516)）。挂在线程视图上要么收不到这个信号、要么仍需穿透到会话视图；挂在会话视图上是能感知「编辑器可能换了」的最浅层。

## 5. 综合实践

**任务：给你的事件源清单装上「听诊器」，亲眼看到一次路径迁移的全链路。**

综合本讲四个模块，完成一个本地实验（**会临时修改源码，实验后必须还原**）：

1. **准备清单**：把 4.1.3 的 16 行事件源总表抄成你的版本，每行留一个「触发证据」空栏。
2. **加日志**（临时改动，约 5 行）：
   - [sidebar.rs:L978](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L978) 的 `WorktreePathsChanged` 分支入口加 `log::info!`（或 `eprintln!`）；
   - [sidebar.rs:L1028](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1028) `move_entry_paths` 入口与 L1072/L1080 两次存储调用处各加一条；
   - [sidebar.rs:L854](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L854) 与 L859 两个 store observe 回调里各加一条；
   - [sidebar.rs:L1993](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1993) `update_entries` 入口加一条；
   - [sidebar.rs:L2113](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2113) `refresh_draft_editor_observations` 入口加一条（打印重连的会话视图数量）。
3. **运行**：`cargo test -p sidebar --lib test_non_archive_thread_paths_migrate_on_worktree_add_and_remove -- --nocapture`，观察日志顺序。
4. **对照**：日志应呈现出 4.3.2 时序的实证版本——`WorktreePathsChanged` → `move_entry_paths` → 两次 `change_worktree_paths` → 两个 store 的 observe 回调 → **一次** `update_entries` → 一次 `refresh_draft_editor_observations`。数一数 `update_entries` 打印了几次：如果合并生效，加/删 worktree 的整个风暴应该只落成个位数次重建。
5. **回填清单**：给总表中 #5、#6、#7、#8、#15/#16 填上触发证据；其余行注明「本次实验未覆盖」。
6. **还原**：`git checkout -- crates/sidebar/src/sidebar.rs`，确认 `git diff` 为空。

**预期结果**：一份带实证日志顺序的时序说明，能回答两个问题——「迁移写入触发的 observe 会不会造成第二次重建」（观察 `update_entries` 的打印次数）和「重连发生的频率」（每次重建恰一次）。日志输出**待本地验证**。

## 6. 本讲小结

- 侧边栏的全部事件源汇入同一条漏斗：16 类事件 → `schedule_update_entries`（`update_task` 合并）→ `update_entries` 全量重推导；订阅只报告「世界变了」，不搬运增量状态，这是结构体文档注释（L730-L733）约束的直接体现。
- `subscribe_to_workspace` 为每个工作区接上三类事件源（`ProjectEvent`、`GitStoreEvent`、`PanelAdded`）加 dock 观察与已有面板补订，开头有 collab 防线。
- `WorktreePathsChanged` 携带旧路径快照，`move_entry_paths` 用「新增对 / 移除 folder」两次集合差分，把两个元数据存储里挂在旧键下的历史行搬到新键，写入触发的 observe 回环经一次重建收敛。
- `subscribe_to_agent_panel` 消费全部五类 `AgentPanelEvent`：前三个同步 `active_entry`（权威来源），`TerminalCloseRequested` 走关闭链路，`ThreadInteracted` 记时间戳；`observe_docks` 则是只 notify 不重建的纯视觉通道。
- `refresh_draft_editor_observations` 是唯一的「每次重建重连」订阅：订阅对象集合（会话视图）没有事件可订阅、消息编辑器实体又会在生命周期切换中被替换，因此清空重连并存于 `Vec<Subscription>` 字段而非 detach。

## 7. 下一步学习建议

本讲弄清了「谁在喊刷新」，下一讲 **u3-l2（重建管线：schedule_update_entries → update_entries）** 顺着漏斗往下走：`update_entries` 的五个步骤各自做什么、`rebuild_contents`（[sidebar.rs:L1342](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1342)）如何从项目分组推导出全部可见行。

在进入下一讲前，建议先自己通读一遍 `update_entries`（L1993-L2021）和 `rebuild_contents` 的主循环，带着本讲的问题读：16 类事件提供的世界状态（存储、分组、面板、git 快照）分别在重建的哪一步被读走？

后续关联：`move_entry_paths` 的写入后果会在 u3-l4（rebuild_contents 全景）的查询侧闭合；`TerminalCloseRequested` → `close_terminal` 链路在 u6-l2（终端条目的激活与关闭）展开；`record_thread_interacted` 与 `record_thread_access` 的区分在 u7-l2（MRU 切换器集成）展开。
