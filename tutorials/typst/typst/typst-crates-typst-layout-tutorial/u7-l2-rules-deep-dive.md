# 常用 show 规则实现详解

## 1. 本讲目标

上一篇 u7-l1 讲清了 show 规则是「怎么挂上去的、挂在哪、长什么样」——`Target::Paged`、`ShowFn<T>` 签名、`NativeRuleMap` 注册表、`register` 按 6 类注册的 59 条规则，以及「纯样式变更型」与「挂 layouter 型」两种写法。本讲是单元 u7 的收尾篇，**不再谈注册机制**，而是钻进 5 条**较复杂的代表性规则**，看它们各自如何把一个语义元素翻译成可排版的内容。

读完本讲你应当能够：

1. 区分 `BlockElem::multi_layouter` 与 `single_layouter` 两种「挂 layouter」方式，说清为什么表格/栈/列表走 `multi`、而旋转/缩放/图形走 `single`。
2. 读懂 `HEADING_RULE`：它为什么要先「测量」编号的宽度（`LocatorLink::measure`），以及这段测量如何驱动标题的悬挂缩进。
3. 读懂 `FIGURE_RULE`：caption 与 body 如何按上下位置组装、浮动体（float）如何挂上去。
4. 读懂 `BIBLIOGRAPHY_RULE`：为什么它有「两栏 grid」与「悬挂缩进」两条分支，以及那条「绕过 grid 的 show 步骤」的注释意味着什么。
5. 读懂 `LAYOUT_RULE`：它是本讲的综合实践主角——如何把当前 region 的 `base()` 尺寸暴露给用户函数，又如何把用户返回的内容再交给 `flow::layout_fragment` 排版。

## 2. 前置知识

本讲承接 u7-l1，默认你已掌握以下概念（以下仅做最简提示，详细解释见对应讲义）：

- **原生 show rule 与 `ShowFn<T>`**（u7-l1）：`fn(&Packed<T>, &mut Engine, StyleChain) -> SourceResult<Content>`，每条 `*_RULE` 是不捕获环境的 `const` 闭包，realize 阶段逐元素调用。
- **Regions / Region**（u2-l2）：`Regions` 是带 `backlog`（候选高度队列）的区域序列，`Region`（pod）是单区域退化；`regions.base()` 返回 `(size.x, full)`——宽度用当前宽、高度用未被削短的整区域高度，是相对尺寸的基准。
- **Frame / Fragment**（u2-l3）：`Frame` 是单张二维画布，`Fragment` 是 `Vec<Frame>`；`layout_frame` 强制单帧、`layout_fragment` 允许多帧。
- **Locator / LocatorLink / 测量模式**（u2-l4）：`Locator` 给每个排版实例分配稳定 `Location`；`LocatorLink` 跨 memoize 边界按需取外层信息；测量（measure）是一种「只为量尺寸、不产生真实定位」的排版。
- **flow 三段式**（u4-l1）：`collect → compose（每区域一次）→ finalize`；块在 collect 阶段被分成不可断裂与可断裂两类（u4-l5）。

> 本讲引用的源码横跨 `typst-layout`（rules.rs、flow/）与 `typst-library`（container.rs、locator.rs、counter.rs）。永久链接全部锁定在 `146a58329`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-layout/src/rules.rs` | **主角**。本讲的 5 条规则 `HEADING_RULE` / `FIGURE_RULE` / `BIBLIOGRAPHY_RULE` / `TABLE_RULE`+`GRID_RULE` / `LAYOUT_RULE` 全部定义于此。 |
| `crates/typst-library/src/layout/container.rs` | 定义 `BlockElem::single_layouter` / `multi_layouter` 与 `BlockBody` 枚举——规则挂载 layouter 的「卡槽」。 |
| `crates/typst-layout/src/flow/block.rs` | `layout_single_block` / `layout_multi_block` 在排版块时对三种 `BlockBody` 分派，是 layouter 闭包真正被 `callback.call(...)` 调起的地方。 |
| `crates/typst-layout/src/flow/mod.rs` | `layout_frame` / `layout_fragment`——layouter 闭包与 HEADING_RULE、LAYOUT_RULE 内部回排内容时调用的入口。 |
| `crates/typst-library/src/introspection/locator.rs` | `LocatorLink::measure` 与测量模式 `Resolved::Measure`，HEADING_RULE 量编号宽度的底座。 |
| `crates/typst-library/src/introspection/counter.rs` | `Counter::display_at`——HEADING_RULE 求标题编号文本。 |

## 4. 核心概念与源码讲解

本讲按 5 条规则展开，每条即一个最小模块。顺序遵循「先讲贯穿所有挂 layouter 型规则的公共机制（multi vs single），再逐条精读具体规则」。

### 4.1 两种排版器挂载：multi_layouter 与 single_layouter

#### 4.1.1 概念说明

u7-l1 已经点出：凡是「挂 layouter 型」规则，都通过 `BlockElem::multi_layouter` 或 `single_layouter` 把 typst-layout 里那些**没有对外导出的 layouter 函数**（如 `crate::grid::layout_grid`、`crate::stack::layout_stack`）接进排版流程——这正是 `lib.rs` 门面里看不到 `layout_grid` 的原因。

但为什么要分两个挂法？区别在于**这个 layouter 需要看到的「画布」有多大**：

- **`single_layouter`**：layouter 只需要**一个区域**（一个 `Region`：尺寸 + 是否撑满），产**一帧** `Frame`。它本质上是「单区域、单帧、不可断裂」。图形、图片、变换这类「给定一块固定画布就能算出几何」的内容用这种。
- **`multi_layouter`**：layouter 需要**整条区域队列**（完整的 `Regions`：当前区域 + `backlog` + `last`），产**多帧** `Fragment`，可以跨页断裂。表格、栈、列表、多列这类「可能跨页、或需要按 Fr 分配剩余空间」的内容用这种。

一句话总结：**内容会不会跨区域断裂、要不要看后续候选区域，决定了挂 multi 还是 single。**

#### 4.1.2 核心流程

挂载与调用是一条清晰的三段链路：

```
show rule (rules.rs)
   │  调用 BlockElem::multi_layouter(elem, layouter_fn) / single_layouter(elem, layouter_fn)
   ▼
BlockElem，其 body = BlockBody::MultiLayouter(..) / SingleLayouter(..)
   │  这条 Content 被 realize 后，作为「块」进入 flow
   ▼
flow/block.rs: layout_single_block / layout_multi_block
   │  match body {
   │      SingleLayouter(cb) => cb.call(engine, locator, styles, pod)        // 产 Frame
   │      MultiLayouter(cb)  => cb.call(engine, locator, styles, regions)    // 产 Fragment
   │  }
   ▼
被挂的 layouter 函数（crate::grid::layout_grid 等）真正执行排版
```

两个关键差别会在源码精读里看到：

1. `single_layouter` 构造时强制 `breakable: false`；`multi_layouter` 不动它（默认 `true`）。
2. 即使一个 `single_layouter` 块不幸走进了可断裂路径（`layout_multi_block`），flow 也只用 `pod.base()` 给它**单区域**，丢弃 `backlog`——它永远只看到一块画布。

#### 4.1.3 源码精读

**卡槽定义**（`typst-library`）：

[container.rs:406-421](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/container.rs#L406-L421)：`single_layouter` 接受一个 `fn(&Packed<T>, &mut Engine, Locator, StyleChain, Region) -> SourceResult<Frame>`，构造 `BlockBody::SingleLayouter`，并 `.with_breakable(false)` 强制不可断裂。

[container.rs:424-437](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/container.rs#L424-L437)：`multi_layouter` 接受 `fn(..., regions: Regions) -> SourceResult<Fragment>`，构造 `BlockBody::MultiLayouter`，**不设 breakable**（沿用默认 `true`）。

[container.rs:442-451](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/container.rs#L442-L451)：`BlockBody` 枚举三变体 `Content` / `SingleLayouter` / `MultiLayouter`，正是 flow 分派的依据。

**flow 侧的分派**（`typst-layout`）：

[flow/block.rs:48-58](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L48-L58)：在不可断裂路径 `layout_single_block` 里，`SingleLayouter` 直接 `cb.call(engine, locator, styles, pod)` 产 `Frame`；`MultiLayouter` 则把 pod 转成 `Regions`、调起后 `.into_frame()`——**强制压成单帧**（因为这条路径只允许一帧）。

[flow/block.rs:184-188](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L184-L188)：在可断裂路径 `layout_multi_block` 里，`MultiLayouter` 拿到完整 `Regions` 并直接返回 `Fragment`（**可多帧、可跨页**）；而即便这里遇到 `SingleLayouter`（[flow/block.rs:173-176](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L173-L176)），也只用 `Region::new(pod.base(), pod.expand)` 给它单区域、`map(Fragment::frame)` 包成单帧片段。

**规则的归类**（rules.rs）。按挂法把规则分两类，是理解本讲其余规则的前提：

- `multi_layouter`（可能跨页 / 需要 Fr 分配）：
  - [LIST_RULE:127](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L124-L141) → `crate::lists::layout_list`
  - [STACK_RULE](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L682-L684) → `crate::stack::layout_stack`
  - [COLUMNS_RULE](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L678-L680) → `crate::flow::layout_columns`
  - [PAD_RULE](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L674-L676) → `crate::pad::layout_pad`
  - [TABLE_RULE](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L526-L528) / [GRID_RULE](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L686-L688) → `crate::grid::layout_table` / `layout_grid`
  - [LAYOUT_RULE](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L739-L756) → 内联闭包
- `single_layouter`（给定一块画布即可算几何、不可断裂）：
  - [MOVE_RULE](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L716-L718) / SCALE / ROTATE / SKEW → `crate::transforms::*`
  - [REPEAT_RULE](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L732-L734) → `crate::repeat::layout_repeat`
  - [IMAGE_RULE](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L758-L763) 与 LINE/RECT/SQUARE/ELLIPSE/CIRCLE/POLYGON/CURVE → `crate::shapes::*`

注意 `EQUATION_RULE` 的分叉（[rules.rs:805-812](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L805-L812)）：块级方程走 `multi_layouter`（`layout_equation_block`，可带编号、可跨页），行内方程却走 `InlineElem::layouter`（行内版 layouter，见 [container.rs:179-191](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/container.rs#L179-L191)）——这是 block 级与 inline 级两种 layouter 卡槽的对照。

#### 4.1.4 代码实践

**实践目标**：用「挂法」这条线索给规则分类，并验证 `STACK_RULE` 的整条挂载链。

**操作步骤（源码阅读型）**：

1. 打开 `src/rules.rs`，逐条扫描所有 `*_RULE` 常量，按「返回 `BlockElem::multi_layouter(...)` / `single_layouter(...)` / 其它（纯样式或普通 Content）」三类分别列表。
2. 对 `STACK_RULE`（[rules.rs:682-684](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L682-L684)）追完整链路：
   - 它返回 `BlockElem::multi_layouter(elem.clone(), crate::stack::layout_stack).pack()`；
   - 该 Content realize 后作为可断裂块进入 `layout_multi_block`（[flow/block.rs:184-188](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L184-L188)），命中 `MultiLayouter` 分支；
   - `cb.call(engine, locator, styles, regions)` 真正调起 `crate::stack::layout_stack`（见 u6-l5）。

**需要观察的现象 / 预期结果**：你会得到约 10+ 条 `multi`、10+ 条 `single`、其余为纯样式 / Content 的分类表。`STACK_RULE` 链路应落在「可断裂多帧」路径上，这与「栈可以跨页」的直观行为一致。

> 待本地验证：若想眼见为实，可在 `flow/block.rs` 的 `MultiLayouter(cb) =>` 分支临时插一条 `eprintln!("multi: {:?}", elem.span());`，编译一个小 Typst 文档（含一个会跨页的 `#stack()`），观察输出。

#### 4.1.5 小练习与答案

**Q1**：为什么 `IMAGE_RULE` 用 `single_layouter` 而 `TABLE_RULE` 用 `multi_layouter`？

**参考答案**：图片给定一块画布（宽高 + fit 策略）就能算出几何，天然单帧、不可断裂，故用 `single_layouter`（且 `breakable` 被强制为 `false`）；表格可能跨页断裂，且其行高解析需要看后续候选区域（u6-l1），故必须拿到完整 `Regions` 并产出多帧 `Fragment`，只能用 `multi_layouter`。

**Q2**：`single_layouter` 的块如果出现在一个本应可断裂的上下文里，flow 会怎么处理它？

**参考答案**：即便进了 `layout_multi_block`，flow 也只用 `Region::new(pod.base(), pod.expand)` 给它**单区域**（[flow/block.rs:173-176](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L173-L176)），丢弃 `backlog`，并把结果包成单帧片段——它永远不会跨页。

---

### 4.2 HEADING_RULE：编号测量与悬挂缩进

#### 4.2.1 概念说明

标题（`#set heading(numbering: "1.1")`）展示时要解决两件事：

1. **把编号文本拼到正文前面**：形如 `1.2.3  导论`。编号由计数器在标题所在位置求值，再渲染成 Content。
2. **悬挂缩进（hanging indent）**：当标题正文换行时，第二行应当对齐到**正文起始处**（编号之后），而不是顶到页边。为此必须知道「编号占了多宽」。

难点正是第 2 点：**编号文本的渲染宽度，只有真正排一次版才知道**（取决于字体、字号、编号字符串长短）。而 show rule 跑在 realize 阶段，这时还没有最终的页面宽度可供借用——所以 Typst 选择**主动测量**：把编号内容丢进一个「无限大、不撑满」的区域里排一次，读回它的宽度。

测量会带来一个新问题：**测量用的这次排版不该产生真实的元素定位**，否则会和标题本身的 location 撞车、或污染 introspection。于是用到了 u2-l4 提到的测量模式——`LocatorLink::measure`。

#### 4.2.2 核心流程

`HEADING_RULE` 的逻辑可分为四步：

```
1. 求编号文本
   Counter::of(HeadingElem).display_at(engine, location, styles, numbering, span)
     → numbering: Content   // 例如 "1.2.3 "
2.（仅当 hanging_indent = auto 且左对齐时）测量编号宽度
   - pod = 无限大区域 (expand=false) → shrink 到内容
   - link = LocatorLink::measure(location, span)   // 测量模式，base = 标题 location
   - frame = layout_frame(engine, &numbering, Locator::link(&link), styles, pod)
   - indent = frame.size().x + SPACING_TO_NUMBERING(0.3em)
   （否则：用户显式给了 hanging_indent 就用它；否则 indent = 0）
3. 拼装：realized = numbering + 弱间距(0.3em) + body
4. 若 indent ≠ 0，包成「左 inset = indent + 内容首行左移 indent」的块（悬挂缩进）
   否则 BlockElem::packed(realized)
```

悬挂缩进的几何（第 4 步）值得单独说清：块本身在起始侧留 `indent` 的内边距（内容区整体右移 `indent`），但内容最前面再插一个 `-indent` 的水平间距（把第一行的编号拉回到块的左边缘）。结果就是——**编号顶到左边，换行后的正文回到 `indent` 处与编号后的正文对齐**。

#### 4.2.3 源码精读

完整规则在 [rules.rs:246-299](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L246-L299)。逐段看：

**编号求值**（[rules.rs:258-262](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L258-L262)）：`elem.location().unwrap()` 取标题自身位置，`Counter::of(HeadingElem::ELEM).display_at(...)` 在该位置求计数器值并渲染成 Content。`display_at` 的实现在 [counter.rs:271-284](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L271-L284)，内部走 `CounterAtIntrospection`（多趟收敛，见 u3-l5）。

**测量分支的触发条件**（[rules.rs:265](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L265)）：`if hanging_indent.is_auto() && align.x == FixedAlignment::Start`。即只有「用户没显式设悬挂缩进」且「水平左对齐」时，才自动测量编号宽度。显式设了就直接用那个值（[rules.rs:253-256](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L253-L256)）。

**测量本身**（[rules.rs:266-281](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L266-L281)）：

- pod = `Region::new(Axes::splat(Abs::inf()), Axes::splat(false))`——尺寸无限、`expand` 全 false，意思是「给你无限空间、按内容自然收缩」，这样排出来的宽度就是编号的真实宽度。
- `let link = LocatorLink::measure(location, span);`——进入测量模式，`base = location`（标题自己的位置）。
- `let size = (engine.library.routines.layout_frame)(engine, &numbering, Locator::link(&link), styles, pod)?.size();`——回排编号，读宽度。
- `indent = size.x + SPACING_TO_NUMBERING.resolve(styles);`，其中 `SPACING_TO_NUMBERING = Em::new(0.3)`（[rules.rs:247](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L247)）。

**为什么是 `LocatorLink::measure`**：见 [locator.rs:378-383](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L378-L383)。该 link 使下游 locator 解析成 `Resolved::Measure(base, span)`（[locator.rs:233-239](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L233-L239) 与 [locator.rs:293-302](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L293-L302)）：这次排版不会产生真实 location，若内部真触发了内省，会去「找文档里最接近的匹配元素，找不到就回落到 `base`」。这里编号本身只是纯文本，内省主要是个安全网；真正的作用是**让测量排版拿到一条合法、且不与标题真实定位冲突的 locator 链**。

**拼装与悬挂缩进**（[rules.rs:283-298](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L283-L298)）：

- `numbering + spacing + realized`——编号在前，中间是 `0.3em` 的**弱**水平间距（`HElem::new(SPACING_TO_NUMBERING.into()).with_weak(true)`），弱间距在没有相邻内容时会塌缩。
- `if indent != Abs::zero()`：`body = HElem::new((-indent).into()).pack() + realized`（首行左移 `indent`），`inset` 在起始侧设 `indent`，包进 `BlockElem::new().with_inset(inset).with_body(...)`。起始侧由 `styles.resolve(TextElem::dir).start()` 决定（支持 RTL）。

#### 4.2.4 代码实践

**实践目标**：观察「自动测量编号宽度」如何随编号字符串长短变化，并理解测量是真实发生的一排版。

**操作步骤（源码阅读 + 可选本地验证）**：

1. 读 [rules.rs:266-281](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L266-L281)，确认测量用的是「无限大 + expand=false」的 pod，因此返回宽度 = 编号自然宽度。
2. 设想两个标题：`numbering: "1.1"`（短）与 `numbering: "A.1.a"`（长）。说明后者的 `indent` 会更大，因而正文换行时第二行右移更多。
3. （可选，待本地验证）写一个 Typst 文档：

   ```typst
   #set heading(numbering: "1.1")
   = 这是一个很长的标题它会换行并且第二行应当对齐到正文而不是编号下方
   ```

   编译后观察：第一行 `1` 顶左，换行的后续文字左边缘与 `这是…` 的「这」对齐——这正是测量出的 `indent` 在起作用。把 `numbering` 改成更长的 `"1.1.1.1"`，缩进应随之变宽。

**预期结果**：编号越长，自动悬挂缩进越宽；若手动 `#set heading(hanging_indent: 0pt)`，则测量被跳过、不产生悬挂缩进。

#### 4.2.5 小练习与答案

**Q1**：为什么不直接用一个固定的悬挂缩进值，而要费劲测量？

**参考答案**：因为编号宽度依赖于编号字符串（`1` vs `1.2.3` vs `A.a`）和字体字号，无法预知。固定值要么过窄（编号被正文挤掉）要么过宽（浪费空间、正文右移太多）。测量给出精确宽度，悬挂缩进才能严丝合缝。

**Q2**：测量时为什么用 `expand = false` 的无限大区域，而不是真实页面宽度？

**参考答案**：测量只想要「编号本身有多宽」，因此要让它**按内容自然收缩**——无限大区域保证不因空间不足而换行，`expand=false` 保证不被强制撑满某宽度。换页宽度反而是干扰项。

**Q3**：`LocatorLink::measure(location, span)` 的第一个参数 `location` 有什么用？

**参考答案**：它是 `base`——测量模式下的「基点 location」。若测量排版内部意外触发内省（要查计数器 / query），系统会「找文档中最接近的匹配元素，找不到就回落到这个 `base`」，保证至少落在标题附近、计数器可解。见 [locator.rs:293-302](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L293-L302)。

---

### 4.3 FIGURE_RULE：caption 组装与浮动体

#### 4.3.1 概念说明

图（`figure`）= 一个主体 + 一个可选标题（caption），并且可以作为**浮动体**（float）被排到页面顶部或底部。`FIGURE_RULE` 只做「组装」：把 body 与 caption 按 caption 的位置（上 / 下）拼好、塞进一个块；若用户要浮动，再包一层 `PlaceElem`。**真正的浮动插入逻辑不在本规则，而在 flow 的 compose 阶段**（u4-l4）。

#### 4.3.2 核心流程

```
1. realized = body
2. 若有 caption：
   - caption.position == Top    → (caption, body)
   - caption.position == Bottom → (body, caption)
   - 用一个弱 VElem(gap) 分隔，拼成 first + gap + second
3. realized += ParbreakElem::shared()   // 让 body 成为段落边界
4. realized = BlockElem::packed(realized)
5. 若 placement 被设置 (Some(align))：包成 PlaceElem(float=true, scope=elem.scope)
   否则若 scope==Parent 却没开浮动 → 报错
```

#### 4.3.3 源码精读

完整规则 [rules.rs:301-344](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L301-L344)。

**caption 位置**（[rules.rs:307-319](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L307-L319)）：按 `OuterVAlignment::Top` / `Bottom` 决定 `(first, second)` 顺序，中间塞 `VElem::new(elem.gap).with_weak(true)`——弱垂直间距，没有相邻块时会塌缩。

**段落边界**（[rules.rs:322](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L322)）：`realized += ParbreakElem::shared().clone()` 注释写明「确保 body 被视作一个段落」——这是 flow 收集器判断「块 vs 段落」的依据（u4-l2）。

**浮动包装**（[rules.rs:328-341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L328-L341)）：

- 若 `elem.placement.get(styles)` 是 `Some(align)`：构造 `PlaceElem::new(realized).with_alignment(align.map(|a| HAlignment::Center + a)).with_scope(elem.scope).with_float(true)`。注意对齐被映射成「水平居中 + 用户给的（主要表垂直的）对齐」。
- `else if elem.scope == PlacementScope::Parent`：`bail!` 报错「parent-scoped placement 只对浮动图可用」，并给提示 `figure(placement: auto, ..)`。

caption 自身的展示由另一条 `FIGURE_CAPTION_RULE`（[rules.rs:346-347](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L346-L347)）负责：它调用 `elem.realize(engine, styles)`（生成「Figure N: 文字」之类）再 `BlockElem::packed`。

> 衔接 u4-l4：浮动图真正「浮」起来的动作发生在 flow 的 compose——`PlaceElem` 在 collect 阶段被识别为 `PlacedChild(float=true)`，compose 时按 page/column 作用域插入、缩小可用区域并触发 `Stop::Relayout` 重排（u4-l4）。本规则只是「把图声明为浮动」，不负责几何。

#### 4.3.4 代码实践

**实践目标**：验证 caption 位置与浮动的源码行为。

**操作步骤（源码阅读型）**：

1. 读 [rules.rs:307-319](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L307-L319)，确认 caption 在 `Top` 时排在 body **之前**（first=caption）。
2. 在 u4-l4 的 compose 中确认：`PlaceElem` 的 `float=true` 如何让它脱离正文流。

**预期结果 / 待本地验证**：写 `#figure(rect(), caption: [图], placement: bottom)`，编译后图浮到页面底部；改为 `placement: none`（默认）则图留在正文位置。两种情况下 caption 与 body 的先后都由 `caption.position`（默认 `Bottom`，即 body 在前、caption 在后）决定。

#### 4.3.5 小练习与答案

**Q1**：为什么 `FIGURE_RULE` 末尾要追加 `ParbreakElem::shared()`？

**参考答案**：让 body 成为段落边界。flow 收集器据此把 body 当作独立的段落 / 块处理，避免 body 内容与外围文字错误地并入同一段（u4-l2 的 par_situation 维护依赖这些显式段落断点）。

**Q2**：`scope: "parent"` 但没开浮动（`placement: none`）时会发生什么？

**参考答案**：`FIGURE_RULE` 在 [rules.rs:335-341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L335-L341) 直接 `bail!` 报错，提示 parent-scoped 仅对浮动图可用。这是因为 parent 作用域放置（横跨多列）只有浮动体才支持（u4-l4 / u4-l6）。

---

### 4.4 BIBLIOGRAPHY_RULE：两种布局分支

#### 4.4.1 概念说明

参考文献（`#bibliography("ref.bib")`）在展示时按条目是否带「前缀标签」（如 `[1]`、`[Smi24]`）分两种排版形态：

- **有条目带前缀**：用**两列网格**（前缀列 | 条目列），让所有前缀左对齐成整齐一列。
- **无前缀**：用**悬挂缩进**逐条排版——这是无序列表式的「首行顶出、续行缩进」。

这两种形态正是 u4.1「挂 layouter」与 4.2「悬挂缩进块」技术的组合应用，因此是个很好的综合案例。

#### 4.4.2 核心流程

```
1. 取 location、span；realize_title 生成可选标题
2. Works::generate → bibliography.entries（已排序的条目列表）
3. if 任一条目 entry.prefix.is_some()：          // 有前缀 → 两列网格
     - 把每条目拆成两个 GridCell：[ListItemLabel(prefix), BibEntry(body)]
     - GridElem(columns=[Auto,Auto], column_gutter=0.65em, row_gutter=par.spacing)
     - BlockElem::multi_layouter(grid, crate::grid::layout_grid)  // 直接挂 grid layouter，绕过 GRID_RULE
     - 包进 PdfMarkerTag::Bibliography(true, block)
   else：                                        // 无前缀 → 悬挂缩进逐条
     - 对每条目：
         若 bibliography.hanging_indent：构造「-INDENT 首移 + inset INDENT」的块（同 HEADING 套路，INDENT=1.5em）
         否则：BlockElem::packed(BibEntry)
     - Content::sequence(所有块)
     - 包进 PdfMarkerTag::Bibliography(false, body)
4. Content::sequence(标题 + 正文)
```

#### 4.4.3 源码精读

完整规则 [rules.rs:452-518](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L452-L518)。

**两列网格分支**（[rules.rs:465-494](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L465-L494)）：

- 常量 `COLUMN_GUTTER = Em::new(0.65)`、`INDENT = Em::new(1.5)`（[rules.rs:453-454](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L453-L454)）。
- 每条目产出两个 `GridCell`：前缀用 `PdfMarkerTag::ListItemLabel(prefix.located(backlink))`，正文用 `PdfMarkerTag::BibEntry(body)`——`PdfMarkerTag` 只服务于 tagged PDF 无障碍结构（u6-l6），不影响几何。
- 网格 `columns = TrackSizings([Auto, Auto])`（两列都按内容自适应）、`column_gutter = 0.65em`、`row_gutter = par.spacing`。
- 关键一行（[rules.rs:491](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L491)）：`let block = BlockElem::multi_layouter(packed, crate::grid::layout_grid).pack();`。其上方注释（[rules.rs:489-490](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L489-L490)）解释了**为什么不直接返回 GridElem 内容**：「Directly build the block element to avoid the show step for the grid element. This will not generate introspection tags for the element.」——文献希望用 grid 的几何布局，但**不要** grid 元素那套 introspection 打 tag 机制（条目有自己的 backlink/location），所以手动直接挂 `layout_grid`、跳过 `GRID_RULE` 的正常展示。

**悬挂缩进分支**（[rules.rs:496-514](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L496-L514)）：与 `HEADING_RULE` 的悬挂缩进块**完全同构**——`body = HElem::new((-INDENT).into()).pack() + realized`，`inset` 起始侧设 `INDENT`，包进 `BlockElem::new().with_inset(inset).with_body(...)`。`INDENT = 1.5em`。若 `bibliography.hanging_indent` 为 false，则直接 `BlockElem::packed(realized)`。每条目都 `.spanned(span)` 后 `Content::sequence`。

> 这是本讲的一个递归回响：同一套「负间距首移 + 起始侧 inset」的悬挂缩进手法，在 `HEADING_RULE`（测量确定 indent）与 `BIBLIOGRAPHY_RULE`（固定 1.5em）中复用，印证了它们是 typst-layout 里通用的悬挂缩进惯用法。

#### 4.4.4 代码实践

**实践目标**：理解「有前缀走 grid、无前缀走悬挂缩进」的分支判据。

**操作步骤（源码阅读型）**：

1. 读 [rules.rs:465](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L465)，确认分支条件是 `bibliography.entries.iter().any(|entry| entry.prefix.is_some())`——只要**任一**条目带前缀就整体走网格。
2. 对比两分支产出的块：网格分支用 `multi_layouter`（可跨页），悬挂缩进分支是普通 `BlockElem` 序列（每条各自由 flow 排版、可跨页）。

**预期结果 / 待本地验证**：用一个带 `[key]` 前缀的 `.bib` 编译，应看到两列对齐的前缀与条目；换用纯数字编号或无前缀样式，则看到悬挂缩进列表。

#### 4.4.5 小练习与答案

**Q1**：为什么网格分支不直接返回 `GridElem` 内容、而要手动挂 `layout_grid`？

**参考答案**：直接返回 `GridElem` 会让它在后续 realize 中命中 `GRID_RULE`，从而生成 grid 元素自己的 introspection tag。文献条目已有自己的 backlink/location 体系，重复打 tag 会干扰，故手动 `BlockElem::multi_layouter(packed, crate::grid::layout_grid)` 直接挂 layouter、跳过 show 步骤（注释见 [rules.rs:489-490](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L489-L490)）。

**Q2**：悬挂缩进分支的 `INDENT` 是多少？它和 `HEADING_RULE` 的 indent 来源有何不同？

**参考答案**：固定 `1.5em`（`Em::new(1.5)`）。`HEADING_RULE` 的 indent 是**测量**编号宽度算出来的（动态），而这里是**写死的固定值**——因为文献条目没有统一的「编号前缀」可测（这条分支恰是无前缀的情况）。

---

### 4.5 LAYOUT_RULE：把当前 region 暴露给用户函数

#### 4.5.1 概念说明

`layout` 元素是 Typst 给用户的「逃生口」：用户传一个函数，该函数**收到当前排版的可用尺寸**（宽、高），可以据此决定返回什么内容。典型用法是「窄区域显示简版、宽区域显示完整版」：

```typst
#layout(size => {
  if size.width < 20cm { [紧凑版] } else { [完整版] }
})
```

`LAYOUT_RULE` 的职责是：把这条语义请求挂成一个 layouter 闭包——闭包里读取 `regions.base()` 得到尺寸、调用用户函数、再把用户返回的内容**回排进同一组 regions**。

本规则是本讲的综合实践主角，因为它把前面所有概念（multi_layouter、regions.base、layout_fragment、Context/location）串到了一起。

#### 4.5.2 核心流程

```
LAYOUT_RULE 返回：
  BlockElem::multi_layouter(elem, |elem, engine, locator, styles, regions| {
      ① let Size { x, y } = regions.base();        // x=宽, y=整区域高
      ② let loc = elem.location().unwrap();
      ③ let context = Context::new(Some(loc), Some(styles));
      ④ let result = elem.func
            .call(engine, context.track(), [dict!{ "width"=>x, "height"=>y }])?
            .display();                            // 用户函数返回 Content
      ⑤ crate::flow::layout_fragment(engine, &result, locator, styles, regions)
                                                     // 回排，产 Fragment
  }).pack()
```

要点：

- **用 `multi_layouter`**：闭包接收完整 `regions: Regions`，返回 `Fragment`（由 `layout_fragment` 产出），所以 `layout` 内容可跨页断裂。
- **`regions.base()` 而非 `regions.size`**：`base()` 返回 `(size.x, full)`（[regions.rs:73-75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L73-L75)）——宽度用当前宽，高度用**未被削短的整区域高度**。注释（[rules.rs:743-744](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L743-L744)）说：「Gets the current region's base size, which will be the size of the outer container, or of the page if there is no such container.」即用户拿到的是「外层容器（或页面）的尺寸」，而非当前 region 已被前面内容削短后的剩余高度——这正是用户做响应式决策所期望的「整体可用空间」。
- **回排用 `layout_fragment`**：用户返回的 result 是任意 Content，交给 [flow/mod.rs:56-80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L56-L80) 的 `layout_fragment` 在**同一组 regions** 上排版，得到 `Fragment`，正是 `multi_layouter` 闭包要求的返回类型。
- **闭包不捕获环境**：尽管看起来是个闭包，它只用到形参（`elem`、`engine`、`locator`、`styles`、`regions`），因此可退化为 `fn` 指针，满足 `multi_layouter` 对 `f: fn(...)` 的要求（与 u7-l1 「所有 `*_RULE` 都是不捕获环境的 const 闭包」一致）。

#### 4.5.3 源码精读

完整规则 [rules.rs:739-756](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L739-L756)。

- [rules.rs:745](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L745)：`let Size { x, y } = regions.base();`——尺寸来源。`base()` 实现见 [regions.rs:73-75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L73-L75)：`Size::new(self.size.x, self.full)`。
- [rules.rs:746-747](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L746-L747)：`elem.location().unwrap()` 取位置，`Context::new(Some(loc), Some(styles))` 建上下文——用户函数内的 `context` 语法（如查询）会基于它。
- [rules.rs:748-751](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L748-L751)：`elem.func.call(engine, context.track(), [dict!{ "width" => x, "height" => y }])?.display()`——把宽高打包成字典作为**唯一位置参数**传给用户函数（所以用户写 `size => …` 时，`size` 其实是含 `width`/`height` 字段的对象；常见的 `size.width` 写法正源于此）。`.display()` 把返回值转成 Content。
- [rules.rs:752](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L752)：`crate::flow::layout_fragment(engine, &result, locator, styles, regions)`——回排。`layout_fragment` 的实现见 [flow/mod.rs:56-80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L56-L80)，它把 Engine 拆成 tracked 参数后进入带 `#[comemo::memoize]` 的 `layout_fragment_impl`（u2-l1 / u4-l1）。

> 注意 `layout_fragment` 与 `layout_frame` 的关系（[flow/mod.rs:42-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L42-L51)）：后者是前者的单区域便捷封装。本规则用 `layout_fragment` 而非 `layout_frame`，正是因为用户返回的内容可能跨页，需要多区域多帧能力——这与「挂 multi_layouter」的选择一致。

#### 4.5.4 代码实践

**实践目标**（即讲义规格指定的实践）：追踪 `regions.base()` 如何成为用户 `layout` 函数收到的 `width` / `height`，并说明返回结果如何再交给 `flow::layout_fragment` 排版。

**操作步骤（源码阅读型，逐步追踪）**：

1. **入口**：用户写 `#layout(size => …)`，realize 命中 [LAYOUT_RULE](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L739-L756)，返回 `BlockElem::multi_layouter(elem, <闭包>).pack()`。
2. **挂载**：该块进入 flow，因 `body = MultiLayouter`，在 `layout_multi_block` 命中 [flow/block.rs:184-188](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L184-L188) 的 `MultiLayouter` 分支，`cb.call(engine, locator, styles, regions)` 调起闭包，把**当前 regions** 传入。
3. **取尺寸**：闭包内 `regions.base()` → `Size { x, y }`，其中 `x = regions.size.x`（当前宽度，通常 = 页面正文宽），`y = regions.full`（整区域高度，即页面正文高，**不是**当前页已被上方内容削短后的剩余）。
4. **调用户函数**：`dict!{ "width"=>x, "height"=>y }` 作为参数传给 `elem.func`，用户函数体里的 `size.width` / `size.height` 正是这两个值。
5. **回排**：用户返回的 Content 经 `.display()` 后，`crate::flow::layout_fragment(engine, &result, locator, styles, regions)` 在**同一组 regions** 上排版，产出 `Fragment`（可能多帧）。

**需要观察的现象 / 预期结果**：

- 在一个**页面正文区**里，`size.width` ≈ 页面宽 − 左右边距，`size.height` ≈ 页面高 − 上下边距（整高，不是剩余高）。
- 把 `#layout` 放进一个 `#block(width: 50%)` 或 `#box(width: 5cm)` 内，`size.width` 会变成那个容器的宽度——印证注释「外层容器尺寸，没有容器则是页面」。
- 若用户返回的内容超过一页，会跨页断裂——因为回排走的是 `layout_fragment`（多区域），且整条是 `multi_layouter`。

**待本地验证**（可选小实验）：

```typst
#context {
  // 在 layout 里读到的尺寸
  layout(sz => [宽 #sz.width / 高 #sz.height])
}
```

对比把它放在页面顶层 vs 放进 `#block(width: 6cm)[ ... ]` 内，观察 `sz.width` 的变化。

#### 4.5.5 小练习与答案

**Q1**：为什么传给用户的是 `regions.base()` 而不是 `regions.size`？

**参考答案**：`size` 的 `y` 是「当前 region 已被前面内容削短后的剩余高度」，会随排版进程变化；而 `base()` 的 `y = full` 是整区域高度（稳定）。用户要做响应式决策，需要的是「整体可用空间」（外层容器 / 页面尺寸），而非瞬时剩余，故用 `base()`。

**Q2**：为什么本规则用 `multi_layouter` + `layout_fragment`，而不是 `single_layouter` + `layout_frame`？

**参考答案**：用户函数返回的内容可能超过一个区域、需要跨页断裂；`single_layouter` / `layout_frame` 只能产单帧、不可断裂。用 `multi_layouter` 才能把完整 `Regions` 交给闭包，并用 `layout_fragment` 回排出可多帧的 `Fragment`。

**Q3**：闭包里 `elem.func.call(..., [dict!{ "width"=>x, "height"=>y }])` 这一步，解释了用户侧 `layout(size => size.width)` 里 `size` 的什么结构？

**参考答案**：`size` 是一个含 `width` 和 `height` 两个字段的对象（字典），因为规则把 `x`/`y` 打包成 `dict!{ "width"=>x, "height"=>y }` 作为单一位置参数传入。所以 `size.width`、`size.height` 才成立。

---

## 5. 综合实践

把本讲的五条线索串起来，完成下面这个**源码追踪 + 行为预测**任务。

**任务背景**：下面这段 Typst 源码用到了本讲讲过的多条规则：

```typst
#set heading(numbering: "1.1")
#set page(width: 30cm, height: auto, margin: 2cm)

= 一个很长的标题它需要换行才能放下并且带编号

#figure(
  rect(width: 100%, height: 3cm),
  caption: [示意图],
  placement: bottom,
)

#layout(sz => {
  if sz.width < 25cm { [窄] } else { [宽] }
})
```

**请完成**：

1. **规则匹配**：分别指出 `= …`、`#figure(...)`、`#layout(...)` 三处在 realize 时命中本讲的哪条规则（`HEADING_RULE` / `FIGURE_RULE` / `LAYOUT_RULE`）。
2. **挂法归类**：这三条规则分别用 `multi_layouter` 还是 `single_layouter`？为什么？
3. **标题测量追踪**：那个长标题会触发 HEADING_RULE 的测量分支吗？若触发，`indent` 由哪几部分组成？（提示：`numbering: "1.1"` 下首层标题编号是 `1`。）
4. **layout 尺寸预测**：`#layout` 收到的 `sz.width` 大约是多少？为什么？（注意 `page(width: 30cm, margin: 2cm)` 与它被「放进了一个跨页文档」这一事实无关——`base()` 给的是宽度。）

**参考答案要点**：

1. 命中 `HEADING_RULE`、`FIGURE_RULE`、`LAYOUT_RULE`。
2. 三者都用 **`multi_layouter`**：标题虽最终包成 `BlockElem`，但其测量用了 `layout_frame`（单次测量），而 figure 与 layout 都可能跨页 / 需完整 regions。严格说，`HEADING_RULE` 返回的是普通 `BlockElem::packed` / 带 inset 的 `BlockElem`（body 是 `Content`，不是 layouter）；`FIGURE_RULE` 返回 `BlockElem::packed`（或 `PlaceElem` 包之）；只有 `LAYOUT_RULE` 真正用了 `multi_layouter`。figure 的「浮动」由 `PlaceElem` 在 flow compose 处理。**这里要小心区分「规则返回的块类型」与「是否挂 layouter」**——前两者并非挂 layouter 型规则，只是返回普通块内容。
3. 会触发：`hanging_indent` 默认 `auto`、水平默认左对齐 `Start`，满足 [rules.rs:265](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L265) 的条件。`indent = 编号 "1" 的渲染宽度 + 0.3em`（`SPACING_TO_NUMBERING`）。
4. `sz.width ≈ 30cm − 2cm − 2cm = 26cm`（页面宽减左右各 2cm 边距）。因为 `layout` 收到的是 `regions.base().x`，即正文区宽度；页面正文宽 = 页宽 − 左右边距。`> 25cm`？26cm > 25cm，故应显示「宽」。

> 这个练习故意在第 2 问设了一个陷阱：不要把「规则涉及排版」误等同于「用 multi_layouter」。`HEADING_RULE` 与 `FIGURE_RULE` 返回的是普通 Content / BlockElem，只有 `LAYOUT_RULE`（以及 TABLE/GRID/STACK/LIST 等）才是真正挂 layouter 的。区分二者是本讲的核心收获之一。

## 6. 本讲小结

- **挂法二分**：`BlockElem::multi_layouter`（拿完整 `Regions`、产多帧 `Fragment`、可跨页，用于 table/grid/stack/list/columns/layout 等）与 `single_layouter`（拿单 `Region`、产单 `Frame`、强制 `breakable:false`，用于 image/shapes/transforms/repeat）。flow 在 `layout_single_block` / `layout_multi_block` 里按 `BlockBody` 三变体分派。
- **HEADING_RULE**：用 `Counter::display_at` 求编号文本，在默认（`hanging_indent:auto` + 左对齐）情况下用「无限大 + expand=false」pod 配 `LocatorLink::measure` + `Locator::link` **测量编号宽度**，据此算悬挂缩进 `indent = 编号宽 + 0.3em`，再以「负间距首移 + 起始侧 inset」包块实现悬挂缩进。
- **FIGURE_RULE**：按 caption 位置（Top/Bottom）组装 `caption + gap + body`，追加 `ParbreakElem` 让 body 成段，包进块；`placement` 非空时包成 `PlaceElem(float=true)`，真正的浮动插入在 flow compose（u4-l4）。
- **BIBLIOGRAPHY_RULE**：按「是否有条目带前缀」二分——有则两列 `GridElem` 并**手动挂 `layout_grid` 绕过 GRID_RULE 的 introspection**；无则用固定 `1.5em` 的悬挂缩进块（与 HEADING 同构）。
- **LAYOUT_RULE**：挂 `multi_layouter` 闭包，读 `regions.base()`（宽=当前宽、高=整区域高）打包成 `{width, height}` 字典传给用户函数，再把返回内容用 `flow::layout_fragment` 回排进同一 regions——把「当前可用尺寸」暴露给用户、并保留跨页能力。
- **核心判据**：一条规则返回普通 `BlockElem::packed`（body=Content）还是挂 layouter（body=Single/MultiLayouter），取决于它是否需要**自定义排版函数**；而挂哪种 layouter，取决于内容**是否跨区域断裂**。

## 7. 下一步学习建议

本讲结束，u7「规则注册与集成」单元（也是整本 typst-layout 手册）到此完结。建议：

1. **回看被挂的 layouter 实现**：本讲提到 `crate::grid::layout_grid`、`crate::stack::layout_stack`、`crate::flow::layout_fragment` 等，它们的内部机制分别在 u6-l1（GridLayouter）、u6-l5（StackLayouter）、u4（flow）讲过。可挑选一条规则（如 `TABLE_RULE`）作为入口，从 show rule 一路追到 layouter 内部，完成「语义 → 样式 → 几何」的全链路阅读。
2. **阅读 `rules.rs` 中本讲未展开的复杂规则**：`OUTLINE_ENTRY_RULE`（目录条目，涉及 prefix/body/page 三段与 indented 悬挂缩进）、`QUOTE_RULE`（引号粘连与块级 attribution）、`FOOTNOTE_ENTRY_RULE` 都是很好的进阶练习。
3. **跨 crate 视角**：本讲多次跳到 `typst-library`（container.rs 的 layouter 卡槽、locator.rs 的测量模式、counter.rs 的 display_at）。若想理解「元素如何定义、show rule 如何与元素绑定」，可转向 typst-library 手册的相应讲义。
4. **动手改一条规则**：在本地 fork 中给 `LAYOUT_RULE` 的闭包加一条 `eprintln!` 打印 `regions.base()`，编译若干文档观察输出——这是验证「讲义结论」最直接的方式（注意：本讲义的 worker 规定不改源码，此建议仅供你本地学习时使用）。
