# 数学公式布局 MathContext 与 Fragment

## 1. 本讲目标

本讲进入 typst-layout 的「专家层」第三站：数学公式排版。读完本讲，你应当能够：

- 说清楚行内方程（inline）与块级方程（block）两条排版路径的差异，以及它们各自返回什么。
- 看懂 `MathContext` 这个排版上下文如何用一个「字体栈 + 片段累加器」驱动整个递归排版。
- 理解 `layout_realized` 如何按 `MathKind` 把一棵公式树分派到各专用 layouter。
- 彻底搞懂 `layout_into_fragment` 在「多个片段合成一个 `FrameFragment`」时 `text_like` 是怎么判定的（本讲实践重点）。
- 理解为何子项使用不同字体时必须临时 push 一个新字体到栈顶。
- 掌握 `MathFragment` 的四变体统一度量，以及 `italics_correction`、`accent_attach`、`extended_shape` 等 OpenType MATH 表常量的来源与作用。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。

**数学排版与普通文本排版的根本区别。** 普通段落（u5 单元）把字符排成一条条等高的水平线；数学公式则是「二维结构」：分数有分子分母、根号有被开方数、上下标挂在基元四周、大算子（∑∫）要居中在一条横轴上。因此 Typst 把公式当作一棵**递归树**来排版：每个节点（分数、根号、上下标……）先把自己的子节点排成一批 `MathFragment`，再把它们合成一个更大的片段，自底向上拼成最终 Frame。

**OpenType MATH 表。** 现代数学字体（如 Typst 默认的 New Computer Modern Math）内嵌一张 `MATH` 表，提供大量排版常量：横轴高度 `axis_height`、上下标缩放比例 `script_percent_scale_down`、每个字形的「斜体修正 `italics_correction`」（让上标不和主体打架）、「重音附着点 `top_accent_attachment`」（帽子该戴在哪里）、是否「扩展字形 `extended_shape`」等。本讲大量代码就是在读这张表。

**MathClass（数学类）。** Unicode 给每个数学字符分了类（Normal 正常、Binary 二元运算、Relation 关系、Opening 左括号、Closing 右括号、Large 大算子、Space 空格、Special 特殊）。公式里的间距规则完全由相邻字符的 `MathClass` 决定——这正是 u5 段落里没有的机制。

**baseline 与 axis。** 文字行有一条基线（baseline）；数学公式还有一条「横轴（axis）」，大算子、根号、分数线都居中在轴上（比基线略高一点，差距即 `axis_height`）。

**Frame / Fragment / Regions / comemo。** 这些已在 u2-l2、u2-l3、u2-l1 讲透，本讲直接复用：排版产出是一棵 `Frame` 树；`Region(s)` 是可用画布；公开函数普遍走「拆 Engine → 调用 `#[comemo::memoize]` 的 `_impl`」模式。本讲主角是 `Frame`/`Region` 的**消费侧**——怎么把公式树装配成 Frame。

> 一个关键事实：本讲的 `math/mod.rs` **不负责构造公式树**。公式树（`MathItem` IR）由 typst-library 的 `resolve_equation`（排版前的「现实化」一步）产出；typst-layout 只负责把这棵树「排成几何」。这与 u1-l4 讲的「realize 在排版前翻译」是一致的分工。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [src/math/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs) | 数学排版主模块：两个对外入口（行内/块级）、`MathContext`、`layout_realized` 分派表、字体栈与字体选取。 |
| [src/math/fragment/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fragment/mod.rs) | `MathFragment` 四变体枚举与统一度量方法；`FrameFragment` 结构与构造。 |
| [src/math/fragment/glyph.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fragment/glyph.rs) | `GlyphFragment`：单个字形片段，读取 MATH 表常量、拉伸（stretch）装配。 |
| [src/math/run.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/run.rs) | `MathRun = Vec<MathFragment>` 别名；`into_frame` / `into_par_items`（行内断片）与多行 `MathRunFrameBuilder`。 |
| [src/math/text.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/text.rs) | `layout_text` / `layout_number` / `layout_glyph`——三种「叶子」内容的排版，是 `text_like` 的主要来源。 |
| [src/rules.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs) | `EQUATION_RULE`：把 `EquationElem` 挂到行内或块级排版入口（见 u7-l1）。 |

## 4. 核心概念与源码讲解

### 4.1 数学排版的两条入口：行内与块级

#### 4.1.1 概念说明

Typst 的 `$...$` 是行内方程、`$ ... $`（前后换行）是块级方程。这两种方程处在不同的排版容器里：行内方程是段落的一个「行内项」（和文字、`#box` 同级），块级方程是流（flow）里的一个「块」。因此 typst-layout 给出**两个独立的排版入口**：

- `layout_equation_inline`：在段落里排版，返回 `Vec<InlineItem>`，吃一个 `Size`（单区域）。
- `layout_equation_block`：在 flow 里排版，返回 `Fragment`（一序列 Frame），吃 `Regions`（多区域）。

它们由 `EQUATION_RULE` 在构建 Library 时注册（详见 u7-l1）：块级挂到 `BlockElem::multi_layouter`，行内挂到 `InlineElem::layouter`。

[文件路径:L805-L812](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L805-L812) —— 根据 `elem.block` 把方程分派到两条入口。

#### 4.1.2 核心流程

两条入口的前半段几乎一样，可总结为一个共同骨架：

```
取字体 → 警告非 MATH 字体 → 追加 script_scale 样式 →
resolve_equation(把公式树现实化) → 建 MathContext → 排版 → 后处理
```

差异集中在「画布形态、返回类型、断行/编号」上：

| 维度 | 行内 `layout_equation_inline` | 块级 `layout_equation_block` |
|------|-------------------------------|------------------------------|
| 断言 | `!elem.block` | `elem.block` |
| 画布 | 单个 `Size`（行内一行宽×高） | `Regions`（多页/多区域） |
| 返回 | `Vec<InlineItem>` | `Fragment`（`Vec<Frame>`） |
| 内部断片 | `into_par_items()`：在二元/关系符处切开，让段落能在此换行 | 单个 Frame（不可在算式内部断行） |
| 跨区域断行 | 无（断行交给外层段落） | `breakable` 时按行把算式拆到多个区域 |
| 公式编号 | 无 | 有 `numbering` 时附加编号 |
| locator | `locator` | `locator.relayout()`（复用身份） |

#### 4.1.3 源码精读

先看行内入口的骨架与关键后处理：

[文件路径:L49-L102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs#L49-L102) —— `layout_equation_inline`。要点：
- L64-65：`style_for_script_scale(&font)` 根据当前字体的 MATH 常量生成「上下标缩放」样式并链接到样式链——这就是为什么不同字体的上下标缩放不同。
- L70：`MathContext::new(engine, region, font)`，画布是单个 `region: Size`。
- L71-75：若公式不是多行（`!item.is_multiline()`），用 `layout_into_fragments` 排成片段再 `into_par_items()` 切成可在段落中换行的 `InlineItem`；若是多行，则整体合成一个 Frame。
- L83-99：对每个返回的 frame，按字体的 `top_edge`/`bottom_edge` 重新设定 ascent/descent（带一点 `slack`），让公式在文字行里「坐」在正确的基线上。

块级入口的后处理更复杂（多行排版、跨区域断行、编号），其前半段骨架同上：

[文件路径:L104-L135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs#L104-L135) —— `layout_equation_block` 顶部。L125 用 `regions.base()`（整区域尺寸，避开被削短的 `size.y`，回顾 u2-l2）建 `MathContext`。L126-135：若是 `MathKind::Multiline` 走 `layout_multiline` 产出 `MathRunFrameBuilder`，否则 `layout_into_fragments(...).into_frame()` 产单帧。

跨区域断行逻辑在 L138-202：当 `BlockElem::breakable` 为真时，把多行算式的每一行按高度逐区域（`regions.next()`）切分，依赖 `regions.may_progress()` / `may_break()` 防止在过小区域上死循环——这与 u4-l3 的 `distribute` 守卫是同一思想。编号附加在 L204-248。

#### 4.1.4 代码实践

**目标**：用一个 `.typ` 文件直观对比两条路径的返回与断行行为。

**步骤**：
1. 写一个最小文档 `eq.typ`：
   ```typ
   这是一段含 $a + b = c$ 的行内方程。

   $ a + b = c $
   $ sum_(n=1)^infinity 1/n^2 = pi^2 / 6 $
   ```
2. 用 typst CLI 编译（待本地验证）：`typst compile eq.typ`。
3. 阅读上面的 `layout_equation_inline` 与 `layout_equation_block`，对照下表填写。

**需观察的现象 / 预期结果**：
- 行内方程 `$a + b = c$` 会作为段落的一项排版；段落若宽度不足，可在 `+`、`=` 处（`MathClass::Binary`/`Relation`）换行——这正是 `into_par_items()` 切片的目的。
- 块级方程独占一行、居中，可跨页断行（L138-202）。
- 把第二行编号打开 `$ ... $ <eq>` 并编译，应看到右侧出现编号（L204-248 的 `add_equation_number`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么行内入口要返回 `Vec<InlineItem>` 而不是单个 Frame？
**答案**：因为行内方程可能需要像普通文字一样在段落中换行；`into_par_items()` 在二元运算符和关系符处把方程切成多个可断片的 `InlineItem`，并插入零宽的换行机会（回顾 run.rs 的 `is_line_break_opportunity`），让段落布局器能在这些位置断行。

**练习 2**：行内入口里 `MathContext::new` 的画布参数是 `region: Size`，块级入口却是 `regions.base()`。为何块级用 `base()` 而非 `regions.size`？
**答案**：`regions.size.y` 在多区域排版中会被逐区域削短，而公式排版需要「整页可用尺寸」作相对基准；`base() = (size.x, full)` 刻意取未被削短的整区域高度（回顾 u2-l2）。

---

### 4.2 MathContext：排版上下文与字体栈

#### 4.2.1 概念说明

整棵公式树的排版都由一个 `MathContext`（数学上下文）对象驱动。它做三件事：
1. 持有 `Engine`（编译上下文，回顾 u2-l1）。
2. 持有一个**字体栈** `Vec<FontInstance>`——栈顶始终是「当前字体」，公式里几乎所有度量（字形、轴高、间距）都从它读取。
3. 持有一个**片段累加器** `fragments: MathRun`（即 `Vec<MathFragment>`），把排版出的片段按顺序攒起来。

为什么是「栈」而不是单一字体？因为公式内部可能局部换字体（例如 `#text("DejaVu Sans")[...]` 嵌进公式，或某个子项设置了不同 math 字体）。换字体时把新字体 push 上去，用完 pop，保证「当前字体」始终与正在排版的内容匹配。

#### 4.2.2 核心流程

`MathContext` 的核心交互如下：

```
new(base, font)        # fonts_stack = [font]，fragments = []
  └─ font()            # 返回 fonts_stack.last()，即栈顶
  └─ push(frag)        # 往 fragments 累加一个片段
  └─ layout_into_self(item):  # 递归排版一棵子树，边排边 push
        for child in item:
            if child 字体 != 外层字体:
                push 新字体 → layout_realized(child) → pop
            else:
                layout_realized(child)
```

字体栈的 push/pop 发生在 `layout_into_self` 里，是本节的关键。

#### 4.2.3 源码精读

先看结构体与构造：

[文件路径:L363-L382](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs#L363-L382) —— `MathContext` 结构与 `new`。注意 `region: Region::new(base, Axes::splat(false))`：数学片段不可「撑满」画布（expand 全 false），尺寸完全由内容决定；`fonts_stack` 初始化为单元素向量（`font()` 用 `last().unwrap()` 取栈顶，注释明确保证栈非空）。

[文件路径:L386-L399](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs#L386-L399) —— `font()` 取栈顶、`push`/`extend` 往 `fragments` 累加。

字体栈 push/pop 的核心：

[文件路径:L438-L463](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs#L438-L463) —— `layout_into_self`。逐子项排版时，记录外层字体 `outer_font`，若子项样式与外层不同**且字体也不同**，则：
- L452-453：`get_font` 取子项字体并 push 到栈顶；
- L454：再 `style_for_script_scale(self.font())` 用**新栈顶字体**重新生成上下标缩放样式并链接；
- L455：用带新缩放样式的链递归排版 `layout_realized`；
- L456：pop，恢复外层字体。
- L457-459：字体相同时直接排版（无 push/pop 开销）。

> 为何字体不同就必须 push？因为排版出的每个 `GlyphFragment` 都要从「当前字体」读取 MATH 常量（轴高、间距、字形 id）。若不换栈顶，子项的字形会按外层字体的度量去解析，导致间距与字形错位。注释 L450-451 也说明这个判断「不精确但够用」——字体变体的小变化通常不影响度量。

支撑这段逻辑的还有两个函数：

[文件路径:L607-L615](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs#L607-L615) —— `style_for_script_scale`：把字体的 `script_percent_scale_down` / `script_script_percent_scale_down` 写进 `EquationElem::script_scale` 样式，使上下标缩放比例随字体走。

[文件路径:L617-L637](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs#L617-L637) —— `get_font`：从样式链取字体族，按 `variant` 选字、`instantiate` 成 `FontInstance`。`family.covers().is_none()` 过滤掉带覆盖约束的族。

[文件路径:L639-L648](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs#L639-L648) —— `warn_non_math_font`：若字体没有 `MATH` 表标志位，发一条警告「当前字体非数学字体，渲染可能不佳」。两个入口都在 L62/L117 调用了它。

#### 4.2.4 代码实践

**目标**：通过源码阅读理解字体栈的生命周期，并用一个嵌套换字体的例子验证 push/pop。

**步骤**：
1. 阅读 `layout_into_self`（L438-463），画出在排版 `$ a + #text(font: "Linux Libertine")[b] + c $` 时的字体栈变化时序：
   - 外层字体（math 字体）入栈 → 排 `a`、`+`（栈不变）→ 遇到 `b` 的 `#text` 子项，字体不同 → push Linux Libertine → 排 `b` → pop → 排 `+`、`c`。
2. （可选，待本地验证）在 `layout_into_self` 的 push 与 pop 处各加一条 `eprintln!("push/pop font: {:?}", self.fonts_stack.len())`，编译上面文档，观察栈深度随公式内容的变化。

**预期结果**：栈深度从 1 开始；遇到换字体子项时临时升到 2 再回落到 1；普通字符不改变栈深。

#### 4.2.5 小练习与答案

**练习 1**：`font()` 为什么用 `last().unwrap()` 取栈顶，而不用 `first()`？
**答案**：栈顶代表「当前正在排版的内容所用的字体」。push 新字体后，子项内容应当用新字体度量，故取 `last()`；`new` 保证栈至少有一个元素，所以 `unwrap` 安全。

**练习 2**：`style_for_script_scale` 在 push 新字体后被**重新**调用一次（L454），用新栈顶字体。若省略这一步会有什么后果？
**答案**：上下标缩放比例仍按外层字体计算，而新字体的 `script_percent_scale_down` 可能不同，导致嵌套在不同字体里的上下标缩放比例错误。

---

### 4.3 layout_realized：按 MathKind 的分派

#### 4.3.1 概念说明

`layout_realized` 是「排版一个公式节点」的核心分派函数。它接收一个 `MathItem`（公式树的一个节点）和当前 `MathContext`，把节点排成若干片段 `push` 进上下文。`MathItem` 是 typst-library 定义的 IR，分两大类：非组件项（`Spacing`/`Space`/`Tag`）和带属性的组件 `Component(MathComponent)`，后者的 `kind` 字段是 `MathKind` 枚举——每种数学结构（分数、根号、上下标……）对应一个变体。

#### 4.3.2 核心流程

`layout_realized` 的处理骨架：

```
match item:
    Spacing | Space | Tag        → 直接 push 对应 MathFragment，return
    Component(comp):
        props = comp.props
        插入左侧间距 lspace（align_form_infix 除外）
        match comp.kind:          ← 分派表
            Glyph     → layout_glyph
            Fraction  → layout_fraction
            Radical   → layout_radical
            Scripts   → layout_scripts
            ...（共 18 个变体）
            Multiline → layout_multiline + push FrameFragment
            Group     → layout_into_fragment（递归）+ 重包装
        插入右侧间距 rspace
```

#### 4.3.3 源码精读

`MathKind` 的全部变体（定义在 typst-library）：

[文件路径:L383-L420](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/ir/item.rs#L383-L420) —— `MathKind` 枚举：Group、Multiline、Radical、Fenced、Fraction、SkewedFraction、Table、Scripts、Accent、Cancel、Line、Primes、Text、Number、Glyph、Box、Mathml、External。其中递归或大变体用 `Box` 包装以控制枚举体积。

分派函数本体：

[文件路径:L467-L551](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs#L467-L551) —— `layout_realized`。三段：
- L473-487：先处理非组件项——`Spacing` 转 `MathFragment::Space(amount.at(font_size))`、`Space` 转 `Space(font.math().space_width...)`、`Tag` 转 `MathFragment::Tag`。注意「普通空格」的宽度来自当前字体的 MATH 表 `space_width` 常量。
- L493-499：插入左侧间距 `lspace`，但跳过 `align_form_infix` 项（多行排版会自行处理对齐间距）。
- L502-540：`match &comp.kind` 大分派表，每个变体调用对应 layouter（这些 layouter 是后续讲义 u6-l4 的主题）。
- L543-548：插入右侧间距 `rspace`。

两个特殊变体值得注意：
- `Multiline`（L521-529）：调用 `layout_multiline` 产 `MathRunFrameBuilder`，`build_unaligned()` 产 Frame；若 `item.centered` 则按 `axis_height` 重设基线居中；最后包成 `FrameFragment` push。
- `Group`（L530-539）：分组的括号内容。它调用 `ctx.layout_into_fragment(item, styles)`（递归合成，见 4.4），然后**保留**合成片段的 `italics_correction` 与 `accent_attach`，重新包进一个新 `FrameFragment`——这保证分组不会丢失内部大算子的斜体修正等信息。

#### 4.3.4 代码实践

**目标**：把分派表与「左侧/右侧间距」机制对应到具体公式。

**步骤**：
1. 阅读 `layout_realized` 的分派表（L502-540），为 `$ a + b $` 标注每个字符走哪个分支：`a`→`layout_glyph`、`+`→`layout_glyph`（它的 `MathClass::Binary` 决定间距）、`b`→`layout_glyph`；相邻项之间的间距来自 `lspace`/`rspace`。
2. 再看 `$ sqrt(x + 1) $`：`sqrt` 走 `Radical` 分支，其内部 `x + 1` 是 `Group`，会触发 4.4 的递归合成。

**需观察的现象 / 预期结果**：分派是「一个变体一个 layouter 函数」的扁平结构，没有复杂的类型分发；间距统一由 `props.lspace`/`rspace` 在分派前后插入，layouter 本身不操心间距。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Group` 分支在重新包装时要显式带上 `italics_correction` 和 `accent_attach`？
**答案**：`layout_into_fragment` 会把多个片段合成一个新 `FrameFragment`，而新 `FrameFragment::new` 默认 `italics_correction = 0`、`accent_attach = width/2`。若分组内含大算子（如积分号），它的斜体修正在合成时会丢失，导致后续上标位置错误；故显式从合成片段取回这两项再装入新包装。

**练习 2**：分派表里 `Mathml` 分支（L504）做了什么？
**答案**：调用 `layout_mathml`，它仅发出一条警告「MathML 元素在分页导出时被忽略」（L554-565），不做任何排版——这是 HTML 导出与 PDF 导出的能力差异在分页侧的体现。

---

### 4.4 layout_into_fragment：递归合成与 text_like 判定（实践重点）

#### 4.4.1 概念说明

很多数学结构（括号分组 `(...)`、定界 `fenced`、矩阵单元）内部是一串片段。排版这类结构时，需要把这串片段「打包」成单个 `FrameFragment` 以便整体定位。这件事由 `layout_into_fragment` 完成，它做两件事：
1. 递归排版子树，把产出的片段收集起来（`layout_into_fragments`）。
2. 若片段恰好一个，原样返回；若多个，则合成一个 `FrameFragment`，并在此处判定 **`text_like`**。

`text_like`（文本相似性）描述一个片段是否「表现得像普通文字」：它影响公式内的间距规则与行内排版的断行。一个关键设计：**合成片段的 `text_like` 由其所有子片段投票决定**。

#### 4.4.2 核心流程

```
layout_into_fragment(item):
    fragments = layout_into_fragments(item)   # 递归排版，drain 出 MathRun
    if fragments.len() == 1:
        return fragments[0]                    # 单片段，原样返回
    # 多片段：合成
    text_like = fragments
        .filter(|f| f.math_size().is_some())   # 忽略 Space/Tag（无 math_size）
        .all(|f| f.is_text_like())             # 全部 text_like 才为真
    return FrameFragment::new(..., fragments.into_frame())
           .with_text_like(text_like)
```

`is_text_like` 的判定（定义在 4.5）：
- `Glyph`：`!extended_shape`（非扩展字形才算文本相似）。
- `Frame`：取其存储的 `text_like` 字段。
- `Space`/`Tag`：固定 `false`（但因无 `math_size` 会被过滤掉，不参与投票）。

#### 4.4.3 源码精读

收集片段的辅助方法：

[文件路径:L402-L410](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs#L402-L410) —— `layout_into_fragments`：记录 `self.fragments` 的起始下标 `start`，调用 `layout_into_self`（4.2 的递归排版）往累加器里 push 片段，最后 `drain(start..)` 取出本轮新增的片段。这种「在共享累加器上记标尺再 drain」的写法避免了反复分配。

合成与 `text_like` 判定的本体：

[文件路径:L413-L436](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs#L413-L436) —— `layout_into_fragment`。核心是 L425-428 的注释与逻辑：
> 「没有 `math_size` 的片段被忽略：尺寸的概念对它们不适用，因此它们的文本相似性无意义。」

即 `Space`、`Tag` 这两类（`math_size()` 返回 `None`，见 4.5）被 `filter` 掉，不参与 `text_like` 投票。剩下有 `math_size` 的片段（`Glyph`、`Frame`）必须**全部** `is_text_like()`，合成结果才 `text_like = true`。最后 `fragments.into_frame()` 把片段拼成 Frame（run.rs 的 `row_into_line_frame`），包进 `FrameFragment`。

哪些片段天生 `text_like`？看 text.rs 里的显式设置：

[文件路径:L15-L42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/text.rs#L15-L42) —— `layout_text`：把数学模式里的纯文本排成段落并包成 `FrameFragment`，**显式** `.with_text_like(true)`（L40）。

[文件路径:L44-L65](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/text.rs#L44-L65) —— `layout_number`：数字同样 `.with_text_like(true)`（L63）。

对照之下，`layout_fraction`、`layout_radical`、`layout_scripts` 等结构构造 `FrameFragment` 时**不**调用 `with_text_like`，于是保持默认 `false`——分数、根号不是「文本相似」的。

#### 4.4.4 代码实践

**目标**（本讲主实践）：用两个对照例子说清 `text_like` 的判定与字体栈 push 的必要性。

**步骤**：
1. **`text_like` 判定追踪**。考虑两个分组的公式：
   - 例 A：`$ (a + b) $`。`(...)` 是 `Group`，内部 `a + b` 排成三个 `Glyph` 片段（`a`、`+`、`b`）。进入 `layout_into_fragment`：
     - `fragments.len() == 3 ≠ 1`，走合成分支。
     - 三个 `Glyph` 的 `math_size()` 都是 `Some`，且都不是 `extended_shape`，故 `is_text_like()` 全为 `true`。
     - `text_like = true`，合成 FrameFragment 标记为文本相似。
   - 例 B：`$ (x/y) $`。`(...)` 是 `Group`，内部 `x/y` 是 `Fraction`，由 `layout_fraction` 排成**单个** `FrameFragment`（`text_like = false`，未显式设置）。进入 `layout_into_fragment`：
     - 此时 `fragments.len() == 1`（分数本身是一个 Frame 片段），**直接返回**，`text_like` 维持分数自身的 `false`。
     - 若分组里是「分数 + 文字」如 `$ (x/y + 1) $`：片段为 `[分数帧(false), Glyph(+), Glyph(1)]`，len=3，合成；其中分数帧 `text_like=false` ⇒ `all` 为 `false` ⇒ 合成结果 `text_like = false`。
2. **字体 push 必要性追踪**。考虑 `$ a + #text(font: "Linux Libertine")[b] + c $`：
   - 外层用默认 math 字体排 `a`、`+`。
   - 遇到 `b` 的 `#text` 子项，`layout_into_self`（L451）发现字体不同 → push Linux Libertine → 用**该字体**的度量排 `b`（字形 id、字宽都来自新字体）→ pop。
   - 若不 push，`b` 会按外层 math 字体去查字形 id，可能查到 tofu（缺字）或错误字形。

**需观察的现象 / 预期结果**：
- 能口述：`text_like` 是「所有有尺寸子片段的与（AND）」，且 `Space`/`Tag` 不参与。
- 能口述：换字体子项必须临时换栈顶，否则度量来源错误。

**若想实跑验证**（待本地验证）：在 `layout_into_fragment` 的 L425 前加 `eprintln!("combine: len={}, text_like candidates count={}", fragments.len(), fragments.iter().filter(|f| f.math_size().is_some()).count());`，编译例 A/B，观察日志中 len 与候选数是否与分析一致。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `text_like` 判定要先 `filter(|f| f.math_size().is_some())` 再 `all`？若不过滤会怎样？
**答案**：`Space` 和 `Tag` 没有 `math_size`（`math_size()` 返回 `None`），且其 `is_text_like()` 恒为 `false`。若不过滤，只要分组里含一个空格或标签，`all` 就会变 `false`，使本来纯文本相似的分组被误判为非文本相似，破坏间距规则。过滤掉它们后，投票只由「真正有尺寸语义」的片段参与。

**练习 2**：`$ (a + b) $` 与 `$ a + b $`（无括号）在排版路径上有何不同？
**答案**：无括号时 `a + b` 直接作为顶层片段流被 `layout_into_fragments` 收集，不进 `layout_into_fragment` 的合成分支；有括号时多了一层 `Group`，`Group` 分支（L530-539）调用 `layout_into_fragment` 把内部片段合成单个 `FrameFragment` 并保留 `italics_correction`/`accent_attach`，使得整个括号分组成为一个可整体定位的单元。

---

### 4.5 MathFragment 与 FrameFragment：统一度量与 MATH 表常量

#### 4.5.1 概念说明

公式排版的产物是一串 `MathFragment`。它是一个四变体枚举：`Glyph`（单字形）、`Frame`（已排好的子结构）、`Space`（空格/间距）、`Tag`（内省标签）。每个变体内部结构差异很大（`Glyph` 携带 `TextItem`，`Frame` 携带整帧），但上层 layouter（分数、上下标……）需要**统一地**询问任意片段的「宽度、高度、上升下降量、数学类、斜体修正」等度量。因此 `MathFragment` 提供一组统一方法，内部按变体分派。

`FrameFragment` 是 `MathFragment::Frame` 变体的载荷：它把一个 `Frame` 连同一组「数学度量」打包——这些度量（`italics_correction`、`accent_attach`、`base_ascent`、`text_like` 等）是普通 `Frame` 没有的，却是公式排版定位所必需的。

#### 4.5.2 核心流程

```
MathFragment (enum)
├─ Glyph(GlyphFragment)   ← 单字形，度量来自 MATH 表
├─ Frame(FrameFragment)   ← 子结构，度量来自其字段
├─ Space(Abs)             ← 只有宽度
└─ Tag(Tag)               ← 零尺寸（不占空间）

统一方法（按变体分派）：size/width/height/ascent/descent/
    class/math_size/is_text_like/italics_correction/accent_attach/...

FrameFragment 字段：frame, font_size, class, math_size,
    base_ascent, base_descent, italics_correction,
    accent_attach(top, bottom), text_like
```

#### 4.5.3 源码精读

四变体枚举与统一度量：

[文件路径:L22-L28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fragment/mod.rs#L22-L28) —— `MathFragment` 枚举定义。

[文件路径:L103-L109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fragment/mod.rs#L103-L109) —— `math_size()`：`Glyph`/`Frame` 返回 `Some`，`Space`/`Tag` 返回 `None`。这正是 4.4 里 `text_like` 过滤的依据。

[文件路径:L137-L143](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fragment/mod.rs#L137-L143) —— `is_text_like()`：`Glyph` 看 `!extended_shape`、`Frame` 看 `text_like` 字段、其余 `false`。

[文件路径:L145-L159](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fragment/mod.rs#L145-L159) —— `italics_correction()` 与 `accent_attach()`：按变体取字段，缺省时 `accent_attach` 回退到 `width/2`。

[文件路径:L161-L172](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fragment/mod.rs#L161-L172) —— `into_frame()`：把任意片段转成 `Frame` 供组装。`Tag` 包成零尺寸软帧并 push 进去；`Space` 包成 `Frame::soft(size)`。

`FrameFragment` 结构与构造：

[文件路径:L226-L256](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fragment/mod.rs#L226-L256) —— `FrameFragment` 字段与 `new`。关键点：
- L241-242：`base_ascent`/`base_descent` 在 `modified()` **之前**从原始 frame 取值——这两个量记录「应用修饰符（hide/link 等）之前的度量」，因为修饰符可能改变帧内容（回顾 u2-l3 的 `FrameModifiers`），但上下标定位仍需原始度量。
- L245：`frame: frame.modified(&modifiers)` 应用 `FrameModifiers`。
- L247：`font_size` 取 `TextElem::size`，作为后续 em→abs 换算基准。
- L254：`text_like` 默认 `false`，需用 `with_text_like` 显式打开（见 text.rs）。

`GlyphFragment` 与 MATH 表常量读取（这是 italics correction 等度量的真正来源）：

[文件路径:L27-L44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fragment/glyph.rs#L27-L44) —— `GlyphFragment` 字段：`item: TextItem`（文本侧）、`size`/`baseline`（几何）、`italics_correction`/`accent_attach`/`math_size`/`class`/`extended_shape`/`stretchable_axes`（数学侧）、`modifiers`/`shift`/`align`（帧侧）。

[文件路径:L207-L238](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fragment/glyph.rs#L207-L238) —— `update_glyph`：从 OpenType MATH 表读取常量并填充字段。这是度量的「源头」：
- `is_extended_shape`（L210，查 `glyph_info.extended_shapes`）决定 `extended_shape`，进而决定 `is_text_like`；
- `italics_correction`（L211，查 `glyph_info.italic_corrections`）；L213-215：非扩展字形时把斜体修正加进 `x_advance`（让后续间距含修正）；
- `ascent`/`descent`（L218-219，来自字形包围盒）；
- `accent_attach`（L225-228）：优先用 MATH 表的 `top_accent_attachments`，缺省时上侧回退 `(width + italics)/2`、下侧 `(width - italics)/2`。

[文件路径:L470-L500](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fragment/glyph.rs#L470-L500) —— 三个查表辅助：`italics_correction`、`accent_attach`、`is_extended_shape`，全部走 `font.ttf().tables().math?.glyph_info?` 这条路径，缺表则返回 `None`。

`kern_at_height`（用于上下标在特定高度处的字距）：

[文件路径:L183-L211](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/fragment/mod.rs#L183-L211) —— `kern_at_height`：对 `Glyph` 变体查 MATH 表的 `kern_infos`，按角落（四角）与高度返回字距；对装配字形（assembly）按垂直/水平选取首尾部件。这是上下标紧贴大算子时不重叠的关键。

最后是片段流的两种归宿（run.rs）：

[文件路径:L165-L168](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/run.rs#L165-L168) —— `into_frame`：把一串片段排成单行 Frame（无对齐点）。

[文件路径:L173-L252](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/run.rs#L173-L252) —— `into_par_items`：把片段串在二元/关系符处切成多个 `InlineItem`，供段落换行（行内方程专用）。

#### 4.5.4 代码实践

**目标**：把「统一度量」与「MATH 表常量」对应起来，理解 italics correction 的作用。

**步骤**：
1. 阅读 `update_glyph`（glyph.rs L207-238），对照下表填写每个字段的「MATH 表来源」：

   | 字段 | MATH 表来源 | 缺省回退 |
   |------|------------|----------|
   | `extended_shape` | `glyph_info.extended_shapes` | `false` |
   | `italics_correction` | `glyph_info.italic_corrections` | `0` |
   | `accent_attach`(top) | `glyph_info.top_accent_attachments` | `(width + italics)/2` |
   | `ascent`/`descent` | 字形包围盒 `glyph_bounding_box` | `0` |

2. 用 `$ integral_a^b f(x) thin d x $` 观察 `italics_correction` 的效果（待本地验证）：积分号 ∫ 有正的斜体修正，使上标 `b` 不与积分号顶部重叠；若把字体换成无 MATH 表的字体（会触发 L640 的警告），斜体修正归零，上标会贴到积分号上。

**预期结果**：能说清 `extended_shape` 如何同时影响 `is_text_like`（4.4）与拉伸装配（`stretch`，glyph.rs L708 把 `extended_shape = true`）；能说清 `base_ascent` 为何要在 `modified()` 之前取（4.5.3 FrameFragment::new）。

#### 4.5.5 小练习与答案

**练习 1**：`FrameFragment::new` 里 `base_ascent`/`base_descent` 在 `frame.modified(&modifiers)` **之前**取，而 `frame` 字段存的是修饰后的帧。为什么？
**答案**：修饰符（`FrameModifiers`，如 hide/link）可能改变帧的实际内容与尺寸，但上下标、重音等定位需要用「修饰前的原始度量」作为基准。故先把原始 ascent/descent 存进 `base_ascent`/`base_descent`，再对帧施加修饰。

**练习 2**：一个拉伸过的大括号 `{$ left curly bracket right none}`（被拉到很高）的 `GlyphFragment`，其 `extended_shape` 与 `is_text_like` 分别是什么？
**答案**：拉伸装配（`assemble`，glyph.rs L708）会把 `extended_shape` 置为 `true`；于是 `is_text_like()` 返回 `!extended_shape = false`——拉伸后的定界符不再是「文本相似」的，间距规则会把它当作大结构而非普通字符处理。

---

## 5. 综合实践

把本讲四条主线（两条入口 / 字体栈 / 分派与合成 / 度量）串起来，完成一次「源码追踪」任务。

**任务**：追踪块级方程 `$ (sum_(n=1)^infinity 1/n^2) = pi^2/6 $ <eq>` 从入口到产帧的完整路径，回答下列问题。

**操作步骤**：
1. **入口**：由于是块级（`$ ... $` 换行），`EQUATION_RULE`（rules.rs L805-812）分派到 `layout_equation_block`。确认它用 `regions.base()` 建 `MathContext`（mod.rs L125）。
2. **顶层分派**：整个方程是一棵树。最外层 `(...)` 是 `Group`。追踪 `layout_realized` 的 `Group` 分支（L530-539）：它调 `layout_into_fragment`，内部递归 `layout_into_self`。
3. **大算子 ∑**：`sum_(n=1)^infinity` 是 `Scripts`，其基元 `sum` 走 `layout_glyph`（text.rs L69-125）。`sum` 的 `MathClass::Large`，故 `glyph.center_on_axis()`（L115-119）把它居中到横轴。注意 `∑` 可能被拉伸（`extended_shape`），这影响 `is_text_like`。
4. **分数**：`1/n^2` 与 `pi^2/6` 走 `layout_fraction`（u6-l4），各产一个 `FrameFragment`（`text_like = false`）。
5. **合成与 text_like**：`Group` 内片段含分数帧（`text_like=false`）与大算子，故 `layout_into_fragment`（L425-428）合成结果 `text_like = false`。
6. **编号**：`<eq>` 触发 L204-248，用 `Counter::of(EquationElem::ELEM).display_at(...)` 排出编号帧，再 `add_equation_number`（L251-315）把它贴到方程右侧。

**需观察的现象 / 预期结果**：
- 产出 `Fragment` 含一帧（未跨页时），帧内含 ∑（居中于轴）、两个分数、编号。
- 能在源码中指认每一步对应的函数与行号。
- 若把外层字体换成含不同 `script_percent_scale_down` 的字体，`style_for_script_scale`（L607-615）会使上下标缩放比例随之改变。

**若想验证 text_like 影响**（待本地验证）：对比 `$ a + b $` 与 `$ a + 1/2 $` 在窄行内排版时的间距，体会分数（非 text_like）与普通字符（text_like）在间距规则上的差异。

## 6. 本讲小结

- typst-layout 给数学公式提供两条入口：行内 `layout_equation_inline`（返回 `Vec<InlineItem>`，吃单 `Size`，用 `into_par_items` 在二元/关系符处切出换行机会）与块级 `layout_equation_block`（返回 `Fragment`，吃 `Regions`，支持跨区域断行与编号），由 `EQUATION_RULE` 按 `block` 字段分派。
- `MathContext` 以「字体栈 + 片段累加器」驱动整棵公式树排版；栈顶 `font()` 始终是当前字体，子项换字体时临时 push、用完 pop，确保度量来源正确。
- `layout_realized` 是扁平分派表：非组件项（Spacing/Space/Tag）直接转片段；组件项按 `MathKind` 的 18 个变体调对应 layouter，并在前后统一插入 `lspace`/`rspace`。
- `layout_into_fragment` 把多个片段合成一个 `FrameFragment`；`text_like` 由「所有有 `math_size` 的子片段的与」决定，`Space`/`Tag` 因无 `math_size` 不参与投票。
- `MathFragment` 四变体（Glyph/Frame/Space/Tag）提供统一度量；`GlyphFragment` 从 OpenType MATH 表读 `italics_correction`、`accent_attach`、`extended_shape`、`kern_at_height` 等常量；`FrameFragment` 额外保存 `base_ascent`/`text_like` 等数学度量。

## 7. 下一步学习建议

本讲只打开了数学排版的「调度层」与「片段/度量层」，各专用 layouter（分数、根号、上下标、重音、定界、矩阵）的实现尚未展开。建议：

- 继续阅读 **u6-l4 数学元素：分数、根号、上下标、重音等**，逐一看 `fraction.rs`、`radical.rs`、`scripts.rs`、`accent.rs`、`fenced.rs` 如何读取 MATH 表常量并把子片段定位成具体结构。
- 对照阅读 `glyph.rs` 的 `stretch` / `assemble`（L268-709），理解定界符与大算子如何通过 `GlyphAssembly` 从部件拼装到目标尺寸——这是 `extended_shape` 与 `is_text_like` 的另一面。
- 回顾 **u5-l4 文本整形**：数学字形整形（`shaping.rs`）复用了行内整形的 `SharedShapingContext` / `create_shape_plan`，区别在于 script 固定为 `math`、逐字（grapheme cluster）整形，理解二者的异同。
