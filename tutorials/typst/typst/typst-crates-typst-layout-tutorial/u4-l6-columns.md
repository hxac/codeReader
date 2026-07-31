# 列布局 columns 与列平衡

## 1. 本讲目标

本讲是「流式（块级）布局」单元的收尾篇，专讲多列排版。学完后你应当能够：

- 说清 `ColumnsElem` 是怎么通过 show 规则挂到 `layout_columns`、再走进 `layout_flow` 的。
- 手算给定页宽、列数、gutter 时的每列宽度，并解释 RTL 文字方向如何改变列的排列顺序。
- 说清「列平衡（balanced）」要解决什么问题，以及它如何用「反复测量 + 重排」收敛到各列等高。
- 解释 `column_balancing_height` 这个字段在 `compose` ↔ `distribute` 之间扮演的「测量标尺」角色。
- 理解为什么多列排版不能简单地「把 region 缩成列宽就完事」——parent-scoped 浮动体让各列必须能够互相交互。

本讲承接 u4-l4（compose 与浮动体/脚注），是 flow 单元的最后一讲。

## 2. 前置知识

本讲默认你已经掌握以下概念（均在 u2、u4 前几讲建立）：

- **Regions / Region（pod）**：排版的「可用画布」抽象。`Regions` 带一个后续候选高度的 `backlog` 队列；`Region` 是它的单区域退化（见 u2-l2）。
- **flow 三段式骨架**：`configuration`（一次性求配置）→ `collect`（把 `Pair` 收成 `Child`）→ 主循环里每个区域调一次 `compose`（见 u4-l1）。
- **`Stop::Relayout(scope)` 控制流**：`compose` 内部用检查点（checkpoint）+ `Relayout` 来消化「加一个浮动体就缩小区域、整片重排」（见 u4-l4）。
- **`PlacementScope::Column` vs `PlacementScope::Parent`**：浮动体的作用域——列级还是页面/父级（见 u4-l2、u4-l4）。
- **comemo 记忆化与 `_impl` 模式**：公开薄封装把 `Engine` 拆成 tracked 参数后调用带 `#[comemo::memoize]` 的 `_impl`（见 u2-l1）。

一个直觉：多列排版本质上是在一个「页面/容器」内部，把原本一个整宽的排版区域，纵向切成 `n` 个等宽的窄条（列），内容先填满第 1 列，溢出再到第 2 列，依此类推。难点不在「切」，而在两点——(1) 各列等高（平衡）；(2) 跨列的浮动体（比如一篇论文的标题要横跨所有列）。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `src/flow/mod.rs` | 列布局入口 `layout_columns`、列宽计算 `configuration`、配置结构 `ColumnOptions` / `ColumnConfig` |
| `src/flow/compose.rs` | 多列区域的构造与拼装 `page_contents`、单列排版 `column`、列平衡触发点、parent-scoped 浮动体的跨列空间计算 `float` |
| `src/flow/distribute.rs` | 把 `balancing_target` 当作「测量标尺」限制单列填入高度 |
| `src/rules.rs` | `COLUMNS_RULE`：把 `ColumnsElem` 挂到 `layout_columns` |
| `crates/typst-library/src/layout/columns.rs` | `ColumnsElem` 元素定义（`count` / `gutter` / `balanced` 字段与默认值） |

## 4. 核心概念与源码讲解

### 4.1 列布局的入口：ColumnsElem、layout_columns 与 ColumnOptions

#### 4.1.1 概念说明

`ColumnsElem` 是用户写 `#columns(2)[...]` 或 `#set page(columns: 2)` 时产生的元素。它本身不做排版，只是一个「声明」：把 body 分成 `count` 列、列间留 `gutter`、是否 `balanced`。真正干活的是 typst-layout 里的 `layout_columns`。

回顾 u1-l3 的结论：typst-layout 的门面里**没有** `layout_grid`、也**没有** `layout_columns` 这样的导出符号。这些 layouter 是通过 `rules.rs` 的 `register`，以 show 规则的形式挂到 `Target::Paged` 上的。`ColumnsElem` 走的就是这条路。

#### 4.1.2 核心流程

```
ColumnsElem
   │  COLUMNS_RULE（BlockElem::multi_layouter 挂 layout_columns）
   ▼
layout_columns(elem, engine, locator, styles, regions)
   │  把 elem 的 count/balanced/gutter 打包成 ColumnOptions
   ▼
layout_fragment_impl(... column: ColumnOptions)   ← #[comemo::memoize]
   │  realize → layout_flow(... column, mode)
   ▼
configuration(...)  ← 本讲 4.2
   │  把 ColumnOptions 解析成 ColumnConfig（含具体宽度）
```

关键点：`layout_columns` 与普通的 `layout_fragment` 共用同一个 `layout_fragment_impl`，区别只在于传入的 `ColumnOptions`。普通片段传的是「单列、不平衡、零间距」的默认值，而 `layout_columns` 传用户指定的列参数。

#### 4.1.3 源码精读

先看元素定义。`ColumnsElem` 有三个可配字段，默认值都很重要：列数默认 2、gutter 默认为**宽度的 4%**（`Ratio::new(0.04)`）、`balanced` 默认 `false`：

[crates/typst-library/src/layout/columns.rs:58-95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/columns.rs#L58-L95) —— `ColumnsElem` 结构与三个字段的默认值（`count=2`、`gutter=0.04`、`balanced=false`）。

再看注册。`COLUMNS_RULE` 用 `BlockElem::multi_layouter` 把 `layout_columns` 挂到元素上，注册到 `Target::Paged`：

[src/rules.rs:678-680](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L678-L680) —— `COLUMNS_RULE` 把 `ColumnsElem` 的 body 包成一个带 `multi_layouter` 的 `BlockElem`，layouter 指向 `crate::flow::layout_columns`。

最后是入口函数本身。注意它的文档注释——这句注释是本讲 4.5 节的纲领：

[src/flow/mod.rs:82-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L82-L111) —— `layout_columns` 把 `elem.count/balanced/gutter` 读出来塞进 `ColumnOptions`，其余参数照搬 `Engine`，调用共享的 `layout_fragment_impl`。

对比一下普通片段入口传入的「平凡列配置」，你就明白两者的同构关系：

[src/flow/mod.rs:74-78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L74-L78) —— `layout_fragment` 传 `count=1, balanced=false, gutter=0`，相当于「退化的单列」。

`ColumnOptions` 本身只是个传输参数的哈希结构（参与 comemo 缓存键）：

[src/flow/mod.rs:362-371](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L362-L371) —— `ColumnOptions` 三个字段：`count: NonZeroUsize`、`balanced: bool`、`gutter: Rel<Abs>`。

#### 4.1.4 代码实践

**目标**：确认 `ColumnsElem → layout_columns` 这条挂载链确实成立。

**步骤**：
1. 打开 `src/rules.rs`，找到 `COLUMNS_RULE`（约 678 行）和它的 `register` 调用（约 83 行）。
2. 打开 `src/flow/mod.rs` 的 `layout_columns`（87 行），确认它的签名第 1 个参数是 `&Packed<ColumnsElem>`。
3. 用 grep 在 `src/flow/mod.rs` 里搜 `layout_fragment_impl`，确认 `layout_columns` 与 `layout_fragment` 调用的是**同一个** `_impl` 函数。

**预期结果**：你会看到两个入口唯一的差别就是 `ColumnOptions` 的实参——一个来自用户元素，一个是写死的单列默认值。这就解释了为什么「列布局」不需要单独的排版引擎，它只是 flow 的一个配置分支。

#### 4.1.5 小练习与答案

**练习 1**：为什么门面 `lib.rs` 里没有 `pub fn layout_columns`，用户写的 `#columns(2)` 却能正常排版？

**参考答案**：因为 `layout_columns` 通过 `COLUMNS_RULE` 以 `BlockElem::multi_layouter` 的形式挂到了 `ColumnsElem` 上。flow 引擎在排到一个带 `multi_layouter` 的块时，会直接调用该 layouter（即 `layout_columns`），而不需要它出现在公共门面里。这是「挂 layouter 型」show 规则的典型用法（见 u1-l3、u7-l1）。

**练习 2**：如果用户写 `#columns(1)[...]`，会和普通的 `#block` 排版有差别吗？

**参考答案**：几乎没有。`count=1` 时，`page_contents` 会走单列快车道（见 4.3），且 `gutter` 公式里 `(count-1)=0` 不产生间距。它就是一次带平凡 `ColumnOptions` 的 `layout_fragment`。

---

### 4.2 列宽计算：configuration 与 ColumnConfig

#### 4.2.1 概念说明

`ColumnOptions` 是「用户意图」（相对值，比如 gutter 是宽度的 4%），`ColumnConfig` 是「排好版的实际参数」（绝对值，比如 gutter 解析成具体的 pt）。把前者翻译成后者的，是 `configuration` 函数。这一步会算出每列的具体宽度——这是整列排版的基础尺寸。

#### 4.2.2 核心流程

`configuration` 在 `layout_flow` 开头被**一次性**调用，产出一个贯穿整个 flow 的 `Config`。其中列的部分做三件事：

1. 处理「宽度无限」的退化情况：如果当前 region 宽度不是有限值（例如在某些 show 规则或内联测量场景），强制 `count = 1`。
2. 把相对的 `gutter` 用 region 的基准宽度解析成绝对值。
3. 套列宽公式算出每列宽度，并读出文字方向 `dir`。

列宽公式为：

\[
\text{width} = \frac{W - g \cdot (n - 1)}{n}
\]

其中 \(W\) 是 region 的可用总宽（`regions.size.x`），\(g\) 是解析后的 gutter，\(n\) 是列数。直觉：\(n\) 列之间有 \(n-1\) 条缝隙，先把缝隙总宽扣掉，再均分给各列。

#### 4.2.3 源码精读

[src/flow/mod.rs:240-265](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L240-L265) —— `configuration` 中构造 `ColumnConfig` 的片段：无限宽退化、gutter 解析、列宽公式、读 `dir`。

关键三行（精简）：

```rust
let gutter = column.gutter.relative_to(regions.base().x);
let width = (regions.size.x - gutter * (count - 1) as f64) / count as f64;
let dir = shared.resolve(TextElem::dir);
```

注意 `gutter` 是用 `regions.base().x` 来解析的（见 u2-l2：`base() = (size.x, full)`，所以 `base().x` 就是当前宽度）。这里用 `base()` 而非 `size.x` 是一种语义上的稳妥——保证相对值总是相对「完整宽度」而非被削短后的尺寸。

`dir` 直接来自 `text.dir` 样式，它决定列的排列方向（LTR 从左到右，RTL 从右到左），详见 4.3。

最终的 `ColumnConfig`：

[src/flow/mod.rs:401-414](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L401-L414) —— `ColumnConfig`：`count`、绝对 `width`、绝对 `gutter`、`dir`、`balanced`。这是 `compose` 阶段真正消费的结构。

> 旁注：`config.columns.width` 一旦算出，立刻被 `collect` 用作段落排版的宽度基准——见 [src/flow/mod.rs:214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L214)（`Size::new(config.columns.width, regions.full)`）。也就是说，段落里的断行是在**列宽**上进行的，而不是整页宽。

#### 4.2.4 代码实践（本讲的主实践任务之一）

**目标**：手算列宽，验证公式。

**给定**：假设某个 region 的 `regions.size.x = 450pt`（约 A4 减去左右页边距后的正文宽），用户写 `#columns(3, gutter: 20pt)`。

**步骤**：
1. 套公式：\(n = 3\)，\(g = 20\text{pt}\)，\(W = 450\text{pt}\)。
2. 计算：
   \[
   \text{width} = \frac{450 - 20 \times (3-1)}{3} = \frac{450 - 40}{3} = \frac{410}{3} \approx 136.67\text{pt}
   \]
3. 改用默认 gutter（4%）：\(g = 0.04 \times 450 = 18\text{pt}\)，
   \[
   \text{width} = \frac{450 - 18 \times 2}{3} = \frac{414}{3} = 138\text{pt}
   \]

**预期结果**：3 列时每列约 136.67pt（显式 20pt gutter）或 138pt（默认 4% gutter）。

**进阶（待本地验证）**：写一个最小的 typst 文档 `#set page(columns: 3, width: 450pt, margin: 0pt); #columns(3, gutter: 20pt)[...]`，编译后用 PDF 测量工具量第一列宽度，应当约为 136.67pt。

#### 4.2.5 小练习与答案

**练习 1**：为什么宽度无限时要强制 `count = 1`？

**参考答案**：宽度无限意味着没有确定的画布宽度（典型场景是 show 规则里对内容做「测量」、或内联上下文）。列宽公式需要除以 `count` 并扣掉 gutter，若宽度无限，算出的列宽也是无限的，多列失去意义，还可能在下游触发 `cannot expand into infinite width` 的断言（见 `layout_fragment_impl` 开头）。退化成单列是最安全的选择。

**练习 2**：`gutter` 默认是 `Ratio(0.04)`。若页面很窄（比如 `width: 100pt`），默认 gutter 会变成多少？

**参考答案**：\(0.04 \times 100 = 4\text{pt}\)。gutter 随宽度等比例缩放，这正是默认值用 `Ratio` 而非固定长度的用意——窄页面自动收紧列间距。

---

### 4.3 多列区域的构造与拼装：page_contents

#### 4.3.1 概念说明

`page_contents` 是 `compose` 的内层（见 u4-l4），负责「一个页面/容器区域」内部的所有内容拼装。当 `count > 1` 时，它要把这个区域切成多个列子区域，逐列排版，再把各列 frame 横向拼成一个整宽 frame。

回顾 u4-l4 的两层结构：`page()` → `page_contents()` 处理页面级插入物（parent 浮动体），`column()` → `column_contents()` 处理列级插入物（列浮动体、脚注）。多列只是让 `page_contents` 里多了一个「循环排 `count` 列」的外壳。

#### 4.3.2 核心流程

```
page_contents(regions):
  若 count == 1：直接 column(regions)（单列快车道）          ← 4.3 注
  否则：
    1. 构造 backlog：把每个外层区域高度重复 count 次          ← 给 regions.iter() 用
    2. 构造 inner Regions：size=(width, column_height)，expand.x=true
    3. for i in 0..count:
         column(i, inner) → (frame, used_height)
         按 dir 算出该列的 x 偏移，push_frame 拼到 output
         inner.next()
    4. 若 balanced 且 work.done()：触发列平衡（见 4.4）
    5. 返回拼好的 output frame
```

#### 4.3.3 源码精读

单列快车道——`count == 1` 时根本不构造多列结构：

[src/flow/compose.rs:110-114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L110-L114) —— `count == 1` 直接走 `column()`，跳过整段多列逻辑。

backlog 构造是本节最精巧的地方。它把当前列高 `column_height` 和外层 backlog 串起来，每个高度重复 `count` 次，再 `skip(1)` 去掉第一列（第一列直接用 `inner` 的初始 size）：

[src/flow/compose.rs:116-130](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L116-L130) —— backlog 把每个区域高度重复 `count` 次；`inner` 的 size 是 `(列宽, 列高)`，`expand.x` 强制为 `true`（列总是填满列宽）。

为什么要这样构造 backlog？因为 `inner` 会被 `regions.iter()` 遍历来计算 parent 浮动体的剩余空间（见 4.5）。把每个区域高度重复 `count` 次，相当于把「一张纸」展开成「`count` 个列子区域」，这样 `iter()` 走过去正好是「这一页的 `count` 列 + 后续每页的 `count` 列」。`skip(1)` 是因为第一列用的不是 backlog 而是初始 size。

列循环与 RTL 定位：

[src/flow/compose.rs:144-171](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L144-L171) —— 逐列排版并横向拼接；`dir == LTR` 时列从左往右摆，RTL 时从右往左摆；第 0 列还会把自身基线设给整宽 output。

RTL 的关键两行：

```rust
let x = if self.config.columns.dir == Dir::LTR {
    offset
} else {
    regions.size.x - offset - width   // 从右往左
};
offset += width + self.config.columns.gutter;
```

所以阿拉伯语、希伯来语（`#set text(dir: rtl)`）下，第 1 列在最右侧，第 2 列在其左侧，阅读顺序与文字方向一致。

另一个细节是基线传播（165-167 行）：distribute 把 region 基线设成「首帧基线」（通常是第一段的某行）。为了让这个基线能透出到外层，第 0 列的基线会被设成整宽 output 的基线。

#### 4.3.4 代码实践

**目标**：在源码层面追踪一次「3 列、单页」的拼装过程。

**步骤**：
1. 假设 `count=3`，页面正文区高度 `column_height=600pt`，外层 `regions.backlog` 为空。
2. 在 `page_contents` 里手算 backlog：`once(600).chain([])` → `[600]`，`flat_map(repeat_n(_, 3))` → `[600,600,600]`，`skip(1)` → `[600,600]`。
3. 循环跑 3 次：列 0 用初始 `inner`（600pt 高），列 1 用 backlog[0]=600，列 2 用 backlog[1]=600。每列排版后 `inner.next()`。
4. 观察三个 frame 如何按 `offset`（0 → width+gutter → 2(width+gutter)）横向拼到 `output`。

**预期结果**：backlog 长度为 2（= count-1），三个等宽等高的列 frame 被横向拼成一个整宽 frame。

#### 4.3.5 小练习与答案

**练习 1**：backlog 为什么要 `skip(1)`？

**参考答案**：因为第 0 列直接使用 `inner` 的初始 `size`（`(列宽, 列高)`），它不消费 backlog。backlog 是从第 1 列开始用的，所以构造时要把第一个（属于第 0 列的）高度去掉，否则列与 backlog 会错位一格。

**练习 2**：`inner` 的 `expand.x` 被强制设为 `true`（无论外层如何），为什么？

**参考答案**：每列的宽度由列宽公式严格算定（`config.columns.width`），列内排版必须填满这个宽度（这样段落才能正确断行到列宽）。若允许列内容在 x 方向收缩，列宽就失去意义，多列对齐也会错乱。所以 `expand.x=true` 是「列宽契约」的强制执行。

---

### 4.4 列平衡：column_balancing_height 与反复测量

#### 4.4.1 概念说明

默认（`balanced: false`）时，多列的填充是「贪心」的：第 1 列一直填到列高上限才溢出到第 2 列。于是常见这种难看的局面——内容总共只够填满 1.2 列，结果第 1 列顶天立地，第 2 列只有一小截，第 3 列全空。

`balanced: true` 就是为修复这个：当内容**能在一组列里全部放下**时，让各列尽量等高，而不是把第 1 列塞满。典型场景是短文档标题页、术语表、参考文献。

实现思路是一个「测量—重排」的迭代：

1. **第一遍**：不带任何高度限制地排版各列，得到每列实际用高 `u_i`，算出平均高 \(h^* = \frac{1}{n}\sum u_i\)。
2. 把 \(h^*\) 存进 `column_balancing_height`，触发 `Stop::Relayout(Parent)`，整片重排。
3. **第二遍及以后**：把 `balancing_target = h^* - 浮动体高度` 作为「标尺」传给前 `n-1` 列，让 `distribute` 在达到该高度时就停（把后续内容推到下一列）；最后一列不受限，承接剩余。
4. 重排后再算平均高，若标尺还能再放大就继续，否则收敛、停止。

为什么这样能收敛到等高？因为标尺 = 平均高，前 `n-1` 列被卡在平均高附近，剩余内容自然落到最后一列；当「平均高」不再变大时，各列就稳定在等高（或尽量接近）。

#### 4.4.2 核心流程

```
page_contents（balanced 分支，work.done() 为真时）:
  total_used_height = Σ 各列 used_height
  target = total_used_height / count
  if column_balancing_height 为空 或 < target:
      column_balancing_height = Some(target)
      return Err(Relayout(Parent))     ← page() 吞掉、回滚 checkpoint、重排
  否则：收敛，返回 output

column（每列）:
  balancing_target =
    if 是最后一列: None                   ← 最后一列不限高
    else: column_balancing_height - float_height
  column_contents(pod, balancing_target)
    └─ distribute(... balancing_target)   ← 用 target 当标尺
```

distribute 一侧，`balancing_target` 通过两个口子起作用：

- `fits()`：判断「还能不能再放一个元素」时，除了看 region 剩余空间，还看「累计已用高度是否已达到 target」。
- `multi()`：排版可断裂块时，把 region 高度上限 `set_min` 成「target - 已用」，让块在更窄的高度里断行。

#### 4.4.3 源码精读

平衡的触发点在 `page_contents` 末尾，**仅当所有内容都已排完**（`work.done()`）才考虑——这是关键守卫：

[src/flow/compose.rs:173-180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L173-L180) —— 算平均高 `total_used_height / count`；仅当它比已存的 `column_balancing_height` 更大时才更新并 `Relayout(Parent)`。

`work.done()` 这个条件的含义：只有当内容**在当前这一组列里全部放下、没有溢出到下一页**时，才值得平衡。如果内容多到撑满整页还溢出，那各列本来就是满高（= `column_height`），没什么可平衡的——平衡只对「最后一组、没填满」的列有意义。

`column_balancing_height` 字段定义在 `Composer` 上，能在重排间保留（因为 `Relayout(Parent)` 只回滚 `work`，不重建 `Composer`）：

[src/flow/compose.rs:65-80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L65-L80) —— `Composer` 持有 `column_balancing_height: Option<Abs>`，重排时它不被抹掉。

每列排版时计算 `balancing_target`，**最后一列传 `None`**：

[src/flow/compose.rs:208-214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L208-L214) —— `balancing_target`：前 `count-1` 列取 `column_balancing_height - float_height`，最后一列为 `None`。注释说明「只算浮动体高度、不算脚注」。

为什么最后一列不限高？因为前 `n-1` 列被卡在平均高，所有「溢出」的内容都会流到最后一列；最后一列必须放开承接，否则内容无处可去。这也是为什么 `fits()` 的注释特意强调「不把当前元素的 amount 计入 target 判断，避免突出元素堆积在最后一列」。

distribute 一侧消费标尺。`fits()` 增加了一道 target 检查：

[src/flow/distribute.rs:291-300](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L291-L300) —— `fits()`：除了 region 空间，还要 `target.fits(self.used.y)`——累计已用高度没超过 target 才算「放得下」。

可断裂块在排版前先把 region 高度砍到 target 以内：

[src/flow/distribute.rs:353-361](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L353-L361) —— `multi()`：若有 target，把 `pod.size.y` 用 `set_min(remaining)` 收窄到「target - 已用」，让块在这个更紧的高度里断行。

#### 4.4.4 代码实践（本讲的主实践任务之二）

**目标**：用具体数字走一遍平衡的收敛过程，解释 `column_balancing_height` 的角色。

**给定**：`count=2`，列高上限 `column_height=600pt`，`balanced=true`。内容总量「刚好」——自由排版时第 1 列用满 600pt、第 2 列只用 200pt（因为贪心填充）。

**步骤与推演**：

1. **第一遍**（`column_balancing_height = None`，无标尺）：
   - 列 0 自由排，贪心填到上限 → used ≈ 600pt。
   - 列 1（最后一列，本来就拿 `None`）排剩余 → used ≈ 200pt。
   - `total_used_height = 800pt`，`target = 800 / 2 = 400pt`。
   - `column_balancing_height` 为空 → 设为 `Some(400)`，返回 `Relayout(Parent)`。

2. **第二遍**（`column_balancing_height = Some(400)`）：
   - 列 0 **不是最后一列**，拿到 `balancing_target = 400`。`distribute` 在累计用高达到 ~400pt 时就停（`fits()` 的 target 检查），把后续内容推走 → used ≈ 400pt。
   - 列 1（最后一列）拿 `None`，承接被推过来的剩余内容 → used ≈ 400pt。
   - `total_used_height ≈ 800pt`，`target ≈ 400pt`。
   - 判断 `column_balancing_height(400) < target(400)`？**否**（400 不小于 400）→ 不再 relayout，收敛。

**`column_balancing_height` 的角色**：它是一把「跨重排持久化的测量标尺」。第一遍排版**只为测量**（测出平均高），测完把值存进这个字段；后续每一遍排版**用这把标尺限制前 n-1 列的高度**，强迫内容均匀分布。它既是「目标值」又是「终止条件」——当再排版也无法让平均高变大时，迭代停止。

**预期现象**：最终两列都约 400pt 高，而不是 600 + 200。

**若无法本地运行**：明确标注「待本地验证」。可用 `#set page(columns: 2, height: 600pt); #set columns(balanced: true); #lorem(...)` 对照 `balanced: false` 的输出观察列高差异。

#### 4.4.5 小练习与答案

**练习 1**：为什么平衡只在 `work.done()` 时触发？如果内容溢出到第 2 页会怎样？

**参考答案**：`work.done()` 表示内容在当前这组列里已全部排完、没有溢出。只有这种「没填满」的情形才需要平衡——让各列等高更好看。若内容溢出到下一页，说明各列本就被填满到 `column_height`，没有「留白不均」的问题，平衡无意义；且强行平衡反而可能改变分页。所以溢出时直接跳过平衡。

**练习 2**：`fits()` 里判断 target 时用 `target.fits(self.used.y)`，注释说「不把 amount 本身计入」。为什么？

**参考答案**：若把当前正在判断的元素高度 `amount` 也加进去再比 target，那么一旦某个较高元素会让累计用高略超 target，它就会被推到下一列，导致较高的元素不断后移、最终全堆到最后一列。只看「已用高度 `used.y` 是否已达 target」（不含当前元素），能让刚好卡在边界附近的元素留在本列，避免突出元素在最后一列堆积，使各列更均衡。

**练习 3**：`balancing_target` 为什么要减去 `float_height`（浮动体高度）而不是减去整个 `column_insertions.height()`（含脚注）？

**参考答案**：见 [src/flow/compose.rs:208-211](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L208-L211) 的注释。列平衡关心的是「正文内容的均衡分布」，浮动体（top/bottom float）会占据列的顶部/底部空间、压缩正文可用区域，所以要从 target 里扣掉它占的高度。而脚注是独立的、不参与「正文均衡」的考量（脚注本身有专门的高度处理，且 `float_height()` 专为列平衡排除脚注），故不减。

---

### 4.5 parent-scoped 浮动体：为什么列需要互相交互

#### 4.5.1 概念说明

回到本讲开头 `layout_columns` 的文档注释：「This is different from just laying out into column-sized regions as the columns can interact due to parent-scoped placed elements.」（这与「直接往列宽尺寸的 region 里排版」不同，因为列会因 parent 作用域的放置元素而互相交互。）

这句话是整个 `layout_columns` 存在的理由。如果多列只是「把 region 缩窄」，那根本不需要专门的列布局——随便哪个 layouter 拿到窄 region 都能排。真正让多列复杂的，是 **parent-scoped 浮动体**：一个 `#place(scope: "parent", float: true)` 的元素（比如横跨所有列的论文标题）要占据**整宽**空间，然后**压缩所有列**的可用区域。

这意味着各列不能独立排版——加一个 parent 浮动体，所有列都要一起缩小、一起重排。这就是「列互相交互」的含义。

#### 4.5.2 核心流程

```
float(placed, regions):
  base = match placed.scope {
      Column => regions.base(),          // 列级：用列宽作基准
      Parent => self.page_base,          // 页级：用整宽作基准（横跨所有列）
  }
  frame = placed.layout(engine, base)
  remaining = match placed.scope {
      Column => regions.size.y,                              // 仅当前列剩余
      Parent => Σ(剩余各列高度) / count,                      // 跨所有剩余列的平均剩余
  }
  若放不下且 may_progress：入队 work.floats，下个 region 再试
  否则：放进 page_insertions（注意是 page 级插入物！），返回 Relayout(Parent)
```

关键：parent 浮动体进的是 `page_insertions`（不是 `column_insertions`），它会在 `page_contents` 之前由 `page()` 从整片区域里**先把高度扣掉**（见 u4-l4），于是所有列的可用高度同时缩小。

#### 4.5.3 源码精读

`page()` 在进入 `page_contents` 前，先扣掉 page 级插入物（即 parent 浮动体）占的高度——这就是「parent 浮动体压缩所有列」的落点：

[src/flow/compose.rs:84-107](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L84-L107) —— `page()`：`pod.size.y -= self.page_insertions.height()` 后才交给 `page_contents`；遇到 `Relayout(Parent)` 则回滚 checkpoint 重排。

parent 浮动体的基准与剩余空间计算：

[src/flow/compose.rs:318-339](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L318-L339) —— `float()` 中按 `placed.scope` 分流：`Parent` 用 `self.page_base`（整宽）排版，剩余空间用 `regions.iter()` 把「当前列及之后的所有列」的高度求和再除以 `count`。

`remaining` 的 Parent 分支（精简）：

```rust
PlacementScope::Parent => {
    let remaining: Abs = regions
        .iter()                                  // 走过各列子区域
        .map(|size| size.y)
        .take(self.config.columns.count - self.column)  // 只算本页剩余列
        .sum();
    remaining / self.config.columns.count as f64
}
```

这里正好呼应 4.3 里 backlog 的精巧构造——因为每个区域高度被重复了 `count` 次，`regions.iter()` 走过去恰好枚举出「本页剩余的各列子区域」。`take(count - self.column)` 取本页尚未排的列，求和再除以 `count` 得到「平均每列剩余高度」作为 parent 浮动体能用的近似空间（注释说这是「an approximation for page placement」）。

最后，parent 浮动体被塞进 `page_insertions`，并返回 `Relayout(Parent)`：

[src/flow/compose.rs:366-377](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L366-L377) —— 按 scope 选 `page_insertions`（Parent）或 `column_insertions`（Column），`push_float` 后返回 `Relayout(placed.scope)`。

这就闭环了：parent 浮动体进 `page_insertions` → `page()` 重排 → `page_contents` 拿到的 region 已经被扣掉该高度 → 所有列一起变矮 → 各列重新 distribute。**列因此必须能互相交互，不能各排各的。**

#### 4.5.4 代码实践

**目标**：理解「break out of columns」（横跨列）的工作机制。

**步骤**：
1. 阅读 `crates/typst-library/src/layout/columns.rs` 顶部文档的 `Breaking out of columns` 一节（37-57 行），它给了一个用 `#place(top + center, scope: "parent", float: true)` 放论文标题、横跨两列的例子。
2. 对照 `src/flow/compose.rs` 的 `float()`，追踪这个 parent 浮动体：它用 `page_base`（整宽）排版 → 进 `page_insertions` → `page()` 扣高 → 两列都被压缩。
3. 思考：如果这个标题改用 `scope: "column"`（默认），会发生什么？

**预期结果**：`scope: "parent"` 时标题横跨整宽、两列正文都在标题下方；若改成 `scope: "column"`，标题只占第 1 列宽度，正文在第 2 列仍从顶部开始——不再「break out」。

**待本地验证**：用 columns.rs 文档里的例子（`#set page(columns: 2, height: 150pt); #place(top + center, scope: "parent", float: true, ...); #lorem(40)`）编译，对比 `scope: "parent"` 与 `scope: "column"` 的输出。

#### 4.5.5 小练习与答案

**练习 1**：为什么不直接把多列实现成「调用 `layout_fragment` 排第 1 列、溢出的 fragment 再排第 2 列」这种朴素方式？

**参考答案**：因为 parent 浮动体要求各列共享整宽空间并联动重排。朴素方式下各列独立排版，无法处理一个横跨所有列的浮动体——你不知道该在第几列扣减它的高度，也无法让所有列一起缩小。`layout_columns` 通过 `page_insertions` + `Relayout(Parent)` 让「加一个 parent 浮动体 → 全部列重排」成为可能，这正是它的文档注释强调「columns can interact」的原因。

**练习 2**：parent 浮动体的 `remaining` 为什么要除以 `count` 取平均，而不是直接用某一列的高度？

**参考答案**：因为 parent 浮动体横跨所有列，它占用的整宽高度会被「分摊」到每一列——每一列的可用高度都减少同一个量。用「各列剩余之和 / 列数」得到的就是「平均到每列的剩余」，作为该浮动体能否放下的近似判据。注释也明确这是 approximation，因为列内已有内容的高度分布并不完全均匀。

---

## 5. 综合实践

把本讲的三条主线——列宽计算、列平衡、parent 浮动体——串成一个综合任务。

**场景**：你要为一个学术文档的首页排版：两列正文，顶部一个横跨两列的加粗标题（parent 浮动体），且正文用 `balanced` 让两列等高收尾。

**任务**：

1. **算列宽**。设页面正文区宽 `W = 460pt`，`#columns(2, gutter: 24pt)`。手算每列宽度。

   <details>
   <summary>参考答案</summary>
   \[ \text{width} = \frac{460 - 24 \times (2-1)}{2} = \frac{460 - 24}{2} = 218\text{pt} \]
   每列 218pt。
   </details>

2. **画一张时序图**，标出下列事件的发生顺序与涉及的字段/函数：
   - `configuration` 算出 `ColumnConfig.width = 218pt`；
   - `page_contents` 构造 backlog（`count=2`）、`inner` 区域；
   - 标题（parent 浮动体）在 `column_contents` → `distribute` 里被遇到，`float()` 把它塞进 `page_insertions`，返回 `Relayout(Parent)`；
   - `page()` 回滚 checkpoint，扣掉标题高度后重排两列；
   - 两列正文排完（`work.done()`），触发平衡：算平均高、设 `column_balancing_height`、再次 `Relayout(Parent)`；
   - 重排时列 0 拿 `balancing_target`、列 1（最后一列）拿 `None`，收敛后输出。

3. **预测一个边界情况**：如果标题（parent 浮动体）非常高，高到扣掉之后正文几乎填不满两列，平衡过程会怎样？两列的最终高度大致是多少？

   <details>
   <summary>参考答案</summary>
   平衡仍会触发（只要 <code>work.done()</code>）。各列实际用高很小，平均高也很小，两列最终高度都会收敛到「正文总量的一半」附近，而不是顶到 <code>column_height</code>。标题作为 page 插入物独立占用顶部空间，不参与列高平均的计算（它是 <code>page_insertions.height()</code>，在平衡前就已扣除）。
   </details>

**待本地验证**：用以下最小文档实际编译，观察标题横跨、两列等高的效果：

```typst
#set page(width: 460pt + 48pt, columns: 2, gutter: 24pt, margin: 0pt)
#place(
  top + center,
  scope: "parent",
  float: true,
  text(1.4em, weight: "bold")[My Document Title],
)
#set columns(balanced: true)
#lorem(60)
```

## 6. 本讲小结

- `ColumnsElem` 通过 `COLUMNS_RULE`（`BlockElem::multi_layouter`）挂到 `layout_columns`，后者与普通 `layout_fragment` 共用 `layout_fragment_impl`，差别只在传入的 `ColumnOptions`（`count` / `balanced` / `gutter`）。
- `configuration` 把 `ColumnOptions` 翻译成 `ColumnConfig`：列宽公式 \(\text{width} = (W - g(n-1))/n\)，gutter 按基准宽度解析（默认 4%），无限宽时强制单列。
- `page_contents` 把一个区域切成 `count` 个列子区域：backlog 把每页高度重复 `count` 次（供 `regions.iter()` 枚举各列），逐列排版后按 `dir`（LTR/RTL）横向拼成整宽 frame，第 0 列传播基线。
- 列平衡只在 `work.done()`（内容未溢出）时触发：先无标尺测一次得到平均高，存入 `column_balancing_height`，触发 `Relayout(Parent)`；重排时前 `n-1` 列用 `balancing_target` 当标尺（经 `distribute` 的 `fits()` 与 `multi()` 落地），最后一列不限高，直到平均高不再增大即收敛。
- 多列之所以需要专门实现而非「缩窄 region」，是因为 parent-scoped 浮动体（`scope: "parent"`）要横跨所有列、整宽排版，并经 `page_insertions` + `Relayout(Parent)` 压缩所有列、联动重排——这是「列互相交互」的根因。

## 7. 下一步学习建议

- 本讲是 flow 单元（u4）的终点。接下来建议进入 **u5（行内/段落布局）**，看列宽（`config.columns.width`）是如何传给段落、影响断行（`linebreak`）与整形（`shaping`）的。
- 若对浮动体与脚注的细节意犹未尽，可回看 u4-l4（compose），本讲的 parent 浮动体处理正是建立在 u4-l4 的 `page_insertions` / `column_insertions` 两层模型之上。
- 想了解表格里的「跨列/跨行」如何排版，可预习 **u6-l1 / u6-l2（GridLayouter）**，grid 的断裂与列测量与本讲的 `distribute` 测量机制有相通之处。
- 想验证本讲结论，推荐阅读 `crates/typst-library/src/layout/columns.rs` 的元素文档（含多个可直接编译的 `#example`），以及 `src/flow/compose.rs` 顶部关于 `compose` 与 distribution 关系的注释。
