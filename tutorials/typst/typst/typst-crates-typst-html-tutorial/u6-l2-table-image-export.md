# 表格与图片导出

## 1. 本讲目标

typst-html 把 Typst 的 `table` 和 `image` 翻译成浏览器能直接渲染的 `<table>` 与 `<img>`。本讲聚焦 [`src/rules.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs) 中的两条 show 规则——`TABLE_RULE` 与 `IMAGE_RULE`，以及它们调用的助手函数。

学完本讲后，你应当能够：

- 说清 typst-html **不重新解析表格**，而是接手 typst-library 已排版好的 `CellGrid` IR，并把它切分成 `<thead>`/`<tbody>`/`<tfoot>`。
- 解释「连续表头归入 `<thead>`、其余表头降级为 `<tbody>` 里的 `<th>` 行」这条规则是如何用 `take_while` 实现的。
- 追踪 `colspan`/`rowspan` 如何从 `TableCell` 流向 HTML 属性，以及为何值为 `1` 时会被省略。
- 说明图片如何被归一为 `WebImage`、再编码成 `data:` base64 URL 嵌入 `<img>`，以及 `width`/`height`/`image-rendering` 的来源。

本讲承接 u3-l5（内建 show 规则注册机制），默认你已经熟悉 `ShowFn`、`NativeRuleMap`、`register()` 与「内建规则是兜底」的语义。

## 2. 前置知识

- **show 规则与 ShowFn**：每条 `XXX_RULE` 是一个 `ShowFn<T>` 函数指针，接收元素、`Engine`、样式链，返回 `Content`。详见 u3-l5。
- **CellGrid IR**：typst-library 在布局前把 `table`/`grid` 解析成一张统一的单元格网格 `CellGrid`，包含 `entries`（扁平的单元格数组）、`headers`、`footer`、是否有 `gutter` 等信息。**分页导出与 HTML 导出共享同一份 `CellGrid`**，typst-html 只负责把它「再编码」成 HTML。
- **gutter（间隙）坐标**：当表格设置了 `gutter` 时，`CellGrid` 会把间隙也作为「行/列轨道」插入，于是真实内容行只出现在偶数下标（0、2、4…），奇数下标是 gutter。这一点直接影响后文的坐标换算。
- **data URL**：形如 `data:{mime};base64,{数据}` 的 URL，把资源内容直接内联进字符串，无需外部文件。HTML 导出默认不产出独立资源文件，图片因此全部走 data URL。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-html/src/rules.rs` | 本讲主战场：`TABLE_RULE`、`show_cellgrid`、`show_cell`、`IMAGE_RULE` 全在此 |
| `crates/typst-library/src/layout/grid/resolve.rs` | 定义 `CellGrid`、`Cell`、`Entry`、`Header`、`Footer` 等 IR 类型 |
| `crates/typst-library/src/model/table.rs` | 定义用户侧的 `TableCell` 元素及其 `colspan`/`rowspan` 字段 |
| `crates/typst-svg/src/image.rs` | 定义 `WebImage`、`WebImage::new`、`to_base64_url`、`convert_image_scaling`——HTML 与 SVG 导出共用 |
| `tests/suite/layout/grid/html.typ` | 表格导出 HTML 的端到端测试，本讲综合实践的素材 |

## 4. 核心概念与源码讲解

### 4.1 表格导出的整体思路：接手 CellGrid，而非重新解析

#### 4.1.1 概念说明

typst-html 处理表格时有一个关键设计取舍：**它不读用户写的 `table(...)` 源码，而是消费 typst-library 已经算好的 `CellGrid`**。换句话说，单元格的自动定位、`colspan`/`rowspan` 展开成 `Entry::Merged`、表头/页脚的范围计算，这些复杂工作都已经在 `CellGrid` 构建阶段（`grid_to_cellgrid`/`table_to_cellgrid`）完成。typst-html 只需把这张「已经摆好的网格」翻译成 HTML 的行与列。

这意味着 `TABLE_RULE` 本身极其单薄——它的全部职责就是取出 `TableElem` 身上已经算好的 `grid` 字段，转交给 `show_cellgrid`：

```rust
const TABLE_RULE: ShowFn<TableElem> = |elem, _, styles| {
    let grid = elem.grid.as_ref().unwrap();
    Ok(show_cellgrid(grid, styles, elem.span()))
};
```

[`src/rules.rs:573-576`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L573-L576) —— `TABLE_RULE` 仅做「取 grid + 转发」。

`TABLE_RULE` 在 `register()` 中与 `TABLE_CELL_RULE` 一起注册到 `Target::Html`：

```rust
rules.register(Html, TABLE_RULE);
rules.register(Html, TABLE_CELL_RULE);
```

[`src/rules.rs:68-69`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L68-L69) 注册表格两条规则。

#### 4.1.2 核心流程

`CellGrid` 的关键字段（来自 typst-library）：

- `entries: Vec<Entry>` —— 把整张表按行优先**扁平化**存的单元格数组；`Entry::Cell(Cell)` 是真实单元格，`Entry::Merged { parent }` 是被合并占位的位置。
- `headers: Vec<Repeatable<Header>>` —— 表头列表，每个 `Header` 含一个行范围 `range: Range<usize>` 和层级 `level`。
- `footer: Option<Repeatable<Footer>>` —— 至多一个页脚，`Footer` 含 `start`/`end` 行范围。
- `has_gutter: bool` —— 是否有 gutter，决定坐标是否需要「除以 2」换算。

`show_cellgrid` 的整体步骤：

1. 用 `chunks(non_gutter_column_count())` 把扁平 `entries` 切成一行一行的切片。
2. 先从尾部 `drain` 出 **footer** 行，包成 `<tfoot>`。
3. 再用 `take_while` 找出**开头连续的表头**，包成 `<thead>`。
4. 剩余行作为 `<tbody>`（或裸行），其中落在「非连续表头」范围内的行用 `<th>`，其余用 `<td>`。
5. 按 `thead → tbody → tfoot` 顺序拼进 `<table>`。

#### 4.1.3 源码精读

先看 `CellGrid` 与 `Entry` 的定义，建立数据直觉：

```rust
pub enum Entry {
    Cell(Cell),
    Merged { parent: usize },
}

impl Entry {
    pub fn as_cell(&self) -> Option<&Cell> {
        match self {
            Self::Cell(cell) => Some(cell),
            Self::Merged { .. } => None,
        }
    }
}
```

[`crates/typst-library/src/layout/grid/resolve.rs:628-647`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L628-L647) —— `Entry` 区分真实单元格与合并占位；`as_cell()` 对合并位置返回 `None`，后文 `tr` 闭包正是靠它**过滤掉被合并的格子**。

`Cell` 自带 `colspan`/`rowspan`（均为 `NonZeroUsize`）：

```rust
pub struct Cell {
    pub body: Content,
    pub fill: Option<Paint>,
    pub colspan: NonZeroUsize,
    pub rowspan: NonZeroUsize,
    // ...
}
```

[`crates/typst-library/src/layout/grid/resolve.rs:571-580`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L571-L580) —— 单元格的跨度信息已在 IR 阶段算好。

再看 `show_cellgrid` 的骨架（行切分与 `tr` 闭包）：

```rust
fn show_cellgrid(grid: &CellGrid, styles: StyleChain, span: Span) -> Content {
    let elem = |tag, body| HtmlElem::new(tag).with_body(Some(body)).pack().spanned(span);
    let mut rows: Vec<_> = grid.entries.chunks(grid.non_gutter_column_count()).collect();

    let tr = |tag, row: &[Entry]| {
        let row = row
            .iter()
            .filter_map(|entry| entry.as_cell())   // 丢掉 Merged 占位
            .map(|cell| show_cell(tag, cell, styles));
        elem(tag::tr, Content::sequence(row))
    };
    // ... 后续切分 thead / tbody / tfoot
}
```

[`src/rules.rs:578-588`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L578-L588) —— `chunks` 切行、`filter_map(as_cell)` 滤合并、`tr` 把一行格子包进 `<tr>`。

> 注意 `tr` 闭包的 `tag` 参数：同一个闭包既用来生成 `<th>` 行（表头）也用来生成 `<td>` 行（数据），区别只在外层传入的标签。

#### 4.1.4 代码实践

**实践目标**：确认 typst-html 确实依赖「已算好的 CellGrid」而非自行解析。

**操作步骤**：

1. 在 [`src/rules.rs:573`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L573) 处看到 `TABLE_RULE` 直接读 `elem.grid.as_ref().unwrap()`。
2. 用编辑器全局搜索 `elem.grid` 的写入点，会发现它由 typst-library 的 `TableElem::layout_marked`（在 realize 阶段）调用 `table_to_cellgrid` 填充。
3. 打开 [`crates/typst-library/src/layout/grid/resolve.rs:78-127`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L78-L127) 的 `table_to_cellgrid`，确认单元格定位、合并、表头范围都在这里完成。

**需要观察的现象**：`TABLE_RULE` 函数体里**没有任何**对 `table.columns`、`table.cell(...)` 位置的解析逻辑——它只读一个已经算好的 `grid`。

**预期结果**：你会清楚地看到「IR 构造」与「HTML 编码」是两个完全分离的阶段，typst-html 只负责后者。这一结论无需运行即可从源码确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TABLE_RULE` 用 `elem.grid.as_ref().unwrap()` 而不是 `expect` 或提前 `match`？这个 `grid` 字段在什么时刻才会变成 `Some`？

> **参考答案**：`grid` 是 `TableElem` 在 realize/layout 阶段由 `table_to_cellgrid` 填充的「缓存式」字段。show 规则触发时，元素必然已经走过 realize，因此 `grid` 一定是 `Some`，用 `unwrap()` 在此处是安全的内部不变式（invariant）。若在 realize 之前访问才会是 `None`，但 show 规则不会在那阶段运行。

**练习 2**：`grid.entries.chunks(grid.non_gutter_column_count())` 为什么用 `non_gutter_column_count()` 而不是 `grid.cols.len()`？

> **参考答案**：当存在 gutter 时，`grid.cols` 里夹着 gutter 列（长度大于真实列数），而 `entries` 数组**只存真实单元格、不含 gutter**。`non_gutter_column_count()` 返回不含 gutter 的真实列数，用它做 `chunks` 大小才能正确按行切分。

---

### 4.2 行分组：thead / tbody / tfoot 与表头范围换算

#### 4.2.1 概念说明

HTML 表格用三个行组容器表达语义：`<thead>`（表头）、`<tbody>`（主体）、`<tfoot>`（页脚）。浏览器允许它们各自由若干 `<tr>` 组成。

Typst 的表头模型比 HTML 更灵活：用户可以写**多个 `table.header(...)`**，它们可以出现在表格**任意位置**（不限于开头），还能带 `level` 与 `repeat`。typst-html 需要把这套模型**降级映射**到 HTML 的三段结构，规则是：

- **从表格开头起、行号连续**的若干表头 → 合并进 `<thead>`。
- 中间或断开的表头 → 不进 `<thead>`，而是在 `<tbody>` 里作为 `<th>` 行出现（HTML 允许 `<tbody>` 中混用 `<th>`/`<td>`）。
- 页脚 → `<tfoot>`（当前实现只取末尾的单一 footer）。

#### 4.2.2 核心流程

坐标换算是这里最容易出错的地方。当 `has_gutter` 为真时，`CellGrid` 内部的行号是「物理行号」（含 gutter），而 `Header.range`/`Footer.start` 存的是**逻辑行号**。换算关系为：

\[
\text{物理下标} = 2 \times \text{逻辑下标}
\]

因此把一个逻辑范围 `[s, e)` 转回逻辑坐标时：

\[
\text{逻辑起点} = \lfloor s / 2 \rfloor,\qquad \text{逻辑终点} = \lceil e / 2 \rceil
\]

终点用 `div_ceil`（向上取整）而非 `/`，是因为终点可能恰好落在某条 gutter 行上（`2*行数 - 1` 的情况），向上取整能正确把它归入上一组。

`show_cellgrid` 的切分顺序很讲究：**先 footer、再 header**，因为两者都从 `rows` 这个可变向量里 `drain`（取走）行：

1. **footer**：`rows.drain(footer_start..)` 取走末尾所有行，包成 `<tfoot>`（用 `<td>`）。
2. **连续表头**：用 `take_while` 数出从第 0 行开始连续的表头数量 `first_mid_table_header`，`drain(..removed_header_rows)` 取走这些行，包成 `<thead>`（用 `<th>`）。
3. **剩余行**：留在 `rows` 里的作为 tbody；逐行判断是否落在某个「非连续表头」范围内，是则用 `<th>`，否则用 `<td>`。
4. **tbody 包裹条件**：只有当确实产出了 `<thead>` 或 `<tfoot>` 时，才把主体包进 `<tbody>`（否则主体直接作为 `<table>` 的子行，HTML 允许省略 `<tbody>`）。

#### 4.2.3 源码精读

**footer 切分**（注意 `div_ceil`）：

```rust
let footer = grid.footer.as_ref().map(|ft| {
    let footer_start = if grid.has_gutter { ft.start.div_ceil(2) } else { ft.start };
    let rows = rows.drain(footer_start..);
    elem(tag::tfoot, Content::sequence(rows.map(|row| tr(tag::td, row))))
});
```

[`src/rules.rs:592-599`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L592-L599) —— 从尾部取走 footer 行；注释里的 TODO 提示未来要支持多个 subfooter。

**header 范围换算函数**：

```rust
let header_range = |hd: &Header| {
    if grid.has_gutter {
        hd.range.start / 2..hd.range.end.div_ceil(2)
    } else {
        hd.range.clone()
    }
};
```

[`src/rules.rs:602-610`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L602-L610) —— 起点地板除、终点向上取整，对应上文公式。

**连续表头判定**（本节的核心算法）：

```rust
let mut consecutive_header_end = 0;
let first_mid_table_header = grid
    .headers
    .iter()
    .take_while(|hd| {
        let range = header_range(hd);
        let is_consecutive = range.start == consecutive_header_end;
        consecutive_header_end = range.end;
        is_consecutive
    })
    .count();
```

[`src/rules.rs:614-624`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L614-L624) —— `take_while` 逐个验收表头：只有当某表头的起点恰好等于「前一段连续表头的终点」时才算连续；一旦出现空隙（`range.start != consecutive_header_end`）就停止。`count()` 得到进入 `<thead>` 的表头个数。

注意 `take_while` 的副作用：即便某个表头不连续、使谓词返回 `false`，`consecutive_header_end` 也已经被更新成它的 `range.end`——但因为 `take_while` 已停止，这个值不再使用，逻辑无副作用隐患。

**thead 取行与 y_offset**：

```rust
let (y_offset, header) = if first_mid_table_header > 0 {
    let removed_header_rows =
        header_range(&grid.headers[first_mid_table_header - 1]).end;
    let rows = rows.drain(..removed_header_rows);
    (
        removed_header_rows,
        Some(elem(tag::thead, Content::sequence(rows.map(|row| tr(tag::th, row))))),
    )
} else {
    (0, None)
};
```

[`src/rules.rs:626-637`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L626-L637) —— 取走开头 `removed_header_rows` 行作为 thead；`y_offset` 记录「 tbody 的第 0 行在原表里的真实行号」，供下一步判定剩余表头时使用。

**tbody 中剩余表头降级为 th 行**：

```rust
let mut next_header = first_mid_table_header;
let mut body = Content::sequence(rows.into_iter().enumerate().map(|(relative_y, row)| {
    let y = relative_y + y_offset;
    if let Some(current_header_range) =
        grid.headers.get(next_header).map(|h| header_range(h))
        && current_header_range.contains(&y)
    {
        if y + 1 == current_header_range.end {
            next_header += 1;
        }
        tr(tag::th, row)
    } else {
        tr(tag::td, row)
    }
}));
```

[`src/rules.rs:643-659`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L643-L659) —— 用 `next_header` 游标顺序扫描剩余表头：某行若落在当前表头范围内就用 `<th>`，否则 `<td>`；扫完一个表头就推进游标。这处理了「表头出现在表格中间」的降级情形。

**tbody 包裹与最终拼装**：

```rust
if header.is_some() || footer.is_some() {
    body = elem(tag::tbody, body);
}

let content = header.into_iter().chain(core::iter::once(body)).chain(footer);
BlockElem::packed(elem(tag::table, Content::sequence(content)))
```

[`src/rules.rs:661-666`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L661-L666) —— 只在有 thead/tfoot 时才显式包 tbody；最终按 `thead? → tbody → tfoot?` 顺序串联进 `<table>`。注意 HTML 规范里 `<tfoot>` 本应放在 `<tbody>` 之前，但现代浏览器（HTML5）允许 `<tfoot>` 在 `<tbody>` 之后，typst-html 选择了语义上更直观的「头-体-脚」顺序。

#### 4.2.4 代码实践

**实践目标**：用一个真实测试用例验证「连续表头进 thead、中间表头降级为 th」。

**操作步骤**：

1. 打开测试 [`tests/suite/layout/grid/html.typ:138-183`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/tests/suite/layout/grid/html.typ#L138-L183) 的 `multi-header-inside-table` 用例。它在表格**中间**插入了 `table.header(level: 2)` 与 `level: 3`，最后还有一个 `repeat: false, level: 4` 的表头。
2. 手动追踪 `show_cellgrid`：开头四个连续表头（`First/Second/Level2/Level3`）应进入 `<thead>`；中间的 `Level 2/Level 3` 因被正文行隔断，不连续，应作为 `<tbody>` 内的 `<th>` 行出现。
3. （可选，待本地验证）在仓库根目录运行该测试的 HTML 快照：`cargo test -p typst --test suites -- html` 观察生成的 `<thead>`/`<tbody>` 结构是否与你的推断一致。

**需要观察的现象**：`<thead>` 只包含开头连续的表头行；中间表头行的标签是 `<th>` 但它们身处 `<tbody>` 内。

**预期结果**：`first_mid_table_header` 恰好等于「开头连续表头的个数」，中间表头由 4.2.3 最后一段的 `next_header` 游标逻辑降级处理。若不便运行，可根据 `take_while` 与游标逻辑从源码层面确认结论。

#### 4.2.5 小练习与答案

**练习 1**：假如一个表格没有表头也没有页脚，只有普通数据行，最终 HTML 里会出现 `<tbody>` 标签吗？

> **参考答案**：不会。代码在 [`src/rules.rs:661-663`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L661-L663) 明确判断 `header.is_some() || footer.is_some()`，两者皆无时 body 不被包进 `<tbody>`，`<tr>` 行直接挂在 `<table>` 下（HTML 允许这种省略，浏览器会隐式插入 tbody）。

**练习 2**：为什么 `header_range` 对起点用 `/ 2`（地板除），对终点却用 `div_ceil(2)`（向上取整）？

> **参考答案**：在 gutter 坐标系下，逻辑行 `r` 对应物理行 `2r`。一个表头范围 `[2s, 2e)` 的起点 `2s` 除以 2 精确还原 `s`；但终点可能因为表头紧贴表格末尾而被 `finalize_headers_and_footers` 扩展到 `2e - 1`（含末尾 gutter 行），此时 `(2e-1)/2` 地板除会错误地缩成 `e-1`，丢失最后一行。用 `div_ceil` 能把 `2e-1` 正确归入 `e`，保证不漏行。

---

### 4.3 单元格：show_cell 与 colspan/rowspan

#### 4.3.1 概念说明

「行」层面的结构由 4.2 处理，「格」层面则交给 `show_cell`：它把一个 `Cell` 翻译成 `<td>` 或 `<th>` 元素，并把跨格信息写成 HTML 的 `colspan`/`rowspan` 属性。

HTML 的 `colspan`/`rowspan` 语义与 Typst 完全对应：一个 `colspan="2"` 的单元格在视觉上占据两列，于是它右边的那个「被占据」的格子就**不应该再产出 HTML 节点**——这正是 4.1.3 里 `filter_map(|entry| entry.as_cell())` 过滤 `Entry::Merged` 的原因。两端配合：`CellGrid` 用 `Merged` 占位表示「这里被上左方的单元格吃掉了」，HTML 端用 `colspan`/`rowspan` 告诉浏览器「这个单元格向右/向下吃了几格」，被吃的位置则直接不输出。

#### 4.3.2 核心流程

`show_cell` 的逻辑很短：

1. 取出 `cell.body`（一个 `Content`），尝试解包成 `TableCell`；若不是 `TableCell`（例如 `grid.cell`），原样返回该 Content。
2. 构造一个 `span` 辅助函数：`|n: NonZeroUsize| (n != NonZeroUsize::MIN).then(|| n.to_string())`。它把跨度值转成字符串，但**当且仅当值为 1 时返回 `None`**。
3. 分别对 `colspan`、`rowspan` 调用 `span`：得到 `Some` 才 `push` 对应属性。
4. 用传入的 `tag`（`td` 或 `th`）新建 `HtmlElem`，挂上属性与 body。

#### 4.3.3 源码精读

```rust
fn show_cell(tag: HtmlTag, cell: &Cell, styles: StyleChain) -> Content {
    let cell = cell.body.clone();
    let Some(cell) = cell.to_packed::<TableCell>() else { return cell };
    let mut attrs = HtmlAttrs::new();
    let span = |n: NonZeroUsize| (n != NonZeroUsize::MIN).then(|| n.to_string());
    if let Some(colspan) = span(cell.colspan.get(styles)) {
        attrs.push(attr::colspan, colspan);
    }
    if let Some(rowspan) = span(cell.rowspan.get(styles)) {
        attrs.push(attr::rowspan, rowspan);
    }
    HtmlElem::new(tag)
        .with_body(Some(cell.clone().pack()))
        .with_attrs(attrs)
        .pack()
        .spanned(cell.span())
}
```

[`src/rules.rs:669-685`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L669-L685) —— `show_cell` 的全部实现。

关键细节：

- `NonZeroUsize::MIN` 就是 `1`。`(n != 1).then(...)` 这一手避免了在每个普通单元格上输出冗余的 `colspan="1"`——HTML 的默认值本就是 1。
- `cell.colspan.get(styles)` 读的是 `TableCell` 元素上的字段，其定义在 typst-library：

```rust
/// The amount of columns spanned by this cell.
#[default(NonZeroUsize::ONE)]
pub colspan: NonZeroUsize,

/// The amount of rows spanned by this cell.
#[default(NonZeroUsize::ONE)]
pub rowspan: NonZeroUsize,
```

[`crates/typst-library/src/model/table.rs:746-752`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/table.rs#L746-L752) —— 默认值都是 `1`，与「省略属性」的语义吻合。

> 另有一条配套规则 `TABLE_CELL_RULE`：

```rust
const TABLE_CELL_RULE: ShowFn<TableCell> = |elem, _, _| Ok(elem.body.clone());
```

[`src/rules.rs:687`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L687) —— 当 realize 递归到 `TableCell` 本身时，只取它的 body（裸内容），不再包标签。这与 4.2 里「`tr` 直接产出 `<td>`/`<th>` 而非 `<cell>`」配合：单元格的外壳标签由 `show_cell` 决定，`TABLE_CELL_RULE` 只负责把单元格「透明解包」成它包含的内容。

#### 4.3.4 代码实践

**实践目标**：追踪 `colspan`/`rowspan` 从用户代码到 HTML 属性的完整路径。

**操作步骤**：

1. 在 [`tests/suite/layout/grid/html.typ:1-32`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/tests/suite/layout/grid/html.typ#L1-L32) 的 `basic-table` 用例中定位两行：
   - 第 16 行 `table.cell(x: 1, rowspan: 2)[Baz]` —— 一个 rowspan。
   - 第 24 行 `table.cell(colspan: 2)[3]` —— 一个 colspan。
2. 沿调用链追踪 `rowspan: 2`：用户参数 → `TableCell.rowspan` 字段（[`table.rs:752`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/table.rs#L752)）→ `CellGrid` 解析时把 `(x=1,y=2)` 与 `(x=1,y=3)` 两个位置分别填成 `Entry::Cell(Baz)` 与 `Entry::Merged{parent: Baz}`（见 [`resolve.rs:1509-1534`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L1509-L1534)）→ HTML 端 `tr` 过滤掉 Merged、`show_cell` 给 Baz 输出 `rowspan="2"`。
3. 同理追踪 `colspan: 2`：`[3]` 占据 `(x=0,y=4)` 与 `(x=1,y=4)`，后者变 Merged 被过滤，`[3]` 带上 `colspan="2"`。

**需要观察的现象**：Baz 所在行的 `<tr>` 有 3 个子格（Foo、Baz、Bar），而下一行 `<tr>` 只有 2 个子格（1、2）——因为 Baz 的 rowspan 已「吃掉」了下方那一格，Merged 占位被 `as_cell()` 过滤。

**预期结果**：最终 HTML 中 `<td rowspan="2">Baz</td>` 与 `<td colspan="2">3</td>` 正确出现，且被合并的位置不产生多余 `<td>`。若运行快照测试可对照确认；不运行时，可从 `filter_map(as_cell)` 与 `span` 闭包两条逻辑交叉验证。

#### 4.3.5 小练习与答案

**练习 1**：若用户写 `table.cell(colspan: 1)[X]`，生成的 HTML 会有 `colspan` 属性吗？为什么？

> **参考答案**：不会。`span` 闭包用 `n != NonZeroUsize::MIN`（即 `n != 1`）作为条件，colspan 为 1 时返回 `None`，属性不被 push。这避免了冗余的 `colspan="1"`，因为 HTML 的默认行为就是跨 1 列。

**练习 2**：为什么需要单独的 `TABLE_CELL_RULE`？没有它会怎样？

> **参考答案**：`show_cell` 已经用 `<td>`/`<th>` 包好了单元格，但 realize 在递归处理单元格**内部内容**时还会再次碰到 `TableCell` 元素本身。`TABLE_CELL_RULE` 把这种情况「透明化」——直接返回 `elem.body`，避免单元格被套上多余标签或被重复处理。没有它，单元格内容可能在递归 show 时产生非预期的嵌套结构。

---

### 4.4 图片导出：IMAGE_RULE 与属性装配

#### 4.4.1 概念说明

`image` 元素的 HTML 化由 `IMAGE_RULE` 负责。和表格类似，它也不重新解码图片文件，而是调用 `elem.decode(engine, styles)` 拿到一个已解码的 `Image` 对象，然后把它装配成一个 `<img>` 元素。

`<img>` 是 HTML 的**空元素**（void element，自闭合），其全部信息都在属性上。`IMAGE_RULE` 需要填充四类属性/样式：

| 来源 | HTML 产物 | 说明 |
| --- | --- | --- |
| 图片二进制 | `src="data:..."` | base64 data URL，详见 4.5 |
| `image.alt` | `alt="..."` | 无障碍替代文本，可选 |
| 像素宽高 | `width`/`height` | 整数，供浏览器**预留空间** |
| 缩放策略 + 用户尺寸 | CSS `image-rendering`/`width`/`height` | 控制渲染方式与最终显示尺寸 |

#### 4.4.2 核心流程

1. `elem.decode(engine, styles)?` 得到 `Image`。
2. `WebImage::new(&image).to_base64_url()` 生成 data URL，写入 `src`。
3. 若用户设了 `alt`，写入 `alt`。
4. 把 `image.width()`/`image.height()`（浮点像素数）四舍五入并饱和转换为 `i64`，写入 HTML 的 `width`/`height` 属性。
5. 构造 CSS `Properties`：
   - 若图片有非默认的缩放策略，写入 `image-rendering`。
   - 若用户显式设了 `image.width`，写入 CSS `width`。
   - 若用户显式设了 `image.height`（且为 `Rel`），写入 CSS `height`。
6. 用 `tag::img` 装配成 `HtmlElem`，包进 `BlockElem`（图片是块级）。

注意 HTML 的 `width`/`height` **属性**（整数，用于占位）与 CSS 的 `width`/`height` **样式**（可带单位，用于最终渲染尺寸）是两套独立机制，`IMAGE_RULE` 同时使用二者。

#### 4.4.3 源码精读

```rust
const IMAGE_RULE: ShowFn<ImageElem> = |elem, engine, styles| {
    let image = elem.decode(engine, styles)?;

    let mut attrs = HtmlAttrs::new();
    let src = typst_svg::WebImage::new(&image).to_base64_url();
    attrs.push(attr::src, src);

    if let Some(alt) = elem.alt.get_cloned(styles) {
        attrs.push(attr::alt, alt);
    }

    // width/height 属性：整数，仅供浏览器预留空间
    let cast = |v: f64| eco_format!("{}", v.round().saturating_as::<i64>());
    attrs.push(attr::width, cast(image.width()));
    attrs.push(attr::height, cast(image.height()));

    let mut css = css::Properties::build((engine, elem.span()));

    if let Some(value) = typst_svg::convert_image_scaling(image.scaling()) {
        css.push("image-rendering", value);
    }

    match elem.width.get(styles) {
        Smart::Auto => {}
        Smart::Custom(rel) => css.push("width", rel),
    }

    match elem.height.get(styles) {
        Sizing::Auto => {}
        Sizing::Rel(rel) => css.push("height", rel),
        Sizing::Fr(_) => {}
    }

    Ok(BlockElem::packed(
        HtmlElem::new(tag::img)
            .with_attrs(attrs)
            .with_css(css.finish())
            .pack()
            .spanned(elem.span()),
    ))
};
```

[`src/rules.rs:774-820`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L774-L820) —— `IMAGE_RULE` 全貌。

几个要点：

- `cast` 闭包对像素尺寸做 `round()` 再 `saturating_as::<i64>()`：HTML 的 `width`/`height` 属性按规范应为整数，浮点尺寸四舍五入「好过没有」，且不会破坏长宽比（注释见 [`src/rules.rs:785-788`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L785-L788)）。`saturating_as` 防止极大/极小值溢出。
- `image.scaling()` 返回 `Smart<ImageScaling>`，由 `convert_image_scaling`（见 4.5）映射成 CSS 字符串；`Auto` 时返回 `None`，不写属性。
- 用户尺寸只处理 `Rel`（相对长度，如 `50%`、`200pt`）；`Sizing::Fr`（弹性比例，仅用于 grid 布局）在图片场景无意义，被显式忽略。
- `css::Properties::build((engine, elem.span()))` 里的 `(engine, span)` 是 `WarningSink`，用于在某个 CSS 值无法序列化时发警告而非整体失败（参见 u4-l4）。

`<img>` 标签来自 `tag::img`，`is_void(tag::img)` 为真，编码阶段它会以自闭合形式输出（参见 u5-l1）。

#### 4.4.4 代码实践

**实践目标**：区分 HTML 的 `width`/`height` **属性**与 CSS **样式**两套机制。

**操作步骤**：

1. 阅读 [`src/rules.rs:785-811`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L785-L811)，对照两组写入：`attrs.push(attr::width, ...)`（HTML 属性，整数）vs `css.push("width", rel)`（CSS 样式，带单位）。
2. 构造一个反例：假设用户写 `#image("photo.png", width: 50%)`。问自己：`width` 属性会是什么值？CSS `width` 会是什么值？
3. 推断：`width` 属性来自 `image.width()`（**图片原始像素宽**，与用户设的 50% 无关），CSS `width` 来自 `elem.width`（`50%`）。

**需要观察的现象**：HTML 属性反映**原始像素尺寸**，CSS 样式反映**用户期望的显示尺寸**。两者独立。

**预期结果**：浏览器先用 HTML 属性预留固定空间（避免加载时布局抖动），再用 CSS 把最终渲染尺寸缩放到 50%。这一推断可直接从源码两条互不干扰的写入路径得出，无需运行（待本地验证：可用 typst 导出一张图后查看 `<img>` 的属性与 style）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Sizing::Fr(_)` 被显式 `match` 却什么都不做？

> **参考答案**：`Fr`（fractional，弹性比例）是 grid/flex 布局专用概念，表示「按比例瓜分剩余空间」。对一张独立图片而言它没有良定义的物理尺寸含义，强行转成 CSS `height` 没有意义，因此忽略。

**练习 2**：`cast` 闭包为什么用 `saturating_as::<i64>()` 而非普通的 `as i64`？

> **参考答案**：`as i64` 在浮点值超出 `i64` 表示范围时行为是「饱和到边界或产生垃圾值」（Rust 旧版本甚至可能给出未定义的溢出结果），而 `saturating_as`（来自 `az` crate）保证溢出时安全地钳制到 `i64::MAX`/`MIN`。虽然真实图片像素尺寸极少溢出，但这是防御性编程。

---

### 4.5 WebImage：跨格式归一与 base64 data URL

#### 4.5.1 概念说明

`IMAGE_RULE` 把「把图片变成可在网页里引用的字节串」这件复杂的事**委托**给了 typst-svg 的 `WebImage`。这是一个重要的复用点：**HTML 导出与 SVG 导出共享同一个 `WebImage`**（SVG 的 `render_image` 同样调用 `WebImage::new(image).to_base64_url()`，见 [`crates/typst-svg/src/image.rs:27`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-svg/src/image.rs#L27)）。

`WebImage` 解决两个问题：

1. **格式归一**：Typst 内部支持的光栅图有「交换格式」（PNG/JPG/GIF/WEBP，已有编码字节）与「像素格式」（裸像素数组，无文件编码）两种；此外还支持 SVG 与 PDF 矢量图。网页端只认少数几种格式，`WebImage::new` 把所有种类统一成 `WebImageFormat`（Png/Jpg/Gif/Webp/Svg）+ 字节数据。
2. **内联引用**：`to_base64_url` 把字节编码成 `data:{mime};base64,...`，让 `<img src>` 无需外部文件。

PDF 图片是个有趣特例：网页不直接支持 PDF，`WebImage` 会用 `hayro_svg` 把 PDF 页**转成 SVG** 再内联。

#### 4.5.2 核心流程

`WebImage::new(image)` 按图片类型分派：

```
ImageKind::Raster
├─ Exchange(Png/Jpg/Gif/Webp)  → 直接复用原字节，记录格式
└─ Pixel(_)                    → 用 PngEncoder 重新编码成 PNG 字节
ImageKind::Svg                  → 直接复用 SVG 字节
ImageKind::Pdf                  → pdf_to_svg() 转成 SVG 字节
```

`to_base64_url()` 则：

1. 拼前缀 `data:{mime};base64,`，其中 `mime` 由 `WebImageFormat::mime()` 给出（如 `image/png`）。
2. 用 `base64::engine::general_purpose::STANDARD` 对 `self.data` 编码。
3. 拼接返回 `EcoString`。

两个函数都标注了 `#[comemo::memoize]`：同一张图片重复出现时不会重复转码/编码。

#### 4.5.3 源码精读

`WebImage` 与格式枚举：

```rust
pub struct WebImage {
    pub format: WebImageFormat,
    pub data: Bytes,
}

#[non_exhaustive]
pub enum WebImageFormat { Png, Jpg, Gif, Webp, Svg }
```

[`crates/typst-svg/src/image.rs:59-74`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-svg/src/image.rs#L59-L74) —— `WebImage` 仅由格式 + 字节组成。`#[non_exhaustive]` 预留未来新增格式（如 AVIF）的余地。

`WebImage::new` 的分派：

```rust
#[comemo::memoize]
pub fn new(image: &Image) -> WebImage {
    let (format, data) = match image.kind() {
        ImageKind::Raster(raster) => match raster.format() {
            RasterFormat::Exchange(format) => (
                /* 映射 ExchangeFormat → WebImageFormat */ raster.data().clone(),
            ),
            RasterFormat::Pixel(_) => (WebImageFormat::Png, {
                // 用 PngEncoder 把裸像素重新编码成 PNG
                ...raster.dynamic().write_with_encoder(encoder)...
            }),
        },
        ImageKind::Svg(svg) => (WebImageFormat::Svg, svg.data().clone()),
        ImageKind::Pdf(pdf) => (WebImageFormat::Svg, Bytes::from_string(pdf_to_svg(pdf))),
    };
    Self { format, data }
}
```

[`crates/typst-svg/src/image.rs:102-131`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-svg/src/image.rs#L102-L131) —— 四个分支归一所有图片类型。`Pixel` 分支会保留 ICC 配置文件（`set_icc_profile`）。

`to_base64_url`：

```rust
#[comemo::memoize]
pub fn to_base64_url(&self) -> EcoString {
    let mut url = eco_format!("data:{};base64,", self.format.mime());
    let data = base64::engine::general_purpose::STANDARD.encode(&self.data);
    url.push_str(&data);
    url
}
```

[`crates/typst-svg/src/image.rs:136-143`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-svg/src/image.rs#L136-L143) —— 标准 base64 编码后拼到 `data:` 前缀之后。`mime()` 定义在 [`image.rs:78-86`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-svg/src/image.rs#L78-L86)。

`convert_image_scaling`（被 `IMAGE_RULE` 用于生成 `image-rendering`）：

```rust
pub fn convert_image_scaling(scaling: Smart<ImageScaling>) -> Option<&'static str> {
    match scaling {
        Smart::Auto => None,
        Smart::Custom(ImageScaling::Smooth) => Some("smooth"),
        Smart::Custom(ImageScaling::Pixelated) => Some("pixelated"),
    }
}
```

[`crates/typst-svg/src/image.rs:46-56`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-svg/src/image.rs#L46-L56) —— `Auto`（默认）不输出属性，让浏览器自行决定；`Smooth`/`Pixelated` 映射到对应 CSS 值。注意 `smooth` 仍是实验性 CSS（注释里链接了 MDN 兼容性说明）。

> 关于复用：`WebImage::new` 与 `to_base64_url` 都带 `#[comemo::memoize]`，意味着同一张 `Image`（按内容哈希）无论被 SVG 导出还是 HTML 导出、被引用多少次，转码与 base64 编码都只发生一次。这是 typst-html 选择「复用 typst-svg 的 `WebImage`」而非自实现的一个额外收益。

#### 4.5.4 代码实践

**实践目标**：理解格式归一，特别是「像素格式重新编码为 PNG」与「PDF 转 SVG」两个特殊分支。

**操作步骤**：

1. 打开 [`crates/typst-svg/src/image.rs:102-131`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-svg/src/image.rs#L102-L131)，对照四个分支。
2. 回答：为何 `RasterFormat::Pixel` 不能像 `Exchange` 那样「直接复用字节」？
3. 阅读 [`image.rs:145-184`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-svg/src/image.rs#L145-L184) 的 `pdf_to_svg`，观察它如何用 `hayro_svg::convert` 把 PDF 页渲染成 SVG 字符串（并内置标准字体）。

**需要观察的现象**：`Exchange` 格式复用字节、`Pixel` 格式重新编码、`Pdf` 格式矢量转换——三种「归一」策略各不相同。

**预期结果**：`Pixel` 分支无法复用字节，因为「像素格式」只是一块裸的像素缓冲区（无 PNG/JPG 文件头与压缩），浏览器无法直接渲染，必须用 `PngEncoder` 重新打包成合法 PNG。这一结论可从 `raster.dynamic().write_with_encoder(...)`（操作的是 `DynamicImage` 而非已有字节）确认。

#### 4.5.5 小练习与答案

**练习 1**：一张内嵌的 PDF 矢量图，最终在 HTML 里以什么 MIME 类型出现？为什么？

> **参考答案**：以 `image/svg+xml` 出现。因为网页不原生支持把 PDF 当 `<img>` 的位图源，`WebImage::new` 的 `ImageKind::Pdf` 分支调用 `pdf_to_svg` 先转成 SVG，再以 `WebImageFormat::Svg` 存储，对应 MIME 即 `image/svg+xml`。

**练习 2**：`WebImage::new` 和 `to_base64_url` 为什么都要加 `#[comemo::memoize]`？两次缓存是否冗余？

> **参考答案**：前者缓存「图片 → WebImage（含可能的 PNG 重编码/PDF 转 SVG）」，后者缓存「WebImage → data URL 字符串」。两者输入输出不同，分别缓存避免重复的是不同阶段的昂贵工作：重编码/矢量转换 vs base64 编码。又因为 SVG 导出也调用同一对函数，缓存还能跨导出器复用。故并不冗余。

---

## 5. 综合实践

把本讲知识串起来，完整追踪一张「带表头、页脚、合并单元格」的 Typst 表格导出为 HTML 的全过程。素材用仓库自带的端到端测试 [`tests/suite/layout/grid/html.typ:1-32`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/tests/suite/layout/grid/html.typ#L1-L32) 的 `basic-table`：

```typst
#table(
  columns: 3,
  rows: 3,
  table.header(
    [The], [first], [and],
    [the], [second], [row],
    table.hline(stroke: red)
  ),
  table.cell(x: 1, rowspan: 2)[Baz],
  [Foo], [Bar],
  [1], [2],
  table.cell(colspan: 2)[3],
  [4],
  table.footer([The], [last], [row]),
)
```

**任务**：按下列步骤，在纸上（或注释里）画出它的 HTML 结构，并标注每个关键判断的来源代码行。

1. **IR 构造（typst-library）**：`table_to_cellgrid` 把表头 2 行的范围记为 `headers[0].range = 0..2`，footer 记为 `footer.start = 5`（行计数），把 `Baz` 在 `(1,2)` 与 `(1,3)` 的第二个位置、`[3]` 在 `(0,4)` 与 `(1,4)` 的第二个位置都填成 `Entry::Merged`。
2. **行切分**：本表无 gutter，`non_gutter_column_count() = 3`，`entries.chunks(3)` 得到 6 行切片（2 表头 + 3 数据 + 1 页脚）。
3. **footer**：`rows.drain(5..)` 取走末行 → `<tfoot><tr><td>The...`（见 [`rules.rs:592-599`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L592-L599)）。
4. **连续表头**：`headers[0]` 起点为 0、等于 `consecutive_header_end`，连续，故 `first_mid_table_header = 1`；`drain(..2)` 取走开头 2 行 → `<thead>` 里 2 个 `<tr>`、每格 `<th>`（见 [`rules.rs:614-637`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L614-L637)）。
5. **tbody**：剩 3 行（原行 2、3、4），`y_offset = 2`。逐行判断：这 3 行都不在任何 header 范围内（header 范围是 `0..2`），故全用 `<td>`（见 [`rules.rs:643-659`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L643-L659)）。因为有 thead 与 tfoot，body 被包进 `<tbody>`。
6. **单元格合并**：
   - 第 2 行（`Foo/Baz/Bar`）：3 个 `Entry::Cell`，Baz 的 `rowspan=2` 被 `show_cell` 写成 `rowspan="2"`。
   - 第 3 行（`1/?/2`）：Baz 下方位置是 `Entry::Merged`，被 `as_cell()` 过滤，只剩 `<td>1</td><td>2</td>`。
   - 第 4 行（`3/?/4`）：`[3]` 右侧是 `Entry::Merged`，过滤后 `<td colspan="2">3</td><td>4</td>`。
7. **最终顺序**：`<table>` = `<thead>` → `<tbody>` → `<tfoot>`（见 [`rules.rs:661-666`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L661-L666)）。

**交付物**：手写出这张表导出后的 HTML 骨架（无需精确到空白），重点体现 `<thead>`/`<tbody>`/`<tfoot>` 三段、`rowspan="2"` 与 `colspan="2"` 出现的位置、以及被合并格子不产生节点这一事实。

**验证方式**：（待本地验证）在仓库根目录运行对应 HTML 快照测试，比对你的手写结果与实际输出。若不运行，则交叉核对 4.1–4.3 的源码逻辑确认推断一致即可。

## 6. 本讲小结

- typst-html **不重新解析表格**，而是接手 typst-library 已算好的 `CellGrid` IR；`TABLE_RULE` 仅做「取 grid + 转发」。
- `show_cellgrid` 用 `chunks` 切行、`drain` 切段，按 **footer → 连续表头 → 剩余主体** 的顺序产出 `<tfoot>`/`<thead>`/`<tbody>`；只有存在 thead/tfoot 时才显式包 tbody。
- **连续表头**（从第 0 行起无空隙）进 `<thead>`；**中间或断开的表头**降级为 `<tbody>` 内的 `<th>` 行，由 `next_header` 游标判定。
- gutter 场景下表头/页脚范围需要 **/2 与 div_ceil(2)** 的坐标换算，因为 `CellGrid` 内部行号含 gutter。
- `show_cell` 用 `Entry::as_cell()` 过滤合并占位，用 `(n != NonZeroUsize::MIN)` 惯用法**省略值为 1 的 colspan/rowspan**；`TABLE_CELL_RULE` 把单元格透明解包为 body。
- `IMAGE_RULE` 装配 `<img>`：`src` 走 base64 data URL、`alt` 可选、HTML `width`/`height` 属性取整数像素尺寸（占位用）、CSS `image-rendering`/`width`/`height` 控制渲染与显示尺寸。
- 图片格式归一与 data URL 生成**复用 typst-svg 的 `WebImage`**：Exchange 直接复用字节、Pixel 重编码为 PNG、PDF 转 SVG，两个函数都经 comemo 缓存。

## 7. 下一步学习建议

- **u6-l1（html.frame 与 SVG 嵌入）**：`WebImage` 是「图片复用 typst-svg」的一条线索，`html.frame` 则是「整段内容复用 typst-svg」的另一条；对照阅读能看清 typst-html 与 typst-svg 的协作全貌。
- **u4-l3 / u4-l4（CSS 属性系统与类型转换）**：本讲反复出现 `css::Properties::build`、`css.push("width", rel)`，如果想搞清 `rel`（一个 `Rel<Length>`）如何变成 CSS 字符串、序列化失败如何降级，应回到这两讲。
- **u3-l5（内建 show 规则注册机制）**：若对 `ShowFn`、`register()`、用户 show 规则覆盖内建规则的优先级仍有疑问，可回看本讲的前置讲义。
- 继续阅读源码：建议通读 [`src/rules.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs) 中尚未展开的 `FOOTNOTE_RULE`、`OUTLINE_RULE`、`BIBLIOGRAPHY_RULE`，它们与本讲两条规则同属「Model 组」映射表，模式高度一致，是巩固 show 规则写法的好素材。
