# hunk 查询 API 家族与过滤机制

## 1. 本讲目标

学完本讲,你应该能够:

1. 在 `hunks`、`hunks_intersecting_range`、`hunks_intersecting_base_text_range`、`hunks_in_row_range`、`range_to_hunk_range` 等接口中,为你的场景选出正确的那一个。
2. 读懂 `hunks_intersecting_range_impl` 的实现机制:SumTree 的 `filter` 剪枝 + `summaries_for_anchors_with_payload` 批量锚点解析。
3. 解释「hunk 结束点向下一行行首扩展」的规则:它什么时候触发、为什么需要、对删除换行符这类编辑意味着什么。
4. 区分 `raw_hunks_intersecting_range`(原始视图)与 `hunks_intersecting_range`(带乐观状态的视图),知道各自被下游谁使用。

本讲只讲「查询」——假设 hunk 已经算好并存在 `BufferDiffSnapshot` 的 SumTree 里(上一讲 u2-l2 的内容);diff 是怎么算出来的,留给单元三。

## 2. 前置知识

### 2.1 快照里存的是什么

上一讲我们说过:`BufferDiffSnapshot` 内部用 `hunks: SumTree<InternalDiffHunk>` 存真实差异,每个 `InternalDiffHunk` 的 buffer 侧位置用**锚点(Anchor)**表示,base 侧位置用**字节区间**表示。本讲的所有查询 API 都是在这棵树上做「区间相交查找」,然后把 `InternalDiffHunk` 组装成公开的 `DiffHunk`(补上 `range: Range<Point>` 和 `secondary_status`)。

### 2.2 锚点为什么不能直接当行号用

`Anchor` 是跨编辑稳定的身份标识,但渲染、staging、跳转都按「行」工作。所以查询 API 的核心工作之一是:**在查询时刻,把存储的锚点解析成当前 buffer 坐标系里的 `Point`**。这一步由 buffer 快照的 `summaries_for_anchors_with_payload` 完成(本讲 4.2 详述)。

### 2.3 「相交」在这里是闭区间语义

本讲所有相交判定都是「触碰即命中」:查询区间的端点恰好贴着 hunk 的边界,也算相交。后面会看到这正是 `hunks_in_row_range(0..3)` 能覆盖「从第 3 行开头开始的 hunk」的原因。

### 2.4 复杂度直觉

SumTree 的 `filter` 让区间查询只需访问 \( O(\log n + k) \) 个节点(\( k \) 为命中 hunk 数,详见 u2-l2 的 filter 谓词作用于子树摘要的剪枝机制);而锚点→Point 的解析被合并成**一次有序批量扫描**,避免了对每个锚点各做一次二分。这是本讲实现里最值得学习的两个工程细节。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/buffer_diff/src/buffer_diff.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs) | 本讲主战场:查询 API 家族(L324–L465)与核心实现 `hunks_intersecting_range_impl`(L1002–L1131)、`hunks_intersecting_range_rev_impl`(L1133–L1156) |
| [crates/text/src/text.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/text/src/text.rs) | `summaries_for_anchors_with_payload`(L2442–L2500):批量把锚点解析成 `Point` 并携带任意载荷 |
| [crates/sum_tree/src/sum_tree.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs) | `SumTree::filter`(L609–L619):返回按摘要剪枝的 `FilterCursor`(u2-l2 已讲 trait,这里只看调用) |
| [crates/editor/src/git.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/git.rs) | 下游用例:编辑器用 `hunks_intersecting_base_text_range` 把 git 侧 hunk 反查成 buffer 侧 hunk(L438–L446) |
| [crates/project/src/git_store.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/project/src/git_store.rs) | 下游用例:staging 路径用 `raw_hunks_intersecting_range`(L1338–L1342) |

## 4. 核心概念与源码讲解

### 4.1 hunks_intersecting_range 系列 API:一张选型表

#### 4.1.1 概念说明

所有正向查询共享同一个底层实现,区别只在**你用哪种坐标表达「我要查哪里」**:

- buffer 锚点(`Range<Anchor>`)——最通用,来自编辑器选区、multi_buffer 等;
- base 字节偏移(`Range<usize>`)——来自 git 侧(hunk 天然按 base 文本的字节描述);
- 行号(`Range<u32>`)——来自渲染层(gutter 按可见行画标记)。

#### 4.1.2 核心流程

| API | 输入坐标 | 内部走向 | 典型调用方 |
| --- | --- | --- | --- |
| `hunks` | 无(全范围) | 转成 min/max 哨兵锚点区间 → `hunks_intersecting_range` | 测试、遍历全部 hunk |
| `hunks_intersecting_range` | buffer 锚点 | filter 按 buffer 坐标剪枝 → `hunks_intersecting_range_impl` | editor/git_ui |
| `hunks_in_row_range` | 行号 | 每端造一个行首锚点 → `hunks_intersecting_range` | 渲染层按行查询 |
| `hunks_intersecting_base_text_range` | base 字节 | filter 按 base 字节剪枝 → `hunks_intersecting_range_impl` | editor 的 git 集成 |
| `hunks_intersecting_range_rev` / `hunks_intersecting_base_text_range_rev` | 同上两式 | `hunks_intersecting_range_rev_impl`(倒序) | 只想要「最后一个」hunk |
| `range_to_hunk_range` | buffer 锚点 | 正向取 first + 反向取 last(见 4.4) | `set_snapshot` 合并变更范围 |
| `raw_hunks_intersecting_range` | buffer 锚点 | 直接 filter,不碰 pending/secondary(见 4.3) | project 的 staging 路径 |

相邻还有一个 `base_text_range_for_buffer_range`(把 buffer 范围映射成 base 字节范围),它走 patch 机制,是下一讲 u2-l4 的主题,这里只留个印象。

#### 4.1.3 源码精读

先看「最通用」的入口,注意 filter 谓词的写法:

[crates/buffer_diff/src/buffer_diff.rs:L324-L338](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L324-L338)

```rust
pub fn hunks_intersecting_range<'a>(
    &'a self,
    range: Range<Anchor>,
    buffer: &'a text::BufferSnapshot,
) -> impl 'a + Iterator<Item = DiffHunk> {
    let unstaged_counterpart = self.secondary_diff.as_deref();
    let range = range.to_offset(buffer);
    let filter = move |summary: &DiffHunkSummary| {
        let summary_range = summary.buffer_range.to_offset(buffer);
        let before_start = summary_range.end < range.start;
        let after_end = summary_range.start > range.end;
        !before_start && !after_end
    };
    self.hunks_intersecting_range_impl(filter, buffer, unstaged_counterpart)
}
```

要点:

- 查询区间先转成**字节偏移**,hunk 摘要的锚点区间也解析成字节偏移,然后在同一坐标系里比较。
- 相交条件是 `!(hunk.end < query.start) && !(hunk.start > query.end)`,即 `hunk.end >= query.start && hunk.start <= query.end`——**闭区间触碰即命中**。
- 这个闭包交给 SumTree 的 `filter`,谓词作用在**子树摘要**(外包络区间)上,整棵不相交的子树直接跳过——这就是 u2-l2 讲过的剪枝,`SumTree::filter` 返回一个 `FilterCursor`:[crates/sum_tree/src/sum_tree.rs:L609-L619](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L609-L619)。

再看 base 侧入口,结构完全对称,只是比较对象换成 `diff_base_byte_range`:

[crates/buffer_diff/src/buffer_diff.rs:L403-L415](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L403-L415)

```rust
pub fn hunks_intersecting_base_text_range<'a>(
    &'a self,
    range: Range<usize>,
    main_buffer: &'a text::BufferSnapshot,
) -> impl 'a + Iterator<Item = DiffHunk> {
    let unstaged_counterpart = self.secondary_diff.as_deref();
    let filter = move |summary: &DiffHunkSummary| {
        let before_start = summary.diff_base_byte_range.end < range.start;
        let after_end = summary.diff_base_byte_range.start > range.end;
        !before_start && !after_end
    };
    self.hunks_intersecting_range_impl(filter, main_buffer, unstaged_counterpart)
}
```

注意:虽然按 base 字节剪枝,**产出的 `DiffHunk` 仍然解析到 `main_buffer` 坐标系**——hunk 的 `range`/`buffer_range` 是 buffer 侧的,只是「找到哪些 hunk」用了 base 坐标。这正是 editor 的 git 集成需要的:git 给出的是 base 字节区间,UI 要的是 buffer 位置,见 [crates/editor/src/git.rs:L436-L446](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/git.rs#L436-L446)——它拿 git 侧 hunk 的 `diff_base_byte_range` 反查,再用 `.find(|hunk| hunk.diff_base_byte_range == diff_base_byte_range)` 精确锁定同一个 hunk。

最后是两个薄封装:

[crates/buffer_diff/src/buffer_diff.rs:L430-L448](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L430-L448)

```rust
pub fn hunks<'a>(
    &'a self,
    buffer_snapshot: &'a text::BufferSnapshot,
) -> impl 'a + Iterator<Item = DiffHunk> {
    self.hunks_intersecting_range(
        Anchor::min_max_range_for_buffer(buffer_snapshot.remote_id()),
        buffer_snapshot,
    )
}

pub fn hunks_in_row_range<'a>(
    &'a self,
    range: Range<u32>,
    buffer: &'a text::BufferSnapshot,
) -> impl 'a + Iterator<Item = DiffHunk> {
    let start = buffer.anchor_before(Point::new(range.start, 0));
    let end = buffer.anchor_after(Point::new(range.end, 0));
    self.hunks_intersecting_range(start..end, buffer)
}
```

- `hunks` 用 `min/max` 哨兵锚点覆盖全 buffer——比 `0..usize::MAX` 更稳,因为哨兵锚点在任何版本里都解析为 buffer 首尾。
- `hunks_in_row_range` 的两端都锚在**行首**,且右端用 `anchor_after`(右偏)。配合闭区间相交语义,`0..3` 会命中「恰好从第 3 行行首开始」的 hunk。

#### 4.1.4 代码实践

**实践目标**:体验闭区间「触边即命中」语义,并验证 `hunks_in_row_range` 与 `hunks_intersecting_range` 的一致性。

**操作步骤**(以下为示例代码,需你自己在 `mod tests` 中添加后运行):

```rust
#[gpui::test]
async fn test_row_range_touching_boundary(cx: &mut gpui::TestAppContext) {
    let diff_base = "zero\none\ntwo\nthree\nfour\nfive\n".to_string();
    let buffer_text = "zero\nONE\ntwo\nthree\nFOUR\nfive\n".to_string();

    let buffer = Buffer::new(ReplicaId::LOCAL, BufferId::new(1).unwrap(), buffer_text);
    let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base, cx);

    // 两个 hunk:行 1..2(one -> ONE)、行 4..5(four -> FOUR)
    // 行号查询 0..1:右端 anchor_after(1, 0) 恰好贴着 hunk 1 的起点
    let hunks_in_rows = diff
        .hunks_in_row_range(0..1, &buffer)
        .map(|hunk| hunk.range)
        .collect::<Vec<_>>();
    assert_eq!(
        hunks_in_rows,
        vec![Point::new(1, 0)..Point::new(2, 0)]
    );

    // 行号查询 2..3:落在两个 hunk 之间的未改动区域,但右端贴着行 3 结束,
    // 不触碰任何 hunk 边界,应为空
    assert_eq!(diff.hunks_in_row_range(2..3, &buffer).count(), 0);
}
```

**需要观察的现象**:第一个断言通过——查询范围 `0..1` 本身不包含行 1,却命中了行 1..2 的 hunk,因为 `anchor_after(Point::new(1, 0))` 与 hunk 起点「贴边」。

**预期结果**:两个断言均通过(基于源码闭区间语义推得,待本地验证)。

#### 4.1.5 小练习与答案

**练习 1**:`hunks_intersecting_range` 的 filter 里,如果把 `after_end` 写成 `summary_range.start >= range.end`(把触边改成不相交),`hunks_in_row_range(0..1)` 还能命中行 1..2 的 hunk 吗?

**答案**:不能。`hunk.start`(行 1 首偏移)等于 `range.end`(行 1 首偏移),`>=` 会把它判为 after_end 而过滤掉。这解释了为什么源码用严格的 `>` 与 `<`:让端点相等的「贴边」保持命中,行对齐的调用方(如 gutter)才不会漏掉恰好从查询边界开始的 hunk。

**练习 2**:为什么 `hunks()` 用 `Anchor::min_max_range_for_buffer` 而不是构造 `buffer.anchor_before(0)..buffer.anchor_before(buffer.len())`?

**答案**:min/max 哨兵锚点是两个保留极值,任何编辑都不会让它们「落到行中间」或在版本间失效;而显式首尾锚点在 buffer 后续被编辑后解析可能偏移。此外 `summaries_for_anchors_with_payload` 对 `is_min()`/`is_max()` 有 O(1) 快路径(见 4.2.3),哨兵锚点不走通用查找。

**练习 3**:git 侧只告诉你「base 文本第 19..24 字节处有个改动」,你该用哪个 API 拿到它在 buffer 里的位置?

**答案**:`hunks_intersecting_base_text_range(19..24, &buffer)`。它按 base 字节剪枝定位 hunk,但返回的 `DiffHunk.range`/`buffer_range` 已解析到 buffer 坐标。这正是 `crates/editor/src/git.rs` 中 `hunks_intersecting_base_text_range` 的用法。

### 4.2 hunks_intersecting_range_impl:filter + summaries_for_anchors_with_payload

#### 4.2.1 概念说明

这是所有正向查询的共用引擎,做三件事:

1. 用 filter 谓词在 SumTree 上剪枝,拿到候选 `InternalDiffHunk`;
2. 把每个 hunk 的**两个**锚点(起点、终点)连同 hunk 本身打包成「(锚点, 载荷)」流,交给 buffer 快照**一次性批量解析**成 `Point`;
3. 每次从解析结果里取回相邻两条,还原出一个完整 `DiffHunk`,期间应用行尾扩展、pending 匹配、secondary 匹配。

「批量解析」是性能关键:逐个锚点调用 `offset_for_anchor` 是每次一个二分;而 `summaries_for_anchors_with_payload` 要求输入**按锚点顺序排列**,内部游标只前进不回退,总代价是一次线性扫描。

#### 4.2.2 核心流程

```text
输入: filter(作用于 DiffHunkSummary), buffer, secondary(Option<&Self>)

1. hunks.filter(buffer, filter)            # SumTree 剪枝遍历,得到候选 InternalDiffHunk
2. flat_map: 每个 hunk 拆成两条记录(保持锚点有序)
   (start_anchor, (start_anchor, base_start, hunk))
   (end_anchor,   (end_anchor,   base_end,   hunk))
3. buffer.summaries_for_anchors_with_payload::<Point, _>(记录流)
   → 迭代产出 (Point, 原载荷),锚点已解析到当前 buffer 坐标系
4. 循环: 每次消费两条 → 还原 (start_point, end_point, base 区间)
   4a. start_anchor 无效(is_valid == false)→ 丢弃该 hunk
   4b. end_point 列 > 0 且未到 buffer 末尾 → 行尾扩展到下一行行首
   4c. pending_hunks 游标推进并匹配 → 覆盖 secondary_status 或抑制该 hunk
   4d. secondary 游标推进并匹配 → 填 secondary_status
5. 组装公开 DiffHunk 产出
```

pending/secondary 的匹配细节属于单元四(乐观更新与三方模型),本讲只需知道「游标在这里被推进、状态在这里被合成」。

#### 4.2.3 源码精读

第一步,把 hunk 拆成「锚点 + 载荷」对:

[crates/buffer_diff/src/buffer_diff.rs:L1008-L1026](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1008-L1026)

```rust
let anchor_iter = self
    .hunks
    .filter::<_, DiffHunkSummary>(buffer, filter)
    .flat_map(move |hunk| {
    [
        (
            hunk.buffer_range.start,
            (
                hunk.buffer_range.start,
                hunk.diff_base_byte_range.start,
                hunk,
            ),
        ),
        (
            hunk.buffer_range.end,
            (hunk.buffer_range.end, hunk.diff_base_byte_range.end, hunk),
        ),
    ]
});
```

要点:

- 载荷(payload)是四元组的后三项:**锚点本身、对应的 base 字节端点、整个 hunk 的克隆**。hunk 必须存两份(起点记录、终点记录各一份),`InternalDiffHunk` 因此派生了 `Clone`。
- 由于树按 buffer 锚点有序,且每个 hunk 的 start ≤ end,整个 `anchor_iter` 全局有序——这是下一一步批量解析的正确性前提。

第二步,两条游标准备 + 批量解析:

[crates/buffer_diff/src/buffer_diff.rs:L1028-L1039](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1028-L1039)

```rust
let mut pending_hunks_cursor = self.pending_hunks.cursor::<DiffHunkSummary>(buffer);
pending_hunks_cursor.next();

let mut secondary_cursor = None;
if let Some(secondary) = secondary.as_ref() {
    let mut cursor = secondary.hunks.cursor::<DiffHunkSummary>(buffer);
    cursor.next();
    secondary_cursor = Some(cursor);
}

let max_point = buffer.max_point();
let mut summaries = buffer.summaries_for_anchors_with_payload::<Point, _, _>(anchor_iter);
```

第三步,消费循环的开头——每次取两条还原一个 hunk,并做有效性检查与行尾扩展:

[crates/buffer_diff/src/buffer_diff.rs:L1040-L1056](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1040-L1056)

```rust
iter::from_fn(move || {
    loop {
        let (start_point, (start_anchor, start_base, hunk)) = summaries.next()?;
        let (mut end_point, (mut end_anchor, end_base, _)) = summaries.next()?;

        let base_word_diffs = hunk.base_word_diffs.clone();
        let buffer_word_diffs = hunk.buffer_word_diffs.clone();

        if !start_anchor.is_valid(buffer) {
            continue;
        }

        if end_point.column > 0 && end_point < max_point {
            end_point.row += 1;
            end_point.column = 0;
            end_anchor = buffer.anchor_before(end_point);
        }
```

- `summaries.next()?`:流尽时 `from_fn` 返回 `None`,迭代结束;`continue` 表示丢弃当前 hunk 回到循环取下一对。
- `is_valid` 检查:锚点必须属于当前 buffer(且版本可解析),跨 buffer / 过期的锚点直接丢弃,而不是 panic。

第四步(本讲只看结构),pending 与 secondary 的游标匹配,以及最终组装:

[crates/buffer_diff/src/buffer_diff.rs:L1121-L1128](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1121-L1128)

```rust
return Some(DiffHunk {
    range: start_point..end_point,
    diff_base_byte_range: start_base..end_base,
    buffer_range: start_anchor..end_anchor,
    base_word_diffs,
    buffer_word_diffs,
    secondary_status,
});
```

再对照 text crate 里的批量解析器本体:

[crates/text/src/text.rs:L2442-L2500](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/text/src/text.rs#L2442-L2500)

```rust
pub fn summaries_for_anchors_with_payload<'a, D, A, T>(
    &'a self,
    anchors: A,
) -> impl 'a + Iterator<Item = (D, T)>
where
    D: 'a + TextDimension,
    A: 'a + IntoIterator<Item = (Anchor, T)>,
{
    let anchors = anchors.into_iter();
    let mut fragment_cursor = self
        .fragments
        .cursor::<Dimensions<Option<&Locator>, usize>>(&None);
    let mut text_cursor = self.visible_text.cursor(0);
    let mut position = D::zero(());

    anchors.map(move |(anchor, payload)| {
        if anchor.is_min() {
            return (D::zero(()), payload);
        } else if anchor.is_max() {
            return (D::from_text_summary(&self.visible_text.summary()), payload);
        }
        // ...按 fragment 定位锚点,游标只前进...
        (position, payload)
    })
}
```

- `D = Point`(也可以是 `usize`、`Utf16Offset` 等任何 `TextDimension`)。
- `fragment_cursor` 与 `text_cursor` 都是**单调前进**的游标;因为输入锚点有序,整体是 O(文本长度 + 锚点数) 的一次扫描,而不是每个锚点独立二分。
- min/max 哨兵有 O(1) 快路径;锚点无法定位时 panic(带 buffer id / version 的诊断信息),这就是为什么 impl 里要先 `is_valid` 过滤。
- 无载荷版本 `summaries_for_anchors`(L2432–L2440)是它的 `payload = ()` 特化。

#### 4.2.4 代码实践

**实践目标**:直观感受「一次批量解析、按对消费」的结构,并验证词级差异字段随 hunk 一起产出。

**操作步骤**(示例代码,需你自己在 `mod tests` 中添加):

```rust
#[gpui::test]
async fn test_print_hunk_fields(cx: &mut gpui::TestAppContext) {
    let diff_base = "zero\none\ntwo\nthree\nfour\nfive\n".to_string();
    let buffer_text = "zero\nONE\ntwo\nthree\nFOUR\nfive\n".to_string();

    let buffer = Buffer::new(ReplicaId::LOCAL, BufferId::new(1).unwrap(), buffer_text);
    let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base, cx);

    for hunk in diff.hunks(&buffer) {
        println!(
            "range = {:?}..{:?}, base = {}..{}, word_diffs = {}",
            hunk.range.start,
            hunk.range.end,
            hunk.diff_base_byte_range.start,
            hunk.diff_base_byte_range.end,
            hunk.buffer_word_diffs.len(),
        );
    }
}
```

用 `cargo test -p buffer_diff test_print_hunk_fields -- --nocapture` 运行(`--nocapture` 让 println 输出可见)。

**需要观察的现象**:恰好打印两行——`range = (1,0)..(2,0), base = 5..9` 与 `range = (4,0)..(5,0), base = 19..24`;由于两处修改行数相同且 ≤ 5 行,`word_diffs` 计数通常非空(词级差异是否计算还取决于 `word_diff_enabled` 设置,测试上下文里默认开启)。

**预期结果**:输出与上述一致(字节区间为手工推算,待本地验证)。

#### 4.2.5 小练习与答案

**练习 1**:为什么把 hunk 拆成两条「(锚点, 载荷)」记录,而不是对每个 hunk 分别调用两次 `summary_for_anchor`?

**答案**:两个原因。其一,性能:`summaries_for_anchors_with_payload` 要求输入全局有序,内部两个游标(fragment、text)只前进,总代价是一次线性扫描;逐 hunk 调用则每次都要独立定位。其二,正确性边界一致:批量入口对无效锚点的行为(panic 带诊断)与 `is_valid` 预过滤配套,把「快照滞后期间锚点失效」处理成「跳过该 hunk」。

**练习 2**:循环里 `summaries.next()?` 出现在取 start 之后、取 end 之前(各自带 `?`)。如果候选 hunk 数量是奇数条记录,会发生什么?

**答案**:不可能出现奇数——每个 hunk 恰好产出两条记录(start、end),`flat_map` 保证成对;`from_fn` 的循环体也是严格取两条。第二个 `?` 实际上是防御式写法:流尽即返回 `None` 结束迭代。这提示我们:该迭代器的「消费协议」是「一次一对」,与 `assert_hunks` 等测试工具逐 hunk 消费的方式对应。

**练习 3**:`hunks_intersecting_base_text_range` 与 `hunks_intersecting_range` 的 filter 不同,却汇入同一个 `hunks_intersecting_range_impl`。批量解析阶段用的分别是哪套坐标?

**答案**:filter 阶段前者用 base 字节、后者用 buffer 字节(转 offset 后比较);但进入 impl 之后统一都用 **buffer 锚点**做批量解析——`DiffHunkSummary` 里两种区间都在,剪枝用哪种由调用方决定,产出永远解析到 `main_buffer` 坐标系。

### 4.3 行尾扩展与 raw / rev 两个变体

#### 4.3.1 概念说明

**行尾扩展规则**:hunk 的结束锚点解析后若落在某行**中间**(column > 0),且还没到 buffer 末端,就把结束点推进到**下一行行首**。它保证公开的 `DiffHunk.range` 尽可能是「整行对齐」的,让按行工作的消费者(gutter 渲染、按行 staging)拿到规整的区间。

什么时候结束点会落在行中间?diff 计算时锚点本就锚在行首(`process_change` 用 `Point::new(row, 0)`,见 [crates/buffer_diff/src/buffer_diff.rs:L1290-L1292](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1290-L1292)),但查询传入的 `buffer` 可以比快照新——异步窗口内用户又编辑了 buffer。一个典型情形:用户**删掉了 hunk 结束边界处的换行符**(把两行接成一行),原本位于下一行行首的左偏锚点就会解析到合并后那一行的中间。行尾扩展把这种「漂移」重新规范化成整行语义。

**raw 变体**:`raw_hunks_intersecting_range` 跳过 pending 与 secondary 的全部逻辑,直接从树上取「原始」hunk。staging 这类要**写索引**的路径必须用原始视图——乐观 UI 状态不该影响「实际有哪些差异」的判断。

**rev 变体**:反向迭代,从最后一个命中 hunk 往回走;只要「最后一个」时避免遍历前面全部。

#### 4.3.2 核心流程

行尾扩展的判定(已在 4.2.3 引用,这里单看条件):

```text
若 end_point.column > 0 且 end_point < max_point:
    end_point ← 下一行行首 (row+1, 0)
    end_anchor ← 在新位置重新锚定(anchor_before)
否则:
    保持原样
```

注意 `end_point < max_point` 这个守卫:buffer 最后一行没有换行符时,hunk 结束点就是 `max_point`,此时**不**扩展——没有「下一行」可推。

同一套规范化也施加给 pending hunk 与 secondary hunk 的范围(各自把 `end.column > 0` 推到下一行行首),保证后续 `pending_range == (start_point..end_point)` 的比较是「行对齐对行对齐」的公平比较。

#### 4.3.3 源码精读

raw 版本——注意它直接 `self.hunks.filter(...).map(...)`,完全绕开 impl:

[crates/buffer_diff/src/buffer_diff.rs:L340-L366](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L340-L366)

```rust
/// Like [`hunks_intersecting_range`], but ignores optimistic pending hunks
/// (both secondary-status overrides and suppressions) and does not compute a
/// secondary status.
pub fn raw_hunks_intersecting_range<'a>(
    &'a self,
    range: Range<Anchor>,
    buffer: &'a text::BufferSnapshot,
) -> impl 'a + Iterator<Item = DiffHunk> {
    let range = range.to_offset(buffer);
    let filter = move |summary: &DiffHunkSummary| {
        let summary_range = summary.buffer_range.to_offset(buffer);
        !(summary_range.end < range.start) && !(summary_range.start > range.end)
    };
    self.hunks
        .filter::<_, DiffHunkSummary>(buffer, filter)
        .map(move |hunk| {
            let buffer_range = hunk.buffer_range.clone();
            DiffHunk {
                range: buffer_range.to_point(buffer),
                secondary_status: DiffHunkSecondaryStatus::NoSecondaryHunk,
                ...
            }
        })
}
```

两个细节:

- filter 与非 raw 版**逐字符相同**(同样的闭区间语义),文档注释也特意强调边界行为一致;
- 它**不做行尾扩展**、不做 `is_valid` 过滤、`secondary_status` 恒为 `NoSecondaryHunk`——一个「直读树」的薄封装。

它的下游用户是 staging 路径:[crates/project/src/git_store.rs:L1338-L1342](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/project/src/git_store.rs#L1338-L1342) 在计算 index 写入足迹时用它收集 unstaged hunk——乐观 UI 状态(用户刚点的 stage 按钮)绝不能混进「磁盘上真实差异」的清单。

rev 版本——`FilterCursor` 一路 `prev()`:

[crates/buffer_diff/src/buffer_diff.rs:L1133-L1156](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1133-L1156)

```rust
fn hunks_intersecting_range_rev_impl<'a>(
    &'a self,
    filter: impl 'a + Fn(&DiffHunkSummary) -> bool,
    buffer: &'a text::BufferSnapshot,
) -> impl 'a + Iterator<Item = DiffHunk> {
    let mut cursor = self.hunks.filter::<_, DiffHunkSummary>(buffer, filter);

    iter::from_fn(move || {
        cursor.prev();

        let hunk = cursor.item()?;
        let range = hunk.buffer_range.to_point(buffer);

        Some(DiffHunk {
            range,
            // The secondary status is not used by callers of this method.
            secondary_status: DiffHunkSecondaryStatus::NoSecondaryHunk,
            ...
        })
    })
}
```

- 每次迭代先 `cursor.prev()` 再取 `item()`,产出顺序是从后往前;
- 它**不做行尾扩展、不算 secondary_status**(注释明说调用方不用),比正向 impl 轻量得多——这正是 `range_to_hunk_range` 只用它取「最后一个 hunk 的端点」的原因。

#### 4.3.4 代码实践

**实践目标**:通过源码阅读,理解 raw 与非 raw 视图的分界;通过一个可运行的小实验,观察行尾扩展的守卫条件。

**操作步骤**:

1. 阅读上面 raw 版本的调用点 [crates/project/src/git_store.rs:L1338-L1342](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/project/src/git_store.rs#L1338-L1342),回答:为什么这里不能用 `hunks_intersecting_range`?
2. 小实验(示例代码,需你自己在 `mod tests` 中添加):

```rust
#[gpui::test]
async fn test_no_extension_at_max_point(cx: &mut gpui::TestAppContext) {
    // base 的最后一行有换行符,buffer 的没有:产生一个修改 hunk,
    // 其 buffer 侧结束点是 (2, 3) —— 最后一行行尾,即 max_point
    let diff_base = "a\nb\nc\n".to_string();
    let buffer_text = "a\nb\nCCC".to_string();

    let buffer = Buffer::new(ReplicaId::LOCAL, BufferId::new(1).unwrap(), buffer_text);
    let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base, cx);

    let hunk = diff.hunks(&buffer).next().unwrap();
    println!("end = {:?}", hunk.range.end);
    assert_eq!(hunk.range.end, Point::new(2, 3)); // max_point,不扩展
}
```

**需要观察的现象**:hunk 结束点是 `(2, 3)`(第三行行尾),没有被推进到 `(3, 0)`——`end_point < max_point` 守卫生效:buffer 之外没有「下一行」。

**预期结果**:断言通过(待本地验证)。可再把 buffer 文本改成 `"a\nb\nCCC\n"` 对比:此时结束点解析为 `(3, 0)`(column 为 0),同样不触发扩展,但原因不同——它本来就行对齐。

#### 4.3.5 小练习与答案

**练习 1**:如果不做 `is_valid` 过滤,把过期锚点直接送进 `summaries_for_anchors_with_payload`,会发生什么?

**答案**:该函数对定位不到的锚点会 panic(打印 buffer id、version、锚点的 timestamp/offset/bias 等诊断信息)。所以 impl 在消费每对记录前用 `is_valid` 把失效 hunk 静默跳过——查询 API 对「快照略滞后于 buffer」必须是安全的。

**练习 2**:`hunks_intersecting_range_rev_impl` 为什么可以不算 `secondary_status`?

**答案**:它的调用方只有 `range_to_hunk_range`(取最后一个 hunk 的区间端点)和少数只要「最后一个/倒序若干个」hunk 区间的场景,这些场景只读 `buffer_range`/`diff_base_byte_range`。源码注释也写明 "The secondary status is not used by callers of this method"。省掉 pending/secondary 两个游标的推进,反向查询就轻了一个量级。

**练习 3**:行尾扩展为什么同时改 `end_point` 和 `end_anchor`(重新 `anchor_before`),而不是只改其中一个?

**答案**:产出的 `DiffHunk` 同时携带 `range: Range<Point>`(行号世界)和 `buffer_range: Range<Anchor>`(锚点世界),两者必须描述同一位置。若只改 point,锚点仍指向行中,下游拿 `buffer_range` 做文本切片会切到半行;若只改锚点,point 与锚点不一致同样混乱。注意 `anchor_before(end_point)` 用左偏锚在新位置锚定,保证后续在行首边界处的插入不会把这个锚点推进行内。

### 4.4 range_to_hunk_range:正反两次查询的外包络

#### 4.4.1 概念说明

`range_to_hunk_range` 解决的问题是:「我有一个 buffer 范围(可能只覆盖 hunk 的一部分,或跨越多个 hunk),把它**扩展**成恰好包住所有被触碰 hunk 的完整范围」。返回一对区间:buffer 侧锚点区间 + base 侧字节区间。当查询范围不与任何 hunk 相交时,两者都是 `None`。

#### 4.4.2 核心流程

```text
1. first ← hunks_intersecting_range(range) 的第一个 hunk      # 正向,带扩展与 secondary
2. last  ← hunks_intersecting_range_rev(range) 的第一个 hunk  # 反向,轻量
3. buffer 范围 ← first.buffer_range.start .. last.buffer_range.end
4. base 范围   ← first.diff_base_byte_range.start .. last.diff_base_byte_range.end
5. 任一端取不到(None)→ 返回 (None, None)
```

即「正向拿左端点、反向拿右端点」拼外包络。

#### 4.4.3 源码精读

[crates/buffer_diff/src/buffer_diff.rs:L450-L465](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L450-L465)

```rust
pub fn range_to_hunk_range(
    &self,
    range: Range<Anchor>,
    buffer: &text::BufferSnapshot,
) -> (Option<Range<Anchor>>, Option<Range<usize>>) {
    let first_hunk = self.hunks_intersecting_range(range.clone(), buffer).next();
    let last_hunk = self.hunks_intersecting_range_rev(range, buffer).next();
    let range = first_hunk
        .as_ref()
        .zip(last_hunk.as_ref())
        .map(|(first, last)| first.buffer_range.start..last.buffer_range.end);
    let base_text_range = first_hunk
        .zip(last_hunk)
        .map(|(first, last)| first.diff_base_byte_range.start..last.diff_base_byte_range.end);
    (range, base_text_range)
}
```

要点:

- `first`/`last` 由**两次独立查询**得到;两者命中集合其实相同(同一 filter 语义),一个从头取首个、一个从尾取首个。
- `.zip` 保证任一端为 `None` 时两个返回值一起变 `None`,不会出现「有 buffer 范围没 base 范围」的半吊子结果。
- crate 内部的调用点在 `set_snapshot` 里:secondary diff 变化时,用它把「secondary 变更范围」扩展成完整 hunk 范围,再合并进 `DiffChanged` 的 `changed_range`/`extended_range`(见 [crates/buffer_diff/src/buffer_diff.rs:L2097-L2121](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2097-L2121),细节在 u3-l5)。

#### 4.4.4 代码实践

见本讲第 5 节综合实践的第 2 步——那里把「两条查询路径等价」与「范围扩展」放进同一个测试。

#### 4.4.5 小练习与答案

**练习 1**:为什么 `first` 用正向 impl(带 pending/secondary 合成),`last` 却用轻量的反向 impl?两者信息量不对称没关系吗?

**答案**:`range_to_hunk_range` 只消费两个端点(`buffer_range.start`/`.end` 与 base 字节端点),而**端点位置不受 pending/secondary 影响**——那套逻辑只改 `secondary_status` 或整块跳过 hunk(Suppress)。真正要小心的是 Suppress 会让正向查询跳过被抑制的 hunk,导致 first 落在更后面的 hunk 上;这是有意的:被抑制(已 staged)的 hunk 本就不该计入扩展范围。区间端点本身两个 impl 给出的一致。

**练习 2**:如果查询范围正好落在两个 hunk 之间的空隙里(不相交),返回什么?

**答案**:`(None, None)`。正向与反向查询的 `next()` 都是 `None`,`zip` 之后两个 `map` 都产出 `None`。调用方(如 `set_snapshot`)以 `if let Some(...)` 消费,自然跳过。

**练习 3**:假设 hunk A 在行 1..2、hunk B 在行 4..5,传入行 2..4 的锚点范围(两端都贴边),`range_to_hunk_range` 返回的行范围是什么?

**答案**:行 1..5。查询范围左端贴着 A 的 end(闭区间命中 A),右端贴着 B 的 start(命中 B);外包络取 A.start..B.end,即 `first.buffer_range.start..last.buffer_range.end` = 行 1 行首 .. 行 5 行首。这也再次体现了 4.1 讲的「触边即命中」语义。

## 5. 综合实践

**任务**:写一个测试,验证「同一处修改,两条查询路径殊途同归」,再用 `range_to_hunk_range` 把一个跨两个 hunk 的范围扩展成完整 hunk 范围。

场景设定:

```text
diff_base:    zero\none\ntwo\nthree\nfour\nfive\n
buffer:       zero\nONE\ntwo\nthree\nFOUR\nfive\n
```

两个 hunk:

| hunk | buffer 行区间 | base 字节区间 | 删除文本 | 新增文本 |
| --- | --- | --- | --- | --- |
| 1 | 1..2 | 5..9 | `one\n` | `ONE\n` |
| 2 | 4..5 | 19..24 | `four\n` | `FOUR\n` |

base 字节区间手工推算:`zero\n` 5 字节(0..5),`one\n` 4 字节(5..9),`two\n` 4 字节(9..13),`three\n` 6 字节(13..19),`four\n` 5 字节(19..24),`five\n` 5 字节(24..29)。

**操作步骤**(以下为示例代码,需你自己在 `mod tests` 中添加):

```rust
#[gpui::test]
async fn test_two_query_paths_and_hunk_range_expansion(cx: &mut gpui::TestAppContext) {
    let diff_base = "
        zero
        one
        two
        three
        four
        five
    "
    .unindent();

    let buffer_text = "
        zero
        ONE
        two
        three
        FOUR
        five
    "
    .unindent();

    let mut buffer = Buffer::new(ReplicaId::LOCAL, BufferId::new(1).unwrap(), buffer_text);
    let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base.clone(), cx);

    // 第 1 步:路径 A(buffer 锚点)与路径 B(base 字节)查同一处修改(行 4 的 FOUR)
    let by_buffer = diff
        .hunks_intersecting_range(
            buffer.anchor_before(Point::new(4, 0))..buffer.anchor_after(Point::new(4, 0)),
            &buffer,
        )
        .collect::<Vec<_>>();
    let by_base = diff
        .hunks_intersecting_base_text_range(19..24, &buffer)
        .collect::<Vec<_>>();

    // 两条路径产出的 DiffHunk 完全相等(同一棵树、同一套组装逻辑)
    assert_eq!(by_buffer, by_base);
    assert_eq!(by_buffer.len(), 1);
    assert_eq!(by_buffer[0].range, Point::new(4, 0)..Point::new(5, 0));
    assert_eq!(by_buffer[0].diff_base_byte_range, 19..24);

    // 第 2 步:从行中间取点,跨越 hunk 1 与 hunk 2,扩展成完整 hunk 范围
    let (hunk_range, base_text_range) = diff.range_to_hunk_range(
        buffer.anchor_before(Point::new(1, 1))..buffer.anchor_after(Point::new(4, 1)),
        &buffer,
    );

    let hunk_range = hunk_range.unwrap();
    assert_eq!(
        hunk_range.start.to_point(&buffer)..hunk_range.end.to_point(&buffer),
        Point::new(1, 0)..Point::new(5, 0)
    );
    assert_eq!(base_text_range.unwrap(), 5..24);
}
```

**运行方式**:在 zed 仓库根目录执行 `cargo test -p buffer_diff test_two_query_paths_and_hunk_range_expansion`。

**需要观察的现象**:

1. `by_buffer` 与 `by_base` 逐字段相等——包括 `buffer_range` 里的锚点、`secondary_status`(均为 `NoSecondaryHunk`)与词级差异字段。
2. `range_to_hunk_range` 把行 1 列 1 到行 4 列 1 的「斜跨」范围,扩展成行 1 行首到行 5 行首的完整外包络;base 侧同步扩展为 5..24(第一个 hunk 的 base 起点到最后一个 hunk 的 base 终点)。

**预期结果**:全部断言通过。其中字节区间 5..9 / 19..24 / 5..24 为手工推算,锚点行为依据 `hunks_intersecting_range_impl` 的闭区间相交与行首锚点解析规则推得——**待本地验证**。若 `assert_eq!(by_buffer, by_base)` 失败,pretty_assertions 会给出逐字段的红绿 diff,那是排查「两条路径是否真的共享组装逻辑」的最佳入口。

**扩展思考**(选做):把第 1 步的 base 查询区间改成 `21..22`(落在 `four` 单词内部),断言结果不变——这验证了相交判定只需要「有重叠」,不需要精确对齐 hunk 边界。

## 6. 本讲小结

- 正向查询 API 家族(`hunks` / `hunks_intersecting_range` / `hunks_in_row_range` / `hunks_intersecting_base_text_range`)共享 `hunks_intersecting_range_impl`,区别只在 filter 用 buffer 坐标还是 base 字节坐标做剪枝;相交判定一律是**闭区间触边即命中**。
- 核心实现分四步:SumTree `filter` 剪枝 → 每个 hunk 拆成两条「(锚点, 载荷)」记录 → `summaries_for_anchors_with_payload` 一次有序批量解析成 `Point` → 按对消费并组装公开 `DiffHunk`;失效锚点被 `is_valid` 静默跳过。
- **行尾扩展规则**:hunk 结束点解析后落在行中(column > 0)且未到 buffer 末端时,推进到下一行行首,锚点同步重新锚定;`max_point` 处不扩展。同一规范化也施加于 pending/secondary 范围,保证三方比较公平。
- `raw_hunks_intersecting_range` 是绕开 pending/secondary 的「直读树」视图,staging 写索引的路径(project 的 git_store)必须用它;反向家族轻量(不算 secondary、不扩展行尾),专供「只要最后一个 hunk」的场景。
- `range_to_hunk_range` = 正向取 first + 反向取 last 的外包络,同时返回 buffer 锚点区间与 base 字节区间,任一端取不到则双双为 `None`。

## 7. 下一步学习建议

本讲结束後,「hunk 已经在树上、如何高效查出来」这条线就完整了。下一讲 **u2-l4 坐标映射** 将处理另一个方向的换算:不查 hunk,而是把 buffer 坐标直接映射到 base 文本坐标(`patch_for_buffer_range`、`buffer_point_to_base_text_point` 等,位于 [crates/buffer_diff/src/buffer_diff.rs:L751-L800 附近](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L751-L800)),核心工具是 text crate 的 `Patch` 的 invert/compose。

继续阅读源码的建议顺序:

1. 对照本讲 4.2 重读一遍 `hunks_intersecting_range_impl`(L1002–L1131),这次把注意力放在 pending/secondary 游标的推进上——它是单元四两讲的前置。
2. 看 [crates/editor/src/git.rs:L436-L446](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/git.rs#L436-L446) 与 [crates/project/src/git_store.rs:L1338-L1342](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/project/src/git_store.rs#L1338-L1342) 两个真实调用点,体会「选哪个 API」不是口味问题,而是语义(乐观状态要不要参与)问题。
3. 想深挖批量锚点解析,可读 [crates/text/src/text.rs:L2432-L2500](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/text/src/text.rs#L2432-L2500) 中 fragment 游标与 text 游标的协作方式。
