# 网格高级机制：rowspans、repeated 表头、lines

## 1. 本讲目标

在上一篇（u6-l1）里，我们已经看清了 `GridLayouter` 的主链路：列宽一次性求解、行高逐 region 懒解析、单元格经 `layout_fragment` 排版、跨页产出多帧 `Fragment`。但那篇刻意回避了三个「让表格真正可用」的难题：

1. 一个单元格要**跨多行**（rowspan），而每行的高度是事后才知道的，这个单元格该怎么排？
2. 表格跨页时，**表头（header）要在每一页顶部重复**，而且表头可能有好几级、还可能彼此冲突，怎么管？
3. 表格的**线条（hline/vline）**遇到合并单元格（colspan/rowspan）要断开、遇到不同来源的 stroke 要合并，怎么生成？

本讲围绕这三个难题展开，对应 `src/grid/` 下三个文件。读完本讲你应当能够：

- 说清楚为什么 rowspan 必须延迟到「所有跨及行都排完」才能排版，以及 `unbreakable_rows_left` 如何在排版过程中抑制断裂。
- 画出表头在 `upcoming_headers → pending_headers → repeating_headers` 三个向量之间的流转，并解释多级表头在新 region 顶部如何被重复。
- 描述 `generate_line_segments` 如何把一根线切成若干段、`StrokePriority` 如何决定叠放顺序。

## 2. 前置知识

本讲假设你已经读过 u6-l1，掌握了下列概念（不再重复解释）：

- **CellGrid**：typst-library 把元素树摊平成的二维矩阵，合并格以 `Entry::Merged { parent }` 指回父格。
- **Sizing 三态**：`Rel`（定/相对高）、`Auto`（按内容测量）、`Fr`（分数瓜分剩余）。
- **region / `Regions`**：排版的「画布」，逐 region 推进；表格跨页就是跨 region（见 u2-l2）。
- **`layout_fragment`**：即 flow，单元格内容就是交给它排版的。
- **`finish_region`**：组装当前 region 的一帧、推进 region 队列的函数。

此外补充三个本讲反复用到、但定义在 typst-library `layout::grid::resolve` 里的类型：

- **`Header` / `Footer`**：一组连续行构成的表头/表脚，带 `range`（行范围）、`level`（层级）、`repeated`（是否重复）等字段。
- **`Repeatable<T>`**：给 `Header`/`Footer` 套一层「是否可重复」的标记，是表头状态机的核心包装类型。
- **`Line`**：用户用 `hline`/`vline` 显式放置的一条线，带 `index`、`start`/`end`、`stroke`、`position`（`Before`/`After`）。

还要先建立一个贯穿全讲的**计数器心智模型**——`GridLayouter` 有一个字段 `unbreakable_rows_left`：当它大于 0 时，排版器**禁止换 region**。表头行、表脚行、以及「不可断裂行组」里的行都会在排版前把它「充能」，排完一行再减一。它是表头与 rowspan 共用的「把若干行绑死在同一个 region」的机制，本讲会反复回到它。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/grid/rowspans.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/rowspans.rs) | 跨行单元格的检测、延迟排版、auto 行的 rowspan 高度模拟；定义 `Rowspan`、`UnbreakableRowGroup`、`RowspanSimulator`。 |
| [src/grid/repeated.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/repeated.rs) | 表头/表脚的重复机制：三向量状态机、孤儿行防护（orphan prevention）、在新 region 顶部重排表头。 |
| [src/grid/lines.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/lines.rs) | 线段生成：`generate_line_segments` 通用分段器、`vline_stroke_at_row`/`hline_stroke_at_column` 的 stroke 合并、`StrokePriority`。 |
| [src/grid/layouter.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs) | `GridLayouter` 本体。本讲会读它的主循环 `layout`、`layout_row_internal`、`render_fills_strokes`、`finish_region` 中处理 rowspan 与表头重置的片段。 |

三个子系统并非独立：rowspan 与表头都依赖 `unbreakable_rows_left`；表头测量复用 rowspan 的 `simulate_unbreakable_row_group`；线条生成又必须感知 rowspan/colspan 才能正确断开。本讲按「rowspan → 表头 → 线条」的顺序讲，因为后者依赖前者的概念。

---

## 4. 核心概念与源码讲解

### 4.1 跨行单元格 rowspans：延迟到所有跨及行排完

#### 4.1.1 概念说明

rowspan（跨行单元格）指的是一个单元格占据多行，典型场景是表格里「合并纵向若干格」。在 Typst 里，合并信息在 typst-library 阶段已经摊平进 `CellGrid`：一个跨 3 行的单元格只在它的**起始行** `(x, y)` 存一个真正的 `Cell`（带 `rowspan: 3`），下面两行对应位置是 `Entry::Merged { parent }`。

排版它的难点在于一个**先有鸡还是先有蛋**的困境：

- rowspan 的内容要排进一个**矩形区域**，这个区域的高度 = 它跨的**所有行高度之和**。
- 但行高是**逐 region 懒解析**的——`Auto` 行的高度要等测量完内容才知道，而且行还可能**跨页断裂**。
- 所以当排版器走到 rowspan 的起始行 `y` 时，它根本不知道 `y+1`、`y+2` 行有多高，甚至不知道这些行最终落在哪几个 region。

解决办法是**延迟**：先记录这个 rowspan，等它跨的**所有行都被解析完**，把每一段高度按 region 累加好，再回过头用「每 region 一段高度」的多区域 pod 把单元格内容一次性排完。这就是文件顶部那段注释点明的核心——

> We need to do this only once we already know the heights of all spanned rows, which is only possible after laying out the last row spanned by the rowspan.

#### 4.1.2 核心流程

rowspan 的生命周期分四步：

1. **检测**（排版某行时）：`check_for_rowspans` 扫描该行的每个单元格，发现 `rowspan > 1` 就往 `self.rowspans` 里塞一条 `Rowspan` 记录，此时高度相关字段全部留空待填。
2. **充能不可断裂**：若该 rowspan 的单元格 `breakable == false`，或它整个落在一个不可断裂行组里，则通过 `unbreakable_rows_left` 把它跨的行绑死在同一个 region。
3. **累积高度**（`finish_region` 里逐行放置时）：每放好一行，就把该行高度加到「跨及该行」的每条 rowspan 在**当前 region** 的高度桶里；用 `max_resolved_row` 防止被表头重复的行重复计入。
4. **回填排版**：当 rowspan 的**最后一行**被解析完，调用 `layout_rowspan`：用累加好的 `heights`（每 region 一段）构造多区域 pod，调 `layout_cell` 排版，把产出的各帧分别 `push_frame` 回对应 region 的 frame。

用一个伪代码示意第 3、4 步的关系：

```
# finish_region 中，逐行放置时
for row in current.lrows:
    for rowspan in rowspans 跨及该 row:
        rowspan.heights[current_region] += row.height   # 累积
    for rowspan 其最后一行恰为该 row:
        layout_rowspan(rowspan)   # 高度已齐，回填排版
```

> 为什么「最后一行排完」才是触发点？因为只有到那时，所有跨及行的高度都已知，`heights` 才完整。在那之前任何时刻回填都会得到错误的区域高度。

#### 4.1.3 源码精读

**`Rowspan` 结构体**——一条待处理的跨行单元格的全部信息：

[grid/rowspans.rs:11-48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/rowspans.rs#L11-L48) 定义了 `Rowspan`。关键字段：

- `heights: Vec<Abs>`——**每个 region 一段可用高度**，这是延迟排版能成立的命脉，在 `finish_region` 里逐 region 累积。
- `first_region` / `dy` / `region_full`——记录 rowspan 首次出现的 region、在该 region 的纵向偏移、该 region 的 `full`（供相对尺寸用）。
- `max_resolved_row: Option<usize>`——已计入高度的「最大行号」，防止重复行（如表头）二次贡献高度。
- `is_effectively_unbreakable`——该 rowspan 是否实质不可断裂（见 4.1.5）。

**第 1 步：检测**——`check_for_rowspans` 在排版每行前被调用（见 `layout_row_internal`），逐列查单元格的 `effective_rowspan`：

[grid/rowspans.rs:203-231](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/rowspans.rs#L203-L231)。注意 `dx` 来自 `points(self.rcols...)`（列偏移），高度相关字段（`dy`/`first_region`/`heights`）都先填占位值，留到 `finish_region` 填。

**第 3 步：累积高度**——发生在 `finish_region` 的「放置已排好行」循环里：

[grid/layouter.rs:1707-1746](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1707-L1746)。这段做三件事：首次触达时设 `first_region`/`dy`/`region_full`；把 `heights` 向量补齐到当前 region 并把当前行高度加进最后一个桶；若该行是 rowspan 跨及的最后一行（`is_last`），设 `max_resolved_row` 防止重复计入。

紧接着是**第 4 步：回填排版**的触发判断：

[grid/layouter.rs:1754-1790](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1754-L1790)。条件 `rowspan.y + rowspan.rowspan == y + 1 && is_last`（或更早结束的 rowspan）一旦满足，就从 `self.rowspans` 取出并调 `layout_rowspan`，同时把当前 region 的 frame 和已解析表头高度作为 `current_region_data` 传入。

**`layout_rowspan` 本体**——用累积好的 `heights` 构造多区域 pod 并排版：

[grid/rowspans.rs:103-199](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/rowspans.rs#L103-L199)。要点：

- L123-136：`[first_height, backlog @ ..] = heights` —— 把首段高度当 pod 的 `size.y`，其余段当 `backlog`，正好把「每 region 一段高度」喂给一个标准 `Regions`。
- L138-148：若 rowspan 跨了 `Auto` 行且可断裂，pod 的 `full` 设为 `region_full`，使其内部相对尺寸按整页算（与普通 auto 行单元格一致）。
- L174-196：把产出帧逐个 `push_frame` 回**已完成的 region + 当前 region**；第一帧用原始 `dy`，后续帧的 `dy` 取「该 region 重复表头的高度」，使续接部分从表头之下开始。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是亲眼看「累积 → 回填」的配合。

1. **实践目标**：验证 rowspan 的 `heights` 在 `finish_region` 里被逐 region 累积，且只在最后一行排完后才触发 `layout_rowspan`。
2. **操作步骤**：
   - 在 [grid/layouter.rs:1739](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1739)（`*rowspan.heights.last_mut().unwrap() += height;`）后临时加一行：
     ```rust
     eprintln!("[accumulate] rowspan@({}, {}) region {} += {:?} (now {:?})", rowspan.x, rowspan.y, current_region, height, rowspan.heights);
     ```
   - 在 [grid/rowspans.rs:152](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/rowspans.rs#L152)（`layout_cell` 调用前）加一行：
     ```rust
     eprintln!("[layout_rowspan] cell@({}, {}) heights={:?}", x, y, heights);
     ```
   - 用 typst CLI 编译一个跨页、含 rowspan 的表格（示例代码）：
     ```typst
     #set page(height: 3cm)
     #table(
       columns: 2,
       table.cell(rowspan: 3)[A], [1], [2], [3],
       table.cell(rowspan: 3)[B], [4], [5], [6],
     )
     ```
3. **需要观察的现象**：日志里同一个 rowspan 会先出现多次 `[accumulate]`（每解析一行、每 region 一条），`heights` 向量逐步变长；只有当最后一行解析完，才会出现一次 `[layout_rowspan]`，且其 `heights` 恰好是累积到的完整序列。
4. **预期结果**：你会清楚看到「先累积、末行触发」的顺序；若把 `rowspan: 3` 改成不可断裂（`table.cell(rowspan: 3, breakable: false)`），所有 `[accumulate]` 会挤在同一个 region，`heights` 只有一个元素。
5. 改完务必撤销这两处 `eprintln!`，不要提交。

> 若本地无法编译，可改为纯阅读：对照 [layouter.rs:1707-1746](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1707-L1746) 与 [rowspans.rs:123-136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/rowspans.rs#L123-L136)，在纸上为一个跨 3 行、其中 1 行是 auto 的 rowspan 推演 `heights` 的变化。**待本地验证**运行日志。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `max_resolved_row` 必须存在？如果没有它，重复表头会让 rowspan 出什么问题？

> **参考答案**：表头在新 region 会被重复排版，其行号 `y` 与原始行相同。若不追踪 `max_resolved_row`，重复出现的表头行会被当成「新解析的行」再次把高度加进 `rowspan.heights`，导致 rowspan 可用高度被重复计入而膨胀。`max_resolved_row` 保证每条被跨的行**只贡献一次**高度（见 [layouter.rs:1712-1713](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1712-L1713) 与 [layouter.rs:1741-1745](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1741-L1745)）。

**练习 2**：`is_effectively_unbreakable` 为何不是简单等于 `!cell.breakable`？

> **参考答案**：一个 rowspan 即使自身 `breakable == true`，若它**恰好整体落在一个不可断裂行组内**（例如它在表头里，或与其它不可断裂单元格同组），也应当被当作不可断裂处理。该字段在 `check_for_rowspans` 先初始化为 `!cell.breakable`（[rowspans.rs:219](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/rowspans.rs#L219)），随后在 `check_for_unbreakable_rows` 里按「剩余不可断裂行数是否覆盖整个 rowspan」二次置位（[rowspans.rs:288-293](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/rowspans.rs#L288-L293)）。

#### 4.1.6 选读：不可断裂行组与 auto 行 rowspan 模拟

这部分较硬核，初学者可先跳过。它处理两个边界情形。

**(a) 不可断裂行组 `UnbreakableRowGroup`**。当若干行因「不可断裂单元格」或「跨页 rowspan」而必须绑在一起时，排版器要在换 region 前**预知这组行共需多高**，否则会换到新 region 才发现放不下。`simulate_unbreakable_row_group` 干的就是这件事：

[grid/rowspans.rs:306-368](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/rowspans.rs#L306-L368)。它从 `first_row` 起逐行累加：`Rel` 行直接解析高度，`Auto` 行用无限高测量（不可断裂 auto 行只占一 region），`Fr` 行记 0；同时用 `check_for_unbreakable_cells` 判断还要把后续几行也拉进组。算出总高后，`check_for_unbreakable_rows` 会跳过放不下的 region（[rowspans.rs:268-272](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/rowspans.rs#L268-L272)），并把 `unbreakable_rows_left` 充能为这组的行数（[rowspans.rs:275](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/rowspans.rs#L275)）——此后 `layout_row_internal` 便不会在排这几行时换 region。

**(b) rowspan 结束于 auto 行时的循环依赖**。这是最棘手的情形：rowspan 内容要多高取决于它跨的行提供多少空间，而其中 auto 行的高度又取决于 rowspan 内容要多高——循环了。更糟的是，跨页时**行间 gutter 会消失**（页顶不留 gutter），使可用空间进一步缩水。

`run_rowspan_simulation` 用**迭代模拟**逼近：先测 rowspan 需求，预测 auto 行需长高多少；再模拟后续跨及行在各 region 的落位（含 gutter 消失），修正预测；如此最多 5 轮直到稳定：

[grid/rowspans.rs:879-1002](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/rowspans.rs#L879-L1002)。文件 L829-877 的注释给了一个极好的数值例子：一个 8pt 高的 rowspan跨 `(1pt, auto, 0.5pt, 0.5pt)` 四行、行间 1pt gutter，正常需 auto 行长高 3pt（8 − 2pt定高 − 3pt gutter）；但若末行被挤到下一页、其前 1pt gutter 消失，可用空间就少 1pt，auto 行必须再多长一点。模拟正是为预测这类 gutter 消失而存在。辅助状态机 `RowspanSimulator`（[rowspans.rs:1006-1270](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/rowspans.rs#L1006-L1270)）逐行重放 region 推进与表头/表脚扣减，最终汇总「已被后续行覆盖的高度」，从而算出 auto 行真正需要补的高度。

---

### 4.2 重复表头 repeated：多级 pending/upcoming/repeating 状态机

#### 4.2.1 概念说明

表格常常有一个「表头」（如 `[姓名, 年龄]`），当表格跨页时，我们希望**每一页顶部都重复出现这个表头**。Typst 的 `table.header` 还支持**多级表头**——例如一级表头「人员信息 / 成绩」下面再各跟一行子表头——以及**显式不重复**的表头。

用一个状态机来管理这些表头在排版过程中的「身份变迁」：

- **`upcoming_headers`**：**尚未遇到**的表头。初始化为 `grid.headers` 的全部。主循环走到某表头的行范围时，从队首消费。
- **`pending_headers`**：**首次排出来了、但还没确认不是孤儿**的表头。它已经进过一次 frame，但若紧跟它的非表头行还没排出来（即它可能是页尾孤零零一个表头），就要回滚。
- **`repeating_headers`**：**已确认、会在每个新 region 顶部重复**的表头。按 `level` 升序排列。

它们之间的流转是单向的：`upcoming → pending → repeating`。再加上两个机制：

- **孤儿行防护（orphan prevention）**：表头不允许单独出现在某页底部。用一个快照 `lrows_orphan_snapshot` 记下「排表头前的行数」，若本 region 结束时快照仍在（说明表头后面没跟别的行），就把表头行撤掉、下个 region 重排。
- **`unbreakable_rows_left`**：表头一组行必须排在同一个 region 顶部，所以在排它们之前把该组行数充能进去，强制不可断裂。

#### 4.2.2 核心流程

表头的处理分布在三处调用点：

1. **主循环遇表头**（`layout`）：`place_new_headers` 从 `upcoming_headers` 消费一组连续表头，决定它们是 short-lived（立即排、不进 pending）还是常规（进 pending 等确认）。
2. **每个非表头行排完后**：`flush_orphans` 清掉孤儿快照，并调 `flush_pending_headers` 把 `pending`（且 `repeated == true`）的表头**升格**进 `repeating`。
3. **换 region 时**（`finish_region` 末尾）：`layout_active_headers` 在新 region 顶部把 `repeating_headers` + `pending_headers` 重新排一遍。

多级表头的关键在于 `place_new_headers` 会**累积连续的表头**：遇到第一个表头时不立即处理，而是等到「下一个表头不再紧邻或不再冲突」才把这一整组一起放进 `pending`。冲突规则按 `level`：新表头会**踢掉** `repeating_headers` 里所有 `level >= 新表头 level` 的旧表头（它们不再重复）。

用一张文字流转图：

```
upcoming_headers ──(主循环遇到)──► place_new_headers
                                        │
                          ┌─────────────┴─────────────┐
                  short-lived                     常规表头
                  立即排版                          │
                  (不重复)                   存入 pending_headers
                                                   │
                                   排完一个非表头行 (flush_orphans)
                                                   ▼
                                         repeating_headers
                                                   │
                                  换 region (layout_active_headers)
                                                   ▼
                                    在新 region 顶部重复排版
```

#### 4.2.3 源码精读

**三个向量与 `unbreakable_rows_left` 字段**定义在 `GridLayouter` 主体：

[grid/layouter.rs:42-67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L42-L67)。注意 L42-45 对 `unbreakable_rows_left` 的注释：*while this is positive, no region breaks should occur*。

**主循环如何遇表头**——`layout` 里：

[grid/layouter.rs:329-338](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L329-L338)。若当前行 `y` 落在「`upcoming_headers` 第 `consecutive_header_count` 个表头」的范围内，就调 `place_new_headers` 并把 `y` 跳到该表头范围末尾。紧跟一行非表头内容后排完后会 `flush_orphans`（[layouter.rs:365](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L365)）。

**`place_new_headers`——多级累积与冲突踢除**：

[grid/repeated.rs:33-122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/repeated.rs#L33-L122)。读这段重点看三处：

- L39-50：`split_at(consecutive_header_count)` 把队首若干个连续表头切出来；若下一个表头**紧邻且非 short-lived**，则 `return Ok(())` 提前返回，**等下次循环继续累积**——这就是「多级表头攒成一组」的逻辑。
- L70-80：用 `partition_point` 按 `level` 找到第一个与新表头冲突的旧 repeating 表头，`truncate` 掉它们，并同步把 `repeating_header_heights` 里对应高度扣回来——**冲突表头停止重复**。
- L83-119：区分 short-lived（立即排，`layout_new_headers(..., true, ...)`）与常规（`layout_new_headers(..., false, ...)` 后存入 `pending_headers`）。

**`layout_new_headers`——首次排版 + 孤儿快照**：

[grid/repeated.rs:362-430](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/repeated.rs#L362-L430)。L391-401 是孤儿防护的入口：`should_snapshot` 为真时记下 `lrows_orphan_snapshot = Some(self.current.lrows.len())`，返回 `should_snapshot` 让调用方知道是否成功建快照；若建不了（region 无法 progress），调用方会 `flush_orphans` 把这批表头「定稿」。L405-406 把这组表头行数充能进 `unbreakable_rows_left`。

**`flush_pending_headers`——升格 pending 为 repeating**：

[grid/repeated.rs:171-195](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/repeated.rs#L171-L195)。只搬 `header.repeated` 为真的；注释详细论证了为何 push 到末尾仍能保持按 `level` 升序（因为冲突的旧表头已在 `place_new_headers` 里被 truncate）。

**`layout_active_headers`——新 region 顶部重排**：

[grid/repeated.rs:203-352](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/repeated.rs#L203-L352)。它在 `finish_region` 换 region 后被调用（见 [layouter.rs:1827-1830](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1827-L1830)）。要点：

- L206：`disambiguator = self.finished.len()`——用**已完成的 region 数**当消歧值，保证同一表头在每次重复时拿到不同 `Location`（详见 u2-l4 的 disambiguator）。
- L221-243：若表头 + 表脚在当前 region 放不下，就跳过 region（排一个空帧）直到放下。
- L267：把 `repeating + pending` 的总行数充能进 `unbreakable_rows_left`，**这正是抑制这组表头中途断裂的关键**。
- L296-339：逐个排 repeating 表头（`is_being_repeated: true`）与 pending 表头（`is_being_repeated: false`），并把高度记进 `repeating_header_heights`，供 rowspan 测量与线条优先级判断使用。

**`unbreakable_rows_left` 抑制断裂的「消费端」**——在 `layout_row_internal`：

[grid/layouter.rs:434-437](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L434-L437) 只有 `unbreakable_rows_left == 0` 才允许 `regions.is_full()` 时换 region；而 [layouter.rs:461](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L461) 每排完一行就 `saturating_sub(1)`。两者一充一放，把表头/不可断裂组「焊」在同一 region。

#### 4.2.4 代码实践

这是本讲的**主实践任务**（对应任务规格）。

1. **实践目标**：用源码追踪解释「多级表头在新 region 顶部如何被重复」以及「`unbreakable_rows_left` 如何抑制断裂」。
2. **操作步骤**：
   - 先读 [grid/layouter.rs:314-368](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L314-L368) 的主循环，确认表头由 `place_new_headers` 接管、正文行排完后调 `flush_orphans`。
   - 再读 [repeated.rs:33-122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/repeated.rs#L33-L122)（`place_new_headers`），重点理解 L39-50 的「连续累积」与 L70-80 的「冲突踢除」。
   - 接着读 [repeated.rs:203-352](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/repeated.rs#L203-L352)（`layout_active_headers`），注意 L267 把表头行数加进 `unbreakable_rows_left`。
   - 最后对照 [layouter.rs:434-461](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L434-L461) 验证：只要 `unbreakable_rows_left > 0`，即使 `regions.is_full()` 也不会换 region。
3. **需要观察的现象**（在脑中/纸上推演一个两级表头跨页表格）：
   - 第 1 个 region：`upcoming` 消费掉两个表头 → 进 `pending` → 排第一行正文后 `flush_orphans` 把它们升格为 `repeating`。
   - 换 region 时：`layout_active_headers` 把这两个 repeating 表头在**新 region 顶部再排一遍**，`disambiguator` 加 1，`unbreakable_rows_left` 被充能为两表头总行数，于是它们不会被中途截断。
4. **预期结果**：你能用自己的话写出「为什么表头不会被拆到两页」——因为 `layout_active_headers` 充能了 `unbreakable_rows_left`，而 `layout_row_internal` 在该值 > 0 时拒绝换 region。
5. 若想本地验证，可编译下面示例（示例代码），并临时在 [repeated.rs:298](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/repeated.rs#L298)（`layout_header_rows` 调用处）加 `eprintln!`，观察每个 region 顶部表头被重排的次数与 `disambiguator`：

   ```typst
   #set page(height: 2.5cm)
   #table(
     columns: 3,
     table.header(
       [大类 A], [], [],
       [子类], [项], [值],
     ),
     ..(1, 2, 3, 4, 5, 6, 7, 8, 9),
   )
   ```
   **待本地验证**日志输出。

#### 4.2.5 小练习与答案

**练习 1**：`pending_headers` 为什么要存在？为什么不能让一个新表头直接进 `repeating_headers`？

> **参考答案**：新表头刚排出来时，可能紧跟其后的非表头行还没排（它可能成为页尾孤儿：单独一个表头挂在页底，下面没内容）。`pending` 是「待确认」状态：只有当**至少一个非表头行**成功排在它之后（`flush_orphans` 被调用），才证明它不是孤儿，方可升格为 `repeating`。直接进 `repeating` 会让孤儿表头也被重复，且无法回滚。

**练习 2**：short-lived 表头是什么？为什么它不进 `pending`、也不重复？

> **参考答案**：short-lived 表头是**紧跟着一个同级或更低级冲突表头**的表头，因此它永远没有机会重复（下一行就被新表头覆盖）。`place_new_headers` 在 L42-46 判定它，并走 L83-95 分支：立即排版、`flush_orphans`、不进 `pending`。它们「实质上不是表头」，所以注释里说 *basically are not headers, for all intents and purposes*。

**练习 3**：表脚（footer）的重复机制和表头有何异同？

> **参考答案**：表脚用同一套 `Repeatable` 包装与 `simulate_footer`/`prepare_footer`/`layout_footer` 机制（[repeated.rs:471-546](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/repeated.rs#L471-L546)），同样用 `unbreakable_rows_left` 绑死、同样在每个 region 重排。不同的是：表脚在每个 region **末尾**排版（由 `finish_region` 在组装阶段调 `layout_footer(..., true)`），且 `RowState::is_being_repeated` 对表脚是「最后一次为真、此前为假」，与表头（「首次为假、其后为真」）相反（见 [layouter.rs:186-190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L186-L190)）。

---

### 4.3 线段生成 lines：hline/vline 分段与 stroke 合并

#### 4.3.1 概念说明

表格的线（`table.stroke`、`table.hline`、`table.vline`）听起来简单，实际生成时有两个麻烦：

1. **一根线会被合并单元格打断**。比如一根竖线（vline）若穿过一个 colspan 单元格，在 colspan 范围内**不能画**，必须断成多段。
2. **同一位置可能有两个以上的 stroke 来源**：网格全局 `stroke`、单元格逐边覆盖（`cell.stroke`）、用户显式 `hline`/`vline`。它们要按优先级**合并（fold）**，并决定谁画在谁上面。

Typst 把这件事拆成三层：

- **`StrokePriority`**：三个来源的优先级，`GridStroke < CellStroke < ExplicitLine`。
- **`generate_line_segments`**：一个与方向无关的通用分段器，沿一条轴走过每个 track（轨），把「stroke 与优先级都相同」的连续 track 合成一段，遇到合并单元格或 stroke 变化就断开。
- **`vline_stroke_at_row` / `hline_stroke_at_column`**：针对竖/横线、给定位置的 stroke 解析函数，负责「这里该不该画（会不会穿 colspan/rowspan）」与「画的话 stroke 是什么、优先级是什么」。

#### 4.3.2 核心流程

`render_fills_strokes`（在 `layout()` 末尾调用）为**每个已完成的 region** 重新画一遍线：

1. **竖线**：枚举每个列边界 `x`，以该 region 的「已解析行 `(y, height)` 序列」为 tracks，调 `generate_line_segments(..., vline_stroke_at_row)`，得到若干 `LineSegment`。
2. **横线**：枚举每个行边界 `y`（含底部边框），以列宽为 tracks，调 `generate_line_segments(..., hline_stroke_at_column)`。
3. 把所有段转成 `Geometry::Line(...).stroked(stroke)` 形状，连同 `(thickness, priority)` 收集起来。
4. **按 `(thickness, priority)` 排序**后统一压入 frame——粗线、高优先级画在上面。

`generate_line_segments` 内部对每个 track 调一次 `line_stroke_at_track` 闭包：

- 返回 `None` → 该 track 不画（穿过了合并单元格），当前段被**中断**并 yield。
- 返回 `Some((stroke, priority))` → 若与当前段同 stroke 同优先级，**延伸**当前段；否则**断开**旧段、开启新段。

连续同质的 track 因此被合并成一段，合并单元格处自然断开。

#### 4.3.3 源码精读

**`StrokePriority` 与 `LineSegment`**：

[grid/lines.rs:13-26](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/lines.rs#L13-L26) 定义三档优先级（注释说明每档来源）；[lines.rs:31-44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/lines.rs#L31-L44) 定义一段线的数据：`stroke`、`offset`（自轴起点的偏移）、`length`、`priority`。

**`generate_line_segments`——通用分段器**：

[grid/lines.rs:80-254](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/lines.rs#L80-L254)。核心是 L143-253 的 `std::iter::from_fn` 闭包：

- L149-169：先 fold 用户在该 track 范围内的所有显式 `Line` 的 stroke（后指定的优先）。
- L193-195：调 `line_stroke_at_track` 拿到 `(stroke, priority)` 或 `None`。
- L198-219：若正在构建段且 stroke/优先级相同 → 延伸（`length += size`）；若不同 → 旧段 yield、新段开启。
- L230-235：若返回 `None`（穿合并单元格）→ 当前段 yield 并置空。
- L252：tracks 耗尽时把最后一段 yield。

**`vline_stroke_at_row`——竖线在某行该不该画、用什么 stroke**：

[grid/lines.rs:275-360](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/lines.rs#L275-L360)。两段逻辑：

- L289-302：**colspan 检测**——若 vline 不在左右边界，检查右侧单元格的父格 `parent.x < x`，若是则说明有个 colspan 横穿此处 → 返回 `None`（不画）。
- L304-359：**stroke 合并**——取左单元格的 `stroke.right`、右单元格的 `stroke.left`、用户 vline stroke 三者，用 `fold_or` 合并；优先级按「是否有显式 line → 是否有单元格覆盖 → 否则网格 stroke」决定（L330-336）。

**`hline_stroke_at_column`——横线，多了 rowspan 与表头/表脚的考量**：

[grid/lines.rs:396-558](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/lines.rs#L396-L558)。比 vline 多两点：

- L414-441：**rowspan 检测**，且更精细——即便某 rowspan 理论上穿过这根 hline，若它的起始行在**本 region 不存在**（被删掉的空 auto 行）或落在前一 region，则这根 hline 仍可画（L423-440 用 `local_parent_y` 判断）。
- L447-543：处理**区域顶/底边框**与**表头/表脚上方线条优先级**。例如重复表头下沿的线在跨页时应优先（`top_stroke_comes_from_header`，L505-513），表脚上方的线同理（`bottom_stroke_comes_from_footer`，L516-525）。

**收集与排序**——在 `render_fills_strokes`：

[grid/layouter.rs:559-582](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L559-L582) 把 vline 段转成形状；[layouter.rs:759-806](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L759-L806) 对 hline 做同样的事并收集；最后 [layouter.rs:799-806](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L799-L806) 按 `(thickness, priority)` 排序，注释解释：粗线画在细线之上避免「细 hline 嵌在粗 vline 里」的层叠瑕疵，等粗时 hline 因 stable sort 后压入而盖在 vline 上。

#### 4.3.4 代码实践

这是一个**可运行实践**——`lines.rs` 自带单元测试，直接跑即可观察分段行为。

1. **实践目标**：通过阅读/运行 `lines.rs` 的测试，验证线段在 colspan/rowspan 处的断开与合并。
2. **操作步骤**：
   - 阅读 [lines.rs:644-707](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/lines.rs#L644-L707) 的 `test_vline_splitting_without_gutter`。它构造了一个含多个 colspan 的 4×6 网格（[lines.rs:594-642](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/lines.rs#L594-L642)），对每个列边界 `x` 断言 vline 被切成了哪些段。
   - 在 typst 仓库根目录运行：
     ```bash
     cargo test -p typst-layout --lib grid::lines::test::
     ```
3. **需要观察的现象**：测试通过；特别注意第 3 列边界（`x=2`）的 vline 被 colspan 切成 3 段、第 4 列边界（`x=3`）因全列被 colspan 合并而为空（`vec![]`，[lines.rs:690-692](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/lines.rs#L690-L692)）。
4. **预期结果**：3 个测试（`test_vline_splitting_*` ×2、`test_hline_splitting_*` 系列）全部 pass。若你把某个 `Entry::Merged` 改回 `Entry::Cell`（即取消一个合并），相应测试会失败——这反向印证了「合并单元格让线断开」。
5. 进一步阅读 [lines.rs:1475-1518](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/lines.rs#L1475-L1518) 的 `test_hline_splitting_considers_absent_rows`：它证明当 rowspan 跨及的某行因空 auto 行被移除时，原本被阻挡的 hline 会**重新可画**——呼应 4.3.1 提到的「rowspan 检测要考虑行是否实际存在」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `render_fills_strokes` 要把所有线段先收集进 `lines` 向量、排序后一次性压入 frame，而不是算出一根画一根？

> **参考答案**：两个原因（见 [layouter.rs:482-488](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L482-L488) 与 [layouter.rs:799-806](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L799-L806)）：一是按 `(thickness, priority)` 排序，让粗线/高优先级画在上面，避免层叠瑕疵；二是 `prepend` 每次会把 frame 里已有项整体后移，逐根压入会是二次复杂度，收集后一次性压入是线性。

**练习 2**：vline 检测 colspan 用 `parent.x < x`，hline 检测 rowspan 用 `parent.y < y`，为什么 hline 还要再查 `local_parent_y`？

> **参考答案**：rowspan 可能跨页/跨 region，它的起始行 `parent.y` 可能在前一 region 或是已被删除的空 auto 行，于是「理论上 rowspan 穿过这根 hline」但「本 region 里它实际从 hline 下方才开始」，此时 hline 不应被阻挡。`local_parent_y` 在本 region 实际存在的行里找 rowspan 的首个跨及行（[lines.rs:430-434](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/lines.rs#L430-L434)），只有它仍 `< y` 才真阻挡。vline 不需要这步，因为 colspan 不会跨 region。

**练习 3**：三档 `StrokePriority` 在同位置同时存在时，最终 stroke 由谁决定？优先级字段又影响什么？

> **参考答案**：最终 stroke 由 `fold_or` 合并得到——优先采用更高来源（显式 line > 单元格覆盖 > 网格全局），但若高来源未指定则回落到低来源（见 [lines.rs:352-358](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/lines.rs#L352-L358)）。`priority` 字段不影响 stroke 本身，只影响**等粗时的叠放顺序**（排序的次关键字），以及在 `generate_line_segments` 里**不同优先级的相邻 track 会被强制断成两段**（[lines.rs:201-203](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/lines.rs#L201-L203)）。

---

## 5. 综合实践

把三个子系统串起来，设计这样一个**源码追踪任务**：构造一个「跨页、含两级重复表头、含一个跨行单元格、且开了 gutter 与显式线条」的表格，然后跟踪它的一次排版。

示例代码：

```typst
#set page(height: 3cm)
#show: it => [
  #it
  #it
]

#table(
  columns: 3,
  stroke: 0.5pt,
  gutter: 4pt,
  table.header(
    table.cell(colspan: 2)[组 A], [],
    [项], [子项], [值],
  ),
  table.cell(rowspan: 4)[#rect(width: 100%, height: 100%, fill: aqua)], [1], [a], [10],
  [2], [b], [20],
  [3], [c], [30],
  [4], [d], [40],
)
```

任务步骤：

1. **rowspan 视角**：指出那个 `rowspan: 4` 的单元格。回答：它在哪一行被 `check_for_rowspans` 发现？它的 `heights` 向量在跨页时会有几个元素（即跨几个 region）？它的最后一行排完后，`layout_rowspan` 把帧 `push_frame` 回了哪几个 region？
2. **表头视角**：两个表头行（「组 A」行与「项/子项/值」行）走过了 `upcoming → pending → repeating` 的哪几步？在新 region 顶部，`layout_active_headers` 给它们分配的 `disambiguator` 是多少？`unbreakable_rows_left` 被充能为几，从而保证它们不被截断？
3. **线条视角**：竖线遇到「组 A」这个 colspan 单元格时，被 `vline_stroke_at_row` 判定为 `None` 而断开——在 [lines.rs:289-301](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/lines.rs#L289-L301) 处确认这个判断；横线遇到 rowspan 单元格时又如何？跨页后，重复表头下沿的横线为何优先级更高（参 [lines.rs:505-513](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/lines.rs#L505-L513)）？

完成后再用 typst CLI 编译它，对照 PDF（或 `typst compile --format svg`）核对你的推演是否与实际渲染一致。这是检验你是否真正理解三个子系统的最佳方式。

## 6. 本讲小结

- **rowspan 的本质是「延迟」**：在起始行 `check_for_rowspans` 登记，在 `finish_region` 里逐 region 把跨及行高度累积进 `heights`，直到最后一行解析完才用这些高度构造多区域 pod 回填排版（`layout_rowspan`）。`max_resolved_row` 防止重复表头二次计入高度。
- **`unbreakable_rows_left` 是 rowspan 与表头共用的「焊死」机制**：充能后在 `layout_row_internal` 里禁止换 region，排完一行减一，从而把不可断裂行组与表头行绑在同一 region。
- **表头是三向量状态机**：`upcoming_headers`（未遇）→ `pending_headers`（首次排出、待确认非孤儿）→ `repeating_headers`（确认、每 region 重复）。`place_new_headers` 处理多级累积与冲突踢除，`layout_active_headers` 在新 region 顶部重排。
- **孤儿行防护** 用 `lrows_orphan_snapshot` 实现：表头后排到非表头行才算「脱孤」并升格 pending→repeating，否则撤回重排。
- **线条生成分三层**：`StrokePriority`（Grid < Cell < Explicit）定来源优先级；`generate_line_segments` 把同质连续 track 合段、遇合并单元格（返回 `None`）或 stroke 变化则断；`vline/hline_stroke_at_*` 负责 colspan/rowspan 穿透检测与三来源 stroke 合并。
- **跨页一致性**：线条在每个 region 用该 region 实际存在的行重画，hline 的 rowspan 检测会考虑「行是否实际存在于本 region」；重复表头/表脚的下沿/上沿线在跨页时获得更高优先级。

## 7. 下一步学习建议

本讲补齐了 `GridLayouter` 的高级机制，至此 u6 单元关于网格/表格的部分已完整。建议的后续学习路径：

- **横向对比另一类「容器型」layouter**：阅读 `src/stack.rs`（u6-l5）和 `src/lists.rs`（u6-l6），看它们如何复用「逐 region 累积 + 终结」的骨架，又如何省略 rowspan/表头这类复杂度。
- **回到 `layout_cell` 的 HACK 注入 tag**：结合 u2-l3（Frame/Fragment 的 group/parent）与 u2-l4（Locator/Tag），重新理解本讲里 `disambiguator`、`layout_cell` 的 tag 注入为何能保证跨页 rowspan 与重复表头的内省顺序正确。
- **追踪一个真实回归**：在 typst 仓库 `git log -- src/grid/` 里找一条近期修复表格的 commit，用本讲建立的心智模型去解读它改的是 rowspan、表头还是线条，验证你的理解。
- 若要继续深挖 rowspan 的 auto 行模拟，可专门精读 [rowspans.rs:829-1002](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/rowspans.rs#L829-L1002) 的迭代逼近算法，并手算其注释里的 8pt 例子。
