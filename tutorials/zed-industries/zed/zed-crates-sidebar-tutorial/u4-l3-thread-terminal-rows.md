# 线程行与终端行渲染：图标、状态与高亮

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `ThreadItem` 组件（位于 `ui` crate）暴露的常用 builder 方法，以及它在 `render` 内部如何决定「图标槽」「标题槽」「元数据行」三个区域的最终内容。
2. 走读 `render_thread`，把它的每一个 builder 调用映射回 `ThreadEntry` 的字段或 `Sidebar` 的状态，并理解 `selected` / `focused` / `hovered` 三种行状态的含义。
3. 走读 `render_terminal`，理解它与线程行的对称差异，特别是「标题装饰前缀图标化」这条独有路径。
4. 掌握 `split_leading_icon_char` 与 `pick_icon_glyph` 的两步拆分算法，能对任意标题手工推出图标字符、裁剪后标题与重映射后的高亮位置。
5. 理解特性开关 `agent-thread-worktree-label` 如何在渲染期（而非重建期）裁剪 worktree/branch 标签。

## 2. 前置知识

- **RenderOnce 组件与 builder 模式**：`ThreadItem` 是一个实现了 `RenderOnce` 的「一次性组件」——构造后立刻被消费成元素树，不适合 `Render` 那样持有实体状态。它的所有外观参数都通过同名 builder 方法链式设置（如 `.icon(...)`、`.timestamp(...)`），未设置的项使用 `ThreadItem::new` 里的默认值。这与 CLAUDE.md 中 GPUI 章节对 `RenderOnce` 的描述一致。
- **三种行状态**（承接 u2-l3 与 u4-l1）：
  - `is_active`：当前全局活跃条目（`active_entry` 匹配），对应 `ThreadItem::selected(...)`（行背景高亮）；
  - 键盘选中：`selection == Some(ix)` 且侧边栏持有焦点，对应 `ThreadItem::focused(...)`（边框）；
  - `hovered`：鼠标悬停，对应 `action_slot`（行尾操作按钮）出现。
- **高亮位置是 UTF-8 字节偏移**：搜索过滤产生的高亮位置是字符串的字节下标，`HighlightedLabel` 的文档明确写着 "Characters are identified by UTF-8 byte position"（见 [crates/ui/src/components/label/highlighted_label.rs:L17-L20](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/label/highlighted_label.rs#L17-L20)）。本讲 4.4 的位置重映射全是字节运算。
- **特性开关（feature flag）**：Zed 用 `feature_flags` crate 在运行期下发开关值，`cx.flag_value::<T>()` 读取当前值。开关值变化会触发一次重渲染，但不会触发侧边栏的数据重建。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/sidebar/src/sidebar.rs` | 侧边栏本体。本讲涉及 `render_list_entry` 的分发（L2164-L2233）、`render_thread`（L6103-L6463）、`render_terminal`（L6465-L6551）、`split_leading_icon_char`（L235-L263）、`pick_icon_glyph`（L265-L304）、`apply_worktree_label_mode`（L664-L686） |
| `crates/ui/src/components/ai/thread_item.rs` | `ThreadItem` 行组件与 `ThreadItemWorktreeInfo`、`AgentThreadStatus` 的定义与渲染，文末还有一组可直接阅读的 `preview()` 示例 |
| `crates/agent_ui/src/terminal_thread_metadata_store.rs` | `terminal_title_prefix`（装饰前缀检测）与 `compose_terminal_thread_title`（标题组装） |
| `crates/agent_ui/src/thread_metadata_store.rs` | `worktree_info_from_thread_paths`：把线程存储的路径列表变成 `ThreadItemWorktreeInfo`（本讲的输入端） |
| `crates/feature_flags/src/flags.rs` | `AgentThreadWorktreeLabel` 枚举与 `AgentThreadWorktreeLabelFlag` 开关定义 |
| `crates/sidebar/src/sidebar_tests.rs` | 已有 `test_split_leading_icon_char`（L14961-L15023），是本讲实践的参照模板 |

## 4. 核心概念与源码讲解

### 4.1 ThreadItem：可复用的行组件

#### 4.1.1 概念说明

`ThreadItem` 不在 sidebar crate 里，而是 `ui` crate 提供的通用「agent 线程行」组件：一个圆角行，左边是图标槽，中间是标题（可高亮），右边是时间戳，下面可选一行元数据（项目名、worktree/branch 标签、diff 统计）。sidebar 的线程行、终端行，以及后面 u7 要讲的 `ThreadSwitcher` 弹层条目，用的都是它。

把行组件下沉到 `ui` crate 的好处：外观规则（状态图标优先级、渐变淡出、chip 排版）只写一份，消费方只负责「投影」——把自己的数据结构翻译成 builder 参数。

#### 4.1.2 核心流程

`ThreadItem::render` 内部按优先级填充三个区域：

```text
图标槽（三选一，按优先级）：
  1. status == Running        → 旋转加载图标（LoadCircle + 旋转动画）
  2. Error / WaitingForConfirmation / notified
                               → 红叉 / 警告 / 强调色圆点
  3. agent_icon：
       icon_char 有值          → 把该字符当图标渲染（Label）
       否则 custom_icon_from_external_svg 有值 → 外部 SVG 图标
       否则                    → Icon::new(self.icon)

标题槽（四选一，按优先级）：
  1. title_slot 有值           → 任意元素（侧边栏用它放行内重命名编辑器）
  2. title_generating          → 呼吸动画标题
  3. highlight_positions 为空  → 普通 Label
  4. 否则                      → HighlightedLabel(title, positions)

元数据行（任一存在才渲染第二行）：
  project_name / project_paths / linked worktree chip / diff 统计 / 时间戳
  注意：只有 kind == Linked 的 worktree 才渲染成 chip，Main 的不显示
```

#### 4.1.3 源码精读

先看数据结构与状态枚举：

- [crates/ui/src/components/ai/thread_item.rs:L10-L17](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/ai/thread_item.rs#L10-L17)：`AgentThreadStatus` 四态枚举（Completed / Running / WaitingForConfirmation / Error），这是行状态徽标的全部来源。
- [crates/ui/src/components/ai/thread_item.rs:L26-L33](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/ai/thread_item.rs#L26-L33)：`ThreadItemWorktreeInfo`，含 `worktree_name`、`branch_name`、`full_path`、`highlight_positions`、`kind`（Main/Linked）。sidebar 在重建时生成它（见 4.2.3 的输入端说明）。
- [crates/ui/src/components/ai/thread_item.rs:L35-L67](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/ai/thread_item.rs#L35-L67)：`ThreadItem` 结构体字段全景——图标三态（`icon` / `icon_char` / `custom_icon_from_external_svg`）、标题四态（`title` / `title_slot` / `title_generating` / `highlight_positions`）、diff 统计（`added` / `removed`）、交互回调（`on_click` / `on_hover` / `action_slot`）。

图标槽的优先级实现：

- [crates/ui/src/components/ai/thread_item.rs:L300-L316](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/ai/thread_item.rs#L300-L316)：`agent_icon` 的三级选择——`icon_char` 优先，其次外部 SVG，最后才是 `IconName`。`icon_char` 的 builder 文档也写明它「Takes precedence over `Self::icon` and `Self::custom_icon_from_external_svg`」（[L115-L120](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/ai/thread_item.rs#L115-L120)）。
- [crates/ui/src/components/ai/thread_item.rs:L318-L353](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/ai/thread_item.rs#L318-L353)：状态图标覆盖逻辑——Running 直接换成旋转加载图标；Error/Waiting/通知圆点只在没有更高优先级状态时顶替 `agent_icon`。**注意**：通知圆点也会覆盖掉终端行的 `icon_char`，这是后面 4.3 的一个关键细节。

标题槽的实现：

- [crates/ui/src/components/ai/thread_item.rs:L355-L381](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/ai/thread_item.rs#L355-L381)：四选一的标题渲染。`title_slot` 是任意元素（侧边栏重命名时放编辑器）；`title_generating` 用 2 秒周期、透明度在 0.4~0.8 间脉动的动画；有高亮位置时用 `HighlightedLabel`。非不透明窗口下放弃渐变淡出改用截断（承接 u4-l1 讲过的窗口外观判断）。

元数据行的实现：

- [crates/ui/src/components/ai/thread_item.rs:L412-L419](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/ai/thread_item.rs#L412-L419)：只保留 `kind == WorktreeKind::Linked` 且至少有名字或分支的 worktree——**Main worktree 的信息在行内根本不渲染**（组件 preview 里专门有一个 "Main Worktree (hidden)" 示例印证这一点，见 [L789-L807](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/ai/thread_item.rs#L789-L807)）。
- [crates/ui/src/components/ai/thread_item.rs:L541-L558](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/ai/thread_item.rs#L541-L558)：chip 的图标选择——当 `worktree_name` 被清空只剩 `branch_name` 时用分支图标 `GitBranch`，否则用 worktree 图标 `GitWorktree`。这正是 4.2 里特性开关 `Branch` 模式产生效果的落点。
- [crates/ui/src/components/ai/thread_item.rs:L594-L606](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/ai/thread_item.rs#L594-L606)：diff 统计徽标（`DiffStat`，绿增红删）与时间戳，均以 `•` 圆点分隔。

悬停与选中态：

- [crates/ui/src/components/ai/thread_item.rs:L437-L443](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/ai/thread_item.rs#L437-L443)：`selected` 上背景色、`focused` 上聚焦边框、`hover` 上悬停背景——三个状态互相独立。
- [crates/ui/src/components/ai/thread_item.rs:L460-L484](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/ai/thread_item.rs#L460-L484)：仅在 `hovered` 时渲染 `action_slot`，前面垫一层 `GradientFade` 让按钮从文字下「浮」出来，并对鼠标按下 `stop_propagation`，避免点按钮误触行点击。

一个值得注意的观察：`is_remote` 字段只有存储与 setter（[L61](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/ai/thread_item.rs#L61)、[L202-L205](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/ai/thread_item.rs#L202-L205)），当前 `render` 并未消费它——传了 `is_remote` 今天不会有任何视觉效果。

#### 4.1.4 代码实践

1. **实践目标**：不运行任何代码，仅通过阅读 `preview()` 示例与 `render` 实现，确认「图标槽优先级」和「Main worktree 隐藏」两条规则。
2. **操作步骤**：
   - 阅读 [crates/ui/src/components/ai/thread_item.rs:L647-L987](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/components/ai/thread_item.rs#L647-L987) 的 `Component::preview`，找出名为 "Waiting for Confirmation"、"Error"、"Running Agent"、"Main Worktree (hidden)" 的四个示例。
   - 对每个示例写下你预期的图标槽内容，然后对照 L300-L353 的优先级代码验证。
3. **需要观察的现象**：`Waiting for Confirmation` 与 `Error` 示例都没有调用 `.icon(...)` 之外的覆盖，但图标槽显示的是警告/红叉而非默认的 `ZedAgent` 图标——因为状态图标优先级更高。
4. **预期结果**：四个示例的图标槽依次为：警告图标（Warning）、红叉（Close）、旋转加载图标（LoadCircle）、默认 agent 图标（Main worktree 的 chip 不出现，元数据行只剩 diff 统计与时间戳）。
5. 运行效果可通过 macOS 上的 visual test_runner 观察（承接 u1-l2：`zed_visual_test_runner` 仅限 macOS）；在其他平台本实践为纯源码阅读型，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：一个线程行同时满足「状态为 Running」且「notified 为 true」，图标槽显示什么？
**答案**：旋转加载图标。Running 检查在 L340 最先发生，直接返回 spinner，通知圆点只在非 Running、非 Error、非 Waiting 时才顶替 `agent_icon`。

**练习 2**：为什么 `ThreadItem` 要提供 `title_slot` 这样一个「任意元素」槽位，而不是只提供字符串标题？
**答案**：行内重命名需要把标题临时替换成一个真正的 `Editor` 实体（可输入、可聚焦、要捕获 Enter/Esc）。字符串 API 表达不了「这一格放一个交互组件」，所以留了 `AnyElement` 槽。

**练习 3**：`ThreadItemWorktreeInfo` 同时携带 `worktree_name` 与 `branch_name`，谁决定它们最终显示成什么样的 chip？
**答案**：消费方决定。`ThreadItem::render` 负责排版（名字 + `/` + 分支 + 图标选择），而「是否保留名字/分支」由 sidebar 的 `apply_worktree_label_mode` 在传入前裁剪（见 4.2.3）——组件只管画，不管策略。

### 4.2 render_thread：把 ThreadEntry 投影成 ThreadItem

#### 4.2.1 概念说明

`render_thread` 是纯粹的「投影函数」：输入一个重建好的 `ThreadEntry`（承接 u2-l1 的行模型、u3-l4 的重建产物），输出一个装配好的 `ThreadItem`。它自己不查数据库、不做模糊匹配——所有数据都已在 `contents` 里。它额外承担三件事：

1. 从 `self`（Sidebar 实体状态）补充行模型里没有的即时状态：悬停下标、重命名目标、标题再生成集合；
2. 装配悬停操作按钮与右键菜单；
3. 用特性开关裁剪 worktree 标签。

#### 4.2.2 核心流程

```text
render_list_entry(ix)                     ← u4-l1 的虚拟列表回调
  ├─ is_focused = 侧边栏持有焦点
  ├─ is_selected = is_focused && selection == Some(ix)   （键盘选中）
  ├─ is_active = active_entry.matches_entry(entry)        （全局活跃）
  └─ match entry { Thread → render_thread(ix, t, is_active, is_selected) }

render_thread：
  1. 派生布尔：is_draft / is_empty_draft / is_running / is_renaming / has_notification
  2. 组装数据：标题、时间戳（空草稿无）、图标（草稿固定圆圈）、worktrees（过特性开关）
  3. 链式装配 ThreadItem（约 25 个 builder 调用）
  4. 悬停且非重命名 → action_slot：重命名 + 上下文按钮（停止/丢弃草稿/归档）
  5. on_click → 清空 selection，按工作区 Open/Closed 分流激活
  6. 非草稿且有会话 → 再包一层 right_click_menu（右键菜单）
```

#### 4.2.3 源码精读

**入口分发**：[crates/sidebar/src/sidebar.rs:L2173-L2183](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2173-L2183) 计算三种行状态，[L2217-L2220](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2217-L2220) 按 `ListEntry` 变体分发。注意实参顺序：`render_thread(ix, thread, is_active, is_selected, cx)` 中第四个参数在函数签名里叫 `is_focused`——即「键盘选中且侧边栏聚焦」传给 `ThreadItem::focused`。

**第一步：派生布尔与数据**（[crates/sidebar/src/sidebar.rs:L6111-L6161](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L6111-L6161)）：

- 通知状态查的是 `contents.is_thread_notified(...)`（L6111），即 u2-l1 讲过的「跨重建继承的通知集合」。
- `is_running` 把 Running 与 WaitingForConfirmation 归为一类（L6121-L6124），只影响悬停时上下文按钮显示「停止生成」。
- 时间戳：空草稿给空字符串（L6139-L6143）——空字符串会让 `ThreadItem` 的 `has_timestamp` 为假，第二行元数据可能整行不渲染。
- 图标三岔口（L6152-L6156）：草稿行固定 `IconName::Circle` 并在 L6166-L6168 配 20% 透明度的哑色；非草稿用 `thread.icon` / `thread.icon_from_external_svg`。这两个字段一个来自重建时的 `resolve_agent_icon`（按 agent_id 映射：原生 agent → `ZedAgent`，自定义 agent → `Terminal`，另查 agent server 商店的外部 SVG 图标，见 [L1376-L1388](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1376-L1388)），活跃线程则被 `apply_active_info` 用面板实时信息覆盖（u3-l4）。
- `title_generating` 是两个来源的或：行模型里的实时标志 ‖ Sidebar 自己的 `regenerating_titles` 集合（L6158-L6161，后者记录用户点了「重新生成标题」但还没回来的线程）。

**第二步：特性开关裁剪 worktree 标签**。[crates/sidebar/src/sidebar.rs:L6147-L6150](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L6147-L6150) 在渲染现场读取 `AgentThreadWorktreeLabelFlag` 的值并过滤 `worktrees`：

- [crates/sidebar/src/sidebar.rs:L664-L686](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L664-L686)：`apply_worktree_label_mode` 的三模式——`Both` 原样；`Worktree` 清空所有 `branch_name`；`Branch` 只在已知分支时清空 `worktree_name`（没有分支信息时保留 worktree 名兜底，注释写明「空 chip 比图标不匹配更糟」）。清空名字后，`ThreadItem` 端会自动改用 `GitBranch` 图标（4.1.3 已讲）。
- [crates/feature_flags/src/flags.rs:L81-L100](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/feature_flags/src/flags.rs#L81-L100)：开关定义。枚举默认 `Both`，开关名 `agent-thread-worktree-label`，`enabled_for_staff` 为 false（默认谁都不开）。
- 开关变化如何生效？[crates/sidebar/src/sidebar.rs:L804](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L804) 在构造时调用 `AgentThreadWorktreeLabelFlag::watch(cx)`，它只是 `observe_global::<FeatureFlagStore>` 后 `cx.notify()`（[crates/feature_flags/src/feature_flags.rs:L139-L142](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/feature_flags/src/feature_flags.rs#L139-L142)）。也就是说**裁剪发生在渲染期**：开关变了 → 重渲染 → `render_thread` 现场再读 `flag_value`。这符合 u3-l2 的架构约束——`ThreadEntry` 里不存「裁剪后」的标签，避免增量状态。

**第三步：链式装配**（[crates/sidebar/src/sidebar.rs:L6163-L6187](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L6163-L6187)）：

```rust
let thread_item = ThreadItem::new(id, title.clone())
    .base_bg(sidebar_bg)
    .icon(icon)
    .status(thread.status)
    .is_remote(is_remote)
    .when_some(icon_svg, |this, svg| this.custom_icon_from_external_svg(svg))
    .worktrees(worktrees)
    .timestamp(timestamp)
    .highlight_positions(thread.highlight_positions.to_vec())
    .title_generating(title_generating)
    .notified(has_notification)
    .when(thread.diff_stats.lines_added > 0, |this| {
        this.added(thread.diff_stats.lines_added as usize)
    })
    // ……（removed 同理）
    .selected(is_selected)   // ← 注意：这里 is_selected = is_active
    .focused(is_focused)
    .hovered(is_hovered)
```

注意 `.selected(...)` 传的是活跃状态（L6185 的 `is_selected` 是 L6118 由 `is_active` 起的别名），`.focused(...)` 才是键盘选中——名字与直觉相反，读代码时要小心。

**第四步：悬停操作与重命名槽**：

- 重命名时（[L6196-L6217](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L6196-L6217)）：`title_slot` 放入重命名编辑器，外层 div 捕获 `Newline` / `Confirm` / `Cancel` 三个动作统一走 `finish_thread_rename`（细节在 u5-l4 展开）。同时 `is_truncated(false)` 关掉标题渐变淡出。
- 悬停且非重命名时（[L6218-L6310](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L6218-L6310)）：`action_slot` 里必有铅笔重命名按钮；上下文按钮三选一——运行中 → 红色停止按钮（`stop_thread`）；空草稿 → 无按钮；有内容草稿 → 丢弃（`remove_draft`）；普通线程 → 归档（`archive_thread`，且要求有 `session_id`）。
- 行点击（[L6311-L6333](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L6311-L6333)）：先 `selection = None`（鼠标接管，清掉键盘选中，承接 u2-l3），再按 `ThreadEntryWorkspace::Open` / `Closed` 分流到 `activate_thread` 或 `open_workspace_and_activate_thread`（u6-l1 展开）。

**第五步：右键菜单**（[crates/sidebar/src/sidebar.rs:L6335-L6462](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L6335-L6462)）：草稿或无 `session_id` 的线程直接返回裸行（L6335-L6341）；否则用 `right_click_menu` 把 `thread_item` 包成触发器，菜单项为：Rename Title（恒有）、Regenerate Thread Title（仅 Zed 原生 agent 线程）、Open Thread as Markdown（活跃或原生线程，L6353）、Archive Thread。菜单项回调通过 `cx.weak_entity()` 拿弱引用回写 Sidebar，避免实体环。

#### 4.2.4 代码实践

1. **实践目标**：为 `render_thread` 的每个 builder 调用建立「参数 ← 数据来源」对照表，验证「投影函数」的定性。
2. **操作步骤**：
   - 对照 [L6163-L6187](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L6163-L6187)，把下表右列填完（左列已给出前三行示例）：

     | builder 调用 | 数据来源 |
     | --- | --- |
     | `.timestamp(timestamp)` | `thread_display_time(&thread.metadata)` 格式化；空草稿给空串（L6139-L6143） |
     | `.notified(has_notification)` | `self.contents.is_thread_notified(&thread.metadata.thread_id)`（L6111） |
     | `.title_generating(...)` | `thread.is_title_generating` ‖ `self.regenerating_titles` 包含该 id（L6158-L6161） |
     | `.icon(...)` / `.custom_icon_from_external_svg(...)` | ？ |
     | `.worktrees(worktrees)` | ？ |
     | `.added(...)` / `.removed(...)` | ？ |
     | `.selected(...)` / `.focused(...)` | ？ |

   - 填完后回答：这些来源里有几个来自 `self.contents` 之外的 Sidebar 字段？（答案应是三个：`hovered_thread_index`、`renaming_thread_id`、`regenerating_titles`，外加 `focus_handle` 相关的焦点状态。）
3. **需要观察的现象**：纯阅读即可完成；如果想验证，可在本地临时把 `.notified(has_notification)` 改为 `.notified(false)` 后运行通知相关测试（如 `test_background_thread_completion_triggers_notification`），观察是否变红。
4. **预期结果**：对照表每一行都能在 L6103-L6187 找到唯一对应语句；改 `notified` 的本地实验预期让依赖通知徽标的断言失败（改动属本地练习，验证后请还原）。
5. 若不在本地运行，本实践结论可完全由源码推导，无需「待本地验证」标注。

#### 4.2.5 小练习与答案

**练习 1**：`ThreadSwitcher`（u7 的线程切换器）的条目同样使用 `ThreadItemWorktreeInfo`（[sidebar.rs:L5809-L5817](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L5809-L5817)），但它没有调用 `apply_worktree_label_mode`。这说明什么？
**答案**：特性开关只裁剪侧边栏列表行的标签；切换器弹层不受开关影响，始终按 `Both` 模式展示。同一个组件、不同消费方、不同策略——这正是把策略放在消费方而非组件里的直接后果。

**练习 2**：为什么 `resolve_agent_icon` 在重建期执行（结果烘进 `ThreadEntry`），而 `apply_worktree_label_mode` 在渲染期执行？
**答案**：agent 图标取决于 agent 注册信息，随重建自然刷新即可，烘进行模型可减少渲染期查询；而标签模式是个可能随时变的实验开关，若烘进行模型就需要「开关变化触发重建」这条额外链路。放在渲染期只需 `watch` → `cx.notify()` → 重渲染，符合「可现算的不落字段」的约束。

**练习 3**：悬停时正在运行的线程与普通线程的 `action_slot` 有何区别？
**答案**：两者都有重命名铅笔；运行中线程的上下文按钮是「停止生成」（Stop 图标、错误色），普通线程是「归档」（Archive 图标），有内容草稿是「丢弃草稿」，空草稿没有上下文按钮。

### 4.3 render_terminal：终端行与图标化标题

#### 4.3.1 概念说明

`render_terminal` 与 `render_thread` 结构对称但更薄：终端没有生命周期状态（无 Running/Error 等徽标）、没有 diff 统计、没有标题生成动画、没有重命名与右键菜单。它独有的核心是**标题装饰前缀图标化**：Agent 跑命令时产生的终端往往标题形如 `✳ pnpm build`、`[!] codex waiting`——前缀是 agent 留下的装饰符号。侧边栏把这个前缀拆出来，直接当作行的图标显示，标题则只显示剩余部分。

#### 4.3.2 核心流程

```text
render_terminal(ix, terminal, is_active, is_focused)
  1. 基础数据：id 用 terminal_id（而非下标！）、时间戳用 created_at、底图标固定 Terminal
  2. display_title → split_leading_icon_char(title, highlight_positions)
       Some((icon_char, trimmed, adjusted))
         → .icon_char(icon_char) + 标题用 trimmed + 高亮用 adjusted
       None → 无 icon_char，标题与高亮原样使用
  3. 链式装配 ThreadItem（无 status/added/removed/title_generating）
  4. 悬停 → action_slot 只有一个关闭按钮（close_terminal）
  5. on_click → activate_terminal_entry
```

#### 4.3.3 源码精读

**前缀从哪来**：agent_ui 在组装终端标题时就会保留装饰前缀——[crates/agent_ui/src/terminal_thread_metadata_store.rs:L75-L88](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/agent_ui/src/terminal_thread_metadata_store.rs#L75-L88) 的 `compose_terminal_thread_title`：用户给终端起了自定义标题时，重新拼上原终端标题的前缀（`format!("{prefix}{custom_title}")`）。所以「标题带装饰前缀」是写入元数据时就固化的形态，侧边栏渲染时才拆。

**渲染主体**：[crates/sidebar/src/sidebar.rs:L6465-L6510](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L6465-L6510)：

- L6473：行 id 用 `terminal-{terminal_id}`——而线程行用的是 `thread-entry-{ix}`（下标）。终端行以稳定 id 标识，线程行以下标标识，这是一个容易忽略的不对称。
- L6474：时间戳恒取 `metadata.created_at`，没有线程那套「空草稿给空串」的分支。
- L6490-L6494：调用 `split_leading_icon_char`（4.4 精读），三种返回值分别流向 `.icon_char(...)`、标题与高亮位置。
- L6496-L6507：builder 链——注意**没有** `.status(...)`（保持默认 Completed，所以状态图标永不出现）与 `.added/.removed`；`.notified(terminal.has_notification)` 用的是重建期算好并存进 `TerminalEntry` 的字段（[L1462-L1463](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1462-L1463)，来自活跃面板的 `has_notification`），与线程行渲染时现查 `contents` 不同。

**一个重要后果**：通知圆点的优先级高于 `icon_char`（4.1.3 的图标槽规则）。也就是说，有通知的终端行会显示强调色圆点而不是装饰前缀字符——装饰图标在有通知时让位。

**悬停与点击**：[crates/sidebar/src/sidebar.rs:L6516-L6536](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L6516-L6536) 悬停只放一个「关闭终端」按钮（tooltip 复用了 `ArchiveSelectedThread` 动作名），点击调 `close_terminal`；[L6537-L6549](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L6537-L6549) 行点击调 `activate_terminal_entry`（u6-l2 展开）。与线程行不同：不清理 `selection`（线程行 L6314 清了），也没有右键菜单包装。

#### 4.3.4 代码实践

1. **实践目标**：对一组终端标题手工推演渲染结果，验证你理解了「前缀拆分 → 图标化 → 高亮重映射」全链路。
2. **操作步骤**：对下表每个标题写出：检测到的前缀（`terminal_title_prefix` 的返回值）、`ThreadItem` 收到的标题、`icon_char`。先自己推，再对照 4.4 的算法核对：

   | display_title | 前缀 | 显示标题 | icon_char |
   | --- | --- | --- | --- |
   | `✳ pnpm build` | ？ | ？ | ？ |
   | `v1 pnpm build` | ？ | ？ | ？ |
   | `pnpm build` | ？ | ？ | ？ |
   | `... waiting` | ？ | ？ | ？ |
   | `✳Thinking` | ？ | ？ | ？ |

3. **需要观察的现象**：`v1 ...` 与 `✳Thinking` 两行是关键——前者以字母开头、后者前缀后没有空白分隔，两者都不该拆分。
4. **预期结果**（可与 4.4.5 答案核对）：`✳ pnpm build` → `✳ ` / `pnpm build` / `✳`；`v1 pnpm build` → 不拆；`pnpm build` → 不拆；`... waiting` → `... ` / `waiting` / `…`；`✳Thinking` → 不拆。实际 UI 效果待本地验证（可运行 Zed 并在 agent 面板里触发一条终端命令观察侧边栏行）。
5. 推理结果可完全由源码得出；已有测试 `test_split_leading_icon_char`（[crates/sidebar/src/sidebar_tests.rs:L14961-L15023](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar_tests.rs#L14961-L15023)）覆盖了同类样例，可作对照。

#### 4.3.5 小练习与答案

**练习 1**：列出 `render_terminal` 相对 `render_thread` 缺失的 5 个特性。
**答案**：无状态徽标（不调 `.status`）、无 diff 统计（`.added/.removed`）、无标题生成动画、无行内重命名与右键菜单、悬停只有一个关闭按钮（线程行还有重命名 + 上下文按钮）。另外线程行点击会清空 `selection`，终端行不清。

**练习 2**：为什么终端行的通知标志用 `terminal.has_notification`（行模型字段），线程行却用 `contents.is_thread_notified(...)`（渲染期现查）？
**答案**：终端通知没有跨重建记忆的需求——每轮重建直接从活跃面板现算（`live_notified_terminal_ids`），算完落进行模型即可；线程通知要检测「Running→Completed 跳变」，必须依赖跨重建继承的 `notified_threads` 集合（u2-l1、u3-l4），所以渲染期向 `contents` 查询。

**练习 3**：若终端标题是 `🇺🇸 deploy prod`，`icon_char` 是什么？
**答案**：`🇺🇸`（国旗 emoji 由两个 regional indicator 组成，是多码点字素簇）。`pick_icon_glyph` 用 `graphemes(true)` 取第一个字素簇，保证不会被切半个 emoji（见 4.4.3）。

### 4.4 split_leading_icon_char 与 pick_icon_glyph：两步拆分算法

#### 4.4.1 概念说明

这是 sidebar crate 的一对自由函数，解决「agent 在标题前面贴的装饰符号」问题：

- `terminal_title_prefix`（定义在 agent_ui crate）：判断标题是否以「非字母数字前缀 + 空白分隔」开头，是则返回含尾部空白的前缀切片。设计意图：只把「明显是装饰」的头部拆出来——以字母/数字开头的标题（如版本号 `v1`）绝不误伤，没有空白分隔的（如 `✳Thinking`）也当成普通标题。
- `pick_icon_glyph`：把检测到的前缀浓缩成**一个**可显示的字形。规则：剥掉一对 ASCII 括号（`[!]` → `!`）、连续点号缩成省略号（`...` → `…`）、最后取第一个字素簇（`>>>` → `>`，多码点 emoji 完整保留）。
- `split_leading_icon_char`：把两者串起来，并完成第三件事——**高亮位置重映射**：高亮位置是全标题的字节偏移（4.2 已知它由搜索过滤产生于全标题上），标题被裁掉前缀后，落在前缀里的位置丢弃，其余位置整体减去前缀字节长度。

#### 4.4.2 核心流程

```text
split_leading_icon_char(title, highlight_positions)
  1. prefix = terminal_title_prefix(title)?        —— 无装饰前缀 → None
  2. icon_char = pick_icon_glyph(prefix)?           —— 浓缩不出字形 → None
  3. trimmed = title[前缀字节长度..]                —— 剩余为空 → None
  4. positions = positions 中 ≥ 前缀长度者 − 前缀长度
  5. 返回 (icon_char, trimmed, positions)

pick_icon_glyph(prefix)
  1. trim；空 → None
  2. 若首字符是 [ ( { < 且有对应的 ] ) } > 收尾 → 剥掉括号再 trim；
     剥完变空 → 回退用原前缀
  3. 以 ".." 开头 → 返回省略号 U+2026
  4. 返回第一个字素簇（graphemes(true)，多码点 emoji 不拆）；
     该字素簇 trim 后为空 → None
```

#### 4.4.3 源码精读

- [crates/sidebar/src/sidebar.rs:L235-L263](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L235-L263)：`split_leading_icon_char` 主体。L246-L249 的 `stripped_len = prefix.len()` 与 `&title[stripped_len..]` 都是**字节**运算（`prefix` 是 `&str`，`.len()` 是字节数），空标题保护在 L248-L250。位置重映射的 `filter` + `map` 在 L252-L256。
- [crates/sidebar/src/sidebar.rs:L265-L304](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L265-L304)：`pick_icon_glyph` 主体。三个要点：
  - L280-L290：括号剥壳只认 `[ ]`、`( )`、`{ }`、`< >` 四对 ASCII 括号，且只剥一层；剥完 trim 后若为空（如前缀就是 `"[ ]"`），回退到原前缀继续处理——这个回退分支容易被忽略；
  - L293-L295：`starts_with("..")` 判定在括号剥壳**之后**，所以 `[...] working` 先变成 `...` 再命中省略号分支；
  - L298-L303：`graphemes(true)` 取扩展字素簇（sidebar.rs:62 引入 `unicode_segmentation::UnicodeSegmentation`），这是国旗 emoji、变体选择符不被切碎的保证。
- [crates/agent_ui/src/terminal_thread_metadata_store.rs:L96-L136](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/agent_ui/src/terminal_thread_metadata_store.rs#L96-L136)：`terminal_title_prefix` 的扫描循环。终止条件有三：遇到字母/数字字符立刻返回 `None`（L103-L105）；空白出现在任何前缀字符之前返回 `None`（L107-L110，所以 `" leading space"` 不拆）；只有「先见过非字母数字字符、再遇到空白」才把前缀连同空白收入（L111-L125），从未遇到空白则整体返回 `None`（L131-L135，所以 `"✳Thinking"` 不拆）。
- 高亮位置的生产端（承接 u3-l4 / u5-l3）：[crates/sidebar/src/sidebar.rs:L1847-L1855](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L1847-L1855) 在重建的过滤阶段用 `fuzzy_match_positions(&query, terminal_title)` 把位置算在**完整标题**上；render_terminal 再经本函数重映射。worktree 名字上的高亮（L1857-L1865）不经此函数，因为前缀拆分只发生在标题上。
- 已有测试：[crates/sidebar/src/sidebar_tests.rs:L14961-L15023](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar_tests.rs#L14961-L15023) 的 `test_split_leading_icon_char` 覆盖了拆分/不拆分/括号/省略号/emoji/位置重映射六类样例——但它测的是外层 `split_leading_icon_char`，**`pick_icon_glyph` 本身没有直接的单测**（这正是本讲实践的切入点）。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：为 `pick_icon_glyph` 建立一组输入输出样例表并推理结果，然后补一个表格驱动的单元测试验证推理。

**第一步：样例推理**。`pick_icon_glyph` 的入参是「已检测到的前缀」（真实调用中含尾部空白，函数第一步会 trim）。按下表先自己推出结果：

| 输入前缀 | 推理过程 | 预期输出 |
| --- | --- | --- |
| `"[!] "` | trim → `[!]` → 剥括号 → `!` | `Some("!")` |
| `">>> "` | trim → `>>>` → 首字符 `>` 不在括号列表 → 首字素簇 | `Some(">")` |
| `"... "` | trim → `...` → 以 `..` 开头 | `Some("…")`（U+2026） |
| `"[...] "` | trim → `[...]` → 先剥括号得 `...` → 再命中点号分支 | `Some("…")` |
| `"🇺🇸 "` | 首字素簇为整个国旗（两个 regional indicator） | `Some("🇺🇸")` |
| `"(?) "` | 剥圆括号 → `?` | `Some("?")` |
| `"[ ] "` | 剥括号后 trim 为空 → **回退原前缀** `[ ]` → 首字素簇 | `Some("[")` |
| `"   "` | trim 后为空 | `None` |
| `"!"` | 无括号、非点号、首字素簇 | `Some("!")` |

**第二步：编写测试**。`pick_icon_glyph` 是 sidebar.rs 的私有函数，而 `sidebar_tests.rs` 以 `#[cfg(test)] mod sidebar_tests;`（[sidebar.rs:L83-L84](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L83-L84)）挂入库内且开头就是 `use super::*;`，所以测试文件可以直接调用它。在本地把下面的测试追加到 `sidebar_tests.rs` 末尾（示例代码，非项目原有代码）：

```rust
// 示例代码：为 pick_icon_glyph 补的表格驱动测试
#[test]
fn test_pick_icon_glyph() {
    let cases: &[(&str, Option<&str>)] = &[
        ("[!] ", Some("!")),
        (">>> ", Some(">")),
        ("... ", Some("\u{2026}")),
        ("[...] ", Some("\u{2026}")),
        ("🇺🇸 ", Some("🇺🇸")),
        ("(?) ", Some("?")),
        // 剥掉括号后为空 → 回退原前缀，取其首字素簇
        ("[ ] ", Some("[")),
        ("   ", None),
        ("!", Some("!")),
    ];

    for (input, expected) in cases {
        assert_eq!(
            pick_icon_glyph(input).as_deref(),
            *expected,
            "pick_icon_glyph({input:?}) 应为 {expected:?}"
        );
    }
}
```

**第三步：运行**。在仓库根目录执行：

```bash
cargo test -p sidebar --lib test_pick_icon_glyph
```

**需要观察的现象**：9 个用例全部通过；若把 `("[ ] ", Some("["))` 改成 `None` 会失败——证明「剥壳后为空回退原前缀」的分支确实存在。

**预期结果**：测试通过，且与第一步的推理表逐行一致。`assert_eq!` 比较的是 `Option<SharedString>` 与 `Option<&str>`（经 `as_deref` 归一），无需手写比较器。本实践的命令输出属「待本地验证」——样例表与断言本身已由源码 L265-L304 逐行核对。

#### 4.4.5 小练习与答案

**练习 1**：`split_leading_icon_char(&"✳".into(), &[])` 返回什么？为什么？
**答案**：`None`。`terminal_title_prefix("✳")` 扫描完整个字符串也没遇到空白（`saw_whitespace_after_prefix` 为假），返回 `None`，第一行就以 `?` 短路。已有测试 L14983 明确锁定了这个行为：只有符号、没有空白分隔的标题不拆。

**练习 2**：标题 `"✳ fix the bug"`、查询 `"fix"` 产生了高亮位置。fuzzy 匹配返回的字节位置是多少？`split_leading_icon_char` 重映射后又是多少？
**答案**：`"✳ fix the bug"` 中 `✳` 占 3 字节、后跟 1 字节空格，所以 `f` 在字节 4、`i` 在 5、`x` 在 6，即 `[4, 5, 6]`；前缀 `"✳ "` 长度 4，重映射后为 `[0, 1, 2]`——恰好落在裁剪后标题 `"fix the bug"` 的 `fix` 上。这正是字节偏移设计的意义：减去前缀字节长度即完成平移。

**练习 3**：如果把 `pick_icon_glyph` 里 `starts_with("..")` 的判定移到括号剥壳**之前**，哪个已有测试用例会失败？
**答案**：`"[...] working"`（测试 L15001-L15003）。剥壳前字符串是 `[...]`，不以 `..` 开头，会走到首字素簇分支返回 `[`，与断言的 `…` 不符。这个顺序依赖是算法的隐藏契约：先剥壳、再浓缩。

## 5. 综合实践

**任务：从一条终端标题出发，画出完整的「渲染决策表」并落成可运行的测试。**

1. 选定 6 个标题样例（建议含：`✳ pnpm build`、`[!] codex waiting`、`... working`、`🇺🇸 flag`、`v1 Running`、`✳Thinking`），并假设搜索框里输入了会命中标题的查询词。
2. 对每个样例，沿调用链依次填写一张决策表，每列都要给出代码依据（文件 + 行号）：

   | 标题 | `terminal_title_prefix` | `pick_icon_glyph` | ThreadItem 标题 | 高亮位置（重映射前后） | 图标槽最终内容 |
   | --- | --- | --- | --- | --- | --- |

   最后一列要分两种情况讨论：`has_notification` 为假（显示 `icon_char`）与为真（通知圆点顶替，见 4.1.3 的优先级规则）。
3. 把表中 `pick_icon_glyph` 一列的结论沉淀为 4.4.4 的表格驱动测试并运行；再运行既有测试做对照：

   ```bash
   cargo test -p sidebar --lib pick_icon_glyph
   cargo test -p sidebar --lib test_split_leading_icon_char
   ```

4. （可选，纯推理）对一个「linked worktree + 已知分支」的线程行，写出特性开关三种模式（`Both` / `Worktree` / `Branch`）下元数据行 chip 的形态差异，包括 chip 图标在 `GitWorktree` 与 `GitBranch` 之间的切换条件（4.1.3 与 4.2.3）。

完成标准：决策表每一格都能指出源码依据；两个测试命令全绿（命令输出待本地验证）；能口头解释「通知圆点会吃掉装饰图标」与「`Branch` 模式下无分支的 worktree 保留名字兜底」这两个反直觉行为。

## 6. 本讲小结

- `ThreadItem` 是 `ui` crate 的通用行组件，消费方只做「投影」：sidebar 把 `ThreadEntry` / `TerminalEntry` 翻译成约 25 个 builder 参数，外观规则全部留在组件内。
- 图标槽有严格优先级：Running 旋转图标 > Error/Waiting/通知圆点 > `icon_char` > 外部 SVG > `IconName`；标题槽则是：重命名编辑器 > 生成中动画 > 高亮 Label > 普通 Label。
- `render_thread` 是纯投影函数，但仍从 `self` 补三类即时状态（悬停下标、重命名目标、标题再生成集合），并负责悬停按钮、右键菜单与点击激活的分流。
- `render_terminal` 更薄但独占「标题前缀图标化」路径：前缀拆出来当图标、标题裁剪、高亮位置按字节平移；终端行 id 用稳定的 `terminal_id` 而非下标。
- 特性开关 `agent-thread-worktree-label` 的裁剪发生在渲染期（`watch` 只触发 `cx.notify()`），`ThreadEntry` 不存裁剪结果；切换器不受该开关影响。
- `pick_icon_glyph` 目前没有直接单测，外层 `split_leading_icon_char` 有——本讲实践为前者补上了表格驱动测试。

## 7. 下一步学习建议

- **u5-l1（动作注册与键位上下文）**：本讲的 `on_click` / 悬停按钮只是交互入口，下一单元系统讲解 gpui 动作如何被声明、分发与绑定键位。
- **u5-l3（过滤搜索）**：本讲只讲了高亮位置的「消费端」（重映射与渲染），其「生产端」——`fuzzy_match_positions` 如何在重建期写进行模型——在 u5-l3 展开。
- **u7-l1（ThreadSwitcher）**：`ThreadItem` 的第三个消费方，可对照本讲总结「同一组件、不同策略」的取舍；顺带阅读 `thread_item.rs` 的 `preview()` 之外，还可以看 `crates/zed/src/visual_test_runner.rs` 中 `ThreadItemBranchNameTestView` 等测试视图（L2876、L3129 附近），了解组件如何被单独可视化验证。
