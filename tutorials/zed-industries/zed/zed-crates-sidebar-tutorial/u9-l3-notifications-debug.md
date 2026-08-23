# 通知、导入横幅与调试工具（u9-l3）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚侧边栏的两套通知集合——`notified_threads`（跨重建继承的「记忆」）与 `notified_terminals`（每次重建现算的「投影」）——各自的产生、清空与收敛时机。
2. 跟踪一条完整的通知消费链：从集合 → 行徽标（`.notified(...)`）→ 折叠分组头圆点 → `WorkspaceSidebar::has_notifications` 契约方法 → 状态栏切换按钮上的小圆点。
3. 理解两条线程导入横幅（外部 Agent 导入、跨通道导入）的显隐条件，以及 `import_banners_use_verbose_labels` 这个 `Option<bool>` 字段如何在两条横幅同时出现时「冻结」按钮文案、避免文字跳动。
4. 区分三种用户提示形态：持久徽标（本讲主线）、一次性 `StatusToast`、常驻导入横幅。
5. 会用 `dev::DumpWorkspaceInfo` 调试动作把多工作区状态倾倒进一个只读缓冲区，并能读懂输出里 `DISAGREES`、`workspace ID mismatch` 这类「烟枪」行，用于排查分组错乱问题。

本讲是单元九的收官，也是整本手册最后一次系统走读 `sidebar.rs`。前面八讲建立的世界观——「侧边栏是纯响应式组件，一切状态从当前世界全量重推导」——在本讲的通知体系里会得到最典型的体现：**通知本质上是一种「记忆字段」，它记录的是两次重建之间的跳变，这是全量重推导架构唯一无法从当前状态算出来的信息。**

## 2. 前置知识

### 2.1 三种用户提示形态

Zed 的侧边栏里有三类容易混淆的「提醒」机制，本讲全部覆盖：

| 形态 | 生命周期 | 数据存放 | 本讲对应 |
| --- | --- | --- | --- |
| **通知徽标**（notification badge） | 持久，直到用户「看到」它 | `SidebarContents.notified_threads` / `notified_terminals` | 4.1、4.2 |
| **状态吐司**（StatusToast） | 一次性，几秒后自动消失或手动关掉 | `notifications` crate 的 Toast 系统 | 4.3 |
| **导入横幅**（onboarding banner） | 常驻，直到用户点 Import 或 × | `Dismissable` 键值持久化 | 4.4 |

直觉上的区别：徽标回答「有什么东西等你回来看」；吐司回答「刚才发生了一件事」；横幅回答「有一个功能建议你试试」。

### 2.2 记忆字段与全量重推导

回顾 u1-l3 与 u3-l2 确立的架构约束：`Sidebar` 结构体上凡是能从当前世界状态推导出来的信息都禁止存字段。唯一豁免的是：

- **记忆字段**：记录「上一刻」的状态（`live_thread_statuses`、`draft_kinds`、`thread_last_accessed`）；
- **易失交互状态**：`selection` 等。

「线程从 Running 变成 Completed」是一个**事件**（跳变），不是当前状态——只看当前世界你不知道它上一刻是不是 Running。所以通知必须靠记忆字段 + 旧快照来检测。这是理解 4.1 全部代码的钥匙。

### 2.3 电平触发与边沿触发

- **电平触发**（level-triggered）：只要条件成立就反复生效。终端通知是这一类——`AgentPanel` 里 `terminal.has_notification` 是一个可随时读取的布尔值。
- **边沿触发**（edge-triggered）：只在「变化的瞬间」生效一次。线程通知是这一类——必须在重建时对比新旧状态才能捕获跳变。

这个区别直接决定了两套集合的不同待遇（继承 vs 现算），见 4.1.2。

### 2.4 动作（Action）与调试工具

`gpui::actions!` 宏定义的带命名空间动作可以绑定键位、也可以在命令面板里按名字触发。`dev` 命名空间是 Zed 的开发者调试动作族，`DumpWorkspaceInfo` 就住在里面。它最终把诊断文本写进一个只读 `Editor` 缓冲区——「用编辑器当日志面板」是 Zed 里常见的调试手法。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [crates/sidebar/src/sidebar.rs](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs) | 主战场：`SidebarContents` 的两个通知集合、跳变检测、徽标渲染、两条导入横幅、`show_thread_title_toast`、`dump_workspace_info` |
| [crates/sidebar/src/sidebar_tests.rs](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs) | 两个通知测试（线程完成触发徽标、终端铃触发徽标）与 `(!)` 标记的出处 |
| [crates/agent_ui/src/agent_panel.rs](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/agent_panel.rs) | 终端通知的**产生端**：`TerminalEvent::Bell` → `mark_terminal_notification`；以及激活终端时的清除分支 |
| [crates/agent_ui/src/thread_import.rs](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_import.rs) | 横幅的持久化（`Dismissable` 键）与「其他发布通道有没有线程」的数据库探测 |
| [crates/workspace/src/multi_workspace.rs](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs) | `Sidebar` trait 契约（`has_notifications` 是 trait 方法）与 `sidebar_has_notifications` 转发 |
| [crates/workspace/src/status_bar.rs](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/status_bar.rs) | 通知的最外层消费方：状态栏侧边栏切换按钮上的小圆点 |
| [crates/zed/src/zed.rs](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/zed/src/zed.rs) | `dump_workspace_info` 动作的注册点 |

## 4. 核心概念与源码讲解

### 4.1 通知集合：notified_threads 与 notified_terminals 的产生与清空

#### 4.1.1 概念说明

`SidebarContents` 是一次重建的完整快照（u2-l1），其中两个字段承载通知：

```rust
#[derive(Default)]
struct SidebarContents {
    entries: Vec<ListEntry>,
    notified_threads: HashSet<agent_ui::ThreadId>,
    notified_terminals: HashSet<TerminalId>,
    project_header_indices: Vec<usize>,
    has_open_projects: bool,
}
```

- [src/sidebar.rs:L475-L482](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L475-L482) —— 两个通知集合与其他快照字段并列声明。

两者待遇截然不同，这是本讲最重要的不对称设计：

| | `notified_threads` | `notified_terminals` |
| --- | --- | --- |
| 信号源 | Running→Completed **跳变**（边沿） | `AgentPanel` 终端上的 `has_notification` 布尔（电平） |
| 跨重建 | **继承**旧快照（`rebuild_contents` 开头从 `previous` 接管） | **不继承**，每次从面板现算重建 |
| 清除时机 | 该线程成为「当前正在看的活跃线程」时移除 | 面板侧激活该终端时清布尔 → 下一轮重建集合自然消失 |
| 为什么 | 跳变是事件，错过就丢了，必须记住 | 布尔随时可查，存了反而会跟面板状态脱节 |

为什么线程通知必须记住？因为「完成」只发生一次。如果重建时才去查状态，线程已经是 Completed，你无从得知它上一刻是否 Running、用户是否错过了那一刻。而终端的铃响状态本来就活在 `AgentPanel` 的 `terminal.has_notification` 字段里，侧边栏每次重建重读一遍即可，记忆反而是负担（面板清了、侧边栏没清，徽标就成了幽灵）。

#### 4.1.2 核心流程

一次 `rebuild_contents` 中，两个集合各自经历一轮处理。用集合语言描述线程通知的完整生命周期：

\[
N_{\text{new}} = \Bigl(\bigl(N_{\text{old}} \cup J\bigr) \setminus R\Bigr) \cap C
\]

其中：

- \( N_{\text{old}} \) —— 旧快照继承来的通知集合；
- \( J \) —— 本轮检测到的跳变集合（上一刻 Running、这一刻 Completed、且用户没在看的线程）；
- \( R \) —— 本轮被「看到」的线程（当前活跃、非后台的线程，看到即清除）；
- \( C \) —— 当轮仍存在的线程 id 集合（`current_thread_ids`，已消失行的通知一并丢弃）。

流程伪代码：

```text
rebuild_contents():
    previous = mem::take(self.contents)
    notified_threads = previous.notified_threads      # ① 继承（唯一从旧快照回收的字段）
    notified_terminals = {}                           # 每次清零，全部现算

    live_notified_terminal_ids = ∪ 各打开工作区 AgentPanel 中 has_notification 的终端 id

    for 每个 项目分组:
        收集终端行（多路查询 + seen_terminal_ids 去重）
            TerminalEntry.has_notification = live_notified_terminal_ids 含该 id
        notified_terminals |= { 有 has_notification 的终端行 }   # ② 终端集合回填

        if 分组展开:
            for 每个线程行:
                if 状态==Completed 且 !is_active_thread 且 old_statuses[session]==Running:
                    notified_threads += thread_id                  # ③ 跳变插入
                if is_active_thread 且 !is_background:
                    notified_threads -= thread_id                  # ④ 看到即清除
        else:  # 分组折叠，线程行不加载，走 live_infos 支线
            通过 old_statuses / 元数据存储反查 thread_id，跳变检测照做     # ③' 折叠也能通知
            if 该分组是活跃分组: notified_threads -= 活跃 thread_id        # ④'

    notified_threads ∩= current_thread_ids            # ⑤ 收敛：消失的行不保留通知
    self.contents = SidebarContents { entries, notified_threads, notified_terminals, ... }
```

终端链路的完整事件流（跨 crate）：

```text
终端进程发出铃声 (BEL)
  → TerminalEvent::Bell                      [agent_panel.rs 的事件订阅]
  → AgentPanel::mark_terminal_notification: terminal.has_notification = true
  → cx.emit(AgentPanelEvent::EntryChanged)   [面板通知外界「条目变了」]
  → Sidebar::subscribe_to_agent_panel 收到 EntryChanged
  → schedule_update_entries(false)
  → rebuild_contents: live_notified_terminal_ids 收集 → TerminalEntry.has_notification
                      → notified_terminals 回填 → 分组头 has_notifications
```

反向（清除）链路：用户激活该终端 → 面板把 `terminal.has_notification` 置回 `false` 并再次发出 `EntryChanged` → 侧边栏重建 → 集合中不再有该 id。

#### 4.1.3 源码精读

**继承点：旧快照里唯一被回收的字段。**

- [src/sidebar.rs:L1356-L1362](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L1356-L1362) —— `mem::take` 取走旧快照后，`notified_threads` 整体接管（继承），`notified_terminals` 则就地新建空集合（不继承）。

**跳变检测（分组展开的正路）：**

```rust
if thread.status == AgentThreadStatus::Completed
    && !is_active_thread
    && session_id
        .as_ref()
        .and_then(|sid| old_statuses.get(sid))
        .is_some_and(|(s, _)| *s == AgentThreadStatus::Running)
{
    notified_threads.insert(thread.metadata.thread_id);
}

if is_active_thread && !thread.is_background {
    notified_threads.remove(&thread.metadata.thread_id);
}
```

- [src/sidebar.rs:L1738-L1750](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L1738-L1750) —— 插入条件是四重合取：现在 Completed、用户没在看（`!is_active_thread`）、有 session_id、且记忆字段 `old_statuses`（即 `live_thread_statuses`，u3-l2 讲过的记忆字段）里记录它上一刻是 Running。第二段是清除：正在看的非后台线程不算通知。注意 `is_active_thread` 的判定要求「活跃条目匹配**且**其所属工作区就是当前活跃工作区」（见上文 1731-1736 行），跨窗口的活跃不算「在看」。

**折叠分组的支线：**

```rust
let thread_id = old_statuses
    .get(&info.session_id)
    .map(|(_, tid)| *tid)
    .or_else(|| {
        ThreadMetadataStore::global(cx)
            .read(cx)
            .entry_by_session(&info.session_id)
            .map(|m| m.thread_id)
    });
```

- [src/sidebar.rs:L1758-L1788](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L1758-L1788) —— 分组折叠时线程行不加载（u3-l4），但 `live_infos` 仍可拿到活跃会话的状态。这里先从 `old_statuses` 反查 thread_id，查不到再兜底查全局元数据存储，然后做同样的 Running→Completed 跳变检测并插入通知。设计意图：**徽标在折叠的分组头上也要能亮**（用户收起了分组，更应该被提醒）。

**终端通知的现算：从面板到行再到集合。**

- [src/sidebar.rs:L1392-L1402](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L1392-L1402) —— 重建开头遍历所有打开工作区的 `AgentPanel`，把 `has_notification` 为真的终端 id 收进 `live_notified_terminal_ids`（这就是「电平信号」的读取点）。
- [src/sidebar.rs:L1458-L1471](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L1458-L1471) —— `make_terminal_entry` 闭包把该布尔烙到每个 `TerminalEntry.has_notification` 字段上（行级数据，u2-l1 讲过它与线程的通知「集合风格」并存）。
- [src/sidebar.rs:L1527-L1536](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L1527-L1536) —— `notified_terminals` 从当次收集的终端行反填——所以它不需要继承，面板就是它的「存储」。

**面板侧：信号的产生与清除（agent_ui crate）。**

- [crates/agent_ui/src/agent_panel.rs:L2196-L2199](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/agent_panel.rs#L2196-L2199) —— 面板订阅终端事件，`TerminalEvent::Bell` 分派给 `mark_terminal_notification`。
- [crates/agent_ui/src/agent_panel.rs:L2607-L2637](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/agent_panel.rs#L2607-L2637) —— 铃声处理：终端当前可见则直接返回（用户在看，无需通知）；否则置 `has_notification = true`，且只在「从无到有」时发出 `AgentPanelEvent::EntryChanged`（避免重复刷）。
- [crates/agent_ui/src/agent_panel.rs:L2884-L2894](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/agent_panel.rs#L2884-L2894) —— 清除分支：终端变成活跃且可见时把布尔置回 `false` 并再次发出 `EntryChanged`，侧边栏下一轮重建自然把徽标摘掉。
- [crates/sidebar/src/sidebar.rs:L1098-L1107](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L1098-L1107) —— 侧边栏一侧：`EntryChanged` 落入 u3-l1 讲过的订阅网络，触发 `schedule_update_entries(false)`。通知的传播完全复用既有的刷新漏斗，没有专线。

**收尾收敛：**

- [src/sidebar.rs:L1956](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L1956) —— `notified_threads.retain(|id| current_thread_ids.contains(id))`：已被归档/删除的线程不再占着通知。折叠分组里 `current_thread_ids` 会被显式扩进分组的存量线程 id（1911-1920 行），正是为了不让 retain 误杀折叠分组的通知。

**测试断言的出口：`(!)` 标记。**

- [crates/sidebar/src/sidebar_tests.rs:L584-L591](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L584-L591) —— `visible_entries_as_strings` 辅助函数在渲染线程行字符串时，若 `is_thread_notified` 为真就追加 ` (!)`。u9-l2 里反复出现的 `(!)` 标记，出处就在这里。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `(!)` 徽标在「后台线程完成」的瞬间出现，理解它由跳变检测驱动而非状态驱动。

**操作步骤**：

1. 在仓库根目录运行线程通知测试：

   ```bash
   cargo test -p sidebar --lib test_background_thread_completion_triggers_notification
   ```

2. 打开 [crates/sidebar/src/sidebar_tests.rs:L4064-L4119](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L4064-L4119) 对照阅读。这个测试的剧本分四幕：
   - **4071-4085 行**：在工作区 A 打开线程、发消息让它进入 Running，并写入元数据存储；
   - **4088-4094 行**：加入并激活第二个工作区 B——从此 A 的线程成为「后台」；
   - **4096-4104 行**：断言此时列表是 `"  Hello * (running)"`，**没有** `(!)`——还在跑，谈不上「完成通知」；
   - **4106-4118 行**：`connection_a.end_turn(...)` 制造 Running→Completed 跳变，断言变成 `"  Hello * (!)"`——徽标亮起。`*` 是 `is_live`，`(!)` 就是 `notified_threads` 里有了这个 thread_id。
3. 再运行终端侧的对称测试：

   ```bash
   cargo test -p sidebar --lib test_agent_panel_terminal_notifications_update_sidebar
   ```

   对照 [crates/sidebar/src/sidebar_tests.rs:L3042-L3092](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3042-L3092)：`insert_test_terminal` 插入两个终端（Build、Server），`emit_test_terminal_bell` 对**非活跃**的 Build 终端模拟铃声（[agent_panel.rs:L6786-L6798](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/agent_panel.rs#L6786-L6798) 里它就是发出一条真实的 `TerminalEvent::Bell`）；断言 `sidebar.has_notifications(cx)`、集合里有 `build_terminal_id`、行上 `has_notification` 为真；随后 `activate_terminal` 激活它，断言全部翻转为假。

**需要观察的现象**：两个测试均通过；线程测试的两段字符串断言分别是「无 `(!)` → 有 `(!)`」；终端测试的三个布尔断言经历「全真 → 全假」。

**预期结果**：你将确认「线程通知 = 跳变检测 + 集合记忆；终端通知 = 面板布尔 + 每轮现算」这条不对称设计。测试的完整输出**待本地验证**（本讲义写作环境未执行 cargo）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `notified_threads` 要从旧快照继承，而 `notified_terminals` 不用？

答案：线程通知的信号源是「Running→Completed」这个一次性跳变（边沿触发）。重建时只看当前状态只能看到 Completed，无法得知上一刻是否 Running，所以必须把「已经通知过谁」记在集合里跨重建传递。终端通知的信号源是面板上随时可读的 `has_notification` 布尔（电平触发），每轮重建重读一遍就是正确答案；若继承旧集合反而会在面板清除后留下幽灵徽标。

**练习 2**：分组折叠后，里面的线程完成了，分组头上会亮通知圆点吗？靠哪段代码？

答案：会。[src/sidebar.rs:L1758-L1788](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L1758-L1788) 的折叠支线通过 `live_infos` + `old_statuses`（兜底查 `ThreadMetadataStore::entry_by_session`）反查 thread_id 后照常做跳变检测并插入 `notified_threads`；分组头的 `has_notifications` 汇总（见 4.2.3）会让折叠头亮起圆点。此外 [src/sidebar.rs:L1911-L1920](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L1911-L1920) 会把分组存量线程 id 扩进 `current_thread_ids`，防止收尾 retain 把折叠分组的通知误杀。

**练习 3**：`is_active_thread` 的判定为什么额外要求「活跃条目的工作区 == 当前活跃工作区」？去掉这个条件会怎样？

答案：见 [src/sidebar.rs:L1731-L1736](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L1731-L1736)。`active_entry` 是全局概念（u2-l3），可能指向另一个窗口正在看的线程；如果只比 thread_id，别的窗口在看这个线程也会被视为「用户在看」而清掉本窗口的徽标。加上工作区相等判定后，只有「本窗口当前工作区正在看它」才算看到。

### 4.2 通知的消费：从行徽标、分组头到状态栏圆点

#### 4.2.1 概念说明

集合本身只是数据，用户看到的是三层 UI：

1. **行徽标**：线程行 / 终端行标题旁的小圆点，由 `ThreadItem::notified(...)` 驱动（u4-l3 讲过 ThreadItem 组件）。
2. **折叠分组头圆点**：分组折叠时行不可见，圆点亮在分组头上；且只在「没有运行中线程、没有等待确认线程」时才显示（运行/等待状态优先占位）。
3. **状态栏切换按钮圆点**：侧边栏整个收起时，状态栏上的侧边栏开关图标仍要提示「里面有东西等你」——这是 `has_notifications` 作为 `WorkspaceSidebar` **trait 契约方法**存在的理由：状态栏在 workspace crate，不能直接摸 sidebar crate 的私有字段，只能走契约。

第三层还牵出一个架构细节：`update_entries` 收尾时的**边沿触发通知**——只有通知有无发生**翻转**时才 `cx.notify()` 宿主 `MultiWorkspace`，避免每轮重建都惊动宿主重渲染（u3-l2 提过，这里看到它的用途）。

#### 4.2.2 核心流程

```text
SidebarContents.notified_threads / notified_terminals
    ├─→ 行渲染: is_thread_notified(id) / TerminalEntry.has_notification
    │       └→ ThreadItem.notified(bool)                     [线程行 sidebar.rs:6111,6178]
    │       └→ ThreadItem.notified(terminal.has_notification) [终端行 sidebar.rs:6503]
    ├─→ 分组头: has_notifications = 组内任一行在集合中       [sidebar.rs:1891 / 1937]
    │       └→ 折叠时且无运行/等待线程 → Accent 圆点          [sidebar.rs:2390-2399]
    └─→ trait 方法 has_notifications(): 任一集合非空          [sidebar.rs:7687-7689]
            └→ update_entries 里翻转时 cx.notify() 宿主        [sidebar.rs:2001,2014-2018]
            └→ MultiWorkspace::sidebar_has_notifications 转发   [multi_workspace.rs:420-424]
                    └→ 状态栏 SidebarStatus.has_notifications   [status_bar.rs:77,92]
                            └→ IconButton.indicator(Indicator::dot()) [status_bar.rs:264-267]
```

#### 4.2.3 源码精读

**行级徽标。**

- [src/sidebar.rs:L6111](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L6111) 与 [src/sidebar.rs:L6178](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L6178) —— `render_thread` 开头用 `is_thread_notified` 现查集合，末尾把结果喂给 `ThreadItem::notified(...)`（u4-l3 讲过 ThreadItem 的两槽结构，徽标渲染在组件内部）。
- [src/sidebar.rs:L6503](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L6503) —— 终端行更直接：行数据自带 `has_notification`，无需查集合。

**分组头汇总（两个分支各一份）。**

```rust
let has_thread_notifications = matched_threads
    .iter()
    .any(|t| notified_threads.contains(&t.metadata.thread_id));
let has_terminal_notifications = matched_terminals
    .iter()
    .any(|t| notified_terminals.contains(&t.metadata.terminal_id));
```

- [src/sidebar.rs:L1876-L1894](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L1876-L1894) —— 搜索过滤分支：只统计**过滤后**留在列表里的行（搜出来的行有通知才亮头）。
- [src/sidebar.rs:L1904-L1940](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L1904-L1940) —— 常规分支：终端走行集合；线程在折叠时行不在 `threads` 里，所以 1911-1928 行专门查元数据存储拿分组存量线程 id 来比对（并顺手把这些 id 补进 `current_thread_ids` 防误杀，呼应练习 2）。

**折叠分组头上的圆点渲染。**

- [src/sidebar.rs:L2390-L2399](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L2390-L2399) —— `render_project_header` 中折叠态的第三个徽标位：`has_notifications && !has_running_threads && waiting_thread_count == 0` 时渲染一个 Accent 色的小圆。优先级明确：运行图标 > 等待警告 > 通知圆点。

**trait 契约与转发。**

- [crates/workspace/src/multi_workspace.rs:L122-L130](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L122-L130) —— `Sidebar` trait（sidebar crate 里以别名 `WorkspaceSidebar` 导入，见 [src/sidebar.rs:L65-L69](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L65-L69)）把 `has_notifications` 定为契约方法——workspace crate 只认接口不认实现。
- [src/sidebar.rs:L7687-L7689](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7687-L7689) —— sidebar 的实现：任一集合非空。
- [src/sidebar.rs:L2001](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L2001) 与 [src/sidebar.rs:L2014-L2018](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L2014-L2018) —— `update_entries` 在重建前快照 `had_notifications`，重建后比较，**翻转才**通知宿主 `MultiWorkspace`（宿主重渲染状态栏），侧边栏自身则每轮 `cx.notify()`。这是典型的边沿触发：宿主只关心「有没有」这个一位信号的变化。
- [crates/workspace/src/multi_workspace.rs:L420-L424](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L420-L424) —— 宿主把 trait 方法包装成 `sidebar_has_notifications` 公开查询。
- [crates/workspace/src/status_bar.rs:L77](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/status_bar.rs#L77) 与 [crates/workspace/src/status_bar.rs:L92](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/status_bar.rs#L92) —— 状态栏在构造 `SidebarStatus` 快照时读入该布尔。
- [crates/workspace/src/status_bar.rs:L264-L267](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/status_bar.rs#L264-L267) —— 最终消费点：侧边栏切换按钮在有通知时挂 `Indicator::dot()`，即使用户收起了整个侧边栏也能看到「里面有东西」。

#### 4.2.4 代码实践

**实践目标**：亲手验证「状态栏圆点由边沿触发驱动」——徽标出现/消失的瞬间宿主才重渲染。

**操作步骤**（源码阅读型实践，配合 4.1.4 的测试）：

1. 在 [src/sidebar.rs:L2014-L2018](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L2014-L2018) 处本地临时加两条日志（本地练习，勿提交）：

   ```rust
   if had_notifications != self.has_notifications(cx) {
       log::info!("sidebar notifications flipped: {had_notifications}");
       // ...原有的 multi_workspace.update(...)
   }
   ```

2. 运行 `cargo test -p sidebar --lib test_agent_panel_terminal_notifications_update_sidebar -- --nocapture`，数一数日志出现的次数。
3. 删除日志，还原源码。

**需要观察的现象**：整个测试里铃响（真→假→真……的翻转链）只触发有限次日志；而大量中间重建（如终端元数据刷新）不产生日志。

**预期结果**：确认宿主只在「有无通知」这一位信号翻转时被唤醒。若日志一条都不出现，说明该测试路径下翻转恰好被合并进同一次重建——这本身也是合并窗口（u3-l2）的证据。具体次数**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：状态栏在 workspace crate，`SidebarContents` 是 sidebar crate 的私有结构。通知信号是怎么跨过 crate 边界的？

答案：通过 trait 契约。workspace 的 `Sidebar` trait 声明了 `has_notifications(&self, cx) -> bool`（[multi_workspace.rs:L125](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L125)），sidebar 的 `impl WorkspaceSidebar for Sidebar` 把它实现为「任一通知集合非空」（[sidebar.rs:L7687-L7689](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7687-L7689)），宿主经对象安全的 `SidebarHandle` 装箱转发后用 `sidebar_has_notifications` 查询（[multi_workspace.rs:L420-L424](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L420-L424)）。依赖方向与 u8-l3 讲的序列化契约一致：契约在 workspace、实现在 sidebar。

**练习 2**：分组头圆点为什么排在运行图标、等待警告之后（2390 行的 `&&` 条件）？

答案：三个徽标争夺同一个尾部槽位。运行中和等待确认是「正在发生、需要用户介入」的强状态，通知只是「已经完成、可以回来看」的弱提示；强状态在时显示强状态，避免用户误以为只是完成通知。`has_notifications && !has_running_threads && waiting_thread_count == 0` 把优先级固化成了一个合取条件。

**练习 3**：如果 `update_entries` 每轮都无条件 `multi_workspace.update(cx, |_, cx| cx.notify())`，会有什么实际问题？

答案：宿主 `MultiWorkspace` 的重渲染会连带重渲染状态栏等挂在它下面的 UI。侧边栏重建很频繁（十六类事件源汇入，u3-l1），无条件通知会把大量无关刷新放大到宿主层级。边沿触发把宿主的刷新次数压到「通知有无翻转」这一位信号的变化次数，是性能上的收敛。注意 `cx.notify()` 本身是幂等的「标脏」操作（一帧内多次调用只渲染一次），所以这里优化的是跨层级放大，不是帧内去重。

### 4.3 StatusToast：一次性的错误提示通道

#### 4.3.1 概念说明

`StatusToast` 来自 `notifications` crate（[src/sidebar.rs:L41](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L41) 导入），是 Zed 通用的轻量提示组件：在工作区底部弹出一条带图标、可手动关闭的消息，几秒后自动消失。它与 4.1 的通知徽标是两个世界：

| | 通知徽标 | StatusToast |
| --- | --- | --- |
| 持续性 | 持久，直到「看到」 | 一次性，超时即走 |
| 状态 | 存进 `SidebarContents` | 不落在 Sidebar 任何字段里 |
| 语义 | 「有东西等你」 | 「刚才出了个状况」 |

sidebar crate 里 `StatusToast` 只有一个使用场景：**线程标题再生成失败时的错误提示**（u5-l4 讲过标题再生命令）。集中在一个辅助函数里，体现了「一处构造、多处复用」。

#### 4.3.2 核心流程

```text
用户触发 RegenerateThreadTitle（或恢复期发现标题缺失）
  → panel.regenerate_thread_title(...) 返回 ThreadTitleRegenerationResult
      ├─ Started / AlreadyGenerating → 正常进行，无 toast
      └─ NoModel → show_no_thread_summary_model_toast
  → 或：LanguageModelRegistry 没有配置 thread_summary_model → 同上
  → show_thread_title_toast:
        StatusToast::new(message) + Warning 图标 + dismiss 按钮
        → workspace.toggle_status_toast(toast) 挂到工作区 toast 栈
```

#### 4.3.3 源码精读

```rust
fn show_thread_title_toast(workspace: Entity<Workspace>, message: &'static str, cx: &mut App) {
    workspace.update(cx, |workspace, cx| {
        let toast = StatusToast::new(message, cx, |this, _cx| {
            this.icon(
                Icon::new(IconName::Warning)
                    .size(IconSize::Small)
                    .color(Color::Warning),
            )
            .dismiss_button(true)
        });
        workspace.toggle_status_toast(toast, cx);
    });
}
```

- [src/sidebar.rs:L3706-L3718](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L3706-L3718) —— 唯一的构造点：`StatusToast::new` 接收消息与一个配置闭包（Warning 图标 + 可关闭），随后交给 `workspace.toggle_status_toast`。toast 的生命周期由 workspace 的 toast 栈管理，`Sidebar` 不留任何引用——这就是「一次性」的代码形态。
- [src/sidebar.rs:L3720-L3726](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L3720-L3726) —— 语义化包装：`show_no_thread_summary_model_toast` 把具体文案（「没有为总结线程标题配置模型」）固定下来。
- [src/sidebar.rs:L3740-L3759](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L3740-L3759) —— 两个触发点：`regenerate_thread_title` 返回 `NoModel`，或全局模型注册表里查不到 `thread_summary_model`。两处都先取 `active_workspace` 再弹 toast（错误反馈到 UI 层，符合 CLAUDE.md 的错误处理约定——不静默丢弃）。
- [crates/workspace/src/workspace.rs:L8114](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/workspace.rs#L8114) —— `Workspace::toggle_status_toast` 的签名（泛型 `ToastView`），toast 的展示/超时/去重逻辑都在 workspace 侧，sidebar 只负责构造。

#### 4.3.4 代码实践

**实践目标**：摸清「什么配置状态下会弹出这个 toast」，并确认它不经过任何通知集合。

**操作步骤**：

1. 确认 sidebar crate 没有为 toast 写过测试：`git grep -n "status_toast\|StatusToast" crates/sidebar/src/sidebar_tests.rs`——应当零命中（本讲义已验证为零命中）。这本身就是信息：toast 属于「无需锁定契约的展示层」。
2. 阅读触发链 [src/sidebar.rs:L3728-L3759](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L3728-L3759)，回答：`ThreadTitleRegenerationResult` 的四个变体里哪几个会走到 toast？（`NoModel` 会；`Started`、`AlreadyGenerating` 提前 return；`NotOpen` 继续走异步兜底路径。）
3. 如需亲眼看到：本地启动 Zed（`cargo run -p zed`），在未配置 thread summary model 的状态下对某线程执行「重新生成标题」命令，观察底部弹出的 Warning toast。

**需要观察的现象**：toast 数秒后自动消失；侧边栏行上**不会**出现 `(!)` 徽标；`has_notifications` 不变。

**预期结果**：验证「toast 完全不经过 `SidebarContents`」——它是旁路的、无记忆的提示。第 3 步现象**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `show_thread_title_toast` 拿的是 `Entity<Workspace>` 而不是 `&mut Context<Sidebar>` 里直接弹？

答案：StatusToast 挂载在**工作区**的 toast 栈上（`workspace.toggle_status_toast`），不是侧边栏的子元素。toast 要在侧边栏收起、切换视图时仍然可见，所以它的宿主必须是工作区。函数签名因此以 `Entity<Workspace>` 为参数、内部 `workspace.update(...)`，两个调用点各自传入 `active_workspace`。

**练习 2**：如果把标题再生失败改成「通知徽标」式的持久提示，用户体验会差在哪里？

答案：徽标语义是「有一条线程等你回来看」，出现在列表行/分组头上；而标题再生失败是**操作反馈**——用户刚发起的命令失败了，提示必须立刻出现在视线焦点附近且不需要去列表里找。一次性 toast 的「出现-消失」节奏正好匹配「我知道了」的交互；持久徽标反而会留下一个永远不被「看到」而清不掉的圆点（除非额外设计清除路径）。

### 4.4 两条导入横幅与 verbose 标签的稳定性

#### 4.4.1 概念说明

侧边栏底部（列表主体与底栏之间，两视图共享，见 u4-l1 的渲染骨架）最多挂两条「导入横幅」：

1. **ACP 导入横幅**：「Looking for threads from external agents?」——检测到用户配置过外部 Agent（如 Claude Agent、Codex 的命令行会话）且未关闭过该提示时显示，按钮打开导入模态（跳到归档视图）。
2. **跨通道导入横幅**：「Threads found from other channels」——探测到**其他 Zed 发布通道**（如 stable/dev/beta 各有独立数据库）里存有线程时显示，按钮执行跨通道导入。

两条横幅共用同一个渲染函数 `render_import_onboarding_banner`，差异只在文案、显隐谓词与点击行为。核心设计点是**按钮文案消歧**：

- 只有一条横幅时，按钮就叫 "Import Threads"（短）；
- 两条同时出现时，两个都叫 "Import Threads" 就分不清了，于是分别改成 "Import Threads from External Agents" / "Import Threads from Other Channels"（长）；
- 难点在于**稳定性**：横幅是动态出现的（跨通道探测是异步的）。如果第一条先渲染成短文案、几秒后第二条出现、第一条按钮当场变成长文案，按钮宽度跳变会很扎眼。解法是把「当前是否用长文案」冻结在一个 `Option<bool>` 字段里——**首次渲染时定档，之后不再改**。

#### 4.4.2 核心流程

```text
构造期 (Sidebar::new):
    channels_with_threads(cx) 异步探测其他发布通道的数据库
        → 结果写入 self.cross_channel_import_channels + cx.notify()

每帧 render():
    show_acp          = 活跃工作区有外部 Agent 且 ACP 横幅未 dismissed
    show_cross        = cross_channel_import_channels 非空 且跨通道横幅未 dismissed
    verbose           = self.import_banners_use_verbose_labels
                            .get_or_insert(show_acp && show_cross)   # 首帧定档，之后冻结
    按 show_acp / show_cross 挂 0~2 条横幅，按钮文案由 verbose 决定

点击 × :
    对应 Dismissable::dismiss(cx)  → 写 KeyValueStore，横幅永久消失
点击 Import:
    ACP  → show_archive + show_thread_import_modal
    跨通道 → 遥测 + dismiss + import_threads_from_other_channels
```

#### 4.4.3 源码精读

**字段与冻结语义。**

```rust
/// For the thread import banners, if there is just one we show "Import
/// Threads" but if we are showing both the external agents and other
/// channels import banners then we change the text to disambiguate the
/// buttons. This field tracks whether we were using verbose labels so they
/// can stay stable after dismissing one of the banners.
import_banners_use_verbose_labels: Option<bool>,
```

- [src/sidebar.rs:L782-L791](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L782-L791) —— 字段声明与完整的设计注释（注释本身就把「为什么」讲透了）。`None` 表示「尚未定档」；构造函数里初始化为 `None`（[src/sidebar.rs:L921](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L921)）。

**render 中的定档点。**

- [src/sidebar.rs:L7887-L7902](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7887-L7902) —— 每帧先算两个显隐布尔，然后 `get_or_insert(show_acp && show_cross)`：`Option<bool>` 为 `None` 时写入当前值并返回引用，之后永远返回首帧的值。这就是「冻结」。注意一个推论：如果首帧只有一条横幅（定档 `false`），之后第二条才异步到来，两条都会显示但**都保持短文案**——牺牲一次消歧机会，换取文案宽度永不跳变。

**两条显隐谓词。**

- [src/sidebar.rs:L7427-L7441](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7427-L7441) —— ACP 谓词：活跃工作区的 `agent_server_store` 报告有外部 Agent，且 `AcpThreadImportOnboarding::dismissed(cx)` 为假。
- [src/sidebar.rs:L7467-L7470](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7467-L7470) —— 跨通道谓词：渠道列表非空且未 dismissed。渠道列表的填充在构造期（[src/sidebar.rs:L867-L876](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L867-L876)）：异步任务完成后写 `cross_channel_import_channels` 并 `cx.notify()`——这正是「横幅会迟到」的根源，也是冻结字段存在的原因。

**两个渲染函数。**

- [src/sidebar.rs:L7443-L7465](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7443-L7465) —— `render_acp_import_onboarding`：`on_import` 监听器先 `show_archive`（切到归档视图，u8-l1）再打开导入模态；按钮文案由 `verbose_labels` 二选一；`on_dismiss` 直接调 `AcpThreadImportOnboarding::dismiss(cx)`。
- [src/sidebar.rs:L7472-L7517](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7472-L7517) —— `render_cross_channel_import_onboarding`：先用 `" and "` 连接渠道名拼出描述文案；`on_import` 里先发遥测事件（含左右侧信息），再 dismiss，最后对活跃工作区执行 `import_threads_from_other_channels`。

**共用横幅骨架。**

- [src/sidebar.rs:L7612-L7675](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7612-L7675) —— `render_import_onboarding_banner`：一个纯函数式组件工厂。顶部渐变背景（accent 色 6% 透明度渐隐）、小号标题 + 关闭按钮、Muted 描述、带下载图标的描边按钮。两条横幅的所有差异都被参数化掉了。

**dismiss 的持久化。**

- [crates/agent_ui/src/thread_import.rs:L35-L64](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_import.rs#L35-L64) —— 两个单元结构体各自实现 `Dismissable`，`KEY` 分别是 `"dismissed-acp-thread-import"` 与 `"dismissed-cross-channel-thread-import"`——底层是 KeyValueStore，跨会话生效。点过一次 ×，横幅永久退场。
- [crates/agent_ui/src/thread_import.rs:L66-L79](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_import.rs#L66-L79) —— `channels_with_threads` 的探测方式：直接打开其他发布通道的数据库文件，`SELECT 1 FROM sidebar_threads LIMIT 1` 查一眼有没有线程。轻量、无需载入整个数据库。

#### 4.4.4 代码实践

**实践目标**：通过思维实验 + 源码验证，彻底吃透 `get_or_insert` 冻结语义。

**操作步骤**：

1. 手动推演四种时序下 `verbose` 的最终值与按钮文案（先写下你的答案）：

   | 时序 | 首帧 show_acp | 首帧 show_cross | 定档值 | 之后的变化 | 按钮文案 |
   | --- | --- | --- | --- | --- | --- |
   | A | true | false | ? | cross 一秒后出现 | ? |
   | B | true | true | ? | 用户关掉 cross | ? |
   | C | false | true | ? | acp 后来出现又消失 | ? |
   | D | false | false | ? | 两条永远没出现 | ? |

2. 对照 [src/sidebar.rs:L7891-L7893](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7891-L7893) 与字段注释（L783-L787）核对答案。
3. 如需动态验证（可选，本地进行）：在 `render` 的 `get_or_insert` 之后临时加 `log::info!("verbose={verbose}")`，运行 `cargo run -p zed`，观察日志只在你首次打开侧边栏那一帧出现一次、此后不再变化。

**需要观察的现象**（第 3 步）：无论后续横幅如何出现/消失，`verbose` 日志值恒定不变。

**预期结果**：时序 A 定档 `false`，两条同显但都叫 "Import Threads"（接受歧义换稳定）；B 定档 `true`，关掉 cross 后剩下的 ACP 横幅**仍叫** "Import Threads from External Agents"（注释里「stay stable after dismissing one of the banners」说的就是这种情况）；C 定档 `false`；D 定档 `false`（横幅永远不出现时冻结值无实际影响）。动态现象**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么不用 `bool` 而用 `Option<bool>` 存这个状态？

答案：`bool` 无法区分「还没决定」和「已决定为 false」。用 `bool` 的话只能初始化成 `false` 或 `true` 之一，等于无条件定档；而横幅显隐是异步已知的（渠道探测、外部 Agent 发现都可能晚到），正确语义是「**第一次真正需要渲染横幅区域时**再看两条是否同时在」。`Option<bool>` + `get_or_insert` 恰好表达「首帧惰性定档、之后只读」。

**练习 2**：两条横幅的 dismiss 状态分别存在哪里？重启 Zed 后还在吗？

答案：都通过 `Dismissable` trait 存进 KeyValueStore，键分别是 `"dismissed-acp-thread-import"` 和 `"dismissed-cross-channel-thread-import"`（[thread_import.rs:L48-L64](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/thread_import.rs#L48-L64)）。持久化跨会话，重启后仍然 dismissed。注意与 4.1 的对照：通知徽标不持久化（会话级记忆），横幅的 dismiss 才持久化。

**练习 3**：跨通道横幅的 `on_import` 里为什么先 `CrossChannelImportOnboarding::dismiss(cx)` 再执行导入？

答案：导入动作一旦发起，引导使命即完成，立即 dismiss 防止导入过程中/完成后横幅仍然挂在底部（导入是异步的，若等它完成再 dismiss，期间用户可能重复点击造成重复导入）。先 dismiss 再导入是「单次触发」的常见守卫模式；ACP 横幅则不同——它的 `on_import` 打开的是模态（可取消），因此不 dismiss，等用户在模态里真正完成操作或关闭模态时再处理。

### 4.5 DumpWorkspaceInfo 调试动作与 dump_workspace_info

#### 4.5.1 概念说明

多工作区分组是侧边栏最容易出「玄学 bug」的区域：一个工作区到底归进哪个分组、linked worktree 有没有被正确识别、面板记的工作区 ID 和工作区自己记的是否一致——这些状态分散在 `MultiWorkspace`、`Workspace`、`AgentPanel` 三处，肉眼排查极难。`dev::DumpWorkspaceInfo` 就是为此准备的诊断动作：把上述状态整体倾倒成一个文本报告，写进一个只读编辑器缓冲区。

它对排查分组问题的价值在于三行「烟枪」输出：

- `ProjectGroupKey (workspace, DISAGREES): ...` —— MultiWorkspace 认为的分组键与 Workspace 自算的分组键不一致（分组错乱的直接证据）；
- `⚠ workspace ID mismatch! panel has ..., workspace has ...` —— AgentPanel 记住的 workspace_id 与工作区当前的 database_id 脱节（线程挂错工作区的证据）；
- worktree 行的 `[branch: ...]` / `[linked worktree -> 主仓路径]` 标注 —— 验证 u2-l2 讲的 linked worktree 分组归并是否生效。

#### 4.5.2 核心流程

```text
用户触发 dev::DumpWorkspaceInfo（命令面板或键位）
  → zed.rs 里 workspace.register_action(sidebar::dump_workspace_info)
  → dump_workspace_info(workspace, _, window, cx):
        1. 取 multi_workspace（没有则退化为单工作区列表）
        2. 输出 MultiWorkspace 头 + 全部 ProjectGroupKey
        3. 逐工作区输出:
             - ProjectGroupKey（并对比 MultiWorkspace 版 vs Workspace 自算版，分歧时打 DISAGREES）
             - dump_single_workspace: DB ID、worktrees（分支/linked 标注）、
               AgentPanel 的 workspace ID 一致性、活跃线程、后台线程
        4. 异步: create_buffer → set_text(output)
             → MultiBuffer::singleton + 只读 Editor → add_item_to_active_pane
```

两个值得注意的工程细节：

- **借位检测**：动作处理器运行在「正在 update 的 `Workspace` 实体」上下文里，对**本实体**再做 `read_with` 会嵌套借用而 panic，所以代码对 `*ws == this_entity` 的情况走直接访问、其他工作区才走 `read_with`——源码里有两段注释专门解释这一点。
- **无 MultiWorkspace 也能用**：`workspace.multi_workspace()` 升级失败时退化为 `[this_entity]` 单工作区报告。

#### 4.5.3 源码精读

**动作定义。**

```rust
gpui::actions!(
    dev,
    [
        /// Dumps multi-workspace state (projects, worktrees, active threads) into a new buffer.
        DumpWorkspaceInfo,
    ]
);
```

- [src/sidebar.rs:L96-L102](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L96-L102) —— `dev` 命名空间下声明（u5-l1 讲过命名空间与键位引用）。doc comment 会展示给用户。

**注册点（zed crate）。**

- [crates/zed/src/zed.rs:L1431](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/zed/src/zed.rs#L1431) —— `workspace.register_action(sidebar::dump_workspace_info);`。注意它**不在** `#[cfg(debug_assertions)]` 里（紧随其后的 `ShowWorkspaceError` 才是），即 release 构建也可用。

**主函数：收集与分歧检测。**

- [src/sidebar.rs:L7965-L7993](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7965-L7993) —— 函数签名是标准的动作处理器形态（`&mut Workspace`、动作引用、`Window`、`Context<Workspace>`）。开头解析 MultiWorkspace 并退化处理（7976-7980），然后输出工作区计数与全部分组键列表（7985-7993）——对照 u2-l2：分组键就是 `ProjectGroupKey`（主 worktree 路径 + 远程主机）。
- [src/sidebar.rs:L8006-L8034](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L8006-L8034) —— 分组键双重计算与分歧报告：对当前实体用 `workspace.project_group_key(cx)`（注释解释了为什么只能对本实体直接调），对其他工作区同时算 `MultiWorkspace::project_group_key_for_workspace`（生效值）与 `Workspace::project_group_key`（自算值），两者不一致时同时打印两行并标注 `DISAGREES`。这就是排查「为什么这两个文件夹分到一组/没分到一组」的第一现场。
- [src/sidebar.rs:L8036-L8045](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L8036-L8045) —— 嵌套借用规避的第二处：本实体直接调 `dump_single_workspace(workspace, ...)`，其他工作区经 `ws.read_with(...)`。

**单工作区报告。**

- [src/sidebar.rs:L8081-L8100](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L8081-L8100) —— `dump_single_workspace` 开头：workspace 的 database_id（没有则 `(none)`）与 worktree 清单的表头。
- [src/sidebar.rs:L8104-L8132](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L8104-L8132) —— 每个 worktree 一行：绝对路径、`(hidden)` 标注、`[branch: ...]`、`[linked worktree -> 主仓路径]`。分支与 linked 判定来自 git 仓库快照（按 `work_directory_abs_path` 前缀匹配）——正是 u2-l2 / u3-l4 里 `branch_by_path` 同源的信息。
- [src/sidebar.rs:L8134-L8144](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L8134-L8144) —— 面板一致性检查：`panel.workspace_id()` 与 `workspace.database_id()` 不等时打印 `⚠ workspace ID mismatch!`（用 Unicode 警告符 `\u{26a0}` 突出）。
- [src/sidebar.rs:L8146-L8170](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L8146-L8170) —— 活跃线程：标题、session_id、状态（idle/generating）、条目数、是否 `awaiting confirmation`（有待确认的工具调用）。
- [src/sidebar.rs:L8172-L8205](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L8172-L8205) —— 后台（retained）线程清单，未连接的会话打印 `(not connected)`。

**输出落盘：编辑器即日志面板。**

- [src/sidebar.rs:L8047-L8078](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L8047-L8078) —— 异步段：`project.create_buffer` 建缓冲区、`set_text(output)` 灌入报告、`MultiBuffer::singleton` 包装（标题 "Workspace Info"）、构造**只读** Editor（`set_read_only(true)`、`set_should_serialize(false)` 不进工作区序列化、面包屑头同名），最后加进活跃窗格。用 `spawn_in` + `detach_and_log_err` 走标准异步错误处理。

#### 4.5.4 代码实践

**实践目标**：亲手产出一份 Workspace Info 报告，并解释它对排查分组问题的价值。

**操作步骤**：

1. **测试路径**（无需 GUI）：侧边栏没有为 dump 写专门测试，但动作是普通函数，可在既有测试脚手架里借位调用。更简单的做法是先跑通环境：

   ```bash
   cargo test -p sidebar --lib test_visible_entries_as_strings
   ```

   确认测试基建可用（u9-l1 的脚手架链）。然后在本地复制一个测试，在 `setup_sidebar` 之后通过 `workspace.dispatch_action(&dev::DumpWorkspaceInfo, window, cx)` 触发（动作处理器是异步落缓冲区的，需要 `run_until_parked` 后从活跃窗格的 Editor 里读文本断言）。此步为进阶练习，**允许迭代**。

2. **GUI 路径**（推荐）：`cargo run -p zed` 启动 Zed，多加几个项目文件夹（含至少一个 git 仓库），打开命令面板搜索 `dev: dump workspace info` 并执行。

**需要观察的现象**：编辑器里出现一份文本报告，结构为：

```text
MultiWorkspace: N workspace(s)
Project group keys (M):
  - ProjectGroupKey { ... }

--- Workspace 0 (active) ---
ProjectGroupKey: ...
Workspace DB ID: ...
Worktrees:
  - /path/to/repo [branch: refs/heads/main]
  - /path/to/repo/.worktrees/feat [linked worktree -> /path/to/repo]
Active thread: ... (session: ...) [idle, 12 entries]
Background threads (1):
  - ...
```

**预期结果**：对照报告写一段诊断说明，回答三个问题：(a) 每个工作区落在哪个 `ProjectGroupKey`，与你在侧边栏看到的分组是否一致；(b) linked worktree 行的 `->` 箭头是否指向主仓（验证 u2-l2 的「linked worktree 与主仓同组」）；(c) 有无 `DISAGREES` 或 `⚠ workspace ID mismatch` 行——出现任何一条都意味着状态分叉，是分组/挂载类 bug 的直接线索。GUI 输出**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `dump_workspace_info` 对当前实体和其他工作区要用两种不同的读取方式？

答案：动作处理器由 `workspace.register_action` 注册，调用时已处于对该 `Workspace` 实体的 `update` 闭包内。gpui 禁止对同一实体嵌套借用（再 `read_with` 本体会 panic），所以代码用 `*ws == this_entity` 区分：本实体直接拿现成的 `&mut Workspace` 引用，其他实体才走 `ws.read_with(...)`。源码 8006-8008 与 8036-8037 行的注释明确记录了这个约束——这是「在实体更新回调里访问实体」的经典陷阱（CLAUDE.md 的 GPUI 章节也有相关约定）。

**练习 2**：报告里 `ProjectGroupKey (multi_workspace)` 与 `ProjectGroupKey (workspace)` 两个值各自代表什么？何时会同时打印？

答案：前者是 `MultiWorkspace::project_group_key_for_workspace(ws, cx)` 算出的**生效分组键**（侧边栏分组实际采用的口径，考虑了分组状态的迁移与合并），后者是该 `Workspace` 自己用 `project_group_key(cx)` 现算的键。两者一致时只打印一行生效值；不一致时两行都打印且第二行带 `DISAGREES` 标注（[src/sidebar.rs:L8014-L8029](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L8014-L8029)）——分歧即 bug 信号，例如工作区移动后自算键已变而宿主登记未跟上。

**练习 3**：这个 dump 输出为什么选择写进只读 Editor，而不是 `log::info!` 或弹一个模态？

答案：报告可能有几十上百行（每个 worktree、每个后台线程一行），需要滚动、搜索、复制——Editor 天然提供这些能力且用户已熟悉；`log::info!` 要去另开日志面板检索，模态则不适合长文本也不便复制。同时 `set_should_serialize(false)` 保证这个临时诊断缓冲区不会污染工作区的序列化状态（下次打开 Zed 不会凭空多出一个 "Workspace Info" 标签页）。这是「用现成组件当工具窗口」的低成本高回报做法。

## 5. 综合实践

**任务**：把本讲四条线索串成一次完整的「通知产生 → 消费 → 诊断」观察。

1. **跑通测试层**（10 分钟）：依次运行

   ```bash
   cargo test -p sidebar --lib test_background_thread_completion_triggers_notification
   cargo test -p sidebar --lib test_agent_panel_terminal_notifications_update_sidebar
   ```

   对照 4.1.4 的两段断言说明，在源码里分别标出「插入通知」「清除通知」「收敛 retain」各自行号。

2. **观察真实 UI**（15 分钟，本地运行 `cargo run -p zed`）：
   - 打开一个含 git 仓库的文件夹，在 Agent 面板里新建一个终端，执行 `printf '\a'` 触发铃声，然后把活跃终端切到另一个——观察侧边栏终端行/分组头上的通知圆点，以及状态栏侧边栏按钮上的小圆点；
   - 把线程侧边栏整个收起，确认状态栏圆点仍在（4.2 的第三层消费）；
   - 点回该终端，确认圆点消失（清除链路走 `EntryChanged` → 重建）。

3. **用调试工具留档**（10 分钟）：执行 `dev: dump workspace info`，把报告保存下来，写一段 100~200 字的诊断说明：你的工作区分组键是什么、linked worktree 是否被正确归并、有无 `DISAGREES` / ID mismatch 行、这份报告若附在分组相关 issue 里能帮维护者省掉哪些猜测。

**验收标准**：你能不看讲义说出——终端铃响后到状态栏圆点亮起之间经过的**每一次事件与重建**；以及 `dump_workspace_info` 报告中至少三行输出的数据来源函数。

## 6. 本讲小结

- 侧边栏有两套不对称的通知集合：`notified_threads` 检测 Running→Completed 的**跳变**（边沿触发），必须从旧快照继承；`notified_terminals` 读取面板的 `has_notification` 布尔（电平触发），每轮重建现算、不继承。收尾统一 `retain` 到仍存在的行。
- 通知的消费分三层：行徽标（`ThreadItem::notified`）、折叠分组头圆点（`has_notifications` 汇总，且排在运行/等待状态之后）、状态栏切换按钮圆点（经 `WorkspaceSidebar::has_notifications` trait 契约跨 crate 传递）；宿主只在通知**有无翻转**时被 `cx.notify()`（边沿触发，避免重建风暴放大到宿主层级）。
- `StatusToast` 是旁路的一次性错误提示（标题再生成失败），挂在 workspace 的 toast 栈上、不落入 `SidebarContents`——与持久徽标是两个语义世界。
- 两条导入横幅（外部 Agent / 跨通道）共用一个渲染骨架，显隐靠 `Dismissable` 键值持久化；按钮文案由 `import_banners_use_verbose_labels: Option<bool>` 在**首帧定档**（`get_or_insert`），之后无论横幅如何增减都保持稳定，避免宽度跳变。
- `dev::DumpWorkspaceInfo` 把多工作区状态倾倒进只读 Editor：`ProjectGroupKey` 双口径对比（`DISAGREES`）、面板 workspace ID 一致性（`⚠ mismatch`）、worktree 分支与 linked 标注，是排查分组错乱的首选工具；实现里对「正在 update 的本实体」与「其他实体」分别用直接访问与 `read_with`，规避嵌套借用 panic。

## 7. 下一步学习建议

本讲是单元九乃至整本 sidebar 手册的收官。三个方向继续深入：

1. **横向对比通知系统**：读 `crates/notifications` crate 的 `StatusToast` 与 Toast 栈实现，再看 `crates/agent_ui` 里 `show_terminal_notification` 弹出的系统级通知（[agent_panel.rs:L2639](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/agent_panel.rs#L2639) 起），梳理 Zed 的三层提醒体系（徽标 / 应用内 toast / OS 通知）各自适用场景。
2. **补一块测试**：模仿 `test_background_thread_completion_triggers_notification`（[sidebar_tests.rs:L4064](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L4064)）与 u9-l2 的方法论，为「跨通道横幅 verbose 冻结」写一个测试（构造两条横幅同时出现的首帧，再 dismiss 一条，断言按钮文案不变）——这是留给你的真实空白点，目前没有测试覆盖。
3. **走向更大的图景**：sidebar crate 已读完，建议下一站读 `crates/agent_ui` 的 `AgentPanel`（侧边栏最主要的数据源与对端）或 `crates/workspace` 的 `MultiWorkspace`（宿主与分组状态持有者），用本手册建立的全量重推导、记忆字段、契约分层三把钥匙去拆它们。
