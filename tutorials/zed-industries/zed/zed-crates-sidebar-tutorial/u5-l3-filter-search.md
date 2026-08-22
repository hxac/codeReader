# 过滤搜索：filter_editor 与高亮位置

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚从「用户在搜索框敲下一个字符」到「列表只剩匹配行且首行被选中」的完整链路：`EditorEvent::BufferEdited` → `schedule_update_entries(true)` → `update_entries` → `rebuild_contents` 的过滤支线 → `select_first_entry`。
2. 解释为什么非空查询会先 `take` 掉 `selection`，而空查询不会动它。
3. 读懂 `fuzzy_match_positions` 的真实匹配语义（连续子串、ASCII 大小写不敏感、返回**字节偏移**），不再被 "fuzzy" 这个名字误导。
4. 跟踪 `highlight_positions` 这个 `Vec<usize>` 如何从重建函数一路传到 `ThreadItem` / `HighlightedLabel`，并在终端行剥离图标前缀时被 `split_leading_icon_char` 重新映射。
5. 掌握退出搜索的三条路径（Escape 两段式、清除按钮、`Cancel` 兜底）中 `reset_filter_editor_text` 的作用，以及 `has_filter_query` 驱动的三处 UI 分支。

本讲承接 u5-l1（动作与键位上下文）与 u5-l2（键盘导航），聚焦搜索这一条交互支线；数据流上复用 u3-l2 讲过的重建管线，不重复其细节。

## 2. 前置知识

- **Editor 也是一个实体**：Zed 里的输入框不是 HTML 那种 `<input>`，而是完整的 `Editor` 实体（多光标编辑器的单行退化形态）。侧边栏用 `Editor::single_line` 造出搜索框，给它设占位文案。对它的一切监听都走实体订阅：`cx.subscribe(&editor, ...)`。
- **`EditorEvent::BufferEdited`**：编辑器缓冲区内容发生变化时发出的事件。用户敲键、粘贴、程序调用 `set_text` 都会触发它。这就是「搜索框内容变了」这一事实的唯一通知渠道。
- **selection vs active_entry**（u2-l3 已详述）：`selection` 是键盘焦点在扁平列表 `contents.entries` 中的下标，`Option<usize>`；`active_entry` 是全局当前打开的条目。本讲的「选中态差异」全部指前者。
- **重建管线**（u3-l2 已详述）：所有事件汇入 `schedule_update_entries(select_first_after_update, cx)`，由 `update_task` 合并去抖，最终执行 `update_entries` → `rebuild_contents` 从当前世界状态**全量重推导**列表。搜索过滤不是独立的过滤层，而是 `rebuild_contents` 内部的一个支线。
- **字节偏移 vs 字符下标**：Rust 的 `String` / `SharedString` 按 UTF-8 编码，`&s[i..j]` 切片用的是**字节**偏移。一个 `é` 占 2 字节，一个 emoji 可能占 4 字节。`highlight_positions` 里存的全是字节偏移，这是本讲反复出现的主题。
- **`HighlightedLabel`**：ui crate 提供的展示组件，构造时接收文本和一组下标，把这些位置的字符渲染成高亮色。搜索命中位置最终就是靠它画出来的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/sidebar.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs) | 主实现：`filter_editor` 的创建与订阅、`rebuild_contents` 过滤支线、高亮传递、`reset_filter_editor_text` / `has_filter_query` |
| [src/sidebar_tests.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs) | 搜索相关测试族与 `type_in_search` 辅助函数 |
| [crates/agent_ui/src/threads_archive_view.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/threads_archive_view.rs) | `fuzzy_match_positions` 的定义处（归档视图与侧边栏共用） |
| [crates/agent_ui/src/terminal_thread_metadata_store.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/terminal_thread_metadata_store.rs) | `terminal_title_prefix`：检测终端标题的装饰前缀（u4-l3 已讲，本讲复用） |
| [crates/ui/src/components/ai/thread_item.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs) | `ThreadItem` 与 `ThreadItemWorktreeInfo`，高亮位置的最终消费者 |

> 下方 sidebar.rs 的链接均以 `src/sidebar.rs` 相对路径书写，完整 URL 即上表第一行的 base 拼接 `src/sidebar.rs#L...`。

## 4. 核心概念与源码讲解

### 4.1 filter_editor：创建与 BufferEdited 订阅

#### 4.1.1 概念说明

`filter_editor` 是 `Sidebar` 结构体上的一个 `Entity<Editor>` 字段——侧边栏头部的搜索输入框。它解决的问题是：让用户在几十上百个线程/终端里快速定位目标。

关键设计决策：**搜索框自己不做任何过滤**。它只是一个文本状态容器；真正「列表该显示什么」的裁决发生在 `rebuild_contents` 里。搜索框的职责仅仅是「内容变了就喊一声」，这与其他十五类事件源（u3-l1）的地位完全平等——这正是「每次全量重推导」教条的体现：查询文本只是世界状态的一部分，`rebuild_contents` 每次都重新读它。

#### 4.1.2 核心流程

```text
用户敲键 / set_text
    │
    ▼
Editor 内部 buffer 变化
    │
    ▼
cx.subscribe(&filter_editor) 收到 EditorEvent::BufferEdited
    │
    ├─ 读取 query = filter_editor.text(cx)
    ├─ 若 query 非空 → this.selection.take()   ← 清掉键盘选中下标
    └─ schedule_update_entries(!query.is_empty(), cx)
           │
           ▼ （经 update_task 去抖合并）
    update_entries → rebuild_contents
           │             │
           │             └─ 读取 query，走过滤支线（4.2）
           ▼
    若 select_first_after_update == true
        → select_first_entry()   ← 选中首个 Thread/Terminal 行
```

#### 4.1.3 源码精读

创建搜索框——单行编辑器加占位文案：

[crates/sidebar/src/sidebar.rs:806-810](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L806-L810) 在 `Sidebar::new` 中创建 `filter_editor`：`Editor::single_line` 得到单行输入框，`set_placeholder_text("Search threads…")` 设置空态占位文案。

订阅定义在同文件的构造函数里：

[crates/sidebar/src/sidebar.rs:834-843](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L834-L843) 订阅 `filter_editor` 的 `EditorEvent::BufferEdited`：读出当前查询文本；若非空则 `this.selection.take()` 清空键盘选中下标；然后以 `!query.is_empty()` 作为 `select_first_after_update` 标志调度一次全量刷新。整个订阅 `detach`，与 `Sidebar` 实体同生命周期。

两个条件严格同源（都是 `!query.is_empty()`）：**只有非空查询才既清 selection 又要求刷新后选首行**。空查询（用户按退格删光、或程序清空文本）触发的是一次「普通」刷新，`selection` 原样保留——这一不对称的含义在 4.2.1 与综合实践里展开。

`rebuild_contents` 每次重新读取查询文本，而不是依赖事件回调里传参：

[crates/sidebar/src/sidebar.rs:1354](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1354) 重建函数开头 `let query = self.filter_editor.read(cx).text(cx);`——查询是重建时从编辑器现场读取的输入之一，与线程元数据、活跃信息同级的「世界状态」。

#### 4.1.4 代码实践

**实践目标**：亲眼看到每次键入都驱动一次带 `select_first_after_update = true` 的刷新。

**操作步骤**：

1. 打开 `crates/sidebar/src/sidebar.rs`，在 834 行的订阅回调里临时加两行日志（本地练习，观察完删除）：
   ```rust
   // 示例代码：临时调试日志
   log::info!("filter edited: query={:?} select_first={}", query, !query.is_empty());
   ```
2. 在仓库根目录运行单个测试驱动订阅：
   ```bash
   cargo test -p sidebar --lib test_search_narrows_visible_threads_to_matches
   ```
3. 阅读测试里的 `type_in_search` 辅助函数，注意它**没有**直接调用任何 sidebar 方法：
   [crates/sidebar/src/sidebar_tests.rs:4121-4129](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L4121-L4129) `type_in_search` 先把焦点给搜索框，再调用 `editor.set_text(query, ...)` 写入整段查询文本，最后 `run_until_parked` 等待异步任务收敛。`set_text` 改写了编辑器 buffer，于是 `BufferEdited` 事件走的是与真人敲键完全相同的订阅链路。

**需要观察的现象**：测试日志中查询 `"diff"` 与 `"nonexistent"` 各产生一次 `select_first=true` 的日志（`set_text` 整段替换，每个查询一次事件；具体日志条数与合并行为「待本地验证」）。

**预期结果**：测试通过；日志证明 `set_text` 与手动敲键共享同一条订阅路径。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `rebuild_contents` 要在函数内部重新读 `query`，而不是让订阅回调把查询文本作为参数传进刷新任务？

**答案**：刷新任务可能被合并、延迟，也可能由其他十五类事件源触发（那些事件不携带查询文本）。查询必须每次从世界状态（编辑器实体）现场读取，才能保证任何路径触发的重建都看到一致的查询。若靠参数传递，非搜索事件触发的重建就拿不到查询，过滤状态会丢失——这违反「全量重推导、不存派生状态」的架构约束。

**练习 2**：`EditorEvent::BufferEdited` 与焦点事件（focus/blur）有何分工？

**答案**：`BufferEdited` 只报告「内容变了」，驱动列表重建；焦点状态由 `dispatch_context`（[sidebar.rs:3240-3264](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3240-L3264)，u5-l1 已讲）在每帧现算为 `searching` / `not_searching` 上下文，决定键位优先级与行选中的视觉呈现。内容与焦点是两条独立的状态轴。

### 4.2 查询驱动的重建：过滤支线与匹配规则

#### 4.2.1 概念说明

`rebuild_contents`（u3-l4 已全景走读）在分组循环里按 `query` 是否为空走两条支线。非空支线对每个分组做**四路匹配**：

1. **工作区标签**（分组头显示名）——命中则整个分组连同头部保留；
2. **线程标题**——命中则该线程保留，并记录标题高亮位置；
3. **终端标题**——同上；
4. **worktree 名称**（行内徽标）——命中则该行保留，并记录徽标高亮位置。

保留规则是「或」：任意一路命中，行或分组就留下；四路全空则 `continue`，**连分组头一并丢弃**。这就是搜索时分组头会整块消失的原因。

「非空查询先 take 掉 selection」的理由在本支线里看得很清楚：过滤会重构整个 `entries` 数组——旧行被删、长度骤减。旧 `selection` 指向的下标在新数组里语义完全变了（可能指向另一行，也可能越界）。与其等刷新后再钳制，不如在源头清空，把「选中什么」的裁决权交给刷新后的 `select_first_entry()`——它必然落在**首个匹配行**上，恰好是用户搜索时最想要的落点。空查询则相反：列表恢复全量，多数场景下用户接下来要的是「回到列表继续导航」，所以 selection 不动，由退出路径（4.4）按需清理。

#### 4.2.2 核心流程

非空查询时每个分组执行：

```text
1. fuzzy_match_positions(query, 分组标签) → workspace_highlight_positions
2. 逐线程：匹配标题 → thread.highlight_positions
          匹配各 worktree 名 → worktree.highlight_positions
   任一命中 → 线程进入 matched_threads
3. 逐终端：匹配标题 / worktree 名 → matched_terminals
4. matched_threads 空 且 matched_terminals 空 且 !workspace_matched
       → continue（整组丢弃，含分组头）
5. 否则压入分组头（携带 workspace_highlight_positions）
   + push_entries_by_display_time(匹配行)
```

`fuzzy_match_positions` 的真实语义（名字叫 fuzzy，其实是**连续子串**匹配）：

- 把 query 与 candidate 都按字符展开；窗口数为 \( n - (m - 1) \)，其中 \( n \) 为候选字符数、\( m \) 为查询字符数；候选比查询还短时 `checked_sub` 溢出返回 `None`。
- 从左到右滑动长度等于查询长度的窗口，窗口内每个字符逐一 `eq_ignore_ascii_case` 比较——**要求查询字符连续且有序地出现**，不支持跳跃（`fcr` 匹配不了 `Fix crash`）。
- 大小写折叠仅限 ASCII（`eq_ignore_ascii_case`）；`é` 与 `É` 不视为相等。
- 命中返回首个窗口内各字符的**字节偏移**（来自 `char_indices` 的 `.0`），因此位置永远落在字符边界上。
- 空查询返回 `Some(vec![])`——视为「无条件命中但无高亮」。

#### 4.2.3 源码精读

[crates/agent_ui/src/threads_archive_view.rs:104-128](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/threads_archive_view.rs#L104-L128) `fuzzy_match_positions` 的定义：滑动窗口 + 逐字符 `eq_ignore_ascii_case` 比较的连续子串匹配；空查询返回空位置列表；命中时用 `char_indices` 收集窗口内字符的字节偏移。该函数是归档视图与侧边栏共用的公共工具（sidebar.rs 第 14-17 行从 `threads_archive_view` 导入）。

非空支线的线程匹配：

[crates/sidebar/src/sidebar.rs:1815-1845](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1815-L1845) 先对分组标签 `label`（在 [sidebar.rs:1541](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1541) 由 `group_key.display_name` 生成）做匹配得到 `workspace_highlight_positions`；再逐线程用 `Arc::make_mut` 就地写 `thread.highlight_positions`，同时遍历 `worktrees` 给命中的 worktree 徽标写高亮；三路（标签/标题/worktree）任一命中即收入 `matched_threads`。

终端匹配与之对称：

[crates/sidebar/src/sidebar.rs:1847-1869](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1847-L1869) 对终端标题与各 worktree 名做同样的匹配，命中写入 `terminal.highlight_positions`。

整组丢弃的判定：

[crates/sidebar/src/sidebar.rs:1871-1874](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1871-L1874) 若线程、终端、分组标签三路均无命中，`continue` 跳过本分组——分组头也不压入。

命中后压入分组头与行：

[crates/sidebar/src/sidebar.rs:1884-1902](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1884-L1902) 分组头携带 `highlight_positions: workspace_highlight_positions`（分组标签自身的命中高亮），行经 `push_entries_by_display_time` 按时间压入。注意即使零行命中、仅标签命中，分组头也照常保留（测试 `test_search_only_shows_workspace_headers_with_matches` 锁定该行为，[sidebar_tests.rs:4303](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L4303)）。

空查询支线的对照组：

[crates/sidebar/src/sidebar.rs:1903-1944](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1903-L1944) 空查询时分组头的 `highlight_positions` 恒为 `Vec::new()`；且 `is_collapsed` 时 `continue` 只跳过行、保留头。**关键细节**：这个 `is_collapsed` 守卫在 `else`（空查询）分支内部——搜索分支不受折叠影响，折叠分组里的线程命中时照样浮出（测试 [sidebar_tests.rs:4508-4552](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L4508-L4552)：折叠后搜索 "important"，线程与头都出现）。

`has_filter_query` 是查询非空性的统一判别函数：

[crates/sidebar/src/sidebar.rs:3343-3345](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3343-L3345) `has_filter_query` 即「编辑器文本非空」。它供渲染层多处使用（4.4.3），与 `rebuild_contents` 里直接读 `query` 是同一事实的两种取法。

`select_first_entry` 的落点规则：

[crates/sidebar/src/sidebar.rs:2149-2162](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2149-L2162) 优先选中第一个 `Thread` 或 `Terminal` 行（`position` 跳过分组头）；仅当列表非空却没有任何行条目时才退而选中下标 0（即分组头）。调度侧由 [sidebar.rs:1974-1990](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1974-L1990) 的 `schedule_update_entries` 保证：已有任务且不带 `select_first` 时早退合并；带 `select_first` 的请求**替换**旧任务（旧 `Task` 被 drop 即取消），刷新收尾在 `update_entries` 之后调用 `select_first_entry` 并 `cx.notify()`。

#### 4.2.4 代码实践

**实践目标**：用手算验证 `fuzzy_match_positions` 的「连续窗口」语义。

**操作步骤**：

1. 对下表每组输入，按 4.2.2 的算法手推结果（窗口逐字符比较，返回字节偏移）：

   | query | candidate | 手推结果 |
   | --- | --- | --- |
   | `"diff"` | `"Add inline diff view"` | ？ |
   | `"fix"` | `"Fix crash in panel"` | ？ |
   | `"fcr"` | `"Fix crash in panel"` | ？ |
   | `"FIX CRASH"` | `"Fix Crash In Project Panel"` | ？ |
   | `""` | 任意 | ？ |

2. 与源码 [threads_archive_view.rs:113-125](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/threads_archive_view.rs#L113-L125) 逐步核对。

**参考推演**：`"Add inline diff view"` 的字节下标为 A=0 d=1 d=2 ␣=3 i=4 n=5 l=6 i=7 n=8 e=9 ␣=10 d=11 i=12 f=13 f=14 ␣=15 v=16…；`"diff"` 四字符窗口在起点 11 处命中 d,i,f,f → `[11, 12, 13, 14]`。`"fcr"` 没有任何连续三字符窗口等于 f,c,r → `None`（行被丢弃）。大小写两行由 `eq_ignore_ascii_case` 命中；空查询返回 `Some(vec![])`。

**预期结果**：你的手推与源码逻辑一致；`"fcr"` 一例证明这不是跳跃式 fuzzy 匹配。

#### 4.2.5 小练习与答案

**练习 1**：搜索 "important" 时分组是折叠的，为什么线程还能出现？

**答案**：`is_collapsed` 的 `continue` 只存在于空查询的 `else` 分支（[sidebar.rs:1942-1944](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1942-L1944)）；搜索分支压入匹配行前没有折叠检查——搜索语义刻意压倒折叠语义，用户搜的就是「藏起来的东西」。

**练习 2**：搜索能命中哪些字段？分组头的 `has_threads` 汇总徽标受搜索影响吗？

**答案**：能命中分组标签、线程标题、终端标题、worktree 徽标名四类文本。`has_running_threads` / `waiting_thread_count` 等汇总是在匹配**之前**对全量 `threads` / `live_infos` 统计的（[sidebar.rs:1815](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1815) 之前已统计完毕），反映分组真实状态而非匹配子集。

**练习 3**：若某线程仅因 worktree 名命中而被保留，它的标题高亮位置是什么？

**答案**：空 `Vec`。`highlight_positions` 只在标题匹配成功时被赋值（[sidebar.rs:1826-1828](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1826-L1828) 的 `if let Some` 失败即跳过），行构造时初值就是 `Vec::new()`（如 [sidebar.rs:1469](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1469)）。高亮只画在真正命中的字段上。

### 4.3 highlight_positions：传递链路与前缀重映射

#### 4.3.1 概念说明

`highlight_positions: Vec<usize>`（字节偏移）出现在四个数据结构上：`ThreadEntry`（[sidebar.rs:359](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L359)）、`TerminalEntry`（[sidebar.rs:370](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L370)）、`ListEntry::ProjectHeader`（[sidebar.rs:396](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L396)）以及 ui crate 的 `ThreadItemWorktreeInfo`（[thread_item.rs:27-31](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L27-L31)）。

它是一条**从匹配算法直达像素的数据管道**，中途只有一处会改写它：终端行渲染时若标题带装饰前缀（如 `"$ "`、`">>> "`、emoji），前缀会被剥离并图标化，此时高亮位置必须**减去前缀字节长度**——落在前缀里的高亮直接丢弃，落在剩余标题里的整体左移。不改写的话高亮会画错字符。

这条重映射只发生在终端行；线程行与分组头把位置原样传给渲染组件。

#### 4.3.2 核心流程

```text
rebuild_contents（搜索支线）
    │ 写入 thread/terminal/header/worktree 的 highlight_positions（字节偏移）
    ▼
render_list_entry 按行类型分流
    ├─ ProjectHeader → render_project_header
    │      空位置 → Label；非空 → HighlightedLabel(文本, 位置)
    ├─ Thread → render_thread → ThreadItem.highlight_positions(原样)
    └─ Terminal → render_terminal
           ├─ split_leading_icon_char(标题, 位置)
           │     前缀存在 → (图标字形, 去前缀标题, 重映射位置)
           │     不存在 → (None, 原标题, 原位置)
           └─ ThreadItem.icon_char(图标字形).highlight_positions(位置)
```

重映射规则（字节偏移语义）：

\[ \text{adjusted} = \{\, p - L \mid p \in \text{positions},\ p \geq L \,\}, \quad L = \text{prefix.len()} \]

即：丢弃 \( p < L \)（落入前缀）的位置，其余左移 \( L \) 字节。

#### 4.3.3 源码精读

分组头：空位置与有位置走两种标签组件：

[crates/sidebar/src/sidebar.rs:2297-2307](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2297-L2307) `render_project_header` 里 `highlight_positions` 为空时用普通 `Label`，非空时换 `HighlightedLabel::new(label, positions)` 把命中字符染色；非活跃分组配 `Color::Muted`，透明窗口下截断。

线程行：位置原样透传：

[crates/sidebar/src/sidebar.rs:6176](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6176) `render_thread` 构造 `ThreadItem` 时 `.highlight_positions(thread.highlight_positions.to_vec())`——纯搬运，无改写。`ThreadItem` 侧的 builder 方法定义在 [thread_item.rs:162](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L162)。

终端行：先剥前缀再重映射：

[crates/sidebar/src/sidebar.rs:6489-6494](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6489-L6494) `render_terminal` 调 `split_leading_icon_char(&display_title, &terminal.highlight_positions)`：有前缀则三元组 `(icon_char, 去前缀标题, 重映射位置)`，无前缀则原样返回；随后 [sidebar.rs:6496-6504](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6496-L6504) 把 `icon_char` 喂给 `.icon_char(...)`、重映射位置喂给 `.highlight_positions(...)`。

重映射的实现：

[crates/sidebar/src/sidebar.rs:239-263](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L239-L263) `split_leading_icon_char`：先用 `terminal_title_prefix`（[terminal_thread_metadata_store.rs:96](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/terminal_thread_metadata_store.rs#L96)，扫描规则：遇字母数字即判定无前缀，遇「非字母数字字符后跟空白」则截出前缀）检测装饰前缀；再 `pick_icon_glyph` 选一个代表字形；然后按字节切片 `&title[stripped_len..]` 得到去前缀标题——正因切片是字节操作，位置重映射也必须是字节算术：`filter(p >= stripped_len).map(p - stripped_len)`。

字符边界的安全保证：

[crates/sidebar/src/sidebar_tests.rs:5146-5172](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L5146-L5172) `test_rename_thread_from_sidebar_updates_title_override` 的收尾：用多字节字符 `é` 作为查询后，断言线程的所有 `highlight_positions` 都满足 `title.is_char_boundary(position)`。这成立的原因有二：`fuzzy_match_positions` 用 `char_indices` 产出位置（天然落在字符边界），前缀长度 `stripped_len` 也是整字符累加的字节数（`terminal_title_prefix` 逐字符扫描）——边界减边界仍是边界。

顺带一提：渲染行的选中视觉有焦点门控（u5-l2 已述），搜索时焦点在编辑器上：

[crates/sidebar/src/sidebar.rs:2173-2175](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2173-L2175) `render_list_entry` 里 `is_selected = is_focused && self.selection == Some(ix)`，`is_focused` 是侧边栏根焦点句柄。敲键时焦点在搜索框内，行不会画选中边框——但 `selection` 状态确实存在，测试断言的 `<== selected` 标记读的正是这个状态。

#### 4.3.4 代码实践

**实践目标**：手动执行一次 `split_leading_icon_char` 的重映射。

**操作步骤**：

1. 对下面两组输入，按 4.3.2 的规则手推返回三元组（图标字形、去前缀标题、重映射位置）：

   | 标题 | 查询（先自行算出原始高亮位置） | 手推三元组 |
   | --- | --- | --- |
   | `"$ run tests done"` | `"test"` | ？ |
   | `">> do work"` | `">>"` | ？ |

2. 与 [sidebar.rs:239-263](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L239-L263) 核对；不确定 `pick_icon_glyph` 的字形选择时查 [sidebar.rs:273-304](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L273-L304)（u4-l3 已精读）。

**参考推演**：第一例中 `"$ "`（2 字节）是前缀；`"test"` 在原标题的字节 6-9 命中（`$`=0 ␣=1 r=2 u=3 n=4 ␣=5 t=6 e=7 s=8 t=9…）；重映射后 `6-2=4 … 9-2=7` → `[4,5,6,7]`，恰好指向 `"run tests done"` 里 `tests` 的前四个字符；图标字形为 `"$"`。第二例中前缀是 `">> "`（3 字节），`">>"` 的高亮 `[0,1]` 全部落在前缀内 → 被丢弃，重映射结果为空数组 `[]`——用户搜索的正是被图标化的部分，剩余标题无高亮可画，行为合理。

**预期结果**：两例手推均与源码规则一致；第二例体现「丢弃」分支。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `split_leading_icon_char` 只在终端行调用，线程行不调？

**答案**：装饰前缀是外部 CLI agent 给终端线程标题加的产物（`$ `、`>>>` 等提示符样式），线程标题由 Zed 自己生成或 LLM 总结，不带这类前缀。前缀图标化机制（u4-l3）本来就是为了处理「终端标题被外部装饰」的问题。

**练习 2**：若把重映射写成 `map(|p| p.saturating_sub(stripped_len))`（不 filter），会出现什么 bug？

**答案**：落在前缀内的高亮位置（如第二例的 `[0,1]`）会被钳到 0，把高亮错误地画在剩余标题的第一个字符上，而不是丢弃。`p - L` 对 `p < L` 下溢，`saturating_sub` 掩盖了这一语义错误——`filter` 丢弃才是正确表达。

**练习 3**：`ThreadItemWorktreeInfo.highlight_positions` 与 `ThreadEntry.highlight_positions` 是什么关系？

**答案**：前者挂在行内 worktree 徽标上（`ThreadEntry.worktrees: Vec<ThreadItemWorktreeInfo>` 的字段，[sidebar.rs:360](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L360)），由 worktree 名匹配写入；后者挂在线程标题上。一行可以同时有标题高亮与若干徽标高亮（[sidebar.rs:1829-1837](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1829-L1837) 的循环）。

### 4.4 退出搜索：reset_filter_editor_text、Escape 与清除按钮

#### 4.4.1 概念说明

进入搜索是被动驱动（键入即刷新），退出搜索则是**主动编排**：需要同时处理文本清空、`selection` 清理、焦点迁移三件事，而且不同入口的组合不同。`reset_filter_editor_text` 是共用的原子操作——「把编辑器文本清空，原本就为空则什么都不做并返回 false」。返回值让调用方区分「这次 Escape 真的清了查询」与「查询本来就没有，该做别的事（比如把焦点交还列表）」。

清空文本本身也会触发 `BufferEdited`（空查询 → `schedule_update_entries(false)` 排队），调用方随后又直接调 `update_entries` 立即重建——两条路径最终收敛到同一份列表，多排的一次任务在下一轮泵送时被守卫（`update_task` 已是 `None` 或被合并）消化。

#### 4.4.2 核心流程

三条退出路径：

```text
A. 搜索框内按 Escape（cancel，编辑器聚焦分支）
   重置文本成功？ ── 是 → selection = None；update_entries；留在搜索框
                └─ 否（本来就空）→ selection 为 None 则 select_first_entry
                                  → 焦点交给侧边栏列表

B. 列表内按 Escape（cancel，非编辑器分支）
   重置文本成功？ ── 是 → update_entries（selection 保留）
                └─ 否 → selection = None；焦点交给搜索框

C. 点击头部 "×" 清除按钮（仅 has_query 时显示）
   reset_filter_editor_text + update_entries（selection 不动）
```

#### 4.4.3 源码精读

原子操作本体：

[crates/sidebar/src/sidebar.rs:3332-3341](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3332-L3341) `reset_filter_editor_text`：缓冲区长度大于 0 才 `set_text("")` 并返回 `true`；已空则返回 `false`。`set_text` 触发的 `BufferEdited` 会带着空查询走 4.1 的订阅（不动 selection、不选首行）。

Escape 的两段式处理：

[crates/sidebar/src/sidebar.rs:3281-3311](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3281-L3311) `cancel`（`menu::Cancel` 的处理器）：先处理重命名进行中的情形；搜索框聚焦时（路径 A）先清文本——成功则清 `selection`、立即重建并返回（第一下 Escape 只清查询）；失败说明查询已空，此时若无选中则 `select_first_entry`，然后把焦点交给侧边栏根句柄（第二下 Escape 进列表）。非搜索框聚焦时（路径 B）同样先尝试清查询，但 `selection` 的处理相反：清成功时**保留** selection 只重建；清失败则清 selection 并把焦点送回搜索框。

清除按钮与 `has_filter_query` 的三处 UI 分支：

[crates/sidebar/src/sidebar.rs:7250-7265](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7250-L7265) 头部右侧：当 `selection` 存在且焦点不在搜索框时显示 `FocusSidebarFilter` 的键位提示（怎么回到搜索）；当 `has_query` 为真时显示 "×" 清除按钮，点击执行 `reset_filter_editor_text` + `update_entries`（路径 C，不清 selection）。

[crates/sidebar/src/sidebar.rs:7151-7170](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7151-L7170) `render_no_results`：同一块空态区域，`has_filter_query` 为真显示 "No threads match your search."，为假显示 "No threads yet"——文案随查询状态切换。

[crates/sidebar/src/sidebar.rs:7771-7772](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7771-L7772) 与 [sidebar.rs:7872-7874](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7872-L7874) `render` 主骨架中 `no_search_results = contents.entries.is_empty()` 决定是否把 `render_no_results` 挂到列表区之上（u4-l1 已讲布局，这里只看它依赖的布尔量来自查询过滤的结果）。

Enter 从搜索框进入列表：

[crates/sidebar/src/sidebar.rs:3459-3466](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3459-L3466) `editor_confirm`（由 [sidebar.rs:6553-6562](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6553-L6562) 的 `render_filter_input` 用 `capture_action` 截获编辑器的 `Newline` 触发）：无选中则先 `select_next`（等效选首行），有选中则把焦点交给侧边栏列表——配合 4.2 的 `select_first_entry`，形成「键入 → Enter → 直接操作首个结果」的动线。

#### 4.4.4 代码实践

**实践目标**：通过测试与代码对照，验证 Escape 的两段式语义。

**操作步骤**：

1. 运行：
   ```bash
   cargo test -p sidebar --lib test_escape_from_search_focuses_first_thread
   ```
2. 阅读 [sidebar_tests.rs:4230](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L4230) 起的该测试，对照 4.4.3 的路径 A 推演：第一次 Escape（有查询）发生什么，第二次 Escape（无查询）发生什么。
3. 思考题（下一小节有答案）：路径 B 与路径 C 都不清 `selection`，列表恢复全量后旧下标指向的行可能与过滤前不同，这算 bug 吗？

**需要观察的现象 / 预期结果**：测试通过；测试断言与路径 A 推演一致（具体断言内容「待本地验证」——讲义未运行测试，仅依据源码路径推演）。

#### 4.4.5 小练习与答案

**练习 1**：`reset_filter_editor_text` 为什么返回 `bool` 而不是直接 `()`？

**答案**：调用方需要区分「查询被清了」与「查询本来为空」来决定后续动作：路径 A 中前者直接返回、后者负责把焦点交给列表；路径 B 中前者只重建、后者清 selection 并回焦搜索框。返回值是这条两段式状态机的分支条件。

**练习 2**（4.4.4 思考题）：路径 B / C 不清 `selection` 是 bug 吗？

**答案**：是有意的取舍而非明显 bug。`selection` 的合法性由多道防线共同维护（u5-l2 讲过：转换点清空、`entries.get` 防御读取、导航归一化、焦点门控渲染）：即便旧下标在新列表里指向了别的行，它仍在合法范围内或被防御读取兜住，下一次导航动作会立即归一化。Escape-in-list 场景中用户刚在浏览列表，保留一个「大致位置」比强制回到首行更贴近预期；真要严格归零的是「从搜索框退出」的场景（路径 A），那里确实清了。

**练习 3**：点击 "×" 清除按钮后，`schedule_update_entries` 被调用几次？分别带什么标志？

**答案**：两次。一次来自 `set_text("")` 触发的 `BufferEdited` 订阅（空查询 → `schedule_update_entries(false)`，只排队）；一次是按钮处理器里直接调用的 `update_entries`（立即重建，不经调度）。两次最终都从「空查询」的世界状态重推导，结果幂等。

## 5. 综合实践

**任务**：完整梳理「键入字符 → BufferEdited → schedule_update_entries(true) → select_first_entry」链路，并回答「为什么非空查询要先 take 掉 selection」。

**步骤**：

1. **以测试为剧本**。通读 [test_search_narrows_visible_threads_to_matches](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L4131-L4184)（sidebar_tests.rs:4131-4184）：三条线程 → 键入 `"diff"` → 断言只剩 "Add inline diff view" 且带 `<== selected` → 键入 `"nonexistent"` → 断言列表为空。注意 `<== selected` 标记由断言辅助函数 [visible_entries_as_strings](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L547-L570)（sidebar_tests.rs:547 起）直接读 `sidebar.selection` 产生——它证明刷新后 selection 落在首个匹配行上。
2. **运行剧本**：
   ```bash
   cargo test -p sidebar --lib test_search_narrows_visible_threads_to_matches
   cargo test -p sidebar --lib test_search_then_keyboard_navigate_and_confirm
   ```
   第二个测试展示选中态在匹配结果间的导航（[sidebar_tests.rs:4554-4616](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L4554-L4616)）。
3. **画链路图**。以 `type_in_search`（[sidebar_tests.rs:4121-4129](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L4121-L4129)）为起点，把下列七站连成一张图，每站标注文件与行号：
   `set_text` → `BufferEdited`（sidebar.rs:834）→ `take` selection（:838）→ `schedule_update_entries(true)`（:840）→ 任务收尾 `select_first_entry`（:1983-1986 → :2149）→ `rebuild_contents` 过滤支线（:1815-1902）→ `visible_entries_as_strings` 读到 `<== selected`。
4. **回答 take 之问**。综合 4.1.3 与 4.2.1 写一段话，要点包括：(a) 过滤会重构 `entries`，旧下标语义失效甚至越界；(b) 非空查询必然伴随 `select_first_after_update = true`，刷新收尾的 `select_first_entry` 会确定性地把选中放到首个匹配行（优先 Thread/Terminal、跳过分组头），清空不是丢失而是移交裁决权；(c) 敲键时焦点在编辑器，行选中边框本就不显示（sidebar.rs:2173-2175 的焦点门控），清空没有视觉突变；(d) 空查询走对称的反面——不清、不选首行，把「回到哪里」留给退出路径（4.4）处理。
5. **（可选，本地验证）** 在 `schedule_update_entries` 入口与 `select_first_entry` 出口各加一条 `log::info!`，重跑步骤 2 的测试，对照日志与你的链路图；观察完毕删除日志。

## 6. 本讲小结

- 搜索框是纯文本状态容器：`BufferEdited` 订阅只做三件事——读查询、非空时 `take` selection、以 `!query.is_empty()` 为标志调度刷新；过滤的裁决完全发生在 `rebuild_contents` 的搜索支线（四路匹配：分组标签、线程标题、终端标题、worktree 名，任一命中即保留，全空连分组头一并丢弃）。
- `fuzzy_match_positions` 名为 fuzzy 实为**连续子串**匹配：定长窗口滑动、ASCII 大小写不敏感、返回 `char_indices` 产出的**字节偏移**（天然落在字符边界），空查询返回 `Some(vec![])`。
- `highlight_positions` 是从匹配算法直达像素的管道：分组头经 `HighlightedLabel` 染色，线程行原样透传给 `ThreadItem`，终端行在 `split_leading_icon_char` 剥离装饰前缀时被重映射——落入前缀的位置丢弃，其余左移前缀字节长度。
- 退出搜索有三条路径且编排各异：搜索框 Escape 两段式（先清查询再交焦点）、列表 Escape（清查询保 selection）、清除按钮（只清查询）；共用的 `reset_filter_editor_text` 以返回值区分「清了」与「本来就空」。
- `has_filter_query` 驱动三处 UI：无结果文案切换、清除按钮显隐、以及与 `render_no_results` 相连的空态分支。
- 搜索压倒折叠：`is_collapsed` 的 `continue` 只在空查询分支，折叠分组里的线程命中搜索时照常浮出。

## 7. 下一步学习建议

- 下一讲 u5-l4「行内重命名」继续交互支线：`thread_rename_editor` 的 `BufferEdited` 订阅与搜索框同构，但多了 `suppress_next_rename_edit` 防误判机制——对照本讲 4.1 的订阅形态阅读，体会「同一个事件、不同的状态机」。
- 想加深对匹配语义的理解，可阅读 `crates/agent_ui/src/threads_archive_view.rs` 中归档视图对 `fuzzy_match_positions` 的其他调用，以及 `crates/fuzzy`（真正的子序列模糊匹配 crate）与本函数的差别。
- 想看高亮的最终绘制，进入 `crates/ui/src/components/ai/thread_item.rs` 的 `HighlightedLabel` 组装路径，以及 `crates/ui` 中 `HighlightedLabel` 组件本身如何按字节偏移切分文本。
- 测试编写角度，可模仿本讲引用的搜索测试族，为「搜索命中 worktree 徽标」写一个新断言（参考既有 [test_search_matches_worktree_name](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L6382)，sidebar_tests.rs:6382）。
