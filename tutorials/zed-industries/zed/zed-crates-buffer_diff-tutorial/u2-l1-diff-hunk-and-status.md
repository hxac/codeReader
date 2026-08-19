# u2-l1 DiffHunk 与 DiffHunkStatus：一块差异的完整描述

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐字段说出 `DiffHunk` 六个字段各自的含义与用途，并能解释 `range`（Point 区间）与 `buffer_range`（Anchor 区间）为什么同时存在。
2. 解释 `DiffHunk::status()` 如何根据「buffer 侧区间是否为空」「base 侧区间是否为空」这两个条件，推导出 Added / Modified / Deleted 三种 `DiffHunkStatusKind`。
3. 理解为什么 `buffer_range` 必须用 `Anchor`（锚点）而不是 `Point`：diff 是异步算出来的，行号会过期，锚点不会。
4. 识别「整文件新增」（`is_created_file()`）的三个判定条件，以及这个形状的 hunk 是从源码的哪个分支产生的。

本讲只讲「一块差异长什么样、怎么自我描述」，不涉及 hunk 如何被算法算出来（单元三）、如何被 SumTree 存储（下一讲 u2-l2）、secondary 状态如何合成（单元四）。

## 2. 前置知识

本讲假设你已读过 u1-l2，知道 `BufferDiff` 是 gpui 实体、`BufferDiffSnapshot` 是不可变快照、diff base 是被比较的基准文本。在此之上，补充三个本讲反复用到的基础概念：

- **Point（点）**：Zed 文本坐标，`Point::new(row, column)` 表示「第 row 行第 column 列」。它是**某一时刻**的坐标——buffer 一旦被编辑，同一个位置的 row/column 就变了。
- **Anchor（锚点）**：`text` crate 提供的「会跟着编辑走」的位置句柄。在某个 buffer 版本上创建锚点后，无论之后插入、删除了多少文本，把锚点放回**最新**的 buffer 快照上解析，它指向的还是当初那个逻辑位置。锚点还有两个特殊哨兵：`min` 锚点（在所有内容之前）和 `max` 锚点（在所有内容之后），可以用 `Anchor::is_min()` / `Anchor::is_max()` 识别。
- **空区间**：Rust 标准库 `Range` 的 `is_empty()` 即 `start >= end`。「区间是否为空」正是本讲判定 hunk 类型的核心信号：buffer 侧区间为空意味着「这块内容在当前 buffer 里没有了」（被删掉），base 侧区间为空意味着「这块内容在基准文本里本来就不存在」（是新加的）。

如果这三个概念还模糊，建议先翻一眼 `crates/text/src/anchor.rs` 中锚点的定义再继续。

## 3. 本讲源码地图

本讲全部源码集中在 `crates/buffer_diff/src/buffer_diff.rs`（全文约 4362 行），另外引用 `text` crate 的锚点定义和三个下游使用点：

| 位置 | 作用 |
| --- | --- |
| [buffer_diff.rs:L115-L129](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L115-L129) | `DiffHunk` 结构体定义（本讲主角） |
| [buffer_diff.rs:L131-L139](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L131-L139) | `InternalDiffHunk`：SumTree 里实际存储的内部形态 |
| [buffer_diff.rs:L86-L113](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L86-L113) | `DiffHunkStatus`、`DiffHunkStatusKind`、`DiffHunkSecondaryStatus` 三个状态类型 |
| [buffer_diff.rs:L2290-L2310](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2290-L2310) | `DiffHunk::status()` 与 `DiffHunk::is_created_file()` |
| [buffer_diff.rs:L2312-L2382](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2312-L2382) | `DiffHunkStatus` 的判断辅助方法与测试用构造器 |
| [buffer_diff.rs:L1002-L1131](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1002-L1131) | `hunks_intersecting_range_impl`：查询时把内部 hunk 变成公开 `DiffHunk` 的地方 |
| [buffer_diff.rs:L1191-L1239](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1191-L1239) | `compute_hunks` 中两个特殊分支（空 buffer、无 base 文本） |
| [buffer_diff.rs:L1280-L1292](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1280-L1292) | `HunkSink::process_change`：锚点区间与字节区间的诞生地 |
| [buffer_diff.rs:L2385-L2423](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2385-L2423) | `assert_hunks`：四元组断言工具（本讲实践的主角） |
| [text/anchor.rs:L170-L180](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/text/src/anchor.rs#L170-L180) | `Anchor::is_min()` / `is_max()` 的定义 |

## 4. 核心概念与源码讲解

### 4.1 DiffHunk：面向调用方的差异描述

#### 4.1.1 概念说明

`DiffHunk` 是 buffer_diff 对外暴露的「一块差异」的完整描述。编辑器 gutter 里的一红一绿、git 面板里的一个可 stage 块、agent 拿到的一段改动，底层都是一个 `DiffHunk`。

注意 crate 内部其实存了两个形态：

- `InternalDiffHunk`：真正存进 SumTree 的内部形态，**不含** `range`（Point 行区间）和 `secondary_status`；
- `DiffHunk`：查询 API（如 `hunks_intersecting_range`）返回的公开形态，在内部形态基础上**临时补充**了 `range` 和 `secondary_status`。

为什么要拆两个？源码注释直接给了答案：「内部存 `InternalDiffHunk` 是为了避免重复存储行区间」——Point 行区间可以在查询时用当时的 buffer 快照现算，存下来反而会过期；`secondary_status` 则依赖查询时的 secondary diff 与 pending 状态，同样是查询时才能合成的信息。

#### 4.1.2 核心流程

一个 `DiffHunk` 的生命周期：

1. **诞生**：diff 算法对「base 文本 vs buffer 文本」逐行比较，每发现一块差异就调用一次 `HunkSink::process_change(before, after)`，其中 `before` 是 base 侧的行号区间、`after` 是 buffer 侧的行号区间。
2. **落库**：`process_change` 把行号换算成 `buffer_range`（Anchor 区间）与 `diff_base_byte_range`（base 字节区间），包成 `InternalDiffHunk` 压入 SumTree。
3. **查询成型**：调用方调 `snapshot.hunks_intersecting_range(...)` 时，`hunks_intersecting_range_impl` 从 SumTree 取出内部 hunk，把锚点解析成 Point 得到 `range`，合成 `secondary_status`，组装成 `DiffHunk` 返回。

字段一览：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `range` | `Range<Point>` | hunk 覆盖的 buffer 行区间（查询时现算，如 `1..2` 表示第 1 行） |
| `buffer_range` | `Range<Anchor>` | 同一位置的**锚点**表示，随编辑自动移动，是 hunk 的稳定身份 |
| `diff_base_byte_range` | `Range<usize>` | hunk 对应的 base 文本**字节**区间（注意：不是行号） |
| `secondary_status` | `DiffHunkSecondaryStatus` | 「这块改动 staged 了没有」，无 secondary 时为 `NoSecondaryHunk` |
| `buffer_word_diffs` | `Vec<Range<Anchor>>` | hunk 内词级差异在 buffer 侧的位置（锚点） |
| `base_word_diffs` | `Vec<Range<usize>>` | 词级差异在 base 侧的位置（相对删除文本起点的偏移） |

#### 4.1.3 源码精读

先是公开结构体定义：

[buffer_diff.rs:L115-L129](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L115-L129) 定义 `DiffHunk`：`range` 是「解析为行的 buffer 区间」，`buffer_range` 是锚点区间，`diff_base_byte_range` 是 base 文本里的字节区间；两个 word diff 字段一锚点一偏移，注释说明 `base_word_diffs` 是「相对删除文本起点的偏移」。

```rust
pub struct DiffHunk {
    /// The buffer range as points.
    pub range: Range<Point>,
    /// The range in the buffer to which this hunk corresponds.
    pub buffer_range: Range<Anchor>,
    /// The range in the buffer's diff base text to which this hunk corresponds.
    pub diff_base_byte_range: Range<usize>,
    pub secondary_status: DiffHunkSecondaryStatus,
    pub buffer_word_diffs: Vec<Range<Anchor>>,
    pub base_word_diffs: Vec<Range<usize>>,
}
```

与之对应的内部形态：

[buffer_diff.rs:L131-L139](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L131-L139) 定义 `InternalDiffHunk`，比公开形态多了 `diff_base_point_range`（base 侧行区间，供统计 removed_rows 用），少了 `range` 与 `secondary_status`。

两个区间的「诞生地」在 HunkSink：

[buffer_diff.rs:L1286-L1292](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1286-L1292) 中，`old_line_offsets` 是 base 文本每行的起始字节偏移表，用行号查表得到 `diff_base_byte_range`；buffer 侧则把行号转成 `Point::new(row, 0)` 后调 `anchor_before` 创建锚点区间。

```rust
let diff_base_byte_range = self.old_line_offsets[old_start]..self.old_line_offsets[old_end];

let buffer_row_range = (new_start as u32)..(new_end as u32);

let start = Point::new(buffer_row_range.start, 0);
let end = Point::new(buffer_row_range.end, 0);
let buffer_range = self.buffer.anchor_before(start)..self.buffer.anchor_before(end);
```

查询侧的组装点：

[buffer_diff.rs:L1121-L1128](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1121-L1128) 在迭代器返回前把内部 hunk 组装成 `DiffHunk`：`range` 用刚解析出的 `start_point..end_point`，`buffer_range` 用锚点，`secondary_status` 是前面几步刚合成的默认值。

[buffer_diff.rs:L1038-L1056](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1038-L1056) 用 `buffer.summaries_for_anchors_with_payload` 批量把锚点解析成 Point；若 hunk 结束点落在行中间（`end_point.column > 0`）且未到文件末尾，就把结束点扩展到下一行行首——这是查询 API 的行尾扩展规则，本讲只需知道它只影响返回的 `range` / 结束锚点，详细讨论留给 u2-l3。

#### 4.1.4 代码实践：把一个 hunk 的字段全部打印出来

1. **实践目标**：对一个 modified hunk，亲眼看到六个字段的取值，建立「字段 ↔ 文本」的直觉。
2. **操作步骤**：在你本地克隆的 zed 仓库中，打开 `crates/buffer_diff/src/buffer_diff.rs`，在文件底部 `mod tests` 里（比如 `test_buffer_diff_simple` 之后）新增下面的测试（**示例代码**，非仓库原有）：

   ```rust
   #[gpui::test]
   async fn test_inspect_diff_hunk_fields(cx: &mut gpui::TestAppContext) {
       let diff_base = "one\ntwo\nthree\n".to_string();
       let buffer_text = "one\nHELLO\nthree\n".to_string();
       let buffer = Buffer::new(ReplicaId::LOCAL, BufferId::new(1).unwrap(), buffer_text);
       let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base.clone(), cx);

       for hunk in
           diff.hunks_intersecting_range(Anchor::min_max_range_for_buffer(buffer.remote_id()), &buffer)
       {
           println!("range (Point)        : {:?}", hunk.range);
           println!("buffer_range (Anchor): {:?}", hunk.buffer_range);
           println!("base byte range      : {:?}", hunk.diff_base_byte_range);
           println!("deleted text         : {}", &diff_base[hunk.diff_base_byte_range.clone()]);
           println!("added text           : {}",
               buffer.text_for_range(hunk.range.clone()).collect::<String>());
           println!("status               : {:?}", hunk.status());
       }
   }
   ```

   然后在仓库根目录运行：

   ```bash
   cargo test -p buffer_diff test_inspect_diff_hunk_fields -- --nocapture
   ```

3. **需要观察的现象**：`--nocapture` 让 `println!` 的输出可见；你应该看到 `range` 是 `Point { row: 1, column: 0 } .. Point { row: 2, column: 0 }`，`diff_base_byte_range` 是 `4..8`（`"two\n"` 在 base 里的字节位置），删除文本是 `two\n`，新增文本是 `HELLO\n`。
4. **预期结果**：一个 hunk，`status` 为 `Modified`。`diff_base_byte_range` 用字节而非行号，这点初学者最容易忽略——切 base 文本时必须像上面那样做字节切片。
5. 本实践需要在本地克隆仓库中运行，输出细节「待本地验证」（字段语义可与 `assert_hunks` 的实现交叉印证）。

#### 4.1.5 小练习与答案

**练习 1**：`DiffHunk` 和 `InternalDiffHunk` 各多出哪些字段？为什么 `range` 不存进 SumTree？

**参考答案**：`DiffHunk` 多出 `range`（Point 行区间）和 `secondary_status`；`InternalDiffHunk` 多出 `diff_base_point_range`。`range` 是「相对某个 buffer 版本」的坐标，存下来会在 buffer 继续编辑后过期，而锚点可以在查询时对最新快照现算出行区间；`secondary_status` 依赖查询时刻的 secondary diff 与 pending hunk 状态，同样只能查询时合成。

**练习 2**：`base_word_diffs` 用整数偏移、`buffer_word_diffs` 用锚点，为什么不对称？

**参考答案**：diff base 文本是不可变的字符串快照，偏移永远有效；buffer 还会被用户继续编辑，只有锚点能跟着编辑移动，保证词级高亮不漂移。

**练习 3**：给定 base 文本 `"a\nb\nc\n"`（6 字节），删除 `b` 行的 hunk 的 `diff_base_byte_range` 是什么？

**参考答案**：`2..4`。`"a\n"` 占据字节 0–1，`"b\n"` 占据字节 2–3，因此 `old_line_offsets[1]..old_line_offsets[2]` 就是 `2..4`，对 base 做字节切片 `&diff_base[2..4]` 得到 `b\n`。

### 4.2 DiffHunkStatus 与 status() 的三分支判定

#### 4.2.1 概念说明

`DiffHunkStatus` 是「这块差异是什么性质」的答案，由两部分组成：

- `kind: DiffHunkStatusKind`——`Added`（新增）/ `Modified`（修改）/ `Deleted`（删除）三选一，本讲的主角；
- `secondary: DiffHunkSecondaryStatus`——「相对 index 是否已 staged」等五态，本讲只遇到它的默认值 `NoSecondaryHunk`，完整语义在 u4-l2 展开。

`kind` 完全由两个区间的空与非空推导，不需要任何额外信息：

| `buffer_range` 为空？ | `diff_base_byte_range` 为空？ | `kind` | 直觉解释 |
| --- | --- | --- | --- |
| 是 | 否 | `Deleted` | base 里有这段，buffer 里没有了 → 被删除 |
| 否 | 是 | `Added` | base 里没有这段，buffer 里冒出来了 → 新增 |
| 否 | 否 | `Modified` | 两边都有但内容不同 → 修改 |

（两个区间同时为空的 hunk 不存在——那就不是差异了。）

#### 4.2.2 核心流程

判定逻辑写成数学形式就是：

\[
\text{kind}(h) =
\begin{cases}
\text{Deleted}, & h.\text{buffer\_range} \text{ 为空（} \text{start} = \text{end} \text{）} \\
\text{Added}, & \text{否则若 } h.\text{diff\_base\_byte\_range} \text{ 为空} \\
\text{Modified}, & \text{否则}
\end{cases}
\]

注意判定的**顺序**：先查 buffer 侧是否为空，再查 base 侧。这个顺序在实践中不会产生歧义（如上表，两空不可能同时成立），但读源码时要按这个顺序理解。

调用侧通常不直接 match kind，而是用 `DiffHunkStatus` 上的辅助方法：`is_added()` / `is_modified()` / `is_deleted()` / `is_pending()` / `has_secondary_hunk()`。

#### 4.2.3 源码精读

[buffer_diff.rs:L86-L97](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L86-L97) 定义 `DiffHunkStatus { kind, secondary }` 与三变体枚举 `DiffHunkStatusKind { Added, Modified, Deleted }`，两者都是 `Copy`，可以廉价传递。

[buffer_diff.rs:L2297-L2309](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2297-L2309) 就是上表的三分支实现，注意它判断「buffer 侧为空」用的是**锚点相等**（`self.buffer_range.start == self.buffer_range.end`），判断 base 侧为空用 `Range::is_empty`：

```rust
pub fn status(&self) -> DiffHunkStatus {
    let kind = if self.buffer_range.start == self.buffer_range.end {
        DiffHunkStatusKind::Deleted
    } else if self.diff_base_byte_range.is_empty() {
        DiffHunkStatusKind::Added
    } else {
        DiffHunkStatusKind::Modified
    };
    DiffHunkStatus {
        kind,
        secondary: self.secondary_status,
    }
}
```

[buffer_diff.rs:L2330-L2340](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2330-L2340) 提供 `is_deleted` / `is_added` / `is_modified` 三个便捷判断；[buffer_diff.rs:L2363-L2382](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2363-L2382) 提供 `deleted_none()` / `added_none()` / `modified_none()` 三个测试用构造器（`_none` 后缀指 secondary 为默认值 `NoSecondaryHunk`）。

[buffer_diff.rs:L2385-L2423](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2385-L2423) 的 `assert_hunks` 是测试里对 kind 的标准消费方式：四元组 =（行区间，删除文本，新增文本，状态），其中删除文本用 `&diff_base[hunk.diff_base_byte_range]` 切出、新增文本用 `buffer.text_for_range(hunk.range)` 收集——这正是 4.1 中字段语义的直接应用。

仓库里的现成例子可以佐证 Added 与 Modified 的判定：

[buffer_diff.rs:L2460-L2468](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2460-L2468)（`test_buffer_diff_simple`）断言 `two → HELLO` 是一个 modified hunk：

```rust
&[(1..2, "two\n", "HELLO\n", DiffHunkStatus::modified_none())],
```

[buffer_diff.rs:L2470-L2483](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2470-L2483) 在文件开头插入一行后，新 hunk 的删除文本为空字符串、状态为 `added_none()`：

```rust
(0..1, "", "point five\n", DiffHunkStatus::added_none()),
```

值得注意的是：整个测试模块目前**没有**任何用例直接断言 `deleted_none()`（可用 `Grep` 验证：`DiffHunkStatus::deleted` 在本文件测试中零命中）。也就是说「纯删除」场景是现有测试覆盖的空白，这正是我们在综合实践里要补上的。

#### 4.2.4 代码实践：亲手构造一个 Added hunk

1. **实践目标**：验证「base 侧区间为空 → Added」的判定。
2. **操作步骤**：在 `mod tests` 中新增（**示例代码**）：

   ```rust
   #[gpui::test]
   async fn test_added_hunk_status(cx: &mut gpui::TestAppContext) {
       let diff_base = "one\nthree\n".to_string();
       let buffer_text = "one\ntwo\nthree\n".to_string();
       let buffer = Buffer::new(ReplicaId::LOCAL, BufferId::new(2).unwrap(), buffer_text);
       let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base.clone(), cx);
       assert_hunks(
           diff.hunks_intersecting_range(
               Anchor::min_max_range_for_buffer(buffer.remote_id()),
               &buffer,
           ),
           &buffer,
           &diff_base,
           &[(1..2, "", "two\n", DiffHunkStatus::added_none())],
       );
   }
   ```

   运行 `cargo test -p buffer_diff test_added_hunk_status`。通过后，把期望状态故意改成 `modified_none()` 再运行一次。
3. **需要观察的现象**：第一次通过；第二次失败，`pretty_assertions` 会用红绿对照给出 `(1..2, "", "two\n", Modified…)` 与期望值 `Modified→Added` 的差异（`Debug` 输出里能看到 kind 字段不同）。
4. **预期结果**：base 里不存在 `two` 行，hunk 的 `diff_base_byte_range` 为空区间，`status()` 走第二分支返回 `Added`。
5. 删除与修改场景在本文第 5 节的综合实践中完成。

#### 4.2.5 小练习与答案

**练习 1**：把一行 `two` 改成 `TWO`，为什么 kind 是 `Modified` 而不是「一个 Deleted 加一个 Added」？

**参考答案**：diff 算法在逐行比较时，同一位置的「删旧加新」会合并成**一个** change，其 `before`（base 行区间）与 `after`（buffer 行区间）都非空，所以落入第三分支 `Modified`。（相邻的独立删除与新增何时合并为一个 hunk，是 u5-l1 的测试主题。）

**练习 2**：`DiffHunkStatus::modified_none()` 的 `_none` 指什么？

**参考答案**：指 `secondary` 字段取默认值 `DiffHunkSecondaryStatus::NoSecondaryHunk`，即「没有挂 secondary diff、也不处于任何 pending 状态」。它和 `modified(DiffHunkSecondaryStatus::…)` 构造器的区别只在 secondary 分量。

**练习 3**：不看源码回答：`status()` 先判 Deleted 还是先判 Added？依据是哪个字段？

**参考答案**：先判 Deleted，依据是 `buffer_range.start == buffer_range.end`（buffer 侧锚点区间为空）；不成立时再看 `diff_base_byte_range.is_empty()` 决定 Added，否则 Modified。

### 4.3 为什么 buffer_range 用 Anchor 而不是 Point

#### 4.3.1 概念说明

这是本讲最重要的设计问题。设想只用 Point：diff 在后台线程算完时，buffer 可能又被用户敲了几个键——算出来的 `range = 3..5` 现在可能对应的是别的行。如果 hunk 带着「过期行号」存活，每一次编辑都要人为重算所有 hunk 的行号。

锚点解决的就是这个问题：**锚点在编辑后自动重新定位**。hunk 的身份（`buffer_range` 的两个锚点）在创建时定下来，之后无论 buffer 怎么变：

- 查询时把锚点放到**最新**快照上解析，得到当下正确的 `range`；
- 锚点之间的相等/大小关系不因编辑而破坏——特别地，「空锚点区间」永远是空区间。

第二点正是 `status()` 敢用 `buffer_range.start == buffer_range.end` 判 Deleted 的原因：删除型 hunk 创建时两个锚点就是同一个值，后续编辑中它们一起移动、始终相等，hunk 的 Deleted 身份不会因为 buffer 变化而丢失。

#### 4.3.2 核心流程

时序上可以这样理解：

```text
T0  用户停止输入 → 触发 update_diff（后台线程）
T1  compute_hunks 在 buffer 的版本 V1 上为每个 hunk 创建锚点 → 存入 SumTree
T2  用户又输入了几个字符 → buffer 版本变为 V2
T3  UI 调 snapshot.hunks_intersecting_range(.., &buffer@V2)
      ├─ 锚点在 V2 上解析 → 得到新鲜行号 range
      ├─ 锚点相等性不变 → Deleted/Added/Modified 判定依然正确
      └─ 合成 secondary_status → 返回 DiffHunk
```

也就是说：**稳定性交给锚点，新鲜度交给查询时的解析**。`DiffHunk.range` 只是「本次查询时刻」的投影。

#### 4.3.3 源码精读

[buffer_diff.rs:L1292](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1292) hunk 创建时用 `anchor_before` 把行位置固化成锚点（见 4.1.3 引用的代码）；删除型 hunk 的 `new_start == new_end`，两个锚点是**同一个值**。

[buffer_diff.rs:L1039](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1039) 查询时通过 `buffer.summaries_for_anchors_with_payload::<Point, _, _>` 把所有 hunk 的起止锚点**一次性**解析成 Point——注意这里传的 `buffer` 是调用方给的最新快照，而不是快照里存的旧 `buffer_snapshot`，这正是「新鲜度」的来源。

[text/anchor.rs:L170-L180](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/text/src/anchor.rs#L170-L180) 定义了 `is_min` / `is_max`：只有时间戳、偏移、bias 都取到极端值的特殊哨兵锚点才满足——这为下一节 `is_created_file` 的判定提供了「整个 buffer」的精确表达：

```rust
pub fn is_min(&self) -> bool {
    self.timestamp() == clock::Lamport::MIN
        && self.offset == u32::MIN
        && self.bias == Bias::Left
}

pub fn is_max(&self) -> bool {
    self.timestamp() == clock::Lamport::MAX
        && self.offset == u32::MAX
        && self.bias == Bias::Right
}
```

顺带一提，SumTree 的定位（下一讲 u2-l2 的 `SeekTarget` 实现）同样建立在锚点比较上，而不是行号比较——整个存储层都贯彻了「锚点为纲」的原则。

#### 4.3.4 代码实践：观察锚点跟随编辑移动

1. **实践目标**：亲眼验证「hunk 存储的锚点在 buffer 编辑后仍然指向同一处逻辑位置，而行号是查询时现算的」。
2. **操作步骤**：在 `mod tests` 中新增（**示例代码**）：

   ```rust
   #[gpui::test]
   async fn test_hunk_anchor_survives_edit(cx: &mut gpui::TestAppContext) {
       let diff_base = "one\ntwo\nthree\n".to_string();
       let mut buffer = Buffer::new(
           ReplicaId::LOCAL,
           BufferId::new(3).unwrap(),
           "one\nHELLO\nthree\n".to_string(),
       );
       // 注意：这个 diff 是在编辑之前计算的，之后不会重算
       let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base, cx);

       let hunk_before = diff
           .hunks_intersecting_range(
               Anchor::min_max_range_for_buffer(buffer.remote_id()),
               &buffer,
           )
           .next()
           .unwrap();
       assert_eq!(hunk_before.range, Point::new(1, 0)..Point::new(2, 0));

       // 在 hunk 前面插入一行：所有行号下移一格
       buffer.edit([(0..0, "zero\n")]);

       // 用同一个旧 diff 快照、但传入编辑后的 buffer 再查一次
       let hunk_after = diff
           .hunks_intersecting_range(
               Anchor::min_max_range_for_buffer(buffer.remote_id()),
               &buffer,
           )
           .next()
           .unwrap();
       assert_eq!(hunk_after.range, Point::new(2, 0)..Point::new(3, 0));
       assert_eq!(hunk_after.status(), hunk_before.status());
   }
   ```

   运行 `cargo test -p buffer_diff test_hunk_anchor_survives_edit`。
3. **需要观察的现象**：同一个 hunk 的行号从 `1..2` 变成了 `2..3`（锚点跟随插入移动），而 `status()` 保持 `Modified` 不变（身份不丢）。
4. **预期结果**：测试通过。注意这是刻意为之的「陈旧快照」演示——真实运行时 buffer 编辑后会触发 diff 重算，但本测试说明即使不重算，旧 hunk 的锚点也不会指错地方。
5. 「待本地验证」：本测试为示例代码，运行前请确认你的工作副本允许修改 `crates/buffer_diff`（这是读者本地练习，不影响仓库）。

#### 4.3.5 小练习与答案

**练习 1**：如果在 hunk 覆盖的位置**中间**插入文本，删除型 hunk 的 `buffer_range.start == buffer_range.end` 还成立吗？

**参考答案**：成立。两个锚点是同一个值（或同位置同 bias 的等值锚点），任何编辑对它们施加同样的变换，相等性保持——这正是把 Deleted 判定放在锚点而非 Point 上的收益。

**练习 2**：`hunks_intersecting_range` 的第二个参数 `buffer: &'a text::BufferSnapshot` 为什么由调用方传入，而不是用快照自己存的 `buffer_snapshot`？

**参考答案**：因为调用方能拿到更新的 buffer 快照；锚点在新快照上解析才能得到当下正确的行号。快照自己存的 `buffer_snapshot` 是 diff 计算时刻的旧版本，只用于版本比较等内部逻辑。

**练习 3**：`DiffHunk.range` 与 `DiffHunk.buffer_range` 分别适合什么消费场景？

**参考答案**：`range` 适合「立刻用于渲染/断言」的场景（行号直观、可直接切文本），但它只对查询时的那个版本有效；`buffer_range` 适合「需要跨编辑存续」的场景（如在 secondary diff、pending hunk 里对齐 hunk，或后续再次定位）。

### 4.4 is_created_file：整文件新增的识别

#### 4.4.1 概念说明

「新建文件」是 diff 的一种极端形态：文件在版本控制里根本不存在（untracked），所以**整个 buffer 就是一个 hunk**——base 侧没有任何内容可对应。git 面板需要识别这种情况：新建文件的「Restore」没有意义（无内容可恢复）、编辑器对新建文件的 hunk 有专门的折叠展开行为。

`is_created_file()` 用三个条件精确刻画这个形态：

1. `diff_base_byte_range == (0..0)`——base 侧为空；
2. `buffer_range.start.is_min()`——从 buffer 最开头开始；
3. `buffer_range.end.is_max()`——到 buffer 最末尾结束。

三个条件缺一不可：只判「base 为空」会把任何纯新增行的 hunk 也算进来；加上 min/max 哨兵才保证「覆盖整个 buffer」。

#### 4.4.2 核心流程

这个形状的 hunk 从哪来？回看 `compute_hunks` 的两个特殊分支：

- **有 base 文本**：走 imara-diff 正常计算（单元三的主题）；
- **没有 base 文本**（`diff_base` 为 `None`，即 `base_text_exists == false`，典型就是 untracked 文件）：**不跑算法**，直接手工构造一个覆盖整个 buffer 的 `InternalDiffHunk`，锚点取 min/max 哨兵、字节区间取 `0..0`。

下游通过 `!hunk.is_created_file()` 把新建文件从常规 hunk 里排除，或通过 `hunk.is_created_file()` 走专门的 UI 分支。

#### 4.4.3 源码精读

[buffer_diff.rs:L2291-L2295](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff.rs#L2291-L2295) 就是这三个条件的实现：

```rust
pub fn is_created_file(&self) -> bool {
    self.diff_base_byte_range == (0..0)
        && self.buffer_range.start.is_min()
        && self.buffer_range.end.is_max()
}
```

[buffer_diff.rs:L1228-L1239](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1228-L1239) 是这个 hunk 的出生地——`compute_hunks` 的 `else` 分支（`diff_base` 为 `None` 时）：

```rust
} else {
    tree.push(
        InternalDiffHunk {
            buffer_range: Anchor::min_max_range_for_buffer(buffer.remote_id()),
            diff_base_byte_range: 0..0,
            ...
        },
        buffer,
    );
}
```

[buffer_diff.rs:L1970-L1981](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1970-L1981)（`update_diff`）展示 `None` 从哪来：调用方传入的 `base_text: Option<Arc<str>>` 为 `None` 时，`compute_hunks(None, ...)` 被调用；`base_text_exists` 也随之记为 `false`。

顺带认识相邻的另一个特例：[buffer_diff.rs:L1194-L1210](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1194-L1210) 处理「Zed 用 `"\n"` 表示空 buffer」的情况——若对非空 base 做朴素 diff，会在中间留下一个多余的「保留行」，所以这里手工构造一个覆盖整个 base 的**删除型** hunk（`buffer_range` 是两个 `anchor_before(0)`，即空区间 → status 为 `Deleted`）。它和 created-file 分支一起说明：`compute_hunks` 的输出并非总是算法算的，边界情况是手工特化的。

下游消费点（可以点开链接看上下文）：

- [git_ui/unstaged_diff.rs:L342](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/git_ui/src/unstaged_diff.rs#L342) 用 `snapshot.diff_hunks().any(|h| !h.is_created_file())` 计算 `restore_all` 是否可用——全是新建文件时没有可恢复的内容；
- [multi_buffer.rs:L2910](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/multi_buffer/src/multi_buffer.rs#L2910) 在 `hunk.is_created_file() && !all_diff_hunks_expanded` 时走特殊分支（新建文件的 hunk 默认展示行为与普通 hunk 不同）；
- [editor/git.rs:L3011](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/git.rs#L3011) 编辑器侧也先取出 `is_created_file` 再决定渲染与交互。

#### 4.4.4 代码实践：构造一个 created-file hunk

1. **实践目标**：验证「无 base 文本时，快照里恰有一个 hunk 且 `is_created_file()` 为真」。
2. **操作步骤**：`new_sync` 总是提供 base 文本，所以这里要走实体 API（**示例代码**）：

   ```rust
   #[gpui::test]
   async fn test_created_file_hunk(cx: &mut gpui::TestAppContext) {
       let buffer = Buffer::new(
           ReplicaId::LOCAL,
           BufferId::new(4).unwrap(),
           "one\ntwo\nthree\n".to_string(),
       );

       // 不给 base 文本：模拟尚未纳入版本控制的新文件
       let diff_entity = cx.update(|cx| {
           cx.new(|cx| BufferDiff::new(&buffer, None, None, DiffBaseKind::Head, cx))
       });
       let snapshot = cx.update(|cx| {
           diff_entity.update(cx, |diff, cx| {
               // base_text_exists() 为 false → update_diff 收到 None → 走 created-file 分支
               diff.recalculate_diff_sync(&buffer, cx);
           });
           diff_entity.read(cx).snapshot(cx)
       });

       let hunks: Vec<_> = snapshot
           .hunks_intersecting_range(
               Anchor::min_max_range_for_buffer(buffer.remote_id()),
               &buffer,
           )
           .collect();
       assert_eq!(hunks.len(), 1);
       assert!(hunks[0].is_created_file());
       assert!(hunks[0].status().is_added());
       assert_eq!(hunks[0].diff_base_byte_range, 0..0);
   }
   ```

   运行 `cargo test -p buffer_diff test_created_file_hunk`。
3. **需要观察的现象**：恰有一个 hunk；`is_created_file()` 为真；status 的 kind 为 `Added`（buffer 侧非空、base 侧为空）；`diff_base_byte_range` 为 `0..0`。
4. **预期结果**：测试通过。链路依据：[buffer_diff.rs:L2271-L2283](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2271-L2283) 的 `recalculate_diff_sync` 用 `self.base_text_exists().then(...)` 决定是否传 base 文本，而 [buffer_diff.rs:L2164-L2175](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2164-L2175) 表明刚构造的 `BufferDiff` 尚无快照、`base_text_exists` 为 false，于是 `update_diff` 传入 `None`，命中 4.4.3 的 else 分支。
5. 此测试为示例代码，「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `is_created_file` 不直接写成 `self.diff_base_byte_range.is_empty() && self.buffer_range.end.is_max()`？

**参考答案**：`is_empty` 只要求 `start >= end`，任何「在文件末尾追加几行」产生的纯新增 hunk 的 base 区间也是空的；不加上 `start.is_min()` 与 `end.is_max()` 的完整覆盖约束，就会把「部分新增」误判成「整文件新增」。同时 `== (0..0)` 比 `is_empty()` 更严格（排除理论上非零的空区间），表达也更精确。

**练习 2**：created-file hunk 的 `status().kind` 是什么？为什么？

**参考答案**：`Added`。它的 `buffer_range` 是 min..max（非空），`diff_base_byte_range` 是 `0..0`（空），按三分支判定命中第二分支。

**练习 3**：`compute_hunks` 里 `buffer_text == "\n"` 的特例（[L1197](https://github.com/zed-industries-zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1197)）产出的 hunk 的 status 是什么？

**参考答案**：`Deleted`。该分支构造的 `buffer_range` 是 `anchor_before(0)..anchor_before(0)`（空区间），而 `diff_base_byte_range` 覆盖整个 base（非空），命中第一分支——「整个文件被删空」。

## 5. 综合实践

把本讲四个模块串成一个测试：**三种 status kind + 空 base 的 created-file 场景**。这也是本讲规格指定的实践任务。在 `crates/buffer_diff/src/buffer_diff.rs` 的 `mod tests` 中新增（**示例代码**）：

```rust
#[gpui::test]
async fn test_diff_hunk_status_kinds(cx: &mut gpui::TestAppContext) {
    // 场景一：纯新增 —— base 里没有这行，buffer 里多出这行
    {
        let diff_base = "one\nthree\n".to_string();
        let buffer_text = "one\ntwo\nthree\n".to_string();
        let buffer = Buffer::new(ReplicaId::LOCAL, BufferId::new(5).unwrap(), buffer_text);
        let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base.clone(), cx);
        assert_hunks(
            diff.hunks_intersecting_range(
                Anchor::min_max_range_for_buffer(buffer.remote_id()),
                &buffer,
            ),
            &buffer,
            &diff_base,
            &[(1..2, "", "two\n", DiffHunkStatus::added_none())],
        );
    }

    // 场景二：纯删除 —— base 里有 two 行，buffer 里删掉了（现有测试覆盖的空白）
    {
        let diff_base = "one\ntwo\nthree\n".to_string();
        let buffer_text = "one\nthree\n".to_string();
        let buffer = Buffer::new(ReplicaId::LOCAL, BufferId::new(6).unwrap(), buffer_text);
        let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base.clone(), cx);
        assert_hunks(
            diff.hunks_intersecting_range(
                Anchor::min_max_range_for_buffer(buffer.remote_id()),
                &buffer,
            ),
            &buffer,
            &diff_base,
            &[(1..1, "two\n", "", DiffHunkStatus::deleted_none())],
        );
    }

    // 场景三：修改 —— 同一位置行内容变了
    {
        let diff_base = "one\ntwo\nthree\n".to_string();
        let buffer_text = "one\nHELLO\nthree\n".to_string();
        let buffer = Buffer::new(ReplicaId::LOCAL, BufferId::new(7).unwrap(), buffer_text);
        let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base.clone(), cx);
        assert_hunks(
            diff.hunks_intersecting_range(
                Anchor::min_max_range_for_buffer(buffer.remote_id()),
                &buffer,
            ),
            &buffer,
            &diff_base,
            &[(1..2, "two\n", "HELLO\n", DiffHunkStatus::modified_none())],
        );
    }

    // 场景四：空 base —— 整个 buffer 是一个 created-file hunk
    {
        let buffer = Buffer::new(
            ReplicaId::LOCAL,
            BufferId::new(8).unwrap(),
            "one\ntwo\nthree\n".to_string(),
        );
        let diff_entity = cx.update(|cx| {
            cx.new(|cx| BufferDiff::new(&buffer, None, None, DiffBaseKind::Head, cx))
        });
        let snapshot = cx.update(|cx| {
            diff_entity.update(cx, |diff, cx| diff.recalculate_diff_sync(&buffer, cx));
            diff_entity.read(cx).snapshot(cx)
        });
        let hunks: Vec<_> = snapshot
            .hunks_intersecting_range(
                Anchor::min_max_range_for_buffer(buffer.remote_id()),
                &buffer,
            )
            .collect();
        assert_eq!(hunks.len(), 1);
        assert!(hunks[0].is_created_file());
    }
}
```

在 zed 仓库根目录运行：

```bash
cargo test -p buffer_diff test_diff_hunk_status_kinds
```

**操作步骤**：

1. 把测试加入 `mod tests`（需要 `use super::*` 已有的环境，模块顶部已具备）。
2. 运行上述命令，确认四个场景全部通过。
3. 观察三个 `assert_hunks` 断言里「行区间 / 删除文本 / 新增文本 / 状态」的对应关系：删除场景的行区间是 `1..1`（空）、删除文本是 `two\n`；新增场景正相反。
4. 故意把场景二的期望改成 `(1..2, "two\n", "", deleted_none())` 再运行一次，观察 `pretty_assertions` 的红绿 diff 如何把不一致暴露出来，然后改回。

**预期结果**：四个场景全部通过；失败注入能直观看到断言差异。本综合实践为示例代码，「待本地验证」。

**思考题（不必写码）**：场景二里删除 hunk 的行区间是 `1..1`，而场景三修改 hunk 是 `1..2`——同样是「第 1 行出了问题」，为什么行区间不同？（答案：删除场景下 buffer 侧区间为空，起点终点都是第 1 行行首；这正是 status 判 Deleted 的信号。）

## 6. 本讲小结

- `DiffHunk` 是对外公开的差异描述，六个字段里 `range`（Point 行区间）与 `secondary_status` 是查询时现算的，内部 SumTree 存的是不含这两项的 `InternalDiffHunk`。
- `status()` 的 kind 由两个区间的空与非空完全决定：buffer 侧空 → `Deleted`，base 侧空 → `Added`，否则 `Modified`；`DiffHunkStatus = kind + secondary`。
- `buffer_range` 用锚点而非 Point，是因为 diff 异步计算、行号会过期：锚点提供跨编辑的稳定身份，行号在查询时对最新快照解析获得新鲜度；「空锚点区间永远为空」保证了 Deleted 判定的持久正确。
- `is_created_file()` 要求 base 区间为 `0..0` **且** buffer 区间从 min 哨兵到 max 哨兵，完整覆盖整个 buffer；这种 hunk 由 `compute_hunks` 的「无 base 文本」分支手工构造，不经过 diff 算法。
- `compute_hunks` 还有「空 buffer 表示为 `"\n"`」的手工特例，产出整文件 Deleted 的 hunk——边界情况在源头特化，是本 crate 的一个惯用手法。
- 测试侧用 `assert_hunks` 四元组断言 hunk；现有测试没有覆盖 `deleted_none()` 场景，本讲的综合实践补上了它。

## 7. 下一步学习建议

本讲搞清楚了「一个 hunk 长什么样」，下一讲 **u2-l2（用 SumTree 存 hunk：Item、Summary 与 SeekTarget）** 解决「一堆 hunk 怎么存、怎么高效定位」：`InternalDiffHunk` 的 `sum_tree::Item` 实现、`DiffHunkSummary` 如何聚合 `added_rows` / `removed_rows`、以及锚点和字节偏移两种 `SeekTarget`。建议先通读 [buffer_diff.rs:L183-L272](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L183-L272)（三组 trait 实现），再回看本讲 4.1.3 中 `hunks_intersecting_range_impl` 对 `filter` 的用法，体会「过滤谓词为什么写在 `DiffHunkSummary` 上而非 hunk 上」。之后按学习路线进入 u2-l3 的查询 API 家族。
