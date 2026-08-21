# 列表行模型：ListEntry、ThreadEntry 与 TerminalEntry

## 1. 本讲目标

学完本讲，你应该能够：

- 说出侧边栏列表中三种行（项目分组头 `ProjectHeader`、线程 `Thread`、终端 `Terminal`）各自的字段与用途。
- 逐字段说明 `ThreadEntry` 与 `TerminalEntry` 的数据来自哪里（数据库元数据、活跃线程信息、特性开关、过滤匹配……）。
- 解释 `SidebarContents` 这个「一次重建的完整快照」里，除了 `entries` 之外，通知集合（`notified_threads` / `notified_terminals`）与项目头索引（`project_header_indices`）分别在为谁服务。
- 理解 `From<ThreadEntry> for ListEntry` 这类转换的意义，以及为什么 `Thread` 变体包了一层 `Arc`。

本讲是纯「数据模型」讲：不追渲染、不追键盘交互，只回答一个问题——**侧边栏列表里的每一行，究竟是一个什么样的 Rust 值，它是从哪些数据源拼出来的**。理解了这个，后续第三单元的重建管线（`rebuild_contents`）和第四单元的渲染才有着力点。

## 2. 前置知识

- **实体与全局状态（GPUI）**：`Entity<T>` 是对状态 `T` 的句柄，`Entity<Workspace>` 表示一个打开的工作区。全局单例可以用 `SomeStore::global(cx)` 取得。本讲里出现的 `ThreadMetadataStore`、`TerminalThreadMetadataStore` 都是全局存储。
- **「每次全量重推导」**（u1-l3 已建立）：侧边栏不维护增量状态，任何变化都走 `update_entries → rebuild_contents`，从当前世界状态重新算出整份列表。本讲的 `SidebarContents` 就是这份「重新算出来的结果」的类型。
- **元数据 vs 活跃信息**：
  - *元数据（metadata）*：持久化在数据库里的轻量记录——线程的标题、时间戳、worktree 路径等，进程重启后仍在。代表类型是 `ThreadMetadata` 与 `TerminalThreadMetadata`。
  - *活跃信息（live info）*：只存在于当前进程内存里的状态——某个线程此刻正在运行、标题正在生成中、diff 统计是多少。代表类型是 `ActiveThreadInfo`。
  - 一行线程的最终样子 = 数据库元数据打底 + 活跃信息覆盖。这是本讲最重要的一条主线。
- **`Option` 与 `HashSet`**：`Option<T>` 表示「可能没有」；`HashSet<T>` 是去重集合，本讲里用它表示「哪些 ID 带通知」和「哪些 ID 已经见过」。
- **`Arc<T>`**：原子引用计数的共享指针，克隆它只复制指针不复制数据。后面会解释 `ListEntry::Thread(Arc<ThreadEntry>)` 为什么这样设计。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [crates/sidebar/src/sidebar.rs](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs) | 库根，`Sidebar` 实体与全部行模型定义 | `ThreadEntry`、`TerminalEntry`、`ListEntry`、`SidebarContents` 的定义，以及 `rebuild_contents` 中构造它们的位置 |
| [crates/agent_ui/src/thread_metadata_store.rs](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/agent_ui/src/thread_metadata_store.rs) | 线程元数据存储（数据库） | `ThreadMetadata` 的字段，`ThreadEntry.metadata` 的来源 |
| [crates/agent_ui/src/terminal_thread_metadata_store.rs](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/agent_ui/src/terminal_thread_metadata_store.rs) | 终端线程元数据存储 | `TerminalThreadMetadata` 的字段，`TerminalEntry.metadata` 的来源 |
| [crates/project/src/project.rs](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/project/src/project.rs) | 项目与项目分组键 | `ProjectGroupKey`：`ProjectHeader` 变体的 `key` 字段类型 |
| [crates/sidebar/src/sidebar_tests.rs](https://github.com/zed-industries-zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs) | 测试 | `visible_entries_as_strings`：把 `ListEntry` 压平成字符串的断言出口，实践环节会用到 |

## 4. 核心概念与源码讲解

### 4.1 ListEntry：三种行的统一枚举

#### 4.1.1 概念说明

侧边栏列表在屏幕上看起来有三种长得不一样的行：

1. **项目分组头**：比如 `my-project`，可折叠，右侧可能有「运行中」「等待确认」的状态徽标。
2. **线程行**：一个 Agent 会话，带图标、标题、状态。
3. **终端行**：一个 Agent 终端会话。

渲染层（gpui 的虚拟列表）只认「第 i 个元素」，不关心它是哪种行。所以需要一个统一类型把三种行装进同一个 `Vec`——这就是 `ListEntry` 枚举。这是 Rust 里非常典型的「闭合求和类型」用法：行的种类是固定且已知的，用枚举比 trait 对象更省、更安全，且 `match` 能保证穷尽处理。

#### 4.1.2 核心流程

一次重建中行的产出顺序：

```text
rebuild_contents
  ├── 按项目分组遍历（MultiWorkspace::project_groups）
  ├── 每组先收集 TerminalEntry（终端）
  ├── 再收集 ThreadEntry（线程，折叠组跳过）
  ├── 组内按展示时间倒序合并
  │       push_entries_by_display_time:
  │           Terminal(entry) 与 Thread(arc) 逐个 push 进 entries
  └── 每组开头先 push 一个 ListEntry::ProjectHeader
最终：entries = [Header, 行..., 行..., Header, 行..., ...]
```

注意 `ProjectHeader` 总是在该组的行**之前**压入，且压入前记录当前下标到 `project_header_indices`（见 4.4）。

#### 4.1.3 源码精读

枚举定义：[crates/sidebar/src/sidebar.rs:391-405](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L391-L405)

```rust
#[derive(Clone)]
enum ListEntry {
    ProjectHeader {
        key: ProjectGroupKey,
        label: SharedString,
        highlight_positions: Vec<usize>,
        has_running_threads: bool,
        waiting_thread_count: usize,
        has_notifications: bool,
        is_active: bool,
        has_threads: bool,
    },
    Thread(Arc<ThreadEntry>),
    Terminal(TerminalEntry),
}
```

- `ProjectHeader` 是**结构体变体**：8 个字段全部是「这一组的状态汇总」，而不是某一行的状态。`key` 是分组身份（见下），`label` 是显示名，`highlight_positions` 是搜索命中位置，其余 5 个布尔/计数控制徽标与高亮。
- `Thread(Arc<ThreadEntry>)` 与 `Terminal(TerminalEntry)` 是**元组变体**：直接把行模型整个装进来。线程多包一层 `Arc`，终端没有——这个不对称是刻意的，理由见 4.2.3。

`ProjectHeader` 的 `key` 字段类型定义在 project crate：[crates/project/src/project.rs:6415-6452](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/project/src/project.rs#L6415-L6452)

```rust
/// Identifies a project group by a set of paths the workspaces in this group
/// have.
///
/// Paths are mapped to their main worktree path first so we can group
/// workspaces by main repos.
pub struct ProjectGroupKey {
    /// The paths of the main worktrees for this project group.
    paths: PathList,
    host: Option<RemoteConnectionOptions>,
}
```

也就是说，一个「项目组」由「一组主 worktree 路径 + 可选的远程主机」唯一确定。linked worktree（git 工作树副本）会被归并到主 worktree 对应的组里。`ProjectGroupKey` 的更多细节（折叠状态存在 `MultiWorkspace` 上等）属于下一讲 u2-l2，这里只需知道它是分组头的身份键。

两个 `From` 转换实现：[crates/sidebar/src/sidebar.rs:463-473](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L463-L473)

```rust
impl From<ThreadEntry> for ListEntry {
    fn from(thread: ThreadEntry) -> Self {
        ListEntry::Thread(Arc::new(thread))
    }
}

impl From<TerminalEntry> for ListEntry {
    fn from(terminal: TerminalEntry) -> Self {
        ListEntry::Terminal(terminal)
    }
}
```

这两个实现表达了「任何行模型都能统一变成 `ListEntry`」的约定：调用方拿到一个 `ThreadEntry` 后直接 `.into()` 即可，不必手写变体名；`From<ThreadEntry>` 还顺手把 `Arc` 包装这步细节隐藏掉了。需要说明的是，当前热路径 [`push_entries_by_display_time`](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L5725-L5729) 里为了配合迭代器链，直接用了 `.map(ListEntry::Terminal)` / `.map(ListEntry::Thread)` 构造变体（此时线程已是 `Arc`，无需再包），`From` 实现更多是为其他调用点提供符合习惯的转换入口——阅读时不要误以为它们没被用到就等于无用。

#### 4.1.4 代码实践

**实践目标**：用测试辅助函数 `visible_entries_as_strings` 亲眼看到「`ListEntry` 枚举 → 屏幕上的行」的对应关系。

**操作步骤**：

1. 打开 [crates/sidebar/src/sidebar_tests.rs:547-590](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs#L547-L590) 附近，阅读 `visible_entries_as_strings` 的实现，注意它对三种变体分别格式化成什么字符串。
2. 在仓库根目录运行任一列表相关测试，例如：
   ```bash
   cargo test -p sidebar --lib test_single_workspace_no_threads -- --nocapture
   ```
3. 找一个断言里出现 `[项目名]` 与线程标题的测试（如 `test_search_narrows_visible_threads_to_matches`），对照 `visible_entries_as_strings` 的格式化规则，把断言中的每一行字符串反推回 `ListEntry` 的变体与字段。

**需要观察的现象**：`ProjectHeader` 被格式化为 `v [label]`（展开）或 `> [label]`（折叠）；线程行带 `*`（live）、`(running)` 等状态后缀；当前 `selection` 对应的行尾有 `<== selected`。

**预期结果**：你能对着一条断言字符串，说出它对应 `ListEntry::ProjectHeader { label, .. }` 还是 `ListEntry::Thread(t)`，以及 `t.status`、`t.is_live` 的取值。测试运行输出本身**待本地验证**（本讲义没有替你运行）。

#### 4.1.5 小练习与答案

**练习 1**：`ProjectHeader` 变体有 8 个字段，其中哪一个是「身份」，哪一些是「展示状态」？

答案：`key: ProjectGroupKey` 是身份——两个 `ProjectHeader` 相等与否取决于它，折叠状态、`EntryShape` 等都以它为键；`label`、`highlight_positions`、`has_running_threads`、`waiting_thread_count`、`has_notifications`、`is_active`、`has_threads` 都是每次重建重新推导的展示状态。

**练习 2**：为什么 `ListEntry` 用枚举而不是 `Box<dyn Row>` 这样的 trait 对象？

答案：行的种类封闭（就三种）、数量大（每个可见行一个值）、需要频繁整体 `match`。枚举是紧凑的值类型（此处除 `Arc`/`String` 外无堆分配间接层），`match` 穷尽性检查能在编译期保证新增字段时不漏处理；trait 对象会引入虚表指针和动态分发，且无法方便地按变体取出内部数据。

**练习 3**：`impl From<ThreadEntry> for ListEntry` 里做了什么额外动作？为什么 `From<TerminalEntry>` 不用做？

答案：`From<ThreadEntry>` 额外执行了 `Arc::new(thread)`，把线程行包成共享指针；而 `TerminalEntry` 在 `ListEntry` 里是裸值，所以 `From<TerminalEntry>` 只是原样装进 `Terminal` 变体。

### 4.2 ThreadEntry：线程行（数据库打底 + 活跃信息覆盖）

#### 4.2.1 概念说明

`ThreadEntry` 是一个 Agent 会话在列表里的完整呈现数据。它的 12 个字段不是同时填好的，而是分**三个阶段**：

1. **构造阶段**：从 `ThreadMetadataStore`（数据库）查出 `ThreadMetadata`，配上图标、workspace 归属等，先用保守默认值占位（`status` 取默认、`is_live = false`、`diff_stats` 取默认）。
2. **草稿后处理阶段**：没有 `session_id` 的线程是「草稿」，要尝试推导显示标签，推不出的直接从列表里剔除。
3. **活跃信息合并阶段**：若该线程在本窗口某个 `AgentPanel` 里正开着，用内存中的 `ActiveThreadInfo` 覆盖标题、状态、图标等字段，并置 `is_live = true`。

另外还有第四个「被动阶段」：当用户输入了过滤查询时，`highlight_positions` 才会被 fuzzy 匹配结果填充。

#### 4.2.2 核心流程

```text
make_thread_entry(row, workspace)          # 阶段 1：构造（保守默认值）
  ├── metadata = row                        # 来自 ThreadMetadataStore（数据库）
  ├── icon / icon_from_external_svg = resolve_agent_icon(row.agent_id)
  ├── worktrees = worktree_info_from_thread_paths(row.worktree_paths, branch_by_path)
  ├── draft = row.is_draft() ? Some(WithContent) : None
  └── status=默认, is_live=false, is_background=false,
      is_title_generating=false, diff_stats=默认, highlight_positions=空

# 阶段 2：草稿后处理
for thread in threads:
    if thread.draft.is_some():
        用 draft_display_label_for_thread_metadata 推导 (label, kind)
        能推出 → 覆盖 title、更新 draft kind
threads.retain(非草稿 或 title 非空)          # 推不出标签的草稿剔除
threads.retain(非空草稿 或 正在激活 或 当前活跃)  # 空草稿只在活跃时保留

# 阶段 3：活跃信息合并
for thread in threads:
    if metadata.session_id 在 live_info_by_session 中:
        thread.apply_active_info(info)       # 覆盖 title/status/icon/... 并置 is_live=true

# 阶段 4（可选）：有过滤查询时
thread.highlight_positions = fuzzy_match_positions(query, title)
```

#### 4.2.3 源码精读

结构体定义：[crates/sidebar/src/sidebar.rs:348-362](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L348-L362)

```rust
#[derive(Clone)]
struct ThreadEntry {
    metadata: ThreadMetadata,
    icon: IconName,
    icon_from_external_svg: Option<SharedString>,
    status: AgentThreadStatus,
    workspace: ThreadEntryWorkspace,
    is_live: bool,
    is_background: bool,
    is_title_generating: bool,
    draft: Option<DraftKind>,
    highlight_positions: Vec<usize>,
    worktrees: Vec<ThreadItemWorktreeInfo>,
    diff_stats: DiffStats,
}
```

数据库打底——`ThreadMetadata` 的定义在 agent_ui crate：[crates/agent_ui/src/thread_metadata_store.rs:306-326](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/agent_ui/src/thread_metadata_store.rs#L306-L326)

```rust
/// Lightweight metadata for any thread (native or ACP), enough to populate
/// the sidebar list and route to the correct load path when clicked.
pub struct ThreadMetadata {
    pub thread_id: ThreadId,
    pub session_id: Option<acp::SessionId>,
    pub agent_id: AgentId,
    pub title: Option<SharedString>,
    pub title_override: Option<SharedString>,
    pub updated_at: DateTime<Utc>,
    pub created_at: Option<DateTime<Utc>>,
    pub interacted_at: Option<DateTime<Utc>>,
    pub worktree_paths: WorktreePaths,
    pub remote_connection: Option<RemoteConnectionOptions>,
    pub archived: bool,
}
```

注意 doc comment 的措辞：这份元数据的设计标准就是「够填侧边栏列表、够路由到正确的加载路径」。`session_id` 为 `None` 即草稿（[is_draft](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/agent_ui/src/thread_metadata_store.rs#L329-L333)：`self.session_id.is_none()`）。

构造闭包 `make_thread_entry`：[crates/sidebar/src/sidebar.rs:1563-1586](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1563-L1586)

```rust
let make_thread_entry =
    |row: ThreadMetadata, workspace: ThreadEntryWorkspace| -> Arc<ThreadEntry> {
        let (icon, icon_from_external_svg) = resolve_agent_icon(&row.agent_id);
        let worktrees =
            worktree_info_from_thread_paths(&row.worktree_paths, &branch_by_path);
        // Start drafts as `WithContent`; the post-processing
        // pass below downgrades them to `Empty` if no draft
        // label can be derived.
        let draft = row.is_draft().then_some(DraftKind::WithContent);
        Arc::new(ThreadEntry {
            metadata: row,
            icon,
            icon_from_external_svg,
            status: AgentThreadStatus::default(),
            workspace,
            is_live: false,
            is_background: false,
            is_title_generating: false,
            draft,
            highlight_positions: Vec::new(),
            worktrees,
            diff_stats: DiffStats::default(),
        })
    };
```

图标来自 `resolve_agent_icon`（[sidebar.rs:1376-1388](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1376-L1388)）：内置 Agent 用 `IconName::ZedAgent`，自定义 Agent 用 `IconName::Terminal`，外部注册的 Agent 还可能从 `agent_server_store` 拿到一段 SVG 名。`worktrees` 字段由 agent_ui 导入的 `worktree_info_from_thread_paths` 填充，输入是元数据里的 `worktree_paths` 加上重建时预先从 git 快照收集的 `branch_by_path` 路径→分支名映射（[sidebar.rs:1417-1437](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1417-L1437)）。

活跃信息覆盖——`ActiveThreadInfo` 与 `apply_active_info`：[crates/sidebar/src/sidebar.rs:199-209](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L199-L209)、[crates/sidebar/src/sidebar.rs:373-389](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L373-L389)

```rust
struct ActiveThreadInfo {
    session_id: acp::SessionId,
    title: SharedString,
    status: AgentThreadStatus,
    icon: IconName,
    icon_from_external_svg: Option<SharedString>,
    is_background: bool,
    is_title_generating: bool,
    diff_stats: DiffStats,
}

fn apply_active_info(&mut self, info: &ActiveThreadInfo) {
    self.metadata.title = Some(info.title.clone());
    self.status = info.status;
    self.icon = info.icon;
    ...
    self.is_live = true;
    ...
}
```

合并发生在重建循环里，以 `session_id` 为键：[sidebar.rs:1718-1728](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1718-L1728)——先按 `info.session_id` 建 `live_info_by_session` 查表，再对每个线程查自己的 `metadata.session_id`，命中就 `Arc::make_mut(thread).apply_active_info(info)`。

**为什么是 `Arc<ThreadEntry>`**：后处理阶段要对 `Vec<Arc<ThreadEntry>>` 里的元素做就地修改（草稿标签覆盖、`apply_active_info`）。`Arc::make_mut` 的语义是「引用计数为 1 时直接可变借用，否则克隆一份再改」——因为构造时每个 `Arc` 的引用计数都是 1（还没分享给 `ListEntry`），这些修改是零拷贝的；而一旦被推进 `entries` 变成 `ListEntry::Thread(arc)`，下次重建时旧列表整体丢弃，也没有深拷贝开销。终端行没有这种「构造后再就地修补」的需求，所以保持裸值即可。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：为 `ThreadEntry` 的 12 个字段各写一句「数据来源」注释，并用 `rebuild_contents` 中的真实代码验证每一句。

**操作步骤**：

1. 打开 [crates/sidebar/src/sidebar.rs:348-362](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L348-L362)，在本地副本（或笔记里）为每个字段补一行注释，格式示例：
   ```text
   // 示例代码（写在你的笔记里，不要改动仓库源码）
   struct ThreadEntry {
       metadata: ThreadMetadata,            // 数据库：ThreadMetadataStore 查出的行
       icon: IconName,                      // ?
       icon_from_external_svg: Option<...>, // ?
       status: AgentThreadStatus,           // ?
       workspace: ThreadEntryWorkspace,     // ?
       is_live: bool,                       // ?
       is_background: bool,                 // ?
       is_title_generating: bool,           // ?
       draft: Option<DraftKind>,            // ?
       highlight_positions: Vec<usize>,     // ?
       worktrees: Vec<...>,                 // ?
       diff_stats: DiffStats,               // ?
   }
   ```
2. 对照三处代码逐一验证：
   - 构造默认值：[sidebar.rs:1572-1585](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1572-L1585)
   - 草稿后处理：[sidebar.rs:1671-1702](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1671-L1702)
   - 活跃覆盖与通知：[sidebar.rs:1718-1751](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1718-L1751)
3. 特别标注「**默认值 → 后被谁覆盖**」的字段对，例如 `status`（默认值 → `apply_active_info`）、`draft`（`WithContent` → 可能降级为 `Empty`）、`metadata.title`（数据库标题 → 草稿标签或活跃标题覆盖）。
4. 注意：不要把注释提交进仓库——本实践的产物是你自己的对照表，改源码违反本手册规则。

**需要观察的现象**：你会发现字段可以干净地分成三组——「只来自数据库」（`metadata` 的 id/时间戳/路径部分）、「数据库打底 + 活跃覆盖」（`title`、`status`、`icon`、`is_background`、`is_title_generating`、`diff_stats`）、「纯派生/纯交互」（`workspace`、`draft`、`highlight_positions`、`is_live`、`worktrees`）。

**预期结果**：得到一张 12 行的字段-来源对照表，其中至少 `status`、`draft`、`metadata.title` 三个字段能指出「构造值」与「覆盖点」两处代码位置。对照结论无需运行即可验证（纯静态阅读）；若想动态印证，可运行 `cargo test -p sidebar --lib test_parallel_threads_shown_with_live_status` 观察 live 状态如何进入断言（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：`is_live` 什么时候变 `true`？它为什么不能从数据库读？

答案：只在 `apply_active_info`（[sidebar.rs:384](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L384)）里置 `true`，即该线程的 `session_id` 在当前窗口某个工作区的活跃线程信息里出现时。「活跃」是进程内存态（会话还开着、正在跑），数据库只存持久元数据，两者天然分离。

**练习 2**：`draft` 字段为什么在构造时先统一给 `Some(DraftKind::WithContent)`，之后再「降级」？

答案：见 [sidebar.rs:1568-1571](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1568-L1571) 的注释：构造闭包拿不到 workspace 上下文之外的草稿存储信息，所以先假定草稿有内容；随后 [1671-1685 行](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1671-L1685) 的后处理用 `draft_display_label_for_thread_metadata` 重新判定，推不出标签的降级为 `Empty` 或整行剔除。

**练习 3**：`worktrees` 字段里的分支名是数据库里的吗？

答案：不是（至少不全是）。`worktree_paths`（路径列表）来自元数据，但分支名来自 `branch_by_path` 映射，它是在 `rebuild_contents` 里遍历各工作区 project 的 git 仓库快照现算的（[sidebar.rs:1417-1437](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1417-L1437)），最后由 agent_ui 的 `worktree_info_from_thread_paths` 组装。

### 4.3 TerminalEntry：终端行

#### 4.3.1 概念说明

`TerminalEntry` 是 Agent 面板里「终端线程」的行模型。它和 `ThreadEntry` 结构上对称（metadata + workspace + worktrees + 通知 + 高亮），但显著更薄，原因有二：

- 终端没有「草稿」「标题生成中」「后台运行」「diff 统计」这些线程才有的生命周期概念。
- 终端的「通知」不靠跨重建的状态记忆，而是每次重建直接从各工作区 `AgentPanel` 里活着的终端对象上读取 `has_notification`。

#### 4.3.2 核心流程

```text
# 重建前：从每个打开的工作区的 AgentPanel 收集「带通知的终端 id」
live_notified_terminal_ids = 各面板 terminals 中 has_notification 的 id

# 每个项目组内，按四路查询终端元数据（都过 seen_terminal_ids 去重）：
  1. entries_for_main_worktree_path(组路径)     # 主路径命中
  2. entries_for_path(组路径)                   # 兼容旧数据
  3. 每个 workspace 的根路径 entries_for_path   # 归到打开的工作区
  4. 每个 linked worktree 路径 entries_for_path # 归到 Closed 工作区

make_terminal_entry(metadata, workspace):
  worktrees        = worktree_info_from_thread_paths(...)
  has_notification = live_notified_terminal_ids.contains(terminal_id)
  highlight_positions = 空（有过滤查询时再填）
```

#### 4.3.3 源码精读

结构体定义：[crates/sidebar/src/sidebar.rs:364-371](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L364-L371)

```rust
#[derive(Clone)]
struct TerminalEntry {
    metadata: TerminalThreadMetadata,
    workspace: ThreadEntryWorkspace,
    worktrees: Vec<ThreadItemWorktreeInfo>,
    has_notification: bool,
    highlight_positions: Vec<usize>,
}
```

`TerminalThreadMetadata` 的定义：[crates/agent_ui/src/terminal_thread_metadata_store.rs:47-56](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/agent_ui/src/terminal_thread_metadata_store.rs#L47-L56)

```rust
#[derive(Debug, Clone, PartialEq)]
pub struct TerminalThreadMetadata {
    pub terminal_id: TerminalId,
    pub title: SharedString,
    pub custom_title: Option<SharedString>,
    pub created_at: DateTime<Utc>,
    pub worktree_paths: WorktreePaths,
    pub remote_connection: Option<RemoteConnectionOptions>,
    pub working_directory: Option<PathBuf>,
}
```

构造闭包与去重闸门：[crates/sidebar/src/sidebar.rs:1458-1482](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1458-L1482)

```rust
let make_terminal_entry =
    |metadata: TerminalThreadMetadata, workspace: ThreadEntryWorkspace| {
        let worktrees =
            worktree_info_from_thread_paths(&metadata.worktree_paths, &branch_by_path);
        let has_notification =
            live_notified_terminal_ids.contains(&metadata.terminal_id);
        TerminalEntry { metadata, workspace, worktrees, has_notification,
                        highlight_positions: Vec::new() }
    };
...
let mut push_terminal_metadata = |metadata, workspace| {
    if !seen_terminal_ids.insert(metadata.terminal_id) {
        return;                     // 四路查询会撞出重复，靠 HashSet 挡掉
    }
    terminals.push(make_terminal_entry(metadata, workspace));
};
```

通知来源 `live_notified_terminal_ids` 在重建开头收集：[sidebar.rs:1391-1401](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1391-L1401)，遍历每个工作区的 `AgentPanel`，把 `terminal.has_notification` 的 id 收进集合。四路查询本体在 [sidebar.rs:1483-1526](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1483-L1526)（主路径、组路径、各 workspace 根路径、linked worktree 路径），与线程一侧的查询策略同构。

最后注意两个共享类型：`workspace` 字段的类型 `ThreadEntryWorkspace`（[sidebar.rs:211-220](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L211-L220)）虽然名字带 Thread，但线程和终端共用，分 `Open(Entity<Workspace>)` 与 `Closed { folder_paths, project_group_key }` 两态——细节留给下一讲 u2-l2。

#### 4.3.4 代码实践

**实践目标**：用「对照法」理解终端行比线程行薄在哪里。

**操作步骤**：

1. 把 `ThreadEntry`（12 字段）与 `TerminalEntry`（6 字段）并排列出，划掉终端没有的字段：`icon`、`icon_from_external_svg`、`status`、`is_live`、`is_background`、`is_title_generating`、`draft`、`diff_stats`。
2. 对剩下 6 个同名字段，分别指出两侧的填值代码：终端侧在 [sidebar.rs:1458-1471](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1458-L1471)，线程侧在 [sidebar.rs:1563-1586](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1563-L1586)。
3. 找到测试 `test_terminal_metadata_is_deduped_across_project_groups`（在 [sidebar_tests.rs](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs) 中搜索该名），阅读它如何让同一个终端被两个组查询命中，并断言只出现一行。

**需要观察的现象**：终端的通知在行模型上是 `has_notification: bool`（每行自带），而线程的通知在 `SidebarContents.notified_threads`（集合，行外维护）——两种风格并存。

**预期结果**：能说出「终端通知为什么可以每行自带」：它每次重建都从活着的面板终端现读，不需要跨重建记忆；线程通知要检测「上一刻 Running → 这一刻 Completed」的跳变，必须依赖集合记忆（见 4.4）。第 3 步测试的运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`TerminalEntry.has_notification` 和线程的通知判定时机有何不同？

答案：终端在构造行时直接用 `live_notified_terminal_ids.contains(...)` 现算（[sidebar.rs:1462-1463](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1462-L1463)）；线程在合并活跃信息时比较 `live_thread_statuses` 里存的旧状态，检测 Running→Completed 跳变后写入 `notified_threads` 集合（[sidebar.rs:1738-1746](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1738-L1746)）。

**练习 2**：`seen_terminal_ids` 解决什么问题？不做去重会怎样？

答案：同一个终端会经四路查询（组主路径、组路径、workspace 根路径、linked worktree 路径）多次命中，且它可能逻辑上属于多个组；不去重则同一终端在列表里出现多行。[sidebar.rs:1476-1482](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1476-L1482) 用 `HashSet::insert` 返回 false 挡掉第二次及之后的插入，线程侧有对应的 `seen_thread_ids`。

**练习 3**：终端行构造后还有类似 `apply_active_info` 的修补吗？

答案：没有。终端不存在活跃会话信息的覆盖阶段；构造后唯一可能再动的字段是 `highlight_positions`（过滤查询命中时，[sidebar.rs:1847-1869](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1847-L1869)）。这也是它不需要 `Arc` 的原因之一。

### 4.4 SidebarContents：一次重建的完整快照

#### 4.4.1 概念说明

`SidebarContents` 是 `rebuild_contents` 的**全部产出**。它不只装行：还带着三个「索引/集合」型的派生数据，分别服务三类消费方：

| 字段 | 类型 | 服务对象 |
| --- | --- | --- |
| `entries` | `Vec<ListEntry>` | 渲染列表、键盘导航（selection 的下标就是它） |
| `notified_threads` | `HashSet<ThreadId>` | 线程行上的通知圆点（如 [render_thread](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L6111) 里 `is_thread_notified`）、切换器 |
| `notified_terminals` | `HashSet<TerminalId>` | 终端行的通知、分组头的 `has_notifications` 汇总 |
| `project_header_indices` | `Vec<usize>` | 粘性头部定位、按分组导航 |
| `has_open_projects` | `bool` | 「没有打开的项目」空态覆盖层 |

关键认知：`Sidebar` 结构体上**只有一份** `contents`（u1-l3 里归入「派生状态」的那类字段），每次重建整体替换，旧值只有 `notified_threads` 一个集合被继承。

#### 4.4.2 核心流程

```text
rebuild_contents 开头:
    previous = mem::take(&mut self.contents)        # 旧快照搬空
    notified_threads = previous.notified_threads    # 唯一继承：线程通知记忆
    notified_terminals = HashSet::new()             # 终端通知不继承，全部现算

循环填 entries / project_header_indices / 两个通知集合 ...

收尾:
    notified_threads.retain(在 current_thread_ids 中)   # 消失的线程通知清掉
    self.contents = SidebarContents { entries, notified_threads,
                                      notified_terminals,
                                      project_header_indices, has_open_projects }
```

为什么通知集合要放在 `SidebarContents` 而不是行模型里（线程侧）？因为「线程完成时用户没在看它」这个事实属于**列表级记忆**：行每次重建都是新造的，如果把通知存成 `ThreadEntry` 的字段，重建一瞬间就丢了。放在快照的集合里、并在重建开头显式继承，才能跨重建存活。这也解释了 4.3 练习 1 的不对称：终端通知无需记忆，所以可以每行自带。

#### 4.4.3 源码精读

结构体定义与两个查询辅助：[crates/sidebar/src/sidebar.rs:475-482](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L475-L482)、[crates/sidebar/src/sidebar.rs:501-509](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L501-L509)

```rust
#[derive(Default)]
struct SidebarContents {
    entries: Vec<ListEntry>,
    notified_threads: HashSet<agent_ui::ThreadId>,
    notified_terminals: HashSet<TerminalId>,
    project_header_indices: Vec<usize>,
    has_open_projects: bool,
}

impl SidebarContents {
    fn is_thread_notified(&self, thread_id: &agent_ui::ThreadId) -> bool { ... }
    fn is_terminal_notified(&self, terminal_id: TerminalId) -> bool { ... }
}
```

继承点与初始化：[sidebar.rs:1356-1362](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1356-L1362)

```rust
let previous = mem::take(&mut self.contents);

let old_statuses = &self.live_thread_statuses;

let mut entries = Vec::new();
let mut notified_threads = previous.notified_threads;   // 继承
let mut notified_terminals: HashSet<TerminalId> = HashSet::new();  // 不继承
```

`notified_threads` 的写入（Running→Completed 跳变）与清除（成为活跃线程）：[sidebar.rs:1738-1750](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar.rs#L1738-L1750)；`notified_terminals` 的写入则从当次收集的终端行反填：[sidebar.rs:1532-1536](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1532-L1536)。

收尾的三条 retain 与整体替换：[sidebar.rs:1956-1971](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1956-L1971)

```rust
notified_threads.retain(|id| current_thread_ids.contains(id));

self.thread_last_accessed.retain(|id, _| current_thread_ids.contains(id));
self.terminal_last_accessed.retain(|id, _| current_terminal_ids.contains(id));

self.live_thread_statuses = new_live_statuses;

self.contents = SidebarContents {
    entries,
    notified_threads,
    notified_terminals,
    project_header_indices,
    has_open_projects,
};
```

`project_header_indices` 的写入时机——每组的 header 压入前记录下标（无查询分支）：[sidebar.rs:1930-1940](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1930-L1940)（带查询分支同理，[1884-1894](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1884-L1894)）：

```rust
project_header_indices.push(entries.len());
entries.push(ListEntry::ProjectHeader { key: group_key.clone(), label, ... });
```

它的两个典型消费方：

- 粘性头部：滚动时找「最后一个下标不大于滚动顶部分组头」——[render_sticky_header，sidebar.rs:3149-3154](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L3149-L3154)。
- 分组导航/活跃分组定位：[sidebar.rs:7001-7034](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L7001-L7034)。

`has_open_projects` 由「是否存在根路径非空的工作区」得出：[sidebar.rs:1372-1374](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1372-L1374)，消费点在渲染空态 `no_open_projects`：[sidebar.rs:7771](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L7771)。

组内行的最终排序由 `push_entries_by_display_time` 完成（终端与线程混排，按展示时间倒序，空草稿置顶）：[sidebar.rs:5703-5740](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L5703-L5740)，其中 `thread_display_time = interacted_at.unwrap_or(updated_at)`。

#### 4.4.4 代码实践

**实践目标**：亲眼验证「旧快照里只有 `notified_threads` 被继承」这一断言。

**操作步骤**：

1. 静态验证：在 [sidebar.rs:1356-1370](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1356-L1370) 中数一数 `previous.` 出现的次数——应当只有一处（`previous.notified_threads`）。
2. 动态验证：在测试目录搜索 `test_background_thread_completion_triggers_notification`（[sidebar_tests.rs](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs)），阅读它如何先制造一次带通知的重建，再触发下一次重建，并断言通知仍在——这只有通知集合被继承才可能通过。
3. 运行：
   ```bash
   cargo test -p sidebar --lib test_background_thread_completion_triggers_notification
   ```

**需要观察的现象**：测试通过；同时检查断言里读的是行的通知表现（经由 `visible_entries_as_strings` 或类似输出）而不是某个 `ThreadEntry` 字段——因为线程的通知本来就不存在行模型里。

**预期结果**：确认「线程通知 = 列表级记忆（`SidebarContents.notified_threads`），终端通知 = 每行现算（`TerminalEntry.has_notification`）」这条不对称设计。测试输出**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `notified_threads` 改成 `ThreadEntry` 上的一个 `bool` 字段，会发生什么？

答案：每次重建行都新造，字段回到默认 `false`，用户没在看时完成的线程在下一秒的任何一次刷新（元数据变化、面板事件等都会触发重建）后立即丢失通知圆点。这正是它必须放在 `SidebarContents` 并在 [1356/1361 行](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1356-L1361)显式继承的原因。

**练习 2**：`project_header_indices` 存的是什么的下标？为什么必须在 push header **之前**记录？

答案：存的是每个 `ListEntry::ProjectHeader` 在 `entries` 中的下标。push 之前 `entries.len()` 正好等于 header 即将落位的下标；push 之后再记就要减一，容易错。消费方（如 [render_sticky_header](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L3149-L3154)）依赖它与滚动位置比较。

**练习 3**：`SidebarContents` 为什么 `#[derive(Default)]`？

答案：`Sidebar` 结构体构造时（`Sidebar::new`）需要给 `contents` 字段一个初始值；空列表 + 空集合 + 空索引 + `has_open_projects = false` 正是「还没重建过」的合法初值，`Default` 派生让初始化一行完成，随后 `defer_in` 里的首次 `update_entries` 会整体替换它。

## 5. 综合实践

**任务：画出一行的「出生档案」。**

从 `rebuild_contents` 里任选一条产生 `ListEntry::Thread` 的完整链路，产出一张表 + 一段验证说明：

1. **选场景**：一个打开的工作区 `/repo`（本地、单根、无 linked worktree），数据库里有一条非草稿线程（有 `session_id`），且该线程此刻在本窗口 `AgentPanel` 中处于 `Running`，另有一条无 `session_id` 的空草稿线程。
2. **画表**：为「Running 线程」那一行列出全部 12 个字段的最终值及其来源代码行（构造点或覆盖点），格式如 `status = Running ← apply_active_info（sidebar.rs:1725 调用，381 行赋值）`。
3. **预测**：空草稿那一行会不会出现在 `entries` 里？写出你依据的 retain 语句（[sidebar.rs:1687-1702](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1687-L1702)），并说明「正在激活（`pending_thread_activation`）」与「恰为面板活跃线程」两个例外条件分别放行哪种情况。
4. **验证**：写一个临时测试（模仿 [sidebar_tests.rs:547](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar_tests.rs#L547) 的 `visible_entries_as_strings` 断言风格）复现该场景，对照你的表逐项核对；练习完删除临时测试，不要提交。
5. **延伸一问**：同一场景下再画 `SidebarContents` 的五个字段各自的值，特别是 `project_header_indices`（应为 `[0]`，因为唯一的 header 落在下标 0）与 `has_open_projects`（应为 `true`）。

这个任务把本讲四个最小模块（ListEntry、ThreadEntry、TerminalEntry 可选、SidebarContents）串在一条真实数据流上；运行结果**待本地验证**。

## 6. 本讲小结

- `ListEntry` 是三种行（`ProjectHeader` 结构体变体、`Thread(Arc<ThreadEntry>)`、`Terminal(TerminalEntry)`）的统一枚举，让虚拟列表与键盘导航只面对一个 `Vec<ListEntry>`。
- `ThreadEntry` 是「数据库打底 + 活跃信息覆盖」的两层模型：构造闭包 `make_thread_entry` 先给保守默认值，草稿后处理与 `apply_active_info` 再就地修补——`Arc` + `Arc::make_mut` 让修补零拷贝。
- `TerminalEntry` 是线程行的「薄」版本：没有草稿/状态/标题生成等生命周期概念，通知每行现算（`has_notification`），因此无需 `Arc`、无覆盖阶段。
- `SidebarContents` 是一次重建的完整快照：`entries` 之外，`notified_threads`（唯一从旧快照继承的字段，承载跨重建记忆）、`notified_terminals`、`project_header_indices`（粘性头部与分组导航的索引）、`has_open_projects`（空态判定）各服务一类消费方。
- `From<ThreadEntry>/<TerminalEntry> for ListEntry` 把「行模型 → 列表项」的转换约定固化成惯用形式；热路径 `push_entries_by_display_time` 则直接用变体构造器。
- 去重贯穿收集阶段：`seen_thread_ids` / `seen_terminal_ids` 挡住多路查询的重复命中。

## 7. 下一步学习建议

- **下一讲 u2-l2（工作区与项目分组）**：本讲刻意略过的 `ThreadEntryWorkspace::Open/Closed`、`ProjectGroupKey` 的分组规则、linked worktree 如何被收集——那是 `workspace` 与 `resolve_workspace` 字段的完整故事。
- **u2-l3（选中与活跃）**：`SidebarContents.entries` 的下标如何被 `selection` 使用，以及与 `ActiveEntry` 的区别。
- **提前预习第三单元**：带着本讲的模型去读 [rebuild_contents](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L1342)（u3-l4），你会发现它就是本讲所有构造闭包的宿主；届时可重点关注 `live_thread_statuses`（[sidebar.rs:768](https://github.com/zed-industries/zed/blob/907ed09c9f4476caf250e6ce4bbffb23b4622f3b/crates/sidebar/src/sidebar.rs#L768)）如何为「Running→Completed」通知判定提供上一刻的状态。
