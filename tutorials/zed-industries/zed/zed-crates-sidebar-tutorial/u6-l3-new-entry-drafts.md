# u6-l3 新建条目与草稿管理

## 1. 本讲目标

前两讲（u6-l1、u6-l2）讲的是「激活已存在的条目」：点一个线程行或终端行，把它装回面板。本讲讲反方向的问题——**条目是怎么被创建出来的**，以及创建出来的「草稿」（draft）在侧边栏里过的是怎样一种特殊生活。

「新建」在 Zed 侧边栏里远不止 `new` 一个对象那么简单：同一个「+」按钮，这次点下去可能开一个空白线程草稿，下次点下去却可能开一个终端——取决于你上一次要的是什么；一个草稿在没输入任何文字时几乎「隐身」（只有正在看的那个才显示），一旦敲了几个字就变成钉在分组顶部的实心行，被丢弃时还要连带裁决它的 linked worktree 是否一起归档。

学完本讲，你应该能够：

1. 画出 `create_new_entry` / `create_new_thread` / `create_new_terminal` 三个函数的分工图，并说出 `new_thread_in_group`、`new_terminal_thread` 两个动作与项目头「+」按钮分别汇入哪里。
2. 解释 `NewEntryTarget::LastCreatedKind` 背后的记忆机制：`AgentPanel::last_created_entry_kind` 存在哪、何时写、如何经 KeyValueStore 跨窗口持久化，以及为什么「连续点第二次 +」倾向复用上一种类型。
3. 区分 `DraftKind::WithContent` 与 `DraftKind::Empty` 两种草稿形态，讲清 `draft_display_label_for_thread_metadata` 的三级标题来源（活编辑器 → kvp 草稿库 → 占位符），以及空草稿在 `rebuild_contents` 两道 `retain` 下「什么条件下根本不渲染为行」。
4. 说明 `refresh_refilled_draft_times` 为什么存在、`draft_kinds` 记忆字段豁免于「全量重推导」约束的原因，以及草稿编辑器观察如何驱动标题实时更新。
5. 走读 `remove_draft` 的「预计算 → 押后副作用」两段式结构，理解丢弃一个草稿为什么会牵动工作区移除与归档任务。

## 2. 前置知识

本讲默认你已读过 u3-l4（`rebuild_contents` 全景）与 u6-l1（线程激活全链路）。以下概念做简要回顾，并补充两个本讲新登场的角色：

- **AgentPanel**：挂在工作区（`Workspace`）上的代理面板实体，线程会话与终端视图都活在面板里。侧边栏从不直接创建线程/终端对象，而是调用面板的 `activate_new_thread`、`new_terminal` 等方法——侧边栏是编排者，面板是执行者（u6-l2 的结论同样适用于「创建」）。
- **`ThreadMetadata` 与 `ThreadMetadataStore`**：线程行的持久元数据（标题、路径、时间戳、`archived` 标志等），存放在全局实体 `ThreadMetadataStore` 中（u2-l1）。判定「一行元数据是不是草稿」只有一条规则：`session_id.is_none()`——见 [crates/agent_ui/src/thread_metadata_store.rs:L331-L333](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/thread_metadata_store.rs#L331-L333)。直觉是：草稿是「还没和 agent 建立会话」的线程，一旦发出第一条消息、拿到 ACP `SessionId`，它就「转正」为正式线程。
- **`agent_ui::draft_prompt_store`**（本讲新角色）：草稿提示词的持久仓库，基于全局 KeyValueStore（kvp）。用户在草稿编辑器里敲的字会防抖落盘到这里；`display_label_for_draft` 负责把草稿内容压缩成一行可展示的标题。它是「重启后草稿还能显示标题」的关键。
- **`ThreadEntryWorkspace` 的 Open/Closed 两形态**（u2-l2）：行上携带的工作区信息。`Open` 挂着本窗口的实体句柄，`Closed` 只有路径与分组键。本讲 `remove_draft` 的第一条分支就是在为 Closed 草稿「先开工作区再删」。
- **全量重推导与记忆字段**（u3-l2）：侧边栏的架构约束是「凡可从当前世界状态算出的禁止存字段」，只有「记忆字段」（弥补重建看不到上一刻的盲区）豁免。本讲的 `draft_kinds: HashMap<ThreadId, DraftKind>` 正是一个记忆字段——它记住每个草稿「上一次渲染时是空还是有内容」。
- **`pending_thread_activation`**（u6-l1）：两段异步激活之间的防抖信号。它会参与空草稿的可见性裁决，这里只需记得「它存在且是 `Option<ThreadId>`」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/sidebar/src/sidebar.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs) | 本讲主战场：新建入口三件套、两个动作、`NewEntryTarget`、`DraftKind`、草稿标签推导、可见性 retain、`refresh_refilled_draft_times`、草稿编辑器观察、`remove_draft` 全部在此 |
| [crates/sidebar/src/sidebar_tests.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs) | 本讲实践依赖的测试：`test_new_entry_noops_without_open_project`、`test_draft_title_updates_from_editor_text`、`test_only_actively_viewed_empty_draft_is_visible_in_sidebar`、`test_remove_draft_deletes_metadata_row`，以及播种辅助 `save_draft_metadata_with_main_paths` |
| [crates/agent_ui/src/agent_panel.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs) | 面板一侧：`should_create_terminal_for_new_entry`（LastCreatedKind 的读取端）、`set_last_created_entry_kind_from_user_action`（写回端）、`activate_new_thread` / `new_terminal`、kvp 持久化 |
| [crates/agent_ui/src/draft_prompt_store.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/draft_prompt_store.rs) | 草稿提示词仓库：`display_label_for_draft`、`truncate_draft_label`、`empty_draft_placeholder_label` |

## 4. 核心概念与源码讲解

### 4.1 新建入口三件套：create_new_entry / create_new_thread / create_new_terminal

#### 4.1.1 概念说明

侧边栏里所有「来个新的」的入口——项目分组头上的「+」按钮、键盘动作 `NewThreadInGroup` / `NewTerminalThread`、归档视图的 NewThread 事件——最终都收拢到三个方法：

- `create_new_entry`：**策略入口**。它不自己创建任何东西，只做两个判断：这个工作区有没有打开的项目？面板偏好要线程还是终端？然后分派给下面两个之一。
- `create_new_thread`：**线程执行器**。切工作区 → 让面板激活一个新草稿 → 聚焦面板 → 乐观写 `active_entry`。
- `create_new_terminal`：**终端执行器**。结构同上，只是调用面板的 `new_terminal`，且**不写** `active_entry`（终端的高亮由面板事件同步回来，见 u3-l1 的 `subscribe_to_agent_panel`）。

为什么把「策略」与「执行」拆开？因为「要哪种」是一个随上下文变化的决策（4.2 详述），而「怎么开」是固定流程；拆开后 `new_thread_in_group` 等入口只管「选哪个工作区」，一律调 `create_new_entry` 即可，不必关心偏好。

#### 4.1.2 核心流程

```
UI / 动作入口                                  分派                       执行
─────────────────────────────────────────────────────────────────────────────
项目头「+」按钮（组内无打开工作区）──────┐
项目头「New Thread In…」菜单项 ────────┤
NewThreadInGroup 动作 ─────────────────┼──▶ create_new_entry ──┬─▶ create_new_thread
归档视图 NewThread 事件 ────────────────┘    （查面板偏好）      └─▶ create_new_terminal
NewTerminalThread 动作 ──────────────────────────────────────────▶ create_new_terminal（跳过偏好）

组内没有任何打开工作区时：
  任意入口 ──▶ open_workspace_and_create_entry(key, NewEntryTarget, ...)
                  └─ 异步 find_or_create_workspace 成功后
                        ├─ LastCreatedKind ─▶ create_new_entry（此时再查偏好）
                        └─ Terminal ────────▶ create_new_terminal
```

`create_new_thread` 的四步（与 u6-l1 激活「四件套」同构，只是对象换成了新生的草稿）：

1. 守卫：工作区路径列表为空或宿主 `multi_workspace` 已释放 → 直接返回。
2. `multi_workspace.activate(workspace)`：把目标工作区切为当前展示——「新建」隐含「先切过去」。
3. 面板侧：`panel.activate_new_thread(true, AgentThreadSource::Sidebar, ...)` 激活新草稿，取回 `active_thread_id`，随后 `workspace.focus_panel::<AgentPanel>()` 把焦点交给面板——新草稿立即可以打字。
4. 拿到 `draft_id` 后乐观写 `active_entry = Some(ActiveEntry::Thread { thread_id: draft_id, session_id: None, .. })`：`session_id` 必然是 `None`，因为草稿还没有远端会话（这正是 2 节里 `is_draft` 的判定条件）。

#### 4.1.3 源码精读

策略入口——先做空项目守卫，再按面板偏好二选一（[crates/sidebar/src/sidebar.rs:L6848-L6863](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6848-L6863)）：

```rust
fn create_new_entry(&mut self, workspace: &Entity<Workspace>, ...) {
    if workspace_path_list(workspace, cx).paths().is_empty() {
        return;                       // 空项目：一个入口守卫，三个执行器共用
    }
    if self.should_create_terminal_for_workspace(workspace, cx) {
        self.create_new_terminal(workspace, window, cx);
    } else {
        self.create_new_thread(workspace, window, cx);
    }
}
```

线程执行器的面板交互与乐观写（[sidebar.rs:L6894-L6910](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6894-L6910)）：

```rust
let draft_id = workspace.update(cx, |workspace, cx| {
    let panel = workspace.panel::<AgentPanel>(cx)?;
    let draft_id = panel.update(cx, |panel, cx| {
        panel.activate_new_thread(true, AgentThreadSource::Sidebar, window, cx);
        panel.active_thread_id(cx)
    });
    workspace.focus_panel::<AgentPanel>(window, cx);
    draft_id
});
if let Some(draft_id) = draft_id {
    self.active_entry = Some(ActiveEntry::Thread {
        thread_id: draft_id, session_id: None, workspace: workspace.clone(),
    });
}
```

终端执行器是同构的薄版本，注意它没有 `active_entry` 写入（[sidebar.rs:L6931-L6938](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6931-L6938)）。

两个键盘动作。`NewThreadInGroup` 定义在本 crate 的命名空间里（[sidebar.rs:L86-L94](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L86-L94)），处理器先把分组强制展开、清空 `selection`，再按「组内有无可复用工作区」分派（[sidebar.rs:L6611-L6633](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6611-L6633)）：

```rust
if let Some(key) = self.selected_group_key() {
    self.set_group_expanded(&key, true, cx);   // 新建的线程必须可见 → 先展开
    self.selection = None;
    if let Some(workspace) = self.workspace_for_group(&key, cx) {
        self.create_new_entry(&workspace, window, cx);
    } else {
        self.open_workspace_and_create_entry(&key, NewEntryTarget::LastCreatedKind, window, cx);
    }
} else if let Some(workspace) = self.active_workspace(cx) {
    self.create_new_entry(&workspace, window, cx);   // 无分组上下文：退到活跃工作区
}
```

`NewTerminalThread` 则借自 agent_ui 命名空间（导入见 [sidebar.rs:L20](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L20)），处理器结构完全对称，只是直连 `create_new_terminal`、异步路径携带 `NewEntryTarget::Terminal`（[sidebar.rs:L6635-L6654](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6635-L6654)）。两个动作在 render 根容器注册（[sidebar.rs:L7793-L7794](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7793-L7794)）。

异步兜底 `open_workspace_and_create_entry`：当分组连一个打开的工作区都没有（只剩历史线程），先经 `find_or_create_workspace` 打开（远程项目还要走 `connect_remote` 回调），成功后按 `NewEntryTarget` 回放创建动作（[sidebar.rs:L1286-L1325](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1286-L1325)）：

```rust
cx.spawn_in(window, async move |this, cx| {
    let workspace = task.await?;
    this.update_in(cx, |this, window, cx| match target {
        NewEntryTarget::LastCreatedKind => this.create_new_entry(&workspace, window, cx),
        NewEntryTarget::Terminal => this.create_new_terminal(&workspace, window, cx),
    })?;
    anyhow::Ok(())
})
.detach_and_log_err(cx);
```

`NewEntryTarget` 本体只有两个变体（[sidebar.rs:L116-L120](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L116-L120)）：`LastCreatedKind` 表示「按面板记忆分派」（于是工作区打开后仍要查一次偏好），`Terminal` 表示「明确要终端」（跳过偏好查询）。它之所以必须是数据而不只是两个调用点，正是因为创建动作要**穿越 await 点**——异步打开工作区之后才决定调谁，目标必须先被装进闭包。

项目头「+」按钮的直连分支与「New Thread In…」菜单分支分别见 [sidebar.rs:L2511-L2531](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2511-L2531) 与 [sidebar.rs:L2611-L2618](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2611-L2618)；归档视图的 NewThread 事件处理见 [sidebar.rs:L7586-L7591](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7586-L7591)。

#### 4.1.4 代码实践

**实践目标**：验证空项目守卫，并亲手跟踪一遍「入口 → 分派 → 执行」调用链。

**操作步骤**：

1. 在仓库根目录运行（u1-l2 讲过的过滤方式）：

   ```bash
   cargo test -p sidebar --lib test_new_entry_noops_without_open_project
   ```

2. 打开测试 [sidebar_tests.rs:L1651-L1686](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L1651-L1686) 对照阅读：它构造了一个**零根**工作区（`Project::test(fs, [], cx)`），直接调 `sidebar.create_new_entry(&workspace, ...)`，然后断言面板没有产生任何会话视图、可见行列表为空。
3. 用编辑器跳转功能（gd / ctrl-click）从 `create_new_entry` 出发，依次访问 `should_create_terminal_for_workspace` → `create_new_thread` → `activate_new_thread`（进入 agent_panel.rs），把这条链抄成一张「函数 → 文件:行号」清单。
4. 再从 `new_thread_in_group`（[sidebar.rs:L6611](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6611)）出发走一遍，注意它比「+」按钮多做的两件事：展开分组、清空 selection。

**需要观察的现象**：测试输出为绿；`visible_entries_as_strings` 断言的返回值是一个空 `Vec`——空项目下连分组头都不会渲染。

**预期结果**：`create_new_entry` 的第一道守卫（路径列表为空即返回）挡住了创建，面板的 `active_conversation_view()` 保持 `None`。若你把守卫行注释掉（**仅本地实验，勿提交**），此测试应当变红——`activate_new_thread` 内部也有 `has_open_project` 守卫（[agent_panel.rs:L1776-L1778](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L1776-L1778)），变红与否取决于面板守卫是否足以拦住副作用，这正是双层守卫的价值：**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`create_new_terminal` 与 `create_new_thread` 的函数体几乎逐行对称，唯独少了一段——少的是哪段？为什么可以少？

答案：少了「拿到 id 后乐观写 `active_entry`」。线程路径写 `ActiveEntry::Thread { thread_id: draft_id, session_id: None, .. }` 是为了在面板事件回来之前先把高亮立起来、封住异步空窗；终端路径不需要，因为终端的 `active_entry` 一贯由 `AgentPanelEvent::ActiveViewChanged` 等面板事件同步回来（权威来源在面板，u2-l3），创建动作后下一轮事件自然会把高亮带上，乐观写反而可能与面板状态打架。

**练习 2**：`NewEntryTarget::Terminal` 存在的意义是什么？把 `new_terminal_thread` 的异步分支改成 `NewEntryTarget::LastCreatedKind` 会发生什么？

答案：`NewTerminalThread` 动作语义是「明确要一个终端」。若改用 `LastCreatedKind`，工作区异步打开后会走 `create_new_entry` 重新查询面板偏好——若用户上一次创建的是线程（默认态），点「New Terminal Thread」却得到一个线程草稿，动作语义被破坏。`Terminal` 变体把「意图」冻结在异步边界之前，不受期间偏好变化影响。

**练习 3**：`new_thread_in_group` 为什么要 `self.set_group_expanded(&key, true, cx)`？删掉这行会怎样？

答案：新建的草稿行属于该分组，分组折叠时 `rebuild_contents` 走 `should_load_threads = false` 支线（u3-l4），连行都不收集；即便空草稿行勉强渲染，用户也看不到「刚点出来的东西」。先展开保证新建立即可见。（注意 `set_group_expanded` 修改的是宿主 MultiWorkspace 的分组状态，随后触发的重建才会读到新值。）

### 4.2 LastCreatedKind：面板记住你上一次要的类型

#### 4.2.1 概念说明

`create_new_entry` 的分派依据是 `should_create_terminal_for_workspace`，它只问面板一个问题（[sidebar.rs:L6865-L6874](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6865-L6874)）：

```rust
workspace.read(cx).panel::<AgentPanel>(cx)
    .is_some_and(|panel| panel.read(cx).should_create_terminal_for_new_entry(cx))
```

面板的回答由两个字段与运算（[crates/agent_ui/src/agent_panel.rs:L2002-L2005](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L2002-L2005)）：

```rust
self.last_created_entry_kind == AgentPanelEntryKind::Terminal
    && self.project.read(cx).supports_terminal(cx)
```

`last_created_entry_kind` 是一个**记忆字段**，但有趣的是它不住在 Sidebar 里，而住在 AgentPanel 里。理由有三：

1. **写入点在面板**：`activate_new_thread` 与 `new_terminal` 是面板方法，顺手记录最自然（分别见 [agent_panel.rs:L1780](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L1780) 与 [agent_panel.rs:L1971](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L1971)）。侧边栏若要持有它，就得在每次创建后再回调同步一次，徒增一条易漏的接线。
2. **语义归属**：它记录的是「面板的新建入口偏好」，面板自己的 `+` 按钮（非侧边栏路径）同样使用它，放在面板才能让所有入口共享一份记忆。
3. **持久化通道现成**：面板已有序列化机制，且该偏好还要**跨窗口、跨项目**共享。

于是「连续点击新建按钮为什么第二次倾向复用上一种类型」的完整答案是：第一次点击时 `set_last_created_entry_kind_from_user_action` 把本次类型写入面板字段并异步落盘到全局 kvp；第二次点击时 `create_new_entry` 重新读取该字段，得到的就是第一次的类型——「+」的语义因此是「**再来一个我上次要的那种**」而非「永远来一个线程」。

#### 4.2.2 核心流程

```
                        ┌────────────── 写回端（面板） ──────────────┐
activate_new_thread ──▶ set_last_created_entry_kind_from_user_action(Thread)
new_terminal ─────────▶ set_last_created_entry_kind_from_user_action(Terminal)
                              │
                              ├─ 若值变化：字段更新 + serialize()（面板级持久化）
                              └─ background_spawn: write_global_last_created_entry_kind(kvp)
                                        key = "agent_panel__last_created_entry_kind"

                        ┌────────────── 读取端（侧边栏） ─────────────┐
「+」/ NewThreadInGroup ─▶ create_new_entry
                              └─ should_create_terminal_for_workspace
                                   └─ panel.should_create_terminal_for_new_entry()
                                        = last == Terminal && supports_terminal

                        ┌────────────── 恢复端（面板反序列化） ───────┐
面板 deserialize：serialized_panel.last_created_entry_kind
                  否则 → read_global_last_created_entry_kind(kvp)
                  否则 → 默认 AgentPanelEntryKind::Thread
```

注意写回端有个刻意的不对称：**恢复终端**（restore，即从序列化状态装回已有终端）**不**更新该字段——这是防回归测试 `test_restored_terminal_does_not_update_global_entry_kind`（[agent_panel.rs:L7742](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L7742)）锁定的行为：字段名是 last_created（上次**创建**），被动恢复不算创建，不该污染用户偏好。

#### 4.2.3 源码精读

写回端——值未变化时跳过 serialize，但 kvp 写入无条件执行（保持全局一致）（[agent_panel.rs:L2007-L2024](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L2007-L2024)）：

```rust
fn set_last_created_entry_kind_from_user_action(&mut self, entry_kind: AgentPanelEntryKind, ...) {
    if self.last_created_entry_kind != entry_kind {
        self.last_created_entry_kind = entry_kind;
        self.serialize(cx);
    }
    cx.background_spawn({
        let kvp = KeyValueStore::global(cx);
        async move { write_global_last_created_entry_kind(kvp, entry_kind).await; }
    })
    .detach();
}
```

kvp 的键与读写函数分别位于 [agent_panel.rs:L108](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L108)（`LAST_CREATED_ENTRY_KIND_KEY`）、[agent_panel.rs:L228](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L228)（读）与 [agent_panel.rs:L290](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L290)（写）。

恢复端的三级回退——面板自己的序列化值优先，其次是全局 kvp（新窗口/新面板继承全局偏好），最后默认 Thread（[agent_panel.rs:L1423-L1425](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L1423-L1425)）：

```rust
panel.last_created_entry_kind = serialized_panel.last_created_entry_kind;
// ...
} else if let Some(entry_kind) = global_last_created_entry_kind {
    panel.last_created_entry_kind = entry_kind;
}
```

新工作区加载时如何消费全局偏好的完整行为由 `test_new_workspace_load_uses_global_terminal_entry_kind`（[agent_panel.rs:L7794](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L7794)）锁定。

#### 4.2.4 代码实践

**实践目标**：完成规格指定的第一项任务——跟踪 `NewEntryTarget` / `last_created_entry_kind` 的读取与写回，亲手解释「第二次复用」现象。

**操作步骤**：

1. 制作一张「读 / 写位置对照表」，抄录以下五行（都已在前文给出链接）：写回 ① [agent_panel.rs:L1780](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L1780)（Thread）、写回 ② [agent_panel.rs:L1971](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L1971)（Terminal）、读取端 ③ [agent_panel.rs:L2002-L2005](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L2002-L2005)、侧边栏查询 ④ [sidebar.rs:L6858-L6862](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6858-L6862)、持久化 ⑤ [agent_panel.rs:L2017-L2023](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L2017-L2023)。
2. 运行锁定该行为的面板测试：

   ```bash
   cargo test -p agent_ui --lib test_terminal_entry_kind_controls_new_entry
   ```

3. 阅读该测试（[agent_panel.rs:L9525](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L9525)），找出它断言的两件事：创建终端后字段变为 Terminal；随后的新建入口产出终端而非线程。
4. 用一句话写下你的解释，对照本节 4.2.1 的表述查漏。

**需要观察的现象**：测试通过；测试体内对 `last_created_entry_kind` 的断言值随「创建终端」的动作发生翻转。

**预期结果**：读 / 写表呈现出「写发生在执行器内部、读发生在分派器内部」的闭环——`create_new_entry` 调 `create_new_terminal`，后者（经 `panel.new_terminal`）把偏好写成 Terminal，于是下一次 `create_new_entry` 又会走进同一条分支。这就是「第二次倾向复用」的机制本身。

#### 4.2.5 小练习与答案

**练习 1**：为什么读取端的第二个条件 `supports_terminal(cx)` 不可省略？

答案：偏好说「要终端」只是必要条件；项目可能根本不支持终端（如远程项目未提供 shell、或 `project.supports_terminal` 为假）。此时若仍然走终端分支，用户点「+」会毫无反应。回退到线程分支保证「+」永远有产出。这也解释了为什么偏好是**偏好**而不是命令——环境不允许时静默降级。

**练习 2**：`last_created_entry_kind` 写 kvp 为什么放在 `background_spawn` 里 detach，而不是在主线程同步写？

答案：kvp 写是异步 I/O（落盘），gpui 前台线程不能阻塞等待；且该写入只是「尽力同步全局偏好」，失败也不该影响创建流程本身。`detach` 让它独立运行，代价是极端情况下（写未完成就断电）全局偏好可能滞后于面板内存值一拍——无害，因为面板内存值才是本次会话的读取源。

**练习 3**：假如把这个字段搬到 Sidebar 结构体里，最少需要改几处、会引入什么坏味道？

答案：至少要在 `create_new_thread` / `create_new_terminal` 里各加一次写回、在序列化体系里加一份持久化，还要让非侧边栏入口（面板自己的 + 按钮）也能更新它——要么面板反向调用侧边栏（形成双向依赖），要么两处各存一份（状态分叉）。坏味道的本质：状态离开了它的权威写入源。

### 4.3 草稿行模型：DraftKind 与 draft_display_label_for_thread_metadata

#### 4.3.1 概念说明

草稿行在 `rebuild_contents` 里与普通线程行走同一条构造路径（u3-l4 的 `make_thread_entry`），唯一的分岔是 `ThreadEntry.draft: Option<DraftKind>` 字段：非草稿为 `None`，草稿是两形态之一（[sidebar.rs:L342-L346](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L342-L346)）：

```rust
enum DraftKind {
    WithContent,   // 用户敲过字（或磁盘上有残留提示词）→ 显示内容摘要
    Empty,         // 一个字都没有 → 显示 "New {agent} Thread" 占位符
}
```

为什么必须区分？因为两种草稿的用户价值完全不同：**有内容的草稿是资产**（用户敲的字不能丢，行必须一直在、可回访）；**空草稿只是「新线程插槽」的具象化**（用户正在看的那个才有意义，其余的显示出来全是噪音）。这个区分进而决定了可见性（4.4）、排序（4.4）、悬停按钮（4.4）、切换器收录（4.4）与时间刷新（4.5）五处行为。

草稿的**标题**不走 `ThreadMetadata.title`，而是每次重建时现算。`draft_display_label_for_thread_metadata` 是唯一的推导入口（[sidebar.rs:L306-L328](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L306-L328)），标题来源有三级：

1. **活编辑器文本**（最优）：草稿的 `ConversationView` 还装载在面板里时，直接读消息编辑器的当前文本——未落盘的字也能立刻反映。
2. **kvp 草稿库**（回退）：编辑器不在内存（如重启后、或草稿被「停靠」后从内存卸载）时，读 `draft_prompt_store` 里持久化的提示词块。
3. **占位符**（兜底）：两级都拿不到内容 → `empty_draft_placeholder_label` 生成 "New {agent} Thread"。

#### 4.3.2 核心流程

```
draft_display_label_for_thread_metadata(metadata, workspace, cx)
│
├─ workspace 为 Open(ws)？ ──▶ 提取 Some(ws)；Closed ──▶ None（后续查询只能走无工作区路径）
│
├─ display_label_for_draft(ws, thread_id, cx)          # agent_ui 侧
│    ├─ editor_text_if_in_memory == Some(Some(raw)) ──▶ truncate_draft_label(raw) = Some(label)
│    │                                                  → 返回 (label, WithContent)
│    ├─ editor_text_if_in_memory == Some(None) ──────▶ 返回 None
│    │    （编辑器在内存但为空：明确知道没有内容）
│    └─ 否则（不在内存）──▶ kvp read(thread_id) 取提示词块
│         └─ 有文本块 ──▶ truncate ──▶ (label, WithContent)
│
└─ 以上皆空 ──▶ empty_draft_placeholder_label(ws, agent_id, cx)
                 = "New {agent 显示名} Thread" ──▶ (placeholder, Empty)
```

`truncate_draft_label` 的压缩规则（[draft_prompt_store.rs:L122-L133](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/draft_prompt_store.rs#L122-L133)）：只取第一行 → 清洗 `[@mention](file://…)` 链接为纯 `@mention`（清洗函数在其上方）→ 压缩连续空白 → 截断到 `MAX_LABEL_CHARS`。全空返回 `None`。

占位符里的 agent 显示名（[draft_prompt_store.rs:L169-L184](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/draft_prompt_store.rs#L169-L184)）：Zed 内置 agent 用固定名；自定义 agent 经 `agent_server_store` 查显示名；`workspace` 为 `None`（Closed 工作区）时查不到 store，只能退化为 agent_id 字符串本身——这是 Closed 草稿占位符可能「不好看」的根源，也是 4.5 节 `remove_draft` 要先打开工作区的动机之一。

#### 4.3.3 源码精读

推导入口的两分支结构（[sidebar.rs:L316-L327](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L316-L327)）：

```rust
if let Some(label) = agent_ui::draft_prompt_store::display_label_for_draft(workspace, metadata.thread_id, cx) {
    return Some((label, DraftKind::WithContent));
}
let placeholder = agent_ui::draft_prompt_store::empty_draft_placeholder_label(
    workspace, &metadata.agent_id, cx,
);
Some((placeholder, DraftKind::Empty))
```

注意当前实现里**两个分支都返回 `Some`**——这个函数不会失败，最多给出占位符。真正「草稿不渲染为行」的裁决不在这里，而在 4.4 的两道 retain。

`rebuild_contents` 中的调用点。构造时先一律标 `WithContent`，注释明说了这是「先乐观、后降级」的策略（[sidebar.rs:L1568-L1571](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1568-L1571)）：

```rust
// Start drafts as `WithContent`; the post-processing
// pass below downgrades them to `Empty` if no draft
// label can be derived.
let draft = row.is_draft().then_some(DraftKind::WithContent);
```

后处理遍历每个草稿行，用推导结果**就地覆盖标题**并可能降级 kind（借 `Arc::make_mut` 写时复制，u2-l1 讲过的零拷贝修补手法）（[sidebar.rs:L1671-L1685](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1671-L1685)）：

```rust
for thread in &mut threads {
    if thread.draft.is_none() { continue; }
    if let Some((label, kind)) = draft_display_label_for_thread_metadata(
        &thread.metadata, &thread.workspace, cx,
    ) {
        let thread = Arc::make_mut(thread);
        thread.metadata.title = Some(label);   // 草稿标题 = 现算标签，覆盖库里的旧值
        thread.draft = Some(kind);             // 可能 WithContent → Empty 降级
    }
}
threads.retain(|thread| thread.draft.is_none() || thread.metadata.title.is_some());
```

最后一行 retain 是**第一道 retain**（契约防御）：无标签的草稿不许进列表。如前所述，由于推导函数当前恒返回 `Some`，这道 retain 现在不会真正删行；它守护的是「若未来某些草稿连占位符都给不出，列表也不会出现无标题行」这一契约。

配套谓词 `thread_metadata_would_render_sidebar_row`（[sidebar.rs:L330-L340](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L330-L340)）：非草稿恒真；草稿则等价于「能推出标签」：

```rust
if !metadata.is_draft() { return true; }
draft_display_label_for_thread_metadata(metadata, workspace, cx).is_some()
```

它的消费点不在行收集（那里有更严的 retain），而在**折叠分组**的 `has_stored_thread_rows` 判定：分组折叠时行根本不进列表，但分组头的 `has_threads` 徽标（决定是否渲染 "No threads yet" 子行，u4-l2）仍需回答「这个组里到底有没有东西可显示」——于是对库里逐条元数据问这个谓词（[sidebar.rs:L1798-L1813](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1798-L1813)）。当前它对草稿恒真，因此折叠组的徽标「只看库里有无线程行」；若未来标签推导可能失败，这里会自动收紧。

#### 4.3.4 代码实践

**实践目标**：完成规格指定的第二项任务的前半——用真实测试验证草稿标题的三级来源，并对照 `thread_metadata_would_render_sidebar_row` 说明空草稿何时根本不渲染。

**操作步骤**：

1. 运行：

   ```bash
   cargo test -p sidebar --lib test_draft_title_updates_from_editor_text
   ```

2. 精读测试 [sidebar_tests.rs:L5550-L5629](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L5550-L5629)。它把三级来源中的前两级各演了一遍：
   - 用 `type_draft_prompt(&panel, "Fix the login bug", cx)` 往活编辑器敲字（辅助函数会等 kvp 防抖落盘）；
   - 再用 `panel.new_thread(&NewThread, ...)` 把这个有内容的草稿「停靠」（park）；
   - **阶段 1** 断言标题来自活编辑器文本（草稿还在 `retained_threads` 里装载着）；
   - **阶段 2** 用 `test_unload_retained_thread` 把会话视图从内存卸掉、手动 `update_entries`，断言标题**仍然**是 "Fix the login bug"——这次来自 kvp 草稿库。
3. 回答书面问题：阶段 2 里 `draft_display_label_for_thread_metadata` 走的是哪个分支？`DraftKind` 是哪个？
4. 继续回答：如果把 `type_draft_prompt` 换成不敲任何字，草稿行会以什么标题、什么 kind 进入列表？（提示：结合 4.3.2 流程图的兜底分支，以及 4.4 将讲的第二道 retain——「不敲字的草稿」与「正在被查看的空草稿」是两种命运。）

**需要观察的现象**：测试两个阶段的断言都通过；两次 `draft_title` 读取值一致但数据来源不同。

**预期结果**：阶段 2 走「kvp 回退」分支（编辑器不在内存，`editor_text_if_in_memory` 返回外层 `None`），kind 仍为 `WithContent`（有内容）；不敲字的草稿若被查到标题则只能是占位符 "New {Agent} Thread" 且 kind 为 `Empty`——但只有「活跃面板正在查看」的那一个空草稿能活到渲染（见 4.4）。

#### 4.3.5 小练习与答案

**练习 1**：为什么草稿标题每次重建都现算，而不像普通线程那样信任库里的 `title` 字段？

答案：草稿的本质是「编辑中的文本」，其「标题」是内容的投影。用户每敲一个字，内容就变；若把摘要写回库里，既要处理防抖落盘延迟，又会在用户撤销输入后留下脏标题。现算保证标题与内容严格一致，且天然覆盖「编辑器在内存但未落盘」的窗口期（这也是 4.5 草稿编辑器观察存在的理由：内容变了要触发重算）。

**练习 2**：`display_label_for_draft` 里 `Some(None)` 与外层 `None` 的区别是什么？为什么必须区分？

答案：`editor_text_if_in_memory` 返回的是 `Option<Option<String>>`。外层 `None` = 「这个草稿的编辑器不在内存」→ 还有 kvp 可查；内层 `Some(None)` = 「编辑器就在内存里，且内容为空」→ **明确知道没有内容**，再去查 kvp 只会查到旧磁盘残留（例如用户全删了文字但防抖还没触发），显示错误摘要。区分两者让「空」成为确定知识而不是缺省猜测。

**练习 3**：`thread_metadata_would_render_sidebar_row` 在当前实现下对草稿恒为真，那它存在的价值是什么？

答案：它是「一行元数据是否值得在侧边栏占一行」这一业务判断的唯一命名出口，被折叠分组的 `has_threads` 判定复用。当前恒真只是「占位符兜底让标签推导永不失败」这一事实的推论；判断集中在一处，未来收紧（比如跳过孤儿草稿元数据）只需改一个函数，两个消费点（行收集的 retain、折叠组的徽标）自动一致。

### 4.4 空草稿的可见性、排序与渲染差异

#### 4.4.1 概念说明

4.3 留下的悬念在这里揭晓：**空草稿在什么条件下根本不渲染为行？** 答案写在 `rebuild_contents` 的第二道 retain（[sidebar.rs:L1687-L1702](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1687-L1702)）：

```rust
// Keep empty drafts only while their thread is active; preserve
// drafts with content because they hold user-typed state.
let pending_activation = self.pending_thread_activation;
let active_panel_thread_id = active_workspace
    .as_ref()
    .and_then(|ws| ws.read(cx).panel::<AgentPanel>(cx))
    .and_then(|panel| panel.read(cx).active_thread_id(cx));
threads.retain(|thread| {
    if thread.draft != Some(DraftKind::Empty) {
        return true;                                  // 非空草稿 / 正式线程：无条件保留
    }
    if pending_activation.is_some() {
        return false;                                 // 异步激活进行中：所有空草稿让位
    }
    Some(thread.metadata.thread_id) == active_panel_thread_id
});
```

即：一个空草稿要渲染为行，必须同时满足（1）没有正在进行的异步线程激活（`pending_thread_activation` 为空）；（2）它**恰好是活跃工作区面板正在查看的线程**。翻译成用户语言：**空草稿行就是「你眼前这个新线程插槽」的镜像**——你在哪个草稿上，列表顶上就有它一行；切走了它就消失；同一时刻最多只有一个。`test_only_actively_viewed_empty_draft_is_visible_in_sidebar`（[sidebar_tests.rs:L6177-L6380](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L6177-L6380)）的注释把这个不变式总结为三条：非活跃工作区的空草稿隐藏；被停靠（parked）而用户在看别的线程的空草稿隐藏；切换活跃工作区后占位行跟随新的活跃面板。

两个条件各有来历：

- `pending_activation` 让位（u6-l1）：异步激活的目标线程即将落地，若先渲染出别的空草稿行，落地瞬间行会突兀地插入/消失，高亮也会错位；封住窗口期让列表稳定。
- 与 `active_panel_thread_id` 相等：面板才是「用户在看什么」的权威；`active_entry` 是侧边栏的乐观副本，可能短暂超前（比如刚 `create_new_thread` 写入、面板事件还没回来），用面板值避免「点了 + 但行闪现一下又消失」的竞态。

#### 4.4.2 核心流程

空草稿的一生（同一份 `DraftKind` 数据驱动的五处行为差异）：

| 行为点 | WithContent 草稿 | Empty 草稿 | 代码位置 |
| --- | --- | --- | --- |
| 第二道 retain | 无条件保留（持有用户输入，是资产） | 仅当「无 pending 激活 且 是活跃面板当前线程」 | [sidebar.rs:L1694-L1702](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1694-L1702) |
| 组内排序 | 按 `interacted_at`/`updated_at` 正常排序 | 钉在 `DateTime::MAX_UTC`（组内最顶） | [sidebar.rs:L5714-L5723](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5714-L5723) |
| 行内时间戳 | 显示相对时间 | 空字符串（不显示） | [sidebar.rs:L6139-L6143](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6139-L6143) |
| 行内图标 | `IconName::Circle`（空心圆、淡化） | 同左 | [sidebar.rs:L6152-L6156](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6152-L6156) |
| 悬停上下文按钮 | "Discard Draft"（✕，调 `remove_draft`） | **无**（没法丢弃一个空插槽） | [sidebar.rs:L6258-L6276](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6258-L6276) |
| 切换器收录 | 收录 | 排除（空插槽不参与 ctrl-tab） | [sidebar.rs:L5779-L5782](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5779-L5782) |

排序钉顶的实现在 `push_entries_by_display_time` 的内嵌 `display_time` 函数——空草稿的显示时间直接取最大值，排序键大到无人能敌（[sidebar.rs:L5714-L5723](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5714-L5723)）：

```rust
fn display_time(entry: &ListEntry) -> DateTime<Utc> {
    match entry {
        ListEntry::Thread(thread) if thread.draft == Some(DraftKind::Empty) => {
            DateTime::<Utc>::MAX_UTC
        }
        ListEntry::Thread(thread) => Sidebar::thread_display_time(&thread.metadata),
        ListEntry::Terminal(terminal) => terminal.metadata.created_at,
        ListEntry::ProjectHeader { .. } => unreachable!(),
    }
}
```

悬停按钮三分支完整版（含非草稿的归档按钮）：

```rust
match thread.draft {
    Some(DraftKind::Empty) => None,                      // 无按钮
    Some(DraftKind::WithContent) => Some(/* ✕ Discard Draft → remove_draft */),
    None => Some(/* 📦 Archive Thread → archive_thread */),
}
```

键盘路径同构：`archive_selected_thread` 对草稿行改走 `remove_draft` 而非 `archive_thread`（[sidebar.rs:L5654-L5660](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5654-L5660)）——「归档一个草稿」在产品语义上就是「丢弃它」。

#### 4.4.3 源码精读（测试佐证）

`test_only_actively_viewed_empty_draft_is_visible_in_sidebar` 的场景搭建（[sidebar_tests.rs:L6178-L6272](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L6178-L6272)）值得精读：它构造了**两个工作区**（主仓库 + linked worktree `/wt-feature-a`），各挂一个 AgentPanel；主面板先造一个正式线程（发消息转正），再开一个空草稿；worktree 面板也开一个空草稿；最后显式把主工作区切回活跃。断言用的计数闭包直接数列表里 `DraftKind::Empty` 行的个数（[sidebar_tests.rs:L6280-L6290](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L6280-L6290) 附近）而不是认 thread_id——注释解释了原因：「draft 创建流程可能留下孤儿临时元数据，同样会被过滤器隐藏，数行数比认 id 更稳」。这是一个很值得模仿的断言风格：**断言不变式（至多一个空草稿行），而不是断言具体实现细节（哪个 id）**。

另一个值得跑的测试是 `test_plus_button_reuses_empty_draft`（[sidebar_tests.rs:L5683](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L5683)）：空草稿已活跃时再点 `+`，面板只聚焦它、不再造新的——所以「空草稿插槽」全窗口最多一个的另一半保证在面板侧（`activate_new_thread` 里对停靠草稿的复用分支，[agent_panel.rs:L1782-L1802](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L1782-L1802)），侧边栏的 retain 只是把这个事实如实地投到列表上。

#### 4.4.4 代码实践

**实践目标**：亲眼验证「同一时刻至多一个空草稿行，且它跟随活跃工作区」。

**操作步骤**：

1. 运行：

   ```bash
   cargo test -p sidebar --lib test_only_actively_viewed_empty_draft_is_visible_in_sidebar
   cargo test -p sidebar --lib test_plus_button_reuses_empty_draft
   ```

2. 在第一个测试里找到三处断言对应的场景分支（非活跃工作区隐藏 / 停靠后隐藏 / 切换后跟随），给每处注明它验证的是 retain 的哪个条件。
3. 本地思想实验（不必改码）：若把 retain 里的 `pending_activation.is_some()` 分支删掉，`test_confirm_on_historical_thread_activates_workspace`（[sidebar_tests.rs:L4619](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L4619)）一类的「先开工作区再激活」测试中，激活窗口期活跃面板可能还停留在某个空草稿上——预测会出现什么视觉毛刺（行先出现再消失？高亮错位？），把预测写下来。

**需要观察的现象**：两个测试均通过；第一个测试体内空草稿行计数在任何时刻都 ≤ 1。

**预期结果**：retain 两个条件各自的必要性由测试场景背书；思想实验的答案属于「待本地验证」——真正的验证方式是本地删分支跑全量 `cargo test -p sidebar`，观察哪些测试变红（这比肉眼猜毛刺更可靠）。

#### 4.4.5 小练习与答案

**练习 1**：为什么空草稿钉顶用的是 `DateTime::MAX_UTC` 这种「作弊」排序键，而不是在 push 时特殊处理「空草稿先 push」？

答案：列表行的产出走统一的「线程 + 终端混排、按显示时间倒序」管线（`push_entries_by_display_time`）。给空草稿一个最大时间键，让它在通用比较器里自然夺冠，可以不破坏管线、不加特判分支；代价是读代码的人必须知道这个约定——所以它紧挨着 `DraftKind::Empty` 的匹配臂，语义损失很小。

**练习 2**：有内容的草稿为什么不能也钉顶？想象用户三天前停靠了一个写了半句话的草稿。

答案：有内容草稿是资产，且数量不固定；若全部钉顶，列表头部会被旧草稿占满，淹没按时间排的正常线程。它按 `interacted_at`/`updated_at` 排序——刚编辑过的自然靠上，久搁的沉底——与正式线程同一套时间语义。空草稿钉顶的真正理由是「它是当前操作的焦点」，这个理由只对唯一的那个空插槽成立。

**练习 3**：切换器（`mru_entries_for_switcher`）为什么排除空草稿却不排除有内容草稿？

答案：ctrl-tab 切换器的语义是「回到最近用过的条目」。空插槽没有可回访的内容（面板里它就是一个空编辑器，且同一时刻唯一、随时可由 `+` 直达）；有内容草稿放着用户的半成品，正是用户最可能想回去的地方之一。收录判定与可见性判定各自独立，`DraftKind` 一个字段同时服务两者。

### 4.5 re-filled 草稿、编辑器观察与 remove_draft

#### 4.5.1 概念说明

本模块收尾三件事：时间刷新、观察网与草稿的消亡。

**re-filled 草稿的时间问题**。场景：用户开了草稿（空）、没打字切走了（行消失）、几天后回来点开这个草稿——不对，更典型的场景是：空草稿在活跃插槽里，用户开始打字，第一 keystroke 让它从 `Empty` 变成 `WithContent`。此刻它从「钉顶」切换到「按 `interacted_at` 排序」——若 `interacted_at` 还是几小时前创建时的时间戳，一个**正在被编辑**的草稿会突然沉到列表中部，行在用户眼皮底下「跳走」。`refresh_refilled_draft_times` 就是为这一跳准备的：检测到草稿从 `Empty` 变回 `WithContent`（re-filled），立刻把它的 `interacted_at` 刷成现在，让它继续稳居顶部。

这解释了 `draft_kinds` 记忆字段的存在（[sidebar.rs:L769-L772](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L769-L772)）：**「上一刻是空还是有内容」无法从当前世界状态推导**——当前渲染帧只能看到现在的 kind，看不到它刚从什么变来。这正是 u3-l2 记忆字段豁免权的标准适用情形。

**草稿编辑器观察**。标题是内容的现算投影（4.3），那么「用户敲字」必须触发重建，标题才会跟上。u3-l1 讲过 `refresh_draft_editor_observations` 是唯一「每次重建后清空重连」的订阅组：会话视图集合只能现查、其消息编辑器实体在生命周期转换（Loading → Connected）中会被替换，所以订阅存在 `_draft_editor_observations: Vec<gpui::Subscription>` 字段里随重建更新（[sidebar.rs:L2111-L2147](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2111-L2147)），`MessageEditorEvent::Edited` 触发一次 `schedule_update_entries(false, cx)`。

**remove_draft 的两段式**。丢弃草稿远比「从列表删一行」重：草稿可能挂在一个 linked worktree 工作区上，而那个工作区可能就是为它临时开的——草稿没了，工作区要不要移除？里面的归档根怎么处理？侧边栏在这里沿用了 u6-l2 讲过的模式：**先纯预计算、副作用押后到 `remove_workspaces_then` 的收尾闭包**。

#### 4.5.2 核心流程

```
remove_draft(draft_id, workspace)
│
├─ workspace 为 Closed 且值得为其开工作区
│    └─ open_workspace_for_archive(...) 成功后以 Open 递归调用 remove_draft   # 只递归一层
│
├─ 纯预计算（不产生任何副作用）：
│    ├─ metadata ← ThreadMetadataStore.entry(draft_id)          # 可能已不存在（None 全程容忍）
│    ├─ draft_folder_paths / draft_remote_connection
│    ├─ roots_to_archive ← roots_to_archive_for_paths(...)      # u8-l2 的归档根推导
│    ├─ was_active ← active_entry 是否正是这个草稿
│    ├─ neighbor ← neighboring_activatable_entry(行位置)         # u5-l2 的接班人
│    ├─ workspace_to_remove ← linked_worktree_workspace_to_remove(...)
│    └─ close_item_tasks ← close_items_for_archived_worktrees(...)
│
└─ remove_workspaces_then(workspaces_to_remove, close_item_tasks, ..., finish):
     ├─（移除工作区 / 关闭条目 / 等待用户确认——任一环节取消则整链终止）
     └─ finish 闭包（副作用落地）：
          ├─ 若草稿的工作区被移除 → delete_empty_drafts_for_archive_paths(...)   # 顺带清同类空草稿
          └─ remove_draft_entry(draft_id, workspace, was_active, neighbor, ...)
                ├─ activate_panel_draft = activate_panel_draft && !(was_active && neighbor.is_some())
                ├─ Open 工作区：panel.remove_thread / remove_thread_without_activating_draft
                │    （否则回退）ThreadMetadataStore.delete(draft_id)
                ├─ start_detached_archive_worktree_task(roots_to_archive)         # 归档任务分离执行
                └─ was_active 时：active_entry = None → 激活 neighbor 或对账同步
                                     最后 update_entries(cx)
```

`refresh_refilled_draft_times` 的算法（每次 `update_entries` 中、`rebuild_contents` 之后执行，[sidebar.rs:L2005-L2007](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2005-L2007) 的调用顺序）：

1. 扫描本次重建出的行，收集每个草稿的当前 kind 到 `new_kinds`；
2. 当前 `WithContent` 且记忆里是 `Empty` 的 → 加入 `refilled` 名单；
3. `self.draft_kinds = new_kinds`（记忆整体换血，消失的草稿自动除名）；
4. 对 `refilled` 名单逐个 `store.update_interacted_at(thread_id, Utc::now())`——注意这会写库并触发 Store 的 observe，再排一轮重建（u3-l2 讲过的「草稿时间写回触发重入、收敛循环」）。

#### 4.5.3 源码精读

时间刷新本体——判断条件一行道尽语义（[sidebar.rs:L2073-L2109](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2073-L2109)）：

```rust
if kind == DraftKind::WithContent
    && self.draft_kinds.get(&thread_id) == Some(&DraftKind::Empty)
{
    refilled.push(thread_id);
}
// ...
self.draft_kinds = new_kinds;
if refilled.is_empty() { return; }
let now = Utc::now();
ThreadMetadataStore::global(cx).update(cx, |store, store_cx| {
    for thread_id in refilled {
        store.update_interacted_at(&thread_id, now, store_cx);
    }
});
```

注意 `draft_kinds` 的**换血时机**：即 `refilled` 为空也会先整体替换记忆——保证「草稿消失」（被删、转正为正式线程）后旧条目不残留。转正正是最常见的退出路径：草稿发出第一条消息拿到 `session_id`，`is_draft` 变假、`draft` 字段变 `None`，下次扫描自然不再进 `new_kinds`。

编辑器观察的双重订阅——编辑器本体 + 会话视图实体各订一份（[sidebar.rs:L2126-L2146](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2126-L2146)）：

```rust
if let Some(thread_view) = cv.read(cx).active_thread() {
    let editor = thread_view.read(cx).message_editor.clone();
    self._draft_editor_observations.push(cx.subscribe(
        &editor,
        |this, _editor, event, cx| match event {
            MessageEditorEvent::Edited => this.schedule_update_entries(false, cx),
            _ => (),
        },
    ));
}
// Also subscribe to the ConversationView itself so that editor
// replacements during lifecycle transitions (Loading → Connected) re-wire
// the editor observation above.
self._draft_editor_observations.push(cx.subscribe(
    &cv,
    |this, _cv, _event: &StateChange, cx| {
        this.schedule_update_entries(false, cx);
    },
));
```

`remove_draft` 的 Closed 分支——先开工作区再以 Open 递归（[sidebar.rs:L6668-L6693](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6668-L6693)）：

```rust
if let ThreadEntryWorkspace::Closed { folder_paths, project_group_key } = workspace
    && self.should_load_closed_workspace_for_archive(...)
{
    self.open_workspace_for_archive(
        folder_paths.clone(), project_group_key.clone(), window, cx,
        move |this, workspace, window, cx| {
            this.remove_draft(draft_id, &ThreadEntryWorkspace::Open(workspace), window, cx);
        },
    );
    return;
}
```

为什么值得为一个删除打开整个工作区？因为后续裁决（要不要连带移除这个 linked worktree 工作区、归档哪些根）需要真实的 git/文件系统状态，`Closed` 的两条路径字段撑不起这些查询。

`remove_draft_entry` 的交接优先级——「邻居 > 面板新草稿 > 对账」（[sidebar.rs:L6801-L6845](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6801-L6845)）：

```rust
// Fallback to a neighbor thread when the discarded
// draft was the active entry.
let activate_panel_draft = activate_panel_draft && !(was_active && neighbor.is_some());
```

被丢弃的草稿若正是活跃条目：有邻居就先激活邻居（`activate_panel_draft` 压为假，面板侧用 `remove_thread_without_activating_draft` 防止面板自作主张开新草稿抢焦点，u6-l2 讲过同一手法）；没有邻居才允许面板开新草稿填补空屏；两者皆无则 `sync_active_entry_from_active_workspace` 纯对账。最后 `update_entries(cx)` 收尾，列表与记忆（`draft_kinds` 下轮换血时自动除名）归于一致。

#### 4.5.4 代码实践

**实践目标**：验证「丢弃草稿 = 删除元数据行（而不只是藏起来）」，并把 remove 链路的预计算项逐一对号。

**操作步骤**：

1. 运行：

   ```bash
   cargo test -p sidebar --lib test_remove_draft_deletes_metadata_row
   ```

2. 精读测试（[sidebar_tests.rs:L5865](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L5865)）：它用 `save_draft_metadata_with_main_paths`（[sidebar_tests.rs:L460-L486](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L460-L486)，注意 `session_id: None` —— 一行元数据就此成为草稿）播种一个草稿，触发 `remove_draft`，断言 `ThreadMetadataStore` 里该 id 的行**真的没了**。
3. 对照 4.5.2 的预计算清单，在 `remove_draft`（[sidebar.rs:L6656-L6788](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6656-L6788)）里逐项打钩，标出每一项在 finish 闭包里的消费点。
4. 思考题（写进笔记）：`refresh_refilled_draft_times` 写 `interacted_at` 会触发一轮额外重建，这轮重建里 `draft_kinds` 已换血、不会再有 refilled——为什么这个重入注定收敛而不是无限循环？

**需要观察的现象**：测试通过；断言针对的是 Store 里的元数据行（持久层），不是侧边栏的可见行（投影层）。

**预期结果**：草稿被丢弃后持久元数据一并删除，这与「归档线程」（保留元数据、只置 `archived` 标志，u8-l2）形成鲜明对照——丢弃是彻底销毁。思考题答案：refilled 名单只在「记忆 ≠ 当前」时非空；写库触发的重建读到的已是刷新后的状态，第二轮扫描时两者相等、名单为空、提前 return，循环至多两轮。

#### 4.5.5 小练习与答案

**练习 1**：`refresh_refilled_draft_times` 为什么挂在 `update_entries` 里、紧跟 `rebuild_contents` 之后，而不是放在 `rebuild_contents` 内部？

答案：它要**对比**重建结果（当前 kind）与记忆（上一次 kind），属于「重建之后对结果的审计」；放进 `rebuild_contents` 会把「推导列表」与「对比记忆并写库」两件事搅在一起，且写库副作用（触发又一轮重建）发生在推导中途，顺序上更难推理。分两步让每个函数保持单一职责，也让 u3-l2 讲的「update_entries 五步」清单保持清晰。

**练习 2**：`remove_draft` 里若 `metadata` 为 `None`（库里已无此行），后续代码为什么还能安全走完？

答案：所有依赖 metadata 的预计算都用 `as_ref().map(...).or_else(...)` / `and_then` 链取值：`draft_folder_paths` 回退到 workspace 参数自带的路径；`roots_to_archive` 回退 `unwrap_or_default()`（空归档计划）；`was_active`、`neighbor` 与行位置无关地照常计算。删除一个已不存在的草稿因此退化为「移除关联工作区（若有）+ 对账 + 重建」的幂等操作。

**练习 3**：丢弃草稿后面板可能自动开一个新草稿（`activate_panel_draft` 为真时）。什么情况下这是对的，什么情况下必须压掉？

答案：被弃草稿是活跃条目且**没有可接班邻居**时（整个列表空了、或只剩分组头），面板需要一个内容才不致空屏，开新草稿是对的；有邻居时若再开草稿，焦点会被抢到新插槽、用户刚点掉的「上下文」立刻复活一个空壳，所以要压成 `remove_thread_without_activating_draft`、把选择权交给邻居激活。非活跃草稿的丢弃则完全不碰交接（`was_active` 为假，整段跳过）。

## 5. 综合实践

把本讲五条线索串成一条完整的生命周期。请在本地完成一份「草稿一生」时序文档：

1. **场景**：一个已打开的项目分组，用户依次执行——点项目头「+」（偏好为默认 Thread）→ 敲入 "Fix the login bug" → 点「+」再开一个 → 切回第一个草稿 → 点它的 ✕（Discard Draft）。
2. **任务**：为每一步写一行记录，包含四列：**触发函数**（如 `create_new_entry` → `create_new_thread`）、**状态写入**（哪些字段/Store 被改：`active_entry`、`last_created_entry_kind`、`draft_kinds`、`ThreadMetadataStore`、kvp 草稿库）、**行的命运**（第二个空草稿为何第一刻不显示？敲字瞬间 Empty→WithContent 谁发现、谁刷时间？）、**证据链接**（本讲引用过的源码行号）。
3. **验证**：每一步的「行的命运」列都要能对应到一个可运行的测试（本讲用过的五个测试足够覆盖），把它们整理成文末的回归命令清单：

   ```bash
   cargo test -p sidebar --lib test_new_entry_noops_without_open_project
   cargo test -p sidebar --lib test_draft_title_updates_from_editor_text
   cargo test -p sidebar --lib test_only_actively_viewed_empty_draft_is_visible_in_sidebar
   cargo test -p sidebar --lib test_plus_button_reuses_empty_draft
   cargo test -p sidebar --lib test_remove_draft_deletes_metadata_row
   ```

4. **加分项（待本地验证）**：模仿 `test_draft_title_updates_from_editor_text` 的两阶段结构，尝试编写一个「Empty → 敲一个字 → 标题变为该字、行仍钉顶；再全删 → 标题回到占位符」的测试骨架。播种用 `save_draft_metadata_with_main_paths` + `agent_ui::test_support::open_draft_with_connection`，断言用本讲的 kind 判别式 `thread.draft == Some(DraftKind::Empty)`。骨架先写断言后写准备，失败信息会告诉你缺哪块脚手架。

## 6. 本讲小结

- **新建三件套分层清晰**：`create_new_entry` 是策略入口（空项目守卫 + 查面板偏好），`create_new_thread` / `create_new_terminal` 是对称的执行器（切工作区 → 面板创建 → 聚焦，线程版多一步乐观写 `active_entry`）；`NewEntryTarget` 是为了让创建意图能穿越「异步打开工作区」的 await 边界而存在的数据。
- **「+」的语义是「再来一个上次那种」**：偏好存在 AgentPanel 的 `last_created_entry_kind`，由面板的创建方法顺手写入，经全局 kvp 跨窗口持久化；被动恢复终端刻意不写回（restore ≠ create）。
- **草稿是 `session_id` 为空的线程行**，其标题不走库里的 `title` 而是每次现算：活编辑器文本 → kvp 草稿库 → "New {agent} Thread" 占位符三级回退，产出的 `DraftKind` 区分资产（WithContent）与插槽（Empty）。
- **空草稿行是「眼前插槽」的镜像**：第二道 retain 只保留「无 pending 激活且是活跃面板当前线程」的那一个；它钉在分组顶（`DateTime::MAX_UTC`）、无时间戳、无丢弃按钮、不进切换器——同一份 kind 数据驱动六处行为差异。
- **`draft_kinds` 是标准记忆字段**：「上一刻是空还是有内容」无法从当前状态推导，`refresh_refilled_draft_times` 靠它检测 re-filled 并刷新 `interacted_at`，防止正在编辑的草稿从顶部「跳走」。
- **丢弃草稿是两段式裁决**：Closed 草稿先开工作区；随后纯预计算（邻居、归档根、待移除工作区），副作用押后到 `remove_workspaces_then` 收尾——移交顺序固定为「邻居 > 面板新草稿 > 对账」，最终元数据真删（区别于归档的保留）。

## 7. 下一步学习建议

- **u7-l1（ThreadSwitcher 模态组件）**：本讲看到切换器「排除空草稿」的一处消费点，下一单元完整走读 `thread_switcher.rs` 这个自包含文件，看 `ThreadSwitcherEntry` 如何统一线程与终端。
- **u8-l2（工作树归档流水线）**：`remove_draft` 里预计算出的 `roots_to_archive`、`should_load_closed_workspace_for_archive`、`start_detached_archive_worktree_task` 都只是借用的钩子，其完整判定逻辑（归档根推导、级联关闭）在归档单元展开；建议先读 `test_archive_selected_draft_archives_linked_worktree_after_last_draft`（sidebar_tests.rs:L2574）建立直觉。
- **源码延伸阅读**：想深挖草稿标题的两级来源，可顺着 `draft_prompt_store.rs` 的 `read` / `write`（kvp 读写与防抖）与 `agent_panel.rs` 的 `editor_text_if_in_memory`（`Option<Option<..>>` 语义）继续读；想理解「停靠」机制本身，`test_plus_button_parks_nonempty_draft`（sidebar_tests.rs:L5746）与 `activate_new_thread` 的停靠复用分支是好入口。
