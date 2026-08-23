# u6-l2 终端条目的激活与关闭

## 1. 本讲目标

上一讲（u6-l1）我们沿着 `activate_thread` 走通了线程激活的三岔决策树。本讲把目光转向列表里的另一类可激活条目——终端（Terminal）行。终端行在侧边栏里与线程行并排展示，但它的激活与关闭链路有自己的特点：激活只有两岔（本窗口已打开 / 需要先打开工作区），关闭则由「面板主动请求」驱动，还牵扯到「关掉终端后要不要连带移除它的工作区」这一级联清理问题。

学完本讲，你应该能够：

1. 说出 `activate_terminal_entry` 与 `activate_thread` 在结构上的对称与不对称之处。
2. 讲清 `load_agent_terminal_in_workspace` 如何把一个终端「装回」AgentPanel（含面板尚未创建时的异步路径）。
3. 完整复述关闭链路：`TerminalEvent::CloseTerminal` → `AgentPanelEvent::TerminalCloseRequested` → `close_terminal` → `close_terminal_entry`，以及邻居条目的接管顺序。
4. 解释 `remove_workspaces_then` 为什么存在、它协调哪两类异步任务、以及用户在关闭确认对话框上点「取消」时会发生什么。

## 2. 前置知识

本讲默认你已读过 u6-l1（线程激活全链路），并熟悉以下概念。为独立成篇，先做简要回顾：

- **`TerminalThreadMetadata` 与 `TerminalId`**：终端行的持久元数据（标题、folder 路径、创建时间等）存放在全局的 `TerminalThreadMetadataStore`（一个 gpui 全局实体）中，`TerminalId` 是终端的身份标识。终端没有 `SessionId`——它不像线程那样有远端 ACP 会话身份，所以匹配只用 `terminal_id` 一把钥匙。
- **`ThreadEntryWorkspace`**：终端行携带的工作区信息，分两形态（见 u2-l2）。`Open(Entity<Workspace>)` 表示该终端的文件夹在**本窗口**已有打开的工作区实体；`Closed { folder_paths, project_group_key }` 表示只有重开工作区所需的「身份材料」。
- **`ActiveEntry::Terminal`**：侧边栏的全局高亮条目之一，只有 `terminal_id` 与 `workspace` 两个字段（见 sidebar.rs [L152-L155](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L152-L155)）。
- **`AgentPanel`**：挂在工作区（`Workspace`）上的代理面板，终端视图就活在面板里。侧边栏不拥有终端，只通过事件与面板、元数据存储交互。
- **`neighboring_activatable_entry`**：u5-l2 讲过的「接班人挑选」函数——删除某行前，在同项目分组内按「向下优先、其次向上」找出最近的**可激活**条目（跳过分组头），全分组都没有时才放眼整个列表。
- **Task 与 detach**：gpui 的 `Task` 被 drop 即取消；不想被取消的后台工作要 `.detach()` 或 `.detach_and_log_err(cx)`（见仓库 CLAUDE.md 的 Concurrency 一节）。

一个贯穿全讲的直觉：**侧边栏是编排者（orchestrator），不是执行者**。它决定「先做什么、后做什么、失败怎么办」，真正关闭终端、移除工作区、落盘归档的动作都委托给 AgentPanel、MultiWorkspace 和归档模块完成。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/sidebar/src/sidebar.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs) | 本讲主战场：激活分派、面板装载、关闭链路、级联移除协调全部在此 |
| [crates/sidebar/src/sidebar_tests.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs) | 两个终端关闭测试及测试脚手架（`init_test_project_with_agent_panel`、`setup_sidebar_with_agent_panel` 等） |
| [crates/agent_ui/src/agent_panel.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs) | 面板一侧：终端事件的消费、`close_terminal_internal`、`restore_terminal`、测试辅助 `insert_test_terminal` / `emit_test_terminal_close` |
| [crates/workspace/src/multi_workspace.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs) | 宿主一侧：`remove`（含 `RemovalIntent`）与 `retain_active_workspace` |

## 4. 核心概念与源码讲解

### 4.1 activate_terminal_entry：终端激活的两岔分派

#### 4.1.1 概念说明

点击一个终端行、在切换器里确认一个终端、用 `NextThread`/`PreviousThread` 循环到一个终端，最终都会落到 `activate_terminal_entry`。它是终端版的 `activate_thread`：接收元数据与行上携带的 `ThreadEntryWorkspace`，决定「就地激活」还是「先开工作区再激活」。

与线程激活的对称与不对称：

| 维度 | 线程（`activate_thread`） | 终端（`activate_terminal_entry`） |
| --- | --- | --- |
| 输入 | `ThreadMetadata` + `Entity<Workspace>` 句柄 | `TerminalThreadMetadata` + `ThreadEntryWorkspace`（两形态） |
| 分支数 | 三岔：本窗口 / 其他窗口 / 放弃 | 两岔：`Open` / `Closed` |
| 异步空窗保护 | `pending_thread_activation` 封住装载期 | 无对应字段（见下方解释） |
| MRU 时间戳 | `record_thread_access` → `thread_last_accessed` | `record_terminal_access` → `terminal_last_accessed` |

为什么终端没有跨窗口分支？因为线程激活传入的是**实体句柄**，需要查证它属于哪个窗口（sidebar.rs [L3928-L3951](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3928-L3951)）；而终端行在 `rebuild_contents` 时就由本窗口的 MultiWorkspace 推导出了 `Open`（本窗口实体）或 `Closed`（只有路径材料）两种形态之一，分派信息已经「编码」在行的 workspace 字段里。`Closed` 形态的处理是「在本窗口打开（或复用）工作区后递归回本地激活」，而不是去别的窗口找。

#### 4.1.2 核心流程

```
activate_terminal_entry(metadata, workspace, retain, window, cx)
├─ workspace 为 Open(entity)
│    └─ activate_terminal_in_workspace(entity, ...)          # 就地激活，见 4.1.3
└─ workspace 为 Closed { folder_paths, project_group_key }
     └─ open_workspace_and_activate_terminal(...)            # 先开工作区
          ├─ multi_workspace.find_or_create_workspace(...)   # 返回 Task
          └─ cx.spawn_in: await 打开结果
               ├─ 失败 → dismiss_connection_modal，链路终止
               └─ 成功 → activate_terminal_in_workspace(...) # 收敛回本地路径
```

`activate_terminal_in_workspace` 的「五件套」（与线程激活的「四件套」同构，多一步可选 retain）：

1. `record_terminal_access`：写入 MRU 时间戳（排序用，见 u7-l2）。
2. 乐观写 `active_entry = Some(ActiveEntry::Terminal { .. })`：不等面板，先把高亮立起来。
3. `multi_workspace.activate(workspace)`：把该工作区切为当前展示；`retain` 为真时追加 `retain_active_workspace`，把临时工作区「钉住」（pin）为持久。
4. `load_agent_terminal_in_workspace(...)`：把终端装进面板（4.2 详述）。
5. `update_entries(cx)`：全量重建列表（唯一真相原则）。

#### 4.1.3 源码精读

分派器本体——按 `ThreadEntryWorkspace` 两形态分流，这是本模块的核心逻辑（[sidebar.rs:L4466-L4491](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4466-L4491)）：

```rust
match workspace {
    ThreadEntryWorkspace::Open(workspace) => {
        self.activate_terminal_in_workspace(&workspace, metadata, retain, window, cx);
    }
    ThreadEntryWorkspace::Closed { folder_paths, project_group_key } => {
        self.open_workspace_and_activate_terminal(
            metadata, folder_paths, &project_group_key, window, cx,
        );
    }
}
```

就地激活的实现（[sidebar.rs:L4561-L4590](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4561-L4590)）——上面「五件套」对应的代码：先 `record_terminal_access(terminal_id)`，随后乐观写 `active_entry`，再经宿主切换工作区（`retain` 为真时调用 `retain_active_workspace` 把临时工作区提升为持久，见 [multi_workspace.rs:L1374-L1386](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L1374-L1386)），装载面板，最后 `update_entries`：

```rust
let terminal_id = metadata.terminal_id;
self.record_terminal_access(terminal_id);
self.active_entry = Some(ActiveEntry::Terminal { terminal_id, workspace: workspace.clone() });

multi_workspace.update(cx, |multi_workspace, cx| {
    multi_workspace.activate(workspace.clone(), None, window, cx);
    if retain {
        multi_workspace.retain_active_workspace(cx);
    }
});

Self::load_agent_terminal_in_workspace(workspace, &metadata, true, window, cx);
self.update_entries(cx);
```

`Closed` 形态的异步收敛（[sidebar.rs:L4592-L4633](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4592-L4633)）：`find_or_create_workspace` 返回任务，`cx.spawn_in` 中 await 后先关掉可能弹出的远程连接模态框，成功再调 `activate_terminal_in_workspace` 递归回本地路径。注意与线程版（u6-l1）不同，这里**没有**设置 `pending_thread_activation` 那样的防重入字段——终端没有草稿占位行合成的顾虑。

调用点一览（`retain` 参数从哪来）：

| 调用点 | retain | 场景 |
| --- | --- | --- |
| [sidebar.rs:L3556-L3560](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3556-L3560) | `false` | 键盘 `Confirm` 命中终端行 |
| [sidebar.rs:L6537-L6549](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6537-L6549) | `false` | 鼠标点击终端行 |
| [sidebar.rs:L5951](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5951) | `true` | 切换器确认（ctrl-tab 松键） |
| [sidebar.rs:L7129-L7132](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7129-L7132) | `true` | `cycle_thread_impl` 循环到终端行 |
| [sidebar.rs:L4450-L4462](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4450-L4462) | `false` | `activate_entry`（关闭旧条目后激活邻居，4.4 会用到） |

规律：**用户显式的「跳转」动作（切换器、循环）带 retain，普通激活不带**——前者希望目标工作区被钉住以免后续切换时被回收。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把终端激活与线程激活的决策树逐行对照，验证「两岔 vs 三岔」的论断。
2. **操作步骤**：
   - 打开 [sidebar.rs:L3928-L3951](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3928-L3951)（`activate_thread`）与 [sidebar.rs:L4466-L4491](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4466-L4491)（`activate_terminal_entry`）。
   - 为两棵决策树的每条边标注：判定条件、调用的目标函数、失败时的行为。
3. **需要观察的现象**：`activate_thread` 的第三岔（找不到任何窗口时）是静默 `return`；终端版的 `Closed` 岔在打开失败时也是静默终止——两处都没有用户可见的错误提示。
4. **预期结果**：得到一张两列对照表，能指出终端版少了 `find_workspace_across_windows` 这一岔。
5. 运行结果属于本地阅读结论，无需运行命令；若想验证行为差异，可在本地给 `open_workspace_and_activate_terminal` 的失败分支加一条 `log::warn!` 后运行任意终端测试观察是否触发——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `activate_terminal_entry` 不需要像 `activate_thread` 那样查证工作区属于哪个窗口？

答案：线程激活的入参是 `Entity<Workspace>` 句柄，句柄本身不携带「属于哪个窗口」的信息，所以要经 `find_workspace_in_current_window` / `find_workspace_across_windows` 现查；而终端行的 `workspace` 字段是 `ThreadEntryWorkspace` 枚举，`Open` 变体里的实体必然来自本窗口的 MultiWorkspace（`rebuild_contents` 就是这么构造它的），`Closed` 变体则直接走「本窗口重开」路径，信息在重建时已编码完毕。

**练习 2**：点击终端行与在切换器里确认终端，对目标工作区的「钉住」行为有何不同？为什么？

答案：点击行传 `retain=false`，切换器确认传 `retain=true`。`retain=true` 时 `activate_terminal_in_workspace` 会调用 `retain_active_workspace`，把临时工作区 pin 成持久，避免用户刚切换过去的工作区在下次切换时被回收；普通点击只是查看，不需要改变工作区的持久性。

**练习 3**：`activate_terminal_in_workspace` 里 `active_entry` 的写入为什么说成「乐观」？

答案：它在面板真正装载终端**之前**就写入了高亮状态。这是 u2-l3 讲过的「用户乐观预写」模式：先让 UI 立即反馈，随后面板装载完成会经 `AgentPanelEvent::ActiveViewChanged` → `sync_active_entry_from_panel` 对账，若装载结果与预写不符会被权威来源纠正。

### 4.2 load_agent_terminal_in_workspace：把终端装回 AgentPanel

#### 4.2.1 概念说明

激活的最后一公里是让 AgentPanel 真正显示这个终端。难点在于：**面板可能还不存在**。AgentPanel 是按需装载的（首次打开代理面板时才 `AgentPanel::load`），所以这个函数有两条路径——同步快路径（面板已在工作区上）与异步慢路径（先装载面板再恢复终端）。

面板侧的语义由 `AgentPanel::restore_terminal` 提供幂等性：终端已在面板里就仅激活它；不在则用元数据里的工作目录、标题、创建时间重新 spawn 一个同 id 的终端。

#### 4.2.2 核心流程

```
load_agent_terminal_in_workspace(workspace, metadata, focus, window, cx)
├─ 快路径：workspace.panel::<AgentPanel>() 已存在
│    ├─ panel.restore_terminal(metadata, focus, Sidebar 来源, None, ...)
│    └─ focus ? focus_panel : reveal_panel
└─ 慢路径：无面板
     └─ cx.spawn（异步，detach_and_log_err）
          ├─ panel = AgentPanel::load(workspace).await
          ├─ update_in：面板若已被他人并发加入则复用，否则 add_panel
          ├─ panel.restore_terminal(metadata, focus, Sidebar 来源, Some(workspace), ...)
          └─ focus ? focus_panel : reveal_panel
```

`focus_panel` 与 `reveal_panel` 的区别：前者把键盘焦点也交给面板，后者只是把面板展示出来。本函数在激活链路中的调用（`activate_terminal_in_workspace`）传的是 `focus=true`。

#### 4.2.3 源码精读

快慢两路的判别与收尾（[sidebar.rs:L4493-L4559](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4493-L4559)）——先定义 `restore_terminal` 闭包，再查现有面板；有则同步恢复并 `focus_panel`/`reveal_panel` 后提前返回：

```rust
let mut existing_panel = None;
workspace.update(cx, |workspace, cx| {
    if let Some(panel) = workspace.panel::<AgentPanel>(cx) {
        existing_panel = Some(panel);
    }
});

if let Some(agent_panel) = existing_panel {
    restore_terminal(agent_panel, metadata, focus, None, window, cx);
    workspace.update(cx, |workspace, cx| {
        if focus {
            workspace.focus_panel::<AgentPanel>(window, cx);
        } else {
            workspace.reveal_panel::<AgentPanel>(window, cx);
        }
    });
    return;
}
```

慢路径要点（同文件 [L4537-L4558](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4537-L4558)）：先 `downgrade` 工作区句柄、克隆元数据、取 `async_window_cx`，再 `cx.spawn` 异步等待 `AgentPanel::load`；回到前台后用 `workspace.panel::<AgentPanel>(cx).unwrap_or_else(|| workspace.add_panel(...))` 兼容「等待期间别人已把面板装上」的竞态，最后同样恢复终端并聚焦。整段以 `.detach_and_log_err(cx)` 结尾——装载失败只记日志，不打扰用户。

面板侧的幂等恢复（[agent_panel.rs:L2401-L2435](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L2401-L2435)）：`has_terminal` 时仅激活既有终端直接返回；否则设置 `pending_terminal_spawn` 并用元数据 `spawn_terminal`，把保存的 `working_directory`、`custom_title`、`created_at` 原样带回：

```rust
if self.has_terminal(metadata.terminal_id) {
    self.activate_terminal(metadata.terminal_id, focus, window, cx);
    return;
}
if !self.supports_terminal(cx) {
    return;
}
self.pending_terminal_spawn = Some(metadata.terminal_id);
```

对照线程版 `load_agent_thread_in_workspace`（u6-l1）：结构完全同构——查现有面板、有则恢复、无则 `AgentPanel::load` 后恢复，这是「线程/终端对称」在装载层的体现。差异在恢复原语：线程是装载会话视图，终端是 `panel.restore_terminal(...)`。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：跟踪一次「点击终端行」的完整调用链，验证快路径在测试里被走到。
2. **操作步骤**：
   - 从 [sidebar.rs:L6537-L6549](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6537-L6549)（`render_terminal` 的 `on_click`）出发，依次抄下经过的每个函数名与文件行号，直到 `AgentPanel::restore_terminal`。
   - 再看测试 `test_agent_panel_terminals_appear_in_sidebar_and_search`（[sidebar_tests.rs:L1762 起](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L1762)）：`setup_sidebar_with_agent_panel`（[sidebar_tests.rs:L1751-L1759](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L1751-L1759)）已经把面板加进了工作区，所以该测试中所有激活都走快路径。
3. **需要观察的现象**：快路径里共有两次 `workspace.update`（查面板一次、聚焦一次）。
4. **预期结果**：得到形如 `on_click → activate_terminal_entry(retain=false) → activate_terminal_in_workspace → load_agent_terminal_in_workspace(快) → panel.restore_terminal` 的链路清单。
5. 结论可纯靠阅读得出；若要眼见为凭，可在快路径 `return;` 前临时加一条 `log::info!("fast path")` 再跑 `cargo test -p sidebar --lib test_agent_panel_terminals_appear`——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：慢路径里为什么要写 `workspace.panel::<AgentPanel>(cx).unwrap_or_else(|| workspace.add_panel(panel.clone(), window, cx))`，而不是直接 `add_panel`？

答案：`AgentPanel::load(...).await` 期间用户或其他代码可能已经把一个面板加到了该工作区（比如用户手快打开了代理面板）。直接 `add_panel` 会出现同一工作区两个 AgentPanel；先查再补加保证面板实体唯一，且补加的正是 `load` 装好的那个。

**练习 2**：`restore_terminal` 的第一个分支（`has_terminal` 为真时只激活不重建）为什么重要？

答案：它让「激活」幂等。终端已在面板中时重复点击侧边栏行，只会把面板切回该终端视图，不会销毁并重 spawn 一个同 id 的新终端——终端是有副作用的外部进程，重建意味着丢掉屏幕上的现场。

**练习 3**：慢路径的 `Task` 以 `detach_and_log_err` 收尾而不是被保存或 await，这意味着什么？

答案：意味着装载失败时该任务只记录错误、不影响调用方继续执行；同时任务不会被中途取消（detach 后独立运行到结束）。与 `restoring_tasks`（u6-l1，按 ThreadId 存句柄防重复装载）相比，终端装载没有防重入存储，靠 `restore_terminal` 的幂等分支兜底重复触发。

### 4.3 close_terminal：关闭链路的入口与预计算

#### 4.3.1 概念说明

终端关闭比激活复杂，因为它要回答一个级联问题：**这个终端是不是它所在（linked）工作区的最后一个使用者？** 如果是，关闭终端可能要连带移除工作区、甚至归档磁盘上的 worktree。所以 `close_terminal` 的前半段是纯「预计算」：在动手之前把所有决策依据算好，再交给 `remove_workspaces_then` 统一执行、最后在 finish 回调里落锤。

入口有三个，全部汇入同一个 `close_terminal`：

1. **面板请求**：终端进程自己退出/用户在终端视图里触发关闭 → 面板发出 `AgentPanelEvent::TerminalCloseRequested`（4.3.3 第一段）。
2. **行内悬停按钮**：终端行悬停时右上角出现 ×，点击直接调 `close_terminal`。
3. **键盘 `ArchiveSelectedThread`**：选中的是终端行时，「归档选中条目」动作对终端的语义就是关闭。

#### 4.3.2 核心流程

```
终端视图里触发关闭
  └─ TerminalEvent::CloseTerminal                     （terminal 实体的事件）
       └─ AgentPanel 订阅回调 → request_close_terminal_from_terminal_event
            └─ cx.emit(AgentPanelEvent::TerminalCloseRequested { metadata })
                 └─ Sidebar::subscribe_to_agent_panel 的回调
                      └─ close_terminal(metadata, &Open(workspace), ...)

close_terminal(metadata, workspace, ...)
├─ 门槛：workspace 为 Closed 且需要为归档加载工作区
│    └─ open_workspace_for_archive(..., finish: 用 Open(workspace) 递归调 close_terminal)
├─ 预计算（全部在移除任何东西之前完成）：
│    ├─ is_active        = active_entry 是否正是这个终端
│    ├─ neighbor         = neighboring_activatable_entry(该行下标)
│    ├─ roots_to_archive = roots_to_archive_for_paths(folder_paths, …, except_terminal_id)
│    ├─ workspaces_to_remove ← linked_worktree_workspace_to_remove(...)
│    │     ＋ close_items_for_archived_worktrees(...) 追加/补充
│    └─ terminal_workspace_removed = 该行的 Open 工作区是否在待移除列表里
└─ remove_workspaces_then(workspaces_to_remove, close_item_tasks, …,
     finish: close_terminal_entry(..., activate_panel_draft = !terminal_workspace_removed, ...))
```

注意 `except_terminal_id` 参数：在判断「这个路径还有没有别的终端引用」时，要把自己排除掉——否则永远判定「还有使用者在」，工作区永远无法级联移除。

#### 4.3.3 源码精读

**入口一：面板事件订阅。** `subscribe_to_agent_panel` 里对 `TerminalCloseRequested` 的处理（[sidebar.rs:L1108-L1113](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1108-L1113)）——把发出事件的面板所属工作区包装成 `Open` 再进入关闭链路：

```rust
AgentPanelEvent::TerminalCloseRequested { metadata } => {
    if let Some(workspace) = workspace.upgrade() {
        let workspace = ThreadEntryWorkspace::Open(workspace);
        this.close_terminal(metadata, &workspace, window, cx);
    }
}
```

事件的生产端在面板一侧：终端实体发出 `TerminalEvent::CloseTerminal` 后（[agent_panel.rs:L2200-L2202](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L2200-L2202)），面板**不直接关闭**，而是发出 `TerminalCloseRequested`（事件变体定义见 [agent_panel.rs:L4939](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L4939)）请侧边栏裁决（[agent_panel.rs:L2317-L2325](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L2317-L2325)）。这一「请求-裁决」拆分正是为了让工作区级联清理有决策点。

**入口二：行内悬停 × 按钮**（[sidebar.rs:L6516-L6535](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6516-L6535)）。按钮的 tooltip 借用了 `ArchiveSelectedThread` 动作名（"Close Terminal"），但 `on_click` 直接调 `this.close_terminal(&metadata, &workspace, window, cx)`。

**入口三：键盘归档动作**对终端行的分流（[sidebar.rs:L5662-L5666](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5662-L5666)）——与线程的「删草稿 / 归档」两分支并列，终端直接走 `close_terminal`。

**`close_terminal` 本体**（[sidebar.rs:L4950-L5067](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4950-L5067)）分三段。第一段是 Closed 工作区门槛（L4957-L4986）：`should_load_closed_workspace_for_archive` 判定「这个文件夹确实是个待归档的 linked worktree、且除本终端外无人引用」时，先异步打开工作区，再在回调里用 `Open` 形态**递归调用自己**：

```rust
if let ThreadEntryWorkspace::Closed { folder_paths, project_group_key } = workspace
    && self.should_load_closed_workspace_for_archive(
        folder_paths, project_group_key,
        metadata.remote_connection.as_ref(), None,
        Some(metadata.terminal_id), cx,
    )
{
    let metadata = metadata.clone();
    self.open_workspace_for_archive(
        folder_paths.clone(), project_group_key.clone(), window, cx,
        move |this, workspace, window, cx| {
            this.close_terminal(&metadata, &ThreadEntryWorkspace::Open(workspace), window, cx);
        },
    );
    return;
}
```

第二段是纯预计算（L4988-L5036）：依次算出 `is_active`、`neighbor`（在列表里找到该终端的下标，交给 u5-l2 的 `neighboring_activatable_entry`，见 [sidebar.rs:L4386-L4423](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4386-L4423)）、`roots_to_archive`、`workspaces_to_remove` 与 `terminal_workspace_removed`。`roots_to_archive_for_paths`（[sidebar.rs:L4713-L4749](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4713-L4749)）对每条 folder 路径构建归档计划，并用两层 filter 排除「仍被其他未归档线程或**其他**终端引用」的根（`except_terminal_id` 就是为此传入）。`should_load_closed_workspace_for_archive` 的判定逻辑见 [sidebar.rs:L4635-L4674](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4635-L4674)：文件夹路径与分组主路径相同（不是 linked worktree）直接排除，否则要求路径既无阻断归档的线程、也无其他终端引用。

第三段（L5040-L5066）把一切交给 `remove_workspaces_then`，finish 闭包里做两件事：若终端的工作区已被移除，先删掉该路径下的空草稿；再调 `close_terminal_entry` 落锤，并把 `activate_panel_draft` 设为 `!terminal_workspace_removed`——源码注释点明意图：*工作区已经没了的话，不要在残留的 AgentPanel 里合成兜底草稿*（L5053-L5054）。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：把 `close_terminal` 的预计算清单整理成表，标明每个值在 finish 闭包中的去向。
2. **操作步骤**：通读 [sidebar.rs:L4988-L5066](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4988-L5066)，为 `is_active`、`neighbor`、`terminal_folder_paths`、`roots_to_archive`、`workspace_to_remove`、`close_item_tasks`、`terminal_workspace_removed` 各写一行「来源 → 用途」。
3. **需要观察的现象**：`terminal_folder_paths` 与 `metadata` 在进入异步闭包前都被显式 clone——因为闭包要 `'static`，而 `&mut self` 的借用不能跨 await。
4. **预期结果**：一张 7 行的表，其中 metadata、workspace、is_active、neighbor、roots_to_archive 与标志位经闭包传入 `close_terminal_entry`。
5. 纯阅读即可完成；如需运行验证，参考第 5 节综合实践。

#### 4.3.5 小练习与答案

**练习 1**：面板为什么不自己关闭终端，而要绕一圈发 `TerminalCloseRequested` 给侧边栏？

答案：因为关闭一个终端可能触发工作区移除与 worktree 归档等窗口级联动作，而这些决策信息（列表内容、分组状态、其他线程/终端的引用情况）都在侧边栏与全局元数据存储手里。面板只拥有自己的终端视图，让它直接关闭会绕过级联清理，留下「最后一个使用者已走但工作区还在」的悬空状态。

**练习 2**：`close_terminal` 第一段的递归调用为什么不会无限循环？

答案：递归发生在 `Closed → Open` 的形态转换上：第一段只在 `workspace` 为 `Closed` 且需要加载时触发，回调里传入的是 `open_workspace_for_archive` 拿到的 `Open(workspace)`；第二次进入时 `match` 落在 `Open` 分支，直接进入预计算段，不再满足递归条件。

**练习 3**：`roots_to_archive_for_paths` 的两层 `filter` 分别排除什么？

答案：第一层排除「仍被其他未归档线程引用」的根（经 `path_is_referenced_by_unarchived_threads_for_archive`，其中 `except_thread_id` 为 `None`——线程不豁免）；第二层排除「仍被其他终端引用」的根（`path_is_referenced_by_terminal`，`except_terminal_id` 传本终端 id 豁免自己）。两层都通过的根才会进入归档计划。

### 4.4 close_terminal_entry：落锤——面板关闭、邻居接管与草稿兜底

#### 4.4.1 概念说明

`remove_workspaces_then` 完成移除后（或无需移除时立即）执行 finish 闭包，真正「落下锤子」的是 `close_terminal_entry`。它按固定顺序做四件事：让面板关掉终端视图、删除持久元数据、启动归档后台任务、处理活跃条目的交接。交接有一个关键布尔 `defer_draft_activation`（是否延迟草稿激活），它决定面板用哪个关闭变体。

#### 4.4.2 核心流程

```
close_terminal_entry(metadata, workspace, is_active, neighbor, activate_panel_draft, roots_to_archive, ...)
├─ defer_draft_activation = activate_panel_draft && is_active && neighbor.is_some()
├─ ① workspace 为 Open：
│    ├─ defer || !activate_panel_draft → panel.close_terminal_without_activating_draft(id)
│    └─ 否则                            → panel.close_terminal(id)              # 面板自己激活草稿
├─ ② TerminalThreadMetadataStore.delete(terminal_id)
├─ ③ start_detached_archive_worktree_task(roots_to_archive)                    # 后台归档，detach
├─ ④ is_active 时交接：
│    ├─ active_entry = None
│    ├─ activate_entry(neighbor) 成功 → return（邻居接管，激活逻辑复用 4.1）
│    ├─ defer_draft_activation 且 Open → panel.activate_draft(...)             # 草稿兜底
│    └─ sync_active_entry_from_active_workspace(cx)                            # 对账
└─ ⑤ update_entries(cx)
```

设计要点：**关闭不得抢焦点**。侧边栏行的所属工作区未必是当前活跃工作区，如果关闭动作顺带把面板切到草稿并抢走键盘焦点，用户体验会很突兀——所以有邻居时用 `close_terminal_without_activating_draft`，由侧边栏自己按邻居接管；只有「无邻居可交」且工作区还在时，才让面板落到草稿上。

#### 4.4.3 源码精读

关闭变体的选择（[sidebar.rs:L5081-L5097](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5081-L5097)），源码注释直言动机（*"Closing from the sidebar must not steal focus, since the row's workspace may not be the active workspace."*）：

```rust
let defer_draft_activation = activate_panel_draft && is_active && neighbor.is_some();

if let ThreadEntryWorkspace::Open(workspace) = workspace {
    workspace.update(cx, |workspace, cx| {
        if let Some(panel) = workspace.panel::<AgentPanel>(cx) {
            panel.update(cx, |panel, cx| {
                if defer_draft_activation || !activate_panel_draft {
                    panel.close_terminal_without_activating_draft(terminal_id, window, cx);
                } else {
                    panel.close_terminal(terminal_id, window, cx);
                }
            });
        }
    });
}
```

元数据删除与归档启动（[sidebar.rs:L5098-L5104](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5098-L5104)）：`store.delete(terminal_id, cx)` 会触发元数据存储的 observe → 一次列表重建；`start_detached_archive_worktree_task`（[sidebar.rs:L5548-L5571](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5548-L5571)）先删空草稿，再以带取消通道的独立任务持久化/搬移 worktree 目录，失败仅 `log::error!`。

交接段（[sidebar.rs:L5106-L5125](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5106-L5125)）——三选一的优先级：邻居 > 草稿兜底 > 对账：

```rust
if is_active {
    self.active_entry = None;
    if neighbor.as_ref().is_some_and(|neighbor| self.activate_entry(neighbor, window, cx)) {
        return;
    }
    if defer_draft_activation && let ThreadEntryWorkspace::Open(workspace) = workspace {
        workspace.update(cx, |workspace, cx| {
            if let Some(panel) = workspace.panel::<AgentPanel>(cx) {
                panel.update(cx, |panel, cx| {
                    panel.activate_draft(false, AgentThreadSource::AgentPanel, window, cx);
                });
            }
        });
    }
    self.sync_active_entry_from_active_workspace(cx);
}
self.update_entries(cx);
```

`activate_entry` 的终端分支（[sidebar.rs:L4450-L4462](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4450-L4462)）正是 4.1 的 `activate_terminal_entry(metadata, workspace, /* retain: */ false, ...)`——邻居接管复用普通激活链路，形成闭环。

面板侧两个关闭变体最终都落到 `close_terminal_internal`（[agent_panel.rs:L2266-L2315](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L2266-L2315)）：清除待 spawn 标记、撤通知、从 `terminals` 表移除、删元数据存储条目，若关的是活跃终端则重置 base view，并仅在 `activate_draft_after_close` 为真时激活草稿；最后发 `EntryChanged` 让侧边栏对账：

```rust
if was_active {
    self.base_view = BaseView::Uninitialized;
    self.refresh_base_view_subscriptions(window, cx);
    if activate_draft_after_close {
        self.activate_draft(false, AgentThreadSource::AgentPanel, window, cx);
    }
}
cx.emit(AgentPanelEvent::EntryChanged);
```

一个容易忽略的细节：元数据删除发生了**两次**——面板的 `close_terminal_internal` 删一次（[agent_panel.rs:L2300-L2304](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L2300-L2304)），侧边栏的 `close_terminal_entry` 又删一次。侧边栏那次是为 `Closed` 工作区（本窗口无面板持有它）等场景兜底；对同一 id 重复 delete 是幂等的无害操作。

#### 4.4.4 代码实践

1. **实践目标**：用第 5 节两个测试的场景数据手动演算 `defer_draft_activation`，验证你对三个布尔来源的掌握。
2. **操作步骤**：
   - 场景 A（测试一）：一个工作区、仅一个终端「Dev Server」且为活跃。设 `activate_panel_draft = true`（工作区未被移除）、`is_active = true`、`neighbor = None`，求 `defer_draft_activation`，并推出走哪个面板关闭变体、交接段落入哪一支。
   - 场景 B（测试二）：两个终端「Build」「Server」，关「Server」（活跃），`neighbor = Build`。同样把三个布尔代入求值。
3. **需要观察的现象**：场景 A 应推出 `defer = false` → `panel.close_terminal`（面板自己落草稿）→ 交接段邻居为 None → `sync_active_entry_from_active_workspace`；场景 B 应推出 `defer = true` → `close_terminal_without_activating_draft` → `activate_entry(Build)` 提前 return。
4. **预期结果**：两组推导结论与测试断言一致（测试一断言面板无该终端且列表无该行；测试二断言 `active_terminal_id() == build_terminal_id` 且 `active_entry` 指向 Build）。运行命令见第 5 节，推导本身无需运行。
5. 若推导与实测不符，回到 [sidebar.rs:L5081](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5081) 检查布尔代入是否出错。

#### 4.4.5 小练习与答案

**练习 1**：`close_terminal_entry` 为什么把 `update_entries` 放在最后、而且在 `activate_entry` 成功时干脆 `return` 跳过它？

答案：`activate_entry` 内部最终会经 `activate_terminal_in_workspace` 调 `update_entries`（以及面板装载触发的事件刷新），再执行外层的 `update_entries` 只是重复的全量重建（幂等但浪费）；提前 return 避免一次多余的重建。而普通路径（无邻居交接）仍需显式 `update_entries` 把已删除的行从列表里清掉。

**练习 2**：`defer_draft_activation` 为什么要求 `neighbor.is_some()` 才为真？

答案：有邻居时由侧边栏激活邻居、面板不应抢先落草稿（否则面板先闪一下草稿又被邻居切换覆盖）；若无邻居，`defer` 为假，面板直接落草稿，正好是「无接班人时给用户一个空草稿」的期望行为。若在无邻居时仍用 `close_terminal_without_activating_draft`，面板会停在未初始化的 base view 上，用户看到一块空白面板。

**练习 3**：交接段最后的 `sync_active_entry_from_active_workspace` 在什么情况下真正起作用？

答案：当 `is_active` 为真、无邻居、且 `defer_draft_activation` 为假（例如工作区被移除导致 `activate_panel_draft` 为假）时，`active_entry` 已被清空，需要从当前活跃工作区的面板重新对账出一个活跃条目（可能是草稿线程），让高亮与面板实际展示的内容一致。

### 4.5 remove_workspaces_then：级联移除的异步协调器

#### 4.5.1 概念说明

关闭一个终端可能牵连移除一整个工作区（linked worktree 的最后一个使用者走了），而移除工作区是**需要用户同意的异步过程**（可能有未保存文件的关闭确认），之后还要等「关闭残留编辑器条目」的任务们跑完，才能安全地做后续清理（删空草稿、关终端行）。`remove_workspaces_then` 就是把「移除工作区 + 等待清理任务 + 执行收尾」这三步串成一个可组合的协调器，用 `finish` 回调接收后续动作。它在侧边栏里有三个调用点，全部遵循同一模式——`close_terminal`（[sidebar.rs:L5040](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5040)）与线程归档链路的两处（L5390、L6761）。

#### 4.5.2 核心流程

```
remove_workspaces_then(workspaces_to_remove, close_item_tasks, window, cx, finish)
├─ 两列表皆空 → 立即同步执行 finish（无异步跳变）
├─ 否则：
│    ├─ remove_task = multi_workspace.remove(workspaces, RemovalIntent::KeepProject, ...)
│    └─ cx.spawn_in:
│         ├─ await remove_task；返回 false（用户取消）→ 整条链终止，finish 不执行
│         ├─ 逐个 await close_item_tasks（错误仅 log_err，不中断）
│         └─ update_in → finish(this, window, cx)
```

两个关键设计：

- **`RemovalIntent::KeepProject`**：告诉 MultiWorkspace「移除的是工作区，不是项目」。当该分组再无其他工作区时，MultiWorkspace 事后会把项目的**根** worktree 重新打开（见 `remove` 的文档注释），项目行不会从侧边栏消失。
- **取消即全停**：`remove` 返回 `Ok(false)` 表示用户在关闭确认（`prepare_to_close`）阶段拒绝了，此时连 `finish`（关闭终端行本身）都不执行——终端得以幸存，与用户意图一致。

#### 4.5.3 源码精读

协调器本体（[sidebar.rs:L5128-L5168](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5128-L5168)）——空列表快路径、构造移除任务、异步串联：

```rust
if workspaces_to_remove.is_empty() && close_item_tasks.is_empty() {
    finish(self, window, cx);
    return;
}

let remove_task = if workspaces_to_remove.is_empty() {
    None
} else {
    let Some(multi_workspace) = self.multi_workspace.upgrade() else { return; };
    Some(multi_workspace.update(cx, |multi_workspace, cx| {
        multi_workspace.remove(workspaces_to_remove, RemovalIntent::KeepProject, window, cx)
    }))
};

cx.spawn_in(window, async move |this, cx| {
    if let Some(remove_task) = remove_task
        && !remove_task.await?
    {
        return anyhow::Ok(());   // 用户取消，整条链终止
    }
    for task in close_item_tasks {
        let result: anyhow::Result<()> = task.await;
        result.log_err();
    }
    this.update_in(cx, |this, window, cx| finish(this, window, cx))?;
    anyhow::Ok(())
})
.detach_and_log_err(cx);
```

注意第 5 节的两个测试走的正是**空列表快路径**：单工作区、非 linked worktree 场景下 `roots_to_archive` 与 `workspaces_to_remove` 均为空，`finish` 同步执行——所以测试里一次 `cx.run_until_parked()` 就能跑到断言。

宿主侧 `remove` 的两阶段设计（[multi_workspace.rs:L1755-L1814](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L1755-L1814)）：文档注释写明「consent 阶段只弹窗不改状态，edit 阶段一次同步更新完成删行与替补挑选；`KeepProject` 且组内无其他工作区时事后重开根 worktree」。签名返回 `Task<Result<bool>>`（L1766-L1772），真值表示「确实移除了」：

```rust
// Consent phase: run the standard close lifecycle for every
// workspace being removed. Prompts only; no state changes.
for workspace in &workspaces {
    let should_continue = workspace
        .update_in(cx, |workspace, window, cx| {
            workspace.prepare_to_close(CloseIntent::ReplaceWindow, window, cx)
        })?
        .await?;
    if !should_continue {
        return Ok(false);
    }
}
```

`RemovalIntent` 的两个取值见 [multi_workspace.rs:L290-L294](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L290-L294)；侧边栏关闭/归档链路一律用 `KeepProject`，`CloseProject` 由 `remove_project_group` 等项目级操作使用（[multi_workspace.rs:L972](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L972)）。

`close_item_tasks` 的来源是 `close_items_for_archived_worktrees`（[sidebar.rs:L5170-L5246](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5170-L5246)）：对「部分 worktree 被归档」的混合工作区，逐 pane 关闭引用了被归档 worktree 的编辑器条目（保留工作区本身与其余布局）；若工作区的**全部**可见 worktree 都被归档，则整个工作区进 `workspaces_to_remove`。这段与 u8-l2 的归档流水线重叠，本讲只需记住它的分工。

#### 4.5.4 代码实践（源码阅读型）

1. **实践目标**：验证「用户取消关闭确认 → 终端幸存」这条防御路径。
2. **操作步骤**：
   - 依次阅读 [sidebar.rs:L5152-L5167](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5152-L5167)（`!remove_task.await?` 提前返回）与 [multi_workspace.rs:L1801-L1814](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L1801-L1814)（consent 阶段返回 `Ok(false)`）。
   - 用文字回答：此时面板里的终端还在吗？侧边栏列表里该行还在吗？`close_terminal_entry` 执行过吗？
3. **需要观察的现象**：`finish` 未执行 ⇒ `close_terminal_entry` 未执行 ⇒ 面板未收到关闭指令、元数据未删、列表行仍在——一切都停在预计算之后、落锤之前。
4. **预期结果**：答案是「都在、未执行」。这正是把有副作用的动作全部押后到 `finish` 的收益：任何前置失败或取消都不会留下半完成状态。
5. 本实践为纯阅读结论；构造「带未保存编辑器的 linked worktree 终端」的复现脚本成本较高，标注**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `remove_workspaces_then` 要在 `workspaces_to_remove` 与 `close_item_tasks` 都为空时同步执行 `finish`，而不是统一走异步路径？

答案：统一异步会让最常见场景（普通终端关闭，无任何工作区牵连）平白多一次事件循环跳变，测试也需要多泵几轮才能收敛。同步快路径让两个关闭测试一次 `run_until_parked` 即达断言，也让「无级联」场景的行为更可预测。

**练习 2**：`close_item_tasks` 里的任务失败（`result.log_err()`）为什么只记日志而不中断 `finish`？

答案：这些任务是「关闭引用被归档 worktree 的编辑器条目」，属于清理性质；单个 pane 的条目关闭失败（例如保存失败）不应阻止整个工作区移除与终端行关闭的收尾——那样会留下更不一致的中间态。记日志提供可见性，符合仓库 CLAUDE.md「不要静默丢弃错误」的约定。

**练习 3**：如果侧边栏改用 `RemovalIntent::CloseProject` 会怎样？

答案：分组内无其他工作区时，项目的根 worktree 不会被重开，整个项目行会从侧边栏消失。关闭一个终端连带「删掉整个项目」显然超出用户意图，所以终端/线程关闭链路用 `KeepProject`：移除的是那个 linked worktree 工作区，项目主仓保持在场。

## 5. 综合实践

这是本讲的主实践：**用两个测试还原「面板发出关闭请求 → 侧边栏关闭终端 → 激活相邻条目」的完整事件时序，并写出时序说明。**

### 5.1 运行测试

在 Zed 仓库根目录执行（命令用法见 u1-l2）：

```bash
cargo test -p sidebar --lib test_terminal_close_event_closes_sidebar_terminal
cargo test -p sidebar --lib test_terminal_close_event_activates_neighbor
```

预期两个测试全部通过（**待本地验证**，本讲义未代跑）。

### 5.2 测试做了什么

两个测试共用同一套脚手架（[sidebar_tests.rs:L1720-L1759](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L1720-L1759)）：`init_test_project_with_agent_panel` 初始化全局存储与 FakeFs 假文件系统并造出项目；`setup_sidebar_with_agent_panel` 建侧边栏并给活跃工作区挂上 `AgentPanel::test_new` 的面板（[sidebar_tests.rs:L1740-L1749](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L1740-L1749) 的 `add_agent_panel`）。随后 `insert_test_terminal(title, focus=true, ...)`（[agent_panel.rs:L6667-L6691](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L6667-L6691)）插入并激活一个终端。关键一击是 `emit_test_terminal_close`（[agent_panel.rs:L6800-L6812](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L6800-L6812)）：它对终端实体发出真实的 `TerminalEvent::CloseTerminal`，**从事件链的最早源头**驱动整条关闭链路——这不是直接调侧边栏方法的白盒测试，而是近乎端到端的事件驱动测试。

- 测试一（[sidebar_tests.rs:L2959-L3000](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L2959-L3000)）：单终端场景。断言三件事——面板 `has_terminal` 为假、侧边栏 entries 里再无该终端、`TerminalThreadMetadataStore` 里元数据已删。
- 测试二（[sidebar_tests.rs:L3002-L3040](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L3002-L3040)）：双终端场景，关掉活跃的「Server」。断言 `panel.active_terminal_id() == Some(build_terminal_id)`、`sidebar.active_entry` 指向 Build、可见行只剩 `["v [my-project]", "  Build"]`（`visible_entries_as_strings` 辅助函数见 [sidebar_tests.rs:L547-L602](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L547-L602)）。

### 5.3 任务：写出时序说明

请对照源码填写下表（序号即时序），完成后自查每一步能否在源码里指出对应行：

| # | 时刻 | 组件 | 动作 | 代码位置 |
| --- | --- | --- | --- | --- |
| 1 | 测试触发 | 终端实体 | `cx.emit(TerminalEvent::CloseTerminal)` | [agent_panel.rs:L6809-L6811](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L6809-L6811) |
| 2 | 事件到达 | AgentPanel | 终端事件订阅 → `request_close_terminal_from_terminal_event` | [agent_panel.rs:L2200-L2202](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L2200-L2202) |
| 3 | 请求发出 | AgentPanel | `cx.emit(AgentPanelEvent::TerminalCloseRequested { metadata })` | [agent_panel.rs:L2317-L2325](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/agent_panel.rs#L2317-L2325) |
| 4 | 裁决 | Sidebar | 订阅回调把面板工作区包成 `Open`，调 `close_terminal` | [sidebar.rs:L1108-L1113](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1108-L1113) |
| 5 | 预计算 | Sidebar | 算出 `is_active`、`neighbor`、`roots_to_archive`、`workspaces_to_remove`（两测试中后两者为空） | [sidebar.rs:L4988-L5036](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L4988-L5036) |
| 6 | 协调 | Sidebar | `remove_workspaces_then` 空列表快路径 → 同步执行 finish | [sidebar.rs:L5136-L5139](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5136-L5139) |
| 7 | 面板关闭 | AgentPanel | 测试二：`close_terminal_without_activating_draft`（有邻居）；测试一：`close_terminal`（无邻居，面板落草稿） | [sidebar.rs:L5085-L5097](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5085-L5097) |
| 8 | 元数据 | Sidebar | `store.delete(terminal_id)`（面板侧 `close_terminal_internal` 也删过一次，幂等） | [sidebar.rs:L5098-L5102](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5098-L5102) |
| 9 | 交接 | Sidebar | 测试二：`activate_entry(Build)` → `activate_terminal_entry` → `panel.restore_terminal` 激活 Build 后 return；测试一：无邻居 → 落草稿/对账 | [sidebar.rs:L5106-L5124](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5106-L5124) |
| 10 | 收敛 | 全体 | `EntryChanged` 等事件 + `update_entries` 全量重建 → `run_until_parked` 后测试断言成立 | [sidebar.rs:L5125](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5125) |

写作要求：把表格扩写成一段「以 Server 终端为第一人称」的时序说明（约 200 字），每一步标注源码行号；特别说清第 7 步两个测试为何走了不同分支。

### 5.4 选做：加一条日志验证时序

在 `close_terminal_entry` 的交接段（[sidebar.rs:L5106](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5106)）本地临时插入 `log::info!("close_terminal_entry: is_active={is_active} has_neighbor={}", neighbor.is_some());`，重跑测试二并加 `--nocapture` 观察输出。验证后还原（本讲义禁止改源码，此步仅限你本地实验环境）。**待本地验证**。

## 6. 本讲小结

- 终端激活是 `activate_thread` 的两岔简化版：`activate_terminal_entry` 按 `ThreadEntryWorkspace` 的 `Open`/`Closed` 分流，`Closed` 经 `open_workspace_and_activate_terminal` 异步打开后递归收敛回 `activate_terminal_in_workspace` 的「五件套」（MRU 时间戳、乐观写 `active_entry`、切工作区＋可选 retain、装载面板、重建列表）。
- `load_agent_terminal_in_workspace` 与线程版同构：有面板走同步快路径，无面板 `AgentPanel::load` 异步装载后再恢复；幂等性由面板侧 `restore_terminal` 的 `has_terminal` 分支保证。
- 关闭链路是「请求-裁决」结构：终端实体发 `TerminalEvent::CloseTerminal`，面板转发为 `TerminalCloseRequested`，侧边栏统一在 `close_terminal` 预计算（`is_active`、邻居、归档根、待移除工作区），把所有副作用押后到 `remove_workspaces_then` 的 finish 闭包。
- `close_terminal_entry` 的落锤顺序固定：面板关闭（有邻居用 `close_terminal_without_activating_draft` 防抢焦点）→ 删元数据 → 启动归档后台任务 → 活跃交接（邻居 > 草稿兜底 > 对账）→ 重建列表。
- `remove_workspaces_then` 用 `RemovalIntent::KeepProject` 协调「移除工作区（需用户同意）＋关闭残留条目任务＋收尾回调」；用户取消时整条链（含关闭终端本身）终止；空列表时 finish 同步执行。
- 关闭的级联判定处处带 `except_terminal_id`：判断「路径还有没有使用者」时永远把自己排除在外。

## 7. 下一步学习建议

- 下一讲 u6-l3「新建条目与草稿管理」把视角转向创建侧：`create_new_entry`/`create_new_terminal` 与 `DraftKind` 草稿体系，与本讲的「关闭后落草稿」正好衔接。
- 若想吃透 `roots_to_archive_for_paths`、`close_items_for_archived_worktrees`、`start_detached_archive_worktree_task` 的完整归档流水线，直接预习 u8-l2「工作树归档流水线」，本讲的 4.3/4.5 是它的前置。
- 面板一侧的 `close_terminal_internal`、`restore_terminal`、`activate_draft` 值得在 agent_panel.rs 里完整读一遍——本讲只摘了与侧边栏交互相关的分支。
- 测试方面，`test_agent_panel_terminal_notifications_update_sidebar`（[sidebar_tests.rs:L3042 起](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L3042)）展示了终端通知（Bell）如何经事件更新侧边栏徽标，与本讲的关闭事件链互为对照。
