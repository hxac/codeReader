# 线程行与终端行渲染：图标、状态与高亮

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `ThreadItem` 组件（位于 `ui` crate）暴露的常用 builder 方法，并理解它在 `render` 内部如何决定「图标槽」「标题槽」「元数据行」三个区域的最终内容。
2. 走读 `render_thread`，把它的每一个 builder 调用映射回 `ThreadEntry` 的字段或 `Sidebar` 的状态，并理解 `selected` / `focused` / `hovered` 三种行状态各自的视觉含义。
3. 走读 `render_terminal`，理解它与线程行的对称差异，特别是「标题装饰前缀图标化」这条独有路径。
4. 掌握 `split_leading_icon_char` 与 `pick_icon_glyph` 的两步拆分算法，能对任意标题手工推出：图标字符、裁剪后的标题、重映射后的高亮位置。
5. 理解特性开关 `agent-thread-worktree-label` 如何在**渲染期**（而非重建期）裁剪行上的 worktree/branch 标签。

## 2. 前置知识

- **RenderOnce 组件与 builder 模式**：`ThreadItem` 是实现 `RenderOnce` 的一次性组件——构造后立即被消费成元素树，不持有实体状态。所有外观参数通过同名 builder 方法链式设置（`.icon(...)`、`.timestamp(...)` 等），未设置的项取 `ThreadItem::new` 里的默认值。这是 GPUI 中「纯展示组件」的标准写法（见仓库 CLAUDE.md 的 GPUI 章节）。
- **行的三种状态**（承接 u2-l3、u4-l1）：
  - `is_active`：当前全局活跃条目（`active_entry` 匹配），对应 `ThreadItem::selected(...)` → 行背景高亮；
  - 键盘选中：`selection == Some(ix)` 且侧边栏持有焦点，对应 `ThreadItem::focused(...)` → 行边框；
  - `hovered`：鼠标悬停，对应行尾 `action_slot`（操作按钮）的出现。
- **高亮位置是 UTF-8 字节偏移**：搜索过滤产生的高亮位置是字符串的字节下标。`HighlightedLabel` 的文档明确写着 "Characters are identified by UTF-8 byte position"（[crates/ui/src/components/label/highlighted_label.rs:L16-L20](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/label/highlighted_label.rs#L16-L20)）。本讲 4.3 的位置重映射全部是字节运算。
- **字素簇（grapheme cluster）**：一个「用户感知的字符」可能由多个 Unicode 码点组成，例如旗帜 emoji 🇺🇸 是两个区域指示符码点、`é` 可以是字母加重音符的组合。按字节或按码点切分都会把这类字符切碎，所以 `pick_icon_glyph` 用 `unicode_segmentation` 的 `graphemes(true)` 取「第一个字素簇」。
- **特性开关（feature flag）**：Zed 用 `feature_flags` crate 在运行期下发开关值，`cx.flag_value::<T>()` 读取当前值；`watch` 让视图在开关变化时重渲染。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/sidebar/src/sidebar.rs` | 侧边栏本体。本讲涉及 `render_list_entry` 的分发（L2164-L2221）、`render_thread`（L6103-L6463）、`render_terminal`（L6465-L6551）、`split_leading_icon_char`（L235-L263）、`pick_icon_glyph`（L265-L304）、`apply_worktree_label_mode`（L664-L686） |
| `crates/ui/src/components/ai/thread_item.rs` | `ThreadItem` 行组件本体，连同 `ThreadItemWorktreeInfo`、`WorktreeKind`、`AgentThreadStatus` 的定义与渲染 |
| `crates/agent_ui/src/terminal_thread_metadata_store.rs` | `terminal_title_prefix`：标题装饰前缀的检测算法（`split_leading_icon_char` 的第一步） |
| `crates/agent_ui/src/threads_archive_view.rs` | `format_history_entry_timestamp`：把时间戳格式化成 `3m` / `2h` / `5d` 这类相对时间 |
| `crates/feature_flags/src/flags.rs` | `AgentThreadWorktreeLabel` 枚举与 `AgentThreadWorktreeLabelFlag` 开关定义 |
| `crates/sidebar/src/sidebar_tests.rs` | `test_split_leading_icon_char`（L14961-L15023）：前缀拆分算法的现有防回归测试，是本讲实践的对照物 |

## 4. 核心概念与源码讲解

### 4.1 ThreadItem：一行长什么样

#### 4.1.1 概念说明

`ThreadItem` 是 `ui` crate 提供的通用「AI 会话行」组件，侧边栏的线程行、终端行、线程切换器（u7-l1）和归档视图都复用它。它解决的问题是：把「一行会话数据」的所有视觉规则——图标、状态、标题、worktree 徽标、diff 统计、时间戳、悬停操作——收敛到一个地方，调用方只需按 builder 填参数，不必各自拼布局。

先认识两个附属类型。状态枚举只有四个值：

- [`AgentThreadStatus`](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L10-L17)（默认 `Completed`，另有 `Running` / `WaitingForConfirmation` / `Error`）——注意它定义在 `ui` crate 而非 agent 业务 crate，因为它是**展示概念**。
- [`ThreadItemWorktreeInfo`](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L19-L33)：一个 worktree 徽标的数据（名称、分支名、完整路径、各自的高亮位置、`WorktreeKind::Main/Linked`）。

#### 4.1.2 核心流程

`ThreadItem::render` 把一行画成**上下两条横向布局**，外加一个可选工具提示：

```text
┌────────────────────────────────────────────────┐
│ [图标槽] [标题槽………]        (悬停时: 操作槽) │   ← 主行（恒出现）
│ [占位]   归档图标·项目·路径·worktree徽标·±diff·时间 │   ← 元数据行（仅 has_metadata 时）
└────────────────────────────────────────────────┘
```

两个槽位都存在**优先级**，这是本组件最重要的规则：

- **图标槽**（渲染期决策，见 4.1.3）：
  1. `status == Running` → 旋转的 `LoadCircle`（加载动画）；
  2. `Error` → 红色 `Close`；`WaitingForConfirmation` → 黄色 `Warning`；否则 `notified == true` → 强调色圆点；
  3. 都不命中 → 「agent 图标」，其内部再分三层：`icon_char`（字符）> `custom_icon_from_external_svg`（外部 SVG）> `icon`（`IconName` 枚举）。
- **标题槽**：
  1. `title_slot`（调用方塞进来的任意元素，例如重命名编辑器）；
  2. `title_generating == true` → 呼吸灯动画的 Label；
  3. `highlight_positions` 非空 → `HighlightedLabel`（搜索命中高亮）；
  4. 以上皆否 → 普通 `Label`。
- **元数据行**只在 `has_metadata`（有项目名 / 项目路径 / linked worktree 徽标 / diff 统计 / 时间戳任一项）时渲染；worktree 徽标只显示 `WorktreeKind::Linked` 的条目，且「名称或分支至少有一个」。

#### 4.1.3 源码精读

先看字段全景——[ThreadItem 结构体](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L35-L67)一共 28 个字段，几乎每个都对应一个同名 builder；[ThreadItem::new](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L69-L103) 给出全部默认值（默认图标 `IconName::ZedAgent`、`is_truncated: true`、`status: Completed`）。

图标槽的三层 agent 图标——`icon_char` 优先于一切常规图标：

- [thread_item.rs:L301-L316](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L301-L316)：依次尝试 `icon_char`（直接当 Label 渲染字符）→ 外部 SVG → `IconName`。builder 文档也写明 `icon_char` "Takes precedence over `Self::icon` and `Self::custom_icon_from_external_svg`"（[L115-L120](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L115-L120)）。

状态图标对图标槽的覆盖：

- [thread_item.rs:L318-L353](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L318-L353)：这段先算出 `status_icon`（Error 的红叉 / Waiting 的警告 / 通知圆点），然后做最终裁决——`Running` 用旋转图标，其次 `status_icon`，都无才是 agent 图标。**也就是说：运行中的线程行上你看不到 agent 图标，图标槽被 spinner 占据。**

标题槽四分支与元数据行：

- [thread_item.rs:L355-L381](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L355-L381)：`title_slot` > 生成中脉冲 > `HighlightedLabel`（有高亮位置时）> 普通 `Label`。非不透明窗口下改为截断而非渐隐（注释解释了原因）。
- [thread_item.rs:L412-L419](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L412-L419)：worktree 徽标先过滤出 `Linked` 且名称/分支非空的条目。
- [thread_item.rs:L544-L550](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L544-L550)：徽标图标的选择——只显示分支时用 `GitBranch`，否则用 `GitWorktree`（注释说明 worktree 图标「同时覆盖」两种语义）。
- [thread_item.rs:L589-L606](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L589-L606)：diff 统计（`DiffStat` 组件）与时间戳的渲染，各片段之间用 `•` 分隔。

悬停操作槽与选中/聚焦的视觉差异：

- [thread_item.rs:L460-L484](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L460-L484)：`action_slot` 只在 `hovered` 时挂载，并带一层渐隐遮罩盖住底下被截断的文字；[L437-L443](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L437-L443) 显示 `selected` 决定背景色、`focused` 决定边框色——这正是 4.2 讲的「活跃 = 背景、键盘选中 = 边框」的落点。

一个诚实的观察：`is_remote` 字段目前只有存储与 builder（[L61](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L61)、[L202-L205](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L202-L205)），当前 `render` 实现里**没有**消费它——侧边栏照常传入，但行上并没有「远程」徽标。读源码时不要假设每个 builder 都有视觉对应。

#### 4.1.4 代码实践

1. **实践目标**：建立「字段 → 消费点」的对照能力，识别未被消费的字段。
2. **操作步骤**：打开 `crates/ui/src/components/ai/thread_item.rs`，从 L36 的结构体开始，对每个字段在 `impl RenderOnce for ThreadItem`（L251 起）里搜索 `self.<字段名>`，做一张三列表格：字段名 / builder 方法 / 渲染中的消费点（行号或「未消费」）。
3. **需要观察的现象**：`icon_char`、`status`、`title_slot`、`highlight_positions` 各自出现在哪几个分支里；`is_remote` 与 `archived` 是否出现在 `render` 中。
4. **预期结果**：能得出「图标槽与标题槽各有优先级链」的结论，并发现 `is_remote` 未被渲染消费。`archived` 会被消费（元数据行里的归档图标，L491-L497）。本实践为纯源码阅读，无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：一个 `status == Running` 且 `notified == true` 的线程行，图标槽最终显示什么？

答案：旋转的 `LoadCircle`。`Running` 判断在 [L340-L353](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L340-L353) 中优先于 `status_icon`（通知圆点只是 `status_icon` 的第三候选），所以通知圆点此时被 spinner 覆盖。

**练习 2**：为什么空草稿行通常只有一条主行、没有元数据行？

答案：元数据行受 `has_metadata` 门控（[L421-L425](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L421-L425)）。空草稿的时间戳被置为空字符串（见 4.2 的 `SharedString::default()`）、diff 统计为 0（不调用 `.added`/`.removed`），若无 worktree 徽标，则五个 `has_*` 全为 false，第二行不渲染。

**练习 3**：`ThreadItem` 为什么用 `RenderOnce` 而不是 `Render`？

答案：它是纯展示组件——构造即消费，不需要 `Entity<T>` 包装、不需要跨帧持有状态；所有交互（`on_click`/`on_hover`）以闭包注入。持有状态与订阅的是调用方（`Sidebar`），这符合 GPUI「状态实体 + 一次性元素树」的分工。

### 4.2 render_thread：把 ThreadEntry 拼成一行

#### 4.2.1 概念说明

`render_thread` 是线程行的「数据 → 组件参数」翻译层：输入是重建管线产出的 `ThreadEntry`（u2-l1 讲过：数据库元数据打底、活跃信息覆盖），输出是一个可点击、可悬停、可右键的 `ThreadItem`（必要时再包一层右键菜单）。它本身不画任何像素，所有布局都在 `ThreadItem` 里。

先复习输入。[`ThreadEntry`](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L348-L362) 中与本讲直接相关的字段：`icon` / `icon_from_external_svg`（图标两态）、`status`（状态枚举）、`draft`（草稿标记）、`highlight_positions`（搜索高亮）、`worktrees`（徽标）、`diff_stats`（diff 统计）、`is_title_generating`（标题生成中）。其中活跃字段由 [`apply_active_info`](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L373-L389) 从活的会话视图整体覆盖（`ActiveThreadInfo` 的来源在 [sidebar.rs:L7927-L7958](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7927-L7958)，图标来自会话视图、diff 统计来自 `action_log`）；已关闭线程的行则在重建时用 [`resolve_agent_icon`](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1376-L1388) 按 agent 身份推导（原生 agent → `ZedAgent`，自定义 agent → `Terminal`，外部扩展可提供 SVG 图标），diff 统计归零（[L1565-L1584](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L1565-L1584)）。

#### 4.2.2 核心流程

```text
render_list_entry(ix)                       ← 虚拟列表按需回调（L2164）
  ├─ ListEntry::Thread → render_thread(ix, thread, is_active, is_focused)   ← 本讲
  └─ ListEntry::Terminal → render_terminal(...)                              ← 4.3

render_thread 内部：
  1. 提取状态：通知、悬停、草稿、运行中、重命名中（L6111-L6132）
  2. 派生展示量：背景色、时间戳、远程标记、worktree 徽标（裁剪开关）、图标、标题生成中（L6134-L6161）
  3. ThreadItem builder 链：图标/状态/徽标/高亮/统计/选中/聚焦/悬停（L6163-L6195）
  4. 分支装配：重命名 → title_slot 装入编辑器；悬停 → action_slot 装入按钮组（L6196-L6310）
  5. on_click：清空 selection，按 Open/Closed 分流激活（L6311-L6333）
  6. 有 session_id 且非草稿 → 包一层右键菜单再返回（L6335-L6462）
```

#### 4.2.3 源码精读

分发入口——[sidebar.rs:L2164-L2221](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2164-L2221)：

- `render_list_entry` 先取条目、算 `is_focused`（侧边栏有焦点）与 `is_selected`（键盘选择器在此行）、`is_active`（`active_entry.matches_entry`，双钥匙匹配见 u2-l3），再按 `ListEntry` 三变体分发。注意调用处 `render_thread(ix, thread, is_active, is_selected, cx)` 的第四个参数在 `render_thread` 里形参名叫 `is_focused`——名字换了一次，语义没变。

状态提取与一个命名陷阱——[sidebar.rs:L6117-L6125](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6117-L6125)：

- 这里 `let is_selected = is_active;`——`ThreadItem::selected` 接的是**活跃**（背景高亮），不是键盘选中。随后 `.selected(is_selected).focused(is_focused)`（L6185-L6186）落到 4.1 讲的「背景 vs 边框」。阅读时务必小心这两行的命名，它们与 `render_list_entry` 里的同名词指代不同。

ThreadItem 主体装配——[sidebar.rs:L6163-L6187](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6163-L6187)：

- 图标两选一：草稿恒用 `IconName::Circle`（且 [L6166-L6168](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6166-L6168) 把它染成 20% 不透明度的淡色），非草稿用 `thread.icon` + 可选外部 SVG（`.when_some(icon_svg, ...)`）。
- 时间戳：空草稿给空串（第二行随之消失），否则 `format_history_entry_timestamp`（[threads_archive_view.rs:L1019-L1040](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/threads_archive_view.rs#L1019-L1040)，产出 `3m`/`2h`/`5d`/`3w`/`2mo`）。
- 通知、diff 统计（仅在 >0 时调用 `.added`/`.removed`）、高亮位置逐一直通。

重命名与悬停操作槽——[sidebar.rs:L6196-L6310](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6196-L6310)：

- 重命名时（[L6196-L6217](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6196-L6217)）用 `.title_slot(...)` 把标题替换为 `thread_rename_editor`，容器捕获 `Newline`/`Confirm`/`Cancel` 三个动作统一走 `finish_thread_rename`（状态机细节在 u5-l4）。
- 悬停时（[L6218-L6310](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6218-L6310)）装配 `action_slot`：铅笔按钮恒有；上下文按钮按状态分流——运行中 → Stop Generation；有内容的草稿 → Discard Draft；普通线程 → Archive；空草稿 → 无。

点击激活与右键菜单——[sidebar.rs:L6311-L6341](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6311-L6341)：

- `on_click` 先 `self.selection = None`（点击即清键盘选中，承接 u2-l3），再按 `ThreadEntryWorkspace::Open/Closed` 分流到 `activate_thread` 或 `open_workspace_and_activate_thread`（u6-l1）。
- [L6335-L6341](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6335-L6341) 是提前返回：草稿或无 `session_id` 的线程直接返回裸 `thread_item`，**不包右键菜单**——后面的菜单项（重新生成标题、以 Markdown 打开、归档）全都依赖 ACP 会话 id。

#### 4.2.4 代码实践

1. **实践目标**：把 `render_thread` 的每个 builder 调用映射回数据来源，建立「一行像素 ← 哪个字段」的完整对照。
2. **操作步骤**：阅读 [sidebar.rs:L6103-L6341](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6103-L6341)，手工填写下表（示例已给前两行）：

   | ThreadItem 调用 | 数据来源 |
   | --- | --- |
   | `.icon(IconName::Circle)` | 草稿特判（L6152-L6156） |
   | `.status(thread.status)` | `ThreadEntry.status`（活跃行由 `apply_active_info` 覆盖，L381） |
   | `.worktrees(...)` | ？（提示：`thread.worktrees` 经过了哪个函数？） |
   | `.timestamp(...)` | ？ |
   | `.highlight_positions(...)` | ？ |
   | `.notified(...)` | ？ |
   | `.added(...)` / `.removed(...)` | ？ |

3. **需要观察的现象**：哪些调用有条件（`.when`/`.when_some`）、哪些恒执行；`is_selected` 与 `is_focused` 分别接到哪个 builder。
4. **预期结果**：七个调用全部对上来源，并注意到 `worktrees` 经过了 `apply_worktree_label_mode`（见 4.4）、`notified` 查询的是 `contents.is_thread_notified`（跨重建继承的通知记忆，见 u2-l1）。本实践为源码阅读型，无需运行命令。

#### 4.2.5 小练习与答案

**练习 1**：悬停一个「运行中的普通线程」，行尾会出现哪些按钮？

答案：铅笔（Rename Thread，恒有）+ 红色的 Stop Generation（`is_running` 为真走 [L6245-L6256](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6245-L6256)）。Archive 按钮只在非运行、非草稿时出现。

**练习 2**：为什么线程行的元素 id 用行下标（`thread-entry-{ix}`），而终端行用 `terminal-{terminal_id}`？

答案：线程行 [L6132](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6132) 用 `format!("thread-entry-{}", ix)`，终端行 [L6473](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6473) 用 `terminal.metadata.terminal_id`。终端 id 天然稳定且全局唯一，拿来做元素 id 可避免排序变化时动画/状态错位；线程行当前选择了行下标，列表重排时 id 会跟着变——两处风格不一致，这正是「源码现状 ≠ 最优设计」的一个例证。

**练习 3**：`render_thread` 里 `hovered_thread_index` 同时服务线程行和终端行（终端行在 L6475 也读它）。这样共用有什么隐患？

答案：字段名暗示只属于线程，但两种行按同一个行下标 `ix` 写入/清除，语义上仍然自洽（一行只有一个悬停下标）；隐患主要是**可读性**——读代码的人容易误以为终端行有独立悬停状态。这也是仓库全词命名规则（CLAUDE.md）想避免的那类缩略遗留。

### 4.3 render_terminal 与标题装饰前缀图标化

#### 4.3.1 概念说明

终端行是线程行的「薄」版本：没有草稿、没有状态机、没有 diff 统计、没有右键菜单，图标恒为 `IconName::Terminal`。它的独特之处在于**标题装饰前缀图标化**：外部 agent（如 Claude Code、Codex 这类 CLI agent）启动的终端，标题常常自带装饰前缀——`"[!] codex waiting"`、`">>> Thinking"`、`"... working"`。直接显示既难看又浪费宽度，于是侧边栏把前缀剥出来、浓缩成单个字符，放进图标槽（`icon_char`，优先级高于 Terminal 图标），标题则从干净的部分开始。

这套处理分两步两个函数：`terminal_title_prefix`（在 `agent_ui` crate，检测「有没有前缀、前缀到哪」）→ `pick_icon_glyph`（在 sidebar crate，从前缀里挑一个代表性字符），外加一段高亮位置重映射，统一封装在 `split_leading_icon_char` 里。

#### 4.3.2 核心流程

第一步：[terminal_title_prefix](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/agent_ui/src/terminal_thread_metadata_store.rs#L96-L136) 判定前缀是否存在：

- 从头逐字符扫描：遇到**字母或数字** → 立即返回 `None`（标题以正常文字开头，没有装饰前缀）；
- 在见到任何前缀字符**之前**就遇到空白 → `None`（行首缩进不算前缀）；
- 见到非字母数字非空白的字符（符号、emoji）计入前缀；随后遇到空白即终止（连同后续连续空白一并计入），标记「前缀后确实有空白分隔」；
- 只有「前缀后跟了空白」才返回 `Some(&title[..prefix_byte_len])`——所以纯符号标题（`"✳"`）和无缝标题（`"✳Thinking"`）都不算。

第二步：[pick_icon_glyph](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L273-L304) 从前缀里挑一个字符：

1. `trim()` 后为空 → `None`；
2. 剥掉**一对**包裹的 ASCII 括号：首字符是 `[`/`(`/`{`/`<` 且能同时剥掉对应尾括号才生效，剥完 trim 后非空才采纳（`"[!]"` → `"!"`）；剥完变空（如 `"[ ]"` → `" "`）则**回退用原前缀**；
3. 若以 `".."` 开头 → 整段浓缩成省略号 `"…"`（U+2026）；
4. 否则取**第一个字素簇**（`graphemes(true)`），保证 🇺🇸 这类多码点 emoji 不被切碎；首字素 trim 后为空 → `None`。

第三步：[split_leading_icon_char](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L235-L263) 收尾：

- 前两步任一返回 `None`，或剥掉前缀后标题为空 → 整体 `None`（行按原样渲染）；
- 高亮位置重映射：设剥掉的前缀字节长度为 \( L \)，对每个位置 \( p \)：\( p \ge L \) 时保留并映射为 \( p' = p - L \)；\( p < L \)（落在前缀里）的丢弃。写成公式：

  \[ p' = p - L \quad (p \ge L), \qquad p \text{ 被丢弃} \quad (p < L) \]

#### 4.3.3 源码精读

`render_terminal` 中唯一独有的预处理——[sidebar.rs:L6489-L6494](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6489-L6494)：

- 拆分成功 → `icon_char = Some(...)`、标题用裁剪后的、高亮用重映射后的；失败 → 三者原样。随后 [L6496-L6504](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6496-L6504) 的 builder 链里 `.icon(IconName::Terminal)` 与 `.when_some(icon_char, |this, c| this.icon_char(c))` 并存——由 4.1 的优先级规则保证 `icon_char` 出现时盖过 Terminal 图标。

`split_leading_icon_char` 本体——[sidebar.rs:L239-L263](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L239-L263)：

- 依次调用前两步；注意 `stripped_len = prefix.len()` 是**字节**长度（`&title[stripped_len..]` 按字节切片，因为前缀边界落在字符边界上所以安全）；高亮过滤 + 平移在同一组迭代里完成。

`pick_icon_glyph` 的括号剥离与省略号浓缩——[sidebar.rs:L279-L303](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L279-L303)：

- 括号剥离只剥一层、且要求首尾同时匹配（`strip_prefix` + `strip_suffix` 链式 `.and_then`）；`"...` 判断在括号之后，所以 `"[...] working"` 先剥括号得到 `"..."` 再浓缩成 `"…"`。

终端行的其余部分与线程行对称——[sidebar.rs:L6505-L6549](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6505-L6549)：

- 时间戳恒取 `created_at`（无草稿概念）；`notified` 直通 `terminal.has_notification`（每行现算，见 u2-l1）；悬停操作槽只有一个 Close 按钮（L6516-L6536，点击走 `close_terminal`，链路见 u6-l2）；`on_click` 走 `activate_terminal_entry`；没有 `.status(...)`、没有右键菜单包装。

现有防回归测试（本讲实践的对照物）——[sidebar_tests.rs:L14961-L15023](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar_tests.rs#L14961-L15023)：

- `test_split_leading_icon_char` 覆盖了：`"✳ Implement separate config"` → `("✳", "Implement separate config")`；`">>> Thinking"` → `">"`；`"[!] codex waiting"` → `"!"`；`"... working"`、`"[...] working"`、`"[…] working"` 全部 → `"…"`；`"🇺🇸 flag"` → 国旗 emoji 完整保留；`"# abc"` + 位置 `[0, 2, 3]` → 位置 `[0, 1]`（前缀里的 0 被丢弃）；以及一批应返回 `None` 的反例。

#### 4.3.4 代码实践

1. **实践目标**：为 `pick_icon_glyph` 建立一组输入输出样例并推理结果，然后写一个表格驱动的单元测试验证推理。
2. **操作步骤**：
   - 第一步（推理，先不看测试）：填写下表。前三行已给答案，其余自行推出：

     | 输入（前缀） | 预期输出 | 推理要点 |
     | --- | --- | --- |
     | `"[!]"` | `Some("!")` | 剥方括号 → `"!"` |
     | `">>>"` | `Some(">")` | 非括号开头，不浓缩；取首字素 |
     | `"..."` | `Some("…")` | 以 `".."` 开头浓缩为省略号 |
     | `"🇺🇸"` | ？ | 字素簇切分 |
     | `"(WIP)"` | ？ | 圆括号剥离 |
     | `"[ ]"` | ？ | 剥括号后变空 → 回退原前缀 |
     | `"  "` / `""` | ？ | trim 后为空 |
     | `"✳"` | ？ | 普通符号原样 |

     参考答案：`Some("🇺🇸")`（两个区域指示符码点构成一个字素簇）；`Some("W")`；`Some("[")`（剥出 `" "` → trim 空 → 不采纳，回退 `"[ ]"` 取首字素 `[`）；`None` / `None`；`Some("✳")`。
   - 第二步（本地验证）：`pick_icon_glyph` 是 sidebar.rs 的私有函数，但 `sidebar_tests.rs` 是 crate 内的 `#[cfg(test)]` 模块，可以直接调用（现有 `test_split_leading_icon_char` 即如此）。在本地把下面的**示例代码**追加到 `sidebar_tests.rs`（练习用，勿提交），然后从仓库根目录运行：

     ```bash
     cargo test -p sidebar test_pick_icon_glyph_table
     ```

     ```rust
     // 示例代码：表格驱动测试（本地练习用，验证后请还原）
     #[test]
     fn test_pick_icon_glyph_table() {
         let cases: &[(&str, Option<&str>)] = &[
             ("[!]", Some("!")),
             (">>>", Some(">")),
             ("...", Some("\u{2026}")),
             ("🇺🇸", Some("🇺🇸")),
             ("(WIP)", Some("W")),
             ("[ ]", Some("[")),
             ("✳", Some("✳")),
             ("  ", None),
             ("", None),
         ];
         for (input, expected) in cases {
             assert_eq!(
                 pick_icon_glyph(input).as_deref(),
                 *expected,
                 "input: {input:?}"
             );
         }
     }
     ```

     （`pick_icon_glyph` 返回 `Option<SharedString>`，`SharedString` 解引用为 `&str`，所以用 `.as_deref()` 与 `Option<&str>` 直接比较。）
3. **需要观察的现象**：九个用例是否全绿；如果把 `("[ ]", Some("["))` 误写成 `Some(" ")` 或 `None`，哪一步推理出了偏差。
4. **预期结果**：全部通过即证明对「括号剥离的回退语义」与「字素簇切分」的理解正确。注意 `split_leading_icon_char` 的入口是 `terminal_title_prefix`，所以**整标题** `"[!]"`（无尾部空白）走不到 `pick_icon_glyph`；表格直接测 `pick_icon_glyph` 才能覆盖这个函数本身。上述命令与断言结果为「待本地验证」——本讲义编写环境未运行 cargo。

#### 4.3.5 小练习与答案

**练习 1**：整标题 `"[x] Done"` 经过 `split_leading_icon_char` 得到什么？

答案：`terminal_title_prefix` 检测出前缀 `"[x] "`（`x` 非字母数字？——注意 `x` **是**字母数字！）。因此第一步即返回 `None`，整行按原样渲染：图标槽是 Terminal 图标、标题原样、高亮位置不变。这道题的陷阱在于 `[` 和 `]` 是符号，但**括号内的字母**让扫描提前终止。

**练习 2**：标题 `"🇺🇸 flag"`、高亮位置 `[9]`（指向 `f`，国旗 8 字节 + 空格 1 字节），拆分后位置是多少？

答案：前缀 `"🇺🇸 "` 长度 \( L = 9 \) 字节，\( 9 \ge 9 \) 保留，\( p' = 9 - 9 = 0 \)，即 `[0]`——`f` 成为裁剪后标题 `"flag"` 的第 0 字节。若位置是 `[0]`（指向前缀里的国旗）则被丢弃。

**练习 3**：为什么 `split_leading_icon_char` 要在剥掉前缀后检查 `trimmed_title.is_empty()`？

答案：标题只有前缀（如 `"✳"`）时 `terminal_title_prefix` 本就不会返回 `Some`，但存在边界组合（前缀 + 纯空白尾部）可能使裁剪结果为空；此时返回 `None` 让行按原样渲染，避免出现「有图标没标题」的破行。[sidebar.rs:L248-L250](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L248-L250)

### 4.4 特性开关裁剪 worktree/branch 标签

#### 4.4.1 概念说明

线程行/终端行的元数据行里，每个 linked worktree 徽标默认同时显示「worktree 名 / 分支名」。`agent-thread-worktree-label` 开关允许运营侧在不下发新版本的情况下实验两种裁剪：只看 worktree、或只看分支。它的定义在 [feature_flags/src/flags.rs:L81-L100](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/feature_flags/src/flags.rs#L81-L100)：枚举 `AgentThreadWorktreeLabel { Both（默认）, Worktree, Branch }`，flag 名为 `"agent-thread-worktree-label"`，且 `enabled_for_staff() == false`。

关键设计判断：**裁剪发生在渲染期，而不是重建期**。`render_thread` / `render_terminal` 每次渲染时用 `cx.flag_value::<AgentThreadWorktreeLabelFlag>()` 读当前值（[L6147-L6150](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6147-L6150)、[L6483-L6486](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6483-L6486)），把完整的 `worktrees` 先拷贝再按模式清字段。数据模型（`ThreadEntry.worktrees`、`EntryShape`）完全不受影响——行集合与行高都不因开关而变。

#### 4.4.2 核心流程

```text
Sidebar::new → AgentThreadWorktreeLabelFlag::watch(cx)          ← 开关变化 → cx.notify()（仅重渲染）
每次渲染：
  thread.worktrees / terminal.worktrees（完整数据）
    → apply_worktree_label_mode(worktrees, cx.flag_value::<..>())
        ├─ Both      → 原样
        ├─ Worktree  → 每项 branch_name = None
        └─ Branch    → 有 branch_name 的项 worktree_name = None
                     （无分支名的项保留 worktree 名作回退）
    → ThreadItem::worktrees(...)
        → 渲染时只显示 Linked 且名称/分支非空的徽标
```

#### 4.4.3 源码精读

- [`apply_worktree_label_mode`](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L664-L686)：三种模式的就地清空。`Branch` 分支里的注释值得读——「无分支名时回退显示 worktree 名，一个空徽标比一个错位的图标更糟」，所以只在 `branch_name.is_some()` 时才清 `worktree_name`。
- [`AgentThreadWorktreeLabelFlag::watch` 的效果](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/feature_flags/src/feature_flags.rs#L134-L142)：`watch` 的实现就是 `cx.observe_global::<FeatureFlagStore>(|_, cx| cx.notify()).detach()`——开关一变只触发重渲染，不触发 `schedule_update_entries`。侧边栏在构造期注册（[sidebar.rs:L804](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L804)）。
- 与 ThreadItem 侧的衔接：`Worktree` 模式清掉 `branch_name` 后，徽标只剩 worktree 名 + `GitWorktree` 图标；`Branch` 模式清掉 `worktree_name` 后，4.1 讲的 [chip_icon 规则](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/components/ai/thread_item.rs#L544-L550)会把徽标图标换成 `GitBranch`——同一份开关语义贯穿两个 crate。

#### 4.4.4 代码实践

1. **实践目标**：理解「渲染期裁剪」与「重建期状态」的边界。
2. **操作步骤**：通读 [apply_worktree_label_mode](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L664-L686)，然后回答：如果把这段裁剪搬进 `rebuild_contents`（在构造 `ThreadEntry` 时就清字段），会带来哪些问题？把答案写成两三条要点。
3. **需要观察的现象**：对照 u3-l3 的 `EntryShape` 契约思考——裁剪会不会改变行高？会不会需要在 `EntryShape` 里加字段？
4. **预期结果**：要点应包括：(a) 数据被污染，换回 `Both` 模式需要整表重建才能恢复，而渲染期方案只差一次 `cx.notify()`；(b) `EntryShape` 无需变化，因为裁剪不影响行高与行集合；(c) 重建管线「从世界状态全量重推导」的世界里，开关值并不属于会触发重建的事件源，放进重建反而要新增一条订阅。本实践为源码阅读型，无需运行命令。

#### 4.4.5 小练习与答案

**练习 1**：`Branch` 模式下，一个只知道 worktree 名、不知道分支名的徽标会显示什么？

答案：原样显示 worktree 名 + `GitWorktree` 图标。因为 [`Branch` 分支只在 `branch_name.is_some()` 时清 `worktree_name`](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L675-L683)，避免出现只有图标的空徽标。

**练习 2**：开关从 `Both` 切到 `Worktree` 的瞬间，侧边栏内部发生了什么？

答案：`FeatureFlagStore` 全局变化 → `watch` 注册的观察者调用 `cx.notify()` → `Sidebar` 重渲染 → `render_thread`/`render_terminal` 再次 `cx.flag_value` 读到 `Worktree` → `apply_worktree_label_mode` 清掉 `branch_name`。没有 `update_entries`、没有 `ListState::splice`、没有 `EntryShape` 变化。

## 5. 综合实践

**任务：给一行线程画「完整画像」——从字段到像素。**

构造（或从现有测试里找一个）满足以下条件的线程行：标题为 `"✳ Implement separate config"`、`status = Running`、`diff_stats = {lines_added: 12, lines_removed: 3}`、`notified = true`、有一个 linked worktree（名称 `feature-x`，分支 `main`）、搜索过滤词命中标题里的 `config`。然后：

1. 手推 `render_thread` 的完整参数表：`ThreadItem` 收到的 `icon` / `icon_char` / `status` / `notified` / `added` / `removed` / `worktrees` / `highlight_positions` / `timestamp` 各是什么；
2. 再手推 `ThreadItem::render` 的三个裁决：图标槽落到哪一层（提示：`Running` 优先）、标题槽落到哪个分支（有高亮位置）、元数据行出现哪些片段、徽标图标是 `GitWorktree` 还是 `GitBranch`；
3. 用 4.3.4 的表格驱动测试验证你对字符处理的理解，并把 `"✳ Implement separate config"` 加进 `test_split_leading_icon_char` 风格的断言（注意：线程行走的是 `render_thread`，**不经过** `split_leading_icon_char`——这个函数只用于终端行；想验证这一点，grep 一下它的全部调用点）；
4. 若本地能编译，任选一个现有测试运行对照（如 `cargo test -p sidebar test_split_leading_icon_char`），确认环境可跑后，再看 `visible_entries_as_strings` 类测试如何断言行的可见文本。

预期产出：一张两列对照表 + 三条裁决结论。第 3 步会纠正一个常见误解：前缀图标化是**终端行专属**逻辑，线程行的 `✳` 会原样留在标题里（其图标槽由 agent 身份或状态决定）。命令运行结果为「待本地验证」。

## 6. 本讲小结

- `ThreadItem` 是 `ui` crate 的通用会话行组件：一行 = 主行（图标槽 + 标题槽 + 悬停操作槽）+ 可选元数据行；图标槽与标题槽都有明确的优先级链，`Running` 状态会整体接管图标槽。
- `render_thread` 是纯翻译层：把 `ThreadEntry` 字段与 `Sidebar` 状态映射成 builder 参数；注意 `is_selected = is_active` 的命名陷阱（背景高亮 = 活跃，边框 = 键盘选中）。
- `render_terminal` 更薄，独有「标题装饰前缀图标化」：`terminal_title_prefix`（检测：符号前缀 + 空白分隔）→ `pick_icon_glyph`（剥一层括号 → `..` 浓缩为省略号 → 取首字素簇）→ 高亮位置按字节平移并丢弃落入前缀的位置。
- 高亮位置是 UTF-8 字节偏移，前缀剥离后的重映射是纯字节运算；多码点 emoji 靠字素簇切分保持完整。
- `agent-thread-worktree-label` 开关在渲染期经 `apply_worktree_label_mode` 裁剪徽标标签，不触碰数据模型与 `EntryShape`；开关变化只触发 `cx.notify()`。

## 7. 下一步学习建议

- 下一讲（u4-l4）继续渲染层的收尾：分组头省略号菜单、`workspace_menu_worktree_labels` 的主/次标签规则与 `DefaultBranchCache` 预取——其中 `WorkspaceMenuWorktreeLabel` 与本讲的 `ThreadItemWorktreeInfo` 是平行结构，可对照阅读。
- 之后进入单元五（u5-l1 动作注册与键位上下文）：本讲行上的 `on_click`/悬停按钮最终调用的 `activate_thread`、`archive_thread`、`finish_thread_rename` 等方法，都同时挂在动作处理器上，届时可以看到「鼠标路径」与「键盘路径」如何汇合。
- 想加深字符处理的理解，可以延伸阅读 `crates/ui/src/components/label/highlighted_label.rs`（高亮位置如何在单行替换时随之平移）与 `unicode_segmentation` crate 的字素簇文档。
