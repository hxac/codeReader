# rebuild_contents 全景：从项目分组到可见行

## 1. 本讲目标

学完本讲，你应该能够：

1. 按「准备 → 分组遍历 → 收集线程/终端 → 草稿判定 → 排序过滤 → 收尾 GC」六个阶段走读 `rebuild_contents`（约 630 行的本 crate 最长核心函数），说出每个阶段读取了哪些全局 Store 与哪些 `Sidebar` 记忆字段。
2. 解释线程与终端各自的「四路查询」：为什么同一条元数据会从多个索引起被找到，`seen_thread_ids` / `seen_terminal_ids` 如何保证一行只出现一次，以及查询顺序为什么决定了行的 `Open` / `Closed` 归属。
3. 说明路径消歧 `compute_disambiguation_details` 的「逐级加深直到不重名」算法，以及 `branch_by_path` 如何把 git 分支名映射到行的 worktree 徽标上。
4. 讲清 `apply_active_info` 的合并语义：数据库打底、进程内存覆盖，以及 `old_statuses` 记忆字段如何支撑「Running → Completed 发通知」的跳变检测。

本讲承接 u3-l2（重建管线五步中的第一步「重建」正是 `rebuild_contents`）与 u2-l2（`ProjectGroupKey`、`PathList`、`Open`/`Closed` 等概念在那里建立）。

## 2. 前置知识

### 全量重推导：一行列表是怎么来的

u3-l2 讲过本 crate 的铁律：`Sidebar` 不在事件之间维护增量状态，任何世界变化都汇入 `update_entries → rebuild_contents`，从「当前世界状态」**重新算出**整个 `contents.entries`。本讲就打开这个黑盒：所谓「当前世界状态」具体包括哪些数据源、按什么顺序读、怎么合并成一个 `Vec<ListEntry>`。

### 三类数据源

`rebuild_contents` 读的东西分三类：

- **持久化元数据**（数据库）：`ThreadMetadataStore`（线程）与 `TerminalThreadMetadataStore`（终端），存的是跨会话不丢的行数据（标题、路径、时间戳）。App 重启后列表仍然有内容，靠的就是它们。
- **进程内存态**（活跃面板）：每个打开工作区的 `AgentPanel` 里正在运行的会话，能提供数据库没有的实时信息（当前状态、正在生成的标题、diff 统计）。App 重启后这些全部消失。
- **`Sidebar` 自身记忆字段**：`live_thread_statuses`（上一刻各会话的状态）、`previous.notified_threads`（上一刻的通知集合）等。它们是全量重推导的必要补充——重建只能看到「现在」，看不到「上一刻」，而通知检测恰恰需要比较两个时刻。

### WorktreePaths：一条线程的两套路径

u2-l2 提过 `PathList`；这里再进一步。每条线程/终端元数据存有一个 `WorktreePaths`，它是**两份等长路径表的配对**：

- `folder_paths`：线程实际打开时所在的工作区根路径（在 linked worktree 里打开，就记 linked worktree 的路径）。
- `main_worktree_paths`：这些路径各自所属的**主 worktree** 路径。

定义见 [crates/project/src/worktree_store.rs:L46-L49](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/project/src/worktree_store.rs#L46-L49)：`paths` 是 folder 列、`main_paths` 是 main 列，`ordered_pairs()` 把它们逐位 zip 起来。同一个 `main != folder` 的配对就是 linked worktree。两个元数据 Store 各建了**两个索引**：按 folder 路径查、按 main 路径查——这正是后面「四路查询」的根源。

### 术语速查

| 术语 | 含义 |
| --- | --- |
| 分组（project group） | `MultiWorkspace` 里按 `ProjectGroupKey` 聚合的若干工作区，渲染为一个项目分组头 |
| linked worktree | 同一 git 仓库检出到别的目录的工作树，`main != folder` |
| 活跃信息（live info） | `ActiveThreadInfo`，来自当前打开的 Agent 面板的实时线程数据 |
| 草稿（draft） | 还没有 session（未发过消息）的线程，`session_id.is_none()` |

## 3. 本讲源码地图

| 文件 | 本讲关注的片段 | 作用 |
| --- | --- | --- |
| `crates/sidebar/src/sidebar.rs` | `rebuild_contents`（L1342-L1972） | 主角：全量重建列表内容 |
| `crates/sidebar/src/sidebar.rs` | `apply_active_info`（L379-L388） | 把活跃信息覆盖到数据库行上 |
| `crates/sidebar/src/sidebar.rs` | `draft_display_label_for_thread_metadata` / `thread_metadata_would_render_sidebar_row`（L306-L340） | 草稿可见性判定 |
| `crates/sidebar/src/sidebar.rs` | `workspace_path_list` / `linked_worktree_path_lists_for_workspaces`（L533-L557） | 分组内路径清单（u2-l2 已详述） |
| `crates/sidebar/src/sidebar.rs` | `push_entries_by_display_time` / `thread_display_time`（L5703-L5740) | 排序合并输出 |
| `crates/agent_ui/src/thread_metadata_store.rs` | `entries_for_path` / `entries_for_main_worktree_path`（L621-L655） | 线程元数据的两个索引查询 |
| `crates/agent_ui/src/terminal_thread_metadata_store.rs` | 同名两个查询（L206-L240） | 终端元数据的两个索引查询 |
| `crates/util/src/disambiguate.rs` | `compute_disambiguation_details`（L14-L58） | 路径消歧算法 |
| `crates/project/src/project.rs` | `ProjectGroupKey::display_name` / `path_suffix`（L6458-L6506） | 分组标签渲染 |
| `crates/project/src/worktree_store.rs` | `WorktreePaths`（L46-L106） | 两套路径的存储与访问 |
| `crates/agent_ui/src/thread_metadata_store.rs` | `worktree_info_from_thread_paths`（L374-L436） | 行内 worktree 徽标构造（消费 `branch_by_path`） |
| `crates/sidebar/src/sidebar_tests.rs` | `test_terminal_metadata_is_deduped_across_project_groups`（L1935-L2013） | 去重行为的防回归测试 |

## 4. 核心概念与源码讲解

### 4.1 rebuild_contents 全景：六个阶段的一次全量推导

#### 4.1.1 概念说明

`rebuild_contents` 是一个纯读取函数：输入是「当前世界」，输出是覆盖 `self.contents` 的完整快照。它的文档注释明确写了性能目标与三条正确性性质：

[crates/sidebar/src/sidebar.rs:L1327-L1341](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1327-L1341)

> 对工作区与线程做单次前向遍历加一个 \(O(T \log T)\) 的排序；性质：必须显示每个工作区；必须显示每个线程并挂到正确的工作区；重建后 active 状态必须与当前面板完全一致。

注意「避免额外扫描」不是口号：每次任何事件（哪怕一个线程改了标题）都会完整执行这个函数，它的复杂度就是列表刷新的成本下限。

#### 4.1.2 核心流程

```
rebuild_contents(cx)
│
├─ 阶段 0：准备
│   ├─ upgrade multi_workspace（宿主已死 → 直接返回）
│   ├─ 读出 workspaces / active_workspace / agent_server_store / 过滤词 query
│   ├─ previous = mem::take(&mut self.contents)   ← 旧快照整体让位
│   └─ old_statuses = &self.live_thread_statuses   ← 「上一刻」记忆
│
├─ 阶段 1：全局预算（分组循环之外，只算一次）
│   ├─ 跨工作区收集 live_notified_terminal_ids     ← 终端通知是现算的
│   ├─ 所有分组路径 → 排序去重 → compute_disambiguation_details → path_detail_map
│   └─ 遍历各工作区 git 仓库 → branch_by_path       ← 路径 → 分支名
│
├─ 阶段 2：for group in groups（每个项目分组）
│   ├─ 组内准备：workspace_by_path_list / resolve_workspace / linked_worktree_path_lists
│   ├─ 收集终端：四路查询 + seen_terminal_ids 去重 → terminals
│   ├─ 收集线程（未折叠或有搜索词时）：四路查询 + seen_thread_ids 去重 → threads
│   │   ├─ 草稿判定：draft_display_label… → WithContent/Empty，retain 过滤
│   │   ├─ 合并活跃信息：live_info_by_session → apply_active_info
│   │   ├─ 通知检测：old_statuses 中 Running → 现在 Completed ⇒ notified_threads
│   │   └─ threads.sort_by（按显示时间降序）
│   └─ 搜索过滤（query 非空）：fuzzy 匹配标题/标签/分组名，全不中 ⇒ 整组跳过
│
├─ 阶段 3：压入 entries（每个分组先 push 一个 ProjectHeader，记录下标到
│           project_header_indices，再 push_entries_by_display_time 合并行）
│
└─ 阶段 4：收尾 GC 与提交
    ├─ notified_threads.retain(current_thread_ids.contains)
    ├─ thread_last_accessed / terminal_last_accessed 同样裁剪
    ├─ self.live_thread_statuses = new_live_statuses   ← 「这一刻」成为下一刻的旧值
    └─ self.contents = SidebarContents { … }
```

#### 4.1.3 源码精读

**函数头部与世界读取。** 第一道防线是宿主存活性检查——`Sidebar` 只持 `WeakEntity`，宿主销毁后重建无意义：

[crates/sidebar/src/sidebar.rs:L1342-L1358](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1342-L1358)

```rust
fn rebuild_contents(&mut self, cx: &App) {
    let Some(multi_workspace) = self.multi_workspace.upgrade() else {
        return;
    };
    let mw = multi_workspace.read(cx);
    let workspaces: Vec<_> = mw.workspaces().cloned().collect();
    let active_workspace = Some(mw.workspace().clone());
    // … agent_server_store、query …
    let previous = mem::take(&mut self.contents);
    let old_statuses = &self.live_thread_statuses;
```

`mem::take` 是关键一步：旧 `contents` 被整体取走，`notified_threads` 这个唯一需要跨重建继承的列表级记忆从中继承（见 L1361），其余字段全部作废。`old_statuses` 借用自 `live_thread_statuses`，本函数结尾才会整体替换，因此「上一刻的状态表」在全函数内可用。

**累加器声明。** 一组可变集合承担整次重建的中间状态：

[crates/sidebar/src/sidebar.rs:L1360-L1374](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1360-L1374)

```rust
let mut entries = Vec::new();
let mut notified_threads = previous.notified_threads;   // 继承记忆
let mut notified_terminals: HashSet<TerminalId> = HashSet::new(); // 每次现算
let mut new_live_statuses: HashMap<acp::SessionId, (AgentThreadStatus, ThreadId)> = HashMap::new();
let mut current_session_ids: HashSet<acp::SessionId> = HashSet::new();
let mut current_thread_ids: HashSet<agent_ui::ThreadId> = HashSet::new();
let mut current_terminal_ids: HashSet<TerminalId> = HashSet::new();
let mut project_header_indices: Vec<usize> = Vec::new();
let mut seen_thread_ids: HashSet<agent_ui::ThreadId> = HashSet::new();
let mut seen_terminal_ids: HashSet<TerminalId> = HashSet::new();
```

注意两组集合的分工：`seen_*` 是**组内去重**（防止四路查询重复产出同一行）；`current_*` 是**全局在册名单**（收尾时用来裁剪记忆字段——已经从所有分组消失的线程，不该再占着通知位和访问时间戳）。

**分组清单与终端通知现算。**

[crates/sidebar/src/sidebar.rs:L1390-L1402](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1390-L1402)

```rust
let groups = mw.project_groups(cx);
let mut live_notified_terminal_ids: HashSet<TerminalId> = HashSet::new();
for workspace in &workspaces {
    if let Some(agent_panel) = workspace.read(cx).panel::<AgentPanel>(cx) {
        live_notified_terminal_ids.extend(
            agent_panel.read(cx).terminals(cx).into_iter()
                .filter_map(|terminal| terminal.has_notification.then_some(terminal.id)),
        );
    }
}
```

`project_groups`（[crates/workspace/src/multi_workspace.rs:L855-L864](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/workspace/src/multi_workspace.rs#L855-L864)）返回 `Vec<ProjectGroup>`，每组带 `key` 与按 key 现查出的 `workspaces`。终端通知与线程通知策略不同：终端没有 Running→Completed 式跳变，所以不进 `previous` 记忆，每轮从面板现算 `live_notified_terminal_ids`，行构造时查一次集合即可。

**草稿可见性判定。** 线程收集后有一段后处理：草稿行先统一标为 `WithContent`，再由 `draft_display_label_for_thread_metadata` 降级：

[crates/sidebar/src/sidebar.rs:L1671-L1685](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1671-L1685)

```rust
for thread in &mut threads {
    if thread.draft.is_none() { continue; }
    if let Some((label, kind)) = draft_display_label_for_thread_metadata(
        &thread.metadata, &thread.workspace, cx,
    ) {
        let thread = Arc::make_mut(thread);
        thread.metadata.title = Some(label);
        thread.draft = Some(kind);
    }
}
threads.retain(|thread| thread.draft.is_none() || thread.metadata.title.is_some());
```

标签函数（[crates/sidebar/src/sidebar.rs:L306-L328](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L306-L328)）查 `draft_prompt_store`：有用户输入 → `(输入首行摘要, WithContent)`；没有 → `(占位文案, Empty)`。随后两道 retain：

- 拿不到任何标签的草稿直接消失（`Empty` 也拿得到占位文案，所以实际很少触发）。
- `Empty` 草稿只在「正是当前活跃线程」时保留（[crates/sidebar/src/sidebar.rs:L1694-L1702](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1694-L1702)）：没有内容的空白草稿不该在每个分组下都躺着一行；`pending_activation.is_some()` 期间也先藏起来，防止跨窗口激活的乐观闪烁。这个「一个工作区只保留一个空草稿」的效果由 `create_new_thread` 侧配合保证。

**排序与输出。** 组内线程先按显示时间降序排好：

[crates/sidebar/src/sidebar.rs:L1753-L1757](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1753-L1757)

`thread_display_time`（[crates/sidebar/src/sidebar.rs:L5703-L5705](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L5703-L5705)）取 `interacted_at.unwrap_or(updated_at)`——用户交互过的线程比只被动更新的排得更靠前。随后 `push_entries_by_display_time`（[crates/sidebar/src/sidebar.rs:L5707-L5740](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L5707-L5740)）把终端（按 `created_at`）与线程合并成一条按时间降序的行流，其中 `Empty` 草稿的时间取 `DateTime::<Utc>::MAX_UTC`——永远钉在组内最顶上。

**收尾 GC 与提交。**

[crates/sidebar/src/sidebar.rs:L1956-L1971](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1956-L1971)

```rust
notified_threads.retain(|id| current_thread_ids.contains(id));
self.thread_last_accessed.retain(|id, _| current_thread_ids.contains(id));
self.terminal_last_accessed.retain(|id, _| current_terminal_ids.contains(id));
self.live_thread_statuses = new_live_statuses;
self.contents = SidebarContents {
    entries, notified_threads, notified_terminals,
    project_header_indices, has_open_projects,
};
```

这一段是「记忆字段的垃圾回收」：`current_*` 名单来自所有分组的在册行（含折叠分组补查的 id）。不裁剪的话，归档/删除的线程会永远留在通知集合与 MRU 时间戳里。最后 `new_live_statuses` 覆盖旧表——本轮观测成为下一轮跳变检测的基线。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把 4.1.2 的六阶段流程图与真实代码逐段对上号。
2. **操作步骤**：
   - 打开 `sidebar.rs` L1342-L1972，从上到下读一遍，给每个阶段在代码里画一条分隔线并标注行号范围（例如「阶段 1：L1390-L1437」）。
   - 对照函数文档注释的三条性质（L1338-L1340），各找出一行能体现它的代码：提示——性质一对应分组头无条件 push（L1930）、性质二对应四路查询的全覆盖、性质三对应 L1731-L1736 的 `is_active_thread` 判定。
3. **需要观察的现象**：你会发现在「搜索过滤」分支里（L1871-L1874），整组都不匹配时 `continue` 跳过了分组头——这与性质一「必须显示每个工作区」看似冲突；想清楚为什么「用户主动输入过滤词」是例外。
4. **预期结果**：得到一张行号化的阶段表。过滤分支的 `continue` 是合理的：性质一约束的是默认视图，过滤视图的语义本来就是「只显示匹配项」。
5. 运行行为待本地验证（本实践为纯阅读任务）。

#### 4.1.5 小练习与答案

**练习 1**：`rebuild_contents` 为什么用 `mem::take` 而不是直接 `let previous = self.contents.clone()`？

**答案**：`mem::take` 把旧值零拷贝地移出（`SidebarContents: Default`，留下默认值），既省一次整表深拷贝，又在类型系统层面强制旧快照不可再被写入——本函数结尾会用全新构造的 `SidebarContents` 覆盖 `self.contents`，旧值只读地提供 `notified_threads` 一项记忆。

**练习 2**：`seen_thread_ids` 与 `current_thread_ids` 都是 `HashSet<ThreadId>`，能否合并成一个？

**答案**：不能。`seen_*` 在**单组内**去重——同一分组的多路查询会命中同一行，但不同分组理论上不会共享同一 thread_id（分组键由路径决定，四路查询以组为界）；而 `current_*` 是跨全部分组的**在册名单**，还要包含折叠分组（线程未加载进 `threads`）经 L1911-L1923 补查的 id。两者的生命周期与用途都不同：前者防重复 push，后者做收尾 GC。

**练习 3**：如果把收尾的 `notified_threads.retain(...)` 删掉，用户会看到什么 bug？

**答案**：已归档或已彻底消失的线程的通知 id 会永久残留在集合里。虽然行本身不再渲染、残留 id 通常不可见，但 `has_notifications(cx)` 会聚合计数——一旦这些 id 挂在某个仍存在但被折叠分组的补查名单里，分组头会亮着永远消不掉的通知红点。

### 4.2 四路查询与去重：entries_for_main_worktree_path / entries_for_path

#### 4.2.1 概念说明

线程/终端元数据可能被「多重归属」：一条在 linked worktree `/wt-feature` 里打开的线程，`main_worktree_paths` 是主仓库 `/project`，`folder_paths` 是 `/wt-feature`；分组键又是 `/project`。于是从分组视角找「本组有哪些行」时，同一个行能从多个索引起被命中。侧边栏的做法是**宁可多查、以首见为准**：四路查询全部执行，用 `seen_*` 集合把首见之外的重复全部丢弃。

四路查询（线程与终端各一套，结构完全对称）：

| # | 查询 | 索引键 | 命中什么样的行 | 行的 workspace 归属 |
| --- | --- | --- | --- | --- |
| 1 | `entries_for_main_worktree_path(group_key.path_list())` | main 路径列 | 新式行：主路径与分组键一致（含在 linked worktree 打开的） | 按 `row.folder_paths()` 现查：命中组内工作区 → `Open`，否则 `Closed` |
| 2 | `entries_for_path(group_key.path_list())` | folder 路径列 | 旧式行：没有 main 列时代的存量数据 | 同上 |
| 3 | `entries_for_path(每个打开工作区的路径)` | folder 路径列 | 存量行落在组内某个具体工作区上 | 直接 `Open(该工作区)` |
| 4 | `entries_for_path(每个 linked worktree 路径)` | folder 路径列 | 存量行落在某个 linked worktree 上 | 固定 `Closed`（linked worktree 未打开） |

#### 4.2.2 核心流程

```
对每个 group：
  seen = {}（线程、终端各一个全局 seen 集合）
  查询 1 ─┐
  查询 2 ─┤   每行：seen.insert(id) 失败 ⇒ 丢弃
  查询 3 ─┤         成功   ⇒ 构造 entry（按查询各自的方式定 workspace）
  查询 4 ─┘
```

查询顺序即优先级：**先到先得的查询决定行的 workspace 归属**。查询 1 最先跑，因此新式行的归属由它说了算；查询 3 只能捡到查询 1、2 都没碰过的存量行——这正是它注释里说的「三路都可能 miss 的 stale 行」兜底场景。

#### 4.2.3 源码精读

先看 Store 侧的两个索引查询（线程版；终端版结构相同）：

[crates/agent_ui/src/thread_metadata_store.rs:L621-L655](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/agent_ui/src/thread_metadata_store.rs#L621-L655)

```rust
pub fn entries_for_path<'a>(&'a self, path_list: &PathList, remote_connection: …)
    -> impl Iterator<Item = &'a ThreadMetadata> + 'a {
    self.threads_by_paths.get(path_list).into_iter().flatten()
        .filter_map(|s| self.threads.get(s))
        .filter(|s| !s.archived)
        .filter(move |s| s.matches_remote_connection(remote_connection))
}

pub fn entries_for_main_worktree_path<'a>(&'a self, path_list: &PathList, remote_connection: …)
    -> impl Iterator<Item = &'a ThreadMetadata> + 'a {
    self.threads_by_main_paths.get(path_list).into_iter().flatten()
        // …同上
}
```

两个函数唯一区别是走哪张 `HashMap<PathList, …>` 索引：`threads_by_paths` 按 folder 列、`threads_by_main_paths` 按 main 列。两者都排除已归档行、按远程连接身份过滤（本地窗口不显示 SSH 远端项目的线程）。终端版在 [crates/agent_ui/src/terminal_thread_metadata_store.rs:L206-L240](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/agent_ui/src/terminal_thread_metadata_store.rs#L206-L240)。

侧边栏侧，每组的组内准备与终端四路查询：

[crates/sidebar/src/sidebar.rs:L1439-L1482](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1439-L1482)

```rust
for group in &groups {
    let workspace_by_path_list: HashMap<PathList, &Entity<Workspace>> = group_workspaces
        .iter().map(|ws| (workspace_path_list(ws, cx), ws)).collect();
    let resolve_workspace = |folder_paths: &PathList| -> ThreadEntryWorkspace {
        workspace_by_path_list.get(folder_paths)
            .map(|ws| ThreadEntryWorkspace::Open((*ws).clone()))
            .unwrap_or_else(|| ThreadEntryWorkspace::Closed {
                folder_paths: folder_paths.clone(),
                project_group_key: group_key.clone(),
            })
    };
    // …
    let mut push_terminal_metadata =
        |metadata: TerminalThreadMetadata, workspace: ThreadEntryWorkspace| {
            if !seen_terminal_ids.insert(metadata.terminal_id) { return; }
            terminals.push(make_terminal_entry(metadata, workspace));
        };
```

`resolve_workspace` 是 u2-l2 讲过的 `Open`/`Closed` 判定：以 `PathList` 为键查组内工作区表，一次现查、从不缓存。`push_terminal_metadata` 闭包把去重与 push 绑在一起——`HashSet::insert` 返回 false 即「已见过」，直接返回，后面的四路循环只管往里喂：

[crates/sidebar/src/sidebar.rs:L1483-L1526](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1483-L1526)

```rust
for row in terminal_store.read(cx)
    .entries_for_main_worktree_path(group_key.path_list(), group_host.as_ref()).cloned()
{
    let workspace = resolve_workspace(row.folder_paths());
    push_terminal_metadata(row, workspace);
}
for row in terminal_store.read(cx).entries_for_path(group_key.path_list(), …) { /* 同上 */ }
for ws in group_workspaces {                       // 查询 3
    for row in terminal_store.read(cx).entries_for_path(&ws_paths, …) {
        push_terminal_metadata(row, ThreadEntryWorkspace::Open(ws.clone()));
    }
}
for worktree_path_list in &linked_worktree_path_lists {   // 查询 4
    for row in terminal_store.read(cx).entries_for_path(worktree_path_list, …) {
        push_terminal_metadata(row, ThreadEntryWorkspace::Closed { … });
    }
}
```

线程侧四路（[crates/sidebar/src/sidebar.rs:L1588-L1669](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1588-L1669)）一模一样，只是去重点写成 `if !seen_thread_ids.insert(row.thread_id) { continue; }`。查询 1 上方的注释值得细读（L1588-L1591）：**main 列在新线程上总是写分组键的规范路径**，不管线程实际在哪个 linked worktree 打开——所以查询 1 是主通道，2、3、4 是为存量与异常数据兜底。查询 3 的注释（L1620-L1630）还给出了一种真实故障模式：`main_worktree_paths` 与分组键不一致的 stale 行（例如 main 列被误写成等于 folder 列），要等下次 `handle_conversation_event` 重写元数据才能自愈，在那之前查询 3 保证它至少显示在「它实际所属工作区」的分组下。

`group_host.as_ref()` 来自 `ProjectGroupKey::host()`（远程 SSH 连接信息），与 Store 侧 `matches_remote_connection` 配对——**同一台机器上同路径的本地项目与远端项目是两个分组、两套行**。

#### 4.2.4 代码实践（源码阅读型 + 可选运行）

1. **实践目标**：用 `test_terminal_metadata_is_deduped_across_project_groups` 验证「同一行被两路命中时只出现一次」。
2. **操作步骤**：
   - 在仓库根目录运行：`cargo test -p sidebar --lib test_terminal_metadata_is_deduped_across_project_groups`。
   - 阅读该测试（[crates/sidebar/src/sidebar_tests.rs:L1935-L2013](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar_tests.rs#L1935-L2013)）：它建了 `/project-a`、`/project-b` 两个分组，然后手工保存一条 `worktree_paths = WorktreePaths::from_path_lists(main=["/project-b"], folder=["/project-a"])` 的终端元数据——main 列指向 B 分组、folder 列指向 A 分组，同时落入两个分组的查询范围。
   - 数一数断言：`entries` 中 `terminal_id` 等于该 id 的行数必须恰好为 1。
3. **需要观察的现象**：测试通过；再推演一遍它为什么通过——A 分组的查询 2（folder=A）与 B 分组的查询 1（main=B）都会命中这行，但 `seen_terminal_ids` 是跨分组共享的（声明在 L1370、分组循环之外），后到的查询被丢弃。这条行的 workspace 归属由**先遍历到的分组**决定。
4. **预期结果**：断言 count == 1 成立。（运行输出待本地验证。）
5. 选做：把测试里 `WorktreePaths::from_path_lists` 的两个参数对调，预测断言是否仍通过，再运行对照。

#### 4.2.5 小练习与答案

**练习 1**：为什么去重集合 `seen_thread_ids` 必须声明在分组循环**外**，而 `workspace_by_path_list` 必须在循环**内**？

**答案**：`seen_*` 需要跨分组生效——同一条元数据可能同时落入多个分组的查询范围（上节测试正是构造了这种场景），跨组去重才能保证全局唯一。而 `workspace_by_path_list` 是「当前分组的路径 → 工作区」映射，不同分组的键与值都不同，每组重建一份。

**练习 2**：一条线程在 `/wt-feature`（`/project` 的 linked worktree）里打开且工作区当前打开着。它走哪路查询、最终 `workspace` 是什么？

**答案**：查询 1 命中——它的 `main_worktree_paths = ["/project"]` 等于分组键。`resolve_workspace` 拿它的 `folder_paths = ["/wt-feature"]` 查组内工作区表：工作区若以 `/wt-feature` 为根打开着则 `Open`，否则 `Closed`。查询 4 只兜「工作区没打开」的存量行场景。

**练习 3**：四路查询会不会有行一条都命中不了？什么情况下会？

**答案**：会。若某存量行的 `folder_paths` 与 `main_worktree_paths` 都不等于分组键、不等于组内任何打开工作区的路径、也不等于任何已收集的 linked worktree 路径，它对本组不可见——它会挂到路径真正匹配的**其他**分组下；若这样的分组不存在（例如目录被删），这条行就从侧边栏消失（数据仍在数据库里，u8 的归档/恢复流程可触达）。

### 4.3 路径消歧与分支名映射：compute_disambiguation_details 与 branch_by_path

#### 4.3.1 概念说明

两个分组可能都叫 `zed`（比如 `/code/zed` 与 `/worktrees/focal-arrow/zed`）。分组头只显示最后一级目录名时会撞名，路径消歧负责决定「每个路径要显示几级尾巴才不重名」：`zed` → `focal-arrow/zed` 与 `code/zed`。分支名映射则是另一件小事：行内 worktree 徽标旁的 git 分支名（如 `feature-a`）不在元数据数据库里，只能从**当前打开工作区**的 git 仓库快照现场抓。

#### 4.3.2 核心流程

```
所有分组的所有路径
  → sort_unstable + dedup                      ← 关键：先去重！
  → compute_disambiguation_details(paths, |path, detail| path_suffix(path, detail))
      循环：
        给每个路径算「当前 detail 级」的描述串（取最后 detail+1 级目录）
        描述串相同的路径成组 → 组内 detail 全部 +1
        直到没有冲突（或描述不再随 detail 变化 ⇒ 认定无法区分，停机）
  → path_detail_map: HashMap<PathBuf, usize>

每个打开工作区 → project.repositories → 每个 repo 快照
  → 快照 branch              ⇒ branch_by_path[work_directory_abs_path]
  → 每个 linked worktree 分支 ⇒ branch_by_path[linked.path]
```

#### 4.3.3 源码精读

**消歧的准备与调用点。**

[crates/sidebar/src/sidebar.rs:L1404-L1415](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1404-L1415)

```rust
let mut all_paths: Vec<PathBuf> = groups
    .iter()
    .flat_map(|group| group.key.path_list().paths().iter().cloned())
    .collect();
all_paths.sort_unstable();
all_paths.dedup();
let path_details =
    util::disambiguate::compute_disambiguation_details(&all_paths, |path, detail| {
        project::path_suffix(path, detail)
    });
let path_detail_map: HashMap<PathBuf, usize> =
    all_paths.into_iter().zip(path_details).collect();
```

为什么必须先 `dedup`：两个分组键可以包含同一条路径（如 `zed` 单独一组、`zed+roc` 又一组）。`compute_disambiguation_details` 靠「描述串相同 ⇒ 冲突」工作，完全相同的两条路径会互相视为冲突、把 detail 一路推到全路径。util crate 里专门有一个测试锁定这个坑：[crates/util/src/disambiguate.rs:L154-L201](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/util/src/disambiguate.rs#L154-L201)，注释明确写着「A naive flat_map collects duplicates…The fix is to deduplicate before disambiguating」。

**算法本体。**

[crates/util/src/disambiguate.rs:L14-L58](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/util/src/disambiguate.rs#L14-L58)

```rust
loop {
    let mut any_collisions = false;
    for (index, (item, &detail)) in items.iter().zip(&details).enumerate() {
        if detail > 0 {
            let new_description = get_description(item, detail);
            if new_description == current_descriptions[index] { continue; } // 固定点：不再变
            current_descriptions[index] = new_description;
        }
        descriptions.entry(current_descriptions[index].clone())
            .or_insert_with(Vec::new).push(index);
    }
    for (_, indices) in descriptions.drain() {
        if indices.len() > 1 {
            any_collisions = true;
            for index in indices { details[index] += 1; }
        }
    }
    if !any_collisions { break; }
}
```

「描述串」由 `path_suffix` 生成（[crates/project/src/project.rs:L6494-L6506](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/project/src/project.rs#L6494-L6506)）：取路径**末尾** `detail + 1` 级普通目录成分（忽略 `/`、`.` 等特殊成分）拼成串——detail 0 是 `zed`，detail 1 是 `code/zed`。停机保证在 L31-L33：某条路径的描述随 detail 不再变化（已到根）时它被跳过，两条真同名的路径最终停在同一个描述上但循环因「描述不变 ⇒ 不再进冲突表」而终止。

**消费点：分组标签。**

[crates/sidebar/src/sidebar.rs:L1541](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1541) 处 `let label = group_key.display_name(&path_detail_map);`，而 `display_name`（[crates/project/src/project.rs:L6458-L6482](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/project/src/project.rs#L6458-L6482)）对分组键里每条路径查自己的 detail、生成后缀、去掉 bare clone 的 `.git` 扩展名后用 `, ` 连接；一条路径都没有时显示 `Empty Workspace`。

**分支名收集。**

[crates/sidebar/src/sidebar.rs:L1417-L1437](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1417-L1437)

```rust
let mut branch_by_path: HashMap<PathBuf, SharedString> = HashMap::new();
for ws in &workspaces {
    let project = ws.read(cx).project().read(cx);
    for repo in project.repositories(cx).values() {
        let snapshot = repo.read(cx).snapshot();
        if let Some(branch) = &snapshot.branch {
            branch_by_path.insert(
                snapshot.work_directory_abs_path.to_path_buf(),
                SharedString::from(Arc::<str>::from(branch.name())),
            );
        }
        for linked_wt in snapshot.linked_worktrees() {
            if let Some(branch) = linked_wt.branch_name() {
                branch_by_path.insert(linked_wt.path.clone(), …);
            }
        }
    }
}
```

这份表由行构造时的 `worktree_info_from_thread_paths(&row.worktree_paths, &branch_by_path)` 消费（线程版在 L1566-L1567，终端版在 L1460-L1461）。该函数（[crates/agent_ui/src/thread_metadata_store.rs:L374-L436](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/agent_ui/src/thread_metadata_store.rs#L374-L436)）把每对 `(main, folder)` 变成一个行内徽标：`main == folder` 是主工作树徽标，否则是 linked 徽标。linked 短名来自 `linked_worktree_short_name`（[crates/project/src/git_store.rs:L10627-L10647](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/project/src/git_store.rs#L10627-L10647)）：默认取 folder 的末级目录名，若与主仓库目录同名则改取 folder 的**父目录**名（两个都叫 `zed` 的工作树靠这个区分）；当行涉及多个主项目且短名不一致时，`worktree_info_from_thread_paths` 再给 linked 徽标加 `项目名:短名` 前缀。徽标的 `branch_name: branch_names.get(folder_path).cloned()`——查不到就是 `None`，只显示目录名。注意：**只有打开的工作区能贡献分支名**，已关闭分组（`Closed` 行）的分支徽标自然是空的——git 状态必须真实打开仓库才能读。

#### 4.3.4 代码实践（可运行）

1. **实践目标**：单独验证消歧算法的行为，建立对 detail 数值的直觉。
2. **操作步骤**：
   - 运行 util crate 的算法单测：`cargo test -p util --lib disambiguate`（覆盖 `test_no_conflicts`、`test_simple_two_way_conflict`、`test_deeper_conflict`、`test_duplicate_paths_from_multiple_groups` 等 8 个用例）。
   - 手动推演 `test_deeper_conflict`（[crates/util/src/disambiguate.rs:L96-L111](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/util/src/disambiguate.rs#L96-L111)）：三条路径在 detail 0 都叫 `file.rs`，detail 1 时前两条同为 `src/file.rs`、第三条是 `lib/file.rs`，detail 2 才全部分开。期望 `details == [2, 1, 1]`——注意第三条在 detail 1 已经唯一，**停在 1**，不会陪着前两条升到 2。
3. **需要观察的现象**：全部测试绿；`[2, 1, 1]` 说明 detail 是「逐条路径各自的最小消歧级别」，不是全表统一深度。
4. **预期结果**：8 个用例全部通过。（运行输出待本地验证。）
5. 选做（阅读）：在 `disambiguate.rs` 的测试模块里临时加一条自己的用例（如四条路径两两级联冲突），预测 details 后运行对照，验证后撤销修改。

#### 4.3.5 小练习与答案

**练习 1**：分组键 A = `/a/zed`、B = `/b/zed`、C = `/code/roc`，`path_detail_map` 会是什么？

**答案**：`{ "/a/zed": 1, "/b/zed": 1, "/code/roc": 0 }`。前两条在 detail 0 都叫 `zed` 产生冲突、各升到 1 变成 `a/zed` 与 `b/zed`；`roc` 第一轮就唯一，保持 0。分组头分别显示 `a/zed`、`b/zed`、`roc`。

**练习 2**：两条路径完全相同（没去重就喂进去），算法会死循环吗？

**答案**：不会。detail 升到路径全部成分都用上后，`get_description(item, detail)` 返回值不再随 detail 变化，L31-L33 的固定点检查让它们退出冲突统计，循环终止——两条都停在全路径 detail，显示串相同但算法收敛。这正是 `test_identical_items_terminates` 锁定的行为；侧边栏侧用 `dedup` 从源头避免走到这一步。

**练习 3**：为什么分支名表不放进 `ThreadMetadata` 数据库、而是每轮重建现场收集？

**答案**：分支是**工作区的瞬态**——checkout、切分支随时发生，而元数据行是持久化的历史记录；把瞬态写进持久库会立刻过期。现场收集保证徽标永远反映当前分支，代价是只有打开的工作区才有分支可显示。这也符合本 crate「能现查的不存」的架构约束。

### 4.4 apply_active_info：数据库打底、活跃信息覆盖

#### 4.4.1 概念说明

四路查询拿到的是数据库行：标题可能是旧的、状态字段是默认值、没有 diff 统计。而正在运行的会话活在 `AgentPanel` 里。`apply_active_info` 是两者的黏合剂——以 `session_id` 为键，把面板里的 `ActiveThreadInfo` **整体覆盖**到 `ThreadEntry` 的一组字段上。匹配键选 `session_id` 而非 `thread_id` 是 u2-l3 讲过的理由：线程恢复/重开时本地 `ThreadId` 可重铸，远端会话 id 恒稳定。

#### 4.4.2 核心流程

```
组内：
  live_infos = 组内所有工作区面板的 ActiveThreadInfo 流
      └─ 建索引 live_info_by_session: HashMap<SessionId, ActiveThreadInfo>
         （顺便累计 has_running_threads / waiting_thread_count）

  对 threads 中每一行：
    metadata.session_id 命中 live_info_by_session？
      ├─ 是 ⇒ Arc::make_mut(thread).apply_active_info(info)
      │        new_live_statuses[session_id] = (status, thread_id)   ← 记录「这一刻」
      └─ 否 ⇒ 保持数据库值（is_live = false）

    通知检测：
      status == Completed
        && 不是当前活跃线程
        && old_statuses[session_id] 的状态 == Running      ← 对比「上一刻」
      ⇒ notified_threads.insert(thread_id)

    是活跃线程且非后台 ⇒ notified_threads.remove(thread_id)
```

折叠分组走另一条对称路径：行不加载，但 `live_infos` 仍被处理（状态经 `old_statuses` 或 `entry_by_session` 反查 thread_id，照样检测跳变），保证折叠时不漏通知。

#### 4.4.3 源码精读

**合并本体。**

[crates/sidebar/src/sidebar.rs:L379-L388](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L379-L388)

```rust
fn apply_active_info(&mut self, info: &ActiveThreadInfo) {
    self.metadata.title = Some(info.title.clone());
    self.status = info.status;
    self.icon = info.icon;
    self.icon_from_external_svg = info.icon_from_external_svg.clone();
    self.is_live = true;
    self.is_background = info.is_background;
    self.is_title_generating = info.is_title_generating;
    self.diff_stats = info.diff_stats;
}
```

八个字段全部无条件覆盖——活跃信息的任一字段都比数据库新（数据库只在会话落盘时更新）。`is_live = true` 同时是渲染层的开关：u2-l1 讲过「行是否带实时状态徽标」由它决定。注意哪些字段**没有**被覆盖：`metadata.thread_id`、`session_id`、`worktree_paths`、时间戳、`workspace`——这些是身份与归属，活跃信息无权改写。

**索引与计数一遍扫。**

[crates/sidebar/src/sidebar.rs:L1704-L1716](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1704-L1716)

```rust
let mut live_info_by_session: HashMap<acp::SessionId, ActiveThreadInfo> = HashMap::new();
for info in live_infos {
    if info.status == AgentThreadStatus::Running { has_running_threads = true; }
    if info.status == AgentThreadStatus::WaitingForConfirmation { waiting_thread_count += 1; }
    live_info_by_session.insert(info.session_id.clone(), info);
}
```

`has_running_threads` 与 `waiting_thread_count` 是分组头徽标（转圈/等待计数）的数据源，在索引构建的同一循环里顺带统计——呼应文档注释的「单次前向遍历」纪律。

**合并、跳变检测、通知清理一遍扫。**

[crates/sidebar/src/sidebar.rs:L1718-L1751](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1718-L1751)

```rust
for thread in &mut threads {
    if let Some(session_id) = thread.metadata.session_id.clone() {
        if let Some(info) = live_info_by_session.get(&session_id) {
            let status = info.status;
            let thread_id = thread.metadata.thread_id;
            Arc::make_mut(thread).apply_active_info(info);
            new_live_statuses.insert(session_id, (status, thread_id));
        }
    }
    // …
    if thread.status == AgentThreadStatus::Completed
        && !is_active_thread
        && session_id.as_ref().and_then(|sid| old_statuses.get(sid))
            .is_some_and(|(s, _)| *s == AgentThreadStatus::Running)
    {
        notified_threads.insert(thread.metadata.thread_id);
    }
    if is_active_thread && !thread.is_background {
        notified_threads.remove(&thread.metadata.thread_id);
    }
}
```

三个要点：

- `Arc::make_mut`：`ThreadEntry` 包在 `Arc` 里，`make_mut` 在引用计数为 1 时原地改、否则克隆再改——与排序、草稿后处理共享同一套「先保守构造、后零拷贝修补」的手法（u2-l1）。
- 跳变检测的三个条件缺一不可：现在 `Completed`（不是 Error，也不是还在跑）、用户正看着别的线程（`!is_active_thread`，看着的线程完成不需要打扰）、上一刻是 `Running`（首见即 Completed 的不通知——比如重建期间面板刚恢复）。
- 正在看的前台线程完成时立刻清掉通知（最后一行）——「你回来了，红点消掉」。

**折叠分组的对称路径。**

[crates/sidebar/src/sidebar.rs:L1758-L1795](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1758-L1795)

```rust
} else {
    for info in live_infos {
        // …统计 Running / WaitingForConfirmation…
        let thread_id = old_statuses.get(&info.session_id).map(|(_, tid)| *tid)
            .or_else(|| ThreadMetadataStore::global(cx).read(cx)
                .entry_by_session(&info.session_id).map(|m| m.thread_id));
        if let Some(thread_id) = thread_id {
            // …Running → Completed ⇒ notified_threads.insert(thread_id)…
        }
    }
    if is_active && let Some(ActiveEntry::Thread { thread_id, .. }) = self.active_entry.as_ref() {
        notified_threads.remove(thread_id);
    }
}
```

折叠时线程行不构造（性能考虑，`should_load_threads == false`），但活跃信息依然要处理：thread_id 先从记忆字段 `old_statuses` 反查，查不到再问 Store 的 `entry_by_session`（[crates/agent_ui/src/thread_metadata_store.rs:L594-L597](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/agent_ui/src/thread_metadata_store.rs#L594-L597)）——首帧记忆字段还是空表，反查兜底保证第一轮就能记下 thread_id。折叠分组头仍要正确亮通知红点、显示运行徽标，靠的就是这条路径。配合它的还有 L1798-L1812 的 `has_stored_thread_rows`：用 `thread_metadata_would_render_sidebar_row`（[crates/sidebar/src/sidebar.rs:L330-L340](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L330-L340)，本质是「非草稿恒真；草稿要看能否拿到显示标签」）判断折叠组里「如果展开会不会有线」，从而决定「No threads yet」空态行是否会出现——不物化任何行就完成判定。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：给 `ThreadEntry` 的字段做一张「数据来源」归属表。
2. **操作步骤**：
   - 对照 [crates/sidebar/src/sidebar.rs:L348-L362](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L348-L362) 的字段清单，逐个标注来源：数据库行（`metadata` 全部）、构造闭包默认值（`status`、`is_live` 等）、`apply_active_info` 覆盖（8 个字段）、草稿后处理（`draft`、`title`）、组内派生（`workspace`、`worktrees`）、搜索过滤（`highlight_positions`）。
   - 再读一遍 L1738-L1744 的跳变条件，回答：一个线程在你**正看着它**时跑完了，通知集合会变化吗？（提示：`is_active_thread` 挡住了 insert，且 L1748-L1750 还会主动 remove。）
3. **需要观察的现象**：8 个被覆盖字段与 u2-l1 里「is_live 只能来自进程内存态」的结论一一对应；时间戳类字段（`updated_at` / `interacted_at`）从不出现在覆盖列表里。
4. **预期结果**：得到一张 12 字段的来源表，其中 `metadata.title` 是唯二被两处修改的字段（草稿后处理与活跃覆盖），谁后执行？——合并循环在草稿 retain 之后，所以活跃标题最终生效；而草稿（`session_id == None`）永远匹配不到活跃信息，两个写入点实际互斥。
5. 运行行为待本地验证（本实践为纯阅读任务）。

#### 4.4.5 小练习与答案

**练习 1**：`old_statuses` 里的值是 `(AgentThreadStatus, ThreadId)` 二元组，为什么必须连同 thread_id 一起记？

**答案**：跳变检测的产出是 `notified_threads.insert(thread_id)`——通知集合以 thread_id 为键。折叠分支里（L1769-L1777）活跃信息只有 session_id，要反查 thread_id 就得靠记忆字段里存的这份配对；不存的话每次都要再查一次 Store 的 `entry_by_session`。同时收尾 `new_live_statuses` 写回时也带着 thread_id，保证下一轮重建（可能换到折叠分支）仍有完整配对可用。

**练习 2**：一个线程在面板里从 Running → Completed，但它是当前活跃线程；随后用户切到别的线程。两次重建后它在通知集合里吗？

**答案**：不在。第一次重建：状态变为 Completed，但 `is_active_thread == true` 挡住 insert，且 L1748-L1750 还会 remove（清理此前可能残留的通知）；`new_live_statuses` 已记录 Completed。第二次重建：跳变条件要求 `old_statuses == Running`，而上一轮已记为 Completed——跳变只在这一个重建窗口内可检测，错过即不再触发。用户切换后看到的分组头红点不会为这个线程亮起。

**练习 3**：`WaitingForConfirmation` 也算「值得注意的完成」吗？它如何体现在分组头上？

**答案**：它不触发通知（跳变条件只认 Completed），但它在索引构建循环里单独计数为 `waiting_thread_count`，作为分组头的等待徽标数据源（L1936、L1889 传给 `ListEntry::ProjectHeader`）。也就是说：运行徽标看 `has_running_threads`、等待徽标看计数、红点看通知集合——三者在同一次遍历里产出。

## 5. 综合实践

**任务：绘制 rebuild_contents 的阶段流程图并用真实测试验证去重逻辑。**

1. **实践目标**：把本讲四个最小模块串成一张完整的「数据 → 行」流水线图，并用一个真实测试锚定对去重的理解。
2. **操作步骤**：
   - **第一步（画图）**：以 4.1.2 的骨架为底，给每个阶段标注它读取的全局 Store 与记忆字段。至少覆盖：`ThreadMetadataStore::global`（两个索引各出现在哪几路查询）、`TerminalThreadMetadataStore::global`、`draft_prompt_store`、各工作区的 `AgentPanel`（活跃信息与终端通知）、`project.repositories`（分支名）、`MultiWorkspace::project_groups`（分组清单与折叠状态）、`Sidebar` 自身的 `live_thread_statuses` / `contents.notified_threads`。用不同颜色/符号区分「持久层 / 内存态 / 记忆字段」。
   - **第二步（推演）**：在图上追踪一条具体数据——`main=["/repo"], folder=["/repo-wt"]` 的线程，工作区没打开 `/repo-wt`：它从哪个分组的哪路查询进入、`resolve_workspace` 返回什么、分支徽标有没有值（提示：`branch_by_path` 只收打开工作区的仓库；若主仓库开着，`/repo-wt` 作为 linked worktree 是否在 `snapshot.linked_worktrees()` 里？）。
   - **第三步（验证）**：运行 `cargo test -p sidebar --lib test_terminal_metadata_is_deduped_across_project_groups`，然后按 4.2.4 的步骤阅读该测试的断言，对照你图里的 `seen_terminal_ids` 节点解释 count == 1。
3. **需要观察的现象**：图上「同一个 Store 被多路查询读多次」与「seen 集合在分组循环外」两个结构特征；测试恰好构造了跨分组重复命中的场景。
4. **预期结果**：一张标注完整的流程图 + 一段对测试为何通过的解释（含「行的归属由先遍历到的分组决定」这一细节）。测试运行输出待本地验证。

## 6. 本讲小结

- `rebuild_contents` 是一次**纯读取的全量推导**：`mem::take` 让位旧快照、只继承 `notified_threads` 一项记忆，输出全新 `SidebarContents`；文档注释要求单次前向遍历 + \(O(T \log T)\) 排序，因为它在每个事件后都会完整执行。
- 线程与终端各走**四路查询**（main 索引 → 分组键 folder 索引 → 各打开工作区 folder 索引 → 各 linked worktree folder 索引），`seen_*` 集合跨分组去重，**先到的查询决定行的 `Open`/`Closed` 归属**；查询 1 是新式行的主通道，其余为存量与 stale 数据兜底。
- 路径消歧是「逐级加深尾部目录直到不重名」的迭代算法，入口处必须先排序去重（否则重复路径把 detail 推到全路径）；`display_name` 消费 detail 生成不撞名的分组标签。
- 分支名不在数据库里，每轮从**打开工作区**的 git 快照现场收集进 `branch_by_path`，由 `worktree_info_from_thread_paths` 映射到行内 worktree 徽标——已关闭分组的行因此没有分支徽标。
- `apply_active_info` 以 `session_id` 为键把面板活跃信息整体覆盖到 8 个字段上（`is_live = true` 是实时徽标开关）；「Running → Completed 且非活跃线程」的跳变靠 `old_statuses` 记忆字段检测，折叠分组走 `entry_by_session` 反查的对称路径，保证折叠时通知与徽标依然正确。
- 收尾 GC 用 `current_*` 在册名单裁剪通知集合与两个 MRU 时间戳表——记忆字段不随行消失而泄漏。

## 7. 下一步学习建议

本讲补完了 u3 数据流单元的最后一块：十六类事件汇入 `schedule_update_entries`（u3-l2），经 `rebuild_contents` 全量重推导（本讲），再由 `apply_list_state_diff`（u3-l3）保住列表测量。接下来两条路：

- **走向渲染**（u4 单元）：`contents.entries` 如何变成屏幕上的行——先读 u4-l1 的 `Render for Sidebar` 主骨架，再看 u4-l2/u4-l3 里 `ListEntry::ProjectHeader` 的 `has_running_threads` / `waiting_thread_count` / `has_notifications` / `is_active` 字段（本讲 L1885-L1894 压入的）如何渲染成徽标，以及 `highlight_positions`（本讲搜索分支写入的）如何变成高亮区间。
- **走向交互**（u5/u6 单元）：`entry_shapes` 消费的 `is_collapsed` 与本讲的折叠分支如何联动（u4-l2）；`thread_last_accessed`（本讲收尾裁剪的表）如何喂给 ctrl-tab 切换器排序（u7-l2）。

建议继续精读的源码：`crates/project/src/worktree_store.rs` 的 `WorktreePaths::add_path`（理解 main/folder 配对如何随会话事件演化，解释「stale 行自愈」）；`crates/agent_ui/src/threads_archive_view.rs` 的 `fuzzy_match_positions`（本讲搜索分支的匹配引擎）。
