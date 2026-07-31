# 流式布局：stack、grid、columns

## 1. 本讲目标

本讲讲解 Typst 中三个最常用的「流式布局」元素：`stack`（轴向堆叠）、`grid`（二维网格）、`columns`（多列）。它们都把一组子内容按某种规则排进一块区域里，是 u6-l2 讲过的「Region/Regions → Frame」机制的上层用户。

学完本讲你应该能够：

- 说清 `StackElem` 如何用一个 `dir` 字段同时表达「横排」和「竖排」，以及 `Spacing` 与 `Block` 两类子项的区别。
- 看懂 `GridElem` 的字段构成，理解 `Celled<T>` 如何让 `fill`/`align`/`inset`/`stroke` 既可以是单值、也可以是数组或函数。
- 跟踪 `grid/mod.rs` 与 `grid/resolve.rs` 中从「用户传入的单元格/线条/表头表尾」到归一化 `CellGrid` 的完整解析流程，包括轨道（track）如何与 gutter（间距轨）交错。
- 解释 `ColumnsElem` 的 `count`/`gutter`/`balanced` 三字段如何描述多列切分，以及为什么真正的分帧布局发生在 `typst-layout` 而非本 crate。

一个贯穿全讲的关键认知（承接 u5-l4）：**本 crate 只负责「元素定义」和「grid 的归一化解析」，把内容排成 `Frame` 的算法住在 `typst-layout` 里，运行期经 `Routines` 函数指针回调**。因此本讲大量篇幅在「数据如何被整理成可布局的形状」，而不是「像素如何落下」。

## 2. 前置知识

本讲建立在前面几讲之上，先用通俗语言点出最相关的几点：

- **度量原语（u6-l1）**：`Rel<Length>` 表示「百分比 + 绝对长度」（如 `20% + 5cm`）；`Fr` 是「按份数瓜分剩余空间」的分数长度；`Dir`（`ltr/rtl/ttb/btt`）决定排布方向；`Alignment` 用 `+` 合成二维对齐。它们在本讲里反复作为字段类型出现。
- **区域与帧（u6-l2）**：布局的输入是 `Regions`（一串带 `expand` 标志的区域），输出是 `Frame` 帧树（多帧则打包成 `Fragment`）。`stack`/`grid`/`columns` 都把自己拿到的区域进一步切分，再向下布局子内容。
- **`#[elem]` 宏与字段标注（u3-l3）**：`#[required]`（必填）、`#[default(x)]`（带默认）、`#[fold]`（折叠而非覆盖）、`#[parse(...)]`（覆盖参数解析）、`#[variadic]`（收集成 `Vec`）、`#[external]`（仅文档）、`#[internal]`/`#[synthesized]`（内部/合成字段）。本讲的元素字段几乎用全了这些标注。
- **样式链（u4-l1）**：`get_cloned(styles)`/`get_ref(styles)` 从 `StyleChain` 取字段值，`Fold`/`Resolve` 决定取值方式。`GridElem` 的 `inset`/`stroke` 是 `#[fold]`，`align`/`fill` 则是覆盖式。
- **crate 分离与 Routines（u5-l4）**：行为实现拆到 `typst-eval`/`typst-realize`/`typst-layout` 等行为 crate，本 crate 用 `Routines` 函数指针表在运行期回调它们。**布局算法就是被这样回调的典型行为**。

> 术语提示：本讲反复出现「轨道（track）」一词。在 grid 语境里，一行或一列都叫一条 track；`columns`/`rows`/`gutter` 参数本质上都是在声明一组 track 的大小。

## 3. 本讲源码地图

本讲涉及的关键文件（均位于 `crates/typst-library/src/layout/` 下）：

| 文件 | 作用 |
| --- | --- |
| [`stack.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/stack.rs) | `StackElem` 元素定义、`StackChild`（`Spacing`/`Block`）子项枚举。本 crate 里它很「薄」，只描述结构。 |
| [`columns.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/columns.rs) | `ColumnsElem`（`count`/`gutter`/`balanced`）与 `ColbreakElem`（强制分列）定义。同样较薄。 |
| [`grid/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/mod.rs) | `GridElem` 主元素及其字段；`GridCell`/`GridHLine`/`GridVLine`/`GridHeader`/`GridFooter` 子元素；`TrackSizings`、`GridChild`/`GridItem`、`Celled<T>` 等数据类型；以及 `Synthesize` 实现（触发归一化）。 |
| [`grid/resolve.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs) | 把 `GridElem`（或 `TableElem`）解析成归一化的 `CellGrid`：轨道与 gutter 交错、单元格自动定位、跨列跨行（`colspan`/`rowspan`）、表头表尾、线条归属等。 |
| [`container.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/container.rs) | 定义 `Sizing` 枚举（`Auto`/`Rel`/`Fr`），是 grid 轨道大小的统一词汇。 |
| [`spacing.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/spacing.rs) | 定义 `Spacing` 枚举（`Rel`/`Fr`），是 `stack`/`h`/`v` 间距的词汇。 |
| [`mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/mod.rs) | 在 `global()` 中用 `define_elem` 把四个元素注册进标准库。 |

> 真正产出 `Frame` 的算法（`StackLayouter`、`GridLayouter`、列布局等）在 `crates/typst-layout/` 内，本讲只在概念层面提及，不深入。

## 4. 核心概念与源码讲解

### 4.1 StackElem：轴向堆叠

#### 4.1.1 概念说明

`stack` 解决的问题是「沿一条轴，把若干内容依次排开，中间可插间距」。它最巧妙的一点是**用一个 `dir` 字段同时表达横排和竖排**：当 `dir` 是 `ltr`/`rtl` 时它是水平堆叠器，当 `dir` 是 `ttb`/`btt` 时它是垂直堆叠器（默认 `ttb`，即自上而下，所以 `stack` 默认行为接近普通文档流）。

`stack` 的子项只有两类：要么是一段块级内容（`Block`），要么是一段间距（`Spacing`）。这两类共用一个 `StackChild` 枚举，所以你在 `#stack(...)` 里既能传 `[A]`、`rect(...)`，也能直接传 `1cm` 当作间距。

> 为什么本 crate 里 `stack.rs` 这么短？因为「沿轴堆叠 + 分页」的算法属于布局行为，住在 `typst-layout` 里。本 crate 只给出元素的「形状」（字段 + 子项类型），让用户能构造它、让样式能作用它。

#### 4.1.2 核心流程

从用户写 `#stack(dir: ltr, [A], 1cm, [B])` 到最终排好，大致经历：

1. **解析参数**：`#[variadic] pub children: Vec<StackChild>` 把位置参数逐个收集；每个参数经 `cast!` 判定是 `Spacing` 还是 `Content`（见 4.1.3）。
2. **样式取值**：`dir` 与 `spacing` 经 `StyleChain` 取出（可被 `set` 规则覆盖）。
3. **交给布局（typst-layout）**：布局器拿到 `dir`、`spacing`、`children`，沿 `dir.axis()` 方向依次排放 `Block` 子项，相邻两个 `Block` 之间若没有显式 `Spacing` 子项，就插入默认 `spacing`。
4. **分页**：当 `dir` 为竖直方向且堆叠超出区域高度时，沿 u6-l2 的 `Regions` 链分页。

伪代码（布局侧，概念）：

```
axis = dir.axis()              // "horizontal" 或 "vertical"
cursor = 0
for (i, child) in children.enumerate():
    if child is Spacing(s):    cursor += resolve(s, region.size[axis])
    else:                      frame = layout(child, region.at(cursor))
                               cursor += frame.size[axis]
                               if 上一个也是 Block 且无显式间距: cursor += default_spacing
```

#### 4.1.3 源码精读

`StackElem` 的字段定义极简，只有三个：

[stack.rs:25-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/stack.rs#L25-L51) — `StackElem` 结构与字段。`dir` 默认 `Dir::TTB`（自上而下）；`spacing` 是 `Option<Spacing>`（无默认，即不自动加间距）；`children` 用 `#[variadic]` 收集成 `Vec<StackChild>`。

`StackChild` 把「间距」和「内容」统一在一个枚举里：

[stack.rs:54-60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/stack.rs#L54-L60) — 两个变体 `Spacing(Spacing)` 与 `Block(Content)`。

关键在 `cast!` 的输入分支——它决定了「一个位置参数会被理解成什么」：

[stack.rs:71-79](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/stack.rs#L71-L79) — 先尝试把值转成 `Spacing`（`v: Spacing => Self::Spacing(v)`），再尝试转成 `Content`（`v: Content => Self::Block(v)`）。因此 `1cm` 这样的长度会被当作间距，而 `[A]`/`rect(...)` 被当作内容。

`Spacing` 本身是个两变体枚举（承接 u6-l1 的 `Rel`/`Fr`）：

[spacing.rs:129-136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/spacing.rs#L129-L136) — `Spacing::Rel(Rel<Length>)`（绝对/相对父容器）与 `Spacing::Fr(Fr)`（瓜分剩余空间的分数）。

最后，四个元素都在布局模块的 `global()` 里注册（承接 u1-l3）：

[mod.rs:87-90](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/mod.rs#L87-L90) — `define_elem::<StackElem>()`、`GridElem`、`ColumnsElem`、`ColbreakElem` 连续注册。

#### 4.1.4 代码实践

**实践目标**：验证 `stack` 的「双面性」——同一套 children，换 `dir` 即可横排或竖排；并观察 `Spacing` 子项的作用。

**操作步骤**：

1. 写一段 Typst：

   ```typst
   #stack(
     dir: ttb,
     rect(width: 60pt)[A],
     10pt,
     rect(width: 60pt)[B],
   )
   ```

2. 把 `dir: ttb` 改成 `dir: ltr`，再编译。
3. 删掉中间的 `10pt`，观察默认 `spacing` 是否补上（注意 `spacing` 字段默认是 `none`，即不补）。

**需要观察的现象**：

- `ttb` 时两个矩形上下排列，中间有 `10pt` 间距；`ltr` 时左右排列，间距变成水平方向的 `10pt`。
- 删掉 `10pt` 后，由于未设置 `spacing:`，两个矩形应紧贴（无额外间距）。

**预期结果**：`dir` 单字段切换方向成立；显式 `Spacing` 子项（`10pt`）在两个方向都生效；默认不补间距。若本地未装 Typst，可只做源码阅读：对照 4.1.3 的 `cast!` 分支，解释为何 `10pt` 被当作 `StackChild::Spacing` 而非报错。

> 待本地验证：不同 Typst 版本对纯数字/长度作为位置参数的处理一致，但渲染外观以本地 `typst compile` 为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `StackElem` 在本 crate 里只有字段定义，找不到「堆叠算法」的代码？
**答案**：堆叠算法是布局行为，按 crate 分离原则（u5-l4）住在 `typst-layout`，运行期经 `Routines` 回调；本 crate 只提供元素形状与子项类型。

**练习 2**：`#stack([A], 1fr, [B])` 中 `1fr` 会被解析成哪个 `StackChild` 变体？为什么它能让 A、B 分别贴到容器两端？
**答案**：`1fr` 经 `cast!` 先命中 `Spacing` 分支，成为 `StackChild::Spacing(Spacing::Fr(Fr::new(1)))`。`Fr` 表示「瓜分剩余空间」，这唯一的 `1fr` 独占 A 与 B 之间的全部剩余空间，于是 A、B 被推到两端（类似 `h(1fr)` 在段落里的作用，见 spacing.rs 的 `HElem` 文档）。

---

### 4.2 GridElem 与 grid::resolve：轨道与单元格的归一化

#### 4.2.1 概念说明

`grid` 是三个元素里最复杂的，因为它要在二维平面里同时管理「行轨道」「列轨道」「间距轨（gutter）」「单元格跨列跨行」「表头表尾重复」「显式线条」等概念。 Typst 把这些复杂性收敛成一个核心数据结构 **`CellGrid`**：一个完全归一化、可直接喂给布局器的二维网格。

理解 grid 的关键分工：

- **`GridElem`（grid/mod.rs）**：面向用户的元素，字段即用户能写的全部参数（`columns`/`rows`/`gutter`/`inset`/`align`/`fill`/`stroke`/`children`…）。
- **`Synthesize` 步骤**：`GridElem` 标注了 `#[elem(..., Synthesize, Tagged)]`，所以在被布局前会先跑一次 `synthesize`，调用 `grid_to_cellgrid` 把上面那一堆字段整理成一个 `CellGrid`，存进内部的 `grid` 字段（`#[internal] #[synthesized]`）。
- **`CellGrid`（grid/resolve.rs）**：归一化结果。它的 `cols`/`rows` 已把 gutter 交错进去；它的 `entries` 已把自动定位、`colspan`/`rowspan`、表头表尾、空单元格全部展开。

> 为什么要做这一步归一化？因为用户写 grid 的方式高度灵活（单元格可不写坐标、可跨行列、表头可重复跨页……），如果让布局器直接面对原始 children，分支会爆炸。`CellGrid` 把「位置确定」「合并关系确定」这些工作前置完成，布局器就能专注算尺寸与分帧。这也解释了为什么 grid 的解析逻辑在本 crate（数据归一化），而布局算法在 `typst-layout`（纯计算）。

`Celled<T>` 是另一个值得单独理解的设计：`fill`/`align`/`inset`/`stroke` 这类「每个单元格可能不同」的属性，被统一抽象成「单值 / 数组（按列循环）/ 函数 `(x, y) => value`」三种形态，运行期按单元格坐标解析。

#### 4.2.2 核心流程

从 `#grid(columns: 2, [A],[B],[C],[D])` 到 `CellGrid`，流程如下：

1. **参数解析**：`columns: 2` 经 `TrackSizings` 的 `cast!` 变成 `[Auto, Auto]`（`NonZeroUsize` 分支）；四个内容经 `GridChild`/`GridItem` 的 `cast!` 成为 `GridItem::Cell`。
2. **Synthesize 触发**：`Packed<GridElem>::synthesize` 调 `grid_to_cellgrid`。
3. **`grid_to_cellgrid`**：从样式取出 `inset`/`align`/`columns`/`rows`/`gutter`/`fill`/`stroke`，把每个 `GridChild` 映射成 `ResolvableGridChild`（保留 header/footer 的 repeat/level、把 hline/vline 解析成带 `LinePosition` 的中间表示）。
4. **`resolve_cellgrid` → `CellGridResolver::resolve`**：核心循环。维护一个扁平的 `resolved_cells: Vec<Option<Entry>>`（按行主序、每行 `columns` 个槽位），逐个放置单元格：
   - 用 `auto_index` 计数器为「未指定坐标」的单元格找下一个空位（`resolve_cell_position` + `find_next_available_position`）。
   - 处理 `colspan`/`rowspan`：把被跨过的槽位填成 `Entry::Merged { parent }` 指向主单元格。
   - 处理表头/表尾：它们占整行，并记录可重复的行范围。
   - hline/vline 先「挂起」（`pending_hlines`/`pending_vlines`），等行数确定后再校验位置并入列。
5. **`fixup_cells`**：把所有未填充的槽位补成空单元格（这样连用户没写的格子也能被 show 规则与全局样式作用到）。
6. **`CellGrid::new_internal`**：把内容轨道与 gutter 轨道**交错**拼成最终的 `cols`/`rows`，组装 `CellGrid`。

**轨道与 gutter 的交错**是 `CellGrid` 最值得画图的细节。设内容列有 \(k\) 条、存在 gutter，则最终列轨道序列为：

\[
\text{cols} = [c_0,\ g_0,\ c_1,\ g_1,\ \ldots,\ g_{k-2},\ c_{k-1}]
\]

即内容轨与间距轨交替，且末尾那条多余 gutter 会被 `pop` 掉。因此 \(\text{cols.len()} = 2k - 1\)，反过来由 `cols.len()` 求内容列数为 \(k = 1 + \lfloor \text{cols.len()} / 2 \rfloor\)（这正是 `non_gutter_column_count` 的算法）。行同理。

因为 gutter 被编进了同一个下标空间，**偶数下标是内容轨、奇数下标是 gutter 轨**。于是取单元格时要做下标换算（见 4.2.3 的 `entry()`）。

#### 4.2.3 源码精读

先看 `GridElem` 的整体声明与最核心的几个字段：

[grid/mod.rs:180-198](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/mod.rs#L180-L198) — 注意 `#[elem(scope, since = "forever", Synthesize, Tagged)]`：`scope` 表示有子作用域（`grid.cell` 等），`Synthesize` 表示布局前要跑归一化，`Tagged` 表示可被内省（query）。`columns`/`rows` 字段类型是 `TrackSizings`。

`gutter` 是个「语法糖」字段——它本身不存值（`#[external]`），只在 `#[parse]` 里把 `gutter:` 拆给 `column-gutter`/`row-gutter`：

[grid/mod.rs:200-220](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/mod.rs#L200-L220) — `gutter` 标 `#[external]`（仅文档可见），真正的 `column_gutter`/`row_gutter` 用 `#[parse(...)]` 各自从 `column-gutter`/`row-gutter` 取值，并回退到 `gutter`。这正是 u3-l3 讲过的「`#[external]` 配 `#[parse]`」模式。

`inset`/`align`/`fill`/`stroke` 都用 `Celled<T>` 包裹，且 `inset`/`stroke` 标了 `#[fold]`：

[grid/mod.rs:236-251](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/mod.rs#L236-L251) — `inset: Celled<Sides<...>>` 标 `#[fold]`，`align: Celled<Smart<Alignment>>`。

合成的内部字段 `grid`，存放归一化结果：

[grid/mod.rs:410-419](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/mod.rs#L410-L419) — `#[internal] #[synthesized] pub grid: Arc<CellGrid>`，加上 `#[variadic] pub children: Vec<GridChild>`。`Synthesize` 阶段会填上 `grid`。

`#[scope]` 块挂载五个子元素，使它们以 `grid.cell`/`grid.hline`/… 形式出现（承接 u3-l4 的 `#[scope]`）：

[grid/mod.rs:422-438](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/mod.rs#L422-L438) — `GridCell`/`GridHLine`/`GridVLine`/`GridHeader`/`GridFooter`。

**Synthesize 实现**——这是 grid 解析的入口：

[grid/mod.rs:440-450](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/mod.rs#L440-L450) — `synthesize` 调 `grid_to_cellgrid(self, engine, styles)?`，把结果 `Arc` 起来塞进 `self.grid`。返回 `SourceResult`，意味着解析过程中可报带 span 的错误（如单元格坐标冲突）。

`TrackSizings` 解释了 `columns: 3` 为何等价于 `(auto, auto, auto)`：

[grid/mod.rs:452-462](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/mod.rs#L452-L462) — `cast!` 三个分支：单值 `Sizing` → 一条轨道；`NonZeroUsize` → 那么多条 `Auto`；`Array` → 逐项转换。

`Celled<T>` 的三态定义与按坐标解析：

[grid/mod.rs:889-922](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/mod.rs#L889-L922) — `Value`（所有单元格相同）、`Func`（闭包 `(x, y) => v`）、`Array`（按列循环，用 `x % array.len()` 取）。`resolve(engine, styles, x, y)` 是统一的取值入口。

现在进入 `grid/resolve.rs`。入口函数 `grid_to_cellgrid`：

[grid/resolve.rs:27-75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L27-L75) — 逐字段从样式取值（`inset.get_cloned`、`align.get_ref`、`columns.get_ref`、`stroke.resolve(styles)` 等，承接 u4-l1），把 `tracks`/`gutter` 组成 `Axes<&[Sizing]>`，把 children 映射成 `ResolvableGridChild`，最后调 `resolve_cellgrid(...)` 并用 `.trace(...)` 把错误关联回 `grid` 调用点（承接 u5-l3）。

`CellGrid` 的结构定义：

[grid/resolve.rs:657-679](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L657-L679) — 字段：`entries: Vec<Entry>`（扁平的单元格/合并项）、`cols`/`rows`（含 gutter 的轨道）、`vlines`/`hlines`（按轨道分组的线条）、`headers`/`footer`（可重复的表头表尾）、`has_gutter`（是否含 gutter，决定下标换算）。

**轨道交错的核心实现**在 `CellGrid::new_internal`：

[grid/resolve.rs:693-756](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L693-L756) — 先算内容列数 `num_cols = tracks.x.len().max(1)`；行数取「给定行数」与「按单元格数算出的所需行数」的较大值；`get_or` 用「取指定下标，否则取最后一个，否则取默认」实现轨道大小的「末尾重复」语义；随后两段循环分别把内容轨与 gutter 轨交错 push 进 `cols`/`rows`，最后 `pop` 掉多余末尾 gutter（740–744 行）。

**带 gutter 时的下标换算**在 `entry()`：

[grid/resolve.rs:761-778](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L761-L778) — 有 gutter 时，仅当 `x`、`y` 均为偶数才是内容单元格，其扁平下标为 \((y/2) \cdot c + (x/2)\)，其中 \(c = 1 + \text{cols.len()}/2\) 是内容列数；奇数下标是 gutter，返回 `None`。

单元格的**自动定位**算法：

[grid/resolve.rs:2160-2304](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L2160-L2304) — `resolve_cell_position` 按 `(cell_x, cell_y)` 的 `Smart` 组合分四类处理：全 `Auto`（行主序找下一个空位）、指定列（沿该列向下找）、指定行（沿该行找）、全指定（直接算下标，并检查与表头表尾冲突）。

[grid/resolve.rs:2313-2372](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L2313-L2372) — `find_next_available_position` 跳过已占用槽位、表头行、表尾区域，返回首个可用下标。

`Sizing` 枚举（轨道大小词汇，承接 u6-l1）：

[container.rs:470-491](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/container.rs#L470-L491) — `Auto`（贴合内容）、`Rel(Rel)`（固定/相对）、`Fr(Fr)`（瓜分剩余）。`cast!` 还接受裸 `auto`（`AutoValue => Self::Auto`）。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：跟踪一个 2×2 网格如何被解析成 `CellGrid`，亲手走一遍轨道与单元格的归一化。

**操作步骤**：

1. 想象用户写了：

   ```typst
   #grid(columns: 2, [A], [B], [C], [D])
   ```

2. 对照源码逐步推导（纯源码阅读，无需运行）：

   - **第 1 步：`columns` 字段**。`2` 是 `NonZeroUsize`，命中 [TrackSizings 的 cast!](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/mod.rs#L456-L462) 的 `count: NonZeroUsize => Self(smallvec![Sizing::Auto; count.get()])`，得 `TrackSizings([Auto, Auto])`。`rows` 未给，为空。
   - **第 2 步：children**。四个 `[X]` 经 [GridItem 的 TryFrom](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/mod.rs#L528-L572)，既非 hline/vline，也不是现成 `GridCell`，于是走 `unwrap_or_else` 分支，各自包成 `GridItem::Cell(GridCell::new(...))`。
   - **第 3 步：进入 `Synthesize`** → [`grid_to_cellgrid`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L27-L75) → `resolve_cellgrid` → [`CellGridResolver::resolve`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L1009-L1130)。
   - **第 4 步：放置单元格**。`columns = 2`，四个单元格均无坐标（全 `Auto`），[`resolve_cell_position`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L2184-L2217) 依次给它们下标 `0, 1, 2, 3`（行主序），`auto_index` 每次加 `colspan(=1)`。无 `colspan`/`rowspan`，故无 `Merged`。
   - **第 5 步：组装 `CellGrid`**。进入 [`CellGrid::new_internal`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L693-L756)：`num_cols = max(2, 1) = 2`；`entries.len()=4`，`given=0`，`needed = 4/2 + 0 = 2`，故 `num_rows = max(0, 2) = 2`；`has_gutter = false`（未给 gutter）。于是 `cols = [Auto, Auto]`、`rows = [Auto, Auto]`、`entries = [Cell(A), Cell(B), Cell(C), Cell(D)]`。

3. （可选）把 `columns: 2` 改成 `columns: (60pt, 1fr)` 并加 `gutter: 8pt`，重走第 5 步：此时 `has_gutter=true`，`cols` 应交错成 `[Rel(60pt), Rel(8pt), Rel(1fr)]`（末尾 gutter 被 pop）。

**需要观察的现象 / 预期结果**：

- 无 gutter 时 `cols.len() == num_cols`，`entry(x,y)` 直接用 `y * cols + x` 取单元格。
- 有 gutter 时 `cols.len() == 2*num_cols - 1`，偶数下标才是内容轨。
- 2×2 四格最终落在 `entries[0..4]`，正好对应视觉上的左上、右上、左下、右下。

> 待本地验证：可在 `grid_to_cellgrid` 入口加一条 `eprintln!("cols={:?}", grid.cols)`（仅本地调试，勿提交），编译一个最小 `.typ` 观察打印。注意修改源码仅用于学习，本讲不要求改源码即可完成阅读型实践。

#### 4.2.5 小练习与答案

**练习 1**：`#grid(columns: 3, [A])` 只给了一个单元格，最终 `CellGrid` 有几行几列？空格子会被丢弃吗？
**答案**：`num_cols = 3`；`entries.len()=1`，`needed = 1/3 + (1%3).clamp(0,1) = 0 + 1 = 1`，`num_rows = max(0,1) = 1`。但 [`fixup_cells`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L1666-L1700) 会按 `columns * tracks.y.len()` 补齐——这里 `rows` 为空（`tracks.y.len()=0`），故仅保证至少放下已有单元格。空位不会真被丢弃：用户没写的格子会被补成默认空单元格，以便 show 规则与全局 `fill`/`stroke` 仍能作用其上。

**练习 2**：为什么 `gutter` 字段用 `#[external]` 而不是普通字段？
**答案**：`gutter` 是 `column-gutter` 与 `row-gutter` 的共用语法糖。用 `#[external]` 让它出现在文档里、能被用户写，但不在元素上占存储；真正的两个字段在 `#[parse]` 里各自取值并回退到 `gutter`（见 [grid/mod.rs:212-220](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/mod.rs#L212-L220)）。这是 u3-l3「`#[external]` + `#[parse]`」组合的典型用法。

**练习 3**：一个 `Celled<Smart<Alignment>>` 取值为数组 `(left, center, right)`，在一个 5 列网格里各列的对齐分别是什么？
**答案**：`Celled::Array` 用 `x % array.len()` 取值（见 [grid/mod.rs:915-919](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/mod.rs#L915-L919)）。`array.len()=3`，故列 0→`left`、1→`center`、2→`right`、3→`left`（3%3=0）、4→`center`（4%3=1），即数组按列循环。

---

### 4.3 ColumnsElem：在区域内切分多列

#### 4.3.1 概念说明

`columns` 解决的问题是「把一块内容排成 N 列」，常见于报刊式版面。它和 `grid` 的区别在于：`columns` 不要求你预先把内容分进格子，而是让一段连续的正文**自然流动**，一列排满后自动续到下一列（类似 CSS 的 multi-column）。

`ColumnsElem` 只暴露三个配置：

- `count`：列数（`NonZeroUsize`，默认 2）。
- `gutter`：列间距（`Rel<Length>`，默认 `0.04` 即容器宽度的 4%）。
- `balanced`：是否「平衡」各列高度（默认 `false`）。

外加一个配套元素 `ColbreakElem`（`colbreak`），用于强制断到下一列。

> 与 `grid`/`stack` 一样，`ColumnsElem` 在本 crate 里也是「薄」定义：把区域切成多列、让内容流动、平衡高度的算法都在 `typst-layout`。本 crate 只描述「用户想要什么」。

#### 4.3.2 核心流程

`columns` 切分区域的直觉模型（布局侧，概念）：

设容器可用宽度为 \(W\)、列数为 \(n\)、列间距为 \(g\)（`gutter` 解析后的绝对值）。列与列之间有 \(n-1\) 条 gutter，故单列宽度为：

\[
\text{col\_width} = \frac{W - (n-1)\cdot g}{n}
\]

默认 `gutter = 0.04\,W` 时，对 \(n=2\)：\(g = 0.04W\)，单列宽 \(= (W - 0.04W)/2 = 0.48W\)。

流程概念：

1. 取出 `count`/`gutter`/`balanced`。
2. 把当前区域沿宽度切成 `count` 个子区域（宽度按上式，高度继承）。
3. 把 `body` 在这些子区域里**顺序流动**布局：第一列排满（或遇到 `colbreak`）后续到第二列。
4. 若 `balanced`，则尝试调整断点使各列高度接近相等。
5. 多个区域（分页）时，按 u6-l2 的 `Regions` 链继续。

`colbreak` 的 `weak` 字段：当为 `true` 且当前列已为空时跳过（类似 `pagebreak(weak: true)`）。

#### 4.3.3 源码精读

`ColumnsElem` 的完整定义：

[columns.rs:58-95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/columns.rs#L58-L95) — `count`（`#[positional]`、默认 2）、`gutter`（默认 `Ratio::new(0.04).into()`，即 4%，承接 u6-l1 的 `Rel<Length>`）、`balanced`（默认 `false`）、`body`（`#[required]`）。

`gutter` 字段尤其值得注意——它把「相对父容器宽度」的语义直接编码进默认值：

[columns.rs:65-76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/columns.rs#L65-L76) — `#[default(Ratio::new(0.04).into())]`。`Ratio` 经 `.into()` 成为 `Rel<Length>`（`rel` 分量 = 4%，`abs` 分量 = 0），布局时 `relative_to(容器宽)` 解析为绝对值（u6-l1）。

`ColbreakElem`：

[columns.rs:120-126](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/columns.rs#L120-L126) — 仅一个 `weak: bool` 字段。文档说明它在单列布局或最后一列时退化为「分页」行为。

四个元素在布局模块统一注册（与 4.1.3 同一处）：

[mod.rs:87-90](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/mod.rs#L87-L90) — `ColumnsElem`、`ColbreakElem` 与 `StackElem`/`GridElem` 连续 `define_elem`。

> 文档中还提到一个重要区分（[columns.rs:28-35](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/columns.rs#L28-L35)）：**整篇文档级别的多列应优先用 `page(columns: ...)`**，而非把全部内容包进 `#columns(...)`。因为页面级列布局直接作用在 page 层，能让 `pagebreak`、脚注、行号等正确工作。本讲的 `ColumnsElem` 更适合局部多列容器。

#### 4.3.4 代码实践

**实践目标**：理解 `count`/`gutter` 如何决定每列宽度，并观察 `colbreak` 与 `balanced` 的效果。

**操作步骤**：

1. 写一段：

   ```typst
   #set page(width: 200pt, height: 120pt)
   #columns(2, gutter: 8pt)[
     #lorem(40)
   ]
   ```

2. 按本节的宽度公式手算：\(W \approx 200pt\)（忽略页边距），\(g=8pt\)，\(n=2\)，单列宽 \(\approx (200-8)/2 = 96pt\)。
3. 把 `gutter: 8pt` 删掉，改用默认（4%），重算：\(g=0.04\times 200=8pt\)，结果应与上一步接近。
4. 加 `balanced: true`，观察各列高度是否更均衡。
5. 在文中插入 `#colbreak()`，观察是否强制断到第二列。

**需要观察的现象 / 预期结果**：

- 默认 `gutter`（4%）与显式 `8pt` 在 200pt 宽下外观接近，印证「4% 是相对容器宽度」。
- `balanced: true` 后，原先「第一列很长、第二列很短」会变得接近等高。
- `#colbreak()` 把其后内容推到下一列。

> 待本地验证：精确列宽受页边距与 `body` 自然高度影响，公式只是近似；以本地 `typst compile` 渲染为准。若仅做源码阅读，可对照 [columns.rs:65-76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/columns.rs#L65-L76) 解释「默认 gutter 为何是 4%」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ColumnsElem` 默认 `gutter` 用 `Ratio(0.04)` 而不是一个固定 `pt` 值？
**答案**：用 `Ratio` 让列间距**随容器宽度缩放**——宽页面间距大、窄容器间距小，比例感稳定。`Ratio` 经 `Rel<Length>` 在布局时 `relative_to(容器宽)` 解析为绝对值（u6-l1）。

**练习 2**：`ColbreakElem` 在「单列布局」时会怎样？
**答案**：根据其文档（[columns.rs:98-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/columns.rs#L98-L101)），单列或最后一列时它退化为分页（`pagebreak`）行为；`weak: true` 时若当前列已空则跳过。

---

## 5. 综合实践

把三个元素串起来，做一个「源码阅读 + 现象对照」的综合任务：

**任务**：用一个 `grid` 模拟简易多列布局，并与原生 `columns` 对比，体会二者分工。

1. 写两段等价意图的 Typst：

   ```typst
   #let text-body = lorem(30)

   // 方案 A：grid，每格一段
   #grid(
     columns: 2,
     gutter: 8pt,
     text-body,
     text-body,
   )

   // 方案 B：columns，让正文自然流动
   #columns(2, gutter: 8pt)[
     #text-body #text-body
   ]
   ```

2. **源码追踪（重点）**：
   - 方案 A 走 4.2 的流程：两个单元格被自动定位到 `entries[0]`、`entries[1]`（同一行的两列），`gutter: 8pt` 使 `cols = [Auto, Rel(8pt), Auto]`。每段内容被**钉死**在自己的格子里，不会跨格流动。
   - 方案 B 走 4.3 的流程：`body` 是一段连续内容，在两列里**自然流动**，第一列排满后续到第二列。
3. **观察对比**：方案 A 两段文字各自独立、互不影响高度；方案 B 文字连成一体、按可用高度自动分列。这正是 `grid`（结构化分格）与 `columns`（连续流动）的本质区别。
4. **进阶**：在方案 A 里把第二格换成 `grid.cell(colspan: 2)[...]`，重走 [`resolve_cell_position`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L2160-L2304)：此时 `colspan=2` 占满整行，被跨过的槽位填 `Entry::Merged { parent }`，并触发 [1412-1419 行的 colspan 越界检查](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L1412-L1419)。

> 待本地验证：渲染外观以本地编译为准；源码追踪部分可独立完成，是本综合实践的核心。

## 6. 本讲小结

- `StackElem` 用单个 `dir` 字段统一横排/竖排，子项只有 `Spacing` 与 `Block` 两类，由 `cast!` 的分支顺序决定一个位置参数被理解成间距还是内容；堆叠算法本身在 `typst-layout`。
- `GridElem` 是三者中最复杂的，靠 `Synthesize` 步骤把灵活的用户输入归一化为 `CellGrid`：自动定位、`colspan`/`rowspan`、表头表尾、显式线条全部在 [`grid/resolve.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs) 里前置处理。
- `CellGrid::new_internal` 把内容轨道与 gutter 轨道**交错**拼接（`cols.len() = 2k-1`），偶数下标为内容、奇数为 gutter；`entry()` 据此做下标换算。
- `Celled<T>` 把「逐单元格可变」的属性（`fill`/`align`/`inset`/`stroke`）统一成「单值 / 数组按列循环 / 函数 `(x,y)=>v`」三态，由 `resolve(engine, styles, x, y)` 按坐标取值。
- `ColumnsElem` 只描述「想要几列、间距多大、是否平衡」，真正的切分与流动布局在 `typst-layout`；整篇文档级多列应优先用 `page(columns: ...)`。
- 贯穿全讲的 crate 分离认知：本 crate 提供**元素形状与数据归一化**，`Frame` 产出算法经 `Routines` 回调到 `typst-layout`（u5-l4）。

## 7. 下一步学习建议

- **向下深入布局算法**：阅读 `crates/typst-layout/` 中 `GridLayouter`、stack 与 columns 的布局实现，看 `CellGrid` 如何被转成 `Frame`/`Fragment`，并体会 `Regions` 分页循环（承接 u6-l2）。可参考 `typst-layout` 的学习手册。
- **横向联系表格**：`table` 元素与 `grid` 共享同一套解析管线——[`table_to_cellgrid`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/grid/resolve.rs#L79-L127) 与 `grid_to_cellgrid` 几乎对称，对比阅读能加深对 `ResolvableCell` trait 抽象的理解。
- **回到内省**：`GridElem` 标注了 `Tagged`，意味着 `grid.cell.where(x: 0)` 这类选择器能工作（承接 u4-l2 的 `Selector`）。学完 u9（内省）后可回看 `GridCell` 的 `x`/`y` 字段如何被用于按位置批量设置样式。
- **下一讲（u6-l4）**：进入 `PageElem`/`PlaceElem`/变换元素，把本讲的「区域内排布」推广到「整页配置与绝对/相对定位」。
