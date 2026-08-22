# rebuild_contents 全景：从项目分组到可见行

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `rebuild_contents` 从「宿主 MultiWorkspace + 全局元数据 Store + 活跃 AgentPanel」到「一份完整的 `SidebarContents`」的完整推导过程，并能在源码中定位每个阶段的起止行号。
2. 解释为什么一个分组要用**四路查询**收集线程与终端行，以及 `seen_thread_ids` / `seen_terminal_ids` 如何跨分组去重。
3. 理解路径消歧算法 `compute_disambiguation_details`（分组标签撞名时逐级加深路径后缀）与 `branch_by_path` 分支名映射如何影响行的最终展示。
4. 说明 `apply_active_info` 如何把进程内存里的活跃线程信息「覆盖」到数据库元数据之上，以及通知（`notified_threads`）的检测与清理时机。
5. 理解草稿行的可见性判定规则和搜索过滤阶段的匹配范围。

本讲是前几讲的汇合点：u3-l2 讲了「谁调用 rebuild_contents」（重建管线），u2-l2 讲了「分组键与工作区形态」，本讲讲「rebuild_contents 内部到底做了什么」——它是本 crate 最长的单个函数（约 630 行）。

## 2. 前置知识

### 2.1 全量重推导教义（承接 u3-l2）

回顾 u3-l2 的核心约束：侧边栏是纯响应式组件，**任何**变化都汇入 `schedule_update_entries`，最终由 `update_entries → rebuild_contents` 从当前世界状态**从零重推导**整个列表。`rebuild_contents` 内部不接收任何「增量」，也不维护跨调用的中间缓存（少数「记忆字段」除外，见 4.4）。

### 2.2 数据的两个来源：「死的」与「活的」

侧边栏的每一行数据都来自两个世界：

| 来源 | 存活范围 | 承载内容 | 载体 |
|---|---|---|---|
| 数据库元数据 | 跨进程持久 | 标题、路径、时间戳、is_draft 等静态字段 | `ThreadMetadataStore` / `TerminalThreadMetadataStore`（全局实体） |
| 活跃面板信息 | 仅本进程内存 | 实时状态（Running/Completed…）、正在生成的标题、diff 统计 | 各 Workspace 的 `AgentPanel` |

`rebuild_contents` 的本质就是：**用持久元数据打底，用活跃信息覆盖**，两股数据在行对象上合流。

### 2.3 WorktreePaths：main 与 folder 的双列表

一个线程的 `worktree_paths` 不是单个路径，而是 [`WorktreePaths`](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/project/src/worktree_store.rs#L38-L49)——两条**等长平行**的 `PathList`：

- `main_paths`：主仓库路径（分组键的来源，`threads_by_main_paths` 索引用它）；
- `paths`（folder paths）：线程实际打开的文件夹路径（`threads_by_paths` 索引用它）。

普通工作树中两者相同；linked worktree 中 folder 指向 linked 位置、main 指向原仓库。这个「一把钥匙开两把锁」的设计正是四路查询的根源。

### 2.4 session_id 与 thread_id（承接 u2-l3）

`ThreadId` 是本地铸造的，跨窗口恢复时可变；`SessionId` 是 ACP 远端会话身份，恒稳定。`rebuild_contents` 合并活跃信息时用 **session_id** 做桥（活跃信息只带 session_id）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `crates/sidebar/src/sidebar.rs` | 本讲主战场：`rebuild_contents`（1342–1972 行）及全部辅助函数 |
| `crates/agent_ui/src/thread_metadata_store.rs` | 线程元数据 Store：两路索引查询 `entries_for_path` / `entries_for_main_worktree_path`，以及 `worktree_info_from_thread_paths` 行内徽标计算 |
| `crates/agent_ui/src/terminal_thread_metadata_store.rs` | 终端元数据 Store：同名的两路查询 |
| `crates/util/src/disambiguate.rs` | 路径消歧算法 `compute_disambiguation_details`（纯函数，自带单元测试） |
| `crates/project/src/project.rs` | `ProjectGroupKey::display_name` 与 `path_suffix`（把 detail 级别转成路径后缀字符串） |
| `crates/project/src/worktree_store.rs` | `WorktreePaths` 定义与 `ordered_pairs` |
| `crates/workspace/src/multi_workspace.rs` | `project_groups(cx)`：分组的权威来源 |
| `crates/sidebar/src/sidebar_tests.rs` | 去重验证测试 `test_terminal_metadata_is_deduped_across_project_groups` |

## 4. 核心概念与源码讲解

### 4.1 rebuild_contents 总体流程：一次前向遍历的全量重推导

#### 4.1.1 概念说明

[函数的 doc comment](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1327-L1341) 明确了三件事：

1. **遍历单位是项目分组**（`project_groups`），不是工作区；
2. **性能目标是单次前向遍历 + 一次 O(T log T) 排序**，禁止对数据做额外反复扫描；
3. **三条不变量**：每个工作区必须至少被一个分组展示；每个线程都要挂到它所属的分组上；重建后 active 状态必须与「当前工作区的当前面板的当前线程」精确一致。

「全量重推导」意味着函数开头先把旧的 `contents` 整个拿走（`mem::take`），最后再装回一份全新的——中间任何提前返回都不会留下半新半旧的状态。

#### 4.1.2 核心流程

```text
rebuild_contents(cx)
│
├─ 阶段 0：快照采集
│   ├─ multi_workspace.upgrade() 失败 → 直接返回（宿主已释放）
│   ├─ workspaces / active_workspace / agent_server_store
│   ├─ query ← filter_editor 文本（搜索过滤词）
│   ├─ previous ← mem::take(self.contents)   // 旧快照，只回收 notified_threads
│   └─ old_statuses ← self.live_thread_statuses  // 上一刻的实时状态（记忆字段）
│
├─ 阶段 1：跨分组准备（分组循环之前，只做一次）
│   ├─ live_notified_terminal_ids ← 扫描所有工作区的 AgentPanel 终端通知
│   ├─ path_detail_map ← compute_disambiguation_details(全部分组路径) // 4.3
│   └─ branch_by_path ← 扫描所有仓库快照的分支名                   // 4.3
│
├─ 阶段 2：for group in mw.project_groups(cx)      // 分组主循环
│   ├─ 2a. 终端收集：四路查询 + seen_terminal_ids 去重   // 4.2
│   ├─ 2b. 线程收集：四路查询 + seen_thread_ids 去重     // 4.2
│   │       （分组折叠且无搜索词时跳过 2b，走折叠支线 // 4.4）
│   ├─ 2c. 草稿判定：补标题、retain 空草稿               // 4.5
│   ├─ 2d. 活跃信息合并：apply_active_info + 通知检测    // 4.4
│   ├─ 2e. 组内排序：按 display_time 降序
│   ├─ 2f. 搜索过滤（query 非空时）：fuzzy 匹配 + 高亮位置 // 4.5
│   └─ 2g. 压入 ProjectHeader（先记录下标）+ 行条目
│
└─ 阶段 3：收尾
    ├─ notified_threads ∩= current_thread_ids   // 通知集合收敛到仍存在的行
    ├─ thread_last_accessed / terminal_last_accessed 同样裁剪
    ├─ self.live_thread_statuses = new_live_statuses
    └─ self.contents = SidebarContents { entries, notified_threads, … }
```

#### 4.1.3 源码精读

**入口与快照采集**。[sidebar.rs:1342-1374](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1342-L1374)：先 `upgrade` 宿主弱引用（失败即返回，宿主已死则侧边栏无意义），随后一口气采集所有输入：工作区列表、活跃工作区、agent 图标存储、过滤词。注意 `mem::take(&mut self.contents)` 把旧内容整个取走——只有 `previous.notified_threads` 会被回收复用（见 4.4），其余全部丢弃重算。`old_statuses` 是「记忆字段」`live_thread_statuses` 的借用，它是全量重推导世界里少数允许的跨调用状态：因为「上一刻是否 Running」无法从当前世界状态推出来。

**两组累积器**。[sidebar.rs:1360-1370](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1360-L1370)：`entries`（最终行列表）、`notified_threads`（从旧快照继承）、`notified_terminals`（每次现算）、`new_live_statuses`、三个 `current_*` 集合（本轮实际见到的行身份，收尾裁剪用），以及两个去重集合 `seen_thread_ids` / `seen_terminal_ids`——**注意它们声明在分组循环之外**，这正是「跨分组去重」的关键（见 4.2）。

**收尾装配**。[sidebar.rs:1956-1971](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1956-L1971)：三处 `retain` 把通知记忆与两个「最近访问时间」记忆裁剪到 `current_*` 集合内——行已经消失的记忆不留（否则通知徽标和 MRU 排序会指向幽灵行）；最后整体替换 `self.contents`。

#### 4.1.4 代码实践

**实践目标**：把 4.1.2 的抽象流程落到具体行号，建立「阶段 → 代码位置 → 数据来源」的对照表。

**操作步骤**：

1. 打开 `sidebar.rs`，从 1342 行读到 1972 行（可分段读）。
2. 为阶段 0/1/2a–2g/3 各记下起止行号。
3. 为每个阶段标注它**读取**了哪些全局实体或宿主状态（提示：`TerminalThreadMetadataStore::global`、`ThreadMetadataStore::global`、`AgentPanel`、`MultiWorkspace::project_groups`、`filter_editor`、`agent_server_store`、git 仓库快照）。
4. 把结果画成一张流程图（Mermaid 或手绘均可）。

**需要观察的现象**：你会发现 `ThreadMetadataStore::global(cx)` 在函数内被调用了不止一次（分组循环内 1561 行、折叠支线 1773 与 1913 行、`has_stored_thread_rows` 1799 行）——同一个 Store 在不同阶段、不同分支被反复查询，但没有一处缓存查询结果。

**预期结果**：得到一张类似下表的对照表（行号以当前 HEAD 为准）：

| 阶段 | 行号 | 读取的 Store / 状态 |
|---|---|---|
| 快照采集 | 1342–1374 | MultiWorkspace、filter_editor |
| 终端通知扫描 | 1391–1402 | 各工作区 AgentPanel |
| 路径消歧 | 1404–1415 | project_groups（分组键） |
| 分支名映射 | 1417–1437 | 各 Project 的 git 仓库快照 |
| 终端收集 | 1473–1536 | TerminalThreadMetadataStore |
| 线程收集 | 1560–1669 | ThreadMetadataStore |
| 活跃合并 | 1704–1757 | AgentPanel（经 live_infos） |
| 搜索过滤 | 1815–1902 | fuzzy（内存计算） |

### 4.2 行的收集：多路查询与去重

#### 4.2.1 概念说明

为什么不能「一次查完」？因为持久元数据有**两套索引**，且历史数据不一致：

- 新线程写入时 `main_worktree_paths` 一定指向分组的规范路径（无论线程实际开在哪个 linked worktree）；
- **旧版线程**没有 `main_worktree_paths`，只能按 `folder_paths` 查；
- 还存在「脏行」：存储的 main 路径与分组键不一致（代码注释举例：linked worktree 工作区的旧行 main == folder），要靠「逐个工作区按其根路径查」来兜底，直到下次 `handle_conversation_event` 把它改写成正确形状；
- linked worktree 的线程 folder 路径既不等于分组键也不等于任何打开工作区的根路径，需要单独按每个 linked worktree 路径查。

于是每个分组对线程、终端各做**四路查询**。四路的结果可能重叠（同一个线程被多路命中、甚至被多个分组命中），所以用 `seen_*_ids` 集合做**首次出现即收录**的去重。

#### 4.2.2 核心流程

```text
对每个分组 group（含 key、workspaces）：
  workspace_by_path_list ← { 该组各工作区根路径 → 工作区实体 }
  resolve_workspace(folder_paths)：
      命中表 → ThreadEntryWorkspace::Open(实体)
      未命中 → ThreadEntryWorkspace::Closed { folder_paths, project_group_key }

  终端（先于线程，无条件执行，即使分组折叠）：
    ① entries_for_main_worktree_path(分组键, host)   → resolve_workspace(folder)
    ② entries_for_path(分组键, host)                  → resolve_workspace(folder)
    ③ 对组内每个打开工作区: entries_for_path(ws 根路径) → Open(该工作区)
    ④ 对组内每个 linked worktree: entries_for_path(wt 路径) → Closed { 该路径 }
    每路结果经 push_terminal_metadata：seen_terminal_ids 未收录才 push

  线程（仅当 !is_collapsed || 有搜索词时执行）：
    同样的四路 + seen_thread_ids 去重，构造闭包 make_thread_entry
```

#### 4.2.3 源码精读

**两路索引查询**（以线程 Store 为例，终端 Store 有完全对称的一对）：

- [`entries_for_path`](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/thread_metadata_store.rs#L621-L633)：按 `threads_by_paths`（folder 路径列表）索引取行，过滤掉已归档行、再按远程连接身份过滤——`remote_connection` 为 `None` 时只返回本地线程，为 `Some` 时只返回身份匹配的远程线程。
- [`entries_for_main_worktree_path`](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/thread_metadata_store.rs#L643-L655)：按 `threads_by_main_paths` 索引取行，其余过滤相同。doc comment 点明用途：找到「开在 linked worktree、但归属本主工作树」的线程。

终端侧的同名方法在 [terminal_thread_metadata_store.rs:206-L224](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/terminal_thread_metadata_store.rs#L206-L231)。

**工作区解析**。[sidebar.rs:1443-1455](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1443-L1455)：`resolve_workspace` 闭包把行的 `folder_paths` 现场对照组内打开工作区的根路径表——命中即 `Open`，未命中即 `Closed`（保留重开所需的身份材料）。这是 u2-l2 讲过的「一次现查、从不缓存」判定。

**终端四路查询**。[sidebar.rs:1473-1526](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1473-L1526)：四段 `for row in terminal_store...` 分别对应①②③④。去重集中在 [`push_terminal_metadata`](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1476-L1482)：`HashSet::insert` 返回 `false` 说明已收录，直接返回。

**线程四路查询**。[sidebar.rs:1588-1669](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1588-L1669)：每段前的注释解释了该路存在的理由——主路径是「正路」（[1588-1591](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1588-L1591)），②是「老线程兜底」（[1604-1607](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1604-L1607)），③是「脏行兜底：main 路径与分组键不一致时仍要显示在它实际所属的分组下」（[1620-1630](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1620-L1630)），④是「linked worktree 老线程」。每路的去重是同一个惯用式：

```rust
if !seen_thread_ids.insert(row.thread_id) {
    continue;
}
```

**行构造闭包**。[sidebar.rs:1563-1586](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1563-L1586)：`make_thread_entry` 先给保守默认值（`status: default()`、`is_live: false`……），活跃信息稍后由 4.4 的合并阶段覆盖。注意 1568–1571 行的注释：草稿先一律标成 `WithContent`，由 4.5 的后处理降级。

**linked worktree 路径清单**。[sidebar.rs:537-557](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L537-L557)：只对**单根**工作区收集其根仓库的 linked worktree 路径（多根工作区跳过），并按路径排序保证遍历顺序稳定。

#### 4.2.4 代码实践

**实践目标**：用真实测试验证「跨分组去重」的必要性。

**操作步骤**：

1. 在仓库根目录运行：

   ```bash
   cargo test -p sidebar test_terminal_metadata_is_deduped_across_project_groups
   ```

2. 阅读测试 [sidebar_tests.rs:1935-2013](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L1935-L2013)，注意 1979–1983 行构造的元数据：`main_worktree_paths = [/project-a]`，`folder_paths = [/project-b]`，而窗口里有 project-a、project-b 两个分组。
3. 回答：这个终端会被哪几个分组、哪几路查询命中？
4. （可选的本地破坏性实验）临时注释掉 `push_terminal_metadata` 里的 `if !seen_terminal_ids.insert(...) { return; }`，再跑该测试，预期断言 `count() == 1` 失败（同一个终端出现在两个分组下）。实验后务必还原。

**需要观察的现象 / 预期结果**：测试通过；问题 3 的答案——分组 A（键 `/project-a`）经路①（main 路径）命中它，分组 B（键 `/project-b`）经路②（folder 路径）也命中它；由于 `seen_terminal_ids` 在**整个重建**范围内共享（声明于分组循环之外），第二次命中被丢弃，最终全列表只出现一次。破坏性实验的失败结果为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果一个线程的 `folder_paths` 等于某个 linked worktree 的路径，且 `main_worktree_paths` 已正确写成分组键，它会被哪几路查询命中？去重后收录的是哪一路的结果？

答案：路①（main 路径匹配分组键）和路④（folder 路径匹配 linked worktree 清单）都会命中。`seen_thread_ids` 先收录路①（它在循环中先执行），其 `workspace` 由 `resolve_workspace(folder_paths)` 决定——folder 不在打开工作区表中，所以是 `Closed { folder_paths, project_group_key }`；路④的结果被丢弃。

**练习 2**：为什么终端收集放在 `if group_key.path_list().paths().is_empty() { continue; }` 与 `should_load_threads` 判定**之前**（1537–1544 行），即分组折叠时终端行仍然收集？

答案：折叠只影响线程行（`should_load_threads = !is_collapsed || !query.is_empty()`）；终端行无论折叠与否都要收集，因为 1527–1536 行要用收集结果维护 `current_terminal_ids`（收尾裁剪 `terminal_last_accessed`）和 `notified_terminals`（分组头的通知徽标在折叠时也要显示）。同理 `has_visible_rows` 的计算也依赖 terminals。

**练习 3**：`entries_for_path` 的 `remote_connection` 参数传 `None` 意味着什么？

答案：只返回本地（非远程）线程。分组键的 `host()` 为 `None` 表示本地分组，所以本地分组查询时传 `None`；远程分组则传该主机的连接选项，Store 内按「规范化身份相等」过滤，避免同名不同认证方式的远程被混在一起。

### 4.3 展示名计算：路径消歧与分支名映射

#### 4.3.1 概念说明

两个「名字问题」要在分组循环**之前**解决，因为它们的输入是全体分组的路径集合：

1. **分组标签撞名**：用户同时打开 `/home/me/code/zed` 和 `/home/me/worktrees/zed/focal-arrow/zed`，两个分组键的末级目录都叫 `zed`，若标签只显示末级名字就无法区分。解法是**路径消歧**：为每条路径算一个「细节级别」detail，级别 0 只显示末级组件，撞名则逐级加一层父目录，直到互不相同。
2. **行内徽标要显示分支名与工作树归属**：线程行的 worktree 徽标（`ThreadItemWorktreeInfo`）需要短名、全路径、Main/Linked 类型以及当前分支名。分支名只能从**当前打开工作区**的 git 仓库快照里现场读——Store 里没有存。

#### 4.3.2 核心流程

`compute_disambiguation_details` 是一个迭代消解碰撞的算法：

```text
输入：items（全部路径），get_description(item, detail)
初始化：details = [0; n]，每项按 detail=0 求描述
loop：
  1. 按描述分桶；描述相同的项（碰撞）detail += 1
  2. 若本轮无任何碰撞 → 返回 details
  3. 若某项的描述在升到新级别后不再变化（不动点），
     视为其描述已稳定，不再参与碰撞检查（保证终止）
```

侧边栏把 `get_description` 定为 `project::path_suffix(path, detail)`：从路径末尾取 `detail + 1` 个普通组件用 `/` 连接（detail=0 即末级目录名）。侧边栏侧的调用是[sidebar.rs:1404-1415](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1404-L1415)：收集全部分组键的路径后 **`sort_unstable` + `dedup`** 再消歧——去重不可省，否则同一分组里重复出现的路径会自己跟自己撞名，把 detail 推到全路径（util 的测试 `test_duplicate_paths_from_multiple_groups` 专门锁定了这个回归）。结果装进 `path_detail_map: HashMap<PathBuf, usize>`，供稍后 `display_name` 查询。

分支名映射 [sidebar.rs:1417-1437](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1417-L1437)：遍历所有打开工作区的 Project 的全部仓库快照，把「仓库工作目录绝对路径 → 当前分支名」和「每个 linked worktree 路径 → 其分支名」写进 `branch_by_path`。

#### 4.3.3 源码精读

**消歧算法本体**：[disambiguate.rs:14-58](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/util/src/disambiguate.rs#L14-L58)。doc comment 说明了终止保证：`get_description` 必须最终到达不动点（对路径后缀来说就是「取到全路径后再加级别也不变」），到达不动点的项不再检查碰撞。

**path_suffix**：[project.rs:6494-6506](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/project/src/project.rs#L6494-L6506)。注意它过滤掉非 Normal 组件（如根目录 `/`），`take(detail + 1)` 后再反转拼接——所以 detail=0 是末级名，detail=1 是「父/子」两级。

**分组标签**：[project.rs:6458-6482](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/project/src/project.rs#L6458-L6482) 的 `ProjectGroupKey::display_name` 消费 `path_detail_map`：对键中每条有序路径取 detail 级后缀、剥掉 `.git` 扩展名（裸克隆 `foo.git` 显示为 `foo`），用 `, ` 连接；空分组显示 "Empty Workspace"。侧边栏在 1541 行调用它得到 `label`。

**行内 worktree 徽标**：[`worktree_info_from_thread_paths`](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/thread_metadata_store.rs#L374-L436)。遍历 `WorktreePaths::ordered_pairs()`（main 与 folder 平行对）：

- `main != folder` → Linked 工作树，短名取 folder 相对 main 的差异部分（`linked_worktree_short_name`）；
- 相等 → Main 工作树，短名取 folder 的 `file_name`；
- 每个徽标的 `branch_name` 从传入的 `branch_by_path` 表查询（folder 路径为键）。

最妙的是 423–433 行的二次消歧：当线程的多个 main 路径互不相同、且各徽标短名不完全一致时，Linked 徽标会被改写成 `项目名:短名`（如 `zed:feature`），让用户一眼看出这个 linked worktree 属于哪个主项目。

**消费点**：线程行在 `make_thread_entry`（[sidebar.rs:1566-1567](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1566-L1567)）、终端行在 `make_terminal_entry`（[sidebar.rs:1460-1461](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1460-L1461)）处各自调用一次 `worktree_info_from_thread_paths`。

#### 4.3.4 代码实践

**实践目标**：在不运行 Zed 的前提下吃透消歧算法的输入输出关系。

**操作步骤**：

1. 运行 util crate 的消歧测试，确认算法行为锁定：

   ```bash
   cargo test -p util disambiguate
   ```

2. 手动模拟：设三条路径（已排序去重）
   `[/home/me/code/zed, /home/me/code/roc, /home/me/worktrees/zed/focal-arrow/zed]`，按算法逐轮推演每条路径的 detail。
3. 用 `path_suffix` 验证最终三者的显示后缀应分别为 `code/zed`、`roc`、`focal-arrow/zed`——两个 `zed` 都升到了 detail 1，但父目录不同，互不冲突。
4. 对照 [disambiguate.rs:154-201](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/util/src/disambiguate.rs#L154-L201) 的 `test_duplicate_paths_from_multiple_groups`，解释注释里说的「先去重再消歧」修复了什么。

**预期结果**：步骤 1 全绿（共 9 个测试）；步骤 2/3 的推演结果与 `assert_eq!(details, vec![1, 0, 1])` 类似的形态一致（注意输入顺序不同时 detail 的归属跟着变）；步骤 4 的答案——重复路径若不去重，`/home/me/code/zed` 出现两次会互相碰撞并一路把 detail 推到全路径长度，标签变得不可读。

### 4.4 活跃信息合并：apply_active_info 与通知记忆

#### 4.4.1 概念说明

数据库行是「死」的：标题停留在最后一次落库的时刻、状态字段是默认值。真实状态（正在运行、等待确认、标题生成中、diff 统计）活在当前进程的 `AgentPanel` 里。`all_thread_infos_for_workspace`（[sidebar.rs:7906](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7906-L7939)）从面板的会话视图里把这些信息抽成 `ActiveThreadInfo` 流。合并的桥是 **session_id**：每个 `ActiveThreadInfo` 带 session_id，每个数据库行的 `metadata.session_id` 若与之相等，就用活跃信息覆盖行对象。

「通知」是另一条主线：后台线程从 Running 变成 Completed 时，用户应当看到徽标。但「上一刻是否 Running」无法从当前状态推出——这正是记忆字段 `live_thread_statuses`（`HashMap<SessionId, (AgentThreadStatus, ThreadId)>`）存在的理由：每次重建结束时存下「本轮每个活跃会话的状态」，下一次重建时作为 `old_statuses` 参与跳变检测。

#### 4.4.2 核心流程

```text
展开分支（should_load_threads = true）：
  live_infos → live_info_by_session（同时数出 has_running_threads / waiting_thread_count）
  for thread in threads:
    若 metadata.session_id 命中 live_info_by_session:
      Arc::make_mut(thread).apply_active_info(info)   // 覆盖式合并
      new_live_statuses[session_id] = (status, thread_id)
    通知检测：
      status == Completed
      && 不是当前活跃线程（且活跃线程所在工作区 == 当前工作区）
      && old_statuses[session] 原本是 Running
      → notified_threads.insert(thread_id)
    若是当前活跃的非后台线程 → notified_threads.remove（看它时不算通知）
  threads 按 thread_display_time（interacted_at ?? updated_at）降序排序

折叠分支（分组折叠且无搜索词，线程行未加载）：
  仍从 live_infos 统计 running/waiting 计数
  thread_id 经 old_statuses 反查或 Store.entry_by_session 现查
  跳变检测照做 → notified_threads 照常插入（徽标在折叠的分组头上仍要亮）
```

#### 4.4.3 源码精读

**覆盖式合并**：[`apply_active_info`](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L379-L388) 是 `ThreadEntry` 的方法，一次性覆盖 title、status、icon、外部 SVG 图标、`is_live`、`is_background`、`is_title_generating`、`diff_stats` 八个字段。调用点在 [sidebar.rs:1720-1728](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1720-L1728)：`Arc::make_mut` 保证该行若与其他行共享 `Arc`（例如上一轮快照残留）则写时复制，不污染别人——这是 u2-l1 讲过的「零拷贝就地修补」。

**活跃信息索引与计数**：[sidebar.rs:1704-1716](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1704-L1716) 一趟循环同时完成三件事：建 session → info 的哈希表、数出 `has_running_threads`、数出 `waiting_thread_count`（分组头徽标的两项输入，见 u4-l2）。

**通知跳变检测**：[sidebar.rs:1730-1751](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1730-L1751)。`is_active_thread` 的判定是双重的：`active_entry` 匹配该 thread_id **且**该 entry 的工作区就是当前活跃工作区——即「用户正盯着它看」。检测条件（1738–1746 行）本质是一个三输入与门：新状态是 Completed、用户没在看、上一刻是 Running。1748–1750 行是反向清理：用户正在看的非后台线程永远不该带通知徽标。

**折叠支线**：[sidebar.rs:1758-1795](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1758-L1795)。分组折叠时线程行没被加载，但活跃信息仍然在流——thread_id 先从 `old_statuses` 反查、查不到再向 `ThreadMetadataStore::entry_by_session` 现查（1769–1777 行），保证 `new_live_statuses` 完整、跳变检测照常工作。这样即使用户折叠了分组，后台线程完成时分组头的通知徽标依然亮起。

**排序与压入**：组内排序在 [sidebar.rs:1753-1757](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1753-L1757)，键是 [`thread_display_time`](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5703-L5705)（`interacted_at` 优先于 `updated_at`）。随后 [`push_entries_by_display_time`](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5707-L5740) 把终端行与线程行**合流**成一条按时间降序的序列再 push：终端用 `created_at`、线程用 display_time、**空草稿被钉在 `DateTime::MAX_UTC`（永远置顶）**；同时顺手把可见行的 session/thread id 灌入 `current_*` 集合。

#### 4.4.4 代码实践

**实践目标**：把「通知三输入与门」和「折叠支线」两条逻辑链在源码里走通。

**操作步骤**：

1. 先跑通知产生的回归测试（本讲验证它存在即可，通知体系细节在 u9-l3 展开）：

   ```bash
   cargo test -p sidebar test_background_thread_completion_triggers_notification
   ```

2. 对照 [sidebar.rs:1738-1746](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1738-L1746)，写下三个条件各自防住哪种误报：（a）去掉 Completed 条件；（b）去掉 `!is_active_thread`；（c）去掉 old_statuses 检查。
3. 解释折叠支线 1769–1777 行为什么需要 `or_else` 两段式查询，且第一段优先。

**预期结果**：测试通过。问题 2 参考答案——（a）任何曾经活跃过的线程（哪怕手动停止后仍显示 Idle/Completed 之前的中间态）都会被记通知；（b）用户正在看的线程完成也弹徽标，属于噪音；（c）只要当前是 Completed 就记通知，哪怕它昨天就完成了、今天只是重建了一次列表，导致旧完成状态反复「被发现」。问题 3——`old_statuses` 是上一轮的记忆，零成本且能直接给出 thread_id；只有本进程新出现的会话才需要向 Store 现查，把数据库查询留给真正必要的少数情况。

### 4.5 草稿可见性判定与搜索过滤

#### 4.5.1 概念说明

草稿线程（`metadata.is_draft()`）没有用户起过的标题，不能像普通线程那样直接渲染，这一阶段决定「草稿行显不显示、显示成什么」；搜索过滤则是行进入最终 `entries` 前的最后一道塑形：查询词非空时，只有匹配的行（以及匹配的分组头）能通过。

#### 4.5.2 核心流程

```text
草稿判定（在线程收集之后、活跃合并之前）：
  所有行先带 draft = Some(WithContent)（构造闭包里的乐观默认）
  后处理：对每个 draft 行调 draft_display_label_for_thread_metadata：
    草稿提示词存储有内容 → (用户文本片段, WithContent)
    无内容               → (占位符文案, Empty)
  retain #1：draft 行必须有 title（非 draft 行直接保留）
  retain #2：Empty 草稿仅当「无 pending 激活 且 它就是面板当前线程」才保留

搜索过滤（query 非空的分组分支）：
  对分组标签 label 做 fuzzy 匹配 → workspace_highlight_positions
  对每个线程：标题命中 → 记 highlight_positions；
             任一 worktree 名命中 → 记位置并标记 worktree_matched
  对每个终端：标题 / worktree 名同样处理
  分组保留条件：workspace_matched || 任一线程命中 || 任一终端命中
  否则整个分组（含分组头）不进入 entries
```

#### 4.5.3 源码精读

**草稿标签推导**：[`draft_display_label_for_thread_metadata`](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L306-L328)。注意它拿的是 `ThreadEntryWorkspace`：只有 `Open` 工作区能给出 `workspace` 句柄去查草稿提示词存储，`Closed`（工作区已关）时传 `None`，只能得到占位符。返回的 `DraftKind` 区分「有内容的草稿」与「空草稿」。

**两道 retain**：[sidebar.rs:1671-1702](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1671-L1702)。1685 行第一道：推导不出标签的草稿直接不渲染（`retain(|thread| thread.draft.is_none() || thread.metadata.title.is_some())`）。1687–1702 行第二道带注释 "Keep empty drafts only while their thread is active"：空草稿只在「它是当前面板活跃线程」时显示——刚点完「新建线程」的那一刻它该出现在列表顶上；`pending_thread_activation.is_some()` 时（跨窗口激活正在进行，见 u6-l1）一律不保留，防止陈旧空草稿闪现。这个行为与 [`thread_metadata_would_render_sidebar_row`](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L330-L340)（供折叠时 `has_stored_thread_rows` 判定复用，[1797-1813](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1797-L1813)）保持一致：非草稿恒渲染，草稿看能否推导出标签。

**过滤分支**：[sidebar.rs:1815-1874](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1815-L1874)。匹配用从 `threads_archive_view` 借来的 [`fuzzy_match_positions`](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1817)，命中时把位置写回行对象（`Arc::make_mut` 再次登场）与 worktree 徽标。1871–1873 行：整个分组一无所获时 `continue`——**连分组头都不进列表**。这也是 4.1 流程图中「过滤在分组头压入之前」的原因。随后的分组头（1884–1894 行）带上 `workspace_highlight_positions`，让标签本身的命中字符也高亮。无查询分支（1903–1953 行）则简单得多：分组头无高亮、折叠时 `continue` 跳过行压入。

#### 4.5.4 代码实践

**实践目标**：用现有测试印证过滤链路，并手动推演草稿 retain 规则。

**操作步骤**：

1. 运行过滤测试并阅读：

   ```bash
   cargo test -p sidebar test_search_narrows_visible_threads_to_matches
   ```

2. 在测试中找到「键入查询词 → 列表收敛到匹配项」的断言，把它与本讲的过滤分支逐行对上（哪个断言对应 1871 行的「整组丢弃」？哪个对应标题 `highlight_positions`？）。
3. 手动推演：某分组有 3 个线程——A 普通线程、B `WithContent` 草稿、C `Empty` 草稿且正是面板当前线程，另有 `pending_thread_activation = Some(...)`。问两次 retain 后各线程去留。
4. 把 query 置为空串重新推演，验证走的是无查询分支。

**预期结果**：步骤 3——retain #1：三者都可能有 title（A 一定有；B、C 经后处理一定有标签），全部通过；retain #2：C 是 Empty 草稿，但 `pending_activation.is_some()` 为真 → **C 被丢弃**（1698–1700 行的判定先于「是否活跃线程」），A、B 保留。步骤 4——空查询时 `!query.is_empty()` 为假，走 1903 行起的 else 分支，无高亮计算、折叠分组直接跳过行压入。

#### 4.5.5 小练习与答案

**练习 1**：为什么空草稿要钉在 `DateTime::MAX_UTC` 置顶，而不是用创建时间参与排序？

答案：空草稿代表「用户刚点新建、还没打第一个字」的线程，交互语义上它就是当前操作的焦点，必须始终可见于列表顶部；若按 `created_at` 排序，一个刚创建的空草稿可能排在许多老线程之下，用户会以为什么都没发生（见 [`push_entries_by_display_time` 的 display_time](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L5714-L5723)）。

**练习 2**：搜索时匹配发生在哪些字段上？分组头本身可以命中吗？

答案：四个层面——分组标签（label）、线程标题、终端标题、以及任一 worktree 徽标名。可以：`workspace_matched` 为真时即使组内所有行都不命中，分组头（连同零行）也会保留并高亮标签（1839–1844 行的或条件包含 `workspace_matched`）。

**练习 3**：`notified_threads` 在一次重建中经历了哪三种操作？

答案：① 继承——从 `previous.notified_threads` 整体接管（1361 行）；② 增删——跳变检测插入（1745/1785 行）、活跃线程移除（1749/1793 行）；③ 收敛——收尾 `retain(|id| current_thread_ids.contains(id))` 把已消失行的通知丢弃（1956 行）。

## 5. 综合实践

**任务**：绘制 `rebuild_contents` 的完整阶段流程图，并标注每个阶段读取的全局 Store，最后用测试验证其中一个关键行为。这是本讲规格化的主实践，把 4.1–4.5 串成一张图。

**步骤**：

1. 画主干：阶段 0（快照采集）→ 阶段 1（跨分组准备）→ 分组循环（2a 终端收集 → 2b 线程收集 → 2c 草稿判定 → 2d 活跃合并与通知 → 2e 排序 → 2f 过滤 → 2g 压入）→ 阶段 3（收尾裁剪与装配）。
2. 在每个阶段节点旁标注数据来源，至少覆盖：
   - `MultiWorkspace::project_groups`（分组权威来源，[multi_workspace.rs:855-864](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/workspace/src/multi_workspace.rs#L855-L864)）；
   - `TerminalThreadMetadataStore::global` 与 `ThreadMetadataStore::global`（两路索引 × 四路查询）；
   - 各工作区 `AgentPanel`（活跃线程信息、终端通知）；
   - git 仓库快照（分支名）；
   - `agent_ui::draft_prompt_store`（草稿标签，经 4.5 的两个自由函数）；
   - 记忆字段 `live_thread_statuses` 与旧快照的 `notified_threads`。
3. 用两种颜色（或图例）区分「持久数据」与「进程内瞬态数据」，你会发现行的骨架来自前者、行的灵魂（状态/图标/统计）来自后者。
4. 验证：运行 `cargo test -p sidebar test_terminal_metadata_is_deduped_across_project_groups`，在图上标出该测试覆盖的路径（分组 A 路① + 分组 B 路② → `seen_terminal_ids` 去重 → 单行断言）。

**预期产物**：一张流程图 + 一份 Store 标注表。完成后你应当能不看源码回答：『一个开在 linked worktree 里的线程，从数据库到屏幕上的行，要经过哪几步、读哪几个 Store？』

## 6. 本讲小结

- `rebuild_contents` 是一次**单前向遍历 + O(T log T) 排序**的全量重推导：开头 `mem::take` 旧快照（只回收 `notified_threads`），结尾整体装配新 `SidebarContents`，不留半新半旧状态。
- 每个分组对线程和终端各做**四路查询**（main 路径正路、分组键 folder 兜底、逐打开工作区根路径、逐 linked worktree 路径），靠声明在分组循环之外的 `seen_thread_ids` / `seen_terminal_ids` 做**跨分组首次收录**去重。
- 分组标签撞名由 `compute_disambiguation_details` 逐级加深路径后缀消解（先排序去重再消歧），行内徽标由 `worktree_info_from_thread_paths` 结合 `branch_by_path`（从打开工作区的 git 快照现场收集）生成，多主项目时 linked 徽标会加 `项目名:` 前缀二次消歧。
- 活跃信息经 **session_id 桥**覆盖到行上（`apply_active_info` + `Arc::make_mut` 写时复制）；「Running→Completed 且用户没在看」的跳变靠记忆字段 `live_thread_statuses` 检测，折叠分组也走这条支线，保证分组头徽标照常亮。
- 草稿行经过两道 retain（有标签才渲染；空草稿仅在无 pending 激活且为当前线程时保留）；搜索过滤在分组头压入**之前**发生，整组无命中时连分组头一起丢弃，命中位置写回行与徽标用于高亮。

## 7. 下一步学习建议

数据流三部曲（u3-l1 事件订阅 → u3-l2 重建管线 → 本讲重建内容）到这里闭环。接下来两条路：

1. **向下看渲染**（u4-l1 渲染主骨架）：本讲产出的 `SidebarContents.entries` 如何被 `render()` 消费成真实 UI——粘性头部如何用 `project_header_indices`、行高如何用 `EntryShape` 保留（u3-l3 已铺垫）。
2. **横向看行为**（u5/u6）：`active_entry` 与本讲的活跃合并如何配合（u6-l1 线程激活全链路）、`pending_thread_activation` 为何能让空草稿消失（本讲 4.5 的伏笔在 u6-l1 展开）。

建议顺手重读 `rebuild_contents` 中 1620–1630 行那段关于「脏行」的注释——它是对「为什么不能只查一路」最好的反例说明，也是理解元数据写入路径（`handle_conversation_event` 的改写）为何重要的引子。
