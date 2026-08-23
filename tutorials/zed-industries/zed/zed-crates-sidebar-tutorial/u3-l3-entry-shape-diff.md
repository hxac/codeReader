# EntryShape 与 apply_list_state_diff：保留列表测量值

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `EntryShape` 作为「等高行身份键」的设计动机：为什么相等形状必须渲染出相同高度。
2. 读懂 `entry_shapes` 如何从当前世界状态现场推导形状序列，以及为什么折叠状态要实时查询 `MultiWorkspace`。
3. 手动推演 `apply_list_state_diff` 的前缀/后缀对齐算法，给定新旧两个形状序列算出 `prefix_len`、`suffix_len` 与最终 `splice` 参数。
4. 说明为什么一次纯状态更新（如线程重命名、Running→Completed）不应触碰 `ListState` 的测量缓存，以及测量丢失如何导致粘性项目头闪烁。

本讲承接 u3-l2 的重建管线：`update_entries` 五步中的第四步「差异应用」正是本讲的主角。

## 2. 前置知识

### 虚拟列表与「测量」

gpui 的 `list()` 是虚拟列表：任意时刻只渲染视口附近的少量行。每行第一次被渲染时会**测量**（measure）出自己的实际高度，并存入 `ListState` 内部的一棵平衡树（`SumTree`）。列表总高度、滚动条长度、某行的屏幕坐标，都来自这些缓存的高度值——没渲染过的行没有真实高度，只有估计值。

关键类型是 `ListItem`，它有两个形态：

- `ListItem::Measured { size, .. }`：已渲染过，`size` 是实测高度。
- `ListItem::Unmeasured { size_hint, .. }`：未渲染，只有可选的高度提示。

### splice：告诉列表「哪一段变了」

`ListState::splice(old_range, count)` 的语义是：把下标区间 `old_range` 内的旧项替换为 `count` 个**未测量**的新项。区间之外的项——连同它们的实测高度——原样保留。它是 `ListState` 结构性变更的唯一常规入口。

### 粘性头部为什么依赖测量

侧边栏的项目分组头是「粘性」的：滚动时它吸附在列表顶部，下一个分组头把它顶出去。这个「顶出去」的偏移量是现场算出来的：读取**下一个分组头**的实测边界 `bounds_for_item(next_idx)`，与视口边界求交。如果这个项的测量刚刚被丢掉，`bounds_for_item` 会返回 `None`，偏移量退回 `0`，头部就会在「被顶出去」和「完全贴顶」两个位置之间跳一下——这就是闪烁（flicker）。

### 全量重推导带来的矛盾

u3-l2 讲过本 crate 的核心约束：任何事件都触发 `update_entries → rebuild_contents` 从零重建整个 `contents.entries`。矛盾在于——`ListState` 里缓存的测量值是按下标索引的，如果每次重建都把列表当作「全新的一批行」（例如全量 `splice(0..old_len, new_len)`），所有已测量的行都会被打回未测量状态，粘性头部就闪了。`EntryShape` 与 `apply_list_state_diff` 就是解决这个矛盾的机制。

## 3. 本讲源码地图

| 文件 | 本讲关注的片段 | 作用 |
| --- | --- | --- |
| `crates/sidebar/src/sidebar.rs` | `EntryShape` 枚举（L484-L499） | 行的身份键，相等形状 ⟹ 相同高度 |
| `crates/sidebar/src/sidebar.rs` | `update_entries`（L1992-L2021） | 差异应用的调用点：重建前快照、重建后对齐 |
| `crates/sidebar/src/sidebar.rs` | `apply_list_state_diff`（L2023-L2051） | 前缀/后缀对齐算法，计算最小 splice 区间 |
| `crates/sidebar/src/sidebar.rs` | `entry_shapes`（L2053-L2071） | 从 `contents.entries` 现场推导形状序列 |
| `crates/sidebar/src/sidebar.rs` | `render_sticky_header`（L3142-L3207） | 消费测量值的下游：粘性头部偏移计算 |
| `crates/gpui/src/elements/list.rs` | `ListState::new` / `splice` / `splice_focusable` / `bounds_for_item` | gpui 侧的测量缓存与拼接语义 |
| `crates/sidebar/src/sidebar_tests.rs` | 三个防回归测试（L606-L751） | 锁定「同形状不丢测量、变形状必须换键」两条纪律 |

## 4. 核心概念与源码讲解

### 4.1 EntryShape：等高行的身份键

#### 4.1.1 概念说明

`EntryShape` 回答的问题是：**「重建前后列表里的两个下标，指的是同一块屏幕空间吗？」**

它是 `ListEntry` 的一个精简投影——只保留影响行高的字段，丢弃其余一切：

- `ProjectHeader` 行的高度取决于三件事：分组键 `key`（决定是哪一组）、`has_threads` 与 `is_collapsed`（两者共同决定是否额外渲染一行「No threads yet」占位行）。
- `Thread` 行与 `Terminal` 行的高度不随元数据内容变化——标题改了、状态徽标换了、通知点亮了，行高都不变——所以只用 `ThreadId` / `TerminalId` 作形状。

它本质上是 gpui `list()` 的**稳定性契约**：`list()` 按「下标 → 渲染闭包」工作，并不理解行的语义。侧边栏在重建后想保住旧行的测量值，就必须自己定义「哪些下标之间的行是等价的」，`EntryShape` 就是这个等价关系的编码。

#### 4.1.2 核心流程

```
ListEntry（完整行数据）          EntryShape（身份键）
├─ ProjectHeader                ├─ ProjectHeader { key, has_threads, is_collapsed }
│   ├─ key                    ──►│     （三者都影响行高）
│   ├─ has_threads            ──►│
│   ├─ is_collapsed（实时查询）──►│
│   └─ label / 高亮 / 徽标…   ──►  ✂ 丢弃（不影响行高）
├─ Thread                       ├─ Thread(thread_id)
│   └─ 标题/状态/通知/时间…    ──►  ✂ 丢弃
└─ Terminal                     └─ Terminal(terminal_id)
    └─ 标题/通知…              ──►  ✂ 丢弃
```

契约方向是单向且强制的：**相等形状必须渲染出相同高度**。反过来，形状不同的两行高度也可能碰巧相等——没关系，把相异的行当作「变了」只是多付一次测量，不会错；把高度不同的行当作「没变」才会出错。

#### 4.1.3 源码精读

枚举定义及其文档注释——整段是理解本讲的钥匙：

[crates/sidebar/src/sidebar.rs:L484-L499](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L484-L499)

```rust
/// Identity-and-layout key for a [`ListEntry`] used to preserve measured list items
/// across rebuilds. Equal shapes must render to the same height; add any new
/// height-affecting state here.
#[derive(Debug, PartialEq, Eq)]
enum EntryShape {
    ProjectHeader {
        key: ProjectGroupKey,
        // Toggles the "No threads yet" empty-state row when not collapsed.
        has_threads: bool,
        // Determines whether the "No threads yet" row is rendered (only shown when
        // `!is_collapsed && !has_threads`).
        is_collapsed: bool,
    },
    Thread(ThreadId),
    Terminal(TerminalId),
}
```

这段代码定义了三种行的身份键。注意 `#[derive(Debug, PartialEq, Eq)]`——只派生相等比较、不派生 `Hash`，因为它只用于逐项比对，不做哈希键。文档注释中的「add any new height-affecting state here」是对未来贡献者的纪律要求：给行加任何影响高度的新状态，必须同步加进形状，否则 `apply_list_state_diff` 会把高度变了的行误判为「没变」，跳过重新测量。

为什么 `has_threads` 和 `is_collapsed` 必须进形状？看空态行的渲染条件：

[crates/sidebar/src/sidebar.rs:L2455-L2477](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2455-L2477)

```rust
if !is_collapsed && !has_threads {
    v_flex()
        .w_full()
        .child(header)
        .child(h_flex().px_2().pt_1().pb_2() /* … "No threads yet" 行 … */)
        .into_any_element()
} else {
    header.into_any_element()
}
```

同是 `ProjectHeader` 行，展开且无线程时比其他情况**多渲染一个子行**，高度显著不同。所以 `has_threads` 从 false 翻转为 true（比如第一条线程出现在空项目里）必须算作「形状变了」，强制该行重新测量。

#### 4.1.4 代码实践

1. **实践目标**：验证「形状相等但内容不同」的行确实等高、「形状不同」的行可以等高也可以不等高。
2. **操作步骤**：
   - 阅读 [crates/sidebar/src/sidebar.rs:L2164-L2172](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2164-L2172) 的 `render_list_entry`，确认下标 `ix` 直接索引 `self.contents.entries`——`list()` 渲染的行数据永远来自最新一次重建。
   - 阅读线程行渲染 `render_thread`（用 Grep 在 sidebar.rs 中搜索 `fn render_thread`），观察标题被截断/单行显示，不随文本长度换行。
3. **需要观察的现象**：行内哪些视觉元素变化、哪些不变；特别留意状态图标、通知圆点是否改变行高。
4. **预期结果**：线程行的所有内容变化（标题、状态、通知）都不改变行高，因此不进形状；项目头的空态子行改变行高，因此 `has_threads`/`is_collapsed` 进形状。
5. 结论属于源码阅读判断，无需运行验证；若想实证，可在本地用 `visual_test_runner`（仅 macOS）截图对比。

#### 4.1.5 小练习与答案

**练习 1**：假设未来给项目头加一个「运行中线程计数徽标」，当计数从 0 变为 1 时徽标出现。它需要加进 `EntryShape::ProjectHeader` 吗？

**答案**：不需要。现有代码里 `has_running_threads`、`waiting_thread_count` 已经存在于 `ListEntry::ProjectHeader` 中（见 [crates/sidebar/src/sidebar.rs:L1931-L1940](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1931-L1940) 的构造处），但不在 `EntryShape` 里——因为徽标不改变行高。判断标准只有一个：**该状态是否影响行的渲染高度**。只有像「No threads yet」那样增删子元素的字段才进形状。

**练习 2**：为什么 `EntryShape` 不派生 `Hash`、不实现 `Ord`？

**答案**：它的唯一用途是被 `apply_list_state_diff` 逐项做相等比较（`*prev == next`），既不做 `HashMap` 键也不排序。只派生实际需要的 trait，符合最小接口原则；未来若有哈希需求再补。

### 4.2 entry_shapes：形状序列的现场推导

#### 4.2.1 概念说明

`entry_shapes` 是一个迭代器方法：把 `contents.entries`（当前已重建好的行序列）逐个投影成 `EntryShape`。它被调用两次——重建前一次（基于旧 `contents`）、重建后一次（基于新 `contents`）——两次输出的序列就是 diff 算法的输入。

最值得注意的设计：`is_collapsed` **不存**在 `contents.entries` 里，而是投影时实时查询 `MultiWorkspace`。这延续 u2-l2 的结论：折叠状态由宿主 `MultiWorkspace` 持有（这样关闭再重开侧边栏、乃至跨窗口都能保持），侧边栏不复制一份。

#### 4.2.2 核心流程

```
contents.entries:  [Header(A), Thread(t1), Thread(t2), Terminal(x1), Header(B), …]
                         │          │          │            │          │
entry_shapes 投影  ▼          ▼          ▼            ▼          ▼
形状序列:          [H(A,ht,ic), T(t1),   T(t2),      X(x1),    H(B,ht,ic)]
                    └─ ic 实时查 multi_workspace.group_state_by_key(A)
```

每次调用都从头现查，从不缓存——「形状是当前世界状态的纯函数」，与 u3-l2 的全量重推导教义一致。

#### 4.2.3 源码精读

[crates/sidebar/src/sidebar.rs:L2053-L2071](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2053-L2071)

```rust
fn entry_shapes<'a>(
    &'a self,
    multi_workspace: &'a MultiWorkspace,
) -> impl Iterator<Item = EntryShape> + 'a {
    self.contents.entries.iter().map(move |entry| match entry {
        ListEntry::ProjectHeader { key, has_threads, .. } => EntryShape::ProjectHeader {
            key: key.clone(),
            has_threads: *has_threads,
            is_collapsed: multi_workspace
                .group_state_by_key(key)
                .map(|state| !state.expanded)
                .unwrap_or(false),
        },
        ListEntry::Thread(thread) => EntryShape::Thread(thread.metadata.thread_id),
        ListEntry::Terminal(terminal) => EntryShape::Terminal(terminal.metadata.terminal_id),
    })
}
```

这段代码遍历当前 `contents.entries`，把每个行投影成身份键。三个细节：

- `is_collapsed` 用 `!state.expanded` 反向得出，找不到分组状态时默认 `false`（未折叠）——保守默认值。
- `..` 忽略了 `ProjectHeader` 的大量字段（label、highlight、徽标、is_active），它们不影响高度。
- 借用签名 `&'a self` + `&'a MultiWorkspace`：零拷贝（除 `key` 克隆外），直接在两个借用量上做惰性迭代。

再看快照的取用点，在 `update_entries` 里：

[crates/sidebar/src/sidebar.rs:L2001-L2010](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2001-L2010)

```rust
let had_notifications = self.has_notifications(cx);
let previous_shapes: Vec<EntryShape> =
    self.entry_shapes(multi_workspace.read(cx)).collect();

self.rebuild_contents(cx);
self.refresh_refilled_draft_times(cx);
self.refresh_draft_editor_observations(cx);

// Preserve measurements for unchanged entries so sticky headers do not flicker.
self.apply_list_state_diff(&previous_shapes, multi_workspace.read(cx));
```

这段代码在重建**前**把旧形状收集成 `Vec`（此时 `contents` 还是旧的），重建**后**由 `apply_list_state_diff` 内部再次调用 `entry_shapes` 拿新序列。注释「Preserve measurements for unchanged entries so sticky headers do not flicker」一句话点破本讲主旨。

#### 4.2.4 代码实践

1. **实践目标**：确认折叠状态变化会被形状序列捕获，且不需要侧边栏自己存任何字段。
2. **操作步骤**：
   - 用 Grep 在 sidebar.rs 搜索 `toggle_collapse`，找到折叠切换函数，观察它调用 `MultiWorkspace` 的什么方法、是否直接调用 `schedule_update_entries`。
   - 对照测试 [crates/sidebar/src/sidebar_tests.rs:L720-L751](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar_tests.rs#L720-L751)：`test_collapse_changes_entry_shape` 在 `toggle_collapse` 前后各收集一次 `entry_shapes`，断言 `assert_ne!`。
3. **需要观察的现象**：折叠前后形状序列的差异——`ProjectHeader` 形状中 `is_collapsed` 翻转，且组内 `Thread`/`Terminal` 形状从序列中消失。
4. **预期结果**：`assert_ne!` 通过，证明折叠必然改变形状序列，diff 会把整个受影响区间打回未测量。
5. 可在本地运行 `cargo test -p sidebar test_collapse_changes_entry_shape` 验证（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `entry_shapes` 每次都实时查询 `group_state_by_key`，而不是在 `rebuild_contents` 时把 `is_collapsed` 写进 `ListEntry::ProjectHeader`？

**答案**：两个原因。其一，折叠状态的权威归属是 `MultiWorkspace`（u2-l2 讲过：组内工作区全部关闭后键仍保留，跨视图共享），侧边栏复制一份会造成双份真相。其二，投影是纯函数、现查零成本（一次 `HashMap` 查找），没必要提前物化。若物化进 `ListEntry`，反而要小心重建时机与宿主状态不同步的问题。

**练习 2**：`update_entries` 为什么把「快照旧形状」放在 `rebuild_contents` **之前**，而不是之后从旧 `contents` 里再取？

**答案**：`rebuild_contents` 的最后一步是整体替换 `self.contents = SidebarContents { … }`（[crates/sidebar/src/sidebar.rs:L1965-L1971](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L1965-L1971)），旧 `contents` 会被丢弃。必须在覆盖前收集旧形状，diff 才有「前」可比较。

### 4.3 apply_list_state_diff：前缀/后缀对齐算法

#### 4.3.1 概念说明

有了前后两个形状序列，任务是算出**最小的 splice 区间**：找一个连续下标区间 `[prefix_len, 旧长度 - suffix_len)`，它覆盖了所有差异；区间之外的行不动，测量值原样保留。

算法是最长公共前缀 + 最长公共后缀的对齐（不是最长公共子序列——那太贵也太复杂，且行序列的变更几乎总是局部的）。整个过程等价于编辑 diff 里的「两端夹逼」：

```
旧序列:  [ ══ 公共前缀 ══ ][ ══ 变化区 ══ ][ ══ 公共后缀 ══ ]
新序列:  [ ══ 公共前缀 ══ ][ ══ 替换内容 ══ ][ ══ 公共后缀 ══ ]
                            ↑ splice(old_changed, new_changed_count) 只动这一段
```

#### 4.3.2 核心流程

伪代码（保持与真实代码相同的分支顺序）：

```
函数 apply_list_state_diff(previous_shapes, multi_workspace):
    new_iter ← 按当前 contents 现推导的新形状迭代器
    prefix_len ← 0
    循环:
        取 prev ← previous_shapes[prefix_len]，next ← new_iter.next()
        两者都有且相等     → prefix_len += 1，继续
        两者都耗尽 (None,None) → 直接 return          ← 序列完全相同，什么都不做
        否则               → 跳出，记 leading = next   ← 第一个差异处的新形状

    new_tail  ← [leading…] + new_iter 剩余部分          ← 变化区起点之后的所有新形状
    prev_tail ← previous_shapes[prefix_len..]           ← 变化区起点之后的所有旧形状

    suffix_len ← 从两段尾部向前逐对比较，统计相等的对数（zip 到短者为止）

    old_changed       ← prefix_len .. previous_shapes.len() - suffix_len
    new_changed_count ← new_tail.len() - suffix_len
    list_state.splice(old_changed, new_changed_count)
```

关键不变量：

- `suffix_len ≤ min(prev_tail.len(), new_tail.len())`（`zip` 自动截断到短迭代器），因此 `old_changed` 的起点永远不会越过终点，区间恒有效。
- 后缀比较在**去掉前缀之后的尾段**上进行，避免同一段被前后缀重复计数。
- 完全相同的序列在 `(None, None)` 分支提前返回——**连一次空 splice 都不发**，`ListState` 完全不被触碰。

#### 4.3.3 源码精读

完整函数：

[crates/sidebar/src/sidebar.rs:L2023-L2051](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L2023-L2051)

```rust
/// Splices only the changed entry range, leaving unchanged item measurements intact.
fn apply_list_state_diff(
    &self,
    previous_shapes: &[EntryShape],
    multi_workspace: &MultiWorkspace,
) {
    let mut new_iter = self.entry_shapes(multi_workspace);
    let mut prefix_len = 0;
    let mut leading_new = loop {
        match (previous_shapes.get(prefix_len), new_iter.next()) {
            (Some(prev), Some(next)) if *prev == next => prefix_len += 1,
            (None, None) => return,
            (_, leading) => break leading,
        }
    };

    let new_tail: Vec<EntryShape> = leading_new.into_iter().chain(new_iter).collect();
    let prev_tail = &previous_shapes[prefix_len..];
    let suffix_len = prev_tail
        .iter()
        .rev()
        .zip(new_tail.iter().rev())
        .take_while(|(prev, next)| prev == next)
        .count();

    let old_changed = prefix_len..previous_shapes.len() - suffix_len;
    let new_changed_count = new_tail.len() - suffix_len;
    self.list_state.splice(old_changed, new_changed_count);
}
```

逐段解读这段差异对齐代码：

- **前缀循环（L2030-L2037）**：`loop` + `match` 元组匹配是 Rust 里「双迭代器同步推进」的惯用法。三个分支依次是：相等则前进；双方同时耗尽则整体相同、直接返回（这是纯状态更新的常见路径）；其余任何组合（一方先尽、或遇到首个差异）都跳出，并把**新序列在差异处的那个形状**（可能是 `None`）保存为 `leading_new`。
- **拼出新尾段（L2039）**：`leading_new.into_iter().chain(new_iter)` 把刚才「看了一眼但没消费进前缀」的那个形状接回迭代器剩余部分——这一眼不能丢，否则新序列会缺一项。
- **后缀计数（L2041-L2046）**：两个尾段反转后 `zip`，`take_while` 统计从尾部起连续相等的对数。注意比较的对象是 `prev_tail` 与 `new_tail`（都是去掉公共前缀之后的尾段），不是整个序列。
- **区间计算与拼接（L2048-L2050）**：旧侧变化区是 `[prefix_len, 旧总长 - suffix_len)`，新侧变化区长度是 `new_tail.len() - suffix_len`。`splice` 之外的一切下标保持原测量。

三个边界情形的推演（建议自己笔算一遍）：

| 情形 | 旧序列 | 新序列 | prefix | suffix | splice |
| --- | --- | --- | --- | --- | --- |
| 纯插入 | `[A]` | `[A, B]` | 1 | 0（`prev_tail` 为空，zip 立即结束） | `splice(1..1, 1)`，空旧区间 = 插入 |
| 纯删除 | `[A, B]` | `[A]` | 1 | 0（`new_tail` 为空） | `splice(1..2, 0)`，新计数 0 = 删除 |
| 完全相同 | `[A, B]` | `[A, B]` | — | — | `(None, None)` 提前 return，零调用 |

而这是本 crate 里对 `list_state` 的**唯一**结构性写入——在 sidebar.rs 中全文搜索 `list_state`，除 `splice` 外只有滚动 API（`scroll_to_reveal_item`、`logical_scroll_top` 等）和渲染时的 `list(self.list_state.clone(), …)`。侧边栏从不调用 `ListState::reset`。

#### 4.3.4 代码实践

本讲的主实践（也即综合实践的第一部分）：**手动模拟一次差异计算**。

1. **实践目标**：不经运行，纯手工推出 `apply_list_state_diff` 对给定输入产生的 splice 参数。
2. **操作步骤**：
   - 设 `previous_shapes = [A, B, C, D]`，新序列 = `[A, X, C, D]`（B 被替换为 X）。
   - 按伪代码逐步执行，写下每一步的 `prefix_len`、`leading_new`、`new_tail`、`prev_tail`、`suffix_len`。
   - 最后写出 `old_changed` 区间与 `new_changed_count`，以及 `splice` 调用。
3. **需要观察的现象**：差异只落在下标 1 一处；A、C、D 三行不受影响。
4. **预期结果**（可对照验算）：
   - 前缀：`A == A` → `prefix_len = 1`；接着 `B != X` → 跳出，`leading_new = Some(X)`。
   - `new_tail = [X, C, D]`（长度 3），`prev_tail = [B, C, D]`（长度 3）。
   - 后缀（倒序逐对）：`D == D` ✓、`C == C` ✓、`B != X` ✗ → `suffix_len = 2`。
   - `old_changed = 1..(4 - 2) = 1..2`，`new_changed_count = 3 - 2 = 1`。
   - 最终调用：`list_state.splice(1..2, 1)`——只把下标 1 的旧项 B 换成 1 个未测量的新项 X。
5. 手工推演可自行验算，无需运行；若想用代码验证，见 5. 综合实践。

#### 4.3.5 小练习与答案

**练习 1**：旧序列 `[A, B, C]`，新序列 `[A, B]`（尾部删除）。算法会怎么走？

**答案**：前缀推进到 `prefix_len = 2`（A、B 相等），随后匹配到 `(Some(C), None)`——旧序列还有、新序列耗尽——落入 `(_, leading) => break leading` 分支，`leading_new = None`。`new_tail = None.into_iter().chain(空) = []`，`prev_tail = [C]`，后缀 zip 立即为空，`suffix_len = 0`。得 `splice(2..3, 0)`：删除下标 2。注意 `Option::into_iter` 的巧妙用法让「差异处是删除」也能统一走同一段拼接代码。

**练习 2**：如果 diff 采用了「每次全量 `splice(0..old_len, new_len)``」的偷懒实现，测试里哪一个会失败？为什么？

**答案**：`test_thread_metadata_update_preserves_sticky_header_measurements`（[crates/sidebar/src/sidebar_tests.rs:L606-L685](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar_tests.rs#L606-L685)）会失败。该测试在线程重命名（形状序列完全不变）后断言第二个项目头的 `bounds_for_item` 前后相等；全量 splice 会把所有项打回 `Unmeasured`，`bounds_for_item` 按 gpui 的实现只对 `ListItem::Measured` 返回 `Some`（[crates/gpui/src/elements/list.rs:L726-L736](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/elements/list.rs#L726-L736)），于是 `.expect("same-shape metadata update should preserve next header measurements")` 直接 panic。

**练习 3**：为什么用「前缀+后缀」而不是标准 LCS（最长公共子序列）diff？

**答案**：三点。其一，侧边栏的变更模式是局部的（时间排序下的增删改、分组折叠），前缀+后缀对齐已覆盖绝大多数场景，最坏也只多 splice 几行——多测量是**安全**的（形状不等只是保守判断）。其二，LCS 是 O(n·m) 的，还要构造映射，每次重建都跑一遍不划算。其三，splice 语义本身就是连续区间，前缀+后缀对齐天然产出一个连续变化区，与 API 严丝合缝。

### 4.4 ListState::splice 与粘性头部：为什么不能重置测量

#### 4.4.1 概念说明

最后看 gpui 一侧：`splice` 到底对测量缓存做了什么，以及测量丢失如何沿调用链传导到粘性头部。这条因果链是：

```
全量重置/过宽 splice
  → 区间内 ListItem 全部变回 Unmeasured
    → bounds_for_item(next_header) 返回 None（只认 Measured）
      → render_sticky_header 的 top_offset 兜底为 0
        → 粘性头部在「贴顶」与「被顶出」之间跳变一帧 = 闪烁
```

#### 4.4.2 核心流程

`splice_focusable`（`splice` 的实际实现）分三步：

1. 在 `SumTree` 上定位 `old_range.start`，切出**前段**（原样保留，含测量值）。
2. 跳过 `old_range`（旧项连同测量值一起丢弃），接上 `count` 个 `ListItem::Unmeasured`。
3. 拼回 `old_range.end` 之后的**后段**（原样保留）。

之后还有一段滚动锚定：若当前滚动锚点落在被替换区间内，锚点重置到区间起点；若在区间之后，按下标位移量平移——用户正在看的行不会因为中间插入/删除而跳走。

#### 4.4.3 源码精读

`ListState` 的构造与 splice 入口：

[crates/gpui/src/elements/list.rs:L501-L505](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/elements/list.rs#L501-L505)

```rust
/// Inform the list state that the items in `old_range` have been replaced
/// by `count` new items that must be recalculated.
pub fn splice(&self, old_range: Range<usize>, count: usize) {
    self.splice_focusable(old_range, (0..count).map(|_| None))
}
```

这段代码是列表结构性变更的公开入口：`old_range` 内的项被替换为 `count` 个待重算的新项。侧边栏传 `None` 焦点句柄（行内焦点由行的渲染元素自行管理）。

替换的核心（节选自 `splice_focusable`）：

[crates/gpui/src/elements/list.rs:L522-L535](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/elements/list.rs#L522-L535)

```rust
let mut spliced_count = 0;
new_items.extend(
    focus_handles.into_iter().map(|focus_handle| {
        spliced_count += 1;
        ListItem::Unmeasured {
            size_hint: None,
            focus_handle,
        }
    }),
    (),
);
new_items.append(old_items.suffix(), ());
```

这段代码重建 `SumTree`：新项一律以 `Unmeasured` 进入（无高度提示），而 `old_items.suffix()`——区间之后的旧项——带着它们的实测高度原封不动地拼回。这就是「区间外测量保留」的机制本体。

滚动锚定的补偿：

[crates/gpui/src/elements/list.rs:L537-L548](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/elements/list.rs#L537-L548)

```rust
if let Some(ListOffset { item_ix, offset_in_item }) = state.logical_scroll_top.as_mut() {
    if old_range.contains(item_ix) {
        *item_ix = old_range.start;
        *offset_in_item = px(0.);
    } else if old_range.end <= *item_ix {
        *item_ix = *item_ix - (old_range.end - old_range.start) + spliced_count;
    }
}
```

这段代码在拼接后修正滚动位置：锚点行被替换时对齐到新区间首行，锚点行在区间之后时按下标净位移平移。区间越精准，这条补偿触发的概率越低、滚动越稳——这也是最小 splice 的第二重收益。

再看消费端。粘性头部如何用测量值算「被顶出去」的偏移：

[crates/sidebar/src/sidebar.rs:L3195-L3207](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L3195-L3207)

```rust
let top_offset = self
    .contents
    .project_header_indices
    .iter()
    .find(|&&idx| idx > header_idx)
    .and_then(|&next_idx| {
        let bounds = self.list_state.bounds_for_item(next_idx)?;
        let viewport = self.list_state.viewport_bounds();
        let y_in_viewport = bounds.origin.y - viewport.origin.y;
        let header_height = bounds.size.height;
        (y_in_viewport < header_height).then_some(y_in_viewport - header_height)
    })
    .unwrap_or(px(0.));
```

这段代码计算粘性头部的纵向偏移：找下一个分组头，问它的实测边界是否已探入视口顶部，若是则让当前粘性头上移让位。`?` 与 `unwrap_or(px(0.))` 两处兜底意味着——`bounds_for_item` 一旦返回 `None`（项未测量），偏移直接归零，头部瞬间贴顶。而 `bounds_for_item` 只认已测量的项：

[crates/gpui/src/elements/list.rs:L726-L736](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/elements/list.rs#L726-L736)

```rust
if let Some(&ListItem::Measured { size, .. }) = cursor.item() {
    let &Dimensions(Count(count), Height(top), _) = cursor.start();
    if count == ix {
        // …用实测高度计算窗口坐标并返回 Some(…)…
    }
}
None
```

这段代码表明：非 `Measured` 的项拿不到边界。测试文件里有一条注释把整个因果链写得明明白白：

[crates/sidebar/src/sidebar_tests.rs:L687-L692](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar_tests.rs#L687-L692)

> When a thread's status changes (e.g. Running -> Completed after sending a message), the shape sequence is unchanged, so `update_entries` should not reset the underlying `ListState`. Resetting throws away measured item bounds for one frame, which makes the sticky project header flicker between its pushed-off and fully-on-screen positions.

最后补两个背景事实：`list_state` 的构造在 [crates/sidebar/src/sidebar.rs:L895](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L895)——`ListState::new(0, gpui::ListAlignment::Top, px(1000.))`，初始 0 项、上下各 1000px 的 overdraw（视口外的预渲染缓冲区，见 [crates/gpui/src/elements/list.rs:L310-L313](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/gpui/src/elements/list.rs#L310-L313) 的文档）；渲染处把 `list_state` 与 `render_list_entry` 一起交给 gpui 的 `list()` 元素（[crates/sidebar/src/sidebar.rs:L7865-L7870](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar.rs#L7865-L7870)）。

#### 4.4.4 代码实践

1. **实践目标**：通过运行防回归测试，实证「同形状更新保留测量」这条纪律。
2. **操作步骤**：在仓库根目录运行：

   ```bash
   cargo test -p sidebar test_thread_metadata_update_preserves_sticky_header_measurements
   ```

   然后阅读测试体（[crates/sidebar/src/sidebar_tests.rs:L606-L685](https://github.com/zed-industries/zed/blob/f36aec822be697df9049fed020b593147c93b4cf/crates/sidebar/src/sidebar_tests.rs#L606-L685)），注意它的三段结构：先 `cx.draw` 两次把列表滚到第二个项目头附近并完成测量（`bounds_before` 带 `.expect("…should be measured before metadata update")`）；再用新标题/新时间 `save_thread_metadata` 触发一次重建；最后断言 `bounds_after == bounds_before`。
3. **需要观察的现象**：测试通过；并且能说出为什么重命名不改变形状序列（同一 `ThreadId`，时间戳虽更新但两个线程的相对顺序未变）。
4. **预期结果**：测试绿。若把 `apply_list_state_diff` 的调用替换成 `self.list_state.reset(self.contents.entries.len())`（仅本地实验，勿提交），该测试应因 `.expect` panic 而变红。
5. 本环境未实际执行，输出「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`splice` 之后，被替换区间内的新行显示什么高度？

**答案**：`Unmeasured` 且 `size_hint: None`——没有可靠高度。在下一次布局把它们渲染出来之前，列表总高度和滚动条都基于估计值。这就是为什么算法要尽量缩小区间：区间外的高度是**实测的**，区间内的要等一帧。

**练习 2**：用户正滚动查看下标 10 的行，此时前插了 3 行（splice 的 `old_range` 为 `5..5`，count 3）。滚动锚定怎么处理？

**答案**：锚点 `item_ix = 10` 满足 `old_range.end (5) <= 10`，走第二个分支：`item_ix = 10 - (5 - 5) + 3 = 13`——锚点跟着内容平移，用户看到的还是原来那行。这正是「插入后视口不跳」的保障，也是精准 splice 的附带收益：若 splice 区间比必要的大，区间内的锚点会被强制重置到区间起点（第一个分支），视口就跳了。

**练习 3**：粘性头部的闪烁为什么恰好是「一帧」而不是持续错位？

**答案**：`Unmeasured` 项在下一帧布局时会被实际渲染并测量回 `Measured`，随后 `bounds_for_item` 恢复返回真值。所以丢测量的代价是一帧的估计值错误——对人眼恰好是一次可见的闪跳。这也是测试注释用「for one frame」措辞的原因。

## 5. 综合实践

**任务：手工 diff + 运行验证 + 故意破坏，三步吃透最小拼接。**

**第一步（手工模拟）**：完成 4.3.4 的差异计算——`previous_shapes = [A,B,C,D]`、新序列 `[A,X,C,D]`，逐步写出 `prefix_len = 1`、`leading_new = Some(X)`、`new_tail = [X,C,D]`、`prev_tail = [B,C,D]`、`suffix_len = 2`、`splice(1..2, 1)`。再追加两问自查：若新序列是 `[A,B,C,D,X]`（纯尾部追加）应得 `splice(4..4, 1)`；若是 `[X,A,B,C,D]`（头部插入）应得 `prefix_len = 0`、`suffix_len = 4`、`splice(0..1, 1)`——体会「中间变两端保、两端变整段保」的行为。

**第二步（运行验证）**：

```bash
cargo test -p sidebar test_thread_metadata_update_preserves_sticky_header_measurements
cargo test -p sidebar test_thread_status_update_does_not_reset_list_measurements
cargo test -p sidebar test_collapse_changes_entry_shape
```

三个测试分别锁定三条纪律：同形状更新不丢测量（L606-L685）、无操作重建产出完全相同的形状序列（L687-L718，断言两次 `entry_shapes` 收集结果 `assert_eq!`）、折叠必须改变形状序列（L720-L751，断言 `assert_ne!`）。它们共同构成对 `EntryShape` 契约的双向夹逼：**变了不算变 → 闪烁；没变算成变 → 无害但浪费**。前两条防前者，第三条确认真实变更（折叠）确实会传导为形状变化、触发重新测量。

**第三步（本地破坏性实验，做完还原）**：把 `apply_list_state_diff` 的函数体临时替换为一行 `self.list_state.reset(self.contents.entries.len());`，重新运行前两个测试，观察失败信息（`.expect` 处 panic 与「no-op rebuild」断言）；还原后全绿。这一步把「为什么不能重置」从结论变成亲眼所见。

## 6. 本讲小结

- `EntryShape` 是行的「等高身份键」：相等形状必须渲染出相同高度；给行加影响高度的新状态时必须同步加进形状（doc comment 明文要求）。
- `ProjectHeader` 的形状带 `key + has_threads + is_collapsed`（空态子行改变高度），`Thread`/`Terminal` 只带 id（内容变化不改行高）；`is_collapsed` 投影时实时查询 `MultiWorkspace`，不落地副本。
- `apply_list_state_diff` 用「最长公共前缀 + 最长公共后缀」两端夹逼出最小连续变化区，只对该区间调用 `splice`；序列完全相同时在 `(None, None)` 分支提前返回，`ListState` 零触碰。
- `ListState::splice` 把区间内旧项替换为 `Unmeasured` 新项、区间外项带着实测高度原样保留，并补偿滚动锚点；`bounds_for_item` 只对 `Measured` 项返回真值。
- 粘性头部用 `bounds_for_item(下一个分组头)` 计算「被顶出」的偏移，测量一帧丢失就会闪跳——这就是状态更新（重命名、Running→Completed）不能重置测量的全部原因。
- 三个防回归测试（L606-L751）双向锁死契约：同形状不丢测量、无操作重建形状序列恒等、折叠必改形状。

## 7. 下一步学习建议

- 下一讲 u3-l4《rebuild_contents 全景：从项目分组到可见行》将深入 diff 的「上游」——形状序列由谁生成：项目分组遍历、终端多路查询与去重、路径消歧、活跃信息合并与搜索过滤。
- 想巩固 gpui 虚拟列表机制，可通读 `crates/gpui/src/elements/list.rs` 的 `ListState`（滚动、`measure_all`、`with_uniform_item_height`）与 `List` 元素的布局实现，对照本讲的 `SumTree` 测量缓存。
- 想看「测量保留」在交互层的影响，可提前阅读 u5-l2 将讲的键盘导航：`scroll_to_reveal_item` 依赖准确的项边界，正是最小 splice 的受益者之一。
