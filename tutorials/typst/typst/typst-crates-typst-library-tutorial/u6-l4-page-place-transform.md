# 页面、定位与变换

## 1. 本讲目标

本讲讲解 Typst 布局里「最外层」与「最自由」的一组元素：负责整页配置的 `page`、负责在容器/页面里自由定位的 `place`、负责对内容施加几何变换的 `move`/`scale`/`rotate`/`skew`/`hide`，以及给内容四周加留白的 `pad`。它们是 u6-l2「Region/Regions → Frame」机制在「页面级」和「叠加级」上的用户入口。

学完本讲你应该能够：

- 说清 `PageElem` 为何是一个「几乎不创建元素节点」的特殊元素——它的页面属性全是 `#[ghost]`（只活在样式链），构造函数返回的是一段带样式的 `Content` 序列而非 `page` 元素，因此 `show page:` 不生效。
- 掌握 `PageElem` 各字段（`paper`/`width`/`height`/`margin`/`bleed`/`binding`/`header`/`footer`/`background`/`foreground`/`numbering` 等）的配置方式，特别是 `Margin` 字典的优先级解析与 `Binding` 的左右翻页。
- 理解 `PlaceElem` 的「叠加（overlay） vs 浮动（float）」两种模式、`alignment` + `dx`/`dy` 的定位语义，以及它为何与 `measure` 函数同根同源。
- 看懂 `move`/`rotate`/`scale`/`skew`/`hide` 这组「不影响布局」的变换元素，以及它们背后统一的二维仿射 `Transform`（2×3 矩阵）的构造与复合。
- 解释 `PadElem` 的字段回退解析链（`rest`/`find` → `x`/`y` → `left`/`top`/`right`/`bottom`），并区分它与 `move`（`move` 不改包围盒，`pad` 改）。

一个贯穿全讲的关键认知（承接 u6-l3、u5-l4）：**本 crate 只负责「元素定义」和「数据归一化」，把内容真正排成 `Frame`、计算变换后包围盒的算法住在 `typst-layout` 里，运行期经 `Routines` 函数指针回调**。所以本讲重点在「这些元素如何描述自己的配置」，而不是「像素如何落下」。

## 2. 前置知识

本讲建立在前面几讲之上，先用通俗语言点出最相关的几点：

- **度量原语（u6-l1）**：`Rel<Length>` 表示「百分比 + 绝对长度」（如 `20% + 5cm`）；`Abs` 是绝对长度；`Angle` 是角度；`Ratio` 是比例；`Alignment` 用 `+` 合成二维对齐（如 `top + left`、`center + horizon`）。它们在本讲里反复作为字段类型出现。
- **区域与帧（u6-l2）**：布局的输入是 `Region`（`size` + `expand`），输出是 `Frame` 帧树。`page` 决定每个区域的尺寸，`place`/变换元素则在某一帧内部叠加或改写内容。
- **`#[elem]` 宏与字段标注（u3-l3）**：`#[required]`（必填）、`#[default(x)]`（带默认）、`#[ghost]`（不入 struct、只活样式链）、`#[fold]`（折叠而非覆盖）、`#[parse(...)]`（覆盖参数解析）、`#[external]`（仅文档）、`#[positional]`（位置参数）、`#[internal]`（内部字段）。本讲的元素字段用全了这些标注，尤以 `PageElem` 的「全员 ghost」最具代表性。
- **样式链与 fold（u4-l1）**：`Smart<T>`（`Auto`/`Custom`）表达「智能默认」；`Fold` 让同名字段层层折叠；`set` 规则把属性写进 `Styles`。`PageElem` 的 `margin` 是 `#[fold]` 的 `Smart<Margin<...>>`，`RotateElem`/`ScaleElem`/`SkewElem` 的 `origin` 也是 `#[fold]`。
- **crate 分离与 Routines（u5-l4）**：行为实现拆到行为 crate，本 crate 用 `Routines` 函数指针表回调。`measure` 函数正是通过 `(engine.library.routines.layout_frame)(...)` 回调 `typst-layout` 来「量出」内容的尺寸。

> 术语提示：「ghost 字段」是本讲反复出现的概念。一个标了 `#[ghost]` 的字段**不会出现在元素 struct 的内存布局里**，它只作为「可设置属性」存在于样式链中——这意味着 `set` 规则能改它，但元素实例本身不存它的值。`PageElem` 的几乎所有属性都是 ghost，这是它最反直觉也最关键的设计。

## 3. 本讲源码地图

本讲涉及的关键文件（均位于 `crates/typst-library/src/layout/` 下）：

| 文件 | 作用 |
| --- | --- |
| [`page.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs) | `PageElem`（整页配置，全员 ghost 字段 + 自定义 `Construct`）、`PagebreakElem`（手动分页）、`Margin`/`Binding`/`Parity`/`Paper`/`PageRanges` 等配套类型与纸张尺寸表。 |
| [`place.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/place.rs) | `PlaceElem`（相对父容器定位）、`PlacementScope`（`column`/`parent`）、`FlushElem`（排空浮动元素）。 |
| [`transform.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs) | `MoveElem`/`RotateElem`/`ScaleElem`/`SkewElem` 四个变换元素，以及统一的二维仿射 `Transform` 结构体（2×3 矩阵的构造/复合/求逆）。 |
| [`pad.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/pad.rs) | `PadElem`（四周加留白），含 `left`/`top`/`right`/`bottom` 四字段及其回退解析链。 |
| [`hide.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/hide.rs) | `HideElem`（隐藏内容但保留布局空间），靠一个内部 ghost 样式 `hidden: bool` 实现。 |
| [`measure.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/measure.rs) | 用户可见的 `measure` 函数：把内容放进一个「探针区域」布局，返回其尺寸。用于说明 `place` 与「测量」的内在联系。 |
| [`mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/mod.rs) | 在 `global()` 中用 `define_elem`/`define_func` 把上述元素与函数注册进标准库。 |

> 真正产出 `Frame`、应用变换、计算浮动排布的算法（`PageLayout`、各种 flow/inline 布局器）在 `crates/typst-layout/` 内，本讲只在概念层面提及。

## 4. 核心概念与源码讲解

### 4.1 PageElem：整页配置

#### 4.1.1 概念说明

`page` 是文档最外层的元素——它把内容排版到一页或多页上，并定义这一组页面的「版式」：纸张大小、宽高、边距、页眉页脚、页码、背景前景、分栏、出血区等等。

但它有一处非常特殊、容易踩坑的设计：**`#page(...)` 构造函数并不真的创建一个 `page` 元素节点**。源码文档明确写道：「The page constructor is special: It doesn't create a page element. Instead, it just ensures that the passed content lives in a separate page and styles it.」后果是——**`show page: ..` 没有任何效果**，因为没有元素节点可供 show 规则匹配；想重复每页内容只能用 `set` 规则去配 `header`/`footer`/`background`/`foreground`。

为什么会这样？因为 `PageElem` 的几乎所有属性都是 **`#[ghost]` 字段**：它们不进入 struct 的内存布局，只作为「可设置属性」活在样式链里（回顾 u3-l3、u4-1）。真正决定一页长什么样的，是「这一段内容被附加的页面样式」，而不是「某个 page 元素实例」。

#### 4.1.2 核心流程

从用户写 `#set page("a4", margin: (x: 2cm), header: [..])` 或 `#page[..]` 到生成页面，大致经历：

1. **解析参数**：`paper` 经 `named_or_find` 取得（可作位置参数，如 `"us-letter"`）；`width`/`height` 若未显式给出则回退到 `paper.width()`/`paper.height()`（见 4.1.3 的 `#[parse]`）。
2. **构造（仅 `#page[..]` 走这条）**：`PageElem::construct` 不调用 `Self::new().pack()`，而是返回一个 `Content` 序列：`weak 分页 + FlushElem（不可见占位） + body + boundary 分页`，再 `.styled_with_map(styles)` 把所有页面属性作为样式贴上去。
3. **样式取值**：布局时从 `StyleChain` 取出 ghost 字段（`width`/`height`/`margin`/...）。`margin` 经 `Fold` 折叠、经 `FromValue` 解析字典。
4. **交给布局（typst-layout）**：分页布局器据此构造每一页的 `Region`（含 margin 切出的正文区、header/footer 区），把 body 排进去，必要时自动分页。

伪代码（构造侧，概念）：

```
// #page(..) 的真实构造逻辑（简化）
styles = PageElem::set(engine, args)?      // 把所有 ghost 属性收进 Styles
body    = args.expect::<Content>("body")?
return Content::sequence([
    PagebreakElem::shared_weak(),          // 先弱分页：开始新的一组页面
    FlushElem::new().pack(),               // 不可见占位：保证空 body 也保留一页、且不继承外部共享样式
    body,
    PagebreakElem::shared_boundary(),      // 再分页：结束这一组，恢复之前的页面样式
]).styled_with_map(styles)
```

#### 4.1.3 源码精读

**元素声明与「全员 ghost」**。`PageElem` 带了 `Construct` 能力，意味着它自定义构造逻辑：

[src/layout/page.rs:53-54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L53-L54) — 元素声明 `#[elem(since = "forever", Construct)]`，struct 开头。

注意接下来几乎所有字段都带 `#[ghost]`：`width`、`height`、`flipped`、`margin`、`bleed`、`binding`、`columns`、`fill`、`numbering`、`header`、`footer`、`background`、`foreground`……而 `body` 是 `#[external] #[required]`（external 表示它只存在于文档里、不是真实字段）。也就是说，**`PageElem` 实例本身几乎没有数据**，它更像「一组可设置属性的命名空间 + 一段构造逻辑」。

**`width`/`height` 的回退解析**。`paper` 是 `#[external]`（只进文档），真正存的是 `width`/`height`。它们的 `#[parse]` 把「纸张快捷方式」翻译成具体尺寸：

[src/layout/page.rs:81-88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L81-L88) — `width` 字段：先取显式 `width`，没有则用 `paper.width()`；默认 A4 宽。`#[ghost]` 表示它不进 struct，只进样式链。

`height` 同理（[:97-103](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L97-L103)）。注意 `height` 设为 `auto` 时页面会随内容增长，只有手动 `pagebreak` 才换页——这对本讲义这类「示例文档」很常见。

**`margin`：折叠 + 字典解析**。`margin` 是 `Smart<Margin<Smart<Rel<Length>>>>`，带 `#[fold]` 和 `#[ghost]`：

[src/layout/page.rs:166-168](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L166-L168) — `margin` 字段声明。外层 `Smart` 表示可给 `auto`（自动按 2.5/21 比例算边距，A4 下即 2.5cm）；内层 `Margin` 是四边 + two_sided。

`Margin` 结构体本身：

[src/layout/page.rs:597-604](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L597-L604) — `Margin<T>` 含 `sides: Sides<Option<T>>`（四边）与 `two_sided: Option<bool>`（是否按 inside/outside 区分左右，用于装订）。

字典解析的优先级链在 `FromValue for Margin` 里：

[src/layout/page.rs:667-722](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L667-L722) — 从字典取值。优先级是：`rest`（其余各边）→ `x`/`y`（成对）→ `top`/`bottom`/`left`/`right`/`inside`/`outside`（单边，更具体覆盖更宽泛）。同时检测 `inside`/`outside` 与 `left`/`right` 互斥（[:690-694](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L690-L694)），并用 `two_sided` 记下是否进入了「装订模式」。最终四边合并为 `inside.or(left).or(x)` 等（[:711-714](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L711-L714)）。

`Margin` 还实现了 `Fold`（[:613-620](https://github.com/typst/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L613-L620)），把四边与 `two_sided` 分别折叠——这就是为什么多次 `set page(margin: ..)` 能逐边叠加而非整体覆盖。

**`header`/`footer` 与 ascent/descent**：

[src/layout/page.rs:366-367](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L366-L367) — `header: Smart<Option<Content>>`：`auto`（按 number-align 自动放页码）、`none`（抑制）、或具体内容。`footer` 同构（[:403-404](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L403-L404)）。

`header_ascent`（[:371-373](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L371-L373)）与 `footer_descent`（[:448-450](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L448-L450)）控制页眉上移/页脚下沉进边距的比例，默认都是 `30%`（相对各自边距高度）。

**页码相关**：`numbering`（[:310-311](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L310-L311)）接受编号模式或函数；`number_align`（[:342-344](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L342-L344)）默认 `center + bottom`（放页脚），若竖直分量是 `top` 则放进页眉。`supplement`（[:323-324](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L323-L324)）用于引用时加前缀（如 `p.`）。

**`background`/`foreground` 与 `bleed`**：背景/前景是 `Option<Content>`（[:473-474](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L473-L474)、[:491-492](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L491-L492)）。它们的相对长度是**针对含 `bleed`（出血区）的整页尺寸**解析的——这是把水印/满版背景铺到裁切线之外的关键。`bleed` 字段见 [:218-219](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L218-L219)。

**`binding` 与左右翻页**：

[src/layout/page.rs:725-745](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L725-L745) — `Binding` 枚举（`Left`/`Right`）与 `swap(number)`：左侧装订时偶数页交换左右（因为第 1 页是对的），右侧装订时奇数页交换。这决定了 `inside`/`outside` 边距落在物理左边还是右边。

**构造函数的「不创建节点」**：

[src/layout/page.rs:504-524](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L504-L524) — `impl Construct for PageElem`。注释点明三件事：(1) 不创建 page 元素节点，所以 show 规则匹配不到；(2) `FlushElem::new().pack()` 这个「无效果不可见非标签元素」有两个好处——body 为空也保留一页、且该页不继承 body 的共享样式；(3) 用 `shared_weak`/`shared_boundary` 两个全局单例分页元素包裹。

这里有个跨文件的有趣细节：`FlushElem` 其实定义在 `place.rs`（见 4.2），却被 `page.rs` 直接用作「占位元素」——两个元素在「分页边界」语义上彼此借用。

**`PagebreakElem` 与全局单例**：

[src/layout/page.rs:581-594](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L581-L594) — `shared_weak()`/`shared_boundary()` 用 `singleton!` 缓存全局共享的弱分页元素（回顾 u5-l2 的 `singleton!`、u12-l2 的性能手段）。`weak: true` 表示当前页已空则跳过；`to: Some(Parity)` 可强制下一页为奇/偶页（[:559-569](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L559-L569)）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`page` 不创建元素节点」与「`margin` 字典优先级」两个结论。

**操作步骤**：

1. 新建 `page-demo.typ`，写入：

   ```typ
   // (a) 验证 show page: 无效
   #show page: set text(red)   // 这行不会有任何效果
   #set page("a4", margin: (top: 4cm, x: 3cm))

   // (b) 验证 margin 字典优先级：rest -> x/y -> 单边
   #set page(margin: (rest: 1cm, x: 2cm, left: 3cm))
   #rect(width: 100%, height: 100%, fill: aqua.lighten(80%))

   #page[
     这是一个被 #page(..) 包裹的独立页面。
   ]
   ```

2. 用 Typst CLI 编译：`typst compile page-demo.typ`。
3. 打开生成的 PDF。

**需要观察的现象**：

- (a) 文字不会变红——因为 `show page:` 匹配不到任何元素节点（构造函数返回的是序列，不是 `page` 元素）。若把 `show page:` 改成 `show: set text(red)`（不带 `page:` 的全局 show），文字才会变红，形成对照。
- (b) 四条边距分别是多少？按优先级链：`left` 显式给 `3cm` 覆盖 `x` 的 `2cm`；`right` 没单边值，回落到 `x` 的 `2cm`；`top`/`bottom` 都没单边、没 `y`，回落到 `rest` 的 `1cm`。所以正文框左右 `2cm`/`3cm`（左 `3cm`）、上下 `1cm`。
- (c) `#page[..]` 后会强制开始一个新页（因为 `shared_weak` 弱分页），且这页用的是 `#page[..]` 之前的页面样式。

**预期结果**：背景矩形与正文区域正好吻合上述边距；`(a)` 的红色不生效。若你的边距观察与上述不符，请重读 [:667-722](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L667-L722) 的取值顺序。

> 若本地未安装 Typst CLI，编译步骤「待本地验证」；字段优先级的源码结论不依赖运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PageElem` 的 `body` 标了 `#[external]` 而不是普通的 `#[required]`？这对「`page` 元素实例里存了什么」意味着什么？

**答案**：`#[external]` 表示该字段**只存在于文档中**，不会成为 struct 的真实字段、也不进样式链。配合几乎所有属性都是 `#[ghost]`，意味着一个 `PageElem` 实例在内存里几乎没有数据——它只是「一组可设置属性的载体 + 一段构造逻辑」。真正的「页面」是由这段构造逻辑产出的「带页面样式的 Content 序列」在布局阶段形成的，而非一个 `page` 元素节点。

**练习 2**：`set page(margin: (inside: 2cm, left: 1cm))` 会发生什么？为什么？

**答案**：会报错。`inside`/`outside` 与 `left`/`right` 互斥——前者用于装订（two_sided）模式按内外侧区分左右，后者直接指定物理左右，二者不能同时出现。源码在 [:690-694](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L690-L694) 用 `bail!` 拦截。

---

### 4.2 PlaceElem：相对父容器定位

#### 4.2.1 概念说明

`place` 解决的问题是「在父容器（或当前页文字区）里，把一段内容放到任意位置，而不挤占正常流」。它有两种根本不同的模式：

- **叠加（overlay，默认）**：内容按 `alignment` 对齐到父容器，**覆盖**在已有内容之上，不占流空间。常用于绝对定位、水印、角标。
- **浮动（float）**：内容被推到父容器的顶部或底部，**挤开**正常流内容（像 CSS 的 float）。常用于图表（`figure` 默认就是浮动的 `place`）。

无论哪种模式，都可以用 `dx`/`dy` 做额外偏移——而且这个偏移**不影响布局**（源码注释直言：「the placed content is treated as if it were wrapped in a `move` element」）。

`PlaceElem` 还带有三个能力标注：`Unqueriable`、`Locatable`、`Tagged`（回顾 u3-l2）。这意味着 placed 元素可被定位、可被打标签，但不能直接被 `query`（`Unqueriable` 必须配 `Locatable`）——因为「位置」本身就是内省产物，查询它需要走 location 而非元素匹配。

#### 4.2.2 核心流程

从用户写 `#place(top + right, dx: 5pt, square())` 到定位完成：

1. **解析参数**：`alignment` 是位置参数（`#[positional]`），默认 `Smart::Custom(Alignment::START)`（即 `top + start`，注意是 `Smart`，可给 `auto`）。
2. **取配置**：`float`（是否浮动）、`scope`（`column`/`parent`，决定相对当前列还是跨所有列的父容器）、`clearance`（浮动时与流内容的间距，默认 `1.5em`）、`dx`/`dy`（额外偏移）。
3. **布局（typst-layout）**：
   - 先**测出 body 的尺寸**（这一步本质就是 `measure`，见 4.2.4）。
   - 按 `alignment` 计算在父容器中的锚点；浮动模式则找最近的顶/底空位、考虑 `clearance`。
   - 把 `dx`/`dy`（若是 `Rel` 比例则相对父容器尺寸 resolve）叠加到锚点上，得到最终位置，把 body 的帧绘制到该处。
4. **flush**：浮动元素会「攒着」直到遇到 `place.flush()` 或自然边界才统一排放。

伪代码（布局侧，概念）：

```
size_body = layout(body)                 // 先量出 body 尺寸（= measure）
anchor    = alignment.anchor_in(parent)  // 如 top+right -> 父容器右上角
dx_res    = dx.resolve(parent.width)     // 比例相对父容器宽
dy_res    = dy.resolve(parent.height)
pos       = anchor + (dx_res, dy_res) - align_offset(size_body)
draw frame(body) at pos
```

#### 4.2.3 源码精读

**元素声明与能力**：

[src/layout/place.rs:74-75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/place.rs#L74-L75) — `#[elem(scope, since = "forever", Unqueriable, Locatable, Tagged)]`，`pub struct PlaceElem`。`scope` 表示它有子作用域（下文 `place.flush`）。

**关键字段**：

[src/layout/place.rs:85-87](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/place.rs#L85-L87) — `alignment: Smart<Alignment>`，位置参数，默认 `START`。叠加模式可给任意非 `auto` 对齐；浮动模式只能给 `auto`/`top`/`bottom`。

[src/layout/place.rs:112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/place.rs#L112) — `scope: PlacementScope`：

[src/layout/place.rs:182-190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/place.rs#L182-L190) — `PlacementScope` 枚举：`Column`（默认，相对当前列）/`Parent`（跨所有列的父容器，常用于多栏文档里做通栏标题；目前仅 `float: true` 时支持）。

[src/layout/place.rs:137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/place.rs#L137) — `float: bool`（默认 `false`）。

[src/layout/place.rs:143-144](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/place.rs#L143-L144) — `clearance: Length`（默认 `1.5em`），仅浮动时生效。

[src/layout/place.rs:163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/place.rs#L163) 与 [:169](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/place.rs#L169) — `dx: Rel<Length>` / `dy: Rel<Length>`。注意是 `Rel<Length>`，所以可以是 `50%`（相对父容器该轴尺寸）或绝对值。

[src/layout/place.rs:172-173](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/place.rs#L172-L173) — `body: Content`，必填。

**`FlushElem`（place 的子元素）**：

[src/layout/place.rs:176-180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/place.rs#L176-L180) — `#[scope] impl PlaceElem { #[elem] type FlushElem; }`：把 `FlushElem` 挂为 `place` 函数的子作用域，故用户写 `place.flush()`。

[src/layout/place.rs:212-213](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/place.rs#L212-L213) — `FlushElem` 本体：空元素，作用是「请布局器先把积压的浮动元素排放掉再继续」，常用于防止浮动图表溢到下一节。

#### 4.2.4 代码实践

**实践目标**：用 `measure` 函数显式复现 `place` 内部「先量尺寸再定位」的过程，直观理解 `alignment + dx/dy` 与 `measure` 的同根关系。

**`alignment + dx/dy` 与 `measure` 的关系**（核心解释）：

1. **`dx`/`dy` 是 `Rel<Length>`，比例要相对父容器尺寸 resolve**（回顾 u6-l1）。当 `dx: 50%` 时，引擎必须知道父容器宽度才能算出偏移——这和 `measure` 的 `width`/`height` 参数同为「相对长度的基准」是同一类问题。
2. **`alignment` 定位需要 body 自身的尺寸**。要把一个方块「居中」，引擎既要知道容器中心，也要知道方块多大（才能减去半个宽高）——**算出 body 尺寸这一步，本质上就是测量**。
3. **`measure` 函数正是这种「测尺寸」的显式、用户可见形态**：

   [src/layout/measure.rs:47-105](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/measure.rs#L47-L105) — `measure` 把内容放进一个「探针区域」布局，返回其 `width`/`height`。它构造 pod 区域（[:79-85](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/measure.rs#L79-L85)），再回调 `(engine.library.routines.layout_frame)(...)`（[:96-102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/measure.rs#L96-L102)）真正布局。

   也就是说：**`place` 内部（在 typst-layout）做的事——「布局 body 得到尺寸 → 据此计算 alignment 锚点 → 叠加 resolve 后的 dx/dy」——和 `measure` 共用同一条 `layout_frame` 通路**。`measure` 只是把「只要尺寸」这一步单独暴露给用户。此外 `measure` 还用 `LocatorLink::measure`（[:91-93](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/measure.rs#L91-L93)）进入「测量模式」，让被测内容里的内省特性仍能工作——这与 `PlaceElem` 带 `Locatable`/`Tagged` 是同源的考量。

**操作步骤**：

1. 新建 `place-measure.typ`：

   ```typ
   #set page(width: 200pt, height: auto, margin: 12pt)

   // 用 measure 显式量出方块的尺寸
   #context {
     let s = measure(square(size: 30pt, fill: blue))
     [方块尺寸 = #s.width × #s.height]
   }

   // 用 place 把同样方块定位到右上角，再用 dx/dy 微调
   #rect(width: 100%, height: 60pt, fill: luma(230), place(top + right, dx: -8pt, dy: 8pt,
     square(size: 30pt, fill: blue)))
   ```

2. `typst compile place-measure.typ`。

**需要观察的现象**：

- 第一行应输出 `方块尺寸 = 30pt × 30pt`——这正是 `place` 内部为了把方块对齐到 `top + right` 所必须先知道的信息。
- 第二行：方块本应贴在灰色矩形的右上角（`top + right`）；`dx: -8pt` 把它往左推、`dy: 8pt` 往下推，于是方块稍稍内移。注意灰色矩形本身**没有因 place 而变大**——`place` 是叠加、不占流空间。

**预期结果**：方块位于右上角内侧偏移处；`measure` 报告的尺寸与方块实际占用一致。这印证了「place 定位 = 测量 body + 计算锚点 + 偏移」。

> 编译步骤「待本地验证」；`alignment+dx/dy 与 measure 同源 layout_frame` 的结论来自源码，不依赖运行。

#### 4.2.5 小练习与答案

**练习 1**：`#place(center + horizon, dx: 50%, rect[..])` 中，`dx: 50%` 相对什么解析？为什么不会导致 rect 被推出页面？

**答案**：相对**父容器**（这里是页文字区）的宽度解析为绝对长度，再加到 `center + horizon` 算出的锚点上。它只改变绘制位置、不进入流式布局（等价于包了一层 `move`），所以不会「挤」别的内容，也不会触发自动分页。

**练习 2**：为什么 `PlaceElem` 同时标了 `Locatable` 和 `Unqueriable`？二者矛盾吗？

**答案**：不矛盾。`Locatable` 表示它**能**被赋予一个文档位置（location），供 `locate`/`here` 等使用；`Unqueriable` 表示它**不能**作为元素类型被 `query` 直接匹配。回顾 u3-l2，`Unqueriable` 必须配 `Locatable`——因为 placed 元素的「身份」本质是它的位置而非它的类型，查询它要靠 location 而非元素匹配。

---

### 4.3 Move/Scale/Rotate/Skew/Hide：变换元素

#### 4.3.1 概念说明

这是一组「**变换内容但不改变布局**」的元素（`hide` 是「不改变布局地隐藏」）。它们的共同口号是：**layout still 'sees' it at the original position**——容器仍按内容「未变换前」的尺寸来排版，变换只影响绘制。除 `hide` 外，每个变换元素都有一个 `reflow` 开关：设为 `true` 时才会把变换后的包围盒反馈给布局、重新计算占位。

| 元素 | 作用 | 关键字段 | reflow |
| --- | --- | --- | --- |
| `MoveElem` | 平移 | `dx`/`dy` | 无（永远不影响布局） |
| `RotateElem` | 旋转 | `angle`/`origin`/`reflow` | 可选 |
| `ScaleElem` | 缩放（含镜像） | `factor`/`x`/`y`/`origin`/`reflow` | 可选 |
| `SkewElem` | 倾斜 | `ax`/`ay`/`origin`/`reflow` | 可选 |
| `HideElem` | 隐藏（保留布局空间） | `body`（+ 内部 ghost `hidden`） | 无 |

这四个变换（move/scale/rotate/skew）背后共用同一个数学对象——二维仿射变换 `Transform`（一个 2×3 矩阵）。本 crate 里 `transform.rs` 既定义了「用户可见的四个元素」，也定义了「机器使用的 `Transform` 结构体」。

#### 4.3.2 核心流程

变换如何作用于内容：

1. **解析参数**：`move` 取 `dx`/`dy`；`rotate` 取位置参数 `angle` + `origin` + `reflow`；`scale` 取可选位置 `factor`（同时给 `x`/`y`）+ `origin` + `reflow`；`skew` 取 `ax`/`ay` + `origin` + `reflow`。
2. **构造 `Transform`**：把「平移到 origin → 施加旋转/缩放/倾斜 → 平移回」组合成一个矩阵（`scale_at`/`rotate_at`）。
3. **布局（typst-layout）**：先按「未变换」布局 body 得到原始 `Frame`；把 `Transform` 作用到帧上每个点的坐标；若 `reflow: true`，再计算变换后包围盒并据其调整外部布局，否则沿用原始包围盒（变换后内容可能溢出/重叠）。
4. **`hide` 特殊**：它不构造几何变换，而是给 body 打上内部 ghost 样式 `hidden: bool = true`，让渲染阶段跳过绘制（但布局照常）。

#### 4.3.3 源码精读

**`MoveElem`**：

[src/layout/transform.rs:28-39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L28-L39) — `MoveElem` 只有 `dx`/`dy`（`Rel<Length>`）和必填 `body`。注意它**没有** `reflow`：移动永远不影响布局（这正是 `place` 的 `dx`/`dy` 等价于「包一层 `move`」的含义）。

**`RotateElem`**：

[src/layout/transform.rs:55-99](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L55-L99) — `angle`（位置参数）、`origin`（`#[fold]`，默认 `center + horizon`）、`reflow`（默认 `false`）。`origin` 决定绕哪一点转：如想转完后底边仍贴基线，就设 `bottom + left`。

**`ScaleElem`**：

[src/layout/transform.rs:111-163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L111-L163) — `factor` 是 `#[external]` 位置快捷方式（同时设 `x`/`y`）。真正的字段是 `x`/`y`，它们的 `#[parse]` 把位置 `factor`（`args.find()`）与命名 `x`/`y` 合并（[:124-127](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L124-L127)、[:134](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L134)）。`ScaleAmount` 既可是比例也可是长度：

[src/layout/transform.rs:166-180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L166-L180) — `ScaleAmount` 枚举（`Ratio`/`Length`）及其 `cast!`。负比例可做镜像（如 `x: -100%` 水平翻转）。

**`SkewElem`**：

[src/layout/transform.rs:193-240](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L193-L240) — `ax`/`ay`（默认 `0`）、`origin`（`#[fold]`）、`reflow`（默认 `false`）。倾斜角经 `tan` 进入矩阵（见下）。

**核心数学：`Transform` 结构体**。它是 2×3 仿射矩阵（线性部分 2×2 + 平移 2×1）：

[src/layout/transform.rs:243-251](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L243-L251) — `Transform { sx, ky, kx, sy, tx, ty }`。一个点 \((x,y)\) 变换为：

\[
\begin{pmatrix} x' \\ y' \end{pmatrix}
=
\begin{pmatrix} sx & kx \\ ky & sy \end{pmatrix}
\begin{pmatrix} x \\ y \end{pmatrix}
+
\begin{pmatrix} tx \\ ty \end{pmatrix}
\]

注意 `kx`/`ky` 是「交叉项」（倾斜/旋转引入的非对角元）；`is_only_scale`（[:318-323](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L318-L323)）和 `is_only_translate`（[:326-331](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L326-L331)）是布局器用来走快速路径的判断。

**基本构造**：`identity`（[:255-264](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L255-L264)）、`translate`（[:267-269](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L267-L269)）、`scale`（[:272-274](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L272-L274)）。

**「在某点处」变换**：旋转/缩放若要绕指定 `origin` 进行，用「平移到 origin → 变换 → 平移回」三明治：

[src/layout/transform.rs:277-288](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L277-L288) — `scale_at`/`rotate_at`：`translate(px,py).pre_concat(scale/rotate).pre_concat(translate(-px,-py))`。

**`rotate` 与 `skew` 的矩阵**：

[src/layout/transform.rs:291-301](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L291-L301) — `rotate(angle)`：用 `cos`/`sin` 填入 `sx=sy=cos, ky=sin, kx=-sin`（标准二维旋转矩阵）。

[src/layout/transform.rs:304-310](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L304-L310) — `skew(ax, ay)`：`kx = tan(ax), ky = tan(ay)`。

**矩阵复合 `pre_concat`**（最重要的算子）：

[src/layout/transform.rs:334-343](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L334-L343) — `pre_concat(prev)`：返回 `self ∘ prev`（先施加 `prev`，再施加 `self`），即矩阵乘法 \(M_{self} \cdot M_{prev}\)。这就是把多个变换「串起来」的方式。`post_concat`（[:346-348](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L346-L348)）是其反序版本。

**`invert`**：

[src/layout/transform.rs:353-397](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L353-L397) — 求逆。行列式为 0（退化变换）返回 `None`；对纯缩放-平移（`kx=ky=0`）走快速路径（[:360-375](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L360-L375)），否则用伴随矩阵法（[:377-396](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L377-L396)）。求逆用于把「屏幕坐标」反推回「内容坐标」（如点击命中测试）。

**`HideElem`**：

[src/layout/hide.rs:24-34](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/hide.rs#L24-L34) — `HideElem` 带 `Tagged`，只有必填 `body` 和一个内部 ghost `hidden: bool`。注释说明：隐藏的内容**既不可见也对辅助技术（AT）不可达**，但布局照常——因此 `#hide[Hello] Joe` 会留出 "Hello" 的空白再接 "Joe"。源码还提醒：基于尺寸可能被逆推，不宜用它隐藏高度敏感信息。

#### 4.3.4 代码实践

**实践目标**：直观对比 `reflow: false`（默认，不影响布局）与 `reflow: true`（影响布局）的差异，并验证 `scale` 镜像。

**操作步骤**：

1. 新建 `transform-demo.typ`：

   ```typ
   #set align(center)

   默认（reflow: false）：旋转不挤占邻居
   #box(square(width: 8pt))
   #box(rotate(45deg, square(width: 8pt)))   // 旋转的方块会与左右重叠
   #box(square(width: 8pt))

   #v(1em)

   reflow: true：旋转后的包围盒反馈给布局
   #box(square(width: 8pt))
   #box(rotate(45deg, reflow: true, square(width: 8pt)))
   #box(square(width: 8pt))

   #v(1em)

   镜像：scale(x: -100%)
   #scale(x: -100%)[这是水平镜像的文字 ABCD]
   ```

2. `typst compile transform-demo.typ`。

**需要观察的现象**：

- 第一行：中间方块旋转 45° 成菱形，它的**四个角溢出**原本 8pt 的方框，与左右方块重叠（因为布局仍按 8pt 方框预留）。
- 第二行：`reflow: true` 后，中间预留的方块变大（变成旋转后菱形的外接包围盒），左右方块被推开、不再重叠。
- 第三行：文字被水平翻转（镜像）。

**预期结果**：reflow 开关前后的间距差异清晰可见；镜像文字左右翻转。这印证「变换默认不改包围盒，reflow 才改」。

> 编译步骤「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`rotate(45deg)` 默认绕哪一点旋转？如何让它绕左下角旋转？

**答案**：默认绕 `origin: center + horizon`（元素中心）。要绕左下角，设 `origin: bottom + left`（见 [:80-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L80-L82)）。底层经 `rotate_at(angle, px, py)` 用「平移到该点 → 旋转 → 平移回」实现。

**练习 2**：`Transform::pre_concat(prev)` 和 `post_concat(next)` 各表示什么复合顺序？若想「先缩放再旋转」，应该怎么串？

**答案**：`pre_concat(prev)` 返回 `self ∘ prev`（先施加 `prev`，再施加 `self`）；`post_concat(next)` 返回 `next ∘ self`（先施加 `self`，再施加 `next`），它就是 `next.pre_concat(self)`。要「先缩放再旋转」：`rotate(a).pre_concat(scale(sx, sy))`，或等价地 `scale(sx, sy).post_concat(rotate(a))`（见 [:334-348](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L334-L348)）。

---

### 4.4 PadElem：四周加留白

#### 4.4.1 概念说明

`pad` 给内容四周加留白（padding）。它和 `move` 看似都在内容外加空间，但方向相反：**`move` 平移内容但不改包围盒（留白不变）；`pad` 不平移内容但扩大包围盒（留白变大）**。`pad` 是真实地改变了布局所占空间。

它支持四种粒度：`rest`（四边同值）、`x`/`y`（成对）、`left`/`top`/`right`/`bottom`（单边），更具体的覆盖更宽泛——这套优先级与 `PageElem` 的 `margin` 字典几乎同构（对比 4.1.3）。

#### 4.4.2 核心流程

1. **解析参数**：四个真实字段 `left`/`top`/`right`/`bottom` 各自的 `#[parse]` 把 `rest`/`find`（四边）、`x`/`y`（成对）、单边按优先级合并。
2. **样式取值**：四边都是 `Rel<Length>`，可给比例（相对容器）。
3. **布局（typst-layout）**：把 body 放进一个「缩小了 padding 的内部区域」布局，再把得到的帧整体偏移到加上 padding 后的外框里——外框尺寸 = body 尺寸 + 左右 padding + 上下 padding。

#### 4.4.3 源码精读

**元素声明与字段回退链**：

[src/layout/pad.rs:17-26](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/pad.rs#L17-L26) — `PadElem`。`left` 的 `#[parse]`：

```
let all = args.named("rest")?.or(args.find()?);   // rest 或位置参数 -> 四边
let x   = args.named("x")?.or(all);               // x 覆盖 all
let y   = args.named("y")?.or(all);               // y 覆盖 all
args.named("left")?.or(x)                          // left 覆盖 x
```

[src/layout/pad.rs:29-38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/pad.rs#L29-L38) — `top` = `named("top").or(y)`；`right` = `named("right").or(x)`；`bottom` = `named("bottom").or(y)`。

**`#[external]` 快捷方式**：

[src/layout/pad.rs:41-50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/pad.rs#L41-L50) — `x`/`y`/`rest` 标 `#[external]`：它们**只存在于文档**，不是真实字段。真实字段只有 `left`/`top`/`right`/`bottom`；`x`/`y`/`rest` 的值在 `#[parse]` 阶段就被「摊」进了这四个字段。这与 `PageElem` 的 `paper` 标 `#[external]` 是同一手法（回顾 u3-l3）。

[src/layout/pad.rs:53-54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/pad.rs#L53-L54) — `body: Content`，必填。

> 对比 `PageElem::margin`（字典解析，[:667-722](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L667-L722)）与 `PadElem`（多字段 `#[parse]` 回退，[:20-38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/pad.rs#L20-L38)）：两者都在实现「rest → x/y → 单边」的优先级，只是 margin 用字典 + `Margin` 结构 + `Fold`，pad 用四个独立字段 + `#[parse]` 回退。pad 不折叠（没有 `#[fold]`），每次设置直接覆盖对应边。

#### 4.4.4 代码实践

**实践目标**：验证 `pad` 的字段优先级，并对比 `pad`（改包围盒）与 `move`（不改包围盒）。

**操作步骤**：

1. 新建 `pad-demo.typ`：

   ```typ
   // 优先级：left(单边) 覆盖 x(成对) 覆盖 rest(四边)
   #rect(fill: luma(230),
     pad(rest: 4pt, x: 8pt, left: 16pt, rect(fill: white, [内容]))
   )

   #v(0.5em)

   // 对比：pad 扩大包围盒，move 不扩大
   #let box-with-label(content) = rect(stroke: red, content)

   #box-with-label(pad(x: 10pt, [pad 加了留白])) \
   #box-with-label(move(dx: 10pt, [move 只是平移]))
   ```

2. `typst compile pad-demo.typ`。

**需要观察的现象**：

- 第一个矩形：`left` 实际是 `16pt`（单边覆盖 `x` 的 `8pt`）；`right` 回落到 `x` 的 `8pt`；`top`/`bottom` 回落到 `rest` 的 `4pt`。内容矩形四周的留白厚度应与此一致。
- 第二组：`pad` 版本的外框（红框）明显比内容宽（左右各多 `10pt`）；`move` 版本的红框与内容同宽，但**内容被往右平移**、可能溢出红框右边——直观体现「pad 改包围盒、move 不改」。

**预期结果**：两组的留白/平移差异清晰可见。

> 编译步骤「待本地验证」；字段优先级结论来自源码 `#[parse]` 回退链。

#### 4.4.5 小练习与答案

**练习 1**：`#pad(top: 2pt, x: 6pt, rest: 4pt, [X])` 最终四边各是多少？

**答案**：`rest=4pt` 是兜底；`x=6pt` 覆盖左右为 `6pt`；`top=2pt` 覆盖上为 `2pt`；`bottom` 没单边、没 `y`，回落到 `rest` 的 `4pt`。所以左 `6pt`、右 `6pt`、上 `2pt`、下 `4pt`。

**练习 2**：为什么 `PadElem` 的 `left`/`top`/`right`/`bottom` 没有标 `#[fold]`，而 `PageElem` 的 `margin` 标了？

**答案**：`pad` 是「一次性包裹」语义——每次调用就重新设定四边，不需要多次叠加，故用覆盖式取值（无 `#[fold]`）。`margin` 是「文档级属性」语义——可能被多次 `set page(margin: ..)` 逐边修改（如先设 `x` 再单独改 `left`），需要折叠保留未提及的边，故 `Margin` 实现了 `Fold`（[:613-620](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L613-L620)）。

---

## 5. 综合实践

把本讲的「页面 + 定位 + 变换 + 留白」串起来，做一张带角标水印的「名片页」。

**任务**：用 `page` 配置一张 90mm×54mm 的名片（含出血与背景），用 `place` 在四角放装饰角标，用 `scale`/`rotate` 做一个倾斜水印，用 `pad` 给文字加内边距。

**参考实现**（示例代码）：

```typ
 #set page(
   "eu-business-card",
   flipped: false,
   margin: 0pt,
   bleed: 3mm,
   fill: rgb("f2e5dd"),
   background: place(center + horizon,
     rotate(-12deg, text(28pt, fill: rgb("FFCBC4"))[*SAMPLE*])
   ),
 )

 #place(top + left, dx: 4mm, dy: 4mm, text(8pt, fill: gray)[◆])
 #place(top + right, dx: -4mm, dy: 4mm, text(8pt, fill: gray)[◆])
 #place(bottom + left, dx: 4mm, dy: -4mm, text(8pt, fill: gray)[◆])
 #place(bottom + right, dx: -4mm, dy: -4mm, text(8pt, fill: gray)[◆])

 #pad(x: 6mm, y: 4mm)[
   #set align(left + horizon)
   #text(14pt)[*Sam H. Richards*] \
   #text(9pt, fill: gray)[Procurement Manager]

   #v(1fr)

   #text(8pt)[
     23 W 23rd Street \
     New York, NY 10010
   ]
 ]
```

**操作步骤**：

1. 把上面内容存为 `card.typ`，`typst compile card.typ`。
2. 逐项核对：`page` 用了纸张快捷方式 + `bleed` + `background`（背景相对含 bleed 的整页解析，所以 `place` 居中的水印铺满到裁切线）；四个 `place` 用 `top/bottom + left/right` + `dx`/`dy` 微调到四角内侧；水印用 `rotate` 倾斜；正文用 `pad` 加内边距、`align` 垂直居中。
3. **进阶**：把 `background` 里的 `rotate` 改成 `scale(x: -100%, rotate(-12deg, ...))`，观察水印被镜像后再倾斜；再给 `place` 的角标加 `scope: "parent", float: true`（注意需配 `float`），观察跨页/跨栏行为。

**预期结果**：一张带四角装饰、倾斜水印、出血背景的名片，文字垂直居中且有内边距。若背景未铺到裁切线，检查是否漏了 `bleed`（背景相对长度按含 bleed 的整页解析，见 [:457-461](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L457-L461)）。

> 编译步骤「待本地验证」。

## 6. 本讲小结

- `PageElem` 是「全员 ghost」的特殊元素：几乎所有属性（`width`/`height`/`margin`/`bleed`/`binding`/`header`/`footer`/`background`/`foreground`/`numbering`...）都不进 struct、只活样式链；其 `Construct` 不创建元素节点，而是返回 `weak分页 + FlushElem + body + boundary分页` 的带样式序列，因此 `show page:` 无效。
- `Margin` 的字典解析遵循 `rest → x/y → 单边` 的优先级，`inside`/`outside` 与 `left`/`right` 互斥，`Binding` 按页码奇偶翻页决定内外边距落点；`margin` 实现 `Fold` 以支持多次逐边修改。
- `PlaceElem` 分「叠加（overlay，不占流）」与「浮动（float，挤开流）」两种模式，由 `alignment`（默认 `START`）、`float`、`scope`、`clearance`、`dx`/`dy` 配置；它的定位与 `measure` 同根——都要先把 body 布局出尺寸（经 `layout_frame` routine），再据 `alignment` + resolve 后的 `dx`/`dy` 计算位置。
- 变换元素 `move`/`rotate`/`scale`/`skew` 的口号是「变换但不改布局」（`reflow: true` 才反馈包围盒），背后共用二维仿射 `Transform`（2×3 矩阵），靠 `pre_concat`/`post_concat` 复合、`scale_at`/`rotate_at` 实现「绕指定 origin」；`hide` 则靠内部 ghost 样式 `hidden` 跳过绘制但保留布局空间。
- `PadElem` 用四个真实字段 `left`/`top`/`right`/`bottom` + `#[parse]` 回退链实现与 `margin` 同构的优先级（`rest`/`find → x/y → 单边`），`x`/`y`/`rest` 是 `#[external]` 仅文档字段；`pad` 改包围盒（与 `move` 不改形成对照），且不折叠。
- 全讲再次印证 u6-l3/u5-l4 的主轴：**本 crate 只定义元素与归一化数据，真正产 `Frame`、应用变换、排布浮动的算法住在 `typst-layout`，经 `Routines` 回调**。

## 7. 下一步学习建议

- **进入文本系统（u7）**：本讲的变换/定位常作用于文本，下一步应学 `TextElem` 的海量字段与字体变体，理解 `scale`/`rotate` 作用在文字上时字号、基线如何参与。
- **深入文档模型（u8）**：`PlaceElem` 的浮动是 `figure` 的底层（`figure` 的 `placement` 直接映射到 `place` 的 `float`/`scope`）；学 `FigureElem`/`OutlineElem` 时可回看本讲的 `float`/`flush`。
- **回到内省（u9）**：`PlaceElem` 带 `Locatable`/`Tagged`、`measure` 用 `LocatorLink::measure`，都依赖位置分配；学 `Locator`/`SplitLocator` 与收敛循环后，会更理解「place 的位置」是如何在内省迭代中被确定的。
- **源码延伸阅读**：想看「变换后包围盒如何计算」「浮动如何排队」的真实算法，可去 `crates/typst-layout/` 找对应的 flow/inline 布局器与 `Transform` 的应用点；想看更多纸张尺寸，回看 [`page.rs` 的 `papers!` 宏](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L886-L1032)。
