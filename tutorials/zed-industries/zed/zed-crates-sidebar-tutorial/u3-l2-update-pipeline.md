# 重建管线：schedule_update_entries → update_entries

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `update_task` 的**合并策略**：它为什么不是一个定时器去抖，同一批到达的多次刷新请求如何被合并成一次 `update_entries`，以及 `select_first_after_update` 为什么必须「替换」而不是「等待」。
2. 按顺序说出 `update_entries` 的**五个步骤**（重建、草稿时间刷新、草稿订阅重连、差异应用、默认分支预取），并说明每一步的输入为什么依赖上一步的输出。
3. 理解「每次全量重推导」这条架构约束的含义：`Sidebar` 结构体上的 doc comment 为什么明确禁止「增量协调状态」，哪些字段是被豁免的「记忆字段」。
4. 说清 `cx.notify()` 在管线末尾的作用，以及为什么通知状态变化时还要额外通知宿主 `MultiWorkspace`。

本讲是 u3-l1 的下游：u3-l1 讲清了「十六类事件源汇入同一条漏斗」，本讲拆开这条漏斗本身——事件到达漏斗之后，一次刷新是如何被调度、合并、执行的。

## 2. 前置知识

阅读本讲前，你需要理解以下 gpui 概念（u1-l2、u1-l3 已铺垫，这里按本讲用法再确认一遍）：

- **`Task` 与 drop 即取消**：`cx.spawn(async move |this, cx| ...)` 返回一个 `Task<R>`。它是惰性持有的一等公民：**如果不把返回值存起来，Task 立刻被 drop，其中的工作直接被取消**——异步闭包根本不会执行。所以 `update_task: Option<Task<()>>` 这个字段的存在本身就是管线能工作的前提（见 4.1.3）。
- **前台 executor 的泵送时机**：`cx.spawn` 的闭包在 gpui 前台 executor 上排队，**当前这轮事件派发把控制权交还之后**才会被执行。这正是「合并窗口」的物理基础：在 executor 泵走任务之前到达的所有刷新请求，看到的都是同一个 `Some(task)`。
- **`cx.notify()` 的语义**：调用它只是把当前实体标记为「脏」，加入下一帧的重绘集合；`render()` 要等到 gpui 绘制下一帧时才真正执行。**状态更新与渲染调度是分离的**——本讲末尾的练习会用到这个区别。
- **弱引用更新**：`cx.spawn` 闭包里的 `this` 是 `WeakEntity<Sidebar>`，`this.update(cx, ...)` 返回 `anyhow::Result`——若窗口已关闭、实体已释放，更新失败并以 `.ok()` 吞掉结果，不会 panic。
- **事件漏斗（u3-l1 已建立）**：`Sidebar::new` 的一级订阅（`MultiWorkspaceEvent`、两个元数据 Store 的 observe、过滤编辑器）与二级订阅（`subscribe_to_workspace`、`subscribe_to_agent_panel`、草稿编辑器观察）最终全都调用同一个入口 `schedule_update_entries`。

还需要两个背景事实（u2-l1 已建立）：

- `SidebarContents` 是一次重建的完整快照，其中 `entries` 是可见行的全量列表；`rebuild_contents` 用 `mem::take` 把它整体换掉。
- 列表渲染使用 gpui 的虚拟列表 `ListState`，每个可见行都有「测量值」（高度等）；`apply_list_state_diff` 负责在重建后只拼接变化区间，避免粘性头部闪烁（算法细节留给 u3-l3，本讲只关心它在管线中的位置）。

## 3. 本讲源码地图

| 文件 | 本讲涉及内容 |
| --- | --- |
| [crates/sidebar/src/sidebar.rs](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs) | 本讲主战场：结构体 doc comment、`update_task` 字段、`schedule_update_entries`、`update_entries`、`select_first_entry`、`refresh_refilled_draft_times`、`prefetch_worktree_default_branches`、`has_notifications`、`rebuild_contents` 的首尾 |
| [crates/sidebar/src/sidebar_tests.rs](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar_tests.rs) | 本讲代码实践依据：`type_in_search` 辅助函数与 `test_search_narrows_visible_threads_to_matches` |
| [crates/workspace/src/multi_workspace.rs](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/workspace/src/multi_workspace.rs) | `multi_workspace_enabled` 守卫、`sidebar_has_notifications`（宿主如何消费侧边栏通知状态） |

## 4. 核心概念与源码讲解

### 4.1 调度入口与合并窗口：schedule_update_entries 与 update_task

#### 4.1.1 概念说明

侧边栏有十六类事件源（u3-l1），但一次用户操作往往同时触发好几类：比如一次工作区路径变更会带来 `ProjectEvent::WorktreePathsChanged`、元数据存储改键后的 observe 通知等多个回调。如果每个回调都直接执行一次全量重建，同一轮里就要把所有工作区的所有元数据查好几遍——浪费且毫无必要，因为重建的输入（当前世界状态）在这一轮内根本没变多少。

`schedule_update_entries` 就是解决这个问题的调度入口。它的策略可以概括为一句话：

> **请求可以有很多次，执行永远合并成一批一次。**

它不是基于定时器的去抖（没有任何 `Timer`、没有任何毫秒数），而是基于 gpui executor 的天然批处理：spawn 出的任务在 executor 泵送之前不会执行，这期间到达的所有重复请求都被一个 `Option<Task<()>>` 字段挡住。去抖窗口不是固定时长，而是「到前台 executor 下一次泵送为止」——在真实应用里约等于「一帧之内合并」。

另一个关键设计是 `select_first_after_update` 参数。整个 crate 只有过滤编辑器的订阅会传 `true`（用户输入了非空搜索词时）：语义是「这次重建完成后，把键盘选中态放到第一条匹配结果上」。

#### 4.1.2 核心流程

```text
事件回调到达
    │
    ├─ schedule_update_entries(select_first_after_update, cx)
    │
    ├─ 已有 pending 任务 且 本次不需要 select_first？
    │       └─ 是 → 直接返回（请求被合并进 pending 那次）
    │
    └─ 否则 → cx.spawn 一个新任务，存入 update_task：
            （若原来有 pending 任务，赋值时旧 Task 被 drop → 取消）
            任务体（executor 泵送时执行）：
                this.update:
                    update_task = None     ← 先清标志，再重建（见 4.1.3）
                    update_entries(cx)     ← 五步重建（4.2）
                    若 select_first_after_update：
                        select_first_entry()
                        cx.notify()
```

#### 4.1.3 源码精读

先看字段与它守护的函数。[crates/sidebar/src/sidebar.rs:L782-L782](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L782-L782) 定义了 `update_task: Option<Task<()>>`：它同时承担两个职责——用 `is_some()` 判断「是否已有 pending 刷新」，以及在被替换时通过 drop 取消旧任务。注意如果把 `cx.spawn` 的返回值丢弃，任务会立即被取消，重建永远不会发生，所以这个字段不是可有可无的记账，而是管线的发动机。

再看入口本体：

[crates/sidebar/src/sidebar.rs:L1974-L1990](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1974-L1990) 是 `schedule_update_entries` 的全部 17 行：第 1975-1977 行是合并判据——`if self.update_task.is_some() && !select_first_after_update { return; }`。注意条件里的 `&& !select_first_after_update`：普通请求（`false`）遇到 pending 任务就把自己合并掉；但带 `select_first` 的请求**不走早退**，而是继续往下，用新任务**替换**旧任务。替换时旧 Task 被 drop——由于它还没被 executor 泵走（若已泵走，任务体第一行已把 `update_task` 置回 `None`，这里就不会是 `Some` 了），取消是干净且无副作用的。

[crates/sidebar/src/sidebar.rs:L1979-L1989](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1979-L1989) 是任务体：`this.update(cx, ...).ok()` 里的 `this` 是 `WeakEntity<Sidebar>`，窗口关闭后更新失败、静默返回。任务体内部有一个**顺序上极其讲究**的细节——第 1981 行先把 `this.update_task = None`，第 1982 行才调 `this.update_entries(cx)`。为什么必须先清标志？因为 `update_entries` 的第二步（`refresh_refilled_draft_times`）会**写回** `ThreadMetadataStore`，而这个 Store 的 observe 又会**重入** `schedule_update_entries`（见 4.2.3）。如果先重建再清标志，这次重入会被早退条件吞掉，后续刷新就丢了；先清标志，重入就能排上一个新任务，在下一轮泵送时再收敛一次。

谁在传 `true`？全 crate 唯一一处在过滤编辑器的订阅里：

[crates/sidebar/src/sidebar.rs:L834-L843](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L834-L843) 是 `Sidebar::new` 里对 `filter_editor` 的订阅：`BufferEdited` 事件发生时，若查询非空，先 `this.selection.take()` 立即清掉旧的键盘选中（避免旧高亮在结果收窄后悬空），再 `schedule_update_entries(!query.is_empty(), cx)`——非空查询即 `select_first = true`。也就是说，「输入搜索词后第一条匹配自动被选中」这个体验，就是由这个参数一路带进任务体的。

任务体里的 `select_first_entry`（[crates/sidebar/src/sidebar.rs:L2149-L2162](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2149-L2162)）并不简单取下标 0：它先用 `position` 找**第一个线程或终端行**（跳过项目分组头），找不到任何线程/终端时才退回到下标 0（列表非空但只有分组头的情况）。这保证了搜索后 Enter 直接命中一条结果而不是分组头。

最后补一句：`update_entries` 还有约 20 处**直接调用**（不经调度），例如折叠分组时：

[crates/sidebar/src/sidebar.rs:L3229-L3238](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L3229-L3238) 的 `toggle_collapse` 在改完折叠状态后直接 `self.update_entries(cx)`。规律是：**事件订阅走调度（合并有价值），用户同步交互直接调（当轮就需要新列表，且天然不会成批到达）**。

#### 4.1.4 代码实践

**实践目标**：用肉眼验证「合并」与「替换」两条规则。

1. 打开 `crates/sidebar/src/sidebar.rs`，全文搜索 `schedule_update_entries(`，统计调用点（Grep 结果应为 15 处调用 + 1 处定义）。
2. 逐一标注每处的第二个实参：你应该发现 14 处是字面量 `false`，只有 [sidebar.rs:L840](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L840) 一处传的是表达式 `!query.is_empty()`。
3. 在纸上回答：如果把 L1975 的条件改成 `if self.update_task.is_some() { return; }`（去掉 `&& !select_first_after_update`），哪个用户体验会坏掉？

**需要观察的现象**：`select_first_after_update=true` 的请求到达时若已有 pending 任务，新请求不会被丢弃，而是替换。

**预期结果**：改掉条件后，「已有 pending 刷新时用户输入搜索词」的场景里 `select_first_entry` 不会被执行，搜索结果的第一条不再自动选中。（本练习为源码阅读型推演，结论待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `update_task` 存 `Option<Task<()>>`，而不是一个简单的 `bool is_update_scheduled` 标志？

**答案**：两个原因。其一，`cx.spawn` 返回的 `Task` 若不被持有会立即被 drop、任务被取消，所以必须有人持有它——字段是 `Task` 的必然归宿，`Option` 顺带免费提供了 `is_some()` 的判空能力。其二，替换语义依赖 drop 即取消：带 `select_first` 的请求到来时，直接赋新值就能干净地取消旧任务；`bool` 标志无法表达「取消一个已排队但未执行的工作」。

**练习 2**：同一轮事件派发中，先后来了一次 `schedule_update_entries(false, cx)` 和一次 `schedule_update_entries(true, cx)`，最终 `update_entries` 执行几次？`select_first_entry` 会执行吗？

**答案**：`update_entries` 执行一次；`select_first_entry` 会执行。第一次调用 spawn 了任务 A；第二次调用因为 `select_first=true` 不走早退，用任务 B 替换 A（A 被 drop 取消），B 的任务体里 `update_entries` 之后调用 `select_first_entry()`。

**练习 3**：这个「去抖」窗口有多长？由什么决定？

**答案**：没有固定时长。窗口 = 从 spawn 到前台 executor 下一次泵送之间的这段时间。同一轮事件派发调用栈内触发的所有请求必然落在同一窗口；真实应用中大致等于一帧。它是 executor 批处理的副产物，不是定时器。

### 4.2 五步重建：update_entries 逐段精读

#### 4.2.1 概念说明

`update_entries` 是管线的执行体，它把「从当前世界状态全量重推导列表」这件事拆成固定的五步，外加前置守卫与收尾通知。理解它的关键是：**每一步的输入都是上一步刚生产出来的新 `contents`**，因此步骤顺序不可随意调换：

1. **重建**（`rebuild_contents`）：从各工作区、两个元数据 Store、活跃面板现查全量数据，生成新的 `SidebarContents`（u3-l4 专题精读）。
2. **草稿时间刷新**（`refresh_refilled_draft_times`）：检测「刚从空草稿变回有内容的草稿」，把它们的交互时间刷新为现在，让重新填写的草稿排到列表顶部。
3. **草稿订阅重连**（`refresh_draft_editor_observations`）：对当前可见草稿的消息编辑器重新接线，保证用户继续输入时仍能触发重建（u3-l1 已讲过它为什么每次都要重连）。
4. **差异应用**（`apply_list_state_diff`）：对比重建前后的行「形状」，只把变化区间拼接进 `ListState`，保住未变行的测量值（算法细节 u3-l3 专题）。
5. **默认分支预取**（`prefetch_worktree_default_branches`）：为新出现的项目分组预取远程默认分支，让「新建工作树」子菜单打开时不必做 git I/O（u4-l4 专题）。

#### 4.2.2 核心流程

```text
update_entries(cx)
    │
    ├─ 守卫 1：multi_workspace.upgrade() 失败 → 宿主已释放，直接返回
    ├─ 守卫 2：!multi_workspace_enabled(cx) → AI 功能被禁用，直接返回
    │
    ├─ 前置快照：had_notifications、previous_shapes（必须在重建前！）
    │
    ├─ 步骤 1  rebuild_contents          → 生成新 contents
    ├─ 步骤 2  refresh_refilled_draft_times（读新 contents，可能写回 Store）
    ├─ 步骤 3  refresh_draft_editor_observations（按新 contents 重接订阅）
    ├─ 步骤 4  apply_list_state_diff（previous_shapes vs 新 shapes）
    ├─ 步骤 5  prefetch_worktree_default_branches（读新 contents 的分组头）
    │
    ├─ 收尾 1：通知状态翻转 → multi_workspace.update + cx.notify（宿主徽标）
    └─ 收尾 2：cx.notify()（侧边栏自身重渲染）
```

#### 4.2.3 源码精读

[crates/sidebar/src/sidebar.rs:L1993-L2021](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1993-L2021) 是 `update_entries` 全文（连注释不到 30 行），函数 doc comment 只有一句：「Rebuilds the sidebar's visible entries from already-cached state」——强调它**只读已缓存的态**，不做任何异步等待，这也是它能被随意重入的原因。

**守卫**（L1994-1999）：第一道 `upgrade()` 判空防宿主已死；第二道查功能开关。[multi_workspace.rs:L432-L434](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/workspace/src/multi_workspace.rs#L432-L434) 显示 `multi_workspace_enabled` 就是 `!disable_ai && agent_enabled`——AI 被关掉时侧边栏静默不重建。

**前置快照**（L2001-2003）：`had_notifications` 记下重建前有没有通知；`previous_shapes` 把当前每行的身份键（`EntryShape`）收集成向量。**它们必须发生在 `rebuild_contents` 之前**，因为重建会用 `mem::take` 把 `self.contents` 整个换掉（[sidebar.rs:L1356](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1356)），旧 entries 即刻消失，事后无处采集。

**步骤 1**（L2005）调用 `rebuild_contents`。它的收尾在 [sidebar.rs:L1965-L1971](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1965-L1971)：一次性把 `entries`、两个通知集合、分组头索引、空态标志组装成新 `SidebarContents` 赋给 `self.contents`。

**步骤 2**（L2006）调用 [sidebar.rs:L2076-L2109](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2076-L2109) 的 `refresh_refilled_draft_times`：遍历**新** contents 里的草稿行，把每个草稿当前形态（`DraftKind::Empty` / `WithContent`）与上一轮记忆（`self.draft_kinds`）比对，凡是从 Empty 变回 WithContent 的，就向 `ThreadMetadataStore` 写入「刚刚交互过」的时间戳。注意这次写回会触发 Store 的 observe → 重入 `schedule_update_entries`——4.1.3 讲过的「先清 `update_task` 再重建」正是为了让这次重入能排上新任务，多收敛一轮后排序稳定。

**步骤 3**（L2007）调用 `refresh_draft_editor_observations`：先 `clear()` 掉旧的观察订阅（[sidebar.rs:L2114](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2114)），再按当前会话视图集合重新逐个订阅。会话视图集合只能现查、消息编辑器实体可能被替换，所以它是全 crate 唯一「每次重建都重连」的订阅（u3-l1 专题）。

**步骤 4**（L2010）调用 [sidebar.rs:L2024-L2051](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2024-L2051) 的 `apply_list_state_diff`：对新旧两组形状做前缀对齐 + 后缀对齐，只把中间变化区间 splice 进 `self.list_state`。L2009 的注释点明动机：「Preserve measurements for unchanged entries so sticky headers do not flicker」。`EntryShape` 的取值见 [sidebar.rs:L2053-L2071](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2053-L2071)：分组头带分组键 + 有无线程 + 是否折叠，线程/终端行只带各自 id——id 相同即视为「同一行」。

**步骤 5**（L2012）调用 [sidebar.rs:L2710-L2737](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2710-L2737) 的 `prefetch_worktree_default_branches`：从**新** contents 里收集所有 `ProjectHeader` 的分组键，凡缓存里没有的（[sidebar.rs:L2724-L2726](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2724-L2726) 的 `contains_key` 检查）就发起一次异步预取。把它放在最后一步是自然的：它只消费重建产物、不影响列表内容，纯粹是为未来的菜单交互做铺垫。

**收尾 1**（L2014-2018）：如果 `had_notifications != self.has_notifications(cx)`，就更新宿主并让它 `cx.notify()`。为什么？因为宿主 UI 通过 trait 方法**回调查询**侧边栏的通知状态：[multi_workspace.rs:L420-L423](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/workspace/src/multi_workspace.rs#L420-L423) 的 `sidebar_has_notifications` 读的正是侧边栏的 `has_notifications`（实现在 [sidebar.rs:L7687-L7689](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L7687-L7689)：任一通知集合非空）。侧边栏自己的 notify 不会让宿主重渲染，所以徽标状态翻转时必须额外拍一下宿主。

**收尾 2**（L2020）：`cx.notify()` 把侧边栏自身标记为待重绘。注意**只有**通知翻转才通知宿主，而侧边栏自身每次都 notify——两侧的重绘粒度是不同的。

#### 4.2.4 代码实践

**实践目标**：验证「步骤顺序有依赖」这一论断。

1. 阅读 L2001-2003 与 L2010，回答：把 `previous_shapes` 的采集挪到 `rebuild_contents` 之后会发生什么？
2. 阅读 L2714-2722（`prefetch_worktree_default_branches` 开头从 `self.contents.entries` 收集分组键），回答：把步骤 5 挪到步骤 1 之前会发生什么？是崩溃还是功能退化？
3. 对照 `toggle_collapse`（[sidebar.rs:L3229-L3238](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L3229-L3238)）与 u3-l1 讲过的任一订阅回调，各写一句话说明「谁该直接调、谁该走调度」。

**需要观察的现象**：两个挪动都不会引发编译错误，但都会破坏正确性——这类「顺序依赖」只有读代码才能发现，编译器不帮忙。

**预期结果**：第 1 问——旧 entries 已被 `mem::take` 换掉，采集到的是新形状，`apply_list_state_diff` 的比对基准失效，等于没做差异保护（粘性头部测量被重置）。第 2 问——预取读到的是旧 contents，本轮新出现的分组被漏掉，只能等下一轮重建补上；是功能滞后而非崩溃。（待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：`update_entries` 的 doc comment 说它「Rebuilds the sidebar's visible entries from already-cached state」。为什么强调「already-cached」？

**答案**：整个函数没有任何 `await`，也不发起同步 I/O；所有数据（元数据 Store、活跃面板、工作区列表）都是内存中现成的实体状态。正因为它同步且只读缓存，才能被事件订阅、用户交互、重入的 Store observe 无差别地频繁调用而不怕竞态——调一百次结果一致（幂等），这也是合并策略敢「把多次请求并成一次」的底气。

**练习 2**：五步里哪几步会**写**外部状态（除 `self` 之外）？

**答案**：步骤 2 可能写 `ThreadMetadataStore`（刷新回填草稿的 `interacted_at`）；步骤 3 写订阅集合（drop 旧订阅、注册新订阅）；步骤 4 写 `self.list_state`（自身状态）；步骤 5 可能发起异步 git 查询并最终写 `self.worktree_default_branches` 缓存。收尾处可能通知宿主。其中步骤 2 的写回会引起一轮新的调度，形成「重建 → 写回 → 再重建」的收敛循环。

**练习 3**：为什么 `had_notifications` 的比较是「翻转才通知宿主」，而不是每次都通知？

**答案**：宿主只关心徽标的有无（布尔量），不关心通知内容；布尔量没变时让宿主重渲染是纯浪费。而侧边栏自身的 `cx.notify()` 每次都要调，因为列表内容/选中态可能随时在变。这是一个「按消费者需要的粒度发通知」的小范例。

### 4.3 架构约束：为什么禁止增量协调状态

#### 4.3.1 概念说明

`Sidebar` 结构体头顶的 doc comment 是整个 crate 最重要的三行字：

> The sidebar re-derives its entire entry list from scratch on every change via `update_entries` → `rebuild_contents`. Avoid adding incremental or inter-event coordination state — if something can be computed from the current world state, compute it in the rebuild.

翻译过来：**任何变化都经 `update_entries` → `rebuild_contents` 从零重推导整个列表；不要添加增量或跨事件协调状态——凡是能从当前世界状态算出来的，就在重建时算。**

为什么这么武断？因为事件源有十六类（u3-l1），它们只报告「世界变了」，**不携带精确差异**。如果允许增量状态，每个事件处理器都必须正确推断「这次事件改变了什么、该改哪些中间变量」——十六类事件两两组合的交互空间是灾难，漏一次更新或错一次更新就是列表与真实世界脱节的 bug，而且极难复现。全量重推导把所有正确性论证压缩成一条：「`rebuild_contents` 这个纯函数正确」。

代价是每次重建要做完整查询；收益是无状态漂移、行为可预测、测试可以**数据级**驱动（`save_thread_metadata` 播种 → `run_until_parked` → 直接断言 `contents`，完全绕开 UI）——`sidebar_tests.rs` 里 15000 行测试几乎全靠这个性质才写得动。

但约束不是绝对的。**记「上一刻」的字段是被豁免的**：`live_thread_statuses`（上一轮的线程状态，用于检测 Running→Completed 跳变）、`draft_kinds`（上一轮的草稿形态）、`notified_threads`（历史通知记忆）、`thread_last_accessed`（用户显式访问时间）——它们记录的信息**无法从当前世界状态推导**，恰好落在约束的豁免条款里：「能算出来的都要现算」的反面就是「算不出来的才允许记」。易失交互状态（`selection`、重命名三件套）同理。

#### 4.3.2 核心流程

判断一个新字段能不能加到 `Sidebar` 上的决策树：

```text
新状态 X 从哪来？
    │
    ├─ 能从当前世界状态（工作区 / 元数据 Store / 活跃面板）算出
    │       → 禁止存字段，写进 rebuild_contents 现算
    │
    ├─ 描述「上一刻」的信息（上轮状态、跳变检测、历史记忆）
    │       → 允许记，但要写清 doc comment 说明为什么无法现算
    │
    └─ 易失交互状态（键盘焦点、正在进行的输入）
            → 允许记，重建后需要钳制/清理（如 selection 的 clamp）
```

#### 4.3.3 源码精读

约束原文在 [crates/sidebar/src/sidebar.rs:L730-L733](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L730-L733)，紧贴着 `pub struct Sidebar`——每个想往结构体里加字段的人都会先读到它。

豁免字段各有 doc comment 说明「为什么必须记」。最典型的是 `live_thread_statuses`：[sidebar.rs:L765-L768](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L765-L768) 写明它「Persists live thread statuses across rebuilds so that Running→Completed transitions can be detected even when the group is collapsed (and thread entries are not present in the list)」——分组折叠时行不在列表里，跳变检测若不靠跨重建记忆就会漏通知。`draft_kinds` 的说明在 [sidebar.rs:L769-L772](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L769-L772)，正是 4.2.3 步骤 2 的比对基准。

重建侧对「记忆」的继承是**显式且最小化**的：`rebuild_contents` 开头 `mem::take` 出旧快照后（[sidebar.rs:L1356](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1356)），只有 `notified_threads` 被继承（[sidebar.rs:L1361](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1361)：`let mut notified_threads = previous.notified_threads;`），而 `notified_terminals` 每轮从活跃面板现算（L1362 新建空集合，L1392-1402 从各工作区的 AgentPanel 收集 `has_notification` 的终端）。「该记的记、能算的算」在同一函数里形成了直接对照。

#### 4.3.4 代码实践

**实践目标**：用决策树审视一个真实字段。

1. 读 [sidebar.rs:L757-L761](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L757-L761) 上 `thread_last_accessed` 的 doc comment（「Updated only in response to explicit user actions — never from background data changes」）。
2. 思考：线程的最后访问时间能不能在重建时从元数据 Store 算出来？（提示：Store 里有一个 `update_interacted_at` 写入的交互时间，但它由包括后台数据变化在内的多种事件更新。）
3. 用 4.3.2 的决策树给 `thread_last_accessed` 归类，写两三句话论证。

**预期结果**：它能从 Store 算出「最后一次交互」，但算不出「最后一次**用户显式**访问」——这个语义差异（用于线程切换器的 MRU 排序，u7-l2 展开）无法从当前世界状态推导，所以属于豁免的记忆字段。（此为源码阅读型练习，无需改代码。）

#### 4.3.5 小练习与答案

**练习 1**：假设有人提议：「分组折叠状态每次重建都从 `MultiWorkspace` 查一次太浪费，不如在 `Sidebar` 上存一份 `collapsed_cache`，在折叠事件里更新它。」依据 doc comment反驳。

**答案**：折叠状态的权威来源是 `MultiWorkspace`（u2-l2 讲过它持有 `group_state_by_key`），当前世界状态里随时可查（`entry_shapes` 每次都现查，见 [sidebar.rs:L2063-L2066](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2063-L2066)）。加缓存就是典型的「增量协调状态」：任何绕过侧边栏折叠回调的状态变化（恢复序列化、其他视图改折叠）都会让缓存与权威来源脱节。这正是 doc comment 禁止的场景。

**练习 2**：`notified_threads` 继承自上一轮、`notified_terminals` 每轮现算，为什么待遇不同？

**答案**：终端的通知标志是活跃面板上的现成属性（`terminal.has_notification`），当前世界状态查得到，所以现算；线程的通知需要检测 Running→Completed 的**跳变**，且分组折叠时相关行根本不在列表里，「上一轮是 Running」这个事实查不到，只能靠继承。一个「能算」，一个「必须记」，恰好落在约束的两侧。

### 4.4 cx.notify：重建之后如何让 UI 知道

#### 4.4.1 概念说明

管线执行到最后，数据结构（`contents`、`list_state`）已经更新，但屏幕上还是旧的。`cx.notify()` 是数据世界与渲染世界之间的唯一桥梁：它不渲染任何东西，只把当前实体标记为脏，gpui 会在下一帧把所有脏实体的 `render()` 重新跑一遍。

本管线里 notify 有两个方向、两种粒度：

- **通知自己**（L2020，每次重建都调）：侧边栏列表内容可能变了，自己要重绘。
- **通知宿主**（L2014-2018，仅通知布尔翻转时调）：宿主 `MultiWorkspace` 的 UI 会通过 trait 回调查询 `has_notifications` 来画徽标，侧边栏必须替它触发重绘，但只在布尔值翻转时才值得。

#### 4.4.2 核心流程

```text
cx.notify()
    │
    ├─ 实体进入下一帧的重绘集合
    ├─ 该实体上所有 cx.observe 回调被触发（别的实体在观察侧边栏时）
    └─ 下一帧：gpui 调用 Render::render → 新的元素树 → 布局与绘制

multi_workspace.update(cx, |_, cx| cx.notify())
    │
    └─ 宿主实体进入重绘集合（它的 render 会再次向侧边栏要徽标状态）
```

#### 4.4.3 源码精读

收尾两段在 [crates/sidebar/src/sidebar.rs:L2014-L2021](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2014-L2021)：先用 `had_notifications != self.has_notifications(cx)` 判断翻转，翻转才 `multi_workspace.update(cx, |_, cx| { cx.notify(); })`；然后无条件 `cx.notify()`。`has_notifications` 的实现是 [sidebar.rs:L7687-L7689](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L7687-L7689)：两个通知集合任一非空。宿主侧的消费口是 [multi_workspace.rs:L420-L423](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/workspace/src/multi_workspace.rs#L420-L423) 的 `sidebar_has_notifications`，经由 `WorkspaceSidebar` trait（sidebar crate 与 workspace crate 之间的契约，u8-l3 展开）。

另外注意任务体里 `select_first_after_update` 分支也单独调了一次 `cx.notify()`（[sidebar.rs:L1983-L1986](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1983-L1986)）：`select_first_entry` 改了 `selection`，这次额外 notify 确保选中态变化即使在没有其他状态变化时（理论上少见）也不会漏重绘——多次 `cx.notify()` 是幂等的，多调无害。

#### 4.4.4 代码实践

**实践目标**：体会「数据更新」与「渲染调度」是分离的。

1. 读 [sidebar_tests.rs:L1128](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar_tests.rs#L1128) 附近的 `test_visible_entries_as_strings` 与 `visible_entries_as_strings` 辅助函数的实现（在测试文件头部搜索 `fn visible_entries_as_strings`）。
2. 回答：这些测试断言的是 `render()` 的输出还是 `sidebar.contents` 的数据？如果删掉 `update_entries` 末尾的 `cx.notify()`，这些测试还会通过吗？

**需要观察的现象**：测试根本不依赖渲染发生。

**预期结果**：断言直接读 `contents` 数据结构；删掉 `cx.notify()` 后数据级测试照常通过，坏掉的是真实应用的 UI（列表不再重绘）与所有 observe 侧边栏的下游。这解释了为什么这套测试能跑得又快又稳。（待本地验证：可临时注释 L2020 后跑 `cargo test -p sidebar --lib test_visible_entries`。）

#### 4.4.5 小练习与答案

**练习 1**：`cx.notify()` 被调用了两次（任务体的 select_first 分支一次、`update_entries` 末尾一次）会触发两次渲染吗？

**答案**：不会。notify 只是把实体加入重绘集合，是幂等的标记操作；真正渲染发生在下一帧，一帧内无论标记多少次都只重绘一次。这又是一个「合并」思想的体现。

**练习 2**：为什么宿主的 notify 要包在 `multi_workspace.update(cx, ...)` 里，而不是直接拿宿主句柄调用什么方法？

**答案**：gpui 里 `cx.notify()` 必须在**被更新实体的 Context 上**调用才生效（它标记的是「当前正在 update 的这个实体」）。侧边栏在自己的 `Context<Sidebar>` 里不能直接通知别的实体，必须先 `multi_workspace.update(cx, |_, cx| cx.notify())` 进入宿主的更新闭包，借宿主自己的 `Context<MultiWorkspace>` 标记它。

## 5. 综合实践

本综合实践来自讲义规格，把本讲四个最小模块串成一次完整的动手实验。

**实践目标**：给 `update_entries` 的每个步骤插桩日志，跑一个真实测试还原调用顺序；然后用自己的话解释架构约束。

**操作步骤**：

1. 在本地工作副本中打开 `crates/sidebar/src/sidebar.rs`，仿照下面的样式在 `update_entries`（L1993 起）的守卫之后、五个步骤前后插入日志（`log` 已是本 crate 依赖，[Cargo.toml:L34](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/Cargo.toml#L34)，文件里已有 `log::error!` 的既有用法，如 [sidebar.rs:L4170](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L4170)）：

   ```rust
   // 示例代码：仅用于本地实验，实验结束后请还原
   log::info!("[pipeline] guard passed");
   log::info!("[pipeline] step 1: rebuild_contents ({} entries before)", self.contents.entries.len());
   self.rebuild_contents(cx);
   log::info!("[pipeline] step 2: refresh_refilled_draft_times");
   self.refresh_refilled_draft_times(cx);
   log::info!("[pipeline] step 3: refresh_draft_editor_observations");
   self.refresh_draft_editor_observations(cx);
   log::info!("[pipeline] step 4: apply_list_state_diff");
   self.apply_list_state_diff(&previous_shapes, multi_workspace.read(cx));
   log::info!("[pipeline] step 5: prefetch_worktree_default_branches");
   self.prefetch_worktree_default_branches(cx);
   log::info!("[pipeline] done, entries = {}", self.contents.entries.len());
   ```

2. 运行一个走**调度路径**的列表测试（它在搜索框输入文字，触发 `schedule_update_entries(true, ...)`）：

   ```bash
   cargo test -p sidebar --lib test_search_narrows_visible_threads_to_matches
   ```

3. **日志可见性注意**：测试二进制默认没有初始化任何 logger（本 crate 的测试脚手架 `init_test` 里没有，gpui 的 `TestAppContext` 里也没有），`log::info!` 的输出可能被静默吞掉。若遇到这种情况，实验期间可把 `log::info!` 临时换成 `println!`（或 `eprintln!`）并加 `--nocapture` 运行：

   ```bash
   cargo test -p sidebar --lib test_search_narrows_visible_threads_to_matches -- --nocapture
   ```

   实验结束后还原所有改动（这是插桩实验，不应提交）。

4. 对照日志输出，写出这个测试里 `update_entries` 被执行的完整时序：初始化阶段（三次 `save_thread_metadata` 播种 → `run_until_parked`）执行了几次？`type_in_search("diff")` 之后又执行了几次？每次日志里的 entries 数量如何变化？

5. 写一段 5-8 句的说明，回答规格里的问题：**为什么 `Sidebar` 结构体上的 doc comment（[sidebar.rs:L730-L733](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L730-L733)）要求「不要在事件之间添加增量协调状态」？**论证时至少用上：事件源只报告「世界变了」不携带差异、增量状态需要每个事件处理器正确维护、全量重推导把正确性集中到一个函数、豁免字段只记「上一刻」。

**需要观察的现象**：日志应显示同一轮 `run_until_parked` 内三次播种只合并出少数几次 `update_entries`（而非三次各一遍全流程）；输入 "diff" 后的一次重建 entries 数量从 4（1 分组头 + 3 线程）降到 2（1 分组头 + 1 匹配线程）。

**预期结果**（时序推导，待本地验证）：

1. `init_test_project` + `setup_sidebar` 建立世界；`Sidebar::new` 末尾 `defer_in` 里的首次 `schedule_update_entries(false, ...)`（[sidebar.rs:L886](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L886)）执行第一次 `update_entries`，此时 entries 通常为 0 或仅分组头。
2. 三次 `save_thread_metadata` 各触发一次 Store observe → 调度；`run_until_parked` 泵送后按合并窗口收敛，最终得到 4 行（`v [my-project]` + 三条线程）。
3. `type_in_search("diff", ...)` 内部 `set_text` 触发一次 `BufferEdited`：先 `selection.take()`，再 `schedule_update_entries(true, ...)`；任务体执行第二次可见的 `update_entries`（entries 降为 2），随后 `select_first_entry()` 选中 "Add inline diff view"——这正是测试断言里 `<== selected` 标注的来源。
4. 第二次 `type_in_search("nonexistent", ...)` 同理，entries 降为 0。

如果你的日志时序与此不符，以日志为准并回头修正对本讲的理解——这正是插桩实验的价值。

## 6. 本讲小结

- `schedule_update_entries` 是所有刷新的唯一漏斗：已有 pending 任务时普通请求直接早退（合并），`select_first_after_update=true` 的请求替换旧任务（drop 即取消），去抖窗口是「到前台 executor 下一次泵送为止」，没有任何定时器。
- 任务体里**先清 `update_task` 再调 `update_entries`**，为的是让步骤 2 写回 Store 触发的重入调度能排上新任务，形成收敛循环而不是丢刷新。
- `update_entries` = 两道守卫（宿主存活、功能开关）+ 前置快照（`had_notifications`、`previous_shapes` 必须在重建前采集）+ 五个步骤（重建 → 草稿时间刷新 → 草稿订阅重连 → 差异应用 → 默认分支预取）+ 收尾通知（翻转才通知宿主、每次通知自己）。
- `cx.notify()` 只是标记脏、不渲染；数据级测试读 `contents` 不依赖渲染，所以它们能绕开 UI 直接断言世界状态。
- 「每次全量重推导」约束的含义：能从当前世界状态算出来的禁止存字段；豁免的只有「记上一刻」的记忆字段（`live_thread_statuses`、`draft_kinds`、`notified_threads`、`thread_last_accessed`）与易失交互状态（`selection` 等）。

## 7. 下一步学习建议

- 下一讲 **u3-l3（EntryShape 与 apply_list_state_diff）**深入本讲步骤 4 的算法：前缀/后缀对齐如何算出最小拼接区间，以及为什么只 splice 变化区间能保住粘性头部的测量值。
- 之后 **u3-l4（rebuild_contents 全景）**展开步骤 1 内部：项目分组遍历、四路查询与去重、路径消歧、活跃信息合并、搜索过滤。
- 想先看下游的话，**u4-l1（渲染主骨架）**会告诉你 `cx.notify()` 标记的脏实体在 `render()` 里如何把 `contents` 与 `list_state` 变成屏幕上的元素树。
- 建议顺带重读 [sidebar.rs:L730-L733](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L730-L733) 的 doc comment 与 `Sidebar` 的字段注释——学完本讲再读，每个「记忆字段」的豁免理由都会清晰很多。
