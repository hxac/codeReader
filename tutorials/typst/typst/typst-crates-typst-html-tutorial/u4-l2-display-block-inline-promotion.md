# display 属性与块级/行内提升

## 1. 本讲目标

本讲聚焦 typst-html 如何用一条 CSS 属性——`display`——来精确控制每一个 HTML 元素在浏览器里到底是「独占一行的块」还是「夹在文字流里的行内片段」。

学完后你应当能够：

- 说清 `Display` 枚举有哪些变体，以及它如何对应 HTML 规范 §15 的「用户代理样式表（UA stylesheet）」。
- 读懂 `default_for(tag)` 这张「标签 → 默认 display」对照表，并能查阅任意标签的默认值。
- 理解 `set_display` 为什么只写 `css` 字段、不碰用户 `attrs`。
- 理解 `to_lone_element` 如何剥掉内省 Tag、识别出「单个真实元素」的提升窗口。
- 读懂 `make_block_level` / `make_inline_level` 的提升决策，尤其是哪些 display 值能被「就地提升」、哪些只能「包一层 `<div>`」，以及数学公式（`InlineMath`/`BlockMath`）的特殊处理。

本讲是 u4 单元「空白、display 与 CSS 机制」的第二篇，紧接 u4-l1（空白保护）。它把 u2-l4 讲过的「内容模型分类」与 u3-l3 讲过的 `convert.rs` 转换器主循环串联起来，回答一个核心问题：**当 Typst 的 `block`/`box` 把一段内容标成块级或行内时，typst-html 怎样把这个意图翻译成浏览器看得懂的 CSS？**

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

### 2.1 CSS 的 `display` 与块级/行内

浏览器渲染 HTML 时，每个元素都有一个 `display` 属性决定它如何排版。最经典的两类是：

- **块级（block）**：独占一行，像 `<div>`、`<p>`、`<h1>`，从上往下堆叠。
- **行内（inline）**：像文字一样在一行里流动，像 `<span>`、`<a>`、`<strong>`。

除此之外还有 `inline-block`（行内但可设宽高）、`table` 系列、`flex`/`grid`、以及 MathML Core 引入的 `inline math`/`block math` 等。这些值都定义在 CSS Display Module Level 3 规范里。

### 2.1 HTML 规范 §15 给每个标签规定了「默认 display」

浏览器出厂时自带的样式表叫「用户代理样式表（UA stylesheet）」。HTML 规范的 §15 把它写得很细：比如规定 `<div>` 默认 `display: block`、`<span>` 默认 `display: inline`、`<table>` 默认 `display: table`。typst-html 的 `property.rs` 几乎是 §15 的逐条 Rust 翻译。

### 2.3 Typst 的 `block`/`box` 与 HTML 的块级/行内

在 Typst 里，`block` 和 `box` 是两种最基础的排版容器：`block` 把内容排成块级流，`box` 把内容排成行内片段。当 typst-html 把 Typst 文档转成 HTML 时，它需要把「这段内容被用户包成了一个 block」的意图，映射成 HTML 的块级渲染。但 HTML 元素自己已经有默认 display 了（比如 `<img>` 默认是 `inline`），于是 typst-html 必须做**提升（promotion）**：要么给元素加一条 `display: block` CSS，要么干脆用一个 `<div>` 把它包起来。本讲讲的就是这套提升逻辑。

> 前置讲义术语回顾：`HtmlElement` 的 `css` 字段是「编译器生成的样式」（见 u2-l1、u2-l3），与用户写在 `attrs` 里的 `style` 分开存放，最终由 `resolve_inline_styles`（u4-l3）合并进 `style` 属性。`HtmlNode::Tag` 是仅供内省、不产生 HTML 输出的节点（u2-l1）。`ConversionLevel`（Block/Inline）决定智能引号状态是否自包含（u3-l3）。

## 3. 本讲源码地图

本讲只读两个文件：

| 文件 | 作用 | 本讲用到的部分 |
|------|------|----------------|
| `src/property.rs` | 类型化 CSS 属性。目前只定义了 `Display`。 | `Display` 枚举、`default_for`、`as_str`、`is_tabular` |
| `src/convert.rs` | 把具象化 `Content` 流式翻译为 `HtmlNode` 的转换器。 | `set_display`、`to_lone_element`、`make_block_level`、`make_inline_level`、`handle_block`、`handle_box` |

此外会顺带引用 `src/lib.rs`（`HtmlElem.css` 字段定义）和 `crates/typst-utils/src/lib.rs`（`split_prefix_suffix`），用于说明 `css` 字段和 `to_lone_element` 的底层细节。

数据流定位（来自 u3-l3）：本讲的函数都运行在 `convert_to_nodes` 内部，当 `handle()` 遇到 `BlockElem`/`BoxElem`/`FrameElem` 时被调用，用来决定这些「容器」产出的子节点是否需要调整 display。

---

## 4. 核心概念与源码讲解

### 4.1 `Display` 枚举：CSS display 的类型化表示

#### 4.1.1 概念说明

CSS 的 `display` 属性取值是一个字符串，比如 `"block"`、`"inline-block"`、`"inline math"`。但在 Rust 里用裸字符串容易写错、不好穷举。typst-html 把它建模成一个枚举 `Display`，每个变体对应一个合法的 CSS display 值。这样做有三个好处：

1. **编译期穷举**：新增一个 display 值必须改枚举，`match` 会在编译期提醒你补全所有分支。
2. **可比较、可哈希**：枚举天生 `Eq + Hash`，方便做决策表。
3. **集中维护**：所有「display 怎么序列化成 CSS 字符串」的知识都收在 `as_str()` 一处。

#### 4.1.2 核心流程

`Display` 的变体按 CSS Display Level 3 规范的「取值语法」分类排列，源码注释也对应规范分组：

```
<display-outside>   Block, Inline, RunIn
<display-inside>    Flow, FlowRoot, Table, Flex, Grid, Ruby
<display-listitem>  ListItem
<display-internal>  TableRowGroup, TableHeaderGroup, ..., RubyBase, RubyText
<display-box>       Contents, None
<display-legacy>    InlineBlock, InlineTable, InlineFlex, InlineGrid
MathML Core         InlineMath, BlockMath
```

注意最后两个 `InlineMath`/`BlockMath`：MathML Core 给 `display` 扩展了 `math` 这个特殊取值，序列化为 `"inline math"` / `"block math"`（注意中间有空格），它们会在 4.5 节看到对数学公式有专门的提升分支。

配套的两个方法：
- `as_str()`：把变体翻成 CSS 字符串。
- `is_tabular()`：判断是不是 `table(-.*)?` 家族（用于表格相关判断）。

#### 4.1.3 源码精读

枚举定义，注释里标明来源规范（[property.rs:10-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L10-L57)）——这是整张取值表，从 `<display-outside>` 一直列到 MathML Core 的 `InlineMath`/`BlockMath`。

`as_str()` 是变体到 CSS 标识符的一一映射（[property.rs:242-274](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L242-L274)），其中数学两个变体序列化为带空格的 `"inline math"` / `"block math"`，这是 MathML Core 规范的特殊语法。

`is_tabular()` 用 `matches!` 宏一次判定九个表格相关变体（[property.rs:226-240](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L226-L240)）。

#### 4.1.4 代码实践

**实践目标**：建立「枚举变体 ↔ CSS 字符串」的直觉。

**操作步骤**：打开 [property.rs 的 `as_str`（L242-274）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L242-L274)，对照枚举定义 [property.rs:14-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L14-L57)，逐个写下每个变体序列化后的 CSS 值。

**需要观察的现象**：大部分变体序列化结果就是小写化的变体名（如 `Flex => "flex"`），但有三处「不平凡」：
- `FlowRoot => "flow-root"`（连字符）
- `ListItem => "list-item"`（连字符）
- `InlineMath => "inline math"`、`BlockMath => "block math"`（空格，MathML 特例）

**预期结果**：你会确认「`as_str()` 不是简单的变体名小写化」，连字符和空格的来源是 CSS/MathML 规范的标识符语法。这一步纯源码阅读，无需运行，结论可立即验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Display` 要派生 `Hash`？

**参考答案**：`default_for` 的返回值要被放进 `make_block_level` 的 `match` 做决策，而 `Display` 也可能作为缓存键或样式链中的值参与哈希计算；派生 `Eq + PartialEq + Hash` 让它既能比较又能进哈希结构。源码在 [property.rs:14](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L14) 处 `#[derive(Debug, Copy, Clone, Eq, PartialEq, Hash)]`。

**练习 2**：`InlineMath.as_str()` 返回 `"inline math"`，为什么中间是空格而不是连字符？

**参考答案**：因为这是 MathML Core 规范给 `display` 扩展的取值语法（`inline math` / `block math` 是两个 token 的组合，分别表达 `<display-outside>` 和 `math` 这个 `<display-inside>`），不是 CSS 的单一连字符标识符。见 [property.rs:271-272](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L271-L272)。

---

### 4.2 `default_for`：标签的默认 display（§15 UA 样式表）

#### 4.2.1 概念说明

`default_for(tag)` 回答一个问题：「按照 HTML 规范 §15 的用户代理样式表，这个标签的默认 `display` 是什么？」它是一张静态查表函数：输入一个 `HtmlTag`，返回 `Option<Display>`。

返回 `Option` 是关键设计：**对于未知标签（包括用户自定义的 `my-elem`），返回 `None`**，表示「不对其 display 做任何假设」。这个 `None` 在 4.5 节会看到被当作「可提升」处理。

#### 4.2.2 核心流程

函数体是一个巨大的 `match tag { ... }`，按 §15 的小节分组：

```
§15.3.1 隐藏元素        area/base/.../title   → None（不渲染）
§15.3.2 页面            html/body             → Block
§15.3.3 流内容          div/p/figure/...      → Block
§15.3.4 短语内容        ruby/rt               → Ruby / RubyText
§15.3.6 章节与标题      h1..h6/section/...    → Block
§15.3.7 列表            ol/ul → Block, li → ListItem
§15.3.8 表格            table→Table, tr→TableRow, td→TableCell, ...
§15.3.10+ 表单控件      input/button→InlineBlock, select→InlineBlock, option→Block
§默认                   a/span/img/em/...     → Inline
MathML                  math→InlineMath, mtable→InlineTable, ...
未知                    _                     → return None
```

最后一行 `_ => return None` 是兜底分支，对应「未知/自定义元素」。注意 `Some(...)` 包在最外层 `match` 上，所以 `_` 分支必须用 `return None` 提前退出，绕开外层 `Some`。

#### 4.2.3 源码精读

函数签名与文档（[property.rs:59-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L59-L63)）——明确说这是 §15 的 UA 样式表。

隐藏元素全部映射 `None`（[property.rs:64-75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L64-L75)）：`area`/`base`/`meta`/`script`/`style` 等默认不参与渲染。

表格系列是按规范精确映射的（[property.rs:125-135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L125-L135)）：`table => Table`、`tr => TableRow`、`td => TableCell`，等等。这张表在 4.5 节会直接影响「能否就地提升」的判定。

兜底分支 `_ => return None`（[property.rs:221-223](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L221-L223)）：未知标签不假设 display。

值得专门看的是 `a` 到 `wbr` 这一大段（[property.rs:171-213](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L171-L213)）：注释说「`display: inline` 是 CSS 属性的默认值」。也就是说，这些标签（含 `img`、`span`、`a`、`strong`）在 typst-html 里被**显式**记为 `Inline`，而非依赖「CSS 默认就是 inline」。这点很关键——正因为 `img` 的默认是 `Inline`，它才会触发 4.5 节的「提升为 Block」逻辑。

#### 4.2.4 代码实践

**实践目标**：建立查表能力，能对任意标签说出默认 display。

**操作步骤**：在 [property.rs 的 `default_for`（L62-224）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L62-L224) 里，分别查这几个标签：`img`、`li`、`ruby`、`math`（MathML）、以及一个假设的自定义标签 `my-widget`。

**需要观察的现象**：
- `img` → 命中 [property.rs:189](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L189) 那一行 `tag::img => Self::Inline`。
- `li` → `ListItem`。
- `ruby` → `Ruby`（不是 `Inline`，注意区别）。
- `mathml::math` → `InlineMath`。
- `my-widget` → 未在任何分支列出 → `_` 分支 → `None`。

**预期结果**：你会确认「`img` 默认是 `Inline`，而非 `InlineBlock`」，这正是为什么一个被 `block` 包裹的 `<img>` 需要被提升为 block（4.5 节综合实践会用到）。纯源码阅读，结论立即可验。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `default_for` 返回 `Option<Display>` 而不是 `Display`？哪些情况返回 `None`？

**参考答案**：返回 `None` 表示「typst-html 不对该标签的 display 做任何假设」。两种情况返回 `None`：(1) §15.3.1 的隐藏元素（`meta`/`script`/`style` 等），它们不渲染；(2) 未知/自定义标签（`_` 兜底分支，[property.rs:221-223](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L221-L223)）。这两类虽然都返回 `None`，但在 4.5 节的 `make_block_level` 里语义不同——后者会被当作「可提升为 block」。

**练习 2**：`ruby` 的默认是 `Ruby`，`rt` 的默认是 `RubyText`，为什么它们没有像其他短语内容那样记成 `Inline`？

**参考答案**：因为 §15.3.4 专门为 ruby 注音体系规定了非平凡的 display：`ruby` 元素本身是 `display: ruby`，其子元素 `rt`（注音文字）是 `display: ruby-text`，这样才能让浏览器把注音正确叠放在基底文字上方。typst-html 忠实记录了这个规范行为（[property.rs:99-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L99-L101)）。

---

### 4.3 `set_display`：写入与清除 display 的统一工具

#### 4.3.1 概念说明

提升逻辑（4.5 节）算出一个目标 display 后，需要把它「贴」到节点上。`set_display` 就是这个贴标签的工具，它做了一件看似简单但有讲究的事：**只改 `css` 字段，不碰 `attrs`**。

回顾 u2-l1/u2-l3：`HtmlElement` 有两个独立的样式来源——用户写的 `attrs`（含 `style` 属性）和编译器生成的 `css`（`Properties`）。`set_display` 只往编译器的 `css` 里写，等后续 `resolve_inline_styles`（u4-l3）再把两边的 `style` 合并。

#### 4.3.2 核心流程

```
set_display(node, display):
  match node:
    Element(e) => css = &mut e.css
    Frame(f)   => css = &mut f.css
    其它(Text/Tag) => 直接 return（display 对纯文本/内省标记无意义）
  match display:
    Some(d) => css.push("display", d.as_str())   // 写入
    None    => css.remove("display")             // 清除
```

两个细节：
1. 它同时支持 `HtmlNode::Element` 和 `HtmlNode::Frame`——因为 `html.frame` 产出的内联 SVG 也是一个可以挂 display 的「渲染单元」（见 4.5 节 `Frame => Some(Inline)`）。
2. `None` 分支调用 `css.remove("display")`，用于「撤销」之前可能写过的 display（`make_inline_level` 会用到这个能力）。

#### 4.3.3 源码精读

完整实现（[convert.rs:499-510](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L499-L510)）：

```rust
fn set_display(node: &mut HtmlNode, display: Option<property::Display>) {
    let css = match node {
        HtmlNode::Element(element) => &mut element.css,
        HtmlNode::Frame(frame) => &mut frame.css,
        _ => return,
    };
    match display {
        Some(display) => css.push("display", display.as_str()),
        None => css.remove("display"),
    }
}
```

注意它写的是 `element.css`，不是 `element.attrs`。`css` 字段的定义在 [lib.rs:74-77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L74-L77)，标注为 `#[internal]` 且注释「Currently only used for generated styles」——也就是说，编译器生成的 display 与用户手写的 `style` 属性是分开存放的，避免互相覆盖。`css.push`/`css.remove` 是 `Properties`（来自 `css/encode.rs`，u4-l3）的方法，`push` 会做按名去重的二分插入。

#### 4.3.4 代码实践

**实践目标**：确认 display 写入的是 `css` 而非 `attrs`，并理解它最终的归宿。

**操作步骤**：
1. 读 [convert.rs:500-510](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L500-L510)，确认 `set_display` 写入 `element.css`。
2. 回顾 [lib.rs:74-77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L74-L77)，确认 `css` 是 `#[internal]` 的 `Properties`。
3. 跳到 u4-l3 会讲的 `resolve_inline_styles`（`src/css/resolve.rs`），它是把 `css` 合并进 `style` 属性的环节。

**需要观察的现象**：display 这条 CSS 从不直接出现在 `attrs` 的 `style` 里，而是先进入 `css` 字段，最后才由 `resolve_inline_styles` 与用户已有的 `style` 合并输出。

**预期结果**：你会理解「编译器样式」与「用户样式」的分轨设计——typst-html 永远不会因为生成 `display: block` 而覆盖用户写在 `html.elem(attrs: (style: "..."))` 里的样式，二者是合并关系。纯源码阅读，结论可立即验证。

#### 4.3.5 小练习与答案

**练习 1**：`set_display` 对 `HtmlNode::Text` 和 `HtmlNode::Tag` 会做什么？

**参考答案**：什么都不做，直接 `return`（[convert.rs:504](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L504) 的 `_ => return`）。纯文本节点没有 `css` 字段，display 对它无意义；`Tag` 是仅供内省的节点（u2-l1），不产生 HTML 输出，同样无需 display。

**练习 2**：为什么 `None` 分支要 `css.remove("display")` 而不是「什么都不做」？

**参考答案**：因为同一个节点可能先被 `make_block_level` 写入过 `display: block`，后来又在行内上下文里被 `make_inline_level` 要求恢复成默认。`remove` 保证「撤销 display」是幂等的，让节点回到浏览器对它标签的默认渲染（比如让 `<img>` 回到 `inline`）。

---

### 4.4 `to_lone_element`：识别「单个真实元素」的提升窗口

#### 4.4.1 概念说明

提升（promotion）有一个重要的优化前提：**只有当一个容器里恰好只有一个真实元素时，才能「就地」改它的 display；否则只能包一层 `<div>`/`<span>`。** `to_lone_element` 就是用来判定「这堆节点是否恰好构成单个真实元素」的过滤器。

它要解决一个麻烦：转换器产出的节点序列里，常常夹着一些 `HtmlNode::Tag` 节点——它们是内省用的标记（u2-l1），不产生任何 HTML 输出，但在数组里占着位置。所以「单元素」判定必须先把这些 Tag 剥掉。

#### 4.4.2 核心流程

```
to_lone_element(nodes) -> Option<&mut HtmlNode>:
  (start, end) = nodes.split_prefix_suffix(判断节点是否为 Tag)
  // [start..end] 是剥掉首尾 Tag 后的「真实内核」
  如果 nodes[start..end] 恰好是 [单个 Element 或 单个 Frame]:
    返回对该节点的可变引用
  否则:
    返回 None
```

`split_prefix_suffix` 来自 `typst_utils::SliceExt`，它返回两个下标 `(start, end)`，使得 `[start..end]` 是「去掉匹配谓词的首部和尾部之后」的内核区间。这里谓词是「是否为 Tag」。

#### 4.4.3 源码精读

完整实现（[convert.rs:492-497](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L492-L497)）：

```rust
fn to_lone_element(nodes: &mut EcoVec<HtmlNode>) -> Option<&mut HtmlNode> {
    let (start, end) = nodes.split_prefix_suffix(|node| matches!(node, HtmlNode::Tag(_)));
    matches!(&nodes[start..end], [HtmlNode::Element(_) | HtmlNode::Frame(_)])
        .then(|| &mut nodes.make_mut()[start])
}
```

逐行解释：
- 第一行：用 `split_prefix_suffix` 找出「剥掉首尾 Tag 后」的内核区间 `[start..end]`。
- 第二行：用 `matches!` 判断这个内核是否恰好匹配模式 `[HtmlNode::Element(_) | HtmlNode::Frame(_)]`——即「长度为 1，且唯一元素是 Element 或 Frame」。模式 `[x]` 要求切片长度恰好为 1。
- 第三行：`bool.then(...)`，若为真则用 `nodes.make_mut()` 拿到可变切片（`make_mut` 处理 EcoVec 的写时复制），返回第 `start` 个节点的可变引用。

`split_prefix_suffix` 的实现（[typst-utils/lib.rs:179-190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/lib.rs#L179-L190)）算出 `start = 第一个非 Tag 的下标`、`end = start + 最后一个非 Tag 的相对下标 + 1`，于是 `[start..end]` 正好是「首尾 Tag 都被剥掉」的内核。

#### 4.4.4 代码实践

**实践目标**：理解「剥 Tag」对单元素判定的必要性。

**操作步骤**：手算下面三种节点序列在 `to_lone_element` 下的返回值（假设 `T` 代表某个 `Tag` 节点，`E` 代表一个 `Element`，`F` 代表一个 `Frame`，`T2` 代表另一个 Tag）：
1. `[T, E, T2]`
2. `[E, E]`
3. `[T, T2]`（只有 Tag）

**需要观察的现象**：
1. `[T, E, T2]`：剥掉首尾 Tag 后内核是 `[E]`，长度 1 且是 Element → 返回 `Some(&mut E)`。**这正是「内省 Tag 包夹一个真实元素」的典型形态**。
2. `[E, E]`：首尾都没有 Tag，内核是 `[E, E]`，长度 2 → 返回 `None`（多于一个元素，无法就地提升）。
3. `[T, T2]`：全部是 Tag，`start == end == len`（内核为空），`matches!` 不匹配空切片 → 返回 `None`。

**预期结果**：你会确认「`to_lone_element` 容忍首尾任意数量的内省 Tag，但内核必须恰好是一个 Element 或 Frame」。这解释了为什么内省 Tag 的存在不会破坏就地提升优化。纯逻辑推演，对照源码即可验证。

#### 4.4.5 小练习与答案

**练习 1**：如果 `nodes` 是 `[Tag, Tag, Element]`（两个 Tag 在前，一个 Element 在后，没有尾部 Tag），`to_lone_element` 返回什么？

**参考答案**：返回 `Some(指向那个 Element)`。`split_prefix_suffix` 会把连续的前缀 Tag 都剥掉（[typst-utils/lib.rs:183](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/lib.rs#L183) 的 `position` 找第一个非 Tag），内核是 `[Element]`，匹配 `[Element | Frame]`。这进一步说明它剥的是「任意长度的首尾 Tag」，而非固定一个。

**练习 2**：为什么返回的是 `&mut HtmlNode`（可变引用）而不是副本？

**参考答案**：因为调用方（`handle_block`/`handle_box`）拿到引用后要立刻对它做 `make_block_level`/`make_inline_level` 改 display（4.5 节），需要就地修改 EcoVec 里的那个节点；返回副本就改不到数组里了。注意函数体内用了 `nodes.make_mut()` 来获取可变切片（[convert.rs:496](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L496)），正是为了能在写时复制的 EcoVec 上拿到可变访问。

---

### 4.5 `make_block_level` / `make_inline_level`：块级与行内的双向提升

> 这一节是本讲的核心，也是「提升（promotion）」一词的真正所指。它把前四节的零件组装起来：用 `default_for` 查默认值，用 `to_lone_element` 拿到单元素，用 `set_display` 贴标签。

#### 4.5.1 概念说明

typst 的 `block` 和 `box` 是排版层的「块级/行内」指令。当转换器遇到它们时，需要把内部内容提升成对应层级。但 HTML 元素已经有自己的默认 display，于是提升分三种结局：

1. **就地提升**：元素本身就是行内的（如 `<img>` 默认 `Inline`），而我们要它块级——直接给它加 `display: block` 即可。
2. **保持不动**：元素已经是块级（如 `<div>` 默认 `Block`），或本就该隐藏（`None`）——什么都不做。
3. **无法提升，外包一层**：元素的 display 是 `Ruby`、表格内部系列等「不能简单改成 block」的特殊值——只能用一个 `<div>`（块级）或 `<span>`（行内）把它整个包起来。

`make_block_level` 负责前两种结局的判定（第三种用返回 `Err(Unblockable)` 表达），`make_inline_level` 负责行内方向。两者都会被 `handle_block`/`handle_box` 在确认「单元素」后调用。

#### 4.5.2 核心流程

**`make_block_level(node) -> Result<(), Unblockable>`**：

```
1. 算出 node 的「默认 display」default:
   - Frame(_)                      => Some(Inline)        // frame 默认行内
   - math 元素且 display 属性=="block" => Some(BlockMath)  // 已是块级数学
   - math 元素其它情况               => default_for(tag) = InlineMath
   - 普通元素                       => default_for(tag)
   - Text/Tag 等其它                => Err(Unblockable)   // 无法提升

2. 根据 default 决定 mode（要写入的 display）：
   - None | Block | Table | ListItem | Contents | BlockMath
       => None（已经是块级或隐藏，保持不动）
   - None(未知标签) | Inline | InlineBlock
       => Some(Block)（提升为块）
   - InlineMath
       => Some(BlockMath)（数学从行内升为块级）
   - 其它（Ruby/RubyText/表格内部/InlineTable/...）
       => Err(Unblockable)（无法就地提升）

3. set_display(node, mode)；返回 Ok(())
```

**`make_inline_level(node)`**（总是成功，返回 `()`）：

```
1. mode:
   - 如果是 math 元素且 display 属性=="block" => Some(InlineMath)  // 强制压回行内数学
   - 否则                                    => None              // 清掉 display，恢复默认
2. set_display(node, mode)
```

行内方向的注释（[convert.rs:371-380](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L371-L380)）解释了为什么绝大多数情况只需 `None`（清掉 display）：因为段落里合法的短语内容，其默认 display 恰好都是允许的（`none`/`inline`/`inline-block`/`contents`/`ruby`/`inline math`），恢复默认即可。

#### 4.5.3 源码精读

`make_block_level` 完整实现（[convert.rs:440-486](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L440-L486)）：

```rust
fn make_block_level(node: &mut HtmlNode) -> Result<(), Unblockable> {
    let default = match node {
        HtmlNode::Element(element)
            if element.tag == tag::mathml::math
                && element.attrs.get(attr::mathml::display)
                    .is_some_and(|v| v == "block") =>
        {
            Some(property::Display::BlockMath)
        }
        HtmlNode::Element(element) => property::Display::default_for(element.tag),
        HtmlNode::Frame(_) => Some(property::Display::Inline),
        _ => return Err(Unblockable),
    };

    let mode = match default {
        Some(property::Display::None | property::Display::Block
            | property::Display::Table | property::Display::ListItem
            | property::Display::Contents | property::Display::BlockMath) => None,
        None | Some(property::Display::Inline | property::Display::InlineBlock) =>
            Some(property::Display::Block),
        Some(property::Display::InlineMath) => Some(property::Display::BlockMath),
        _ => return Err(Unblockable),
    };

    set_display(node, mode);
    Ok(())
}
```

三处关键：
- **数学的特殊判定**（[convert.rs:448-456](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L448-L456)）：math 元素的默认 display 不是单看标签，还要看它的 `display` **属性**（注意是 HTML 属性 `display="block"`，不是 CSS）。如果属性是 `block`，默认就是 `BlockMath`（已是块级）；否则走 `default_for` 得 `InlineMath`，再在下一步提升为 `BlockMath`。
- **Frame 当作 Inline**（[convert.rs:458](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L458)）：`html.frame` 产出的内联 SVG 默认按行内处理，所以块级上下文里会被提升为 `Block`。
- **`_ => Err(Unblockable)`**（[convert.rs:481](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L481)）：`Ruby`/`RubyText`/`InlineTable`/表格内部系列等 display 无法安全地改成 block，函数返回错误，交由调用方外包 `<div>`。

`make_inline_level` 实现（[convert.rs:381-391](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L381-L391)）——只有数学的 `display="block"` 属性需要被强制压回 `InlineMath`，其余情况一律 `None`（清 display）。

`Unblockable` 是个空标记类型（[convert.rs:488-490](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L488-L490)），纯粹用来在类型层面表达「这个元素无法就地提升成块级」。

#### 调用现场：`handle_block` / `handle_box`

提升逻辑只有在「容器里恰好是单元素」时才尝试就地提升，否则直接外包。看 `handle_block`（[convert.rs:393-438](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L393-L438)）的关键片段（[convert.rs:422-435](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L422-L435)）：

```rust
if let Some(node) = to_lone_element(&mut children)
    && make_block_level(node).is_ok()
{
    converter.extend(children);   // 就地提升成功，直接展开
    return Ok(());
}
converter.push(HtmlElement::new(tag::div).with_children(children)...); // 否则外包 <div>
```

注意 `&& make_block_level(node).is_ok()` 这个短路：只有 `to_lone_element` 找到单元素 **且** `make_block_level` 成功（没返回 `Unblockable`）时才就地展开；否则 fallback 到外包 `<div>`。这就是「无法提升的 display 值必须包一层 `<div>`」的实现机制。

`handle_box`（[convert.rs:337-369](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L337-L369)）是行内版本：单元素时调 `make_inline_level`（[convert.rs:353-356](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L353-L356)），否则外包一个带 `display: inline-block` 的 `<span>`（[convert.rs:360-366](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L360-L366)）。注意 `make_inline_level` 总是成功（返回 `()`），所以 `handle_box` 不需要像 `handle_block` 那样检查 `.is_ok()`。

`FrameElem` 的处理在 `handle` 里直接对单个 frame 节点调 `make_block_level(...).unwrap()`（[convert.rs:150-154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L150-L154)）：因为 frame 默认是 `Inline`，必然能提升为 block，所以用 `unwrap()` 是安全的（注释也说了「A frame is block-level by default like a Typst `image`」）。

#### 4.5.4 代码实践

**实践目标**：回答本讲开篇的实践任务——一个被 Typst `block` 包裹的单个 `<img>` 会得到什么 CSS，哪些 display 值无法提升。

**操作步骤**（纯源码追踪）：
1. 假设用户写了 `#block[ #html.elem("img") ]`（语义上：一个 block 包着单个 img）。
2. `handle_block` 先用 `html_block_fragment` 把 body 转成子节点，得到（忽略内省 Tag 后）单个 `HtmlNode::Element(img)`。
3. `to_lone_element` 返回 `Some(&mut img_node)`（内核恰是单元素）。
4. 对它调 `make_block_level`：
   - `default = default_for(tag::img)` = `Some(Inline)`（查 [property.rs:189](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L189)）。
   - `Some(Inline)` 命中 `None | Some(Inline | InlineBlock)` 分支 → `mode = Some(Block)`。
   - `set_display(img_node, Some(Block))` → 往 `img.css` 写入 `display: block`。
   - 返回 `Ok(())`。
5. `is_ok()` 为真，`converter.extend(children)` 直接展开，**不外包 `<div>`**。

**需要观察的现象**：最终这个 `<img>` 的 `css` 字段里会有一条 `display: block`，经 `resolve_inline_styles`（u4-l3）后输出为 `<img style="display: block">`，而非 `<div><img></div>`。

**预期结果**（基于源码逻辑推演）：单个 `<img>` 在 block 里被**就地提升**，得到 `display: block`。若要亲眼确认，可在本地用 `typst compile --format html` 编译上述文档查看输出 HTML（**待本地验证**实际输出字符串）。

**第二问：哪些 display 值无法被提升？** 对照 `make_block_level` 的两个 `match`，能成功（返回 `Ok`）的 default 只有这些：
- 保持不动：`None`、`Block`、`Table`、`ListItem`、`Contents`、`BlockMath`；
- 提升为 block：`None`（未知标签）、`Inline`、`InlineBlock`；
- 提升为 block math：`InlineMath`。

**其余全部返回 `Err(Unblockable)`**，会被外包一层 `<div>`。其中实际会作为某些标签默认值、从而真的触发外包的有：`Ruby`（`<ruby>`）、`RubyText`（`<rt>`）、`InlineTable`（MathML `<mtable>`）、`TableRow`（`<tr>`、MathML `<mtr>`）、`TableCell`（`<td>`/`<th>`、MathML `<mtd>`），以及各种 `TableHeaderGroup`/`TableRowGroup`/`TableFooterGroup`/`TableColumn`/`TableColumnGroup`/`TableCaption`/`InlineFlex`/`InlineGrid` 等。一句话：**除了「行内/行内块/行内数学」能就地提升、「块级/表格/列表项/contents」可保持不动外，其余 display 一律无法就地提升，必须包 `<div>`。**

#### 4.5.5 小练习与答案

**练习 1**：为什么 `handle_block` 里要写 `make_block_level(node).is_ok()`，而 `handle_box` 里 `make_inline_level(node)` 不需要检查返回值？

**参考答案**：因为 `make_block_level` 返回 `Result<(), Unblockable>`——有些 display（如 `Ruby`、表格内部系列）无法安全改成 block，会返回 `Err`，`handle_block` 据此 fallback 到外包 `<div>`（[convert.rs:422-435](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L422-L435)）。而 `make_inline_level` 返回 `()`，行内方向总是可行（最坏只是清掉 display 恢复默认），所以 `handle_box` 无需检查（[convert.rs:353-356](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L353-L356)）。

**练习 2**：一个 `<math display="block">` 公式被放进 Typst `block` 时，`make_block_level` 会给它写 display 吗？

**参考答案**：不会写。因为 math 元素带 `display="block"` 属性时，`default` 被特殊判定为 `Some(BlockMath)`（[convert.rs:448-456](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L448-L456)），而 `BlockMath` 在「保持不动」分支里（[convert.rs:465-472](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L465-L472)），`mode = None`，`set_display` 不写入任何东西——它本来就是块级数学，无需提升。

**练习 3**：反过来，一个不带 `display="block"` 属性的 `<math>`（默认 `InlineMath`）被放进 `block` 时会怎样？

**参考答案**：`default = default_for(math) = Some(InlineMath)`，命中 `Some(InlineMath) => Some(BlockMath)` 分支（[convert.rs:478](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L478)），`set_display` 写入 `display: block math`（即 `BlockMath.as_str()`）。这正是「行内数学升为块级数学」的提升。对应的反向操作在 `make_inline_level`：带 `display="block"` 属性的 math 被压回 `InlineMath`（[convert.rs:382-389](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L382-L389)）。

---

## 5. 综合实践

把本讲的知识串起来，做一个完整的「提升决策」推演。

**任务**：下面是一段（语义化的）Typst 源码，假设它编译并经 `handle_html_elem` 转换后，内层 body 产出的「剥掉内省 Tag 后」的子节点序列已给出。请逐个预测 typst-html 最终是「就地改 display」还是「外包一层容器」，以及写出/不写 display 的具体值。

```typst
#block[
  #html.elem("img")            // 情况 A：block 包单个 <img>
]

#block[
  #html.elem("ruby")[...]      // 情况 B：block 包单个 <ruby>
  #html.elem("rt")[...]        // （假设与上同一 block 内另起一个，记作情况 B'）
]

#box[
  #html.elem("div")[...]       // 情况 C：box 包单个 <div>（display 被块级设过）
]
```

**推演步骤**（对照源码填写）：

| 情况 | `to_lone_element` | `default` 查表结果 | 提升函数走向 | 最终输出形态 |
|------|-------------------|--------------------|--------------|--------------|
| A：block 包 `<img>` | `Some`（单元素） | `Some(Inline)` | `make_block_level` → `Some(Block)` | 就地：`<img style="display: block">` |
| B：block 包 `<ruby>` | `Some`（单元素） | `Some(Ruby)` | `make_block_level` → `Err(Unblockable)` | 外包：`<div><ruby>...</ruby></div>` |
| C：box 包 `<div>` | `Some`（单元素） | `Some(Block)`（但 box 上下文） | `make_inline_level` → `None`（清 display） | 就地：`<div>`（恢复为默认 block，但位于行内流中） |

**需要你做的**：
1. 用 [property.rs:62-224](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L62-L224) 的 `default_for` 逐个查 `img`、`ruby`、`div` 的默认值，核对你填的「default」列。
2. 用 [convert.rs:444-486](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L444-L486)（`make_block_level`）和 [convert.rs:381-391](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L381-L391)（`make_inline_level`）核对你填的「走向」列。
3. 思考：情况 B 为什么必须外包 `<div>` 而不能写成 `<ruby style="display: block">`？（提示：把 `Ruby` 改成 `Block` 会破坏 ruby 注音的排版语义，typst-html 选择保留它并外包。）

**预期结果**：你会完整复现 typst-html 的「提升决策表」，并理解它的设计原则——**能就地改 display 的就改（`Inline`/`InlineBlock`/`InlineMath`），不能改的就外包一层中性容器，绝不破坏语义化的 display 值。**

> 若想在本地亲眼看到输出 HTML，可用 `typst compile --format html input.typ output.html`（具体命令以本地 typst 版本为准，**待本地验证**）。本实践的结论完全可由源码追踪得出，不依赖运行。

## 6. 本讲小结

- `Display` 是 CSS `display` 属性的类型化枚举（[property.rs:14-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L14-L57)），`as_str()` 负责序列化（注意 `inline math`/`block math` 带空格）。
- `default_for(tag)` 是 HTML 规范 §15 UA 样式表的 Rust 翻译（[property.rs:62-224](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L62-L224)），未知标签返回 `None`；它直接驱动后续所有提升决策。
- `set_display` 只写编译器的 `css` 字段（`Element`/`Frame`），不碰用户 `attrs`（[convert.rs:500-510](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L500-L510)），最终由 `resolve_inline_styles` 合并进 `style` 属性。
- `to_lone_element` 用 `split_prefix_suffix` 剥掉首尾内省 Tag，判定内核是否恰为单元素/单 frame（[convert.rs:492-497](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L492-L497)），这是「就地提升」的前提。
- `make_block_level` 把行内（`Inline`/`InlineBlock`/未知）提升为 `Block`、行内数学提升为 `BlockMath`，对 `Ruby`/表格内部等无法提升的 display 返回 `Err(Unblockable)`（[convert.rs:444-486](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L444-L486)），由 `handle_block` 兜底外包 `<div>`。
- 数学公式有特殊处理：依据 `<math>` 的 **HTML `display` 属性**（而非 CSS）区分 `BlockMath`/`InlineMath`，块级与行内两个方向都会据此调整（[convert.rs:382-389](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L382-L389)、[convert.rs:448-478](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L448-L478)）。

## 7. 下一步学习建议

本讲讲清了 display **写在哪里、怎么决策**，但还没讲它**怎么变成最终 HTML 里的 `style` 属性**。这正好是下一讲的主题：

- **u4-l3「CSS 属性系统与内联样式解析」**：精读 `css/encode.rs` 的 `Properties`/`PropertiesBuilder`（本讲反复用到的 `css.push`/`css.remove` 的真正实现）和 `css/resolve.rs` 的 `resolve_inline_styles`（把 `css` 字段合并进 `style` 属性的遍历器）。读完你能补上「`set_display` 写的 display 是如何出现在最终 `<img style="display: block">` 里」的最后一环。
- **u4-l4「Typst 类型到 CSS 类型的转换」**：讲 `ToCSS`/`CssWriter`，即 `Display::as_str()` 之外更复杂的 CSS 值（长度、颜色、`calc()`）如何序列化。
- 若对数学公式的完整链路感兴趣，可跳读 **u5-l5「数学公式到 MathML 的转换」**，看 `<math display="block">` 这一属性是如何在 `rules.rs`/`mathml.rs` 里被设置的。

建议同时回看 **u3-l3** 的 `handle` 调度链，把本讲的 `handle_block`/`handle_box`/`FrameElem` 分支放回 `convert_to_nodes` 的整体语境中，你会更清楚提升逻辑在整个转换器里的位置。
