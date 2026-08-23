# 侧边栏与切换器的集成：MRU、预览与确认

## 1. 本讲目标

上一讲（u7-l1）我们读完了 `thread_switcher.rs` 这个自包含的模态组件：它知道自己如何展示条目、如何循环选中、如何在松开修饰键时确认。但它故意不知道三件事——条目从哪里来、按什么顺序排、确认之后谁来真正激活线程。这三件事全部由 `sidebar.rs` 一侧的集成代码承担。

学完本讲，你应该能够：

1. 跟踪 `ToggleThreadSwitcher` 动作从键位到 `toggle_thread_switcher_impl` 的完整路由，并说出「切换器已打开时再按一次」走的是哪条分支。
2. 读懂 `mru_entries_for_switcher` 如何把侧边栏列表投影成切换器条目，以及 `switcher_entry_cmp` 的三层排序键。
3. 对比 `preview_switcher_selection` 与 `confirm_switcher_selection` 的差异：焦点参数、`retain_active_workspace`、MRU 时间戳写入、模态关闭这四件事各自发生在哪一侧。
4. 解释 `record_thread_access` 与 `record_thread_interacted` 这两个时间戳入口为何必须区分——这是本讲最重要的架构约定。

## 2. 前置知识

本讲默认你已读过以下内容，这里只做一句话回顾：

- **u7-l1（ThreadSwitcher 模态）**：`ThreadSwitcher` 是实现 `ModalView` 的实体，持有 `entries: Vec<ThreadSwitcherEntry>` 与 `selected_index`；它通过 `ThreadSwitcherEvent`（`Preview` / `Confirmed` / `Dismissed`）与宿主通信，`ThreadSwitcherEntry::selection()` 把条目压缩成最小激活载荷 `ThreadSwitcherSelection`。
- **u2-l3（ActiveEntry）**：`active_entry` 是全局高亮的「当前条目」，`selection` 才是键盘焦点下标。本讲的预览/确认都会写 `active_entry`。
- **u2-l2（ThreadEntryWorkspace）**：列表行的工作区分 `Open`（挂 `Entity<Workspace>` 句柄）与 `Closed`（仅带 `folder_paths` 与 `project_group_key` 两种重开材料）两形态。
- **u6-l1（线程激活）**：激活四件套——切工作区、乐观写 `active_entry`、经 `load_agent_thread_in_workspace` 装载面板、`update_entries` 重建列表。
- **MRU（Most Recently Used，最近使用）**：一种排序策略——最近被用户「用过」的条目排在最前。ctrl-tab 风格切换器的经典行为是：第一次按下选中第二新的条目（切换到「上一个」），继续按 tab 在历史里向前走，松开修饰键时提交当前选中项。

一个需要建立的直觉：**切换器里的预览（Preview）是「试穿」，确认（Confirmed）才是「购买」**。预览会把面板临时切到目标线程让你瞄一眼，但不动任何排序时间戳、不关模态；取消（Dismissed）可以把一切复原。只有确认才落锤：写 MRU 时间戳、收紧工作区、关模态。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [crates/sidebar/src/sidebar.rs](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs) | 主战场：`toggle_thread_switcher_impl`、`mru_entries_for_switcher`、`switcher_entry_cmp`、`preview/confirm_switcher_selection`、`record_thread_access` / `record_thread_interacted` 及全部调用点 |
| [crates/sidebar/src/thread_switcher.rs](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/thread_switcher.rs) | 模态一侧的事件定义与初选逻辑（`ThreadSwitcherEvent`、`ThreadSwitcherSelection`、`new`、`cycle_selection`、`confirm`） |
| [crates/sidebar/src/sidebar_tests.rs](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs) | `test_thread_switcher_can_activate_agent_panel_terminal`（确认链路）与 `test_thread_switcher_ordering`（MRU 排序） |
| [crates/workspace/src/multi_workspace.rs](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs) | `Sidebar` / `SidebarHandle` 两个 trait 上的 `toggle_thread_switcher` 通道，以及动作路由 |
| [crates/agent_ui/src/agent_panel.rs](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/agent_panel.rs) | `AgentPanelEvent::ThreadInteracted` 的发射点，用于对照两个时间戳入口 |
| [crates/zed_actions/src/lib.rs](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/zed_actions/src/lib.rs) | `ToggleThreadSwitcher` 动作定义（含 `select_last` 字段） |

## 4. 核心概念与源码讲解

### 4.1 toggle_thread_switcher_impl：打开、循环与现场快照

#### 4.1.1 概念说明

`toggle_thread_switcher_impl` 是切换器在侧边栏一侧的唯一入口。它要同时处理两种调用场景：

1. **切换器未打开**：收集 MRU 条目、创建模态实体、接好订阅、把它挂到 `MultiWorkspace` 的侧边栏 overlay 上。
2. **切换器已打开**：不再重建，只推进选中项（循环或跳末尾）。

第二种场景存在，是因为按住 ctrl 期间每敲一次 tab 都会再次触发同一个动作。注意此时焦点在模态上，动作通常由 `ThreadSwitcher` 自己的 `.on_action(Self::toggle)` 消费（见 4.1.3 末尾）；侧边栏的早退分支覆盖的是「切换器开着但动作仍到达侧边栏」的路径。两条分支最终收敛到模态的同一对方法上。

#### 4.1.2 核心流程

```text
动作 ToggleThreadSwitcher { select_last }
  ├─ 路由 A：侧边栏 render 根容器 .on_action → on_toggle_thread_switcher
  └─ 路由 B：MultiWorkspace .on_action → SidebarHandle::toggle_thread_switcher
              （window.defer 后转回 Sidebar trait 方法）
        ↓
toggle_thread_switcher_impl(select_last)
  ├─ 切换器已开？ → switcher.select_last() 或 cycle_selection()，return
  ├─ entries = mru_entries_for_switcher()；len < 2 → return（没东西可切）
  ├─ 快照现场：original_active_entry / original_metadata / original_workspace
  ├─ cx.new(ThreadSwitcher::new(entries, select_last))
  ├─ 订阅 ThreadSwitcherEvent + DismissEvent（存入 _thread_switcher_subscriptions）
  ├─ mw.set_sidebar_overlay(切换器视图)
  ├─ 重放 initial preview（selected_entry() → preview_switcher_selection）
  └─ window.focus(切换器焦点)
```

#### 4.1.3 源码精读

先看动作路由。动作定义在 zed_actions crate，带 `select_last` 字段：

[crates/zed_actions/src/lib.rs:924-928](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/zed_actions/src/lib.rs#L924-L928) — 定义 `agents_sidebar` 命名空间下的 `ToggleThreadSwitcher` 动作，`select_last` 布尔字段区分「循环一步」与「直接选最后一项」。

侧边栏在自己的 render 根容器上注册处理器：

[crates/sidebar/src/sidebar.rs:7797](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7797) — 侧边栏持有焦点时，动作直接进入 `on_toggle_thread_switcher`。

宿主 `MultiWorkspace` 也注册了一份，经 `SidebarHandle` trait 转发：

[crates/workspace/src/multi_workspace.rs:2097-2103](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L2097-L2103) — `MultiWorkspace` 收到动作后调用 `sidebar.toggle_thread_switcher(action.select_last, ...)`。这条路由让焦点不在侧边栏时（例如在编辑器里）也能唤出切换器。

[crates/workspace/src/multi_workspace.rs:226-233](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L226-L233) — `SidebarHandle for Entity<T>` 的转发实现用 `window.defer` 把更新推迟到当前帧末尾，避免在动作分发栈内再入实体更新。trait 声明在 [multi_workspace.rs:172](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/workspace/src/multi_workspace.rs#L172)，侧边栏侧的实现是薄委托：

[crates/sidebar/src/sidebar.rs:7704-7711](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L7704-L7711) — `WorkspaceSidebar` 契约方法 `toggle_thread_switcher` 只是转调 `toggle_thread_switcher_impl`。

主体函数开头的「已打开」分支与最少条目守卫：

[crates/sidebar/src/sidebar.rs:5956-5976](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5956-L5976) — 若 `self.thread_switcher` 已有实体，按 `select_last` 调 `switcher.select_last(cx)` 或 `cycle_selection(cx)` 后返回；否则收集条目，**不足 2 个直接放弃**——只有 0 或 1 个条目时没有「上一个」可切，弹窗毫无意义。

随后是快照，这段是理解「取消可复原」的钥匙：

[crates/sidebar/src/sidebar.rs:5980-5999](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5980-L5999) — 打开前先记录 `original_active_entry`（当前活跃的线程或终端）、`original_metadata`（若活跃条目是线程，从刚收集的 entries 里按 `thread_id` 找回完整元数据，供取消时重载）与 `original_workspace`（宿主当前工作区）。因为预览会真实地切换工作区和活跃条目，这三份快照就是「撤销日志」。

创建实体与两条订阅：

[crates/sidebar/src/sidebar.rs:6001-6076](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L6001-L6076) — `cx.new` 构造 `ThreadSwitcher`，然后 `subscribe_in` 挂两路订阅：`ThreadSwitcherEvent` 的三分支（Preview → 预览并把焦点抢回模态；Confirmed → 确认；Dismissed → 用快照恢复现场后 `dismiss_thread_switcher`），以及 gpui 通用 `DismissEvent` → 直接关闭。注意订阅被 **存入 `_thread_switcher_subscriptions` 字段而非 detach**——切换器的生命周期短于侧边栏，关闭时要随实体一起清理（见 `dismiss_thread_switcher`，[sidebar.rs:5855-5863](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5855-L5863) 清空实体、订阅与 overlay 三样）。

Dismissed 分支的恢复逻辑值得细看：

[crates/sidebar/src/sidebar.rs:6016-6066](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L6016-L6066) — 先把宿主切回 `original_workspace`；再按快照类型分流：线程则重写 `active_entry`、重建列表、以 `focus = false` 重载原线程；终端则经面板 `activate_terminal(terminal_id, false, ...)` 无焦点恢复；原本就没有活跃条目则什么都不做。**注意取消路径全程不写任何时间戳**——取消意味着这次浏览不算数。

收尾的初选重放是本函数最精妙的一处：

[crates/sidebar/src/sidebar.rs:6078-6100](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L6078-L6100) — `ThreadSwitcher::new` 在构造期内就会 `cx.emit(Preview)`（见 [thread_switcher.rs:215-217](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/thread_switcher.rs#L215-L217)），但那时 `cx.new` 的闭包还没返回、宿主订阅尚未建立，这个事件必然丢失。所以宿主在订阅接好后读 `selected_entry()` **手动重放**一次预览，最后把焦点交给模态。

模态一侧对「已打开再按」的处理在它自己的 render 根容器上：

[crates/sidebar/src/thread_switcher.rs:366-369](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/thread_switcher.rs#L366-L369) — 模态注册了 `confirm`、`cancel`、`toggle` 三个动作处理器。焦点在模态上时，后续的 `ToggleThreadSwitcher` 由这里的 `toggle` 消费（[thread_switcher.rs:312-323](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/thread_switcher.rs#L312-L323) 同样按 `select_last` 分流到 `select_last` / `cycle_selection`），与侧边栏的早退分支行为一致。

#### 4.1.4 代码实践

1. **实践目标**：把 4.1.2 的流程图与真实代码逐行对上，并验证「已打开时再触发」的循环行为。
2. **操作步骤**：
   - 运行 `cargo test -p sidebar --lib test_thread_switcher_can_activate_agent_panel_terminal`，确认通过。
   - 打开 [sidebar_tests.rs:3113-3117](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3113-L3117)，注意测试通过 `sidebar.on_toggle_thread_switcher(...)` 直接调用动作处理器，绕过了键位层——这正是单元测试模拟「按下 ctrl-tab」的方式。
   - 再看 [sidebar_tests.rs:9409-9413](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L9409-L9413)：切换器开着时直接对模态实体调 `switcher.update(|s, cx| s.cycle_selection(cx))`，等价于「按住 ctrl 再敲一次 tab」。
3. **需要观察的现象**：两次测试都应绿；第二次调用没有重建 `thread_switcher` 实体（测试里始终用同一个 `sidebar.thread_switcher.as_ref().unwrap()` 读取）。
4. **预期结果**：绿。若想在真实 UI 里触发，可在 Zed 设置中给 `agents_sidebar::ToggleThreadSwitcher` 绑定键位（仓库默认键位表中未搜到该动作的绑定，需自行添加，**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Dismissed` 分支恢复线程时要用打开前从 entries 里找回的 `original_metadata`，而不是直接用 `self.active_entry` 里记录的 thread_id 去查存储？

**答案**：`active_entry` 只存身份（thread_id / session_id / workspace 句柄），而恢复需要完整元数据（session_id、标题等）交给 `load_agent_thread_in_workspace` 重载面板。切换器打开瞬间的 entries 是现成的完整快照，按 thread_id 一次 `find_map` 即可拿到；另外预览期间 `active_entry` 已经被改写指向预览目标，不能再当「原始状态」用——所以快照必须在打开前、预览发生前采集。

**练习 2**：`toggle_thread_switcher_impl` 为什么要求 `entries.len() >= 2` 才打开？

**答案**：切换器的初始选中下标是 1（第二项，即「上一个」），语义是「切到另一个」。只有 0 或 1 个条目时没有可切换对象，弹出一屏单条目列表只会挡视线。对应的初选逻辑在 [thread_switcher.rs:207-213](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/thread_switcher.rs#L207-L213)：`select_last` 为真选末项，否则 `1.min(len - 1)`——条目不足两个时退化为 0。

**练习 3**：切换器的两条订阅为什么存在 `_thread_switcher_subscriptions` 字段里，而 u1-l3 里看到的一级订阅都是 `detach()` 的？

**答案**：detach 的订阅与接收方（侧边栏）同生共死，适合「宿主活多久听多久」的长期事件源。而切换器是短命模态：每次打开新建实体、关闭即弃。若订阅只挂在侧边栏的生命周期上，下一次打开新实体后，旧实体的订阅还残留着（旧实体已被 `self.thread_switcher = None` 丢弃，但 detach 的订阅仍持有它的强引用，且会把事件投给已经无意义的旧实体）。存字段 + `dismiss_thread_switcher` 里 `clear()`，让订阅严格随模态开关而生灭。

### 4.2 mru_entries_for_switcher 与 switcher_entry_cmp：MRU 收集与排序

#### 4.2.1 概念说明

切换器展示的不是「另一个列表」，而是侧边栏列表 `contents.entries` 的一次**投影 + 重排**：

- **投影**：`ProjectHeader` 分组头被抽掉（但它的标签被缝进每条条目的 `project_name`）；空草稿被丢弃；每条线程/终端被翻译成携带展示所需全量数据的 `ThreadSwitcherEntry`。
- **重排**：侧边栏列表按「显示时间」（`interacted_at` 兜底 `updated_at`）分组内排序；切换器则**打平所有分组**，按 `switcher_entry_cmp` 以 MRU 语义重排。

排序函数 `switcher_entry_cmp` 是三级兜底链：内存里的访问时间戳 → 持久化的 `interacted_at` → 最后的 `updated_at`（终端则是访问时间戳 → `created_at`）。

#### 4.2.2 核心流程

`mru_entries_for_switcher` 的推导：

```text
遍历 contents.entries（保持列表顺序）
  ├─ ProjectHeader → 记下 current_header_label / current_header_key，跳过
  ├─ Thread
  │    ├─ draft == Empty → 跳过（有内容的草稿保留）
  │    ├─ workspace 为 Open → 直接用其句柄
  │    └─ workspace 为 Closed → 用分组键向 MultiWorkspace 查
  │        workspace_for_paths(key.path_list(), key.host())
  │        查不到 → 整条丢弃（? 作用于 Option）
  │    └─ 组装 ThreadSwitcherThreadEntry（标题/图标/状态/元数据/
  │        project_name=当前分组标签/worktrees(清空高亮)/diff_stats/
  │        is_draft/is_title_generating/notified/timestamp 文案）
  └─ Terminal → 组装 ThreadSwitcherTerminalEntry
       （workspace 原样保留 ThreadEntryWorkspace，可以是 Closed）

sort_by(switcher_entry_cmp)   # 稳定排序，平局保持列表顺序
```

`switcher_entry_cmp` 的排序键：

```text
sort_time(Thread)    = thread_last_accessed[id]      # 首选：本次会话内显式访问
                       ?? metadata.interacted_at      # 次选：持久化交互时间
                       ?? metadata.updated_at         # 兜底：最后更新时间
sort_time(Terminal)  = terminal_last_accessed[id]
                       ?? metadata.created_at
比较后 .reverse() → 时间越大越靠前（最新在前）
```

#### 4.2.3 源码精读

收集与投影的主体：

[crates/sidebar/src/sidebar.rs:5766-5848](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5766-L5848) — 用 `filter_map` 遍历 `contents.entries` 做一次线性投影。几个关键点：

- 分组头只更新 `current_header_label` / `current_header_key` 两个游标，自身不产出条目；它的标签变成后续每条条目的 `project_name`，让切换器里仍能看出线程属于哪个项目。
- 线程分支跳过 `DraftKind::Empty` 的空草稿（[sidebar.rs:5780-5782](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5780-L5782)），但**有内容的草稿保留**——对应测试 `test_thread_switcher_includes_parked_draft`（[sidebar_tests.rs:5632](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L5632)）。
- **关闭工作区条目的 workspace 归属**（本讲学习目标之三）集中在 [sidebar.rs:5783-5796](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5783-L5796)：线程条目要求一个**具体的工作区实体**才能进切换器（预览要真切工作区），所以 `Closed` 的行用分组键向宿主现查 `workspace_for_paths(key.path_list(), key.host())`——同一分组下只要有任何一个打开的工作区，关闭行的线程就能搭车；整个分组都没打开的工作区时 `?` 让该线程整条出局。而终端条目（[sidebar.rs:5825-5846](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5825-L5846)）把 `ThreadEntryWorkspace` 原样塞进 `workspace` 字段，**允许 Closed**——因为终端的确认路径 `activate_terminal_entry` 自己会处理 Closed（先开工作区再激活，见 4.3.3）。
- `worktrees` 克隆时把 `highlight_positions` 清成空（[sidebar.rs:5809-5817](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5809-L5817)）：搜索高亮位置来自侧边栏的过滤查询，切换器没有搜索框，带着走只会画出不完整的高亮。
- `timestamp` 是经 `format_history_entry_timestamp` 格式化的**展示文案**，不参与排序——排序用的是下面比较器里的原始 `DateTime`。

排序在收集完成后一次完成：

[crates/sidebar/src/sidebar.rs:5850-5852](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5850-L5852) — `sort_by` 是稳定排序：排序键相同的条目保持侧边栏列表里的相对顺序（分组内显示顺序），这让兜底层的行为也可预测。

比较器本体：

[crates/sidebar/src/sidebar.rs:5742-5764](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5742-L5764) — 文档注释直言「ctrl-tab 切换器使用的排序」。`sort_time` 闭包按条目类型取三级兜底键，最后 `.reverse()` 把「最新在前」表达为降序。**内存时间戳优先于一切持久化时间**——这是 4.4 节整节要展开的纪律。

两个内存 Map 的生命周期管理在重建管线的收尾：

[crates/sidebar/src/sidebar.rs:1956-1961](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L1956-L1961) — 每次 `rebuild_contents` 末尾，`thread_last_accessed` / `terminal_last_accessed` 用 `retain` 裁剪到「仍出现在当前列表里的条目」。行消失了（线程被归档、终端被关闭），对应的访问记忆也随之删除，Map 不会无限膨胀。

#### 4.2.4 代码实践

1. **实践目标**：用 `test_thread_switcher_ordering` 验证三级兜底链与「层级压倒绝对时间」的性质。
2. **操作步骤**：
   - 运行 `cargo test -p sidebar --lib test_thread_switcher_ordering`。
   - 阅读 [sidebar_tests.rs:9439-9471](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L9439-L9471)：测试在三个线程都确认过（都在 `thread_last_accessed` 里）之后，播种一个只有元数据的「历史线程」（`updated_at` = 2024 年 6 月，**晚于**前三个线程的 2024 年 1 月）。
   - 对照断言 `vec![thread_id_b, thread_id_a, thread_id_c, thread_id_hist]`。
3. **需要观察的现象**：历史线程虽然 `updated_at` 最新，却排在三个访问过的线程之后。
4. **预期结果**：绿。原因：三个活跃线程走第一级键（`Utc::now()` 真实时钟，2026 年），历史线程没有第一级键、`interacted_at` 也是 `None`（见播种辅助函数签名 [sidebar_tests.rs:387-395](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L387-L395)，第五个参数 `interacted_at` 传了 `None`），落到第三级 `updated_at`。**层级优先于绝对时间**——第一级的任何一个时间戳都压倒第三级，无论各自多大。

#### 4.2.5 小练习与答案

**练习 1**：线程行进了切换器、终端行也进了切换器，两者对 `Closed` 工作区的处理为何不对称？

**答案**：预览线程必须立刻切工作区、装载会话，需要一个现成的 `Entity<Workspace>`，所以 `Closed` 线程要么在同分组找到打开的工作区搭车、要么出局；而终端条目把 `Closed` 原样带进切换器，因为它的确认路径 `activate_terminal_entry` 内部就有 Open/Closed 分流（Closed 先异步打开工作区再收敛回激活，[sidebar.rs:4466-4489](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4466-L4489)）。代价是 Closed 终端在预览阶段无事发生（见 4.3.3 的 `if let Open`）。相关行为由 `test_thread_switcher_preserves_closed_terminal_linked_worktree_workspace`（[sidebar_tests.rs:3249](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3249)）锁定。

**练习 2**：如果两个线程的第一级键完全相同（同一毫秒内确认），切换器里谁在前？

**答案**：`sort_by` 是稳定排序，平局保持 `mru_entries_for_switcher` 收集时的顺序，即侧边栏列表中的出现顺序（分组头之后、组内显示序）。不会随机抖动。

**练习 3**：`thread_last_accessed` 为什么需要在每次重建时 `retain`，而不是线程归档时手动删？

**答案**：这符合本 crate「全量重推导」的架构约束：凡是可以从当前世界状态推导出的记忆都不单独维护失效逻辑。行是否还在列表里，重建结束时用 `current_thread_ids` 一查便知；让 Map 跟随列表裁剪，就不存在「归档路径忘了删」这类增量协调 bug——归档、关闭、过滤 whichever 路径移除了行，裁剪都自动覆盖。

### 4.3 preview_switcher_selection 与 confirm_switcher_selection：预览与确认的分流

#### 4.3.1 概念说明

切换器把用户意图分成三种事件（u7-l1 已讲模态侧），侧边栏对每种的处理强度递增：

| 维度 | Preview（预览） | Confirmed（确认） | Dismissed（取消） |
| --- | --- | --- | --- |
| 切工作区 | 是 | 是 + `retain_active_workspace` | 切回快照工作区 |
| 写 `active_entry` | 是（乐观） | 是（落锤） | 恢复快照 |
| `load_*_in_workspace` 焦点参数 | `false`（不抢焦点） | `true`（聚焦面板） | `false` |
| 写 MRU 时间戳 | **否** | **是** | 否 |
| 关闭模态 | 否 | 是 | 是 |
| 重建列表 | 是 | 是 | 是 |

核心分野：**预览是可逆的临时展示，确认是带 MRU 副作用的提交**。`retain`（收紧工作区）也只在确认时发生——预览期间切过去的面板是「借看」，确认后才算真正驻留（这与 u6-l2 讲过的切换器循环跳转 retain 语义一致）。

#### 4.3.2 核心流程

```text
Preview(selection)
  ├─ Thread：mw.activate(ws)（不 retain）
  │    乐观写 active_entry → update_entries
  │    load_agent_thread_in_workspace(ws, metadata, focus=false)
  └─ Terminal：仅当 workspace 为 Open 才做对称五件事（focus=false）
  （不写时间戳、不关模态；随后订阅回调把焦点抢回模态）

Confirmed(selection)
  ├─ Thread：mw.activate(ws) + retain_active_workspace
  │    record_thread_access(thread_id)          ← MRU 时间戳
  │    写 active_entry → update_entries
  │    dismiss_thread_switcher()
  │    load_agent_thread_in_workspace(ws, metadata, focus=true)
  └─ Terminal：dismiss_thread_switcher()
       → activate_terminal_entry(metadata, workspace, retain=true)
         （内部含 record_terminal_access 与 Closed 分流）
```

#### 4.3.3 源码精读

预览的线程分支：

[crates/sidebar/src/sidebar.rs:5880-5897](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5880-L5897) — 激活工作区（无 retain）、乐观写 `active_entry`、`update_entries` 刷新高亮，然后 `load_agent_thread_in_workspace(workspace, metadata, false, ...)`——第三个参数 `focus = false`，装载但不夺焦点（该函数签名见 [sidebar.rs:3597-3603](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L3597-L3603)）。

预览的终端分支：

[crates/sidebar/src/sidebar.rs:5898-5915](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5898-L5915) — `if let ThreadEntryWorkspace::Open(workspace)` 一层守卫：Closed 终端在预览阶段直接跳过（没有现成工作区可切），要等确认走完整激活链。

确认的线程分支：

[crates/sidebar/src/sidebar.rs:5925-5945](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5925-L5945) — 与预览的四处差异一目了然：`retain_active_workspace` 收紧工作区；`record_thread_access(&metadata.thread_id)` 写入 MRU 时间戳（本条目唯一写点之一）；`dismiss_thread_switcher(cx)` 关模态清订阅；装载改用 `focus = true` 把焦点送进面板。顺序有讲究：先 dismiss 再装载，避免装载触发的面板事件落在一个半关闭的模态状态上。

确认的终端分支：

[crates/sidebar/src/sidebar.rs:5946-5952](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5946-L5952) — 先 dismiss，再整体委托给 `activate_terminal_entry(metadata, workspace, true, ...)`（retain = true）。终端复用 u6-l2 的激活链，`record_terminal_access` 在链内的 `activate_terminal_in_workspace` 里写入：

[crates/sidebar/src/sidebar.rs:4569-4578](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4569-L4578) — `record_terminal_access(terminal_id)` 与乐观写 `active_entry` 并排出现，随后才是切工作区与装载。对照线程侧的对称实现 [sidebar.rs:3862-3871](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L3862-L3871)（`activate_thread_in_workspace`，`record_thread_access` 在 3870 行）。

最后看订阅回调里 Preview 分支的焦点细节：

[crates/sidebar/src/sidebar.rs:6008-6012](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L6008-L6012) — 预览会激活工作区、装载面板，这些操作可能把焦点抢到面板；处理完预览后立刻 `window.focus(&focus)` 把焦点夺回切换器模态，否则下一次 tab 键就到不了模态的 `toggle` 处理器、松键确认也无从触发。

#### 4.3.4 代码实践

1. **实践目标**：对照 `test_thread_switcher_can_activate_agent_panel_terminal` 梳理一次完整 ctrl-tab 循环（本讲综合实践的前半部分也基于它）。
2. **操作步骤**：
   - 通读 [sidebar_tests.rs:3095-3166](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3095-L3166)。
   - 准备：向面板插入两个测试终端 Build / Server（[3101-3111](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3101-L3111)）。
   - 触发：`on_toggle_thread_switcher` 打开切换器（[3113-3117](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3113-L3117)）。
   - 检查条目与初选：两条终端条目都在，选中项是**第二新**的那条（[3119-3144](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3119-L3144)）。
   - 确认：对模态焦点句柄 `dispatch_action(&menu::Confirm, ...)`（[3146-3153](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3146-L3153)）——这正是「松开修饰键」在模态内部的等价物（`handle_modifiers_changed` 最终 dispatch 的就是 `menu::Confirm`，[thread_switcher.rs:325-342](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/thread_switcher.rs#L325-L342)）。
   - 断言：面板的 `active_terminal_id` 与侧边栏 `active_entry` 都指向刚才选中的终端（[3156-3165](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3156-L3165)）。
3. **需要观察的现象**：Confirm 派发后 `cx.run_until_parked()` 收敛，切换器关闭（`thread_switcher` 变 `None`）、终端成为活跃条目。
4. **预期结果**：测试绿。若失败，优先检查你是否能说出 Confirm 从模态 `confirm_selected`（[thread_switcher.rs:283-288](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/thread_switcher.rs#L283-L288)）发出 `Confirmed` 事件、到侧边栏 `confirm_switcher_selection` 终止的完整链路。

#### 4.3.5 小练习与答案

**练习 1**：为什么确认线程时 `dismiss_thread_switcher` 放在 `load_agent_thread_in_workspace` **之前**，而确认终端时也是先 dismiss 再 `activate_terminal_entry`？

**答案**：确认是终态。先把模态实体、订阅、overlay 三样清掉，模态就彻底退出事件流；随后的装载/激活触发的面板事件（`ActiveViewChanged` 等）只会被侧边栏的一级订阅正常消费，不会再与切换器的 Preview/Dismissed 逻辑交叠。反过来先装载后 dismiss 的话，装载过程中的焦点移动可能触发模态的 `on_focus_out`，额外发一轮 Dismissed，与确认路径的收尾互相踩踏。

**练习 2**：预览写 `active_entry` 但不写 `thread_last_accessed`；确认两者都写。如果预览也写时间戳，会发生什么？

**答案**：按住 ctrl 循环浏览时，每经过一个条目都会把它刷成「最新访问」。MRU 列表在浏览过程中被浏览行为本身重排——下一次 cycle 的起点集合就变了，用户「往回走」的心理模型（越早用过的越靠后）被破坏；而且这些浏览根本不代表使用意图。所以时间戳必须只由「确认」这类显式承诺动作写入。

**练习 3**：`retain_active_workspace` 只在确认时调用，它省略掉会怎样？

**答案**：确认后目标工作区成为活跃工作区但旧的也全部保留。retain 语义（u6-l1/u6-l2 讲过）是切换器式的「瞬时切换」：确认切换意味着用户只想停留在目标工作区，其余临时工作区应收起。预览则明确不 retain——借看一眼后取消还要回到原来的工作区集合。

### 4.4 thread_last_accessed / terminal_last_accessed：时间戳写入纪律与两个入口的区分

#### 4.4.1 概念说明

这是本讲的收束点，也是规格里点名的实践思考题：`record_thread_access` 与 `record_thread_interacted` 为什么是两个入口、两份存储、两种语义？

先看字段声明自带的纪律：

- `thread_last_accessed` / `terminal_last_accessed` 是 `Sidebar` 实体上的内存 `HashMap`，**每个窗口一份、不持久化**。字段 doc comment 明文规定：只在**显式用户动作**（点击线程、在切换器里确认等）时更新，绝不因后台数据变化更新；用途只有一个——给切换器排序。
- `record_thread_interacted` 写的是全局单例 `ThreadMetadataStore` 的 `interacted_at` 字段——**持久化到数据库、跨窗口共享**，由面板的 `ThreadInteracted` 事件驱动（用户在会话视图里发生交互，例如发送消息）。它服务于另一个排序：侧边栏主列表的显示时间（`thread_display_time = interacted_at.unwrap_or(updated_at)`，[sidebar.rs:5703-5705](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5703-L5705)），并在切换器里作第二级兜底键。

两者必须区分的原因可以归纳为三条：

1. **稳定性**：MRU 顺序在切换器打开期间必须冻结。若后台事件（线程完成、元数据刷新）或预览本身能改写排序键，列表会在用户指下重排。
2. **语义不同**：「访问过」（导航意图，窗口内、会话内）≠「交互过」（内容互动，持久化、跨会话）。切换器要前者，显示排序要后者。
3. **隔离副作用**：`interacted_at` 是共享的持久状态，任何写入都会波及主列表排序、时间戳展示，甚至其他窗口；切换器的 MRU 需要一块只属于自己的、廉价的草稿区。

#### 4.4.2 核心流程

两个入口的完整数据流：

```text
入口 A：record_thread_access（内存 MRU）
  显式用户动作 ──┬─ activate_thread_in_workspace（点击行 / 本地激活）
                ├─ activate_thread_in_other_window（跨窗口激活）
                └─ confirm_switcher_selection（切换器确认）
                     ↓
  thread_last_accessed[id] = Utc::now()   （窗口内存）
                     ↓
  switcher_entry_cmp 第一级排序键

入口 B：record_thread_interacted（持久交互时间）
  用户在会话视图交互 → AcpThreadViewEvent::Interacted
    → AgentPanelEvent::ThreadInteracted { thread_id }
    → 侧边栏订阅（sidebar.rs:1114-1117）
                     ↓
  ThreadMetadataStore::update_interacted_at(id, now)   （全局持久）
                     ↓
  主列表显示时间 + 切换器第二级兜底键
```

终端只有入口 A 的对称物 `record_terminal_access`（终端没有「交互」语义，兜底键直接用 `created_at`）。

#### 4.4.3 源码精读

字段声明与纪律注释：

[crates/sidebar/src/sidebar.rs:757-763](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L757-L763) — doc comment 写明「只在点击线程、切换器确认等显式用户动作时更新——绝不因后台数据变化更新，用于给线程切换器弹窗排序」。紧随其后的 `thread_switcher` 与 `_thread_switcher_subscriptions` 字段就是 4.1 节的实体与订阅存放处。

三个写入口函数：

[crates/sidebar/src/sidebar.rs:5688-5701](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5688-L5701) — `record_thread_access` / `record_terminal_access` 各一行 `HashMap::insert`；`record_thread_interacted` 则 update 全局 `ThreadMetadataStore` 的 `update_interacted_at`。两个入口在代码上仅数行之隔，语义却分属两个世界。

`record_thread_access` 的全部调用点（判定「显式动作」边界的依据）：

- [sidebar.rs:3870](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L3870) — `activate_thread_in_workspace`：用户点击列表行或键盘确认线程（u6-l1 的本地激活路径）。
- [sidebar.rs:3921](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L3921) — `activate_thread_in_other_window`：跨窗口激活，注意写的是**目标窗口**侧边栏的 Map（`sidebar.record_thread_access(...)` 在目标实体的 update 闭包里）。
- [sidebar.rs:5936](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5936) — `confirm_switcher_selection`：切换器确认。

`record_terminal_access` 的调用点：

- [sidebar.rs:4574](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4574) — `activate_terminal_in_workspace`：终端激活链的公共必经点，切换器确认（经 `activate_terminal_entry`）与列表点击都汇入这里。

入口 B 的事件源头：

[crates/sidebar/src/sidebar.rs:1114-1117](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L1114-L1117) — 侧边栏对 `AgentPanelEvent::ThreadInteracted` 的订阅：调 `record_thread_interacted` 后调度一次普通刷新（`select_first_after_update = false`，不干扰键盘选中）。

[crates/agent_ui/src/agent_panel.rs:4316-4341](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/agent_panel.rs#L4316-L4341) — 面板订阅线程视图的 `AcpThreadViewEvent::Interacted`，转发为 `AgentPanelEvent::ThreadInteracted`。这一层事件来自用户与会话**内容**的互动（如发送消息），与「导航到某个线程」是两回事——这就是两个入口必须分开的源头事实。

测试如何证明纪律真的被执行：

[crates/sidebar/src/sidebar_tests.rs:9334](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L9334) — `test_thread_switcher_ordering` 在第一次确认之前断言 `thread_last_accessed.is_empty()`。测试用 `open_thread_with_connection`（[agent_ui/src/test_support.rs:225-238](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/agent_ui/src/test_support.rs#L225-L238)）直接操作面板开线程，完全绕过侧边栏激活路径——所以尽管开了三个线程，Map 仍是空的。初始顺序 `[A, B, C]`（[9300-9305](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L9300-L9305)）完全由兜底键（播种的 `updated_at`，1/2/3 日）撑起。顺带一提：测试 9285–9288 行的注释声称「打开每个线程都调用了 record_thread_access」，与 9334 行的断言表面矛盾——**断言才是权威**，注释是遗留的失实描述，读测试时永远以断言为准。

随后确认 C 之后：

[crates/sidebar/src/sidebar_tests.rs:9352-9374](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L9352-L9374) — Map 恰好含一个键（C），切换器重开后的顺序变为 `[C, A, B]`：C 升到第一级键（`Utc::now()`），A、B 仍在兜底层按 `updated_at` 排。再确认 A、B 之后 Map 依次增长到两个、三个键，顺序随之演化为 `[A, C, B]` → `[B, A, C]`（[9384-9437](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L9384-L9437)、[9467-9471](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L9467-L9471)）——同一毫秒精度下后确认的 `now()` 更晚，故 B 居首。这份测试就是「时间戳只由确认写入、层级压倒绝对时间」的完整行为快照。

#### 4.4.4 代码实践（本讲规格指定的实践任务）

1. **实践目标**：用文字精确回答「`record_thread_access` 与 `record_thread_interacted` 为何必须区分」，并为答案找到代码证据。
2. **操作步骤**：
   - 运行 `cargo test -p sidebar --lib test_thread_switcher_ordering` 并通读全测试。
   - 找出 9334 行断言（确认前 Map 为空）与 9352-9364 行断言（确认后恰含被确认者），用一句话解释两者合起来证明了什么。
   - 对照 [sidebar.rs:5688-5701](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5688-L5701)：一个写 `self.thread_last_accessed`（窗口内存），一个写 `ThreadMetadataStore::global`（持久库）。
   - 写下你的答案，需覆盖三条：排序稳定性（预览/后台事件不得扰动 MRU）、语义差异（导航访问 vs 内容交互）、副作用隔离（持久状态共享于主列表与跨窗口，内存 Map 只属于切换器）。
3. **需要观察的现象**：测试绿；确认次数与 Map 大小的增长严格一一对应。
4. **预期结果**：绿。若想进一步验证「后台数据变化不写 MRU」，可在本地给 `record_thread_access` 加一行 `log::info!` 再跑全部切换器测试，观察它只在确认/激活路径被触发（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：`thread_last_accessed` 为什么不做持久化？重启 Zed 后切换器顺序靠什么维持？

**答案**：MRU 的第一级键设计为「本窗口会话内的显式访问」。重启后 Map 为空，`switcher_entry_cmp` 自动落到第二级 `interacted_at`（持久化的交互时间）——那本身就是一份高质量的近似 MRU。省掉一份持久化状态，就省掉一次序列化/恢复的失真与迁移成本；两层兜底天然衔接。

**练习 2**：跨窗口激活时（[sidebar.rs:3921](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L3921)），为什么写的是目标窗口侧边栏的 Map 而不是发起方的？

**答案**：u6-l1 的结论——跨窗口激活只写目标窗口状态，发起方不抢高亮。MRU 同理：线程将在目标窗口被使用，目标窗口下次 ctrl-tab 时它应该排最前；发起方窗口没有「使用」这个线程，不该记这笔账。这也再次说明 Map 的语义是「本窗口的使用历史」。

**练习 3**：假如要给切换器加「按标题搜索」，`mru_entries_for_switcher` 里哪段现有代码提示了高亮位置该怎么处理？

**答案**：[sidebar.rs:5809-5817](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L5809-L5817) 收集时把 `worktrees` 的 `highlight_positions` 清空，说明该字段本就为「带高亮的过滤渲染」预留——搜索应发生在收集之前或收集时改写条目标题数据，并把新查询的高亮位置填进 `ThreadItem` 的 `highlight_positions` 链路（参照 u5-l3 侧边栏搜索的 `fuzzy_match_positions`）。

## 5. 综合实践

**任务：写出一篇「ctrl-tab 的一次完整生命周期」时序说明**，把本讲四个模块串成一条线。建议步骤：

1. 运行两个测试并保持输出在手边：
   - `cargo test -p sidebar --lib test_thread_switcher_can_activate_agent_panel_terminal`
   - `cargo test -p sidebar --lib test_thread_switcher_ordering`
2. 以第一测试为脚本，按顺序写出每一步「谁、调用了什么、改变了什么状态」：
   - 按键 → `ToggleThreadSwitcher` 动作（`select_last = false`）经路由 A 或 B 到达 `toggle_thread_switcher_impl`；
   - `mru_entries_for_switcher` 投影 + `switcher_entry_cmp` 排序（此时 `thread_last_accessed` 可能全空，顺序由兜底键撑起——引用 9334 行断言作证据）；
   - `ThreadSwitcher::new` 初选下标 1、构造期 Preview 丢失、宿主经 `selected_entry()` 重放 → `preview_switcher_selection`（`focus = false`、不写时间戳、焦点抢回模态）；
   - 再按 tab → 模态自身 `toggle` → `cycle_selection` → 新一轮 Preview；
   - 松开修饰键 → `handle_modifiers_changed` dispatch `menu::Confirm` → `Confirmed` 事件 → `confirm_switcher_selection`（`record_thread_access` / `record_terminal_access`、`retain_active_workspace`、dismiss、`focus = true` 装载）；
   - 收尾断言：面板活跃终端与 `active_entry` 一致。
3. 在时序图的旁边补一段「若中途按 Esc」：`Dismissed` 分支如何用打开前的三份快照把世界复原，以及为什么复原全程不写任何时间戳。
4. 最后回答规格的思考题（可直接引用 4.4.4 你已写好的答案）：`record_thread_access` 与 `record_thread_interacted` 为何必须区分。

产出物是文字 + 简单时序图，不需要改任何代码。

## 6. 本讲小结

- `toggle_thread_switcher_impl` 是切换器的宿主侧入口：已打开则循环选中，未打开则收集条目（不足 2 个放弃）、快照现场、创建模态、接订阅、挂 overlay，并**重放构造期丢失的初次 Preview**。
- `mru_entries_for_switcher` 把 `contents.entries` 投影成切换器条目：分组头化为 `project_name` 游标、空草稿出局、线程的 Closed 工作区须在同分组找到打开工作区搭车，终端则允许携带 Closed 进入（确认路径自会处理）。
- `switcher_entry_cmp` 是三级兜底的 MRU 排序：访问时间戳 → `interacted_at` → `updated_at`（终端为访问时间戳 → `created_at`），降序排列，层级优先于绝对时间。
- Preview 与 Confirmed 的分水岭是四处差异：焦点参数（false/true）、`retain_active_workspace`（否/是）、MRU 时间戳（不写/写）、模态关闭（否/是）；Dismissed 用打开前的快照整体复原且零时间戳副作用。
- `record_thread_access` 写窗口内存 Map、只由显式用户动作触发、仅供切换器排序；`record_thread_interacted` 写全局持久 Store、由内容交互事件驱动、服务主列表显示序并作切换器兜底——两者混用会破坏 MRU 在浏览期间的稳定性。
- 订阅存在 `_thread_switcher_subscriptions` 字段并随 dismiss 清空，让订阅生命周期严格等于模态开关周期，这是与一级订阅 detach 策略的关键差异。

## 7. 下一步学习建议

- 下一讲 u8-l1 进入**归档与持久化**单元：`SidebarView` 的 ThreadList/Archive 切换与 `ThreadsArchiveView` 的嵌入方式，其中归档视图同样以子实体 + overlay 协作，可对照本讲的 `set_sidebar_overlay` 机制。
- 若想继续在切换器主题里深挖，通读 [sidebar_tests.rs:3249](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3249) 的 `test_thread_switcher_preserves_closed_terminal_linked_worktree_workspace`，验证你对「关闭工作区终端条目归属」的理解。
- 建议回读 [sidebar.rs:1974](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L1974) 起的 `schedule_update_entries`，思考本讲大量出现的 `update_entries` 直调（而非调度）为什么在确认/预览路径是安全的——答案是这些路径本身已在事件分发的同步栈内，且需要立即反映高亮变化。
