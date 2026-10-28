# 网格世界：grid、table、列表与数学布局

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 **轨道（track）** 的数据模型：`Sizing` 的三种形态（`auto` / 固定或相对长度 / `fr`）如何在 `CellGrid` 中统一成带 gutter 的轨道序列，`table` 为什么和 `grid` 共用同一台布局引擎。
2. 讲清 **轨道尺寸协商** 的完整顺序：固定轨道先解析 → `auto` 轨道用试探布局测量 → 剩余空间按比例分给 `fr` 轨道（或空间不足时让 `auto` 轨道「公平收缩」），并能手工推算一张表格的列宽。
3. 描述 `lists.rs` 列表布局的 **架构演进**：它曾经真的复用网格引擎（`CellGrid` + `GridLayouter`），2026 年 5 月起改为专用的 `ListLayouter` + 栈布局，理解这次替换背后的动机（基线对齐）。
4. 了解数学排版如何组织 **符号行与嵌套结构**：`MathItem` 中间表示 → `MathFragment` 片段流 → `MathRun` 行装配，以及分数与根式如何用字体 MATH 表常量逐块拼装并对齐基线。

本讲是第四单元「排版管线」的倒数第二讲：u4-l4 讲了 flow 如何把块级内容分配进 region，u4-l5 讲了行内断行；本讲看两个「二维」的特化布局器——网格与数学公式，它们都建立在前面的 region 协商机制之上。

## 2. 前置知识

- **`Abs` / `Rel` / `Fr`**：`Abs` 是绝对长度（pt）；`Rel` 是「比例 × 基准 + 绝对部分」（如 `20% - 1cm`），要相对某个基准解析；`Fr` 是「剩余空间的分数」（fractional），永远最后参与分配。
- **`Region` / `Regions`**（u4-l4）：给子内容的「可用空间提案」。网格在测量 auto 列时，会给每个单元格发一个试探性的 pod region，让它先排一遍以获知自然宽度——这是本讲反复出现的模式。
- **`Frame` / `Fragment`**：`Frame` 是绝对定位的二维绘图树，且带**基线（baseline）**概念；`Fragment` 是一组 `Frame`（内容可能跨页）。
- **Synthesize 阶段**（u4-l2）：realize 之前，元素有机会把脚本参数「整理」成内部字段。`GridElem`/`TableElem` 正是在这里把用户输入编译成 `CellGrid`。
- **内置规则注册**（u4-l2）：`GRID_RULE`、`TABLE_RULE` 等常量经 `NativeRuleMap` 装配进 `Library`，把元素接线到具体布局函数。
- **试探布局（measure）**：先用一个假想的 region 布局一次、只取尺寸丢弃正式产物的做法。行内断行（u4-l5）里已经见过，网格测量 auto 列、列表测量 marker 都靠它。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-library/src/layout/container.rs` | `Sizing` 枚举：轨道尺寸的三种形态（类型在 library，行为在 layout——u1-l3 的架构决策又一例） |
| `crates/typst-library/src/layout/grid/mod.rs` | `GridElem` / `GridCell` / `TrackSizings` 的定义与 Synthesize 入口 |
| `crates/typst-library/src/layout/grid/resolve.rs` | `grid_to_cellgrid` / `table_to_cellgrid`：把脚本输入解析为 `CellGrid`（单元格定位、跨行列合并、gutter 交织、表头表脚） |
| `crates/typst-library/src/model/table.rs` | `TableElem` 的 Synthesize：`table` 与 `grid` 在这一层汇合 |
| `crates/typst-layout/src/grid/mod.rs` | 布局侧入口：`layout_grid` / `layout_table` / `layout_cell` |
| `crates/typst-layout/src/grid/layouter.rs` | `GridLayouter`：轨道尺寸协商（`measure_columns` 一族）与逐行布局主循环 |
| `crates/typst-layout/src/lists.rs` | 列表/枚举布局：`ListLayouter` 测量与 `ItemLayouter` 基线对齐（本讲讲它的「前世今生」） |
| `crates/typst-library/src/math/ir/` | 数学中间表示：`resolve_equation` 产出 `MathItem` / `MathKind` 树 |
| `crates/typst-layout/src/math/mod.rs` | 数学布局入口 `layout_equation_inline` / `layout_equation_block` 与 `MathContext` |
| `crates/typst-layout/src/math/run.rs` | `MathRun` / `stack_rows`：符号行装配、对齐点、行内断行机会 |
| `crates/typst-layout/src/math/fraction.rs` | 分数布局：MATH 表常量驱动的拼装 |
| `crates/typst-layout/src/math/radical.rs` | 根式布局：拉伸根号、指数抬升 |
| `crates/typst-layout/src/math/table.rs` | 矩阵/向量布局：数学世界里的「小网格」 |
| `crates/typst-layout/src/rules.rs` | 内置规则：`GRID_RULE` / `TABLE_RULE` / `LIST_RULE` / 文献表对网格的真复用 |

数据流总览：

```
脚本: #grid(columns: (auto, 1fr), ...) / #table(...)
  │  GridElem::synthesize / TableElem::synthesize      (typst-library)
  ▼
CellGrid ── 轨道(含 gutter 交织) + Entry(Cell/Merged) + hlines/vlines + headers/footer
  │  GRID_RULE / TABLE_RULE → BlockElem::multi_layouter (rules.rs)
  ▼
GridLayouter (typst-layout/src/grid/layouter.rs)
  │  measure_columns: 固定 → auto 试探测量 → fr 分红 / auto 公平收缩
  │  layout(): 逐行 layout_auto_row / layout_relative_row / Row::Fr
  ▼
Fragment (多页时含重复表头/跨页 rowspan)

脚本: $ a/b + sqrt(x) $   (EquationElem)
  │  resolve_equation (typst-library/src/math/ir)
  ▼
MathItem 树 (MathKind::Fraction / Radical / Glyph / Table / ...)
  │  MathContext::layout_into_self 递归 + layout_realized 按 kind 派发
  ▼
MathFragment 流 → MathRun 行装配(row_into_line_frame / stack_rows) → Frame
```

## 4. 核心概念与源码讲解

### 4.1 模块一：网格轨道模型——从 `TrackSizings` 到 `CellGrid`

#### 4.1.1 概念说明

**轨道（track）** 是网格里一整列（或一整行）的尺寸单位，概念借自 CSS Grid。用户在脚本里写的 `columns: (60pt, 1fr, 2fr)` 就是一个轨道列表，每个轨道是三种尺寸之一：

- `auto`：按内容自适应——把这一列所有单元格各排一遍，取最宽的；
- 固定或相对长度（`10pt`、`20% - 1cm`）：确切大小；
- `1fr`：所有其他轨道定下来之后，按分数瓜分剩余空间。

`grid` 与 `table` 在 Typst 里是**两个语义不同的元素**（无障碍、样式互不影响），但在布局引擎层面**完全是同一台机器**：二者在 Synthesize 阶段都被编译成同一个内部结构 `CellGrid`，再交给同一个 `GridLayouter`。这是「一个通用引擎 + 多个语义外壳」的典型架构。

`CellGrid` 解决的核心问题有三个：

1. **轨道统一**：把用户给的列/行轨道与 gutter（列间距/行间距）轨道交织成一条统一的轨道序列，之后布局器不必再区分「内容轨道」和「间距轨道」；
2. **定位与合并**：单元格支持 `x`/`y` 手工定位和 `colspan`/`rowspan` 跨行列，被跨过的位置要标记为「已合并」，指向真正的父单元格；
3. **附属物收集**：`hline`/`vline` 线条、可跨页重复的表头（header）与表脚（footer）也被收进同一结构。

#### 4.1.2 核心流程

从脚本到布局的完整链路：

```
GridElem / TableElem（脚本参数：columns/rows/gutter/inset/align/fill/stroke + children）
   │  Synthesize::synthesize
   │     grid_to_cellgrid(...) / table_to_cellgrid(...)
   ▼
CellGrid {
     cols/rows: Vec<Sizing>   ← 内容轨道与 gutter 轨道交织后的统一序列
     entries: Vec<Entry>      ← Entry::Cell(Cell) | Entry::Merged { parent }
     vlines / hlines          ← 显式线条
     headers / footer         ← 可重复表头表脚
 }
   │  realize 阶段: GRID_RULE / TABLE_RULE
   │     → BlockElem::multi_layouter(elem, crate::grid::layout_grid / layout_table)
   ▼
GridLayouter::new(grid, regions, ...).layout(engine)   （模块二的主角）
```

gutter 交织的规则（以 2 列 + gutter 为例）：`[列0, gutter, 列1, gutter, 列2, ...]`，最后一条多余的 gutter 会被 `pop` 掉。于是**偶数下标是内容轨道，奇数下标是 gutter 轨道**。

#### 4.1.3 源码精读

先看轨道尺寸的类型定义。`Sizing` 是一个三变体枚举，住在 typst-library：

[`Sizing` 枚举定义](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/container.rs#L476-L485)——`Auto`、`Rel(Rel)`、`Fr(Fr)` 三种轨道形态，并提供 `is_auto()` / `is_fractional()` 谓词。用户脚本能写的所有轨道尺寸最终都归约到这三个变体。

脚本参数到轨道列表的转换由 `TrackSizings` 完成：

[`TrackSizings` 与其 cast 规则](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/grid/mod.rs#L453-L463)——这段代码实现了文档里的便捷写法：单个 `Sizing` 变单轨道；非零整数 `3` 展开成 `(auto, auto, auto)`（`smallvec![Sizing::Auto; count.get()]`）；数组逐项 cast。所以 `columns: 5` 与 `columns: (auto, auto, auto, auto, auto)` 等价。

`GridElem` 把这些参数声明为字段：

[`GridElem` 的 columns/rows/gutter 字段](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/grid/mod.rs#L183-L221)——注意 `gutter` 是 `#[external]` 的语法糖：真正的字段是 `column_gutter` 与 `row_gutter`，二者的 `#[parse]` 属性先找各自的命名参数、找不到就回落到 `gutter`。单元格的跨行列能力则在 `GridCell` 上：

[`GridCell` 的 x/y/colspan/rowspan 字段](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/grid/mod.rs#L804-L840)——`x`/`y` 支持手工定位（`Smart<usize>`，`auto` 表示顺流自动放置），`colspan`/`rowspan` 默认为 1，这就是「跨两列单元格」在数据层的样子。

关键的一步发生在 Synthesize：

[`GridElem` 的 Synthesize 实现](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/grid/mod.rs#L441-L451)——`grid_to_cellgrid(self, engine, styles)?` 把元素编译成 `CellGrid`，存进内部合成字段 `grid: Arc<CellGrid>`（[L411-L413](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/grid/mod.rs#L411-L420)）。布局阶段只需 `elem.grid.as_ref().unwrap()` 就能拿到现成结构，Synthesize 与 Layout 的分界正在这里。

[`grid_to_cellgrid` 的实现](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/grid/resolve.rs#L27-L74)——把 columns/rows/gutter/inset/align/fill/stroke 逐个取出，children（Header/Footer/Item）映射成 `ResolvableGridChild`，最终交给通用的 `resolve_cellgrid`。`table` 那边几乎是逐行对称的代码：

[`TableElem` 的 Synthesize](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/model/table.rs#L310-L320) 调用 [`table_to_cellgrid`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/grid/resolve.rs#L78-L125)——两个函数体只有类型名不同。这就是「table 与 grid 共用引擎」在源码层的直接证据。

解析产物 `CellGrid` 的结构：

[`CellGrid` 结构定义](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/grid/resolve.rs#L654-L677)——`entries` 是行优先的一维数组，`cols`/`rows` 是**含 gutter 轨道**的统一轨道序列，`vlines`/`hlines` 收显式线条，`headers`/`footer` 支撑跨页重复。每个位置上是：

[`Entry` 枚举：Cell 或 Merged](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/grid/resolve.rs#L625-L645)——被 colspan/rowspan 覆盖的位置不是空，而是 `Entry::Merged { parent }`，指向父单元格的下标；`parent_cell_position`（[L793-L803](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/grid/resolve.rs#L786-L803)）负责把合并位置映射回父位置。布局器测量列宽时靠它避免重复测量同一个跨行列单元格。

gutter 交织发生在构造函数里：

[`CellGrid::new_internal` 的轨道交织](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/grid/resolve.rs#L700-L742)——每个内容轨道后面 push 一条 gutter 轨道（`get_or` 让 gutter 列表循环复用、不足时补零），最后 `pop` 掉多余的最后一条。行数取「用户给的行数」与「按单元格数量推算的行数」的较大值（L708-L713），所以 `columns: 4` 时给 12 个单元格会自动长出 3 行。

因为交织，访问 `(x, y)` 位置的坐标换算带了奇偶判断：

[`CellGrid::entry` 的奇偶寻址](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/grid/resolve.rs#L756-L776)——有 gutter 时偶数下标是内容、奇数下标是 gutter（返回 `None`），一维下标换算 `(y / 2) * c + x / 2`。

最后看布局侧的接线。realize 阶段的内置规则把两个元素接到同一个模块：

[`GRID_RULE` 与 `TABLE_RULE`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/rules.rs#L538-L540)——`TABLE_RULE` 把 `TableElem` 包成携带 `crate::grid::layout_table` 回调的 `BlockElem::multi_layouter`（`GRID_RULE` 在 [L698-L700](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/rules.rs#L698-L700) 对称地接 `layout_grid`）。这正是 u4-l2 讲过的 `NativeRuleMap` 机制：元素不变，行为经函数指针注入。

[`layout_grid` 与 `layout_table` 入口](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/grid/mod.rs#L98-L122)——两个函数都是三行：取出 Synthesize 阶段存好的 `CellGrid`，`GridLayouter::new(grid, regions, locator, styles, span).layout(engine)`。区别只在元素类型，引擎零差异。

顺带一提，同文件里的 [`layout_cell`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/grid/mod.rs#L30-L85) 是网格递归布局的最内层：任何单元格最终都回到 `crate::layout_fragment` 通用入口，还会为 table/grid 单元格手工生成 introspection 标签（与 u4-l7 的内省机制相接）。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「colspan 在数据层是 `Entry::Merged`」，并跑通一个现成的跨列测试。

1. 阅读现成测试 [`tests/suite/layout/grid/colspan.typ` 的 grid-colspan 用例](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/suite/layout/grid/colspan.typ#L1-L30)：它同时放了 `grid` 与 `table` 两个版本，第一行 `grid.cell(colspan: 4)[*Full Header*]` 跨满 4 列，第二行两个 `colspan: 2` 各跨一半。
2. 运行（u1-l5 讲过 `cargo testit` 的名字过滤）：

   ```bash
   cargo testit grid-colspan
   ```

3. 对照 `table_to_cellgrid` 与 `CellGrid::entry` 的源码，画出这个 6×4 网格的 entries 表：哪些下标是 `Entry::Cell`，哪些是 `Entry::Merged { parent }`，parent 指向谁。
4. 把同一份 .typ 内容存成 `grid-practice.typ`，用 `cargo run -p typst-cli -- compile grid-practice.typ` 编译，肉眼核对跨列单元格的填充色块确实横跨对应列。

**需要观察的现象**：`grid` 与 `table` 两个版本的输出像素级一致（仅 fill 略有差异的行除外）——因为它们走的是同一台 `GridLayouter`。

**预期结果**：测试通过；手画的 entries 表中，跨列单元格右侧被覆盖的位置都是 `Merged` 且 parent 指回该单元格的起始下标。（测试运行结果待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：`columns: 3` 和 `columns: (auto,)` 分别展开成什么？

> **答案**：`columns: 3` 经 `TrackSizings` 的 `NonZeroUsize` 分支展开为 `(auto, auto, auto)`；`columns: (auto,)` 就是单条 auto 轨道。注意文档特别说明：对 columns 而言单个值只会产生**一列**，这与 rows/gutter「单值循环复用」的行为不同（`get_or` 的循环复用只发生在 gutter 与超出给定长度的 rows 上）。

**练习 2**：为什么 `GridElem` 要在 Synthesize 阶段就把脚本参数编译成 `CellGrid`，而不是留到布局函数里现算？

> **答案**：`CellGrid` 存进 `Arc<CellGrid>` 合成字段后成为元素状态的一部分：(1) 布局函数签名极简（`layout_grid` 只需 unwrap 一下）；(2) 定位冲突、colspan 越界这类**求值期错误**能在 Synthesize 里以正确的 span 报给用户，而不是等布局时才炸；(3) 结构可哈希、可复用，对 comemo 增量编译友好（u5-l8 会展开）。

**练习 3**：2 列 + `column-gutter: 3pt` 的网格，统一轨道序列是什么？`entry(3, 2)` 返回什么？

> **答案**：序列为 `[列0, 3pt, 列1]`（最后的 gutter 被 pop）。`x = 3` 越界（共 3 条轨道，合法下标 0..2）；若问 `entry(1, 2)`——奇数 x 是 gutter 位置，返回 `None`。

---

### 4.2 模块二：轨道尺寸协商——`measure_columns` 的三方谈判

#### 4.2.1 概念说明

网格最核心的问题是：**每条轨道到底多宽？** Typst 的答案是一场固定次序的「三方谈判」：

1. **固定轨道**（`Rel`）先行：相对基准宽度解析成绝对值，占用从不动摇；
2. **`auto` 轨道**其次：把这一列的每个单元格用试探 region 排一遍，取最大自然宽度——这就是 CSS 里的 max-content 测量；
3. **`fr` 轨道**最后：若还有剩余空间，按分数比例瓜分（`1fr` 与 `3fr` 按 1:3 分）；若 `auto` 们已经把空间吃超了，则反过来让超标的 `auto` 轨道「公平收缩」到同一宽度。

行方向（`rows`）用同一套 `Sizing` 语义，但多了两个变数：`auto` 行可能**跨区域断开**（长单元格跨页续排），`fr` 行只是先登记、等区域收尾时才知道自己分到多高。

#### 4.2.2 核心流程

列方向协商的伪代码（对应 `measure_columns`）：

```
rel = Σ 固定轨道解析值        # 先解析所有 Rel 列
fr  = Σ fr 轨道的份数
available = region宽度 − rel

if available ≥ 0:
    (auto, count) = measure_auto_columns(available)   # 试探布局取最大
    remaining = available − auto
    if remaining ≥ 0:
        grow_fractional_columns(remaining, fr)        # fr 按比例分红
    else:
        shrink_auto_columns(available, count)         # auto 公平收缩
```

fr 分红公式（`Fr::share`）：

\[ w_i = \frac{f_i}{\sum_j f_j} \cdot \max(\text{remaining},\ 0) \]

auto 公平收缩（`shrink_auto_columns`）是一个迭代剔除过程：反复计算 `fair = redistribute / overlarge`，把「不超标」（`rcol ≤ fair`）的 auto 列移出待分集合、把它们占用的宽度还给公共池，直到集合不再变化；最后所有仍超标的列统一压到 `fair`。这保证**收缩负担只落在确实过宽的列上**，窄列不受牵连。

#### 4.2.3 源码精读

主入口：

[`measure_columns`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/grid/layouter.rs#L914-L957)——先扫一遍所有列：`Sizing::Rel(v)` 解析进 `rel`（`v.resolve(styles).relative_to(self.regions.base().x)`），`Sizing::Fr(v)` 累进 `fr`；然后 `available = regions.size.x − rel`，按剩余空间走向 `measure_auto_columns` → `grow_fractional_columns` 或 `shrink_auto_columns`。整个函数 40 行出头，是「谈判议程表」本身。

auto 测量是网格里最精巧的部分：

[`measure_auto_columns` 主体](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/grid/layouter.rs#L985-L1099)——对每个 auto 列，遍历该列所有行：经 `parent_cell_position` 找到父单元格（跳过合并位置与已测过的 rowspan，L991-L999）；构造试探 pod `Region::new(Size::new(available, height), Axes::splat(false))` 调 `layout_cell` 真排一遍，`resolved.set_max(frame.width() - already_covered_width)` 取最大值。`already_covered_width`（[L1076](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/grid/layouter.rs#L1076)）减去跨列单元格已从**先前解析的列**那里获得的宽度——一个跨 3 列的宽单元格不应把 3 列都撑到自己的全宽。

跨列单元格还有两条专门规则：

[colspan 只影响最后一条 auto 列 + 全 fr 列启发式](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/grid/layouter.rs#L1001-L1036)——(1) 跨列单元格只把自己「多出来的宽度」记到它所跨的**最后一条** auto 列头上（其余位置 `continue`）；(2) 若单元格已经跨过了网格里**全部** fr 列且区域宽度有限，则干脆不撑宽 auto 列——fr 列分红后自然会腾出空间给它。这两条规则对应的测试就在 [`colspan.typ` 的 grid-colspan-over-all-fr-columns 等用例](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/suite/layout/grid/colspan.typ#L92-L116)。

fr 分红：

[`grow_fractional_columns` 与 `Fr::share`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/grid/layouter.rs#L1101-L1112)——每条 fr 列分得 `v.share(fr, remaining)`，[`share` 的实现](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/fr.rs#L56-L63) 就是上面的比例公式，`remaining.max(Abs::zero())` 保证负余量不分倒扣。

公平收缩：

[`shrink_auto_columns`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/grid/layouter.rs#L1114-L1145)——`while changed && overlarge > 0` 循环不断剔除不需要收缩的列并重算 `fair`，末尾把所有 `rcol > fair` 的 auto 列钳到 `fair`。注释原文「Redistribute space to auto columns so that each gets a fair share」正是文档里「auto 轨道公平分配可用空间」承诺的落点。

行方向的派发在主循环里：

[`layout_row_internal` 按行轨道类型派发](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/grid/layouter.rs#L425-L464)——`Sizing::Auto` 走 `layout_auto_row`（先测量后正式布局，可跨区域断开续排）、`Sizing::Rel` 走 `layout_relative_row`、`Sizing::Fr` 只是把 `Row::Fr(v, y, disambiguator)` 压进当前区域（[L452-L457](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/grid/layouter.rs#L446-L458)），等 `finish_region` 收尾时才知道实际高度——与 u4-l4 里 flow 对 `fr` 间距的处理如出一辙。`Row` 枚举（[L214-L224](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/grid/layouter.rs#L214-L224)）因此分 `Frame`（已完成的行）与 `Fr`（待分红的行）两态。

整场谈判的调度者是 [`GridLayouter::layout` 主循环](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/grid/layouter.rs#L313-L388)：先 `measure_columns` 定列宽，再处理重复表脚预留、逐行推进（跳过表头区间、遇到 footer 布局表脚）、收尾区域、补排遗漏的 rowspan，最后 `render_fills_strokes` 补线与背景。

#### 4.2.4 代码实践

**实践目标**：手工推算一张混合轨道表格的列宽，再用现成测试验证推算。

现成测试 [`grid-columns-sizings-rect`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/suite/layout/grid/grid.typ#L3-L20) 用了 `#set page(width: 100pt)` 与 `columns: (auto, 1fr, 3fr, 0.25cm, 3%, 2mm + 10%)`，auto 列两个单元格分别宽 `0.5cm` 与 `1cm`。

按 `measure_columns` 的议程手算（换算 \(1\,\text{cm} = 72/2.54 \approx 28.35\,\text{pt}\)，\(2\,\text{mm} \approx 5.67\,\text{pt}\)，百分比基准 = 页宽 100pt）：

\[
\begin{aligned}
\text{rel} &= 7.09 + 3 + (5.67 + 10) = 25.76\,\text{pt} \\
\text{available} &= 100 - 25.76 = 74.24\,\text{pt} \\
\text{auto} &= \max(14.17,\ 28.35) = 28.35\,\text{pt} \\
\text{remaining} &= 74.24 - 28.35 = 45.89\,\text{pt} \\
1\text{fr} &= 45.89 \times \tfrac{1}{4} \approx 11.47\,\text{pt},\quad 3\text{fr} \approx 34.42\,\text{pt}
\end{aligned}
\]

操作步骤：

1. 先写下你自己的推算结果；
2. 运行 `cargo testit grid-columns-sizings-rect` 确认测试通过；
3. 把该用例复制到 `sizing-practice.typ`，在网格外面加一行 `#context grid.cell` 不行——更直接的办法：给每列套 `rect(width: 100%)` 已经是这个测试的做法，观察渲染图中各色块的实际宽度比例是否与推算一致（1fr 块 : 3fr 块 ≈ 1 : 3，且 auto 列宽等于 1cm 的那块）；
4. 再做反向实验：把列改成 `columns: (auto, auto)`、页宽保持 100pt，两个单元格各塞一段 80pt 宽的文本，触发 `shrink_auto_columns`——观察两列是否被压到接近 50pt 的公平宽度。

**需要观察的现象**：fr 列宽度严格按 1:3 分红；auto 超载时两条 auto 列收缩后近似等宽。

**预期结果**：推算值与渲染宽度吻合（±四舍五入误差）。具体像素值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`columns: (60pt, 1fr)`、页宽 100pt、只有一个空单元格，1fr 列多宽？

> **答案**：rel = 60pt，available = 40pt；auto 列不存在，auto 总量为 0；remaining = 40pt ≥ 0，唯一 fr 列分得全部 40pt。（`Fr::share` 中 \(1/1 \times 40 = 40\)。）

**练习 2**：三个 auto 列测量出 50pt、30pt、20pt，available 只有 60pt，最终各多宽？

> **答案**：`shrink_auto_columns`：初算 fair = 60/3 = 20pt，20pt 的列不超标被剔除（还给池子 20pt），剩 2 列分 40pt → fair = 20pt，30 与 50 仍超标、无新剔除，循环稳定；最终两列压到 20pt，原 20pt 列不变——结果 (20pt, 20pt, 20pt)。注意这个算法下「恰好等于 fair」的列不再参与挤压。

**练习 3**：为什么 `fr` 行不能像 `Rel` 行那样当场布局？

> **答案**：`fr` 的语义是「其他轨道分完之后的剩余空间」，而网格可能跨多个区域（页）：必须等当前区域里所有固定/自动行都排完、`finish_region` 时才知道剩余高度，才能给 `Row::Fr` 定高。这正是 `Row::Fr` 只登记不布局、推迟到收尾的原因（与 u4-l4 中 flow 对 fr 间距的延迟处理同构）。

---

### 4.3 模块三：列表复用——一段架构演进史

#### 4.3.1 概念说明

大纲给本模块起名「列表项复用网格的方式」。**这个说法描述的是历史版本，而当前 HEAD 已经不是这样了**——这本身就是极好的架构课：

- **旧设计（≈ 2026 年 5 月之前）**：`lists.rs` 里 `layout_list`/`layout_enum` 亲手构造一个两列 `CellGrid`（marker 列 + body 列），交给 `GridLayouter` 布局。列表 = 一种特殊的网格，复用整套 auto 轨道测量与跨页协商。
- **现设计**：提交 `b241417c4`（*Improved list layout (#7895)*，2026-05-27）重写为专用的 `ListLayouter` + `ItemLayouter`，每一项经 `StackLayoutChild::CustomLayouter` 回调交给**栈布局**（`layout_stack_internal`）。

替换的直接动机是**基线对齐**：用户要求列表 marker（如 `1.`、`a)`）与正文第一行基线对齐（`number-align` / `marker-align`）。网格的行模型以「行框」为单位，表达不了「把 marker 下移 x pt，或把 body 重新排版并下移」这种非对称微调（见 4.3.3 的 `baseline_align`），于是列表走上了专用路径。

「复用网格」今天依然存在，只是搬了家：**文献目录（bibliography）**在 realize 阶段真的构造一个 `GridElem` 来排两栏文献条目；**table** 更是整个复用网格引擎（本讲 4.1）。所以准确的结论是：*通用引擎负责真正二维的场合，二维性很弱（两列、基线驱动）的列表退回专用实现。*

#### 4.3.2 核心流程

当前 `layout_items` 的四步流程（源码 doc comment 原文总结）：

```
1. measure_markers   —— 逐项试探布局 marker，取最大宽度（marker_width）
                        （这就是「auto 轨道测量」的迷你版！）
2. measure_bodies    —— 仅当区域宽度无限或 width: auto 时，测 body 最大宽度，
                        让各项在彼此之间对齐而不是对齐页宽
3. layout_item       —— 每项一个 ItemLayouter：排 marker、排 body、
                        基线对齐（或显式垂直对齐）
4. layout_stack_internal —— 各项作为 StackLayoutChild::CustomLayouter
                        交给栈布局器纵向堆叠（间距 = spacing/tight 逻辑）
```

#### 4.3.3 源码精读

先用 git 考古确认「前世」：

```bash
git show b241417c4~1:crates/typst-layout/src/lists.rs | grep -n "CellGrid\|GridLayouter"
#  7:use typst_library::layout::grid::resolve::{Cell, CellGrid};
# 13:use crate::grid::GridLayouter;
# 56:    let grid = CellGrid::new(
# 66:    let layouter = GridLayouter::new(&grid, regions, locator, styles, elem.span());
```

旧版 `layout_list` 在第 56 行构造 `CellGrid::new`（两列：marker 与 body），第 66 行交给 `GridLayouter`——「列表复用网格」的出处。对比当前文件 [`lists.rs` 的 import 区](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/lists.rs#L1-L16)：只剩 `layout_stack_internal`，网格的痕迹完全消失。

现在的入口：

[`layout_list`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/lists.rs#L18-L76)——取出 `indent`/`body_indent`/`tight`/`spacing`（tight 列表默认用段落 `leading`，宽列表用段落 `spacing`，L30-L32）；解析 marker 并 `aligned(marker_align)`；把每项 body 包上 `PdfMarkerTag::ListItemLabel/ListItemBody`（无障碍语义）；最后组装 `ListLayouter` 进入 `layout_items`。`layout_enum`（[L80-L171](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/lists.rs#L80-L171)）只多出编号计算（`numbering.apply`、`reversed` 递减），后半段完全共享。

四步流程的正文：

[`layout_items` 及其四步文档注释](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/lists.rs#L235-L306)——先为每项分配 marker/body 两个 locator（测量与正式布局必须用同一 locator，否则内省定位会错乱，L256-L267 的注释）；然后 `measure_markers`、按需 `measure_bodies`；最后每项闭包成 `StackLayoutChild::CustomLayouter`，交给 `layout_stack_internal(cells, span, gutter, Dir::TTB, ...)` 纵向堆叠。

「auto 轨道」的迷你版：

[`measure_markers`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/lists.rs#L308-L339)——对每个 marker 用 `Region::new((available_width, ∞), 不扩展)` 试探布局，`marker_width.set_max(...)`，与网格 `measure_auto_columns` 对每列取最大宽度是同一个思想，只是规模缩到一「列」。[`measure_bodies`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/lists.rs#L341-L372) 只在宽度无限/不可扩展时执行，避免各项对齐页宽而不是互相对齐。

离开网格的理由——基线对齐：

[`ItemLayouter::baseline_align`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/lists.rs#L509-L557)——计算 `diff = body首帧基线 − marker基线`：`diff ≥ 0` 时把 marker 下移 `diff`（L534-L537）；`diff < 0` 时反过来**用更小的空间重排 body** 再下移（L549-L554）——「重新排版子内容并平移」这种操作超出了网格行模型的表达力，这是重写的核心动机。之后 [`finish`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/lists.rs#L586-L634) 把 marker 放进第一个非空帧、body 整体缩进（RTL 时镜像处理，L604-L625）。

栈布局侧的配合改造：

[`StackLayoutChild::CustomLayouter` 与 `layout_stack_internal`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/stack.rs#L33-L40)——为这次重写专门加的变体：允许每个 child 自带布局回调（注释明说「useful when using stack layout to create other layouters, such as that of lists」），使列表项能借用栈的间距/分页协商而保留自定义内部布局。

复用网格的「现任」：

[`BIBLIOGRAPHY_RULE` 中手工构造 GridElem](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/rules.rs#L479-L505)——带编号前缀的文献表把（前缀, 条目）装进 `GridChild::Item(GridItem::Cell(...))`，构造 `GridElem::new(cells).with_columns(TrackSizings(smallvec![Sizing::Auto; 2]))`（两条 auto 列）加 gutter，`synthesize` 后直接包成 `BlockElem::multi_layouter(packed, crate::grid::layout_grid)`。这就是今天「XX 复用网格」的标准写法。列表的接线则在 [`LIST_RULE`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/rules.rs#L123-L140)：`BlockElem::multi_layouter(elem, crate::lists::layout_list)`，tight 时在前面补一个弱 `v` 间距。

#### 4.3.4 代码实践

**实践目标**：用 git 亲眼看到「复用 → 专用」的切换，并在现实中观察 marker 宽度测量。

1. 在仓库根目录执行：

   ```bash
   git show b241417c4~1:crates/typst-layout/src/lists.rs | sed -n '40,75p'   # 旧版：CellGrid + GridLayouter
   git show b241417c4 --stat                                               # 重写波及的文件
   ```

   注意测试参考图的变化（`tests/ref/render/enum-*`、`issue-1204-*baseline*`）——基线对齐正是这次重写的验收标准。
2. 写 `list-practice.typ`：

   ```typst
   #enum(
     numbering: "i)",
     [第一项], [第二项], [第三项]
   )
   ```

   编译后观察：三个罗马数字 marker `i)` `ii)` `iii)` 宽度不同，但各项正文**起始 x 坐标一致**——这正是 `measure_markers` 取 `max(iii 宽)` 作为统一 marker_width 的效果。
3. 进阶：在 [`measure_markers` 的 set_max 行](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/lists.rs#L329-L331) 前临时加一行 `eprintln!("marker width: {:?}", marker.width());`，`cargo build -p typst-cli` 后重新编译上述文件，核对打印的最大值与 `iii)` 的渲染宽度一致。**看完请还原源码，不要提交这行调试输出。**

**需要观察的现象**：步骤 2 中正文左边界对齐；步骤 3 中打印的最大 marker 宽 ≈ `iii)` 的宽度。

**预期结果**：两者吻合；git 考古输出与 4.3.3 引用的行号一致（行号以你本地 `git show` 输出为准）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `measure_bodies` 只在「区域宽度无限或不可水平扩展」时执行？

> **答案**：宽度有限且可扩展时，列表会撑满可用宽度，各项 body 天然对齐同一边界，无需测量；只有 `width: auto`（无限宽度）或不可扩展的场合，栈布局不会把内容拉满，若不测量，各项 body 会各自为政、左边界参差（源码注释：prompting list items to align between themselves instead of relative to the full page width）。

**练习 2**：`baseline_align` 中为什么 `diff < 0` 分支要「重排 body」而 `diff ≥ 0` 分支只需移动 marker？

> **答案**：`diff ≥ 0` 说明 marker 基线在 body 之上，把 marker 向下平移即可，不改变任何尺寸；`diff < 0` 说明 marker 比正文首行低（如 marker 是高编号、body 首行是大号字），若只移动 body 会让列表项整体变高且与上方内容留缝，所以要用少了 `|diff|` 的空间**重新布局** body（可能因此多断一行），再整体下移——保证 marker 不侵入列表上方的内容。

**练习 3**：如果现在要给文献目录之外再实现一个「标签-内容」两栏结构（如术语表），应该模仿 bibliography 的做法还是 lists 的做法？

> **答案**：若两栏都是普通块级内容、需要跨页与行高协商——模仿 bibliography：构造 `GridElem`（两条 auto 列 + gutter）交给网格引擎；若右栏需要与左栏基线对齐这类**非对称微调**——模仿 lists 的专用布局器 + `StackLayoutChild::CustomLayouter`。判据是「二维性是否退化到基线驱动的一维问题」。

---

### 4.4 模块四：数学排版结构——`MathItem`、`MathRun` 与分数/根式

#### 4.4.1 概念说明

数学排版与普通流式排版是两个世界：公式不是「块从上往下、字从左往右」的流，而是**嵌套盒子的递归组合**——分数把两个子公式上下摞、根式把 radicand 包进拉伸过的根号、矩阵把一堆公式排成阵列。Typst 把这套逻辑分成两层：

- **typst-library 的 `math::ir`**：`resolve_equation` 把求值后的公式内容解析成中间表示 `MathItem` 树（`MathKind::Fraction` / `Radical` / `Glyph` / `Table` 等 19 种，外加 `MathProperties` 携带数学类别、字号档位、左右间距等属性）；
- **typst-layout 的 `math` 模块**：`MathContext` 递归消费这棵树，每个节点产出一串 `MathFragment`（字形/子帧/空隙），同层的片段组成 `MathRun`（一行），按**基线**对齐拼进 `Frame`。

两个贯穿全模块的几何概念：

- **基线（baseline）**：帧内 y 坐标的一个刻度。同一行的所有片段按各自 ascent/descent 对齐到公共基线；分数、根式产出的复合帧也自带基线，于是能无限嵌套。
- **轴线（axis height）**：数学意义上的「公式竖直中心」，比基线略高（约半个 x 字高）。分数的横线、矩阵的整体基线都以轴为参照，来自字体 MATH 表的 `axis_height` 常量。

所有尺寸细节（分数线上移量、根号间隙、上下标缩放比例）都不是硬编码，而是**逐项查当前数学字体 OpenType MATH 表的常量**——这正是 Typst 换数学字体后排版感会变的原理。

#### 4.4.2 核心流程

一个行内公式 $a/b + sqrt(x)$ 的完整旅程：

```
$ ... $ 源码
  │  typst-eval 求值出 EquationElem 的 body（内容树）
  │  行内: inline.rs 收集阶段把它作为 InlineItem 交给 math 模块（u4-l5 衔接点）
  ▼
resolve_equation (typst-library/src/math/ir/mod.rs)
  ▼
MathItem 树: Group[ Fraction(a,b) 的组件, Glyph(+), Radical(x) ... ]
  │  layout_equation_inline / layout_equation_block
  ▼
MathContext（携带字体栈与片段收集器）
  │  layout_into_self: 逐项 layout_realized，按 MathKind 派发：
  │    Fraction → layout_fraction（MATH 常量拼装，产出带基线的子帧）
  │    Radical  → layout_radical（拉伸根号 + 指数抬升）
  │    Table    → layout_table（矩阵：列内子列宽协商 + stack_rows）
  │    ...     → 其余 15 种
  ▼
MathRun = Vec<MathFragment>   （一行片段流）
  │  row_into_line_frame: y = row_ascent − frag.ascent()，全部落到公共基线
  │  行内公式再经 into_par_items: 在二元/关系运算符处切开 → 给段落断行机会
  ▼
Frame（带 baseline）→ 回到行内排版 / 块级 flow
```

行装配的核心不变式：一行里第 i 个片段的放置纵坐标为

\[ y_i = \text{row\_ascent} - \text{ascent}_i, \qquad \text{row\_ascent} = \max_i \text{ascent}_i \]

于是所有片段的基线恰好落在 `y = row_ascent` 这条水平线上。

#### 4.4.3 源码精读

中间表示在 library 侧：

[`resolve_equation`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/math/ir/mod.rs#L19-L32)——入口只有几行：创建 `MathResolver`，把 `EquationElem.body` 解析进 arena 借用的 `MathItem`。顶层的 [`MathItem` 枚举](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/math/ir/item.rs#L65-L75) 只有四个变体：可布局组件 `Component`、显式 `Spacing`、普通空格 `Space`、内省 `Tag`。真正的分类在：

[`MathKind` 枚举](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/math/ir/item.rs#L383-L420)——Group（水平组）、Multiline（多行对齐）、Radical、Fenced（定界符包裹）、Fraction、SkewedFraction、Table（矩阵）、Scripts（上下标）、Accent、Cancel、Line、Primes、Text、Number、Glyph、Box（行内内容）、Mathml、External，共 19 种；大的递归类装箱（`Box<...>`）。每个组件还带 [`MathProperties`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/math/ir/item.rs#L422-L446)：数学类别 `class`（Normal/Relation/Binary/…，决定间距与断行）、字号档 `size`、`cramped` 风格、左右间距 `lspace`/`rspace` 等。

布局入口：

[`layout_equation_inline`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/mod.rs#L50-L102)——`get_font` 选出数学字体（[L617-L637](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/mod.rs#L617-L637) 经 FontBook 逐字体族挑选，`warn_non_math_font` 对没有 MATH 表的字体发警告）；`resolve_equation` 建树后交给 `MathContext`；行内公式的片段流经 `into_par_items` 在运算符处切开成多个 `InlineItem`，让 u4-l5 的行内引擎获得断行机会；最后用字体 edges 把每帧的 ascent/descent 归一到文本行高（L83-L99）。块级版本 [`layout_equation_block`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/mod.rs#L105-L249) 额外处理多行公式的跨区域切分与公式编号（`Counter::of(EquationElem::ELEM)`，接 u4-l7 内省）。

递归引擎：

[`MathContext::layout_into_self`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/mod.rs#L438-L463)——遍历 `MathItem` 序列，若某项的字体设置与外层不同则压栈新字体（`fonts_stack`），保证局部换字体（如 `\frak`）只影响子树。单项派发在：

[`layout_realized` 的按类派发](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/mod.rs#L467-L551)——先按 `props.lspace` 压入左间距（L493-L499），然后一个巨大的 `match &comp.kind` 把 19 种 `MathKind` 一一派发给本模块的 `layout_*` 函数，末尾补 `rspace`。这就是「树 → 片段流」的调度中枢。

片段与行：

[`MathFragment` 枚举](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/fragment/mod.rs#L22-L101)——`Glyph` / `Frame` / `Space` / `Tag` 四态，统一提供 `width` / `ascent` / `descent` / `class` 等接口：**任何复杂结构最终都坍缩成一个带基线语义的矩形片段**，这是数学排版可组合性的来源。行类型 [`MathRun = Vec<MathFragment>`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/run.rs#L17-L24)；

[`row_into_line_frame`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/run.rs#L301-L345)——行装配的落点：`let y = ascent - frag.ascent()`（L335）正是上面的基线不变式；对齐点机制（`cumulative_alignment_points`，[L347-L359](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/run.rs#L347-L359)）支撑 `&` 对齐点多行公式；[`measure_row`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/run.rs#L149-L158) 取行内最大 ascent/descent；[`stack_rows`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/run.rs#L112-L147) 把多行按 leading 竖直堆叠。[`into_par_items` 的切分规则](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/run.rs#L191-L199)——在 `Binary` / `Relation` 类片段后切开（连续关系符、后随闭括号除外），是数学世界对 u4-l5 断行器的接口。

**分数**。[`layout_fraction`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/fraction.rs#L13-L122) 先递归排出分子分母两个子帧（L20-L21），再从字体 MATH 表取 `axis_height`、`fraction_rule_thickness`、`fraction_numerator_shift_up`（Display 档用 `..._display_style_...` 变体）等常量（L23-L49），间隙按「所需 vs 最小值取大」：

\[
\text{num\_gap} = \max\big(\text{shift\_up} - (\text{axis} + \tfrac{t}{2}) - \text{num.descent},\ \ \text{num\_min}\big)
\]

\[
\text{denom\_gap} = \max\big(\text{shift\_down} + (\text{axis} - \tfrac{t}{2}) - \text{denom.ascent},\ \ \text{denom\_min}\big)
\]

（对应 [L51-L53](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/fraction.rs#L51-L53)，\(t\) 是分数线厚度）。分数线宽度取分子分母的较大者，**基线定在分数线中点上方 axis 处**：`baseline = line_pos.y + axis`（[L66](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/fraction.rs#L55-L69)）——于是分数能像普通字符一样参与行装配。`item.line == false` 的分支（L85-L118）是「堆叠式」分数（`\atop`，无横线），改用 `stack_*` 常量只调间隙。斜线分数 [`layout_skewed_fraction`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/fraction.rs#L125-L188) 则把斜杠字形按目标高度**拉伸**（`set_stretch_relative_to`）再斜向摆放分子分母。

**根式**。[`layout_radical`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/radical.rs#L19-L152) 的流程是：先排 radicand，按「radicand 高度 + 线厚 + 间隙」估算目标高度（L27-L38）；把根号字形**竖直拉伸**到该目标（`item.sqrt.set_stretch_relative_to(target, Axis::Y)`，L41）；查 `radical_kern_before_degree` 等常量；指数（`index`，即 `\root n of x` 的 n）的抬升量遵循 TeXbook 第 360 页公式并加了一处 Typst 自己的修正（L86-L96 的注释：`+ index.descent()` 防止下伸部撞上根号）；最后组装帧并 `set_baseline(ascent)`（L109-L110）。顶横线由 `Geometry::Line(...).stroked(...)` 画出（L139-L146）。

**矩阵**。数学自己的「小网格」：[`layout_table`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/table.rs#L17-L192) 先**逐列独立布局**所有单元格（每个单元格还可能按 `&` 对齐点拆成 sub-column，L61-L89），计算每列的子列最大宽（`compute_sub_column_widths`，L211-L221）与每行的最大 ascent/descent，再逐列 `stack_rows` 装配、支持 augment 虚线（L91-L101 负索引归一、L119-L182 画线），最后整体基线定在「高度一半 + 轴高」（L186-L188）——矩阵竖直中心对齐轴而不是基线，这是它与文本网格最有趣的一点不同。与 `GridLayouter` 相比它没有 auto/fr 轨道谈判，因为数学列宽永远是 max-content。

#### 4.4.4 代码实践

**实践目标**：观察分数与根式的实际布局参数从 MATH 表流入帧的全过程。

1. 阅读现成测试 [`tests/suite/math/frac.typ` 的 math-frac-baseline 用例](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/suite/math/frac.typ#L3-L5) 与 [`tests/suite/math/root.typ` 的 math-root-basic 用例](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/suite/math/root.typ#L3-L5)，运行：

   ```bash
   cargo testit math-frac-baseline math-root-basic
   ```

2. 写 `math-practice.typ`：

   ```typst
   $ x = 1/2 = a/(a h) $ \
   $ A = sqrt(x + y) = root(3, x) $
   ```

   编译为 PNG：`cargo run -p typst-cli -- compile math-practice.typ math-practice.png`。
3. 打点观察常量：在 [`layout_fraction` 的 num_gap/denom_gap 计算处](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/math/fraction.rs#L51-L57) 临时插入 `eprintln!("axis={:?} t={:?} num_gap={:?} denom_gap={:?} baseline={:?}", axis, thickness, num_gap, denom_gap, baseline);`（baseline 行计算之后打印），重新 `cargo build -p typst-cli` 并编译上述文件。对照输出的 gap 值与 PNG 里分子下缘到横线的距离，验证「常量 → 几何」的对应。
4. 把公式里的 `1/2` 换成嵌套 `(1/2)/(3/4)` 再编译，观察打点输出出现**多组**日志（递归：外层分数先测量内层分数）——体会「测量阶段已经排过一遍子结构」。
5. **看完务必还原源码改动，不要把调试输出留在工作区。**

**需要观察的现象**：步骤 3 中打印的 `baseline` 与横线位置吻合；步骤 4 中内外两层分数各产生一组日志，且内层先打印。

**预期结果**：日志组数与嵌套层级一致；PNG 中分数线宽度等于分子分母中较宽者。具体数值（New Computer Modern Math 的常量）待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `MathFragment` 要统一提供 `ascent()`/`descent()`，而不是只给 `height()`？

> **答案**：数学行装配的对齐基准是**基线**而非顶边。一个分数帧和一个普通字母 x 高度不同，但只要按 `y = row_ascent − frag.ascent()` 放置，它们的基线就共线；只知 height 无法完成这种对齐，嵌套结构（分数套根式套上下标）也会失去对齐依据。

**练习 2**：矩阵（`math/table.rs`）的基线为什么是 `height/2 + axis` 而不是第一行的基线？

> **答案**：矩阵作为整体出现在公式中时，数学惯例是让它的**竖直中心对准轴线**（分数横线所在的水平线），这样 $[\begin{smallmatrix}a\\b\end{smallmatrix}] + c$ 里加号与矩阵整体视觉居中。若用首行基线，行数越多矩阵越向下溢出轴线。

**练习 3**：行内公式为什么要在二元/关系运算符处把片段流切开（`into_par_items`）？

> **答案**：这是数学给段落断行器（u4-l5）留的「合法断点」：长公式在 `+`、`=` 处允许换行，其他位置（如标识符内部）不允许。切分成多个 `InlineItem::Frame` 后，断行器把它们当作不可再分的行内物件，只能在这些边界处考虑断行；同时在运算符后插入零宽断行机会，运算符留在行尾。

---

## 5. 综合实践

把本讲三个模块串成一个任务：**给一张「跨两列 + auto 轨道」的表格做一次完整的列宽审计，再对比一条分数公式的布局参数。**

**任务设定**：新建 `audit.typ`：

```typst
#set page(width: 200pt)
#table(
  columns: (auto, auto, 1fr),
  table.cell(colspan: 2)[跨两列的表头],
  [c],
  [短], [稍微长一点的单元格], [占满剩余空间的列],
  [x], [y], [z],
)
$ (dif y)/(dif x) = root(3, x + 1) $
```

**步骤**：

1. **手算（模块二）**：固定轨道 0 条；两条 auto 列。先按 `measure_auto_columns` 的规则推演：跨两列的表头只把它「超出第一列已解析宽度」的部分记到**第二条** auto 列（4.2.3 的规则一）；auto 总量确定后，`1fr` 分走 `200pt − gutter − inset − auto 总量`。写下每列的预测宽度。
2. **对照源码解释**：在 `measure_columns` → `measure_auto_columns` → `grow_fractional_columns` 三处各设一个临时 `eprintln!` 打点（打印 `available` / 每条 auto 列的 `resolved` / `remaining`），重编译后跑 `cargo run -p typst-cli -- compile audit.typ audit.pdf`，把打点数值与手算对照，用一段话解释「列宽如何确定」。
3. **验证表格语义（模块一）**：确认输出里表头单元格横跨两列、线条绘制正确；再把 `table.cell(colspan: 2)` 改成 `grid.cell(colspan: 2)`（放进 `#grid(...)`），观察编译报错——阅读 [`GridItem::try_from` 的排错分支](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/grid/mod.rs#L529-L573) 里 "cannot use `table.cell` as a grid cell" 的提示，理解两个语义外壳在解析层的隔离。
4. **观察公式（模块四）**：按 4.4.4 的打点方法记录这条公式的分数与根式参数，画出「分数线 y 坐标 = 基线 − axis、根号拉伸目标高度」的示意，标注在输出的 PNG 上。
5. **清理**：还原所有源码打点；`git status` 确认工作区只剩你新建的 .typ 与 .png/.pdf 产物（建议移出仓库或加入忽略）。

**预期结果**：手算、打点、渲染三者互相印证；能用自己的话说出「固定 → auto → fr」的谈判顺序与 colspan 的记账规则。所有编译产物数值待本地验证。

## 6. 本讲小结

- **轨道模型**：`grid` 与 `table` 是同引擎的双外壳——Synthesize 阶段 `grid_to_cellgrid`/`table_to_cellgrid` 把脚本参数编译成统一的 `CellGrid`（轨道与 gutter 交织、`Entry::Cell/Merged` 合并记账、表头表脚内建），`GridLayouter` 只认 `CellGrid`。
- **尺寸协商**：`measure_columns` 的固定议程——`Rel` 轨道先解析，`auto` 轨道用试探布局取 max-content（跨列单元格只记账到最后一条 auto 列），剩余空间按 \( f_i/\sum f_j \) 分给 `fr`，不足时超标 auto 列迭代收缩到公平份额；行方向同语义，但 auto 行可跨区域、fr 行延迟到收尾。
- **列表的演进**：列表曾用两列 `CellGrid` 复用网格引擎，2026-05（`b241417c4`）起改为专用 `ListLayouter`（marker/body 预测量 + `StackLayoutChild::CustomLayouter` 栈堆叠），动机是网格表达不了的非对称基线对齐；今天真正复用网格的是 bibliography 与 table。
- **数学排版**：library 侧 `resolve_equation` 产出 `MathItem`（19 种 `MathKind` + `MathProperties`），layout 侧 `MathContext` 递归派发，一切坍缩为带 ascent/descent 的 `MathFragment`，行装配按 `y = row_ascent − ascent_i` 共线基线；分数/根式/矩阵的每个间隙都查字体 MATH 表常量，矩阵基线对轴不对基线。
- **方法论**：看到「XX 是不是复用了 YY」这类说法，先 `git log`/`git show` 查证当前 HEAD——架构结论是有时效的，本讲的列表就是活例。

## 7. 下一步学习建议

- **下一讲 u4-l7（内省机制）**：本讲多处埋了钩子——`layout_cell` 手工生成的 Tag、跨页重复表头的 `disambiguator`、块级公式编号用的 `Counter::of(EquationElem::ELEM)`，都依赖 introspection。下一讲讲清 `Location`/`query`/两遍编译如何让这些「跨页重复的东西」拥有稳定身份。
- **继续深挖网格**：`grid/rowspans.rs`（跨行单元格跨页拆分）、`grid/repeated.rs`（表头表脚重复）、`grid/lines.rs`（hline/vline 与单元格描边的优先级合并）是三个独立专题，各自对应 `tests/suite/layout/grid/` 下的同名主题测试。
- **数学进阶**：`math/scripts.rs`（上下标与 limits）、`math/shaping.rs` 与 `fragment/glyph.rs`（数学字形与拉伸）、`typst-library/src/math/ir/resolve.rs`（IR 是怎么从内容树建出来的）值得各花半小时。
- **对照阅读**：CSS Grid 的轨道尺寸算法（MDN "Grid sizing"）与本讲 `measure_columns` 异曲同工，比较两者的 auto 分布策略能加深理解。
