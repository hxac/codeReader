# u2-l2 用 SumTree 存 hunk：Item、Summary 与 SeekTarget

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 buffer_diff 用**哪两棵 SumTree** 存差异、存的元素是什么（`InternalDiffHunk` / `PendingHunk`），以及它们与公开的 `DiffHunk` 的关系。
2. 解释 sum_tree 的「三件套」——`Item`、`Summary`、`SeekTarget`——各自负责什么，`DiffHunkSummary` 是怎么从单个 hunk 算出来、又怎么在树上逐层**合并**的。
3. 手算一个 hunk 的 `added_rows` / `removed_rows`，并说明 `changed_row_counts()` 为什么读一下树根摘要就能得到全 buffer 的新增/删除行数。
4. 会用 `Anchor` 和 `usize` 两种 `SeekTarget` 在 hunk 树上导航，理解查询 API 的 filter 闭包收到的是「子树摘要」带来的剪枝能力。

本讲只讲**存储层**：hunk 放进树里之后如何被聚合、被定位。不讲 hunk 怎么被 diff 算法算出来（单元三），也不讲查询 API 家族的完整行为（下一讲 u2-l3）。

## 2. 前置知识

本讲假设你已读过 u2-l1，知道 `DiffHunk`（公开形态）与 `InternalDiffHunk`（内部形态）的字段差异，以及锚点（`Anchor`）为什么比 `Point` 稳定。在此基础上，补充本讲的主角——SumTree。

- **SumTree 是什么**：sum_tree 是 Zed 自研的 crate，Cargo.toml 里的一句话介绍是「a sum tree data structure, a concurrency-friendly B-tree」（[sum_tree/Cargo.toml:L7](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/Cargo.toml#L7)）。你可以把它理解成一棵**每个节点都缓存了「子树总和」的 B+ 树**：叶子按序存元素，内部节点存每个孩子的摘要（summary）。这与线段树「节点存区间和」是同一个思想，只是它是通用的、摘要类型由使用方定义的。Zed 的 rope（文本缓冲）、编辑器行渲染缓存、本讲的 hunk 列表，底层都是这棵树。
- **Item（元素）**：存进树里的东西。使用方要回答「一个元素贡献了什么摘要」。对应 trait 是 [sum_tree.rs:L34-L38](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L34-L38) 的 `Item`，只有一个方法 `summary()`。
- **Summary（摘要）**：描述「一段子树的总和」的类型。使用方要回答两个问题：「空的摘要长什么样」（`zero`）和「两个摘要怎么合并成一个」（`add_summary`）。对应 [sum_tree.rs:L51-L55](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L51-L55)。
- **SeekTarget（定位目标）**：在树上「跳转」时用来比大小的东西。树本身不知道你想按什么坐标找——buffer_diff 需要既能按**锚点**找 hunk，又能按 **base 字节偏移**找 hunk，所以给这两种类型各自实现了 `SeekTarget`（[sum_tree.rs:L122-L124](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L122-L124)）。
- **Context（上下文）**：`Summary` 有一个关联类型 `Context<'a>`。生成和合并摘要往往需要外部信息——对 buffer_diff 来说是 `&text::BufferSnapshot`（把锚点解析成位置必须有它）。这个设计细节是理解「`DiffHunkSummary` 为什么长这样」的钥匙，4.2 节会展开。

一个直觉先行：**存进树里的是 hunk，树上「刻度」是 hunk 的覆盖范围，「计数器」是新增/删除行数**。查整份 diff 的行数统计 = 读根节点；查某个区间的 hunk = 沿刻度二分下降 + 整子树剪枝。

## 3. 本讲源码地图

本讲源码集中在 `crates/buffer_diff/src/buffer_diff.rs`（约 4362 行），并引用 sum_tree、text 两个 crate 的定义处与三个下游消费点：

| 位置 | 作用 |
| --- | --- |
| [buffer_diff.rs:L47-L55](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L47-L55) | `BufferDiffSnapshot`：持有 `hunks` 与 `pending_hunks` 两棵 SumTree（本讲舞台） |
| [buffer_diff.rs:L131-L147](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L131-L147) | `InternalDiffHunk` 与 `PendingHunk`：两棵树各自存的元素 |
| [buffer_diff.rs:L175-L181](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L175-L181) | `DiffHunkSummary`：本讲主角，hunk 的可聚合摘要 |
| [buffer_diff.rs:L183-L213](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L183-L213) | 两个 `sum_tree::Item` 实现：从元素算摘要 |
| [buffer_diff.rs:L215-L246](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L215-L246) | `sum_tree::Summary` 实现：`zero` 哨兵与 `add_summary` 合并代数 |
| [buffer_diff.rs:L248-L273](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L248-L273) | `Anchor` 与 `usize` 两个 `SeekTarget` 实现 |
| [buffer_diff.rs:L294-L301](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L294-L301) | `changed_row_counts()`：读根摘要拿行数统计 |
| [buffer_diff.rs:L487-L521](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L487-L521) | `hunk_before_base_text_offset` / `hunk_before_buffer_anchor`：两种 SeekTarget 的实际用法 |
| [buffer_diff.rs:L1002-L1039](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1002-L1039) | `hunks_intersecting_range_impl`：filter 游标 + 摘要剪枝的实战现场 |
| [buffer_diff.rs:L1184-L1242](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1184-L1242) | `compute_hunks`：hunk 逐个 `push` 进树的地方 |
| [sum_tree/sum_tree.rs:L34-L55](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L34-L55) | `Item` / `Summary` trait 定义 |
| [sum_tree/sum_tree.rs:L736-L741](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L736-L741) | `SumTree::summary()`：树根摘要 |
| [sum_tree/cursor.rs:L82-L99](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L82-L99) | 游标的 `start()` / `end()` / `item()`：前缀摘要的含义 |
| [text/anchor.rs:L79-L89](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/text/src/anchor.rs#L79-L89) | `min_min_range_for_buffer` 等锚点哨兵区间（`zero` 的原料） |
| [editor/header.rs:L651-L656](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/element/header.rs#L651-L656) | 下游：编辑器头部用 `changed_row_counts()` 显示 +N −M |

## 4. 核心概念与源码讲解

### 4.1 InternalDiffHunk：SumTree 里真正存储的形态

#### 4.1.1 概念说明

u2-l1 讲过，crate 对外暴露的 `DiffHunk` 有 7 个字段，其中 `range`（Point 行区间）和 `secondary_status` 是查询时临时算出来的。真正落库的是精简版 `InternalDiffHunk`——源码注释写明动机：「内部存 `InternalDiffHunk` 是为了避免重复存储行区间」。

`BufferDiffSnapshot` 里**有两棵** `SumTree<InternalDiffHunk>` 血缘的树：

- `hunks: SumTree<InternalDiffHunk>`——diff 算法算出的真实差异，本讲的绝对主角；
- `pending_hunks: SumTree<PendingHunk>`——乐观 UI 状态（stage/unstage 进行中的标记，单元四细讲）。`PendingHunk` 是另一种元素类型，但它的摘要同样是 `DiffHunkSummary`，所以能复用同一套导航逻辑。

#### 4.1.2 核心流程

hunk 进入树的唯一入口是 `compute_hunks`（diff 计算主链路，单元三细讲）：

1. `SumTree::new(buffer)` 建空树——注意把 `&text::BufferSnapshot` 作为上下文交给树，此后每次 `push` 都要用它生成摘要；
2. diff 算法每产出一块差异，`HunkSink` 就攒出一个 `InternalDiffHunk`；
3. 循环 `tree.push(hunk, buffer)` 逐个追加——imara-diff 按行序输出 hunk，追加即有序，SumTree 的 `push` 是追加到尾部的 O(log n) 操作（[sum_tree.rs:L768-L778](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L768-L778)：先为元素生成摘要，再以单元素叶子的形态并进树）。

#### 4.1.3 源码精读

[buffer_diff.rs:L47-L55](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L47-L55) 定义 `BufferDiffSnapshot`：`hunks` 与 `pending_hunks` 两棵树并排，加上 base 文本快照与主 buffer 快照。树的元素类型不同，摘要类型相同。

[buffer_diff.rs:L131-L139](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L131-L139) 定义 `InternalDiffHunk` 的五个字段：锚点区间 `buffer_range`、base 字节区间 `diff_base_byte_range`、**base 行区间 `diff_base_point_range`**（u2-l1 没展开的字段，4.2 节揭晓它存在的理由）、两侧词级差异。

[buffer_diff.rs:L1225-L1227](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1225-L1227) 是 `compute_hunks` 的收尾三行：`for hunk in sink.finish() { tree.push(hunk, buffer); }`——每个 hunk 连同它的摘要在这一刻进入树。

#### 4.1.4 代码实践

**实践目标**：确认「树里的元素数 = 公开 API 返回的 hunk 数」，建立存储层与查询层的对应关系。

**操作步骤**（示例代码，需在你的练习分支上加入 `mod tests` 内，因为 `hunks` 字段与 `DiffHunkSummary` 都是 crate 私有）：

```rust
// 示例代码：加入 crates/buffer_diff/src/buffer_diff.rs 的 mod tests 中
#[gpui::test]
async fn test_sumtree_item_count(cx: &mut gpui::TestAppContext) {
    let diff_base = "one\ntwo\nthree\n".to_string();
    let buffer_text = "one\nTWO\nthree\n".to_string();
    let mut buffer = text::Buffer::new(
        text::ReplicaId::LOCAL,
        text::BufferId::new(1).unwrap(),
        buffer_text,
    );
    let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base.clone(), cx);

    // 路径一：公开查询 API 数 hunk
    let public_count = diff
        .hunks(&buffer)
        .filter(|hunk| hunk.range.start.row == 1)
        .count();

    // 路径二：直接遍历 SumTree 数元素（filter 谓词恒真 = 不剪枝）
    let internal_count = diff
        .hunks
        .filter::<_, DiffHunkSummary>(&buffer, |_| true)
        .count();

    assert_eq!(public_count, internal_count);
    assert_eq!(internal_count, 1);
}
```

**需要观察的现象**：两条路径计数一致；把 buffer_text 改成与 diff_base 完全相同再跑，两个计数都变成 0。

**预期结果**：测试通过。运行方式：仓库根目录 `cargo test -p buffer_diff test_sumtree_item_count`。

#### 4.1.5 小练习与答案

**练习 1**：`InternalDiffHunk` 为什么不存 `range`（Point 行区间）和 `secondary_status`？

答案：`range` 是某一时刻的坐标，buffer 一被编辑就过期，存下来是陈旧数据；查询时用当时的 buffer 快照把锚点解析成 Point 才永远正确。`secondary_status` 依赖查询时刻的 secondary diff 与 pending hunk 状态（见 [buffer_diff.rs:L1058-L1119](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1058-L1119) 的合成逻辑），同样无法预先存储。

**练习 2**：为什么 `hunks` 和 `pending_hunks` 是两棵树而不是一棵？

答案：两者语义与生命周期不同——前者是算法算出的真实差异，后者是「操作反馈先行」的乐观标记。分开存储让真实数据不被 UI 临时状态污染；查询时 `hunks_intersecting_range_impl` 用两个游标分别推进再归并（[buffer_diff.rs:L1008-L1036](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1008-L1036)）。共享 `DiffHunkSummary` 摘要类型则让两棵树复用同一套按锚点导航的能力。

**练习 3**：`compute_hunks` 直接 `push` 而不用排序插入，凭什么保证树有序？

答案：imara-diff 的 `diff.hunks()` 按文本顺序单调输出差异块（[buffer_diff.rs:L1222-L1227](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1222-L1227) 的循环顺序），所以追加即有序。

### 4.2 DiffHunkSummary：一块差异的可聚合「指纹」

#### 4.2.1 概念说明

`DiffHunkSummary` 是树的「刻度 + 计数器」。它有四个字段、三个维度：

| 字段 | 单个 hunk 的含义 | 多个 hunk 合并后的含义 |
| --- | --- | --- |
| `buffer_range: Range<Anchor>` | 这块差异在 buffer 里覆盖的锚点区间 | 所有 hunk 覆盖范围的**并集**（外包络） |
| `diff_base_byte_range: Range<usize>` | base 文本里的字节区间 | base 侧覆盖范围的并集 |
| `added_rows: u32` | hunk 在 buffer 侧**跨越的行数** | 全部 hunk 新增行数之和 |
| `removed_rows: u32` | hunk 在 base 侧跨越的行数 | 全部 hunk 删除行数之和 |

有了它，两类问题变成「读一个数」：全 buffer 改了多少行（树根摘要）；某段子树覆盖到哪里（内部节点摘要，供导航与剪枝）。

#### 4.2.2 核心流程

单个 hunk 的摘要由 `Item::summary` 现场计算：

```
added_rows   = buffer_range.end 的行号 − buffer_range.start 的行号
removed_rows = diff_base_point_range.end.row − diff_base_point_range.start.row
```

注意两点：

- 两个行号差都用了 `saturating_sub`，纯删除 hunk 的 `buffer_range` 为空（start == end），`added_rows` 自然为 0；纯新增 hunk 的 base 区间为空，`removed_rows` 为 0。
- **`removed_rows` 的原料是 `diff_base_point_range`，不是 `diff_base_byte_range`**。字节区间算不出行数——除非有 base 的 rope 去换算。而 `Summary::Context` 只有一个槽位，已经被主 buffer 快照占用（锚点解析要用）。所以 `InternalDiffHunk` 干脆把 base 侧行区间预先存好（HunkSink 在构造 hunk 时用 `diff_base_rope.offset_to_point` 算好，见 [buffer_diff.rs:L1335-L1341](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1335-L1341)）。这是「上下文只有一个」这条约束直接塑造数据结构的例子。

#### 4.2.3 源码精读

[buffer_diff.rs:L175-L181](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L175-L181) 定义 `DiffHunkSummary` 四字段。它只派生 `Debug, Clone`——没有 `PartialEq`，所以测试里只能逐字段比较，不能对整个摘要 `assert_eq!`。

[buffer_diff.rs:L183-L200](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L183-L200) 是 `InternalDiffHunk` 的 `Item` 实现：`summary()` 接收 `&text::BufferSnapshot`（即 `Summary::Context`），把两端锚点 `to_point` 后做行号差。`added_rows` 与 `removed_rows` 的算式一目了然。

[buffer_diff.rs:L202-L213](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L202-L213) 是 `PendingHunk` 的 `Item` 实现：区间字段照抄，但 `added_rows` / `removed_rows` 恒为 0——pending 只是 UI 状态覆盖，不该被计入行数统计；区间保留是因为树要靠它导航。

#### 4.2.4 代码实践

**实践目标**：手算一个修改型 hunk 的 `added_rows` / `removed_rows`，与程序输出对照。

**操作步骤**：base 文本为 `"one\ntwo\nthree\n"`，buffer 把 `two` 改成 `TWO\nEXTRA`（一行变两行）。按 4.2.2 的算式手算：buffer 侧 hunk 覆盖第 1~3 行（改后第 1、2 行是新内容），`added_rows = 3 − 1 = 2`；base 侧行区间是第 1~2 行，`removed_rows = 1`。然后在测试里打印验证：

```rust
// 示例代码：接在 4.1.4 的测试思路之后
let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base.clone(), cx);
let hunk = diff.hunks(&buffer).next().unwrap();
println!(
    "buffer rows {:?}, base bytes {:?}",
    hunk.range, hunk.diff_base_byte_range
);
```

**需要观察的现象**：`hunk.range` 为 `Point {row:1}..Point {row:3}`（两行），base 字节区间对应 `"two\n"`（一行）。

**预期结果**：与手算一致，即该 hunk 摘要为 `added_rows = 2, removed_rows = 1`。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：纯删除 hunk 的 `added_rows` 是多少？为什么？

答案：0。删除意味着 buffer 侧区间为空（起止锚点相同），行号差为 0；`saturating_sub` 只是把「不可能为负」这件事在类型层面兜住。

**练习 2**：如果把 `Summary::Context` 设计成能同时携带主 buffer 快照和 base rope，`InternalDiffHunk` 可以删掉哪个字段？

答案：`diff_base_point_range`。它存在的唯一理由就是摘要计算时上下文里没有 base rope，行数只能预先存好。

**练习 3**：`PendingHunk` 的摘要区间不为空但行数恒为 0，这种「半填充」的摘要有什么用？

答案：区间参与 `add_summary` 的并集合并与 `SeekTarget` 导航，让 pending 树能被按位置查询到；行数为 0 保证即使有人对 pending 树做统计也不会虚增数字。

### 4.3 Item 与 Summary：聚合代数与 changed_row_counts

#### 4.3.1 概念说明

树要维护的不变量是：**任何节点的摘要 = 其子树内所有元素摘要的合并结果**。合并运算由 `Summary::add_summary` 定义，它必须满足「以 `zero` 为单位元的结合半群」——这是树能任意分叉聚合的前提：

\[ S(T) = \text{add}(\text{add}(S_{\text{左}}), S_{\text{右}}) = \sum_{h \in T} S(h), \qquad \text{add}(S, \text{zero}) = S \]

对 `DiffHunkSummary`，加法就是「区间取并 + 计数累加」，单位元 `zero` 是「最小哨兵区间 + 零计数」。

#### 4.3.2 核心流程

`add_summary(&mut self, other)` 的四步（self 累加 other）：

1. `buffer_range.start` 取两者中更靠前的锚点，`buffer_range.end` 取更靠后的——外包络；
2. `diff_base_byte_range` 对整数做同样的 min/max；
3. `added_rows += other.added_rows`；
4. `removed_rows += other.removed_rows`。

`zero` 的构造（[buffer_diff.rs:L218-L225](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L218-L225)）用 `Anchor::min_min_range_for_buffer(buffer.remote_id())` 生成「最小锚点..最小锚点」的空区间（哨兵锚点的定义见 [text/anchor.rs:L79-L89](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/text/src/anchor.rs#L79-L89)），base 字节区间为 `0..0`，行数为 0。选「min..min」而不是「min..max」正是单位元的要求：min 锚点与任何锚点比较永远不大于、max 同理永远不小于，合并后得到的就是对方的区间；若是 min..max 会把一切区间吞成全范围。

在这套代数之上，`changed_row_counts()` 才能一行写完：读树根摘要，返回 `(added_rows, removed_rows)`（[buffer_diff.rs:L298-L301](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L298-L301)，根摘要来自 [sum_tree.rs:L736-L741](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L736-L741)）。实体层 `BufferDiff::changed_row_counts`（[buffer_diff.rs:L2158-L2162](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2158-L2162)）只是转发，diff 未算完时兜底 `(0, 0)`。

#### 4.3.3 源码精读

[buffer_diff.rs:L227-L245](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L227-L245) 是 `add_summary` 全文：锚点端的 min/max 需要传 `buffer` 参与比较（再次体现 Context 的作用），字节端的 min/max 是纯整数运算，最后两行做计数累加。

下游消费 `changed_row_counts()` 的三处真实调用：

- [editor/header.rs:L651-L656](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/element/header.rs#L651-L656)：编辑器头部（面包屑）显示的 `+N −M` 统计，`filter` 掉双零（无改动不显示）；
- [action_log.rs:L1058](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/action_log/src/action_log.rs#L1058)：记录用户改动了多少行；
- [multi_buffer.rs:L563](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/multi_buffer/src/multi_buffer.rs#L563)：multi_buffer 向上层转发单个 buffer 的 diff 统计。

#### 4.3.4 代码实践

**实践目标**：构造覆盖三种状态的 hunk（纯增、改、纯删），手算并断言 `changed_row_counts()`。

**操作步骤**：base 为六行文本，buffer 做三处修改——顶部插入一行、把 `two` 改成 `TWO`、删掉 `five`：

```rust
// 示例代码：加入 mod tests
#[gpui::test]
async fn test_changed_row_counts(cx: &mut gpui::TestAppContext) {
    let diff_base = "
        one
        two
        three
        four
        five
        six
    "
    .unindent();

    let buffer_text = "
        zero
        one
        TWO
        three
        four
        six
    "
    .unindent();

    let mut buffer = text::Buffer::new(
        text::ReplicaId::LOCAL,
        text::BufferId::new(1).unwrap(),
        buffer_text,
    );
    let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base.clone(), cx);

    // 三个 hunk：增 1 行、改 1 行、删 1 行
    assert_hunks(
        diff.hunks(&buffer),
        &buffer,
        &diff_base,
        &[
            (0..1, "", "zero\n", DiffHunkStatus::added_none()),
            (2..3, "two\n", "TWO\n", DiffHunkStatus::modified_none()),
            (5..5, "five\n", "", DiffHunkStatus::deleted_none()),
        ],
    );

    // 手算：(1+1+0, 0+1+1) = (2, 2)
    assert_eq!(diff.changed_row_counts(), (2, 2));
}
```

**需要观察的现象**：三个 hunk 分别是 Added / Modified / Deleted；行数统计是各 hunk 摘要的求和。

**预期结果**：测试通过（`cargo test -p buffer_diff test_changed_row_counts`）。若把删除行改成删除两行，`(2, 2)` 应变为 `(2, 3)`——改完再跑一遍验证你对手算规则的理解。

#### 4.3.5 小练习与答案

**练习 1**：base 五行文本，buffer 把第 2 行替换成三行，`changed_row_counts()` 返回什么？

答案：该 hunk 的 buffer 侧跨越 3 行、base 侧跨越 1 行，返回 `(3, 1)`。

**练习 2**：把 `add_summary` 中 `buffer_range` 的合并改成 `start` 取 max、`end` 取 min（即求交集），会出现什么后果？

答案：聚合语义被破坏——合并多个 hunk 后区间可能变空或收缩，树根摘要不再覆盖所有 hunk，按摘要剪枝的查询会漏掉 hunk，`SeekTarget` 导航也会走错分支。摘要聚合必须取并集（外包络）。

**练习 3**：为什么 `zero` 的 `buffer_range` 用 `min..min` 而不是 `min..max`？

答案：`zero` 是加法单位元，要求与任何摘要合并后得到那个摘要本身。min 锚点在任何比较中都不大于对方、max 锚点都不小于对方，所以 `min..min` 合并后正好还原对方区间；`min..max` 则会把合并结果撑成全范围，不再等于原摘要。

### 4.4 Anchor 与 usize：SeekTarget 与游标导航

#### 4.4.1 概念说明

树存好了、摘要聚合好了，最后一环是**导航**：给定一个位置，找到它落在哪个 hunk 上（或附近）。buffer_diff 需要两种坐标系：

- 按 **buffer 锚点**找：patch 映射、行区间查询都用它；
- 按 **base 字节偏移**找：`usize`，stage/unstage 时按 base 侧位置定位 hunk 用它。

`SeekTarget` 的职责是：拿「目标」与「游标当前所在子树的摘要」比较，返回 `Less` / `Equal` / `Greater` 三者之一，树据此决定向左、下钻还是向右。注意锚点版比较必须传 `buffer`——锚点的大小关系取决于它在哪个快照上解析（时间戳、片段归属），脱离 buffer 无法比较；而 `usize` 是纯整数，直接比。

#### 4.4.2 核心流程

以 `Anchor` 版为例（`usize` 版结构相同）：

```
目标锚点 vs 子树摘要的 buffer_range:
  小于 start  → Less    （目标在整个子树之前，往左走）
  大于 end    → Greater （目标在整个子树之后，往右走）
  否则        → Equal   （目标落在子树覆盖范围内，下钻）
```

两个真实调用点都遵循「先 `seek_forward` 再看 `item()`」的模式，并处理边界：

- `hunk_before_buffer_anchor`（[buffer_diff.rs:L505-L521](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L505-L521)）：seek 后若当前项不存在或目标在当前项 start 之前，就 `prev()` 回退一项，再 `filter` 保证目标 ≥ 项的 start——语义是「最后一个 start 不超过目标的 hunk」；
- `hunk_before_base_text_offset`（[buffer_diff.rs:L487-L503](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L487-L503)）：同样的模式，但目标是 `usize`，走 `usize` 那个 `SeekTarget` 实现。

游标（`Cursor`）的三个关键方法（[cursor.rs:L82-L99](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L82-L99)）：

- `start()`：**当前项之前**所有项的摘要之和（前缀摘要）；
- `end()`：`start()` 加上当前项自己的摘要；
- `item()`：当前项；注意必须先 `seek` / `next` / `prev` 过一次才能调用，否则断言失败（[cursor.rs:L388-L393](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L388-L393)）。

而查询 API 的性能来自 `filter`：`SumTree::filter`（[sum_tree.rs:L609-L619](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L609-L619)）返回的 FilterCursor 在下降时把**子树摘要**喂给谓词，谓词返回 false 就整棵子树跳过。`hunks_intersecting_range` 的谓词正是「子树外包络与查询区间是否相交」（[buffer_diff.rs:L331-L336](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L331-L336)），于是区间外的大块 hunk 根本不会被逐个访问——这就是外包络并集摘要换来的剪枝能力。

#### 4.4.3 源码精读

[buffer_diff.rs:L248-L261](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L248-L261) 实现 `SeekTarget for Anchor`：三段判断分别基于子树摘要 `buffer_range` 的 start 与 end，锚点比较都带 `buffer` 参数。

[buffer_diff.rs:L263-L273](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L263-L273) 实现 `SeekTarget for usize`：与子树摘要的 `diff_base_byte_range`（base 字节维度）比较，纯整数比较，上下文参数直接忽略。

[buffer_diff.rs:L1008-L1010](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1008-L1010) 展示 filter 游标如何变成普通迭代器：`self.hunks.filter::<_, DiffHunkSummary>(buffer, filter)` 之后直接 `flat_map`，FilterCursor 实现了 `Iterator`（[cursor.rs:L727-L740](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L727-L740)）。同函数 L1028-L1029 则是「建游标后先 `next()` 再用」的标准姿势。

#### 4.4.4 代码实践

**实践目标**：亲手推一遍游标，观察前缀摘要（`start()`）如何随推进单调增长，并验证「走完全部后 `start()` 等于树根摘要」。

**操作步骤**（示例代码，加入 `mod tests`；沿用 4.3.4 的三 hunk 场景）：

```rust
// 示例代码：加入 mod tests
use text::ToOffset as _;

let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base.clone(), cx);
let mut cursor = diff.hunks.cursor::<DiffHunkSummary>(&buffer);
cursor.next(); // 游标必须先动一次才能调用 item()
let mut visited = 0;
while let Some(hunk) = cursor.item() {
    let prefix = cursor.start().clone(); // 当前项之前的所有摘要之和
    let end = cursor.end();              // prefix + 当前项摘要
    println!(
        "hunk #{visited} base_bytes={:?} prefix=+{}-{} end=+{}-{} prefix_buffer_bytes={:?}",
        hunk.diff_base_byte_range,
        prefix.added_rows, prefix.removed_rows,
        end.added_rows, end.removed_rows,
        prefix.buffer_range.start.to_offset(&buffer)..prefix.buffer_range.end.to_offset(&buffer),
    );
    visited += 1;
    cursor.next();
}
// 走完全部后：游标前缀 == 树根摘要
assert_eq!(visited, 3);
assert_eq!((cursor.start().added_rows, cursor.start().removed_rows), (2, 2));
assert_eq!(
    (diff.hunks.summary().added_rows, diff.hunks.summary().removed_rows),
    (2, 2)
);
```

**需要观察的现象**（用 `cargo test -p buffer_diff <测试名> -- --nocapture` 运行）：打印逐行递增——

| 游标停在 | prefix（不含本项） | end（含本项） |
| --- | --- | --- |
| hunk #0（新增 zero） | `+0 −0`，buffer 字节 0..0 | `+1 −0` |
| hunk #1（two→TWO） | `+1 −0`，buffer 字节 0..5 | `+2 −1` |
| hunk #2（删 five） | `+2 −1`，buffer 字节 0..13 | `+2 −2` |
| 越过末尾 | `+2 −2`（= 树根摘要） | — |

**预期结果**：断言全部通过；prefix 的 `buffer_range` 外包络随推进逐步撑开（0..0 → 0..5 → 0..13），这就是 `add_summary` 并集合并的动态画面。

#### 4.4.5 小练习与答案

**练习 1**：`SeekTarget::cmp` 返回 `Equal` 意味着「找到目标」吗？

答案：不完全是。`Equal` 只说明目标落在当前（子）树摘要的覆盖范围内，树会继续向叶子下钻；最终命中的是**一个元素**，其区间可能只是「包含目标位置」而非精确等于目标。`hunk_before_*` 系列还会在命中后做 `prev()` 回退，取「start 不超过目标的最后一个 hunk」。

**练习 2**：为什么 `usize` 的 `cmp` 可以不使用上下文参数，`Anchor` 的不行？

答案：字节偏移是全局固定的整数全序；锚点的排序取决于其时间戳与片段归属，必须在特定 buffer 快照上解析后才能比较，所以要把 `buffer` 传进 `cmp`。

**练习 3**：filter 谓词收到的是子树摘要而非单个元素，这带来什么复杂度变化？

答案：区间不相交的整棵子树被一次比较剪掉，查询从遍历全部 n 个 hunk 变为 O(log n + k)（k 为命中数）。前提正是 4.3 节的并集摘要——外包络不相交，子树内任何 hunk 都不可能相交。

## 5. 综合实践

把本讲三个模块串成一个完整测试：**构造三种状态的 hunk → 用 `assert_hunks` 验证内容 → 用 `changed_row_counts` 验证聚合 → 用游标验证前缀摘要增长 → 用 filter 验证剪枝后只访问相关 hunk**。

在练习分支上，把下面的测试加入 `crates/buffer_diff/src/buffer_diff.rs` 底部的 `mod tests`（示例代码）：

```rust
#[gpui::test]
async fn test_sumtree_hunk_storage_practice(cx: &mut gpui::TestAppContext) {
    use text::ToOffset as _;

    let diff_base = "
        one
        two
        three
        four
        five
        six
    "
    .unindent();

    let buffer_text = "
        zero
        one
        TWO
        three
        four
        six
    "
    .unindent();

    let mut buffer = Buffer::new(ReplicaId::LOCAL, BufferId::new(1).unwrap(), buffer_text);
    let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base.clone(), cx);

    // 第 1 步：公开 API 视角——三个 hunk，三种状态
    assert_hunks(
        diff.hunks(&buffer),
        &buffer,
        &diff_base,
        &[
            (0..1, "", "zero\n", DiffHunkStatus::added_none()),
            (2..3, "two\n", "TWO\n", DiffHunkStatus::modified_none()),
            (5..5, "five\n", "", DiffHunkStatus::deleted_none()),
        ],
    );

    // 第 2 步：聚合视角——树根摘要一行给出 +2 −2
    assert_eq!(diff.changed_row_counts(), (2, 2));

    // 第 3 步：游标视角——前缀摘要单调增长，走完等于树根
    let mut cursor = diff.hunks.cursor::<DiffHunkSummary>(&buffer);
    cursor.next();
    let mut visited = 0;
    while let Some(hunk) = cursor.item() {
        let prefix = cursor.start().clone();
        let end = cursor.end();
        println!(
            "hunk #{visited} base_bytes={:?} prefix=+{}-{} end=+{}-{}",
            hunk.diff_base_byte_range,
            prefix.added_rows,
            prefix.removed_rows,
            end.added_rows,
            end.removed_rows,
        );
        visited += 1;
        cursor.next();
    }
    assert_eq!(visited, 3);
    // DiffHunkSummary 未派生 PartialEq，只能逐字段比较
    assert_eq!(
        (cursor.start().added_rows, cursor.start().removed_rows),
        (
            diff.hunks.summary().added_rows,
            diff.hunks.summary().removed_rows
        ),
    );

    // 第 4 步：剪枝视角——只看 base 字节 19..24（"five\n"）附近的 hunk
    let staged_view: Vec<_> = diff
        .hunks
        .filter::<_, DiffHunkSummary>(&buffer, |summary| {
            !(summary.diff_base_byte_range.end < 19 || summary.diff_base_byte_range.start > 24)
        })
        .map(|hunk| hunk.diff_base_byte_range.clone())
        .collect();
    assert_eq!(staged_view, vec![19..24]);
}
```

**运行**：仓库根目录执行

```bash
cargo test -p buffer_diff test_sumtree_hunk_storage_practice -- --nocapture
```

**验收标准**：

1. 四个视角互相印证：3 个 hunk、(2, 2) 的行数、逐行递增的前缀摘要、剪枝后只剩 base 字节 `19..24` 那一个 hunk；
2. 能向别人解释每一步分别踩在本讲哪个 trait 上（`Item::summary` → `Summary::add_summary` → `SeekTarget` / filter 谓词）。

## 6. 本讲小结

- `BufferDiffSnapshot` 用两棵 SumTree 存差异：`hunks: SumTree<InternalDiffHunk>` 是真实差异，`pending_hunks: SumTree<PendingHunk>` 是乐观 UI 状态，两者共享摘要类型 `DiffHunkSummary`。
- `DiffHunkSummary` 有三个维度：buffer 锚点区间（并集）、base 字节区间（并集）、`added_rows` / `removed_rows`（求和）；`added_rows` 来自锚点行号差，`removed_rows` 依赖预先存好的 `diff_base_point_range`（因为 `Summary::Context` 只有主 buffer 一个槽位）。
- 聚合代数：`add_summary` = 区间取外包络 + 计数累加；`zero` 用 min..min 哨兵区间保证单位元性质；`changed_row_counts()` 读树根摘要即可返回全 buffer 的 (+N, −M)，编辑器头部、action_log、multi_buffer 都在用它。
- 导航：`Anchor` 与 `usize` 各实现一个 `SeekTarget`，分别在 buffer 坐标系和 base 字节坐标系上二分下降；游标的 `start()` 是前缀摘要、`end()` 含当前项，用前必须先 `next()`/`seek`。
- filter 谓词作用于**子树摘要**，配合并集外包蕴实现整子树剪枝，这是 `hunks_intersecting_range` 系查询的性能来源。

## 7. 下一步学习建议

下一讲 **u2-l3「hunk 查询 API 家族与过滤机制」**将把本讲的存储层接到调用方：`hunks_intersecting_range`（正向/反向）、`hunks_intersecting_base_text_range`、`hunks_in_row_range`、`range_to_hunk_range` 等接口如何组合 filter 谓词与 `summaries_for_anchors_with_payload`，以及 hunk 结束点向下一行行首扩展的规则。建议先预习 [buffer_diff.rs:L1002-L1131](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1002-L1131)，带着「filter 游标 + 双游标归并」的预期去读；有余力的读者可以再翻翻 [sum_tree/cursor.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs) 里 `search_forward` 的完整实现，看剪枝是如何在内部节点与叶子两个层级发生的。
