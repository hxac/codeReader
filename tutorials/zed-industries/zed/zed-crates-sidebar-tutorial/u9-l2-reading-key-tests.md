# u9-l2 典型测试精读与测试编写方法

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解「防回归测试」如何锁定架构决策——而不只是验证某个 bug 修好了。
2. 掌握 `visible_entries_as_strings` 这类「快照式断言」：把整个可见列表压平成字符串数组，一次断言整个列表状态。
3. 掌握键盘交互测试的标准剧本：`focus_sidebar` + 手动置 `selection` + `cx.dispatch_action`。
4. 会用 `assert_project_header_has_threads` 这类「定向断言」helper，并理解它与快照式断言各自的适用场景。
5. 能模仿本 crate 已有测试的风格，为一个新行为（本讲的例子是「过滤状态下折叠分组」）写出风格一致的 gpui 测试。

本讲是单元九的第二篇。上一篇（u9-l1）解决了「测试世界怎么搭起来」；本讲解决「搭起来之后怎么断言、怎么读别人的测试、怎么写自己的测试」。

## 2. 前置知识

### 2.1 你应该已经掌握的内容

- **u9-l1 的测试脚手架**：`init_test` 铺全局、`init_test_project` 造项目（FakeFs + `Project::test`）、`cx.add_window_view` 把 `TestAppContext` 升级为 `VisualTestContext`、`setup_sidebar` 复刻生产装配、`run_until_parked` 把异步任务泵到收敛。本讲直接复用这些结论，不再重复推导。
- **u3-l3 的 EntryShape 契约**：行的「等高身份键」，`apply_list_state_diff` 用最长公共前缀 + 后缀夹逼出最小变化区间，只对区间内调用 `splice`，区间外的实测高度原样保留。本讲 4.2 节的三个测试就是这份契约的防回归护栏。

### 2.2 本讲新引入的几个概念

- **防回归测试（regression guard）**：Zed 的测试不只是「验证功能正确」，很多测试的断言对象是一条**架构决策**。例如「同形状的元数据更新不得重置列表测量」——如果你重构时不小心让每次重建都整表重置，测试会立刻变红。读这类测试时，先问自己「它锁死的契约是什么」，再看断言。
- **快照式断言 vs 定向断言**：
  - 快照式：把一大块状态序列化成可读字符串，整体比较（本讲的 `visible_entries_as_strings`）。
  - 定向断言：只抽取关心的一个字段比较（本讲的 `assert_project_header_has_threads`）。
  - 前者适合「列表整体长什么样」的场景，失败信息一眼能看出哪里变了；后者适合「某个细节位」的场景，不会被无关变化干扰。
- **`#[track_caller]`**：标注在断言 helper 上，panic 时报错位置指向**调用方那一行**而不是 helper 内部。本文件顶部的一批 helper（如 [src/sidebar_tests.rs:L101](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L101)）都带这个属性。
- **`pretty_assertions::assert_eq`**：文件在 [src/sidebar_tests.rs:L18](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L18) 引入了 `pretty_assertions` 版本的 `assert_eq`，失败时输出结构化彩色 diff，比较两个长字符串数组时尤其好用。
- **`#[gpui::test]`**：gpui 提供的测试宏，配合 `async fn` 与 `cx: &mut TestAppContext`，让测试运行在确定性的模拟 executor 上（没有真实窗口、没有真实线程竞争）。

## 3. 本讲源码地图

本讲几乎全部篇幅都在一个文件里：

| 文件 | 作用 |
| --- | --- |
| `crates/sidebar/src/sidebar_tests.rs`（15111 行） | 本 crate 唯一测试文件，约为实现（8208 行）的两倍。本讲精读其中的断言 helper 与三类代表性测试 |
| `crates/sidebar/src/sidebar.rs` | 被测实现。本讲只引用三处：过滤编辑器订阅（L835-L841）、键盘导航实现（L3468-L3562）、折叠实现（L4274-L4304） |

`sidebar_tests.rs` 内部的大致分区（按行号）：

| 区段 | 内容 |
| --- | --- |
| L1-L200 | `use` 区、`init_test`、断言/查询 helper（`assert_active_thread`、`assert_project_header_has_threads` 等） |
| L203-L523 | 播种与装配 helper（`init_test_project`、`setup_sidebar` 家族、`save_thread_metadata` 家族、`focus_sidebar`） |
| L525-L604 | `format_linked_worktree_chips`、`visible_entries_as_strings` |
| L606-L751 | **测量保留类测试**（本讲 4.2） |
| L1127-L1720 | **键盘交互类测试**（本讲 4.3，中间夹着 `test_visible_entries_as_strings`） |
| L1720-L3100 | 终端 / AgentPanel 相关测试（使用 `assert_project_header_has_threads`，本讲 4.4 引用其一） |
| L2573-L3960 | **归档级联类测试**（本讲 4.4 精读其一） |
| L4121-L4780 | 搜索过滤类测试（`type_in_search` 在 L4121，综合实践会用到） |

## 4. 核心概念与源码讲解

### 4.1 visible_entries_as_strings：把整个列表压平成可断言的字符串

#### 4.1.1 概念说明

侧边栏的核心状态是 `contents.entries`——一个 `ListEntry` 的扁平数组（u2-l1）。测试它时最常见的问题是：「列表现在到底长什么样？」逐字段断言太啰嗦，直接打印 `Debug` 输出又难读。

本 crate 的答案是 `visible_entries_as_strings`：把每一行渲染成一条人能读的 ASCII 字符串，整个列表变成 `Vec<String>`，然后用 `assert_eq!` 一次比完。这本质上是手工编写的**快照测试**——快照格式由测试作者设计，稳定、可读、diff 友好。

#### 4.1.2 核心流程

helper 的执行过程：

1. `sidebar.read_with(cx, ...)` 借出 `&Sidebar`。
2. 遍历 `contents.entries` 并 `enumerate`，拿到行内容与下标 `ix`。
3. 若 `sidebar.selection == Some(ix)`，该行追加 `"  <== selected"` 标记。
4. 按变体格式化：

| 行类型 | 格式 | 例 |
| --- | --- | --- |
| ProjectHeader | `{v 或 >} [{label}]`，`v` 展开 / `>` 折叠 | `"v [my-project]"` |
| Thread | `"  {title}{worktree}{live}{status}{notified}"` | `"  Hello * (running) (!)"` |
| Terminal | `"  {title}{worktree}"` | `"  Dev Server"` |

5. Thread 行的后缀含义：`{worktree}` 是 linked worktree 徽标（形如 ` {wt-a, wt-b}`，由伴生 helper `format_linked_worktree_chips` 生成）；`*` 表示 `is_live`；`(running)`/`(error)`/`(waiting)` 表示 `AgentThreadStatus` 三种非默认态；`(!)` 表示该线程在通知集合里。

注意两个设计细节：

- 折叠图标不是从某个字段读的，而是现场调用 `sidebar.is_group_collapsed(key, cx)` 查询宿主 `MultiWorkspace`（u4-l2 讲过：折叠状态存在宿主上，Sidebar 不留副本）。
- helper **不**做焦点门控——真实 UI 里只有侧边栏持有焦点时才显示选中高亮（u2-l3），但 helper 只看 `selection == Some(ix)` 这个纯数据条件，测试因此更稳定。

#### 4.1.3 源码精读

helper 本体（含折叠图标查询与三种变体的格式化分支）：

- [src/sidebar_tests.rs:L547-L604](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L547-L604) —— `visible_entries_as_strings` 全文。L558-L562 计算 selected 标记；L564-L570 分组头分支（`is_group_collapsed` 决定 `>`/`v`）；L572-L594 线程分支（live/status/notified 三个后缀）；L595-L599 终端分支。
- [src/sidebar_tests.rs:L525-L545](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L525-L545) —— `format_linked_worktree_chips`：跳过 Main worktree、按名字去重、包成 `{name}` 并用 `, ` 连接。

这个 helper 自己也有一个测试，用**手工构造的 entries** 验证每种格式分支都能被正确渲染——这是「给测试代码写测试」的示范：

- [src/sidebar_tests.rs:L1127-L1356](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L1127-L1356) —— `test_visible_entries_as_strings`。L1141-L1147 通过 `mw.test_add_project_group` 往宿主里塞一个已折叠的空分组（这样折叠分支有数据可查）；L1149-L1152 直接往 `s.contents.notified_threads` 里插入一个ThreadId——之所以能这样做，是因为 `notified_threads` 是唯一跨重建继承的列表级记忆（u2-l1/u3-l4）；L1152 起手工拼出覆盖各状态组合的 `ListEntry` 数组。

还有一个高频使用技巧值得单独指出——**排序消除不确定性**：

- [src/sidebar_tests.rs:L3995-L3996](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3995-L3996) —— `test_parallel_threads_shown_with_live_status` 里 `entries[1..].sort()`：两条 "Hello" 线程的相对顺序取决于异步完成时序，于是保留第 0 行（分组头）不动、对其余行排序后再比较。写自己的测试遇到「顺序不稳定」时直接抄这个办法。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到快照式断言的失败输出长什么样，体会它为什么好排查。
2. **操作步骤**：
   - 在仓库根目录运行 `cargo test -p sidebar --lib test_visible_entries_as_strings`，确认通过。
   - 本地临时修改 helper：把 [src/sidebar_tests.rs:L570](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L570) 分组头的格式串里的 `">"` 改成 `"+"`（折叠分支），再运行同一个测试。
3. **需要观察的现象**：第二次运行时 `pretty_assertions` 会给出左右两侧的彩色 diff，一眼指出 `"v [...]"` 与 `"+ [...]"` 的差异位置，并告诉你断言发生在测试的哪一行。
4. **预期结果**：大量以分组头开头的断言变红，失败信息里左右两侧数组对齐展示。验证完把改动还原（不要提交）。
5. 以上命令输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `visible_entries_as_strings` 必须接收 `cx`（一个 `VisualTestContext`），而 `has_thread_entry`（[src/sidebar_tests.rs:L93-L99](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L93-L99)）不需要？

答案：`has_thread_entry` 只读 `sidebar.contents.entries` 这个纯数据字段，借出 `&Sidebar` 就够；而 `visible_entries_as_strings` 要为分组头计算折叠图标，`is_group_collapsed` 需要拿 `cx` 去 `read` 宿主 `MultiWorkspace` 查询折叠状态——折叠状态不在 Sidebar 自己身上（u4-l2 的设计）。

**练习 2**：`test_visible_entries_as_strings` 为什么可以只改 `s.contents.entries` 就让断言生效，不需要触发一次 `update_entries` 重建？

答案：`visible_entries_as_strings` 是 `contents` 的纯投影函数，不经过渲染管线；测试直接把手工构造的行塞进 `entries`，等于把「重建」这一步的输出伪造好了。这正是快照式断言的好处之一：被测对象（格式化逻辑）与数据来源（重建管线）解耦，u3 的管线测试和本测试互不牵连。

**练习 3**：如果未来给 Thread 行新增一个影响显示的状态（比如「已固定」），helper 需要怎么改？

答案：在 Thread 分支的 `format!` 里追加一个后缀（如 `" (pinned)"`），同时更新 `test_visible_entries_as_strings` 里手工构造的条目让它覆盖新分支。注意：如果那个状态还影响行高，则 additionally 要进 `EntryShape`（u3-l3 的契约），那是另一条防线。

### 4.2 测量保留类测试：用测试锁死 EntryShape 契约

#### 4.2.1 概念说明

u3-l3 讲过架构约束：全量重建教义下，`update_entries` 不得重置 `ListState` 的测量缓存，否则粘性项目头会因 `bounds_for_item` 返回 `None` 而闪跳一帧。这条约束不是靠注释维持的，而是靠三个测试锁死的：

| 测试 | 锁定的命题 |
| --- | --- |
| `test_thread_metadata_update_preserves_sticky_header_measurements` | 同形状的元数据更新（如改标题）不丢实测边界 |
| `test_thread_status_update_does_not_reset_list_measurements` | 无操作重建产生完全相同的形状序列 |
| `test_collapse_changes_entry_shape` | 折叠必改形状序列（该重置时就必须重置） |

第三个方向尤其值得注意：前两个测试防止「过度重置」，第三个防止「该重置而不重置」。契约是双向锁死的。

#### 4.2.2 核心流程

**测试一（真实绘制路径）** 是全文件里少数真正调用 `cx.draw` 的测试：

```
init_multi_project_test（两个项目 → 两个分组头）
→ setup_sidebar
→ 播种两条线程元数据（各属一个项目）
→ cx.draw(400×240)                 # 显式绘制，让列表完成布局与测量
→ 读 project_header_indices[1]     # 第二个分组头的下标
→ list_state.scroll_to(header_ix - 1, offset 24px)   # 把它推到「部分滚出」位置
→ 再 cx.draw 一次 + run_until_parked
→ 记录 bounds_for_item(header_ix)  # before
→ save_thread_metadata（改标题、改时间——形状不变）
→ 记录 bounds_for_item(header_ix)  # after
→ assert_eq!(before, after)
```

为什么先滚动？粘性头部只有在「被下一个分组头部分顶出视口」时才处于对测量丢失最敏感的状态（正是闪跳发生的位置），测试要制造这个姿态而不是让头部安稳地停在顶部。

**测试二/三（纯形状序列路径）** 不画 UI，直接比较 `entry_shapes` 投影：

```
save_n_test_threads(2) → 读 before = entry_shapes(...).collect()
→ （测试二）手动调 update_entries / （测试三）调 toggle_collapse
→ 读 after
→ 测试二 assert_eq / 测试三 assert_ne
```

#### 4.2.3 源码精读

- [src/sidebar_tests.rs:L606-L685](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L606-L685) —— 测试一全文。L632-L637 与 L654-L658 两处 `cx.draw`：以 `(0,0)` 原点、`400×240` 尺寸把 sidebar 元素画出来，gpui 借此完成真实的布局测量；L647-L653 用 `list_state.scroll_to(gpui::ListOffset { item_ix, offset_in_item })` 精确控制滚动姿态；L661-L666 取 `bounds_for_item` 并用 `expect` 断言「必须已测量」；L668-L676 重播一条**同 session、新标题、新时间**的元数据（形状不变的数据变化）；L678-L684 核心断言 `assert_eq!(bounds_before, bounds_after)`。
- [src/sidebar_tests.rs:L687-L718](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L687-L718) —— 测试二。L689-L692 的注释直接写明了动机（状态跳变时重置 ListState 会让粘性头在两个位置间闪一帧），这是「注释解释 why」的范本；L701-L705 借 `read_with` 同时借出 sidebar 与 app 上下文调用 [src/sidebar.rs:L2053](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L2053) 的 `entry_shapes`（形状投影，u3-l3）；L706 直接调用 `update_entries` 强制一次重建。
- [src/sidebar_tests.rs:L720-L751](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L720-L751) —— 测试三。L730 先从 project 读出 `project_group_key`，L737-L739 调 `toggle_collapse`，L747-L750 `assert_ne!` 并在消息里写明「折叠必须改变形状序列以便列表重置」。

被测的另一端在 [src/sidebar.rs:L2024](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L2024)（`apply_list_state_diff`）——本讲不重复 u3-l3 的算法讲解，只强调：这三个测试就是该函数存在的理由，读测试等于读需求。

#### 4.2.4 代码实践

1. **实践目标**：验证三个测试确实各自守住一段契约，理解「破坏哪个实现会让哪个测试变红」。
2. **操作步骤**：
   - 先在仓库根目录分别运行：
     - `cargo test -p sidebar --lib test_thread_metadata_update_preserves_sticky_header_measurements`
     - `cargo test -p sidebar --lib test_thread_status_update_does_not_reset_list_measurements`
     - `cargo test -p sidebar --lib test_collapse_changes_entry_shape`
   - 然后做本地破坏性实验：打开 [src/sidebar.rs:L2024](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L2024) 的 `apply_list_state_diff`，把前缀/后缀计算的结果在本地改成「总是整表重置」（等价于令公共前缀与后缀长度为 0），再跑上面三条命令。观察哪些变红。
3. **需要观察的现象**：预期测试一变红（`bounds_after` 变成未测量或数值改变导致 `expect`/`assert_eq` 失败）；测试二只比较形状序列、不触碰 `ListState`，大概率仍然绿；测试三同样不受影响。
4. **预期结果**：你将直观看到「测试一守测量、测试二/三守形状」的分工。实验结束后还原改动（不要提交）。
5. 破坏性实验的具体失败形态**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：测试二为什么不也用 `cx.draw` + `bounds_for_item` 断言，而要比较形状序列？

答案：因为形状序列相等是「测量得以保留」的**充分前提**（`apply_list_state_diff` 的契约就是形状相同时零触碰 `ListState`）。在纯数据层断言前提，比在绘制层断言结果更快也更稳定——不用构造窗口、不用关心滚动姿态。测试一已经覆盖了绘制层的端到端验证，两者互补而非重复。

**练习 2**：`save_n_test_threads(2)` 播种的两条线程标题是 "Thread 1"、"Thread 2"，为什么形状比较不需要关心标题内容？

答案：`EntryShape::Thread` 只含线程 id，不含标题（u3-l3）：标题变化不影响行高，所以不属于形状。改标题正是测试一里「同形状的数据变化」的典型样例。

**练习 3**：如果有人把折叠状态从 `MultiWorkspace` 挪进 `Sidebar` 自己的字段，这三个测试会怎么反应？

答案：`entry_shapes` 的投影源（[src/sidebar.rs:L2053](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L2053) 现场查询宿主）与 `is_group_collapsed` 的实现会变，但只要「折叠必改形状、同形状必保留测量」这两条对外契约不变，测试应当依然全绿。这正是好测试的特征：锁契约、不锁实现。

### 4.3 键盘交互类测试：焦点、选择与动作分发

#### 4.3.1 概念说明

键盘测试要回答的核心问题是：**没有真人按键，怎么驱动 `on_action` 处理器？**

本 crate 的答案由三个积木组成：

1. **`focus_sidebar`**（[src/sidebar_tests.rs:L488-L493](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L488-L493)）：在 `update_in` 里调 `cx.focus_self(window)` 把窗口焦点给 Sidebar 自身的焦点句柄，再 `run_until_parked`。没有焦点，动作分发找不到目标。
2. **`cx.dispatch_action(ActionName)`**：`VisualTestContext` 提供的窗口级动作分发，把动作派发给当前焦点所在的元素层级——与真实按键走的是同一条 dispatch 路径（u5-l1 讲过的 KeyContext 栈）。
3. **直接改 `sidebar.selection`**：很多测试用 `sidebar.update_in(..., |sidebar, _, _| sidebar.selection = Some(0))` 手动设置起点，跳过导航步骤，只测目标分支。这说明 `selection` 虽是私有字段，但测试模块是 `sidebar.rs` 的子模块（`use super::*`，[src/sidebar_tests.rs:L1](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L1)），可以直接触碰内部状态。

#### 4.3.2 核心流程

键盘测试的标准剧本（五步）：

```
1. init_test_project(path, cx)                        # 造项目（内部已 init_test）
2. add_window_view(MultiWorkspace::test_new) + setup_sidebar   # 开窗口 + 装侧边栏
3. save_n_test_threads(n) + run_until_parked          # 播种数据并等重建
4. focus_sidebar（可选：手动置 selection 起点）
5. cx.dispatch_action(某动作) → run_until_parked → 断言
```

断言出口两种：读 `s.selection`（测下标推进）或 `visible_entries_as_strings`（测列表整体变化，含折叠图标与 selected 标记）。

注意 `save_n_test_threads`（[src/sidebar_tests.rs:L241-L258](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L241-L258)）内部每次保存后都会 `run_until_parked`，所以多数测试在其后只需再补一次 `multi_workspace.update_in(..., cx.notify())` + `run_until_parked` 保险。

#### 4.3.3 源码精读

**导航边界的不对称性**——[src/sidebar_tests.rs:L1357-L1414](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L1357-L1414)：

- L1369-L1371 的注释先写明布局：`[header, thread3, thread2, thread1]`（新者在 前，u3-l4 的排序）。
- L1372-L1373：聚焦后断言 `selection == None`——锁定「focus_in 不设默认选中」（对应 [src/sidebar_tests.rs:L1438-L1468](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L1438-L1468) 的 `test_keyboard_focus_in_does_not_set_selection`，其中 L1454-L1456 还验证了 blur 再聚焦不丢已有选中）。
- L1376-L1399：连续 `SelectNext` 从 `None → 0 → 1 → 2 → 3`，再到末尾后**回绕到 0**（L1390-L1391）。
- L1412-L1413：`SelectPrevious` 到顶**清空选中**并把焦点交还搜索框。
- 这两个边界正好对应实现 [src/sidebar.rs:L3468-L3500](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L3468-L3500)：`select_next` 的 match 三臂（前进 / 回绕 / 从 None 起步）与 `select_previous` 的 `Some(0)` 分支（清空 + `filter_editor.focus_handle(cx).focus(window, cx)`）。

**Confirm 的类型分流**——[src/sidebar_tests.rs:L1470-L1520](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L1470-L1520)：

- L1481-L1488 先用快照式断言固化初始列表 `["v [my-project]", "  Thread 1"]`。
- L1491-L1494 聚焦 + 手动把 `selection` 设为 0（分组头）。
- L1497 `cx.dispatch_action(Confirm)` → L1500-L1506 断言列表只剩 `["> [my-project]  <== selected"]`——**Confirm 在分组头上是折叠切换**。
- L1509-L1519 再按一次 Confirm，恢复展开。
- 对应实现 [src/sidebar.rs:L3518-L3562](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L3518-L3562) 的 match：`ProjectHeader` → `toggle_collapse`；`Thread` 按 `Open`/`Closed` 分本地激活与先开工作区；`Terminal` → 激活终端。注意 L3519-L3521：重命名进行中先提交重命名再返回（u5-l4）。

**收起到父级**——[src/sidebar_tests.rs:L1577-L1615](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L1577-L1615)：

- L1589-L1592：两次 `SelectNext` 把选中推到线程行（下标 1）。
- L1604-L1605：线程行上 `dispatch_action(SelectParent)`。
- L1607-L1614 断言两件事：`selection` 迁移到 0（分组头），且列表折叠后只剩分组头一行、selected 标记跟随迁移。
- 对应实现 [src/sidebar.rs:L4290-L4301](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4290-L4301)：从当前下标**向前**找最近的 `ProjectHeader`，把 `selection` 设为它的下标、写入折叠状态、触发 `update_entries`。选中迁移与折叠是同一次原子操作，测试用两条断言分别钉住。

配套的 `test_keyboard_navigation_on_empty_list`（[src/sidebar_tests.rs:L1617-L1649](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L1617-L1649)）展示空列表（只有分组头一行）下的边界：单行回绕停在 0、上行清空、从 None 上行落到末行。

#### 4.3.4 代码实践

1. **实践目标**：把「动作 → 实现分支 → 断言」三者的对应关系在运行中验证一遍。
2. **操作步骤**：
   - 在仓库根目录运行：
     - `cargo test -p sidebar --lib test_keyboard_select_next_and_previous`
     - `cargo test -p sidebar --lib test_keyboard_confirm_on_project_header_toggles_collapse`
     - `cargo test -p sidebar --lib test_keyboard_collapse_from_child_selects_parent`
   - 挑 `test_keyboard_select_next_and_previous`，对照 [src/sidebar.rs:L3468-L3500](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L3468-L3500) 给测试里的每一段 `dispatch_action` 批注它命中的 match 臂。
3. **需要观察的现象**：三个测试全绿；测试名过滤只运行目标测试，输出里能看到各自消耗的时间。
4. **预期结果**：全绿。随后用自己的话回答 u5-l2 遗留的问题——「selection 的合法取值范围由谁维护？」参考答案：没有集中钳制者。合法性由四道防线共同维护：转换点清空（如到顶清空、输入搜索词清空）、`entries.get` 防御读取（[src/sidebar.rs:L3524](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L3524)）、导航归一化（回绕/钳制）、焦点门控渲染。测试对每道防线都有覆盖（清空见 L1412-L1413，防御读取的间接体现是 `test_selection_clamps_after_entry_removal`，[src/sidebar_tests.rs:L1688](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L1688)）。
5. 运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么这些测试用 `cx.dispatch_action(Confirm)` 而不是直接调用 `sidebar.confirm(...)`？

答案：`dispatch_action` 走真实的动作分发路径——焦点栈、KeyContext、`on_action` 注册（u5-l1）。直接调方法会绕过「动作是否真的被注册到了根容器」这一层，如果有人误删了 `.on_action(Confirm)` 的注册，直接调方法的测试照样绿，而 dispatch 版本会红。测交互就应从交互的入口进。

**练习 2**：`test_keyboard_confirm_on_project_header_toggles_collapse` 里，为什么 L1492-L1494 手动设 `selection = Some(0)` 而不是先 dispatch 一次 `SelectNext`？

答案：两种都能到下标 0，但手动设是「给定前提」的写法：本测试的主题是 Confirm 的分流，不是导航。把导航步骤换成直接布置前提，失败时排查面更小（红了一定是 Confirm 分支的问题，不会是导航的回归）。反过来，`test_keyboard_collapse_from_child_selects_parent` 的主题包含「从子行出发」，它就用真实的 `SelectNext` 走过去（L1590-L1591）。前提怎么给，取决于测试想证明什么。

**练习 3**：如果要测「在线程行上按 Confirm 且该线程是 Closed 形态」，测试还需要哪些播种手段？

答案：需要一个 linked worktree 场景让线程行呈现 `ThreadEntryWorkspace::Closed`——参考 [src/sidebar_tests.rs:L3625-L3635](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3625-L3635) 用 `save_thread_metadata_with_main_paths` 把 folder_paths 指到未打开的 worktree 路径。既有测试 `test_confirm_on_historical_thread_activates_workspace`（[src/sidebar_tests.rs:L4618](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L4618)）就是这个套路。

### 4.4 assert_project_header_has_threads 与归档级联类测试

#### 4.4.1 概念说明

`assert_project_header_has_threads` 是「定向断言」的代表：不关心整个列表，只回答一个问题——**名为某某的分组头上，`has_threads` 是不是预期值**。

`has_threads` 为什么值得单独断言？因为它是「空分组是否垫出 'No threads yet' 子行」的开关（u4-l2），直接决定 `EntryShape::ProjectHeader` 的形状（形状里含 `has_threads`）。终端场景下它尤其关键：一个项目组里没有任何线程也没有任何终端时，分组头不得宣称自己有线程。

归档级联类测试则是另一极：**副作用横跨四个层面**，一个测试要同时断言：

1. 数据库元数据（`ThreadMetadataStore` 里的 `archived` 标志 / 草稿删除）；
2. 工作区集合（`MultiWorkspace::workspaces()` 的数量与成员）；
3. 磁盘（FakeFs 上 linked worktree 目录是否被删）;
4. 可见列表（快照式断言）。

#### 4.4.2 核心流程

`assert_project_header_has_threads` 的执行过程：

```
read_with(sidebar)
→ 在 contents.entries 里 find_map：
   找到第一个 label 等于给定项目名的 ProjectHeader，取出 has_threads
→ assert_eq!(查到的值, Some(预期值))
```

找不到时 `has_threads` 是 `None`，与 `Some(false)`/`Some(true)` 都不相等——所以「分组头根本不存在」也会被这个断言抓住，这是 `find_map + Option` 写法的巧妙之处。

归档级联测试 `test_archive_selected_thread_archives_closed_linked_worktree` 的四段结构：

```
一、铺磁盘：FakeFs 上构造主仓库 + linked worktree 的 git 布局
   （insert_tree 两棵树 + add_linked_worktree_for_repo + record_zed_created_worktree）
二、播种三类行：worktree 线程（Closed）、主线程、空草稿
三、前置校验：worktree 线程确实以 Closed 形态出现在列表里
四、执行与断言：focus → 选中 → dispatch(ArchiveSelectedThread) → 泵 8 次
   → 四路断言：DB 归档标志 / 空草稿已删 / 工作区已移除 / 磁盘目录已删
```

#### 4.4.3 源码精读

helper 本体与典型用法：

- [src/sidebar_tests.rs:L101-L127](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L101-L127) —— `assert_project_header_has_threads`。L110-L119 的 `find_map` 带 `if let` 链式匹配（Rust 1.88 起的 let 链），L121-L125 断言并把两侧值都打进失败消息。
- [src/sidebar_tests.rs:L1826-L1841](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L1826-L1841) —— `test_closing_last_agent_panel_terminal_restores_empty_header` 的开头：L1832 在插入终端**之前**断言 `false`，L1834-L1841 插入一个测试终端后断言 `true`。一前一后两次定向断言，钉住「终端也算线程」这条业务规则。

归档级联测试（只看骨架，完整流水线讲解见 u8-l2，本讲关注**测试怎么写**）：

- [src/sidebar_tests.rs:L3567-L3614](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3567-L3614) —— 第一段「铺磁盘」。注意这个测试不用 `init_test_project`，而是 L3569 手动 `init_test(cx)` 后从 `FakeFs::new` 开始手工搭建：主仓库带 `.git/worktrees/feature-a`（L3572-L3586）、worktree 的 `.git` 文件指回主仓（L3587-L3594）、`add_linked_worktree_for_repo` 注册链接关系（L3595-L3606）、`record_zed_created_worktree` 打上「Zed 创建」标记（L3607-L3613，归档四道门槛之一，u8-l2）。**当预制 helper 满足不了场景时，就退回积木层手工搭**——这是本文件里两类测试初始化方式并存的缘故。
- [src/sidebar_tests.rs:L3625-L3659](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3625-L3659) —— 第二段播种 + L3652-L3657 一个内联断言（空草稿不该有持久化的 prompt 内容）+ L3658 手动 `update_entries`。
- [src/sidebar_tests.rs:L3661-L3681](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3661-L3681) —— 第三段前置校验：先用 `position` 找到目标线程下标（L3661-L3668），再 match 其 `workspace` 字段断言确实是 `Closed { folder_paths }`（L3669-L3681）。**前置校验是级联测试的标配**：它保证「后面四路断言红 了」时可以排除「输入本身就不对」的可能。
- [src/sidebar_tests.rs:L3683-L3690](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3683-L3690) —— 执行：`focus_sidebar` → 手动置 `selection` → `dispatch_action(ArchiveSelectedThread)` → **`for _ in 0..8 { cx.run_until_parked() }`**。循环泵多次是因为归档链有多个异步阶段（开工作区、git 存档、删目录），一次 `run_until_parked` 可能只推进一段。
- [src/sidebar_tests.rs:L3692-L3732](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3692-L3732) —— 四路断言：L3692-L3702 查 `ThreadMetadataStore` 的 `archived == Some(true)`；L3703-L3712 查空草稿元数据已删；L3713-L3727 查 `workspace_for_paths` 找不到 worktree 工作区、且工作区总数只剩 1；L3728-L3732 用 `!fs.is_dir(...).await` 查磁盘目录确实没了。每条断言都带一句人类可读的说明文字——级联测试断言多，消息是给未来排查者的路标。

#### 4.4.4 代码实践

1. **实践目标**：从测试侧反向核对 u8-l2 讲过的归档流水线，产出一张「断言 → 实现」对照表。
2. **操作步骤**：
   - 通读 [src/sidebar_tests.rs:L3567-L3733](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3567-L3733)。
   - 为四路终态断言各找到 sidebar.rs 中负责它的函数（提示：`archive_thread` / `archive_and_activate`、`roots_to_archive_for_paths`、`start_archive_worktree_task`、`delete_empty_drafts_for_archive_*`，u8-l2 有行号），填入下表：

| 终态断言 | 负责的实现函数 | 我如何确认 |
| --- | --- | --- |
| `archived == Some(true)` | （填写） | （调用链一句话） |
| 空草稿元数据已删除 | （填写） | |
| worktree 工作区已移除 | （填写） | |
| 磁盘目录已删除 | （填写） | |

3. **需要观察的现象**：四行都能在 u8-l2 的流程图里找到对应阶段；你会发现测试断言的顺序与实现里副作用的落锤顺序一致（先 DB、再工作区、再磁盘）。
4. **预期结果**：得到一张可放进学习笔记的对照表。
5. 表格内容为阅读任务，无需运行验证。

#### 4.4.5 小练习与答案

**练习 1**：`assert_project_header_has_threads` 为什么把结果包在 `Option` 里比较（`Some(expected)`），而不是找到头之后直接比较布尔？

答案：把「分组头不存在」也纳入失败情形。`find_map` 找不到时返回 `None`，与 `Some(false)` 不相等，断言红且消息会打印 `got None`——测试作者立刻知道是「头没了」而不是「标志错了」。这是用类型编码「三态：真 / 假 / 不存在」的手法。

**练习 2**：为什么归档测试在 dispatch 之后要 `for _ in 0..8 { cx.run_until_parked() }`，而键盘测试一次 `run_until_parked` 就够？

答案：键盘测试的动作处理是同步的（改 `selection`、改折叠、一次 `update_entries`），泵一轮收尾即可；归档链是**多阶段异步流水线**（补开工作区 → 推导归档根 → git 存档任务 → 删目录 → 级联移除工作区），每阶段的任务要等前阶段把新任务排进队列，一次 `run_until_parked` 只能推进到「当前已入队任务全部完成」。固定次数循环是这类多阶段测试的朴素而有效的写法。

**练习 3**：如果想给「归档后可见列表只剩主线程」补一条快照断言，应该插在哪、怎么写？

答案：插在四路断言之后（磁盘断言前后皆可），写成 `assert_eq!(visible_entries_as_strings(&sidebar, cx), vec!["v [my-project]", "  Main Thread  <== selected"]);` 之类——注意归档落锤会交接邻居条目（u6-l2 的 `neighboring_activatable_entry`），selected 标记落在哪条需要按实现的交接规则推断，拿不准就先只断言行集合、把 selected 标记的期望留待运行确认。

## 5. 综合实践

本讲的综合实践分两步：先精读一个未在本讲展开的测试，再模仿写出新测试。全程在本地进行，改动不要提交。

### 第一步：逐段注释 `test_parallel_threads_shown_with_live_status`

目标测试在 [src/sidebar_tests.rs:L3959-L4006](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L3959-L4006)，只有 47 行。参考注释如下（准备 / 执行 / 断言三段）：

**准备段（L3961-L3972）**：

- L3961-L3964：`init_test_project_with_agent_panel` 造带真实 `AgentPanel` 的项目并开窗口、装侧边栏——因为本测试需要**真实的 ACP 会话状态**（Running），纯元数据播种给不出 `is_live`。
- L3967-L3969：`StubAgentConnection::new()` 创建打桩的 agent 连接，`open_thread_with_connection` 在面板里打开线程 A，`send_message` 发一条消息让它进入生成中。
- L3971-L3972：读出 A 的 `session_id`，`save_test_thread_metadata` 把它落进元数据库——只有落库的线程才会出现在侧边栏列表（u3-l4 的多路查询）。

**执行段（L3974-L3993）**：

- L3974-L3981：通过 `connection.send_update` 推一条 `AgentMessageChunk`，维持 A 处于 Running。
- L3984-L3991：预设下一条 prompt 的响应，打开线程 B 并发消息——面板同时只能激活一个线程，B 打开后 A 转**后台**（background）但仍 Running。
- L3993：`run_until_parked` 等两条线程的状态都传播到侧边栏。

**断言段（L3995-L4005)**：

- L3995-L3996：取快照后 `entries[1..].sort()`——两条 "Hello" 的相对顺序依赖异步时序，排序消歧（4.1 讲过的技巧）。
- L3997-L4005：期望两条都是 `Hello`、都带 `*`（live），其中一条带 `(running)`——证明**后台线程的运行状态不被丢失**（这正是测试名 "parallel threads shown with live status" 锁定的行为）。

你的任务：把上面的三段注释抄进本地代码（或写在笔记里），逐行核对是否同意每条注释；不同意的地方写下你的版本。

### 第二步：模仿写一个新测试——「过滤状态下折叠分组」

现有测试 [src/sidebar_tests.rs:L4507-L4552](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L4507-L4552)（`test_search_finds_threads_inside_collapsed_groups`）覆盖了「先折叠、后搜索」；反过来「先搜索、在过滤态下折叠、再清空过滤词」没有现成测试。请补上它。

以下是**示例代码**（不是项目原有代码），可加在 `sidebar_tests.rs` 的搜索测试区附近：

```rust
#[gpui::test]
async fn test_collapse_during_search_persists_after_clearing_filter(cx: &mut TestAppContext) {
    let project = init_test_project("/my-project", cx).await;
    let (multi_workspace, cx) =
        cx.add_window_view(|window, cx| MultiWorkspace::test_new(project.clone(), window, cx));
    let sidebar = setup_sidebar(&multi_workspace, cx);

    // 准备：两条线程，"Important thread" 更新时间更晚，排在前面
    for (id, title, minute) in [
        ("t-1", "Important thread", 1),
        ("t-2", "Other thread", 0),
    ] {
        save_thread_metadata(
            acp::SessionId::new(Arc::from(id)),
            Some(title.into()),
            chrono::TimeZone::with_ymd_and_hms(&Utc, 2024, 1, 1, 0, minute, 0).unwrap(),
            None,
            None,
            &project,
            cx,
        );
    }
    cx.run_until_parked();

    // 基线：未过滤的完整列表
    assert_eq!(
        visible_entries_as_strings(&sidebar, cx),
        vec![
            //
            "v [my-project]",
            "  Important thread",
            "  Other thread",
        ]
    );

    // 执行 1：输入过滤词，只剩匹配行，且自动选中首个匹配（u5-l3）
    type_in_search(&sidebar, "important", cx);
    assert_eq!(
        visible_entries_as_strings(&sidebar, cx),
        vec![
            //
            "v [my-project]",
            "  Important thread  <== selected",
        ]
    );

    // 执行 2：选中态在线程行上按左键——收起到父级
    cx.dispatch_action(SelectParent);
    cx.run_until_parked();

    // 断言 2：搜索压倒折叠，匹配行仍可见；选中迁移到分组头
    assert_eq!(
        visible_entries_as_strings(&sidebar, cx),
        vec![
            //
            "> [my-project]  <== selected",
            "  Important thread",
        ]
    );

    // 执行 3：清空过滤词
    type_in_search(&sidebar, "", cx);

    // 断言 3：过滤清除后折叠状态保留——列表只剩分组头
    assert_eq!(
        visible_entries_as_strings(&sidebar, cx),
        vec![
            //
            "> [my-project]  <== selected",
        ]
    );
}
```

三条执行依据（为什么期望是这些）：

1. `type_in_search`（[src/sidebar_tests.rs:L4121-L4129](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L4121-L4129)）先聚焦搜索框再 `set_text`；过滤订阅（[src/sidebar.rs:L835-L841](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L835-L841)）在非空查询时 take 掉 `selection` 并以 `select_first_after_update = true` 调度刷新——所以断言 1 里匹配行带 selected 标记；清空（空查询）时不清 selection、以 `false` 调度刷新（[src/sidebar.rs:L840](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L840)），列表仍会重建。
2. 过滤态下线程行按 `SelectParent` 走 [src/sidebar.rs:L4290-L4301](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar.rs#L4290-L4301)：向前找分组头、`selection` 迁到 0、写折叠状态、重建。搜索压倒折叠的既有行为由 `test_search_finds_threads_inside_collapsed_groups` 的 L4544-L4551 背书（折叠态下搜索命中行照常显示）。
3. 折叠状态存于宿主 `MultiWorkspace`（u4-l2），与过滤词无关；清空过滤词触发的重建会读它，于是只剩分组头一行。

运行（仓库根目录）：

```bash
cargo test -p sidebar --lib test_collapse_during_search
```

**预期结果与迭代指引**：三条断言中，断言 1 有现成测试背书、把握最大；断言 2 与断言 3 依赖「折叠写入与过滤重建的组合路径」，属于本测试真正要验证的增量行为，**待本地验证**。如果失败：

- 先在可疑断言前加 `println!("{:#?}", visible_entries_as_strings(&sidebar, cx));`，用 `cargo test -p sidebar --lib test_collapse_during_search -- --nocapture` 看真实快照，再修正期望——这本身就是快照式断言的开发循环：先看真实输出，再固化期望。
- 若断言 2 里匹配行消失了（折叠在过滤态下也吞掉命中行），说明与 `test_search_finds_threads_inside_collapsed_groups` 锁定的行为出现了不一致，值得去 Zed 仓库提 issue——你的测试发现了一个真实回归。
- 允许最终因断言设计不当而迭代修改；测试稳定通过后，对比它与你读过的既有测试风格是否一致（helper 复用、断言消息、`run_until_parked` 位置），然后把改动还原或单独提交到你的 fork。

## 6. 本讲小结

- `visible_entries_as_strings` 是快照式断言出口：把 `ListEntry` 三种行压平成带 `v/>`、`*`、`(running)`、`(!)`、`<== selected` 标记的字符串数组；`entries[1..].sort()` 是消除顺序不确定性的标准技巧。
- 测量保留三测试双向锁死 EntryShape 契约：同形状更新保测量（要 `cx.draw` + `scroll_to` + `bounds_for_item` 的端到端路径）、无操作重建形状恒等、折叠必改形状（后两者只需纯数据层的 `entry_shapes` 序列比较）。
- 键盘测试剧本 = `focus_sidebar` + （可选）手动置 `selection` + `cx.dispatch_action` + `run_until_parked`；从 dispatch 入口进才能覆盖动作注册层；前提是手动布置还是真实导航达成，取决于测试主题。
- `assert_project_header_has_threads` 是定向断言代表，`find_map + Option` 把「不存在」也编码进失败情形；级联测试的标配是前置校验 + 多路终态断言 + 多次 `run_until_parked` 泵送。
- 写新测试的方法论：先找最接近的既有测试当模板，复用它的 helper 与结构；期望值从实现代码推导；断言拿不准时先 `println!` + `-- --nocapture` 看真实快照再固化。

## 7. 下一步学习建议

- **下一讲 u9-l3（通知、导入横幅与调试工具）** 是单元九收官：`notified_threads` 的产生路径（`test_background_thread_completion_triggers_notification`）、`DumpWorkspaceInfo` 调试动作。本讲 4.1 看到的 `(!)` 标记将在那里找到完整来路。
- **组合型测试进阶阅读**：`test_search_then_keyboard_navigate_and_confirm`（[src/sidebar_tests.rs:L4554](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L4554)，搜索 + 导航 + 确认三阶段串联）与 `test_workspace_lifecycle`（[src/sidebar_tests.rs:L902](https://github.com/zed-industries/zed/blob/d9ad6aff67e47de43abb270d22de75dd950f1b48/crates/sidebar/src/sidebar_tests.rs#L902)，多工作区生命周期）展示了本讲三类测试风格的组合运用。
- **实践建议**：给你自己在前面各讲做本地实验时观察到的行为各补一个测试（比如 u5-l3 的三条退出搜索路径），跑绿后再读一遍 diff，检验自己的测试是否做到了「锁契约、不锁实现」。
