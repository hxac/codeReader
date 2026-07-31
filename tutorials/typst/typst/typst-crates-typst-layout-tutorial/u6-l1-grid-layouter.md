# 网格与表格布局 GridLayouter

## 1. 本讲目标

本讲进入专家层的第一站——表格与网格的排版引擎 `GridLayouter`。读完本讲，你应当能够：

1. 说清楚一个 `grid` / `table` 元素从源码到 `CellGrid` 再到最终 `Frame` 的完整流水线，并知道每一步分别发生在 `typst-library` 还是 `typst-layout`。
2. 理解列尺寸（`rcols`）的解析三态——`Auto` / `Rel` / `Fr`——以及当可用空间不够时，`Fr` 列如何瓜分剩余空间、`Auto` 列如何被「公平收缩」。
3. 理解行是逐 region（区域）「懒」解析的，`Fr` 行被推迟到 `finish_region` 才落地，`Auto` 行可以跨 region 断裂，从而理解「跨页表格如何逐段产出 frame」。
4. 读懂 `layout_cell` 中那段被注释标注为 `HACK` 的代码：为什么要为 `table.cell` / `grid.cell` **手动**生成内省 tag，以及当单元格内容跨多帧时如何用 `FrameParent` + `Group` 修正内省顺序。

> 前置讲义：本讲承接 [u2-l3 Frame 与 Fragment](u2-l3-frame-and-fragment.md)（排版产出物）和 [u4-l1 flow 布局总览](u4-l1-flow-overview.md)（块级排版主循环）。网格本身是「一个被 flow 驱动的 `multi_layouter`」，而单元格内部又回调 `crate::layout_fragment`（即 flow）来排版自身——理解 flow 是前提。

## 2. 前置知识

在进入源码前，先用三条你已经熟悉的事实锚定本讲的语境：

- **Region / Regions 是排版的画布**（见 u2-l2）。一个 `Regions` 描述「当前可用尺寸 + 后续候选高度队列」。网格排版时，整张表格被塞进一串 region（通常每个 region 对应一页的正文区），表格会**逐区域地把自己「挤」进这些区域**，放不下就换区域。
- **排版结果是一棵 Frame 树**（见 u2-l3）。`Fragment = Vec<Frame>`；当内容跨多区域时，`layout_fragment` 返回多帧。表格的最终产物就是「每个区域一帧」的 `Fragment`，每帧是该区域里那张可见的表格片段。
- **flow 用 `multi_layouter` 驱动自定义块**（见 u4-l1、u4-l5）。`BlockElem` 可以挂一个「多帧排版器」函数，flow 在排到这种块时把 `Regions` 交给它，由它自己决定如何跨区域产出多个 frame。表格/网格正是这种块。

CSS Grid 的「轨道尺寸算法（track sizing algorithm）」是理解本讲的最好类比：Typst 的列/行 sizing 与 CSS Grid 的 `auto` / `fr` / 固定尺寸在概念上几乎一一对应，只是 Typst 把它实现成了一套**逐区域、可断裂**的版本。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 所属 crate | 作用 |
| --- | --- | --- |
| `src/grid/mod.rs` | typst-layout | 入口 `layout_grid` / `layout_table`，以及单元格排版 `layout_cell`（含 HACK tag 注入） |
| `src/grid/layouter.rs` | typst-layout | `GridLayouter` 的全部排版逻辑：列解析、逐行排版、region 切换、`finish_region` 组装 |
| `src/rules.rs` | typst-layout | `GRID_RULE` / `TABLE_RULE`：把 layouter 挂到 `BlockElem::multi_layouter` |
| `src/layout/grid/resolve.rs` | typst-library | `grid_to_cellgrid` / `table_to_cellgrid`、`CellGrid`、`Cell`、`Entry`、`Header`/`Footer` 等数据结构 |
| `src/layout/grid/mod.rs` | typst-library | `GridElem::synthesize`：在排版前把元素解析成 `CellGrid` 并缓存 |

一句话记忆分工：**typst-library 负责「把元素摊平成二维单元格矩阵 `CellGrid`」，typst-layout 负责「把这个矩阵排成若干 frame」。**

## 4. 核心概念与源码讲解

### 4.1 从元素到 CellGrid：表格/网格的解析阶段

#### 4.1.1 概念说明

用户在 Typst 里写的 `table(...)` / `grid(...)` 是一棵嵌套的元素树：`TableChild` 可以是 `Header` / `Footer` / `Item`，`Item` 又分为 `Cell` / `HLine` / `VLine`，单元格还可能带 `colspan` / `rowspan`、显式的 `x` / `y` 坐标。这棵树对排版很不友好——排版器想要的是一个**扁平的二维矩阵**：第 `y` 行第 `x` 列是什么、它合并了哪些格子、线段画在哪。

「解析阶段」就是把树摊平成矩阵的过程。它产出的核心数据结构是 `CellGrid`：一个一维的 `entries` 向量（行优先存储），加上列/行轨道尺寸定义、线条列表、表头/表脚信息。合并单元格在矩阵里用 `Entry::Merged { parent }` 指回它的「父单元格」。

注意：这一步**完全不算几何**，只决定「谁在哪里」。高度、宽度都是排版阶段才算的。

#### 4.1.2 核心流程

```
GridElem / TableElem (用户元素树)
        │  Synthesize::synthesize  (排版前自动调用)
        ▼
grid_to_cellgrid / table_to_cellgrid
        │  1. 读取 columns/rows/gutter/fill/align/inset/stroke 样式
        │  2. 把每个 child 摊平，自动定位（auto-index）
        │  3. 处理 colspan/rowspan → 填 Entry::Merged
        │  4. 收集 HLine/VLine → hlines/vlines
        │  5. 确定 Header/Footer 的行范围、是否重复
        ▼
CellGrid { entries, cols, rows, vlines, hlines, headers, footer, has_gutter }
        │  存入 elem.grid 字段（Arc<CellGrid>），供排版阶段读取
```

关键点：解析结果被**缓存在元素自身**（`self.grid = Some(Arc::new(grid))`），所以排版阶段 `layout_grid` 一上来就 `elem.grid.as_ref().unwrap()` 直接取用，不会重复解析。这也是为什么 `CellGrid` 实现了 `Hash`——它要参与 comemo 记忆化。

#### 4.1.3 源码精读

解析发生在 typst-library，由 `Synthesize` trait 触发：

[src/layout/grid/mod.rs:440-450](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/mod.rs#L440-L450) —— `GridElem` 的 `synthesize`：调用 `grid_to_cellgrid` 把自己解析成 `CellGrid`，结果存进 `self.grid` 字段。排版阶段的 `layout_grid` 只读这个字段。

[src/layout/grid/resolve.rs:27-75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L27-L75) —— `grid_to_cellgrid`：读取全部网格样式（`columns` / `rows` / `column_gutter` / `row_gutter` / `fill` / `align` / `inset` / `stroke`），把每个 `GridChild` 翻译成 `ResolvableGridChild`，最终交给 `resolve_cellgrid`。

最终产物的形状：

[src/layout/grid/resolve.rs:657-679](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L657-L679) —— `CellGrid` 结构体：`entries`（一维行优先的单元格矩阵）、`cols` / `rows`（**已含 gutter 轨道**）、`vlines` / `hlines`（线条，按轨道索引分组）、`headers` / `footer`（可重复的表头/表脚）、`has_gutter`（是否有间隙）。

矩阵里每个位置是 `Entry`：

[src/layout/grid/resolve.rs:628-637](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L628-L637) —— `Entry` 枚举：`Cell(Cell)` 表示这里有一个真实单元格；`Merged { parent }` 表示这里被某个合并单元格占据，`parent` 指回它的扁平索引。

> 为什么列/行轨道「已含 gutter」？因为 `CellGrid::new_internal` 在构造时把 gutter 当作**零宽/自动宽的额外轨道**插入到内容轨道之间（见下文 `is_gutter_track` 的偶/奇判定）。这样排版器只需一套统一的「逐轨道」逻辑，不必为 gutter 单开分支。

[src/layout/grid/resolve.rs:844-849](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L844-L849) —— `is_gutter_track`：当网格有 gutter 时，**奇数索引是 gutter 轨道**，偶数是内容轨道。这个简单的奇偶规则贯穿整个 layouter。

#### 4.1.4 代码实践

**实践目标**：用断言理解「自动定位」与「合并单元格」如何反映到 `entries` 矩阵。

**操作步骤**（源码阅读型，无需运行）：

1. 阅读 [src/layout/grid/resolve.rs:2160-2304](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L2160-L2304) 的 `resolve_cell_position`，看一个 `(auto, auto)` 单元格如何用 `auto_index` 计数器找到下一个空位。
2. 假设 2 列网格，依次放入单元格 A、B、C（均自动定位，无合并）。手工推算 `auto_index` 的变化：放 A 前为 0，A 占 `(0,0)` 后变为 1；B 占 `(1,0)` 后变为 2；C 占 `(0,1)` 后……
3. 再假设 D 设置 `rowspan: 2`，验证它会在其下方那个位置写入 `Entry::Merged { parent }`（见 [resolve.rs:1509-1534](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L1509-L1534)）。

**需要观察的现象 / 预期结果**：自动定位按**行优先**填空，`auto_index` 每次至少前进 `colspan` 步；合并单元格会把被覆盖的位置标成 `Merged` 并记下父索引，排版器随后通过 `parent_cell_position` 把它们还原到父单元格。

#### 4.1.5 小练习与答案

**练习 1**：为什么解析结果（`CellGrid`）要实现 `Hash` 并缓存在元素上，而不是每次排版都重新解析？

**参考答案**：因为表格/网格通过 `BlockElem::multi_layouter` 挂进 flow，flow 的排版函数受 `#[comemo::memoize]` 缓存。把解析结果缓存进 `elem.grid`、并让 `CellGrid` 可哈希，能使「相同输入 → 相同 `CellGrid`」这一步被记忆化命中，避免在内省收敛的多次重排中重复做昂贵的摊平/自动定位工作。

**练习 2**：`is_gutter_track` 用「奇数索引即 gutter」的判定的前提是什么？

**参考答案**：前提是 `has_gutter` 为真，且 `CellGrid::new_internal` 在构造轨道时严格按「内容轨道、gutter 轨道、内容轨道、gutter 轨道……」交替插入（最后一条多余的 gutter 会被 `pop` 掉）。只要这个交替不变量成立，偶/奇判定就成立。

---

### 4.2 GridLayouter 的初始化与列尺寸解析：rcols 与 fr 分配

#### 4.2.1 概念说明

拿到 `CellGrid` 后，`GridLayouter` 要解决的头号问题是：**每列多宽？** 这就是 `rcols`（resolved columns，已解析列宽）。列宽有三种来源，对应 `Sizing` 的三个变体：

- `Rel(v)`：相对/固定尺寸（如 `10pt`、`20%`），可直接解析成绝对长度。
- `Auto`：自动尺寸，由该列里**最宽的单元格**决定——需要真的把单元格排版一遍去测量。
- `Fr(v)`：分数尺寸，瓜分「其它列吃剩下的」可用空间，按分数比例分配。

这三者的求解有先后顺序：先把能确定的（`Rel`）确定掉，再测量 `Auto`，最后把剩余空间分给 `Fr`。若 `Auto` 列加起来已经超宽，则反过来对 `Auto` 列做「公平收缩」。

#### 4.2.2 核心流程

`measure_columns` 的算法（CSS Grid track sizing 的简化版）：

```
rel := Σ 已解析的 Rel 列宽
fr  := Σ 所有 Fr 列的分数

available := regions.size.x - rel          # 扣除固定列后的可用宽度

if available >= 0:
    auto, count := measure_auto_columns(available)   # 测量每个 Auto 列
    remaining := available - auto
    if remaining >= 0:
        grow_fractional_columns(remaining, fr)        # 还有富余 → 分给 Fr
    else:
        shrink_auto_columns(available, count)         # 超宽 → 公平收缩 Auto

width := Σ rcols
```

`Fr` 的分配公式很简单：对每个 `Fr(v)` 列，

\[
\text{rcol}_i = v_i \cdot \text{share}(fr, \text{remaining}) = \frac{v_i}{fr} \cdot \text{remaining}
\]

其中 `share` 是 `Fr` 类型提供的方法，等价于「按分数比例切分 `remaining`」。

`Auto` 的「公平收缩」（`shrink_auto_columns`）则是一个迭代过程：反复计算一个 `fair = redistribute / overlarge` 的公平份额，把已经「不需要这么宽」的 `Auto` 列剔除出待分摊集合，剩下的过宽列统一压到 `fair`。

#### 4.2.3 源码精读

入口把元素交给 layouter，再交给 `layout()`：

[src/grid/mod.rs:100-109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L100-L109) —— `layout_grid`：取出已解析的 `CellGrid`，构造 `GridLayouter` 并调用 `.layout(engine)`。`layout_table`（[L112-122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L112-L122)）与之一模一样——在排版层面，`table` 和 `grid` 共用同一个 `GridLayouter`，二者的差别在更上层（`table` 默认带描边/填充/inset）。

构造函数里一个容易忽略但很关键的设置：

[src/grid/layouter.rs:240-295](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L240-L295) —— `GridLayouter::new`：
- [L249-250](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L249-L250) 强制 `regions.expand = (true, false)`——横向填满、纵向不填满。原因写在注释里：测量 `Auto` 行时列宽已定，于是可以开启横向 expand 让单元格按确定的列宽排版。
- [L252-262](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L252-L262) 为每个单元格预分配一个 `Locator`（存进 `cell_locators` 哈希表），保证每个单元格有稳定、唯一的内省身份。
- [L276](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L276) 一次性解析 `is_rtl`，RTL 网格的列序与单元格横向偏移都会用到它。

列解析主体：

[src/grid/layouter.rs:915-957](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L915-L957) —— `measure_columns`：
- [L924-935](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L924-L935) 第一遍扫描：`Rel` 列直接解析并累加进 `rel`，`Fr` 列累加进 `fr`，`Auto` 列先留 0。
- [L938](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L938) `available = regions.size.x - rel`。
- [L945-950](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L945-L950) 测量 `Auto` 后按 `remaining` 的正负分流到「长胖（grow）」或「瘦身（shrink）」。

[src/grid/layouter.rs:967-1099](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L967-L1099) —— `measure_auto_columns`：对每个 `Auto` 列，遍历该列所有单元格，用一个 `pod`（单 region）实际调用 `layout_cell` 排版（[L1081-1089](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1081-L1089)），取「所需宽度」的最大值（[L1090](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1090) `resolved.set_max(...)`）。注意 colspan 只影响「最后一个被跨的 Auto 列」，避免重复计宽。

[src/grid/layouter.rs:1102-1112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1102-L1112) —— `grow_fractional_columns`：对每个 `Fr(v)` 列执行 `*rcol = v.share(fr, remaining)`，即按分数比例瓜分 `remaining`。`fr` 为 0 时直接返回。

[src/grid/layouter.rs:1115-1145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1115-L1145) —— `shrink_auto_columns`：当 `Auto` 列总宽超过 `available` 时的迭代公平收缩——剔除不需要收缩的列，把剩下的统一压到 `fair`。

#### 4.2.4 代码实践

**实践目标**：手算一个 3 列网格的列宽，验证 `measure_columns` 的分流逻辑。

**操作步骤**：

1. 构造一个 `grid(columns: (auto, 1fr, 2fr))`，假设可用宽度 `W = 300pt`，`Auto` 列经测量需要 `90pt`。
2. 按算法推算：`rel = 0`、`fr = 3`、`available = 300`、`auto = 90`、`remaining = 210 >= 0` → 走 `grow_fractional_columns`。
3. 计算 `1fr` 列宽 = \(210 \times \frac{1}{3} = 70\text{pt}\)，`2fr` 列宽 = \(210 \times \frac{2}{3} = 140\text{pt}\)。
4. 再把 `Auto` 列改成需要 `400pt`（超过 `available`），验证走 `shrink_auto_columns` 分支（此时没有其它 Auto 列可分摊，单列直接被压到 `available = 300pt`）。

**需要观察的现象 / 预期结果**：`Fr` 列永远只瓜分「扣掉固定列与 Auto 列之后」的剩余；`Auto` 列在空间充裕时取自然宽度，在拥挤时被公平压缩。总宽 `width = Σ rcols` 在 [L954](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L954) 汇总，是后续行排版与 frame 宽度的依据。**待本地验证**：可在 [measure_columns](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L915-L957) 末尾临时插一条 `eprintln!("rcols={:?}", self.rcols);`，用 `cargo test` 跑表格相关测试观察输出。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GridLayouter::new` 要把 `regions.expand.x` 强制设为 `true`？

**参考答案**：列宽一旦在 `measure_columns` 里确定，单元格排版时就应该**按确定的列宽填满**（横向 expand），而不是让单元格按自然宽度收缩、再让列宽跟着变。纵向不 expand（`false`）则是因为行高是逐区域动态决定的，表格不应把 region 撑满到 `initial.y`（除非有 `Fr` 行，那在 `finish_region` 另算）。

**练习 2**：一个 colspan=2 的单元格横跨「一个 Auto 列 + 一个 Fr 列」，它的宽度会贡献给谁？

**参考答案**：只贡献给其中**最后一个被跨的 Auto 列**（见 [measure_auto_columns:1002-1018](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1002-L1018) 的 `last_spanned_auto_col` 判定），并且会把「其它已解析列已经提供给它的宽度」扣除后再比较（`already_covered_width`，[L1076](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1076)）。`Fr` 列的宽度在测量 `Auto` 时尚未确定，因此先不计入。

---

### 4.3 逐 region 排版行：auto/rel/fr 行与表格断裂

#### 4.3.1 概念说明

列宽是一次性确定的（`rcols`），但**行高不是**——行高取决于内容，而内容能不能放进当前 region 取决于剩余高度。因此行是**逐区域、逐行**地「懒」解析的。`GridLayouter` 维护三类行：

- `Auto` 行：高度由该行单元格决定，**可以跨 region 断裂**（一行表格被切成多段，分布到多帧）。
- `Rel` 行：固定/相对高度，**不可断裂**，但放不下时可以**强制换 region**。
- `Fr` 行：分数高度，**推迟到 `finish_region` 才求解**，瓜分该 region 里未被其它行用掉的高度。

排版主循环逐行处理，遇到放不下的情况就调用 `finish_region` 把当前已排好的行打包成一帧，并推进到下一个 region 继续。最终每个 region 对应一帧，所有帧组成 `Fragment`。`rrows`（resolved rows）记录「每个 region 里各行的实际高度与原始行号」，供后续画线、填色使用。

#### 4.3.2 核心流程

```
layout():
  measure_columns()                  # 先定列宽
  [若重复表脚] prepare_footer 并扣减 region 高度
  y = 0
  while y < rows.len():
      若 y 命中表头范围 → place_new_headers，跳过表头行
      若 y 命中重复表脚 → layout_footer，跳过表脚行
      否则 layout_row(y):
          layout_row_internal:
              若 region 已满且是内容行 → finish_region(false)   # 换区域
              match rows[y]:
                  Auto  → layout_auto_row     # 可断裂
                  Rel   → layout_relative_row # 不可断，可强制换 region
                  Fr    → current.lrows.push(Row::Fr(..))      # 推迟
      y += 1
  finish_region(true)                # 收尾最后一帧
  补排遗漏的 rowspans
  render_fills_strokes()             # 画线、填色，返回 Fragment
```

`finish_region` 是「组装一帧」的核心：遍历当前 region 累积的行（`current.lrows`），其中 `Row::Fr` 此时才用 `v.share(fr, remaining)` 求出高度并真正排版，最后把所有行帧 `push_frame` 叠成该 region 的输出帧，并 `regions.next()` 推进。

#### 4.3.3 源码精读

行类型的内部表示：

[src/grid/layouter.rs:216-224](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L216-L224) —— `Row` 枚举：`Frame(Frame, y, is_last)` 是已排好的行帧（`is_last` 表示这是该行在多区域断裂时的最后一帧）；`Fr(Fr, y, disambiguator)` 是尚未求解的分数行，等 `finish_region` 处理。

[src/grid/layouter.rs:207-212](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L207-L212) —— `RowPiece`：最终记录在 `rrows` 里的行段，含 `height` 与原始行号 `y`。

主循环与换区域时机：

[src/grid/layouter.rs:314-388](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L314-L388) —— `layout`：先 `measure_columns`，处理重复表脚，然后 `while y < rows.len()` 逐行调度，最后 `finish_region(true)` 收尾、补排 rowspans、`render_fills_strokes` 出 `Fragment`。

[src/grid/layouter.rs:425-464](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L425-L464) —— `layout_row_internal`：
- [L434-437](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L434-L437) **换区域的关键守卫**：`unbreakable_rows_left == 0 && regions.is_full() && is_content_row` 时先 `finish_region(false)`。gutter 行不触发换区域（避免在 gutter 上断开），不可断行组（表头/表脚）期间也不换区域。
- [L447-458](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L447-L458) 按 `Sizing` 分派到三种行排版器；`Fr` 行只是 `push(Row::Fr(..))`，不立即排版。

`Auto` 行（可断裂）：

[src/grid/layouter.rs:1149-1233](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1149-L1233) —— `layout_auto_row`：先用 `measure_auto_row` 得到该行在各 region 的高度序列 `resolved: Vec<Abs>`。
- 若只有一帧高度（[L1186-1196](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1186-L1196)）→ `layout_single_row` 排一帧。
- 若有多帧（[L1222-1230](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1222-L1230)）→ `layout_multi_row` 排多帧，**每排完一帧就 `finish_region(false)` 换区域**，把同一行切成多段分到多帧——这就是「跨页表格逐段产出 frame」的实现。

`Rel` 行（不可断、可强制换区域）：

[src/grid/layouter.rs:1429-1464](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1429-L1464) —— `layout_relative_row`：解析出固定高度后排一帧；[L1449-1458](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1449-L1458) 的 `while !fits(height) && may_progress_with_repeats()` 循环：当前 region 放不下就 `finish_region` 跳到下一个 region 重试。`may_progress_with_repeats` 防止在「换区域也不会变高」时死循环（与 u4-l3 的 `may_progress` 思路一致）。

`Fr` 行与 `finish_region` 组装：

[src/grid/layouter.rs:1599-1834](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1599-L1834) —— `finish_region`：
- [L1666-1680](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1666-L1680) 统计已用高度 `used` 与该 region 的 `Fr` 分数总和 `fr`；若有 `Fr` 行且区域高度有限，输出帧高度撑满到 `initial.y`。
- [L1693-1697](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1693-L1697) 遇到 `Row::Fr(v, y, ..)` 时，`remaining = regions.full - used`，`height = v.share(fr, remaining)`，再调 `layout_single_row` 真正排版——这是 `Fr` 行的最终落地点。
- [L1792-1794](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1792-L1794) 把每行帧 `push_frame` 叠到 `output`，并把 `RowPiece` 推进 `rrows`。
- 末尾 [L1797-1805](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1797-L1805) 调 `finish_region_internal` 登记本帧。

[src/grid/layouter.rs:1838-1862](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1838-L1862) —— `finish_region_internal`：把输出帧压进 `self.finished`、把 `rrows` 压进 `self.rrows`，并 **`regions.next()`** 推进到下一个区域。

单行排版如何调用单元格（与 4.4 呼应）：

[src/grid/layouter.rs:1467-1524](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1467-L1524) —— `layout_single_row`：逐列构造 `pod = Region(width, height)`，调用 `layout_cell(...).into_frame()` 得到单元格帧，按列累加 `offset.x` 用 `push_frame` 摆放（[L1501-1516](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1501-L1516)）；RTL 时把 `pos.x` 翻转。

[src/grid/layouter.rs:1527-1582](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1527-L1582) —— `layout_multi_row`：可断裂行的多帧版本，`pod.backlog = &heights[1..]` 把后续高度作为 backlog，`layout_cell` 一次返回多帧，再用 `outputs.iter_mut().zip(fragment)` 把每个单元格的各帧分别叠到对应输出帧上。

#### 4.3.4 代码实践

**实践目标**：亲眼看到一张跨页表格如何被切成多帧，并对应到 `finish_region` 的多次调用。

**操作步骤**：

1. 编写一个会产生跨页表格的 Typst 文档（示例代码）：

   ```typst
   #set page(height: 120pt)
   #table(
     columns: 2,
     [A], [B],
     ..range(40).map(n => [单元格 #n]).flatten(),
   )
   ```

2. 用 typst 编译，观察输出 PDF 中表格被切到第二页（第一页装不下的行进入第二页）。
3. 对照源码追踪：放不下的某行是 `Auto` 行 → `layout_auto_row` → `measure_auto_row` 返回多段高度 → `layout_multi_row` 产多帧 → 每帧后 `finish_region(false)`（[L1228](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1228)）。
4. （可选）在 [finish_region_internal](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1838-L1862) 的 `self.finished.push(output)` 前插入 `eprintln!("region {} rows={:?}", self.finished.len(), rrows);`，重新 `cargo build` 后编译上面的文档，观察每个 region 各排了哪些行段。

**需要观察的现象 / 预期结果**：表格行被分成两组（对应两帧），第一帧的行高之和接近但不超过页面正文高度；`rrows` 里会看到同一个原始行号 `y` 可能出现在多个 region（若该行本身被切断）。**第 4 步需要本地能编译 typst，若环境不具备则标注为「待本地验证」**，前 3 步属于源码阅读型，可直接完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Fr` 行要在 `finish_region` 里才求解高度，而不是像 `Rel` 行那样在 `layout_row_internal` 里立即排版？

**参考答案**：`Fr` 行的高度是「该 region 里其它行用剩下的空间」的按比例分配，必须等同一 region 内所有 `Auto` / `Rel` 行都排完、`used` 统计出来后才能算（`remaining = regions.full - used`）。若提前排版，后面再排的行又会改变 `used`，导致 `Fr` 行高度不自洽。所以 `layout_row_internal` 只把它压成 `Row::Fr(..)` 占位，延迟到 `finish_region` 统一落地。

**练习 2**：`layout_relative_row` 的 `while` 循环里为什么要有 `may_progress_with_repeats()` 这个条件？

**参考答案**：若换到下一个 region 也得不到更高的可用空间（例如处于最后一个区域、或重复表头/表脚占满后剩余高度不变），继续 `finish_region` 只是徒劳，最终会死循环。`may_progress_with_repeats()` 判断「换区域是否真能改善空间」（考虑重复表头/表脚的占用），不能改善时就停止换区域、接受溢出。这与 flow 的 `may_progress`（u4-l3）是同一类防死循环守卫。

---

### 4.4 单元格排版 layout_cell 与手动 tag 注入

#### 4.4.1 概念说明

每个单元格的内容都是一段普通的 Typst `Content`，排版它本质上就是「在一个小 region 里跑一次 flow」。这件事由 `crate::layout_fragment` 完成。但表格单元格有一个特殊需求：**PDF 无障碍（accessibility）与内省（query/outline）需要每个单元格是一个可定位、可查询的元素**，也就是说每个单元格的 frame 里必须有一对 `Tag::Start` / `Tag::End` 标记（见 u2-l4）。

正常情况下，元素在 realize 阶段就会自动获得 tag。但表格/网格单元格的 tag「不知为何被认为对排版有影响」（见源码注释），于是 Typst 采用了一个 **HACK**：让 `layout_cell` 在排版完单元格后，**手动**为 `TableCell` / `GridCell` 生成并插入这对 tag。

更微妙的是，当一个单元格的内容**跨多帧**（跨页单元格）时，单靠在首帧插 tag 会破坏内省顺序——这时要把所有帧都设上同一个 `FrameParent`，让内省器把它们当作一个整体 group，紧贴在父元素的 Start tag 之后展开（这正是 u2-l3 讲过的 `group.parent` + `start/end_insertion` 机制）。

#### 4.4.2 核心流程

```
layout_cell(cell, locator, regions, is_repeated):
  locator.split()
  tags := None
  if cell.body 是 TableCell/GridCell:
      设置 is_repeated，调用 generate_tags 得到 (elem, loc, key)
  fragment := crate::layout_fragment(cell.body, ...)   # 真正排版单元格
  frames := fragment.into_frames()

  if 有 tags:
      if frames 只有 1 帧:
          首帧 prepend(Start) + push(End)              # 简单情况
      else:                                              # 跨多帧
          所有帧 set_parent(FrameParent(loc, Inherit::Yes))  # 转成 group
          首帧 prepend_multiple([Start, End])          # 空内容 tag 对
  return Fragment::frames(frames)
```

要点：

- `is_repeated` 会被写回单元格（`table_cell.is_repeated.set(is_repeated)`），用来区分「这是重复表头里的同一格」——重复单元格需要不同的内省身份（disambiguator）。
- `generate_tags` 用 `hash128(&cell)` 作为 key、`locator.next_location` 分配位置，这与 u2-l4 的 Locator 机制一致。
- 单帧时 tag 直接挂在首帧首尾；多帧时用 `FrameParent` 把多帧绑成一个逻辑 group，保证内省顺序正确。

#### 4.4.3 源码精读

[src/grid/mod.rs:30-85](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L30-L85) —— `layout_cell` 全貌。逐段看：

[src/grid/mod.rs:38-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L38-L41) —— **HACK 注释**原文：理想情况下表格/网格单元格本可以直接标记为 locatable，但「这些 tag 不知为何被认为对排版有影响」，于是用手动生成 tag + layouter 里的一处检查（指 `measure_auto_row` 里 `is_empty_frame` 把「只含 tag 的帧」视作空帧，[layouter.rs:1341-1343](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1341-L1343)）让测试套件通过。

[src/grid/mod.rs:44-52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L44-L52) —— 识别 `TableCell` / `GridCell`，写回 `is_repeated`，调 `generate_tags`。

[src/grid/mod.rs:54-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L54-L55) —— `crate::layout_fragment` 真正排版单元格内容（即 flow），拿回多帧 `Fragment`。

[src/grid/mod.rs:62-65](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L62-L65) —— **单帧**情形：首帧 `prepend(Tag::Start)`、`push(Tag::End)`，`TagFlags { introspectable: true, tagged: true }`。

[src/grid/mod.rs:66-81](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L66-L81) —— **多帧**情形（跨页单元格）：给每一帧 `set_parent(FrameParent::new(loc, Inherit::Yes))`，把它们都变成指向同一父位置的 group 子帧；再在首帧 `prepend_multiple([Start, End])` 放一对「空内容」tag。注释解释了原因：逻辑子帧会被立即插到父元素 Start tag 之后，从而保证内省器里的顺序正确（对应 u2-l3、u3-l5 的 `start_insertion`/`end_insertion`）。

[src/grid/mod.rs:87-96](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L87-L96) —— `generate_tags`：`key = hash128(&cell)`、`loc = locator.next_location(engine, key, span)`、`cell.set_location(loc)`，返回 `(cell.pack(), loc, key)`。

layouter 侧对「只含 tag 的帧」的兼容处理：

[src/grid/layouter.rs:1338-1355](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1338-L1355) —— `measure_auto_row` 里的 `is_empty_frame`：把「只含 `FrameItem::Tag` 的帧」视作空帧，用于决定是否跳过首个区域重新测量。这正是 HACK 注释里说的「layouter 里的一处检查」，它抵消了手动 tag 对 auto 行高度测量的干扰。

最后，`layout_cell` 的调用点在 `measure_auto_columns`（测列宽）、`layout_single_row` / `layout_multi_row`（排整行）、`measure_auto_row`（测行高）等多处，单元格的 `Locator` 由 `cell_locator` 提供：

[src/grid/layouter.rs:298-311](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L298-L311) —— `cell_locator`：取预分配的 locator 做 `relayout()`；若 `disambiguator > 0`（重复表头/表脚中的同一格）再 `split().next_inner(disambiguator)`，让每次重复都得到不同身份。

#### 4.4.4 代码实践

**实践目标**：理解「为何要手动生成 tag」以及「单帧 vs 多帧」两条分支的差别。

**操作步骤**：

1. 阅读 [src/grid/mod.rs:38-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L38-L82) 的 `layout_cell`，对照 HACK 注释。
2. **跟踪单帧分支**：构造一个普通表格 `#table(columns: 2, [a], [b])`，每个单元格内容很短、绝不跨页。此时 `frames` 长度为 1，走 [L62-65](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L62-L65)：tag 直接挂在首帧。
3. **跟踪多帧分支**：把某个单元格改成超长内容使其跨页（例如 `table.cell([...很长的正文...])`），该单元格的 `layout_cell` 会返回多帧，走 [L66-81](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L66-L81)：所有帧被 `set_parent` 绑成 group。
4. 用 `query` 验证可定位性：在文档里写 `#context query(table.cell).len()`，对照单元格数量，确认每个单元格都成了一个可查询元素。

**需要观察的现象 / 预期结果**：第 4 步 `query(table.cell)` 的长度应等于表格里 `table.cell` 的个数，说明手动注入的 `introspectable: true` tag 确实让单元格进入了内省索引；跨页单元格虽然分布在多帧，但因 `FrameParent` 绑定，仍被内省器视为一个整体、只计一次。**步骤 4 依赖本地编译 typst；若不具备则标注「待本地验证」**，前 3 步为源码阅读型。

#### 4.4.5 小练习与答案

**练习 1**：如果不手动注入 tag，而是把 `TableCell` / `GridCell` 声明为 locatable 元素让 realize 自动生成 tag，按 HACK 注释会发生什么？

**参考答案**：注释指出这些 tag「会被认为对排版有影响」，具体表现是：auto 行高度测量会把「只含 tag 的帧」算作有内容，导致本应判为空、跳过首区域重测的单元格被误判为非空，从而得到错误的行高、破坏测试套件。当前做法（手动注入 + `is_empty_frame` 把纯 tag 帧当空帧）是在不改动 locatable 机制的前提下绕开这个干扰。

**练习 2**：跨多帧的单元格为什么要给每一帧都 `set_parent(...)`，而不是只在首帧插 Start/End？

**参考答案**：内省器按帧树深度优先遍历，并在遇到带 `parent` 的 group 时，把它的 tag 整体插到父元素 Start 之后（`start_insertion`，见 u3-l5）。若只在首帧插 tag，后续帧就与这个单元格失去逻辑关联，跨帧单元格会被内省器拆成多个片段、顺序错乱。给每帧设同一 `FrameParent`、再在首帧放一对空内容 tag，内省器就能把它们当成一个整体，正确建立「单元格 → 其全部 frame」的覆盖关系。

**练习 3**：`cell_locator` 里 `disambiguator > 0` 时为什么要再 `split().next_inner(disambiguator)`？

**参考答案**：重复表头/表脚会在每个 region 重新排一遍相同的单元格。如果它们共用同一个 `Location`，内省器就无法区分「第 1 页的表头单元格」与「第 2 页的表头单元格」。用 `disambiguator`（通常等于已完成的 region 数）作为子身份，能让每次重复都得到不同 `Location`，从而可独立查询（参考 u2-l4 的 disambiguator 概念）。

## 5. 综合实践

把本讲四条主线串起来，完成下面这个「跨页表格全链路追踪」任务。

**任务**：给定下面这份 Typst 文档，请按本讲学到的知识，手工/源码追踪它从元素到 PDF 的全过程，并回答 5 个问题。

```typst
#set page(height: 150pt, width: 300pt)
#show table.cell: it => {
  // 你的观察点：每个单元格都会经过这里
  it
}
#table(
  columns: (auto, 1fr, 1fr),
  align: center,
  [名称], [数量], [备注],
  ..range(30).flatmap(n => ([物 #n], [(n*2)], [—])).flatten(),
)
```

需要回答：

1. **解析阶段**：这张表会被 `table_to_cellgrid` 解析成多大的 `entries` 矩阵？`cols` 向量长度是多少（注意有没有 gutter）？
2. **列宽**：`auto` 列宽度由谁决定？两个 `1fr` 列各分到多少（写出 `measure_columns` 的 `available` / `remaining` 计算，假设页宽 300pt、边距各 50pt、`auto` 列测得 40pt）？
3. **跨页**：30 行数据大概率装不下一页。指出负责「把同一行切到多帧」的函数链（从 `layout_auto_row` 到 `finish_region`），并说明 `rrows` 最终会有几个元素（几个 region 就几个元素）。
4. **单元格 tag**：每个 `table.cell` 在 `layout_cell` 里走了单帧还是多帧分支？绝大多数单元格走哪条？为什么 `query(table.cell)` 仍能查到全部单元格？
5. **画线**：表格默认描边。指出 `render_fills_strokes` 里 hline/vline 的绘制顺序（为什么 hline 默认压在 vline 之上），以及 fills 与 lines 谁在更底层。

**预期结果 / 自检**：
- 第 2 题应得到 `available = 200 - 40 = 160`，每个 `1fr` 列 80pt。
- 第 3 题函数链：`layout_auto_row` → `measure_auto_row`（多段高度）→ `layout_multi_row`（多帧）→ 每帧后 `finish_region(false)` → `finish_region_internal`（`regions.next()`）。`rrows` 元素数 = 最终帧数 = 占用页数。
- 第 4 题：绝大多数单元格走**单帧**分支（[L62-65](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L62-L65)）；只有被行断裂切成多帧的少数单元格走多帧分支。全部可查，是因为每帧都注入了 `introspectable: true` 的 tag，跨帧者再靠 `FrameParent` 聚合。
- 第 5 题：vline 先画、hline 后画（[L514](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L514) 与 [L585](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L585) 的注释），按 thickness 排序后 prepend，fills 在 lines 之前（最底层）——见 [L904-908](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L904-L908)。

> 若本地可编译 typst，建议在第 2、3 题用 `eprintln!` 实测验证；否则按源码推导并标注「待本地验证」。

## 6. 本讲小结

- 表格/网格排版是一条两段式流水线：**typst-library 把元素摊平成 `CellGrid`（解析，不算几何）**，**typst-layout 的 `GridLayouter` 把 `CellGrid` 排成若干 frame（排版）**；解析结果缓存在 `elem.grid` 并参与 Hash 以配合 comemo。
- 列宽（`rcols`）一次性求解，三态分明：`Rel` 直接解析、`Auto` 靠实际排版单元格测量、`Fr` 瓜分剩余空间（`v.share(fr, remaining)`）；`Auto` 超宽时走 `shrink_auto_columns` 公平收缩。
- 行高**逐 region 懒解析**：`Auto` 行可跨 region 断裂（`layout_multi_row` + 每帧 `finish_region`），`Rel` 行不可断但可强制换区域，`Fr` 行推迟到 `finish_region` 才用 `regions.full - used` 落地。
- `finish_region` 是「组装一帧」的核心：统计 `used`/`fr`、求解 `Fr` 行、把行帧叠成输出帧、登记 `rrows`、`regions.next()` 推进。跨页表格 = 多次 `finish_region` = 多帧 `Fragment`。
- 单元格排版走 `crate::layout_fragment`（即 flow）；`layout_cell` 用一个 **HACK** 手动为 `TableCell`/`GridCell` 注入 `introspectable` tag，单帧直接挂首尾 tag，多帧用 `FrameParent` 把多帧绑成 group 以修正内省顺序，并在 layouter 侧用 `is_empty_frame` 抵消 tag 对测量的干扰。

## 7. 下一步学习建议

本讲只覆盖了 `GridLayouter` 的「主干」——列解析、逐 region 排行、单元格 tag。表格还有三块较独立的复杂机制没有展开，分别对应单元 u6-l2 的三篇子讲义：

- **rowspans**（`src/grid/rowspans.rs`）：跨行单元格为何要等到所有被跨行排完才排版、`UnbreakableRowGroup` 如何抑制断裂。
- **repeated**（`src/grid/repeated.rs`）：重复表头/表脚的多级状态机（`pending_headers` / `upcoming_headers` / `repeating_headers`），以及 orphan 防护。
- **lines**（`src/grid/lines.rs`）：`hline`/`vline` 的线段生成、stroke 合并与跨 colspan 的断开。

建议阅读顺序：先 u6-l2 把这三块补齐，再回到 `finish_region` 里之前略读的 rowspans 排版循环（[layouter.rs:1707-1790](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/layouter.rs#L1707-L1790)）与 `render_fills_strokes` 的画线细节，那时你会看明白它们如何与主干耦合。如果你对「`Fr` 分数分配」的更通用形式感兴趣，可以接着读 [u6-l5 栈布局 StackLayouter](u6-l5-stack.md)——栈布局的 `Fr` share 与本讲的 `grow_fractional_columns` 是同一套机制在不同主轴上的实例。
