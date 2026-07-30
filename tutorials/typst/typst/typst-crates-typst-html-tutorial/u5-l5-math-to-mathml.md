# 数学公式到 MathML 的转换

## 1. 本讲目标

本讲承接 u3-l5（内建 show 规则注册机制），把视线聚焦在 `EQUATION_RULE` 这一条规则背后发生的事情：当用户写下一个 Typst 公式 `$ a/b $` 后，typst-html 是怎样把它变成浏览器能直接渲染的 [MathML Core](https://www.w3.org/TR/mathml-core/) 元素的。

读完本讲，你应当能够：

1. 说清 `convert_math_to_nodes` 这个「公式翻译器」的整体结构与它依赖的 `MathContext` 状态机。
2. 追踪一个分数 / 根号 / 上下标 / 表格公式，从 `EquationElem` 一路到 `<mfrac>` / `<mroot>` / `<msubsup>` / `<mtable>` 的转换路径。
3. 理解 `MathItem` IR 各叶子节点（glyph、text、number、prime）如何映射成 MathML token 元素，以及 `mo`（算符）属性如何生成。
4. 解释 `EQUATION_CSS_STYLES` 为什么存在、它注入到哪里、它覆盖了 MathML Core 用户代理样式表（UA stylesheet）的哪些行为，从而让浏览器渲染逼近 Typst 的分页导出。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，Typst 的公式有两套「后端」。** 一套是分页导出（PDF / PNG 等），把公式排版成一帧 `Frame`；另一套就是本讲的 HTML 导出，把公式翻译成 MathML。两条路径在「排版前的中间表示」处分叉——它们共享同一个公式 IR（intermediate representation），即 `typst_library::math::ir` 中的 `MathItem`。换句话说，typst-html 并不从零解析公式语法，而是接手一个已经分析好的 `MathItem` 树。

**第二，MathML Core 是一套专门的 XML/HTML 元素族。** 和普通 HTML 不同，它有一组语义化标签：`<mfrac>`（分数）、`<msqrt>`（平方根）、`<mroot>`（n 次根）、`<msub>` / `<msup>` / `<msubsup>`（下/上标）、`<munder>` / `<mover>` / `<munderover>`（上下限）、`<mtable>`（矩阵 / 多行）、`<mi>`（标识符）、`<mo>`（算符）、`<mn>`（数字）、`<mrow>`（水平编组）、`<mtext>`（普通文本）、`<mspace>`（空白）等。浏览器（尤其 Chromium、Firefox、Safari 新版）内置了 MathML Core 的渲染引擎与一套「用户代理样式表」（User Agent stylesheet，下称 UA stylesheet），负责字号缩放（`math-depth` / `math-style`）、斜体自动变换、算符间距等。

**第三，所谓「逼近分页导出」是一场 CSS 覆盖战。** 浏览器的 UA stylesheet 与 Typst 分页后端的排版规则并不完全一致（例如分数左右留白、多行公式行间距、表格对齐方式）。typst-html 通过一份注入到 `<head>` 的 `<style>`，精准地覆盖那些不一致的规则。本讲的核心谜题就是：覆盖了什么、为什么覆盖。

> 名词速查：**IR（intermediate representation，中间表示）**——源代码与最终产物之间的一棵类型化数据树，本讲的 IR 是 `MathItem`。**UA stylesheet**——浏览器自带的默认样式表，类似于 HTML 里 `<h1>` 默认加粗那种「出厂设置」。**OpenType 特性（feature）**——字体里内嵌的排版开关，本讲会用到 `dtls`（dotless，让带重音字母在算符上方退化为无点形式）。

## 3. 本讲源码地图

本讲涉及的关键文件如下。主战场是 `src/mathml.rs`，规则触发点在 `src/rules.rs`，CSS 注入点在 `src/document.rs`，IR 定义在 `typst-library`。

| 文件 | 作用 |
| --- | --- |
| `src/rules.rs` | 注册 `EQUATION_RULE`：把 `EquationElem` 具象化为 `MathItem`，再调 `convert_math_to_nodes`，最后包成 `<math>`。 |
| `src/mathml.rs` | 公式翻译器的全部实现：`convert_math_to_nodes` 入口、`MathContext` 状态机、按 `MathKind` 分派的 `handle_*` 函数族、`mo` 属性生成器 `make_mo`，以及 `EQUATION_CSS_STYLES`。 |
| `src/document.rs` | 编译主链路末尾：若文档含公式，把 `EQUATION_CSS_STYLES` 注入 `<head>` 的 `<style>`。 |
| `typst-library/.../math/ir/item.rs` | `MathItem` / `MathKind` / `MathProperties` / `FractionItem` / `RadicalItem` 等结构定义（被翻译的「输入」）。 |

> 本讲引用 `typst-library` 的行号时，永久链接指向 `crates/typst-library/` 下对应路径。由于 `typst-library` 可能独立更新，如发现行号漂移请以函数/类型名为准。

## 4. 核心概念与源码讲解

### 4.1 convert_math_to_nodes：转换总入口与 MathContext 状态机

#### 4.1.1 概念说明

`convert_math_to_nodes` 是公式翻译的统一入口，但它本身极薄——真正干活的是它构造出来的 `MathContext` 状态机。这个设计的关键在于：MathML 元素是否需要显式写 `math-style`、`math-shift`、`scriptlevel` 这类属性，**取决于它和父元素的「样式上下文」是否不同**。浏览器会按 UA stylesheet 自动套用一套默认值，只有当某个子元素的目标值和默认值（或外层已设定的值）不一致时，才需要写属性。否则输出会充斥着冗余的 `scriptlevel="1"`。

因此 `MathContext` 必须一路追踪「当前生效的 CSS 上下文」，这正是 `CssContext` 字段存在的理由。

#### 4.1.2 核心流程

翻译一个 `MathItem` 的整体流程：

1. `convert_math_to_nodes` 用 `(engine, styles, block)` 构造一个 `MathContext`。
2. 调 `ctx.handle_into_nodes(&item)`，把根 `MathItem` 翻成一个 `Vec<Content>`（每个 `Content` 是一个 `HtmlElem`，即一个 MathML 元素）。
3. 内部递归：`handle_into_nodes` → `handle_into_self` → 对每个孩子 `handle_realized`，后者按 `MathKind` 分派到具体的 `handle_fraction` / `handle_radical` / …。

`CssContext` 的三个字段对应 MathML Core 的三条 CSS 属性轴：

- `math_style`（`Normal` = displaystyle，`Compact` = compact/textstyle）：控制是否「满尺寸」渲染。
- `math_shift`（`Normal` / `Compact`）：控制上下标是否倾斜（意大利体偏移）。
- `math_depth`：等价于旧规范的 `scriptlevel`，决定字号缩放层级。

#### 4.1.3 源码精读

入口 [`convert_math_to_nodes`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L197-L205) 只有三行有效逻辑：构造 `MathContext`，再调 `handle_into_nodes`：

```rust
pub(crate) fn convert_math_to_nodes(
    item: MathItem,
    engine: &mut Engine,
    styles: StyleChain,
    block: bool,
) -> SourceResult<Vec<Content>> {
    let mut ctx = MathContext::new(engine, styles, block);
    ctx.handle_into_nodes(&item)
}
```

`MathContext::new` 初始化 [`CssContext::new(block)`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L294-L301)：块级公式从 `math_style: Normal`（满尺寸）起步，行内公式从 `Compact` 起步。这就是行内公式 `$a$` 比 display 公式 `$$ a $$` 更小的源头之一。

`CssContext` 提供一组「派生新上下文」的纯函数（如 `depth_auto_add`、`style_compact`、`shift_compact`、`depth_add`），它们是构造子元素目标上下文的积木。其中 [`from(size, cramped)`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L326-L350) 把 Typst 自己的 `MathSize`（Display/Text/Script/ScriptScript）与 cramped 标志翻译成 MathML 的三轴上下文，是两套术语之间的「翻译表」。

`MathContext` 用 [`with_css`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L394-L399) 做临时的上下文替换（保存旧值 → 跑闭包 → 还原），让递归进入子公式时改写 CSS 上下文、退出时自动恢复，这是整棵公式树「上下文栈」得以正确维护的关键。

#### 4.1.4 代码实践

阅读型实践，目标是看清「上下文追踪如何省掉冗余属性」。

1. 打开 [`handle_realized`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L491-L650)。
2. 定位 [`let target = CssContext::from(props.size, props.cramped);`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L526)。
3. 观察紧随其后的 `scriptlevel` / `displaystyle` 计算都带有 `.then(...)` 条件——只有 `target != ctx.css` 时才生成属性字符串。
4. **预期观察**：源码注释明确写着「We clamp the tracked depth at 2 so we don't emit unnecessary `scriptlevel="2"` everywhere」。这说明上下文追踪不仅是正确性问题，也是输出整洁度问题。

5. 如果无法运行项目，**待本地验证**：可脑内推演——一个 display 公式的最外层 `<mi>a</mi>`，由于 `target` 与 `ctx.css` 完全相等，生成的元素上不会带任何 `math-style`/`scriptlevel` 属性。

#### 4.1.5 小练习与答案

**练习 1**：为什么行内公式和块级公式的初始 `math_style` 不同？这个差异最终体现在输出 HTML 的哪里？

> **参考答案**：行内公式从 `Compact` 起步、块级公式从 `Normal` 起步（`CssContext::new`）。差异最终体现在 `<math>` 元素的 `display` 属性上——见 4.2.3，块级公式带 `display="block"`，这会让浏览器把它当作「displaystyle」满尺寸渲染。

**练习 2**：`MathContext::with_css` 用 `std::mem::replace` 保存旧值、闭包结束后还原。如果不还原会有什么后果？

> **参考答案**：CSS 上下文会「泄漏」给后续兄弟节点。例如处理一个分数的分子时临时进入了更小字号的上下文，若不还原，紧跟在分子之后的分母（以及公式后续部分）会被错误地认为「当前已在更小字号」，导致它们不再写出本该写出的 `scriptlevel` 属性，浏览器渲染出的字号就错了。

### 4.2 EQUATION_RULE：方程元素的触发与 `<math>` 包裹

#### 4.2.1 概念说明

`convert_math_to_nodes` 接受的 `MathItem` 从何而来？答案是 show 规则。在 u3-l5 中我们见过 `register()` 把若干 `XXX_RULE` 注册进 `NativeRuleMap`；数学部分只有一条规则 `EQUATION_RULE`，它绑定到 `EquationElem`（即 Typst 里的 `math.equation`，写作 `$ ... $`）。当具象化引擎遇到一个 `EquationElem` 时，就执行这条规则，完成「公式元素 → MathML DOM」的转化。

#### 4.2.2 核心流程

`EQUATION_RULE` 三步走：

1. **具象化为 IR**：调 `typst_library::math::ir::resolve_equation`，把 `EquationElem` 连同它内部的公式内容分析成一棵 `MathItem`。这一步与分页导出共享，是「两条后端共用一份 IR」的接合点。
2. **翻译为 MathML 节点**：调本讲的 `convert_math_to_nodes(item, engine, styles, block)`，得到一串 `Content`（各 MathML 元素）。
3. **包裹 `<math>`**：把这串内容塞进 `<math>` 元素；若是块级公式，设 `display="block"` 属性，并整体再包一层 `BlockElem`（呼应 u4-l2 的块级提升）。

#### 4.2.3 源码精读

规则注册在 `register()` 的 Math 分组里，仅此一条：

[注册 EQUATION_RULE](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L85-L86)：`rules.register(Html, EQUATION_RULE);`

规则本体 [`EQUATION_RULE`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L822-L841)：

```rust
const EQUATION_RULE: ShowFn<EquationElem> = |elem, engine, styles| {
    let arenas = Arenas::default();
    let item = resolve_equation(
        elem, engine,
        Locator::synthesize(elem.location().unwrap()),
        &arenas, styles,
    )?;

    let block = elem.block.get(styles);
    let body = convert_math_to_nodes(item, engine, styles, block)?;
    let math = HtmlElem::new(tag::mathml::math)
        .with_body(Some(Content::sequence(body)))
        .with_optional_attr(attr::mathml::display, block.then_some("block"))
        .pack()
        .spanned(elem.span());

    Ok(if block { BlockElem::packed(math) } else { math })
};
```

注意几个细节：

- `block` 来自 `elem.block.get(styles)`——这正是区分行内（`$a$`）与块级（独占一行的 `$ a $`）的开关。
- `display` 属性用 `block.then_some("block")`：**只有块级才写属性，行内不写**。MathML Core 规定不带 `display` 的 `<math>` 默认就是 inline 行为。
- 块级时多套一层 `BlockElem::packed(math)`，让公式在 HTML 主转换链里被当作块级内容处理（详见 u3-l3 的 `handle` 分派与 u4-l2 的 display 提升）。
- `tag::mathml::math` 与 `attr::mathml::display` 来自 `tag` / `attr` 模块下的 `mathml` 子表——即 MathML 专用标签与属性常量（参见 u2-l4 的 `tag.rs` 与 u2-l3 的 `attr.rs`）。

#### 4.2.4 代码实践

源码阅读型实践：追踪「块级 vs 行内」的差异点。

1. 阅读 [`EQUATION_RULE`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L822-L841)。
2. 列出 `block == true` 与 `block == false` 两种情况下，输出在 **两个层面** 的不同：(a) `<math>` 元素是否带 `display` 属性；(b) 外层是否多一个块级包裹。
3. **预期结果**：块级公式同时满足「带 `display="block"`」+「外层 `BlockElem`」；行内公式两者皆无。
4. 进一步思考：为什么区分块级/行内要用 HTML 的 `display` **属性**而非 CSS？因为 MathML Core 规范规定 `<math>` 的 `display` 属性才是触发 displaystyle 渲染的正规途径，CSS 无法等价表达（这点 u4-l2 已有铺垫）。

#### 4.2.5 小练习与答案

**练习 1**：`resolve_equation` 的返回值类型是什么？它和分页导出有什么关系？

> **参考答案**：返回 `MathItem`（由它被直接传给 `convert_math_to_nodes(item: MathItem, ...)` 可知）。分页导出（PDF）同样消费 `MathItem`，两条后端在 `resolve_equation` 之后就分叉了——HTML 走 `convert_math_to_nodes`，分页走 Frame 排版。共享 IR 是 typst-html 不必重新实现公式语法分析的原因。

**练习 2**：如果用户写了 `#show math.equation: html.frame`，会发生什么？（提示：回顾 u3-l5 与 u6-l1）

> **参考答案**：`html.frame` 会把内容切回 `Target::Paged` 排版成 Frame 再以 SVG 嵌入。u3-l5 提到 `FrameElem` 在 `Target::Paged` 上注册为 no-op，正是为了防止这类 show 规则导致 Frame 无限嵌套。此时公式不再走 MathML 路径，而是变成一张 SVG 图。

### 4.3 MathItem 处理：结构与叶子到 MathML 元素的映射

#### 4.3.1 概念说明

这是本讲信息量最大的一节。`MathItem` 是一个枚举，本质是「公式语义结构」的树。typst-html 的工作是把树上的每个节点翻译成对应的 MathML 元素。结构节点（分数、根号、上下标、表格、多行）对应 MathML 的容器元素；叶子节点（单个字符、数字、文本、素数符号）对应 token 元素（`<mi>`/`<mo>`/`<mn>`/`<mtext>`）。

理解这节的关键是建立一张「IR 结构 → MathML 元素」的对照表，并明白每种结构在翻译时如何调整子元素的 CSS 上下文（让分子分母自动变小、让根指数更小等）。

#### 4.3.2 核心流程

所有 `MathKind` 的分派都集中在一个 `match` 里，位于 [`handle_realized`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L570-L625) 内：

```
match comp.kind {
    Fraction(item)  => handle_fraction   // <mfrac>
    Radical(item)    => handle_radical    // <msqrt> / <mroot>
    Scripts(item)    => handle_scripts    // <msub>/<msup>/.../<mmultiscripts>
    Accent(item)     => handle_accent     // <mover>/<munder>
    Table(item)      => handle_table      // <mtable>
    Multiline(item)  => handle_multiline  // <mtable class="multiline-equation">
    Fenced(item)     => handle_fenced     // <mrow> + 括号 <mo>
    Glyph(item)      => handle_glyph      // <mi> 或 <mo>
    Text(item)       => handle_text       // <mtext> 或 <mo>(large)
    Number(item)     => handle_number     // <mn>
    Primes(item)     => handle_primes     // <mo>（素数符号）
    Group(_)         => 递归 handle_into_node
    Mathml(item)     => handle_mathml     // 用户手写 math.elem
    ...
}
```

IR → MathML 的核心对照表（重点记忆）：

| IR 结构（`MathKind`） | 关键字段 | MathML 元素 | 子元素上下文调整 |
| --- | --- | --- | --- |
| `Fraction` | numerator / denominator / line | `<mfrac>`（无横线时加 `linethickness="0"`，即二项式系数） | 分子 `depth_auto_add + style_compact`；分母再加 `shift_compact` |
| `Radical` | radicand / index（`None` 即平方根） | 无 index → `<msqrt>`；有 index → `<mroot>` | 被开方数 `shift_compact`；根指数 `depth_add(2) + style_compact + shift_compact` |
| `Scripts` | base + top/bottom + 四角 top_right 等 | 见下方脚本元素选择表 | 上标 `depth_add(1) + style_compact`；下标再加 `shift_compact` |
| `Table` | cells（二维）/ align / alternator | `<mtable>`，每格 `<mtd>`，每行 `<mtr>` | 每个 cell `depth_auto_add + style_compact + shift_compact` |
| `Multiline` | rows（已按行/列切分） | `<mtable class="multiline-equation [aligned]">` | 同行内逐格翻译 |
| `Fenced` | open / body / close | `<mrow>` 包住 open + body + close | open 用 `Form::Prefix`，close 用 `Form::Postfix` |

脚本（Scripts）的元素选择逻辑（见 [`handle_scripts`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L877-L911)）按「有哪些角」挑最贴合的 MathML 元素：

| 有哪些附件 | MathML 元素 |
| --- | --- |
| 仅右下 `br` | `<msub>` |
| 仅右上 `tr` | `<msup>` |
| 右上 + 右下 | `<msubsup>` |
| 任意左侧角 | `<mmultiscripts>`（用 `<mprescripts>` 分隔前后脚本） |
| 顶部 `t` / 底部 `b`（limit） | 再外层包 `<mover>` / `<munder>` / `<munderover>` |

#### 4.3.3 源码精读

**分数** [`handle_fraction`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L690-L709)：

```rust
let num   = ctx.with_css(ctx.css.depth_auto_add().style_compact(), |c| c.handle_into_node(&item.numerator))?;
let denom = ctx.with_css(ctx.css.depth_auto_add().style_compact().shift_compact(), |c| c.handle_into_node(&item.denominator))?;
let line  = (!item.line).then_some("0");
Ok(HtmlElem::new(tag::mfrac)
    .with_body(Some(num + denom))
    .with_optional_attr(attr::linethickness, line)
    .pack())
```

要点：分子在前、分母在后（MathML `<mfrac>` 规定顺序）；`!item.line`（二项式系数，无横线）才写 `linethickness="0"`；注释里的 `// UA stylesheet.` 表示这些上下文调整与浏览器 UA stylesheet 对 `mfrac` 子元素的默认缩放保持一致，避免重复输出属性。

**根号** [`handle_radical`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L711-L735)：

```rust
let radicand = ctx.with_css(ctx.css.shift_compact(), |c| c.handle_into_nodes(&item.radicand))?;
let index = item.index.as_ref().map(|idx|
    ctx.with_css(ctx.css.depth_add(2).style_compact().shift_compact(), |c| c.handle_into_node(idx))
).transpose()?;
let (tag, body) = if let Some(index) = index {
    (tag::mroot, radicand.into_content() + index)   // n 次根：被开方数 + 指数
} else {
    (tag::msqrt, Content::sequence(radicand))       // 平方根：只放被开方数
};
Ok(HtmlElem::new(tag).with_body(Some(body)).pack())
```

要点：`index` 为 `None`（平方根）→ `<msqrt>`；有 `index`（n 次根）→ `<mroot>`，且 MathML 规定 `<mroot>` 第一个孩子是被开方数、第二个是指数——代码里 `radicand + index` 正好满足。

**上下标** [`handle_scripts`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L850-L914)：先用宏 `handle!` 收集六个可能的附件（top/bottom + 四角），每个都在 `handle_into_node_lone` 下翻译（`_lone` 会剥掉孤 `mo` 的 `form`/`lspace`/`rspace`，见 4.4.3），然后两个 `match` 分别决定「角脚本」与「上下限」各自该包什么元素，最后串成嵌套结构（先角脚本、再上下限）。

**表格** [`handle_table`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L916-L969)：把二维 `cells` 翻成 `mtr`/`mtd`，并根据 `alternator` 与 `align` 给 `mtable` / `mtd` 打上对齐 class（如 `cases`、`aligned`、`right-align`），这些 class 正是 4.4 节 CSS 要作用的目标。

**叶子：glyph/number/text/primes** 都走 MathML token 元素。以 [`handle_number`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L999-L1008) 为例，直接产出 `<mn>`；`handle_glyph` 的分派更复杂（见下节）。

#### 4.3.4 代码实践

可运行实践：生成一个含分数与根号的文档，观察输出。

1. **实践目标**：验证 `$ a/b $` 与 `sqrt(...)` / `root(..., n)` 的输出确实是 `<mfrac>` / `<msqrt>` / `<mroot>`。
2. **操作步骤**：
   - 新建 `math.typ`，内容如下：
     ```typst
     #set page(margin: 20pt)

     行内分数 $a/b$ 与块级公式：

     $ a/b $

     平方根 $sqrt(2)$ 与三次根 $root(x, n: 3)$。
     ```
   - 编译为 HTML（任选其一）：
     ```bash
     typst compile math.typ math.html
     # 或显式指定格式
     typst compile --format html math.typ math.html
     ```
   - 打开 `math.html`，查看其中的 `<math>` 片段。
3. **需要观察的现象**：
   - 行内 `$a/b$` 的 `<math>` **不带** `display` 属性；独占一行的 `$ a/b $` 的 `<math>` 带 `display="block"`。
   - 分数输出形如 `<mfrac><mi>a</mi><mi>b</mi></mfrac>`。
   - 平方根输出 `<msqrt>…</msqrt>`；三次根输出 `<mroot><mi>x</mi><mn>3</mn></mroot>`。
   - `<head>` 内应能看到一个 `<style>`，里面就是 4.4 节讲的 `EQUATION_CSS_STYLES`。
4. **预期结果**：元素名与嵌套顺序与本节「对照表」一致。具体属性（如 `<mi>` 是否带 `mathvariant`）会随内容变化，属正常现象。
5. 若本地未安装 typst CLI，**待本地验证**：可仅做源码阅读——对照 [`handle_fraction`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L690-L709) 与 [`handle_radical`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L711-L735) 推演上述输出。

#### 4.3.5 小练习与答案

**练习 1**：Typst 的二项式系数（无横线的「分数」）会生成什么样的 `<mfrac>`？

> **参考答案**：生成带 `linethickness="0"` 的 `<mfrac>`。代码里 `let line = (!item.line).then_some("0");`——`item.line` 为 `false`（不画横线）时才写 `linethickness="0"`，让浏览器不画分数线，其余结构与普通分数相同。

**练习 2**：一个同时有右上标和右下标的公式（如 $a_b^c$）会生成什么？只有左下标呢？

> **参考答案**：右上+右下 → `<msubsup>`（base, br, tr 三个孩子）；只要出现任意左侧角，就改用 `<mmultiscripts>`，并用一个空的 `<mprescripts>` 把左侧脚本与右侧脚本隔开（见 [`handle_scripts`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L882-L895) 的 `_ => (tag::mmultiscripts, ...)` 分支）。

### 4.4 MathItem 叶子与 `mo` 算符属性（补足 MathItem 处理）

> 本节是对 4.3 的补充，聚焦最繁琐的一类叶子——算符 `<mo>` 的属性生成，这是「MathItem 处理」里最值得单独拆开看的部分。

#### 4.4.1 概念说明

MathML 用 `<mo>` 表示算符（`+`、`∑`、括号、关系符等）。算符的间距、拉伸、形态（前缀/中缀/后缀）在 MathML Core 里有一套复杂规则：浏览器会查一张「算符字典」（operator dictionary）决定默认间距。typst-html 的策略是——**只在 Typst 计算出的值与算符字典默认值不一致时才输出属性**，从而既保真又简洁。

#### 4.4.2 核心流程

`handle_glyph` 根据 Unicode 数学类（`MathClass`）决定一个字符走 `<mi>` 还是 `<mo>`：

- `Normal` / `Alphabetic` / `Special` / `GlyphPart` / `Space` → `<mi>`（标识符），并对会被自动斜体变换的字符设 `mathvariant="normal"` 抵消。
- `Binary` / `Relation` → `<mo>` 形态 infix；`Vary` / `Unary` → prefix；`Opening` → prefix+fence；`Closing` → postfix+fence；`Punctuation` → separator；`Large`（如 `∑`）→ largeop。
- 特例：`!` 与素数字符虽是 `Normal` 类，但语义上是后缀算符，强制走 `<mo>` postfix。

`make_mo` 是 `<mo>` 属性的总装配器，它逐项比较「期望值」与「算符字典默认值」，差异项才写属性。

#### 4.4.3 源码精读

`handle_glyph` 的 `<mi>` / `<mo>` 分派见 [`handle_glyph`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L737-L813)。

`make_mo` 的核心是「按需输出属性」。截取 [`make_mo`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L1086-L1199) 的间距处理：

```rust
let lspace = lspace.unwrap_or(Em::zero()).get();
let lspace = (force_space || lspace != info.lspace).then(|| eco_format!("{lspace}em"));
let rspace = rspace.unwrap_or(Em::zero()).get();
let rspace = (force_space || rspace != info.rspace).then(|| eco_format!("{rspace}em"));
```

`info` 来自 `OperatorInfo::of(text, form, ...)`（typst-assets 提供的算符字典镜像）。`(force_space || x != info.x).then(...)` 就是「不一致才写」的判定。其余属性（`fence`、`separator`、`stretchy`、`symmetric`、`minsize`、`largeop`、`movablelimits`）同理。

几个值得注意的设计：

- **`force_space`**：斜杠 `/` 作为中缀（分数线水平写法）时强制写 `lspace`/`rspace`，因为浏览器的算符字典还没更新 `/` 的间距（源码注释里列出了一串相关 issue 链接）。
- **`handle_into_node_lone`**（[L457-L461](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L457-L461)）：处理作为「附件」的孤立算符时，会调 [`strip_inert_mo_attrs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L1229-L1239) 剥掉 `form`/`lspace`/`rspace`——因为 MathML 规定 `<mrow>` 里孤立的 `mo` 会被强制当作 postfix 并忽略间距，主动剥掉这些属性可避免误导。
- **`movablelimits`**：在 compact（行内）样式下，浏览器会把上下限附件挪到角标位置；若算符支持 movablelimits 但 Typst 想保持上下限，就显式写 `movablelimits="false"` 禁用（见 [L1152-L1155](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L1152-L1155)）。

#### 4.4.4 代码实践

阅读型实践：理解「按需输出属性」如何让输出保持简洁。

1. 阅读 [`make_mo`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L1086-L1199)。
2. 找到所有 `.then(...)` 调用，统计 `<mo>` 最多可能带多少个属性。
3. **预期结果**：理论上可带 `form`/`fence`/`separator`/`lspace`/`rspace`/`stretchy`/`symmetric`/`minsize`/`largeop`/`movablelimits` 共 10 个属性，但每个都受「与默认值不同」或 `force_space` 条件约束，实际输出通常远少于此。
4. **待本地验证**：编译 `$ a + b = c $`，查看输出里 `+` 和 `=` 的 `<mo>` 到底带不带 `lspace`/`rspace`（若算符字典默认间距已等于 Typst 值，应不带）。

#### 4.4.5 小练习与答案

**练习**：为什么素数符号（如 $x'$）会被强制生成 `<mo>` 而不是 `<mi>`？

> **参考答案**：见 [`handle_glyph`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L749-L763) 开头的特判：素数字符的 Unicode 数学类是 `Normal`，但语义上是后缀算符（postfix），所以当 `props.class() == Normal && is_prime(text)` 时直接 `return` 一个 `Form::Postfix` 的 `<mo>`。这样它才能获得正确的后缀间距。

### 4.5 EQUATION_CSS_STYLES：用 CSS 覆盖 MathML Core UA 样式

#### 4.5.1 概念说明

哪怕 MathML 元素生成得完全正确，浏览器按 UA stylesheet 渲染出的结果仍可能与 Typst 分页导出有偏差。typst-html 的对策是 [`EQUATION_CSS_STYLES`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L99-L180)：一份精心调校的 CSS，**只在文档确实含公式时**注入到 `<head>`。它的总原则写在源码注释里——

> The main purpose is to ensure the MathML produced is rendered by browsers as closely as possible to Typst's paged export. Things not included here mean that the UA stylesheet already matches!

也就是说：**只覆盖不一致的部分，一致的不写**。Typst 分页导出的「真相」是 `typst_library::math::ir::resolve` 中的 `resolve_*` 函数与 `typst_library::math` 的 `style_*` 函数，EQUATION_CSS_STYLES 就是把这些 Typst 风格「翻译」成等价 CSS。

#### 4.5.2 核心流程

这份 CSS 分六大块，每块都解决一类渲染偏差：

1. **对齐（Alignment）**：多行公式 / cases / 矩阵的左右对齐。
2. **表格（Tables）**：撤销 `mtable` 的 `math-style: compact`，改在 `mtd` 上设分母样式。
3. **方程（Equations）**：用 `mtable` 做多行布局时，清零列间距、给非末行加行间距。
4. **分数（Fractions）**：修正 UA 给 `<mfrac>` 加的 `1px` 内边距。
5. **重音（Accents）**：统一 `dtls` OpenType 特性的开/关。
6. **scriptlevel/displaystyle/math-shift 杂项**：让下附着变 cramped、撤销重音自身被改写的样式。

注入机制：编译主链路 `html_document_common` 在 `finalize_dom` 之后查询文档是否含 `EquationElem`，若有就在 `<head>` 末尾追加一个 `<style>`。

#### 4.5.3 源码精读

注入点在 [`html_document_common`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L192-L215)：

```rust
let has_equations = !engine
    .introspect(QueryIntrospection(EquationElem::ELEM.select(), Span::detached()))
    .is_empty();

if has_equations {
    let root = output.root_mut();
    let head = root.children.make_mut().iter_mut().find_map(|node| match node {
        HtmlNode::Element(elem) if elem.tag == tag::head => Some(elem),
        _ => None,
    });
    let head = head.expect("head to be present in document output");
    head.children.push(
        HtmlElement::new(tag::style)
            .with_children(eco_vec![HtmlNode::Text(
                EQUATION_CSS_STYLES.clone(),
                Span::detached(),
            )])
            .into(),
    );
}
```

要点：

- **条件注入**：无公式则不注入，避免无谓字节。这也解释了为什么「逼近分页」的 CSS 不是全局样式，而是按需出现。
- **`<head>` 必须存在**：它 `expect("head to be present in document output")`。注释 `// TODO: this becomes an error when html fragments are supported` 暗示未来 HTML 片段（无 `<head>`）场景会改成报错——当前依赖 `finalize_dom` 总会生成 `<head>`（见 u3-l2）。
- 这一步在 `resolve_inline_styles` **之后**运行，因为注入的是独立 `<style>` 元素而非内联样式，与内联样式解析互不干扰。

EQUATION_CSS_STYLES 本体（[`mathml.rs#L99-L180`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L99-L180)）用 `eco_format!` 模板拼出，末尾两个占位符由 Typst 常量填入：

```rust
EQUATION_ROW_GAP.to_css(()),   // 行间距：EQUATION_ROW_GAP = Em::new(0.5) → "0.5em"
FRAC_PADDING.to_css(())        // 分数左右留白：FRAC_PADDING = Em::new(0.1) → "0.1em"
```

两常量定义见 [L193-L195](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L193-L195)（`EQUATION_ROW_GAP`）与 `typst-library` 的 [`FRAC_PADDING`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/math/frac.rs#L9-L10)（`Em::new(0.1)`）。`Em::to_css` 的实现是 [输出数值 + "em"](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/css/encode.rs#L353-L358)（u4-l4 讲过的 `ToCss`/`CssWriter` 机制）。

逐块看这份 CSS 解决了什么偏差：

| CSS 块 | 关键规则 | 解决的渲染偏差 |
| --- | --- | --- |
| 对齐 | `mtable.aligned mtd:nth-child(odd){ text-align:right }` 等 | Typst 的 `aligned`（如 align 点）是奇数列右对齐、偶数列左对齐；浏览器默认全部居中，须手动覆盖。`cases` / 行内多行则恒左对齐。 |
| 表格 | `mtd { math-depth: auto-add; math-style: compact; math-shift: compact }` | UA 对顶层 `mtable` 设 `math-style: compact`，Typst 不希望整个表变小，而是希望**每个 cell** 像「分母」那样缩小，故把 compact 下移到 `mtd`。 |
| 方程 | `mtable.multiline-equation mtd { padding: 0 }`、非末行 `padding-bottom: 0.5em` | 多行方程用 `mtable` 做布局，须清掉列内边距（Typst 列间无留白），并补行间距 0.5em。 |
| 分数 | `mfrac { padding-inline: 0; margin-inline: 0.1em }` | UA 给 `mfrac` 加了 `1px` **内**边距，会让分数线变长而不是在外围留白；Typst 要的是外围 0.1em 留白（= `FRAC_PADDING`），故清内边距、改用 `margin-inline`。 |
| 重音 | `mover[accent="true"] > :first-child { font-feature-settings: "dtls" }`，`.dotted` 类关闭之 | 只有 Firefox 默认开 `dtls`（让重音下的字母变无点）；为跨浏览器一致，统一默认开启，再用 `.dotted` 类按需关闭（见 [`handle_accent`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L815-L848) 的 `dotted` 判定）。 |
| 杂项 | `munder > :nth-child(2) { math-shift: compact }` 等 | 让下附着变 cramped 以匹配 Typst；并撤销重音对自身基座被改写的 `math-depth`/`math-style`/`math-shift`（因为重音的基座字号应保持不变）。 |

类名常量定义在 [L183-L190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L183-L190)（`multiline-equation` / `aligned` / `cases` / `right-align` / `left-align` / `flushed` / `left-flush` / `right-flush`），它们正是 4.3 节 `handle_table` / `handle_multiline` / `table_mtd_class` 给元素打上的 class——**生成端打 class、CSS 端消费 class**，两端由此对齐。

#### 4.5.4 代码实践

综合实践：亲手验证「分数留白」这条 CSS 解决了什么。

1. **实践目标**：理解 `mfrac { margin-inline: 0.1em }` 的作用，并验证它在输出中确实出现。
2. **操作步骤**：
   - 编译 4.3.4 节的 `math.typ` 得到 `math.html`。
   - 在 `math.html` 的 `<head>` 里找到 `<style>`，定位 `/* Fractions */` 段，应能看到 `mfrac { padding-inline: 0; margin-inline: 0.1em }`。
   - 用浏览器打开 `math.html`，对比「分数左右是否有约 0.1em 的留白」与「分数线长度是否恰好等于分子分母宽度（不因 1px 内边距而变长）」。
3. **需要观察的现象**：分子分母较窄时，若无 `margin-inline`，分数会紧贴两侧算符；有了 0.1em 留白后视觉间距更接近 Typst PDF 导出。同时因 `padding-inline: 0`，分数线不会因 UA 的 1px 内边距而变长。
4. **预期结果**：浏览器渲染的分数留白与 Typst 分页导出基本一致。
5. **进阶（可选，待本地验证）**：用浏览器开发者工具临时禁用 `mfrac` 的 `margin-inline` 与 `padding-inline` 规则，观察分数两侧留白消失 / 分数线变长的差异，直观感受「覆盖」的必要性。

#### 4.5.5 小练习与答案

**练习 1**：为什么 EQUATION_CSS_STYLES 用 `LazyLock<EcoString>` 而不是普通 `const`？

> **参考答案**：它的内容依赖运行期计算——`EQUATION_ROW_GAP.to_css(())` 与 `FRAC_PADDING.to_css(())` 要调 `to_css` 把 `Em` 序列化成字符串，而 `to_css` 不是 `const fn`（它走 `CssWriter`/`Number` 格式化）。`LazyLock` 让这份字符串在首次访问时构造一次、之后复用，既满足「非 const 构造」又避免每次注入都重算。

**练习 2**：如果文档里没有任何公式，`<head>` 里会出现这个 `<style>` 吗？为什么？

> **参考答案**：不会。注入由 `if has_equations { ... }` 守卫（[document.rs#L196](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L196)），`has_equations` 通过内省查询 `EquationElem` 得到。无公式时整段注入被跳过，输出更精简，也避免向无关文档注入 MathML 专属样式。

## 5. 综合实践

把本讲四个最小模块串起来：追踪一条**含分数的三次根公式**从 Typst 源码到最终 HTML 的完整旅程。

**任务**：写一份 `big.typ`：

```typst
#set page(margin: 20pt)
$ root(x + 1/y, n: 3) $
```

编译并回答下列问题（结合源码）：

1. **触发点**：`EquationElem` 命中哪条 show 规则？该规则在 [`rules.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L822-L841) 里先调谁拿到 IR？
2. **结构映射**：最外层是 `Radical`（有 index）→ 应输出 `<mroot>`。它的第一个孩子是什么（被开方数）？第二个孩子是什么（根指数）？被开方数本身又含一个 `Fraction` → 输出什么元素？
3. **上下文调整**：根指数翻译时 [`handle_radical`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L711-L735) 用了哪个 CSS 上下文（`depth_add(2) + style_compact + shift_compact`）？分数的分子呢？
4. **CSS 注入**：输出 `<head>` 里应有 `<style>`，其中 `/* Fractions */` 段的 `margin-inline` 值是多少？它修正了 UA stylesheet 的什么问题？
5. **元素核对**：展开 `big.html`，确认根指数 `3` 出现在 `<mn>` 里、分数横线两侧分别是 `x+1` 与 `y`。

**验收标准**：能画出这棵公式对应的 MathML 元素树（`<math display="block">` → `<mroot>` → [`<mfrac>` → …] + `<mn>3</mn>`），并能指出树上每个元素的字号缩放来自哪一层 CSS 上下文调整。

## 6. 本讲小结

- typst-html 不重新解析公式，而是接手 `typst_library::math::ir` 的 `MathItem` IR——它与分页导出共享同一份公式分析结果，分叉点在 `resolve_equation` 之后。
- [`EQUATION_RULE`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L822-L841) 是唯一的数学 show 规则：调 `resolve_equation` 拿 IR → 调 `convert_math_to_nodes` 翻译 → 包进 `<math>`（块级带 `display="block"` 且外套 `BlockElem`）。
- [`convert_math_to_nodes`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L197-L205) 入口极薄，真正承重的是 `MathContext` 状态机与 `CssContext` 三轴（`math_style`/`math_shift`/`math_depth`）追踪——只在子元素目标上下文与当前不一致时才输出属性，保持输出简洁。
- 结构 → MathML 的映射是一张清晰的对照表：`Fraction→<mfrac>`、`Radical→<msqrt>/<mroot>`、`Scripts→<msub>/<msup>/<msubsup>/<mmultiscripts>` 叠 `<mover>/<munder>/<munderover>`、`Table/Multiline→<mtable>`，每种结构在翻译子元素时都同步调整 CSS 上下文以自动缩小字号。
- `make_mo` 用「与算符字典默认值不同才写属性」的策略生成 `<mo>`，最多 10 个属性但实际按需输出；孤立算符作为附件时还会剥掉 `form`/`lspace`/`rspace`。
- [`EQUATION_CSS_STYLES`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L99-L180) 是一份「逼近分页导出」的 CSS，按需注入 `<head>` 的 `<style>`，覆盖对齐、表格缩放、多行行距、分数留白、重音 `dtls` 等六类偏差——只写 UA stylesheet 不一致的部分。
- 生成端（`handle_table`/`handle_multiline`/`table_mtd_class` 打 class）与消费端（EQUATION_CSS_STYLES 作用 class）通过固定的类名常量（[L183-L190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L183-L190)）对齐，这是理解整套数学样式机制的关键耦合点。

## 7. 下一步学习建议

- **横向对比**：回到 u3-l5，把 `EQUATION_RULE` 与 `TABLE_RULE`、`IMAGE_RULE` 放在一起看，体会 show 规则「读样式 → 生成 HtmlElem」的不同复杂度层级。
- **纵向深挖 IR**：阅读 `typst-library/src/math/ir/item.rs` 与 `ir/resolve.rs`，理解 `MathItem` 是如何从用户的公式内容 `resolve` 出来的，以及 `style_for_denominator`、`style_cramped` 等 `style_*` 函数如何对应到本讲的 CSS 上下文调整与 EQUATION_CSS_STYLES 注释里提到的「真相」。
- **接续专家主题**：下一单元（u6）会进入 `html.frame` 与 SVG 嵌入（u6-l1）、表格与图片导出（u6-l2）等专题；其中 u6-l1 会解释当用户用 `#show math.equation: html.frame` 把公式改成 SVG 嵌入时会发生什么，正是对本讲 4.2.5 练习 2 的展开。建议读 u6-l1 前复习本讲的 `EQUATION_RULE` 与 u3-l5 的 `FrameElem` no-op 规则。
- **规范对照**：遇到不确定的 MathML 行为时，以 [MathML Core 规范](https://www.w3.org/TR/mathml-core/)（尤其 §3.2.4 算符属性、§3.2.5 间距规则、用户代理样式表一节）为准——typst-html 的注释里多次直接引用了规范章节号。
