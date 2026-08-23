# 线程激活全链路

## 1. 本讲目标

学完本讲，你应该能够：

- 画出 `activate_thread` 的完整决策树：先查本窗口、再查其他窗口、都不在则（由调用方）先打开工作区——并说出每条分支最终调用的函数。
- 解释「激活一个线程」实际上是四件事的组合：**切换工作区 → 乐观写入 `active_entry` → 让 AgentPanel 装载线程 → 触发列表重建**。
- 理解 `pending_thread_activation` 这个 `Option<ThreadId>` 在异步激活过程中起到的「防误清空、防空草稿」两道保险作用。
- 理解 `restoring_tasks: HashMap<ThreadId, Task<()>>` 为什么以 `ThreadId` 为键存储任务句柄，以及「从 map 中移除 = 取消任务」这一 gpui 惯用法。
- 说出已关闭原生线程的 Markdown 降级路径（`open_closed_native_thread_as_markdown`）的触发条件与执行步骤。

## 2. 前置知识

### 2.1 「激活」到底激活了什么

在 Zed 的多项目模型里，一个线程（Agent 会话）永远隶属于某个工作区（workspace）。所以「在侧边栏里点开一个线程」不是单纯地把某个视图换到前台，而是要同时完成：

1. **切工作区**：如果该线程所属的工作区不是当前活跃工作区，要先把整个窗口切过去（工作区承载编辑器、面板、终端等一切）。
2. **装载线程**：让目标工作区的 `AgentPanel` 把这个会话加载为当前会话视图。
3. **更新侧边栏状态**：高亮（`active_entry`）、记录访问时间（MRU 排序用）、重建列表。
4. 必要时**先打开/恢复工作区**——线程所属的工作区可能早已关闭，甚至其 git worktree 已被归档删除。

本讲就是把这条链上的每个函数拆开看。

### 2.2 Open 与 Closed：行数据里的两种工作区形态

u2-l2 讲过，`ThreadEntry.workspace` 是 `ThreadEntryWorkspace` 枚举：

- `Open(Entity<Workspace>)`：工作区还活着，**实体句柄本身就是「它存在于某个窗口」的证明**（gpui 的 `Entity` 按实体 ID 判等）。
- `Closed { folder_paths, project_group_key }`：工作区已关闭，只剩「重开所需的身份材料」——一组路径和一个分组键。

这个二分直接决定了入口处的分流：`Open` 走 `activate_thread`，`Closed` 走 `open_workspace_and_activate_thread`。

### 2.3 一个窗口一个 MultiWorkspace

u1-l1 建立过这个模型：`Sidebar` 挂在 `MultiWorkspace` 上，而**每个窗口恰好持有一个 `MultiWorkspace`**。因此：

- 「本窗口的工作区」= 自己宿主 `multi_workspace.read(cx).workspaces()` 里的成员；
- 「其他窗口」= 遍历 `cx.windows()`，把每个窗口 downcast 成 `WindowHandle<MultiWorkspace>` 再逐个查（[sidebar.rs:3564-3581](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3564-L3581)）。

跨窗口激活的本质，就是**操作另一个 `MultiWorkspace` 实体和另一个窗口里的 `Sidebar` 实体**。

### 2.4 active_entry 与「乐观写」

u2-l3 讲过 `active_entry`（全局高亮的活跃条目）与 `selection`（键盘焦点下标）的区别。本讲要补上的是它的**写入时序**问题：

面板装载线程是异步的（可能要等 `AgentPanel::load` 完成），但高亮必须立刻出现。所以本地激活会**抢先**把 `active_entry` 写好（乐观更新），再等面板事件回来「对账」。对账期间靠 `pending_thread_activation` 标记「我有一个还没被面板确认的激活」，防止中间事件把高亮清掉。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [crates/sidebar/src/sidebar.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs) | 本讲主战场：激活入口、三条路径、并发防护、Markdown 降级全部在此 |
| [crates/sidebar/src/sidebar_tests.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs) | 十余个 `test_activate_*` / `test_confirm_*` / `test_clicking_*` 测试是行为的规格说明书 |
| [crates/workspace/src/multi_workspace.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs) | `activate` / `retain_active_workspace` / `find_or_create_workspace` 的实现方 |
| [crates/agent_ui/src/agent_panel.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs) | `load_agent_thread`：面板侧真正装载线程的地方 |
| [crates/agent_ui/src/threads_archive_view.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/threads_archive_view.rs) | 归档视图；`restoring` 集合防止重复触发恢复 |

sidebar.rs 内本讲涉及的关键片段（按行号排序）：

| 行号 | 内容 |
|---|---|
| 764 / 774 | `pending_thread_activation` 与 `restoring_tasks` 字段定义 |
| 1155-1219 | `sync_active_entry_from_panel`：消费 `pending_thread_activation` 的对账函数 |
| 1686-1702 | `rebuild_contents` 中空草稿的保留判定（读取 pending 标记） |
| 3518-3562 | `confirm`：键盘确认入口 |
| 3564-3595 | 两个工作区查找辅助函数 |
| 3597-3664 | `load_agent_thread_in_workspace` |
| 3666-3704 | `open_closed_native_thread_as_markdown` |
| 3831-3841 | `is_thread_active_in_workspace` |
| 3843-3883 | `activate_thread_locally` |
| 3885-3926 | `activate_thread_in_other_window` |
| 3928-3951 | `activate_thread`：三岔决策 |
| 3953-4012 | `open_workspace_and_activate_thread` |
| 4054-4248 | `open_thread_from_archive`（归档路径的三岔决策 + 恢复任务） |
| 6311-6332 | 线程行 `on_click`：鼠标点击入口 |
| 7070-7136 | `cycle_thread_impl`：NextThread/PreviousThread 入口 |

## 4. 核心概念与源码讲解

### 4.1 决策树入口：activate_thread 与两个查找辅助

#### 4.1.1 概念说明

`activate_thread` 只处理一种情况：**调用方手里已经有活着的工作区句柄**（即 `ThreadEntryWorkspace::Open`）。它要回答的问题只有一个——「这个工作区在哪个窗口？」答案有三种：

1. 在**我所在的窗口** → 本地激活（`activate_thread_locally`）；
2. 在**别的窗口** → 跨窗口激活（`activate_thread_in_other_window`）；
3. **哪都不在** → 直接返回（防御性分支：实体还活着却找不到宿主窗口，正常流程不会走到）。

注意：工作区已关闭（`Closed`）的线程根本不会进入这个函数——调用方在更早的地方就分流去了 `open_workspace_and_activate_thread`（见 4.4）。

#### 4.1.2 核心流程

```
activate_thread(metadata, workspace, retain)
│
├─ find_workspace_in_current_window(workspace)?
│    命中 → activate_thread_locally(metadata, workspace, retain)   // 路径 1：本窗口
│    未命中 ↓
├─ find_workspace_across_windows(workspace)?
│    命中 (target_window, workspace) →
│        activate_thread_in_other_window(metadata, workspace, target_window)  // 路径 2
│    未命中 → return                                                // 路径 3：防御性放弃
```

两个查找函数都用 `candidate == workspace` 做谓词——比较的是 `Entity<Workspace>` 句柄（等价于实体 ID 相等），不是路径比较。这一点很重要：**激活的身份判据是「同一个实体」而不是「同一个路径」**，两个窗口恰好打开了同一路径的两个独立工作区实体时，只会命中真正持有该实体的那个窗口。

#### 4.1.3 源码精读

先看主函数，短得可以直接读完（[sidebar.rs:3928-3951](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3928-L3951)）：

```rust
fn activate_thread(
    &mut self,
    metadata: ThreadMetadata,
    workspace: &Entity<Workspace>,
    retain: bool,
    window: &mut Window,
    cx: &mut Context<Self>,
) {
    if self
        .find_workspace_in_current_window(cx, |candidate, _| candidate == workspace)
        .is_some()
    {
        self.activate_thread_locally(&metadata, &workspace, retain, window, cx);
        return;
    }

    let Some((target_window, workspace)) =
        self.find_workspace_across_windows(cx, |candidate, _| candidate == workspace)
    else {
        return;
    };

    self.activate_thread_in_other_window(metadata, workspace, target_window, cx);
}
```

这段代码是决策树本体：先本窗口、后跨窗口、找不到就放弃。`retain` 参数只在本地路径有意义（是否把工作区「钉住」，见 4.2）。

本窗口查找（[sidebar.rs:3583-3595](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3583-L3595)）从自己的宿主 `multi_workspace` 出发，只查一个窗口的工作区列表：

```rust
fn find_workspace_in_current_window(
    &self,
    cx: &App,
    predicate: impl Fn(&Entity<Workspace>, &App) -> bool,
) -> Option<Entity<Workspace>> {
    self.multi_workspace.upgrade().and_then(|multi_workspace| {
        multi_workspace
            .read(cx)
            .workspaces()
            .find(|workspace| predicate(workspace, cx))
            .cloned()
    })
}
```

跨窗口查找（[sidebar.rs:3564-3581](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3564-L3581)）则枚举应用的所有窗口，把每个窗口的根视图 downcast 成 `WindowHandle<MultiWorkspace>` 再逐个查，返回 `(窗口句柄, 工作区)` 二元组：

```rust
fn find_workspace_across_windows(
    &self,
    cx: &App,
    predicate: impl Fn(&Entity<Workspace>, &App) -> bool,
) -> Option<(WindowHandle<MultiWorkspace>, Entity<Workspace>)> {
    cx.windows()
        .into_iter()
        .filter_map(|window| window.downcast::<MultiWorkspace>())
        .find_map(|window| {
            let workspace = window.read(cx).ok().and_then(|multi_workspace| {
                multi_workspace
                    .workspaces()
                    .find(|workspace| predicate(workspace, cx))
                    .cloned()
            })?;
            Some((window, workspace))
        })
}
```

谁会调用 `activate_thread`？本 crate 内有四个入口，全部先把 `Closed` 分流掉：

- 键盘确认 `confirm`（[sidebar.rs:3533-3554](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3533-L3554)）：`Open` → `activate_thread`，`Closed` → `open_workspace_and_activate_thread`；
- 鼠标点击线程行 `on_click`（[sidebar.rs:6311-6332](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6311-L6332)）：同样的二分；
- 线程循环切换 `cycle_thread_impl`（NextThread / PreviousThread 动作，[sidebar.rs:7105-7127](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7105-L7127)）：同样二分，且 `Open` 分支传 `retain = true`；
- 归档视图打开 `open_thread_from_archive`（见 4.4）。

（ThreadSwitcher 的确认路径没有复用 `activate_thread`，而是就地内联了相同步骤，见 [sidebar.rs:5919-5954](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5919-L5954)，u7-l2 会展开。）

#### 4.1.4 代码实践

**实践目标**：用一个真实测试观察「本地激活会切换活跃工作区」这一基本行为。

**操作步骤**：

1. 在 Zed 仓库根目录运行：

   ```bash
   cargo test -p sidebar --lib test_confirm_on_historical_thread_activates_workspace
   ```

2. 打开 [sidebar_tests.rs:4619-4684](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L4619-L4684)，对照阅读：测试先创建两个工作区并切到第二个（4630-4668 行），再把 `selection` 设为 1（历史线程行）并派发 `Confirm`（4674-4677 行）。

**需要观察的现象**：测试通过；断言（4680-4683 行）确认活跃工作区已从 `workspace_1` 切回 `workspace_0`——也就是 `activate_thread` 走了本地路径并把窗口切了过去。

**预期结果**：`test_confirm_on_historical_thread_activates_workspace ... ok`。注释 4671-4673 行还记载了历史教训：以前工作区字段是 `Option<usize>`，历史线程为 `None` 时激活会提前返回、不切换工作区——这个测试就是防那次回归的。（本讲义环境未运行 cargo，**待本地验证**。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `activate_thread` 的谓词用 `candidate == workspace`（实体相等）而不是比较路径？

**答案**：`Open` 变体里持有的 `Entity<Workspace>` 是唯一身份。两个窗口可以各自打开同一路径的两个独立工作区实体；若按路径匹配，可能会在错误的窗口里激活一个「长得像」的工作区，而真正持有该线程面板状态的是另一个实体。实体相等保证命中的一定是线程当初所属的那个工作区。

**练习 2**：`find_workspace_across_windows` 里的 `window.read(cx).ok()` 为什么要转 `Result` 再 `ok()`？

**答案**：跨窗口访问时目标窗口可能已经被关闭（实体释放），`WindowHandle::read` 返回 `Result`，`ok()` 把失败当作「这个窗口里没有」优雅跳过，继续尝试下一个窗口。这是跨窗口代码必须做的防御——自己的窗口不会在自己手上消失，别人的窗口会。

**练习 3**：路径 3（两个查找都落空）在什么情况下会发生？发生了会怎样？

**答案**：理论上是「实体仍存活但宿主 MultiWorkspace 的 `workspaces()` 里查不到」。正常数据流不会出现（`Open` 句柄来自重建时对当前打开工作区的枚举），所以这是一个防御性分支：静默返回，不激活也不崩溃。它的存在让 `activate_thread` 对「世界在异步间隙里变了」保持健壮——这正是 u3-l2 讲过的「全量重推导」世界观的配套写法。

---

### 4.2 本地激活：activate_thread_locally 与 load_agent_thread_in_workspace

#### 4.2.1 概念说明

本地激活是三条路径中最常用的一条，完成一次完整的「四件套」：短路检查 → 乐观写状态 → 切工作区 → 面板装载。其中「面板装载」由 `load_agent_thread_in_workspace` 承担，它还要处理「面板尚未创建」的情况（工作区刚打开时 AgentPanel 可能还没实例化，需要异步 `AgentPanel::load`）。

#### 4.2.2 核心流程

```
activate_thread_locally(metadata, workspace, retain)
│
├─ ① 宿主已释放？→ return
├─ ② is_thread_active_in_workspace？   // 该线程已在此工作区激活
│      是 → 只 focus_panel，返回        // 快路径：什么状态都不用改
├─ ③ 乐观写三件套：
│      active_entry = Thread { thread_id, session_id, workspace }
│      record_thread_access(thread_id)         // 刷新 MRU 时间戳
│      pending_thread_activation = Some(id)    // 标记「待面板确认」
├─ ④ multi_workspace.activate(workspace)       // 切工作区
│      retain == true 时再 retain_active_workspace()
├─ ⑤ load_agent_thread_in_workspace(workspace, metadata, focus=true)
└─ ⑥ update_entries()                          // 立刻重建列表刷新高亮
```

`load_agent_thread_in_workspace` 内部又是一个两分支：

```
load_agent_thread_in_workspace(workspace, metadata, focus)
│
├─ workspace.panel::<AgentPanel>() 已存在？
│    是 → 同步路径：panel.load_agent_thread(...) + focus/reveal_panel
│    否 → 异步路径：cx.spawn {
│            AgentPanel::load(workspace).await     // 从序列化状态恢复面板
│            workspace.add_panel(panel)
│            panel.load_agent_thread(...) + focus/reveal_panel
│         }.detach_and_log_err()
```

#### 4.2.3 源码精读

先看短路检查（[sidebar.rs:3831-3841](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3831-L3841)）——「该线程已经在此工作区激活」需要同时满足活跃工作区相符与 `active_entry` 指向同一线程同一工作区：

```rust
fn is_thread_active_in_workspace(
    &self,
    thread_id: &ThreadId,
    workspace: &Entity<Workspace>,
    cx: &App,
) -> bool {
    self.active_workspace(cx).as_ref() == Some(workspace)
        && self.active_entry.as_ref().is_some_and(|entry| {
            entry.is_active_thread(thread_id) && entry.workspace() == workspace
        })
}
```

再看主体（[sidebar.rs:3843-3883](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3843-L3883)）。注意 3862-3864 行的注释，它是「乐观写」存在的理由：

```rust
// Set active_entry eagerly so the sidebar highlight updates
// immediately, rather than waiting for a deferred AgentPanel
// event which can race with ActiveWorkspaceChanged clearing it.
self.active_entry = Some(ActiveEntry::Thread {
    thread_id: metadata.thread_id,
    session_id: metadata.session_id.clone(),
    workspace: workspace.clone(),
});
self.record_thread_access(&metadata.thread_id);
self.pending_thread_activation = Some(metadata.thread_id);

multi_workspace.update(cx, |multi_workspace, cx| {
    multi_workspace.activate(workspace.clone(), None, window, cx);
    if retain {
        multi_workspace.retain_active_workspace(cx);
    }
});

Self::load_agent_thread_in_workspace(workspace, metadata, true, window, cx);

self.update_entries(cx);
```

这段代码依次完成流程图的 ③④⑤⑥：三件套乐观写、切工作区（可选钉住）、装载线程、重建列表。`record_thread_access` 只有一行（[sidebar.rs:5688-5690](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5688-L5690)）：往 `thread_last_accessed` 写入当前时间。字段声明处的注释（[sidebar.rs:757-760](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L757-L760)）强调它**只由显式用户动作更新**，因为 ThreadSwitcher 的 MRU 排序依赖它。

`MultiWorkspace::activate`（[multi_workspace.rs:1316-1372](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L1316-L1372)）值得看一眼宿主侧做了什么：若目标就是当前工作区只聚焦；否则把旧/新工作区都 `hold` 进窗口的 `held` 列表、在多工作区模式下 `pin` 住、发布新的 `active_workspace_id`、重刷窗口 chrome，最后发出 `MultiWorkspaceEvent::ActiveWorkspaceChanged` 并聚焦。`retain_active_workspace`（[multi_workspace.rs:1377-1386](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L1377-L1386)）则是把「临时」工作区提升为持久（pinned），使其在侧边栏关闭后仍不被丢弃——`cycle_thread_impl` 是唯一的 `retain = true` 调用方。

最后是装载函数（[sidebar.rs:3597-3664](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3597-L3664)）。闭包 `load_thread` 封装了「让面板装载线程」这一个动作：

```rust
let load_thread = |agent_panel: Entity<AgentPanel>,
                   metadata: &ThreadMetadata,
                   focus: bool,
                   window: &mut Window,
                   cx: &mut App| {
    agent_panel.update(cx, |panel, cx| {
        panel.load_agent_thread(
            Agent::from(metadata.agent_id.clone()),
            metadata.thread_id,
            Some(metadata.folder_paths().clone()),
            metadata.title.clone(),
            focus,
            AgentThreadSource::Sidebar,
            window,
            cx,
        );
    });
};
```

随后按「面板是否已存在」分成同步/异步两路（3623-3663 行）：已存在则直接 `load_thread` 再 `focus_panel`/`reveal_panel`；不存在则 `cx.spawn` 一个任务先 `AgentPanel::load(workspace).await` 从序列化状态恢复面板、`add_panel` 挂进工作区，再走同样的装载与聚焦，失败经 `detach_and_log_err` 记日志。面板侧的 `load_agent_thread`（[agent_panel.rs:4371-4394](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L4371-L4394)）开头会先反归档该线程，并检查当前视图是否已经是它——是则只发一个 `ActiveViewChanged` 事件就返回。**正是这个事件回头触发 `sync_active_entry_from_panel`，把 `pending_thread_activation` 消费掉**（见 4.5）。

#### 4.2.4 代码实践

**实践目标**：验证 `focus` 参数与「快路径」的存在——同一线程重复激活不会重复装载。

**操作步骤**：

1. 阅读 `is_thread_active_in_workspace`（3831-3841 行）与 `activate_thread_locally` 的快路径（3855-3860 行）。
2. 在本地（练习性质，不提交）把快路径临时改为 `if false {`，运行：

   ```bash
   cargo test -p sidebar --lib test_confirm_on_historical_thread_activates_workspace
   ```

3. 观察结果后还原代码。

**需要观察的现象**：改造后测试大概率仍然通过（快路径主要是省去重复的 `activate` 与 `load_agent_thread` 调用，行为上是幂等的），但你可以借此确认：去掉快路径后每次确认都会走完整的 `activate` + 装载链。

**预期结果**：能说出快路径省掉了哪三步（`active_entry` 重写、`multi_workspace.activate`、`load_agent_thread_in_workspace`）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `active_entry` 要「乐观写」而不是等面板事件回来再写？

**答案**：面板装载走事件回来是**延迟**的，而 `multi_workspace.activate` 会同步发出 `ActiveWorkspaceChanged`——如果高亮依赖面板事件，中间这个事件可能先把 `active_entry` 清空（源码注释 3862-3864 行描述的正是这个竞态）。乐观写让 UI 立即正确，竞态窗口交给 `pending_thread_activation` 兜底。

**练习 2**：`load_agent_thread_in_workspace` 里 `focus` 与 `reveal_panel` 的区别是什么？哪些调用点传 `focus = false`？

**答案**：`focus_panel` 会把键盘焦点移进面板，`reveal_panel` 只确保面板可见不抢焦点。`activate_thread_locally` 与 `activate_thread_in_other_window` 传 `true`（用户明确要打开线程），ThreadSwitcher 的**预览**路径（[sidebar.rs:5896](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5896)）传 `false`——预览不该抢走用户手里的焦点，直到松键确认（u7-l2 展开）。

**练习 3**：异步分支里 `workspace.update_in(...)` 为什么用 `unwrap_or_else` 再查一次面板，而不是直接 `add_panel`？

**答案**：`AgentPanel::load` 等待期间，别的代码路径可能已经把一个 AgentPanel 加进了这个工作区（例如用户手动打开了面板）。先 `workspace.panel::<AgentPanel>(cx)` 查一次、查不到才 `add_panel`，避免同一工作区挂两个面板实体。这是异步完成后「重新对账再落地」的典型写法。

---

### 4.3 跨窗口激活：activate_thread_in_other_window

#### 4.3.1 概念说明

当线程的工作区活在**另一个窗口**里时，激活要做的事变成：把那个窗口提到前台、在那个窗口里切工作区并装载线程，然后**更新那个窗口里 Sidebar 的状态**（高亮、MRU、pending 标记）。本窗口（发起方）的侧边栏不抢高亮——线程将在别的窗口里活跃。

难点在于：发起方拿到的是 `WindowHandle<MultiWorkspace>`，而 `Sidebar` 是注册在 MultiWorkspace 上的 `Box<dyn SidebarHandle>`，需要先取出再 downcast 回具体类型才能写字段。

#### 4.3.2 核心流程

```
activate_thread_in_other_window(metadata, workspace, target_window)
│
├─ ① target_window.update：
│        window.activate_window()               // 操作系统层面把窗口带到前台
│        multi_workspace.activate(workspace)     // 目标窗口内切工作区
│        load_agent_thread_in_workspace(focus=true)
│      update 失败（窗口已关）→ log_err，activated = false
├─ ② activated 成功后，找到目标窗口的 Sidebar 实体：
│        multi_workspace.sidebar() → to_any() → downcast::<Sidebar>()
├─ ③ 目标侧边栏写入三件套：
│        pending_thread_activation = Some(thread_id)
│        active_entry = Thread { ... }
│        record_thread_access + update_entries
└─ ④ 本窗口（发起方）什么都不改 —— 不抢高亮
```

#### 4.3.3 源码精读

完整函数（[sidebar.rs:3885-3926](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3885-L3926)）：

```rust
let activated = target_window
    .update(cx, |multi_workspace, window, cx| {
        window.activate_window();
        multi_workspace.activate(workspace.clone(), None, window, cx);
        Self::load_agent_thread_in_workspace(&workspace, &metadata, true, window, cx);
    })
    .log_err()
    .is_some();
```

第一步在目标窗口的上下文里执行三连：提前台、切工作区、装载线程。`.log_err().is_some()` 把「窗口在间隙中被关闭」降级为一次日志 + 放弃。第二步取目标侧边栏（注意类型擦除后的还原链）：

```rust
if let Some(target_sidebar) = target_window
    .read(cx)
    .ok()
    .and_then(|multi_workspace| {
        multi_workspace.sidebar().map(|sidebar| sidebar.to_any())
    })
    .and_then(|sidebar| sidebar.downcast::<Self>().ok())
{
    target_sidebar.update(cx, |sidebar, cx| {
        sidebar.pending_thread_activation = Some(metadata_thread_id);
        sidebar.active_entry = Some(ActiveEntry::Thread { ... });
        sidebar.record_thread_access(&metadata_thread_id);
        sidebar.update_entries(cx);
    });
}
```

`sidebar()` 返回的是 `&dyn SidebarHandle`（u8-l3 会讲这个 trait 契约），`to_any()` 转成 `AnyEntity` 再 `downcast::<Self>()` 才能拿到具体的 `Sidebar` 实体句柄，然后写入与本地路径 ③ 完全相同的三件套。**差异只有一点：写的是目标窗口那份状态，发起方自己不动。**

测试 [test_activate_archived_thread_reuses_workspace_in_another_window_with_target_sidebar](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L7907-L7991) 把这个行为差异锁死了，两组断言正好一正一反（7978-7990 行）：

```rust
// 发起方：不该抢高亮
sidebar_a.read_with(cx_a, |sidebar, _| {
    assert!(!is_active_session(&sidebar, &session_id),
        "source window's sidebar should not eagerly claim focus ...");
});
// 目标方：应该立即高亮
sidebar_b.read_with(cx_b, |sidebar, _| {
    assert_active_thread(sidebar, &session_id,
        "target window's sidebar should eagerly focus the activated archived thread");
});
```

#### 4.3.4 代码实践

**实践目标**：用测试观察跨窗口激活的三个可观察效果。

**操作步骤**：

1. 运行两个跨窗口测试：

   ```bash
   cargo test -p sidebar --lib test_activate_archived_thread_reuses_workspace_in_another_window
   ```

   （名字是前缀匹配，`..._with_target_sidebar` 会一并跑掉。）

2. 对照阅读 [sidebar_tests.rs:7841-7853](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L7841-L7853)：测试用 `cx.add_window` 创建了**两个**窗口（A 持有 /project-a，B 持有 /project-b），再在 A 的侧边栏里激活一个属于 /project-b 的线程。

**需要观察的现象**：三个断言分别验证——两个窗口的工作区数量都还是 1（没有把对方的工作区复制过来）、活动窗口变成了 B（`cx.active_window()`）、A 的侧边栏没有高亮该会话。

**预期结果**：两个测试都 `ok`。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么发起方不把自己的 `active_entry` 也写上？两个窗口同时高亮同一个线程不行吗？

**答案**：`active_entry` 的语义是「当前真正打开的条目」，而线程只在目标窗口的面板里打开。若两边都高亮，用户在 A 窗口会误以为线程在这里活跃；且 A 随后的面板事件同步（`sync_active_entry_from_panel` 只认**本窗口活跃工作区**的面板，[sidebar.rs:1166-1173](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1166-L1173)）会把错误高亮清掉，造成闪烁。单一事实源：线程在哪个窗口打开，哪个窗口的侧边栏高亮。

**练习 2**：`window.activate_window()` 和 `multi_workspace.activate(...)` 是同一个概念吗？

**答案**：不是。前者是**操作系统窗口**层面的操作（把另一个 OS 窗口带到前台并获得焦点）；后者是**窗口内工作区**切换（更换显示的工作区、刷新窗口 chrome、发出 `ActiveWorkspaceChanged`）。跨窗口激活两者都要做：先提窗口，再切窗口里的工作区。

**练习 3**：如果目标窗口里的 Sidebar 不是 `Sidebar` 类型（downcast 失败），会发生什么？

**答案**：`if let` 静默跳过状态写入——线程照样在目标窗口装载并打开（第一步的 `activated` 已完成），只是目标窗口侧边栏的高亮要等自己的面板事件同步（`sync_active_entry_from_panel` 的常规分支）追上来。功能不丢，只是少了一次「立即高亮」。这是渐进降级，不是错误。

---

### 4.4 打开工作区再激活：open_workspace_and_activate_thread 与 Markdown 降级

#### 4.4.1 概念说明

`Closed` 行的激活是异步长链路：先通过 `MultiWorkspace::find_or_create_workspace` 按路径**找或开**一个工作区（可能弹连接远程的模态框），拿到新工作区后再回到 `activate_thread`——此时工作区必然在本窗口，走本地路径。所以这条路径本质是「先创造前提，再递归回路径 1」。

归档视图 `open_thread_from_archive` 是另一个独立的三岔决策入口：归档线程没有工作区实体，只有路径，它按「本窗口按路径找 → 跨窗口按路径找 → 都没有则打开」决策，若线程的 worktree 已被归档删除，还要先经 `restoring_tasks` 走 git 恢复链。

最后还有一个不走工作区的降级出口：右键菜单「Open Thread as Markdown」，把已关闭的原生线程直接以 Markdown 文档打开。

#### 4.4.2 核心流程

```
confirm / on_click 遇到 Closed { folder_paths, project_group_key }
│
└─ open_workspace_and_activate_thread(metadata, folder_paths, key)
     ├─ ① pending_thread_activation = Some(id)     // 提前挂上保险（注释 3966-3968 说明用途）
     ├─ ② multi_workspace.find_or_create_workspace(
     │        folder_paths, host, provisional_key, connect_remote 回调, OpenMode::Activate)
     │      → 返回 Task<Result<Workspace>>
     └─ ③ cx.spawn_in:
          ├─ open_task.await 失败 → dismiss_connection_modal
          │      + 仅当 pending 仍是本线程时清空它 → 结束
          └─ 成功 → dismiss_connection_modal
                 → activate_thread(metadata, workspace, retain=false)
                    // 此时工作区刚挂进本窗口 → 必走 activate_thread_locally
```

归档入口的对称决策（无 worktree 恢复需求的分支，[sidebar.rs:4118-4145](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4118-L4145)）：

```
open_thread_from_archive
├─ folder_paths 为空 → 反归档后用「活跃工作区」本地激活
├─ find_current_workspace_for_path_list 命中 → activate_thread_locally
├─ find_open_workspace_for_path_list 命中 → activate_thread_in_other_window
├─ 都没有 → open_workspace_and_activate_thread
└─ （归档删除过 worktree 时）先恢复 git worktree，再走上面三岔
```

注意这里的查找函数（[sidebar.rs:4014-4052](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4014-L4052)）与 4.1 的同名函数**谓词不同**：按 `PathList` 路径匹配外加远程连接身份比对（`same_remote_connection_identity`），因为归档元数据里没有实体句柄可用。

#### 4.4.3 源码精读

`open_workspace_and_activate_thread`（[sidebar.rs:3953-4012](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3953-L4012)）。先看开头的保险与注释：

```rust
let pending_thread_id = metadata.thread_id;
// Mark the pending thread activation so rebuild_contents
// preserves the Thread active_entry during loading and
// reconciliation cannot synthesize an empty fallback draft.
self.pending_thread_activation = Some(pending_thread_id);
```

注释直说了 pending 标记在这个异步窗口期的两个作用：保住 `active_entry` 不被中间事件清掉、阻止重建合成空草稿行（见 4.5）。随后发起异步打开：

```rust
let open_task = multi_workspace.update(cx, |this, cx| {
    this.find_or_create_workspace(
        folder_paths,
        host,
        provisional_key,
        |options, window, cx| connect_remote(active_workspace, options, window, cx),
        None,
        OpenMode::Activate,
        None,
        window,
        cx,
    )
});
```

`find_or_create_workspace` 是宿主提供的「按路径找或开」原语；`connect_remote` 回调（u4-l4 讲过的薄委托）在目标是远程项目时弹连接流程。任务完成后的善后（[sidebar.rs:3990-4009](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3990-L4009)）：

```rust
let result = open_task.await;
// Dismiss the modal as soon as the open attempt completes so
// failures or cancellations do not leave a stale connection modal behind.
remote_connection::dismiss_connection_modal(&modal_workspace, cx);

if result.is_err() {
    this.update(cx, |this, _cx| {
        if this.pending_thread_activation == Some(pending_thread_id) {
            this.pending_thread_activation = None;
        }
    })
    .ok();
}

let workspace = result?;
this.update_in(cx, |this, window, cx| {
    this.activate_thread(metadata, &workspace, false, window, cx);
})?;
```

三个细节值得咀嚼：(1) 模态框无论成败先关，防止失败时残留；(2) 清 pending 前先比较 `== Some(pending_thread_id)`——若期间用户又激活了别的线程（pending 已被覆盖），不能误清别人的标记（ABA 防护）；(3) 成功后递归调用 `activate_thread`，此时 `find_or_create_workspace` 返回的工作区已挂进本窗口，决策树必然落到本地路径——**长链路最终收敛回同一条短链路**。

Markdown 降级（[sidebar.rs:3666-3704](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3666-L3704)）是给「不想恢复工作区、只想看看内容」的出口：

```rust
let load_task =
    thread_store.update(cx, |store, cx| store.load_thread(session_id.clone(), cx));
...
window
    .spawn(cx, async move |cx| {
        let db_thread = load_task.await?;
        let Some(db_thread) = db_thread else {
            anyhow::bail!("Thread not found in database");
        };

        let markdown = db_thread.to_markdown();

        cx.update(|window, cx| {
            agent_ui::open_markdown_in_workspace(
                thread_title, markdown, workspace, window, cx,
            )
        })?
        .await
    })
    .detach_and_log_err(cx);
```

它完全绕开激活链：从数据库按 `session_id` 装载线程记录（找不到就报错）、转成 Markdown 文本、在**当前**工作区里作为文档打开。入口在右键菜单（[sidebar.rs:6352-6353](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6352-L6353) 与 [6411-6447](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6411-L6447)）：

```rust
let can_open_as_markdown = thread.is_live || is_zed_thread;
```

菜单处理器先试「工作区还开着就走面板的 `open_thread_as_markdown`」，面板不在（工作区已关）且是 Zed 原生线程时才落到本函数——所以它名副其实地是**已关闭原生线程**的降级路径。

#### 4.4.4 代码实践

**实践目标**：观察 Closed 路径真的会开出一个新工作区并激活。

**操作步骤**：

1. 运行：

   ```bash
   cargo test -p sidebar --lib test_clicking_worktree_thread_opens_workspace_when_none_exists
   ```

2. 对照 [sidebar_tests.rs:7033-7125](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L7033-L7125)：测试用 FakeFs 造了一个主仓库加一个 linked worktree（/wt-feature-a），窗口只打开主仓库；线程元数据属于 worktree 路径，因此渲染为 `Closed` 行（显示 `WT Thread {wt-feature-a}`）。然后聚焦侧边栏、设 `selection = Some(1)`、`cx.dispatch_action(Confirm)`。

**需要观察的现象**：断言（7108-7124 行）显示工作区数量从 1 变 2，新工作区的路径正是 /wt-feature-a。

**预期结果**：测试 `ok`。这条链路就是 `confirm` → `open_workspace_and_activate_thread` → `find_or_create_workspace` → `activate_thread`（本地）的完整实践样本。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`open_workspace_and_activate_thread` 为什么要**递归**调用 `activate_thread`，而不是直接调 `activate_thread_locally`？

**答案**：让「工作区在哪个窗口」的判定保持单一决策点。理论上 `find_or_create_workspace` 的产物一定挂在本窗口，直接调本地路径也能工作；但走 `activate_thread` 让决策树不必依赖这个隐含前提——万一未来打开语义变化（例如复用其他窗口的已有工作区），这里不需要改动。正确性优先于省一次查找。

**练习 2**：失败清理为什么写成 `if this.pending_thread_activation == Some(pending_thread_id)` 而不是直接置 `None`？

**答案**：从发起打开到任务失败之间隔着任意长的异步时间，用户可能又激活了另一个线程，把 pending 覆盖成了新值。直接清 `None` 会把别人正在进行的激活保险拆掉；先比对再清理保证只撤销自己那次。这是异步回调里修改共享状态的通用防护（compare-and-clear）。

**练习 3**：`can_open_as_markdown = thread.is_live || is_zed_thread`，为什么 live 线程也允许？

**答案**：菜单处理器对 live 线程会优先走**面板**的 `open_thread_as_markdown`（面板实体还在，能直接导出当前会话），本函数只是面板不在时的兜底。允许 `is_live` 是为了让菜单项对两类线程都可见，运行时再按面板是否存在分流；而 `is_zed_thread` 限定兜底路径只服务 Zed 原生线程——只有原生线程的记录才在本地数据库里，能按 `session_id` 装载并 `to_markdown`。

---

### 4.5 并发防护：pending_thread_activation 与 restoring_tasks

#### 4.5.1 概念说明

激活链路有两段异步空窗：乐观写与面板确认之间（毫秒级）、打开/恢复工作区与最终激活之间（秒级，可能弹模态框）。两个字段分别封住这两段空窗：

- `pending_thread_activation: Option<ThreadId>`——**单个**「正在途中」的激活标记。它不存任务、只存身份，作用是告诉其余系统「这个线程的激活正在进行，别把它的痕迹清掉」。因为全应用同一时刻用户只会有一个进行中的激活意图，`Option` 足够，新激活覆盖旧的。
- `restoring_tasks: HashMap<ThreadId, Task<()>>`——**多个**并行的归档恢复任务句柄，按线程粒度存取，持有 `Task` 即维持任务存活，移除即取消。

#### 4.5.2 核心流程

`pending_thread_activation` 的完整生命周期：

```
写入（三个生产者）                          消费 / 读取
────────────────────────                  ─────────────────────────
activate_thread_locally:3871        ──┐
activate_thread_in_other_window:3915 ├─→ sync_active_entry_from_panel:1177-1196
open_workspace_and_activate_thread:3969┘     面板活跃线程 == pending？
                                              是 → 用面板状态回填 active_entry（含
                                                    session_id），清空 pending，返回 true
                                              否 → 保留现状 active_entry，返回 false
                                         rebuild_contents:1689-1702
                                             pending 存在期间：空草稿一律不保留
失败回滚（一个清理点）
open_workspace_and_activate_thread:3996-4003
    仅当 pending 仍是本线程时清空
```

`restoring_tasks` 的生命周期：

```
open_thread_from_archive（有已归档 worktree 时）
  └─ 组装 restore_task（恢复 worktree → 改写路径 → 反归档 → 打开并激活）
  └─ restoring_tasks.insert(thread_id, restore_task)   // 4247 行
        │
        ├── 任务自然完成/失败 → 各出口 remove(&thread_id)（4112 / 4172 / 4227 行）
        └── 用户在归档视图点「取消」→ CancelRestore 事件 → remove(thread_id)（7581 行）
                ↓ remove 即 drop 旧 Task → gpui 取消该任务
```

#### 4.5.3 源码精读

先看两个字段的声明（[sidebar.rs:764](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L764) 与 [sidebar.rs:774](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L774)）：

```rust
pending_thread_activation: Option<agent_ui::ThreadId>,
...
restoring_tasks: HashMap<agent_ui::ThreadId, Task<()>>,
```

消费点 `sync_active_entry_from_panel` 是对账核心（[sidebar.rs:1155-1196](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1155-L1196)，doc 注释明说它同时负责解决 pending）：

```rust
if let Some(pending_thread_id) = self.pending_thread_activation {
    let panel_thread_id = panel
        .active_conversation_view()
        .map(|cv| cv.read(cx).parent_id());

    if panel_thread_id == Some(pending_thread_id) {
        let session_id = panel
            .active_agent_thread(cx)
            .map(|thread| thread.read(cx).session_id().clone());
        self.active_entry = Some(ActiveEntry::Thread {
            thread_id: pending_thread_id,
            session_id,
            workspace: active_workspace,
        });
        self.pending_thread_activation = None;
        return true;
    }
    // Pending activation not yet resolved — keep current active_entry.
    return false;
}
```

要点有二。其一，**未解决就保持现状**：pending 存在期间，面板报告的任何「别的线程」都不会改写 `active_entry`（返回 false 交由调用方保留高亮）——这就是「防误清空」。其二，确认时刻从**面板的活线程**读取 `session_id` 回填，比乐观写时拿到的元数据更权威（乐观写用的 `metadata.session_id`，恢复场景下可能已被更新）。

第二道保险在重建里（[sidebar.rs:1686-1702](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1686-L1702)）：

```rust
// Keep empty drafts only while their thread is active; preserve
// drafts with content because they hold user-typed state.
let pending_activation = self.pending_thread_activation;
...
threads.retain(|thread| {
    if thread.draft != Some(DraftKind::Empty) {
        return true;
    }
    if pending_activation.is_some() {
        return false;
    }
    Some(thread.metadata.thread_id) == active_panel_thread_id
});
```

正常规则是「空草稿只有恰好是面板活跃线程时才保留」；但激活途中面板还没装载完该线程，若按正常规则跑，重建可能给目标线程**合成一个空草稿占位行**，激活完成后再消失——视觉上闪一下。pending 存在期间直接把空草稿全部裁掉，就是注释里说的 "reconciliation cannot synthesize an empty fallback draft"。

`restoring_tasks` 一侧，插入点是 `open_thread_from_archive` 的最后一行（[sidebar.rs:4247](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4247)）：

```rust
self.restoring_tasks.insert(thread_id, restore_task);
```

任务内部的所有出口（恢复后无需 worktree 的 4112 行、恢复失败的 4171-4172 行、恢复成功的 4227 行）都会 `remove(&thread_id)` 收尾。而用户主动取消走事件订阅（[sidebar.rs:7580-7582](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7580-L7582)）：

```rust
ThreadsArchiveViewEvent::CancelRestore { thread_id } => {
    this.restoring_tasks.remove(thread_id);
}
```

在 gpui 里，`Task` 被 drop 即取消（u1-l3 的 Concurrency 知识点）。**所以 `remove` 不只是清理簿记，它就是「取消按钮」的实现**：从 map 里拿掉句柄 → 任务被 drop → 恢复链路停在下一个 await 点。至于「同一线程不会被重复启动恢复」，防线不在 sidebar，而在归档视图自己的 `restoring: HashSet<ThreadId>`（[threads_archive_view.rs:449](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/threads_archive_view.rs#L449)：已在恢复中的条目直接忽略再次点击，458 行 `mark_restoring`）。

#### 4.5.4 代码实践

**实践目标**：把 `restoring_tasks` 的「移除即取消」语义与 `ThreadId` 键的设计读到能复述。

**操作步骤**：

1. 通读 `open_thread_from_archive`（[sidebar.rs:4054-4248](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4054-L4248)），数一数 `restoring_tasks.remove` 出现的全部位置（4112、4172、4227、7581 共四处），给每处标注触发条件（成功/失败/用户取消）。
2. 回答下方「综合实践」中的思考题：为什么键是 `ThreadId` 而不是列表下标、路径或全局唯一槽位。
3. 运行一个归档恢复相关测试加深体感（任选）：

   ```bash
   cargo test -p sidebar --lib test_activate_archived_thread
   ```

**需要观察的现象**：名字前缀匹配会跑掉整组 `test_activate_archived_thread_*` 测试（约 7 个），覆盖「按路径复用工作区」「跨窗口复用」「无路径时用活跃工作区」「开新工作区」等分支。

**预期结果**：整组测试 `ok`；能说出四个 `remove` 点各自的触发条件。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`pending_thread_activation` 为什么是 `Option<ThreadId>` 而不是像 `restoring_tasks` 那样的 `HashMap`？

**答案**：两个领域的并发度不同。激活意图是**用户驱动**的，同一时刻只有一个「正在途中」的意图，新意图自然覆盖旧的（`activate_thread_locally` 直接赋值），`Option` 表达了「至多一个」的领域事实。归档恢复则可能同时挂起多个（用户可以对多个归档线程点恢复），每个都要独立取消，所以需要 map。数据结构形状跟随领域约束，而不是统一风格。

**练习 2**：如果把 `restoring_tasks` 改成 `Vec<Task<()>>`（不记键），哪两个功能会坏？

**答案**：(1) **取消**——`CancelRestore` 事件只携带 `thread_id`，没有键就无法定位该取消哪一个；drop 全部任务会把别的正在恢复的线程一并取消。(2) **完成时自清理**——任务各出口的 `remove(&thread_id)` 无法实现，任务结束后句柄残留 map，既泄漏又让「是否在恢复中」的簿记永久为真。

**练习 3**：`sync_active_entry_from_panel` 确认成功时为什么要从面板重新读 `session_id`，而不是沿用乐观写入时的那份？

**答案**：乐观写时用的 `metadata.session_id` 是**启动激活那一刻**的快照；确认发生在异步之后，期间线程可能经历了重新连接、会话重建（u2-l3 讲过：`ThreadId` 本地可重铸、`SessionId` 才是远端稳定身份）。从面板的活跃线程实体上现读 `session_id`，保证 `active_entry` 里存的是确认时刻的最新身份，后续 `matches_entry` 的双钥匙匹配才可靠。

---

## 5. 综合实践

把本讲所有分支串成一张图，并回答两个设计问题。**这是本讲的核心实践，建议动手完成。**

### 任务 A：绘制 activate_thread 决策树全景图

从「用户意图」出发，画出覆盖以下要素的流程图（手绘或 mermaid 均可）：

```
用户意图（四种入口：confirm 键盘确认 / 行点击 / NextThread 循环 / 归档视图打开）
│
├─ 行的 workspace 形态？
│    ├─ Open(workspace)
│    │    └─ activate_thread（4.1 决策树）
│    │         ├─ 本窗口 → activate_thread_locally
│    │         │    ├─ 快路径：已是活跃线程 → 仅 focus_panel
│    │         │    └─ 慢路径：乐观三件套 → activate(+retain) 
│    │         │              → load_agent_thread_in_workspace → update_entries
│    │         └─ 其他窗口 → activate_thread_in_other_window
│    │          （activate_window + activate + 装载 + 目标侧边栏三件套）
│    └─ Closed { folder_paths, project_group_key }
│         └─ open_workspace_and_activate_thread
│              ├─ 挂 pending 保险 → find_or_create_workspace（异步）
│              ├─ 失败 → 关模态 + compare-and-clear pending
│              └─ 成功 → activate_thread（收敛回本地路径）
└─ 归档入口特有：先按路径三岔（本窗口/跨窗口/新开），
   有已归档 worktree 时先进 restoring_tasks 恢复链
```

要求在每条边上标注：**触发条件**与**调用的函数名（含 sidebar.rs 行号）**。

### 任务 B：回答两个设计问题（写进你的笔记）

1. **为什么 `restoring_tasks` 以 `ThreadId` 为键存储 `Task<()>`？** 参考答案要点（对照 4.5 验证你的表述）：
   - **存活**：gpui 中 `Task` 被 drop 即取消，map 持有句柄是任务得以跑完的唯一保证（对照 CLAUDE.md 的 Concurrency 规则：不 detach 就必须存字段）；
   - **并发度**：多个线程可同时处于恢复中，需要逐线程的槽位而不是单个 `Option`；
   - **可取消**：`CancelRestore` 事件只带 `thread_id`，以 ThreadId 为键才能 O(1) 定位并 drop 对应任务——`remove` 就是取消；
   - **自清理**：任务各出口按同一键 `remove`，簿记与任务同生共死；
   - **为什么不是下标/路径**：下标随重建漂移（u3-l3 的教训），路径可能被 worktree 恢复改写（恢复链里就有 `update_restored_worktree_paths`），只有 `ThreadId` 在整条异步链路上稳定。
2. **`pending_thread_activation` 在「乐观写」与「异步打开」两类窗口期里各挡住了什么？**（答案在 4.5.3：挡误清空 + 挡空草稿合成；消费条件是面板活跃线程与之相等。）

### 验证方式

画完后用三组测试自检图上每条边（均为 `cargo test -p sidebar --lib <名字前缀>`）：

| 测试 | 覆盖的边 |
|---|---|
| `test_confirm_on_historical_thread_activates_workspace` | Open → 本地路径 → 切工作区 |
| `test_clicking_worktree_thread_opens_workspace_when_none_exists` | Closed → 打开工作区 → 收敛回本地 |
| `test_activate_archived_thread_reuses_workspace_in_another_window_with_target_sidebar` | 跨窗口路径 + 双窗口状态差异 |

（本讲义撰写环境未运行 cargo，以上命令与结果均为**待本地验证**。）

## 6. 本讲小结

- **激活 = 四件套**：切工作区（`multi_workspace.activate`）、乐观写 `active_entry`、面板装载（`load_agent_thread_in_workspace`，内部再分面板已存在的同步路与未存在的异步路）、重建列表（`update_entries`）。
- **`activate_thread` 是三岔决策树**：实体相等判据先查本窗口（`activate_thread_locally`）、再跨窗口枚举（`activate_thread_in_other_window`）；工作区已关闭的行根本不进这个函数，由调用方分流去 `open_workspace_and_activate_thread`，后者异步打开成功后**递归回本地路径**收敛。
- **跨窗口激活的要点是「写别人的状态、不写自己的」**：提前台 + 目标窗口内三件套，发起方侧边栏不抢高亮；目标侧边栏经 `dyn SidebarHandle` → `to_any` → `downcast` 取回具体实体。
- **`pending_thread_activation` 封住两段异步空窗**：乐观写与面板确认之间防止 `ActiveWorkspaceChanged` 等中间事件清掉高亮、防止重建合成空草稿占位行；消费点是 `sync_active_entry_from_panel` 的相等判定，失败回滚用 compare-and-clear 防 ABA。
- **`restoring_tasks: HashMap<ThreadId, Task<()>>`**：持有任务句柄维持存活、按线程粒度支持多路并发恢复、`remove` 即取消（`CancelRestore` 事件的实现）；重复启动的防线在归档视图自己的 `restoring` 集合里。
- **Markdown 是绕开激活的降级出口**：`open_closed_native_thread_as_markdown` 按 `session_id` 从数据库装载、`to_markdown`、在当前工作区开文档；仅限面板已不在且是 Zed 原生线程的场景。

## 7. 下一步学习建议

- **下一讲 u6-l2（终端条目的激活与关闭）**：终端侧有一套对称结构（`activate_terminal_entry` / `load_agent_terminal_in_workspace`），读完本讲再看它会发现「同构 + 更薄」；重点差异在终端多出的关闭链路（`TerminalCloseRequested` → 激活邻居）。
- **u7-l2（侧边栏与切换器的集成）**：ThreadSwitcher 的 preview/confirm 没有复用 `activate_thread` 而是内联了相同步骤，届时用本讲的「四件套」做对照表，体会 `focus=false` 预览与 `focus=true` 确认的分寸。
- **延伸阅读源码**：`MultiWorkspace::find_or_create_workspace`（[multi_workspace.rs:1098](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L1098) 起）看「找或开」的完整判定；`AgentPanel::load_agent_thread`（[agent_panel.rs:4371](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L4371) 起）看装载的短路层次（已在当前视图 → 草稿 → retained → 存储）。
