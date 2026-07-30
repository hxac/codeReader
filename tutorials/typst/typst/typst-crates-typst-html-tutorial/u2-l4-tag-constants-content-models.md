# 预定义标签常量与内容模型分类

## 1. 本讲目标

本讲承接 [u2-l2](u2-l2-htmltag-interning.md)（你已经知道 `HtmlTag` 是一个被驻留的、可 `Copy` 的标签句柄），把目光从「单个标签名如何构造」抬到「`tag.rs` 这个文件整体在做什么」。

读完本讲，你应该能够：

1. 说清 `tag.rs` 的两层内容：一层是上百个**预定义标签常量**，一层是按 HTML 规范分组的**内容模型分类函数**。
2. 区分 `is_void` / `is_raw` / `is_escapable_raw` / `is_foreign` 这一组「**语法分类**」，并说出它们各自在 `encode.rs`、`convert.rs`、`typed.rs` 里触发了哪些不同处理。
3. 理解 `is_flow_content` / `is_phrasing_content` 等「**内容模型分类**」对应 HTML 规范的哪一节，以及它们与 `Display::default_for` 的协作。
4. 说清 `should_group_into_pars` 与 `is_whitespace_collapsing` 表达的段落分组策略，并诚实地区分「当前 HEAD 中真正被调用的分类函数」与「定义好、留给外部或未来使用的分类函数」。

## 2. 前置知识

- **HTML 内容模型（content model）**：HTML 规范给每种元素规定了「它能出现在哪里」「它能包含什么」。最常见的几大类是 flow content（流式内容）、phrasing content（短语内容）、heading content（标题内容）、sectioning content（分节内容）、metadata content（元数据内容）、embedded content（嵌入内容）、interactive content（交互内容）、palpable content（可感知内容）。一个元素可以同时属于好几类，例如 `<a>` 既是 phrasing 又是 interactive。
- **void 元素**：HTML 里一类没有结束标签、不能有子节点的元素，如 `<img>`、`<br>`、`<meta>`。
- **raw text / escapable raw text 元素**：`<script>`、`<style>` 的内容是「原始文本」（浏览器不解析其中的标签，只到对应的 `</script>` 为止）；`<textarea>`、`<title>` 的内容是「可转义的原始文本」（仍然不解析子标签，但会处理 `&amp;` 之类的实体）。
- **replaced 元素**：渲染内容不由 CSS 决定、而由元素自身（或外部资源）决定的元素，如 `<img>`、`<iframe>`、`<input>`。它们在「空白折叠」时表现为一个不可拆的整体。
- **`Display::default_for`**：`property.rs` 中按 HTML 规范 §15 的用户代理（UA）样式表给出的每个标签默认 `display` 值（如 `div` 默认 `block`、`span` 默认 `inline`、`script` 默认 `none`）。它在 `tag.rs` 里被复用来派生几个「非标准」分类。
- **驻留与 `HtmlTag::constant`**：[u2-l2](u2-l2-htmltag-interning.md) 讲过，`HtmlTag::constant` 是编译期 `const fn`，失败则 `panic`，用于把标准标签预定义成零成本常量。本讲正是它的集中应用。

## 3. 本讲源码地图

| 文件 | 在本讲中的作用 |
| --- | --- |
| `src/tag.rs` | **本讲主角**。两大块：预定义标签常量表 + 内容模型分类函数（`is_*` / `should_group_into_pars`），以及 `mathml` 子模块。 |
| `src/property.rs` | 提供 `Display::default_for`，被 `is_whitespace_collapsing` 与 `should_group_into_pars` 复用，是把「内容模型」与「渲染层级」粘在一起的桥梁。 |
| `src/convert.rs` | 分类函数的**下游消费方之一**：用 `is_raw`/`is_escapable_raw` 决定空白模式，用 `is_whitespace_collapsing`/`is_replaced` 驱动空白保护状态机。 |
| `src/encode.rs` | 另一个下游消费方：用 `is_void`/`is_foreign_self_closing` 决定自闭合，用 `is_raw`/`is_escapable_raw` 选编码路径，用 `is_metadata_content` 决定美化换行。 |
| `src/typed.rs` | 用 `is_void`/`is_raw` 决定类型化构造函数（如 `html.div`）是否生成 `body` 参数。 |

## 4. 核心概念与源码讲解

### 4.1 常量表基石：预定义标签常量

#### 4.1.1 概念说明

`tag.rs` 最显眼的第一部分，是上百行长得一模一样的常量：

```rust
#![allow(non_upper_case_globals)]
#![allow(dead_code)]
...
pub const a: HtmlTag = HtmlTag::constant("a");
...
pub const img: HtmlTag = HtmlTag::constant("img");
...
pub const script: HtmlTag = HtmlTag::constant("script");
```

两点先讲清楚：

- 文件顶部 [`#![allow(non_upper_case_globals)]`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L3) 故意放宽了「常量名必须大写」的 lint——因为这里就是要用小写的 HTML 标签名（`img`、`script`）做常量名，让代码里 `tag::img` 读起来和写 HTML 一样自然。
- [`#![allow(dead_code)]`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L4) 是本讲的一条**关键线索**：它说明 `tag.rs` 里有相当一部分常量和函数**并不一定被本 crate 内部调用**。它们是按 HTML 规范忠实整理出来的「参考表 + 公共 API」，对外（`tag` 是 [`pub mod tag`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L5)）暴露，也方便现在或未来的代码取用。本讲 4.4 节会明确列出「哪些分类函数当前真正被调用、哪些只是定义好待用」。

每个常量都通过 [`HtmlTag::constant("img")`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L59) 在编译期驻留（见 [u2-l2](u2-l2-htmltag-interning.md)），代价为零、可 `Copy`、比较时是整数比较。除 HTML 标签外，文件末尾还有一个 `mathml` 子模块，用同样手法预定义了约三十个 MathML 标签（`mfrac`、`msqrt` 等）。

#### 4.1.2 核心流程

拿到一个 `HtmlTag` 之后，`tag.rs` 提供两种「问询」：

1. **语法问询**：这个标签属于哪种语法类别？void？raw？escapable raw？foreign？（见 4.2）
2. **内容模型问询**：这个标签属于哪些内容模型大类？flow？phrasing？…（见 4.3）

这两组分类函数全部是 `match` 表达式 + 常量列表，纯查表、无副作用、可 `const` 友好，是连接「DOM 数据模型」与「编码/转换行为」的查表中枢。

### 4.2 语法分类：is_void / is_raw / is_escapable_raw / is_foreign

#### 4.2.1 概念说明

这一组函数对应 [HTML 规范 §13.1.2 Elements](https://html.spec.whatwg.org/multipage/syntax.html#elements-2)，按「**语法**」而非「内容」给元素分类。注释里直接写明了出处：

```rust
// HTML spec § 13.1.2 Elements
```

四个函数职责如下：

| 函数 | 判定什么 | 典型成员 |
| --- | --- | --- |
| `is_void` | 是否为 void（无子节点、无结束标签） | `area` `base` `br` `col` `embed` `hr` `img` `input` `link` `meta` `source` `track` `wbr` |
| `is_raw` | 是否为「原始文本」元素 | `script`、`style` |
| `is_escapable_raw` | 是否为「可转义的原始文本」元素 | `textarea`、`title` |
| `is_foreign` | 是否为外来命名空间元素（当前即 MathML） | 委托给 `mathml::is_mathml` |

另外还有一个 `is_foreign_self_closing`，专门标记 MathML 里自身就需要自闭合的 `mprescripts`、`mspace`。

#### 4.2.2 核心流程

这组分类的下游影响分三处：

1. **`typed.rs`（构造函数生成）**：`html.img` 这类类型化函数在生成参数表时，会用 `is_void(tag)` 决定**要不要生成 `body` 参数**——void 元素没有 body。再用 `is_raw(tag)` 决定 body 内容如何 cast。
2. **`encode.rs`（序列化）**：`is_void` 与 `is_foreign_self_closing` 决定标签是否自闭合（`/>` 还是 `>`，以及要不要写结束标签）；`is_raw` 与 `is_escapable_raw` 决定走 `write_raw` 还是 `write_escapable_raw`，二者对子文本的转义策略完全不同。
3. **`convert.rs`（内容转换）**：`is_raw` 与 `is_escapable_raw` 会让子内容的空白模式强制变成 `Whitespace::Pre`，因为这些元素的文本不应该被折叠。

#### 4.2.3 源码精读

先看四个语法分类函数本身：

`is_void`——13 个成员的纯查表（[`tag.rs:125-142`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L125-L142)）：

```rust
pub fn is_void(tag: HtmlTag) -> bool {
    matches!(tag, self::area | self::base | self::br | self::col
        | self::embed | self::hr | self::img | self::input
        | self::link | self::meta | self::source | self::track | self::wbr)
}
```

`is_raw` / `is_escapable_raw` 只有两三个成员（[`tag.rs:145-152`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L145-L152)）：

```rust
pub fn is_raw(tag: HtmlTag) -> bool { matches!(tag, self::script | self::style) }
pub fn is_escapable_raw(tag: HtmlTag) -> bool { matches!(tag, self::textarea | self::title) }
```

`is_foreign` 委托给 MathML 子模块（[`tag.rs:158-160`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L158-L160)）；`is_foreign_self_closing` 标记两个 MathML 自闭合标签（[`tag.rs:163-165`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L163-L165)）。

下游消费：**`typed.rs` 决定是否生成 body 参数**（[`typed.rs:91-92`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L91-L92)）：

```rust
if !tag::is_void(tag) {
    let raw = tag::is_raw(tag);
    // ...据此决定 body 参数与 cast 方式
}
```

**`encode.rs` 决定自闭合与编码路径**（[`encode.rs:142-160`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L142-L160)）——这是语法分类影响**最集中**的地方：

```rust
if tag::is_void(element.tag) || tag::is_foreign_self_closing(element.tag) {
    if !element.children.is_empty() {
        bail!(element.span, "HTML void elements must not have children");
    }
    return Ok(());
}
...
if tag::is_raw(element.tag) {
    write_raw(w, element)?;
} else if tag::is_escapable_raw(element.tag) {
    write_escapable_raw(w, element)?;
} else if !element.children.is_empty() {
    write_children(w, element)?;
}
```

注意 `bail!`：如果有人给 void 元素塞了子节点，编码阶段会直接报错——这正是 `is_void` 在「保证输出合法 HTML」上承担的护栏职责。

**`convert.rs` 决定空白模式**（[`convert.rs:177-185`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L177-L185)）：

```rust
let whitespace = if converter.whitespace == Whitespace::Pre
    || elem.tag == tag::pre
    || tag::is_raw(elem.tag)
    || tag::is_escapable_raw(elem.tag)
{
    Whitespace::Pre
} else {
    Whitespace::Normal
};
```

即 `script/style/textarea/title`（以及 `<pre>`）内部的空白一律按原样保留，不参与折叠。

#### 4.2.4 代码实践

**实践目标**：亲手验证 void 与 raw 标签在编码阶段的特殊对待。

**操作步骤（源码阅读型 + 可选运行）**：

1. 打开 [`tag.rs:125-152`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L125-L152)，确认 `img` 在 `is_void` 列表里、`script` 在 `is_raw` 列表里。
2. 在 Typst 源码里写一段：

   ```typst
   #html.elem("img")[不该有的子内容]
   ```

   然后用 `typst compile --format html` 导出。

3. 对照 [`encode.rs:142-146`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L142-L146) 预测会发生什么。

**需要观察的现象 / 预期结果**：因为 `is_void(img)` 为真，而 void 元素被强制要求 `children.is_empty()`，编译应在 HTML 编码阶段报错 `HTML void elements must not have children`。如果运行结果与此不符，记下实际报错文案以待本地确认。

> ⚠️ 待本地验证：具体报错文案与触发层级请以本地实际编译输出为准；本实践主要目的是建立「`is_void` → 编码护栏」的因果链。

#### 4.2.5 小练习与答案

**练习 1**：`<title>` 在 `is_raw` 还是 `is_escapable_raw` 里？为什么 `<title>` 和 `<script>` 不能共用同一条编码路径？

**参考答案**：`title` 在 `is_escapable_raw`（与 `textarea` 同列），见 [`tag.rs:150-152`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L150-L152)。区别在于「可转义」：`<title>`/`<textarea>` 的内容虽然不解析子标签，但仍会处理 `&amp;` 之类的字符实体；`<script>`/`<style>` 则连实体都不处理，是纯原始文本。因此在 `encode.rs` 里一个走 `write_escapable_raw`、一个走 `write_raw`，不能合并。

**练习 2**：`is_foreign` 与 `is_foreign_self_closing` 有什么关系？后者是前者的子集吗？

**参考答案**：`is_foreign` 判定「是否属于外来命名空间」（当前等价于 `is_mathml`）。`is_foreign_self_closing` 只列出 `mprescripts`、`mspace` 两个 MathML 标签，说明这俩元素在 MathML 语法里自身就需自闭合。所以后者是前者的真子集，二者在 `encode.rs:136,142` 被分别用于「要不要加 `/`」和「要不要提前 return」两件事。

### 4.3 内容模型分类：is_flow_content / is_phrasing_content 等

#### 4.3.1 概念说明

第二组函数对应 [HTML 规范 §3.2.5.2 Kinds of content](https://html.spec.whatwg.org/multipage/dom.html#kinds-of-content)，按「**能出现在哪、能包含什么**」给元素分类。文件里同样标了出处：

```rust
// HTML spec § 3.2.5.2 Kinds of content
```

这一组函数较多，彼此并不互斥——一个标签可以同时是 flow、phrasing、interactive：

| 函数 | 规范含义（简） |
| --- | --- |
| `is_metadata_content` | `<head>` 里那类元素：`base/link/meta/noscript/script/style/template/title` |
| `is_flow_content` | 几乎所有可在 `<body>` 内出现的元素（最大的一类） |
| `is_sectioning_content` | `article/aside/nav/section` |
| `is_heading_content` | `h1`..`h6`、`hgroup` |
| `is_phrasing_content` | 可出现在段落（`<p>`）里的元素，是 flow 的子集 |
| `is_embedded_content` | 引入外部资源：`audio/canvas/embed/iframe/img/math/object/picture/video` |
| `is_interactive_content` | 可交互：`a/audio/button/details/embed/iframe/img/input/label/select/textarea/video` |
| `is_palpable_content` | 「有实质内容」、不应为空的元素 |
| `is_script_supporting_element` | `script/template`，不参与内容渲染 |

#### 4.3.2 核心流程

这一组分类的**特点**是：大部分在当前 HEAD 中**并未被本 crate 内部直接调用**（这是 `#![allow(dead_code)]` 的直接体现，4.4 节给出完整清单）。它们更像是「为正确性而忠实抄录规范 + 作为公共 API」的存在。少数被真正使用的入口：

- `is_metadata_content`：在 `encode.rs` 的 `wants_pretty_around` 里决定 `<title>`/`<style>` 等是否要在美化输出时换行。
- `is_phrasing_content`：在 `tag.rs` 内部被 `should_group_into_pars` 复用（见 4.4）。
- `mathml::is_mathml` / `mathml::is_token`：被多处复用，分别判定「是否 MathML」「是否 MathML 记号元素」，后者影响美化打印。

注意 `is_flow_content` 和 `is_phrasing_content` 的成员高度重叠——phrasing 是 flow 的子集，且二者都包含了 `mathml::math`。这种「列表式分类」忠实于规范，但代价是单个元素会出现在多张表里。

#### 4.3.3 源码精读

`is_flow_content` 是规模最大的一个，近百个成员（节选自 [`tag.rs:185-275`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L185-L275)）：

```rust
pub fn is_flow_content(tag: HtmlTag) -> bool {
    matches!(tag, self::a | self::abbr | self::address | ... 
        | self::mathml::math | ... | self::ul | self::var | self::video | self::wbr)
}
```

`is_phrasing_content`（[`tag.rs:291-350`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L291-L350)）规模也接近，但它**不含** `div/p/blockquote/h1..h6/section/ul/ol/table` 这类块级或分节元素——这正是「能放进 `<p>`」这个定义的直接体现。

`is_metadata_content`（[`tag.rs:170-182`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L170-L182)）唯一被内部用到，下游在 `encode.rs` 的 `wants_pretty_around`（[`encode.rs:362`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L362)）：

```rust
t if tag::is_metadata_content(t) => true,
```

即元数据元素在美化模式下前后都换行。

#### 4.3.4 代码实践

**实践目标**：用查表结果反推每个标签「属于哪些内容模型」。

**操作步骤**：

1. 选定三个标签：`img`、`script`、`a`。
2. 逐一在以下函数列表里检索：`is_flow_content`、`is_phrasing_content`、`is_embedded_content`、`is_interactive_content`、`is_metadata_content`、`is_palpable_content`、`is_script_supporting_element`。
3. 把结果填进下表（答案见 4.3.5）。

**预期结果**：`img` 会命中很多类；`script` 命中的类较少且行为特殊；`a` 既是 phrasing 又是 interactive。

#### 4.3.5 小练习与答案

**练习 1**：完成下表（✓ 表示属于该内容模型）。

| 标签 | flow | phrasing | embedded | interactive | metadata |
| --- | --- | --- | --- | --- | --- |
| `img` | ? | ? | ? | ? | ? |
| `script` | ? | ? | ? | ? | ? |
| `a` | ? | ? | ? | ? | ? |

**参考答案**：

| 标签 | flow | phrasing | embedded | interactive | metadata |
| --- | --- | --- | --- | --- | --- |
| `img` | ✓（L229） | ✓（L314） | ✓（L360） | ✓（L378） | ✗ |
| `script` | ✓（L255） | ✓（L334） | ✗ | ✗ | ✓（L177） |
| `a` | ✓（L188） | ✓（L294） | ✗ | ✓（L372） | ✗ |

（行号对应 [`tag.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L185-L275) 各函数中的成员位置。）

**练习 2**：为什么 `script` 同时是 flow content 又是 phrasing content，却在浏览器里「看不见」？

**参考答案**：因为它的 `display` 默认值是 `None`（见 [`property.rs:73`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L73)）。「内容模型」回答的是「**能出现在哪**」（语法/结构层面），而「看不看得见」由 `display`（渲染层面）决定。这两者是正交的：`script` 在结构上允许出现在 flow/phrasing 位置，在渲染上却被 UA 样式表藏起来。这条区分正是 4.4 节 `should_group_into_pars` 要把 `display: none` 的元素**排除**出段落分组的原因。

### 4.4 段落分组与空白保护：should_group_into_pars 与 is_whitespace_collapsing

#### 4.4.1 概念说明

`tag.rs` 在规范分类之外，还定义了几个「**非标准**」分类函数，文件里明确标注：

```rust
// Non-standard sets.
```

它们不复述规范，而是为 typst-html 自己的转换逻辑服务，并且都**复用了前面的分类 + `Display::default_for`**。其中最关键的两个是：

- `is_whitespace_collapsing(tag)`：在「正常 UA 样式」假设下，该元素**相邻的单个空格会被浏览器折叠**吗？用于空白保护状态机。
- `should_group_into_pars(tag)`：当 Typst 强制进行段落分组时（顶层、或与原生 `block`/`parbreak` 同流），这种 HTML 元素是否适合被收进 `<p>` 里？

`should_group_into_pars` 的文档注释特别值得读，因为它把「为什么最大集合恰好是 phrasing content」「为什么排除 hidden content」「为什么对透明内容模型（`<a>/<ins>/<del>/...`）当前做无条件分组」都讲了一遍——是 typst-html 在「HTML 语义」与「Typst 的段落观」之间做权衡的精彩记录。

#### 4.4.2 核心流程

两个函数都把决策建立在 `Display::default_for` 之上：

- `is_whitespace_collapsing` ≈ 「`display` 默认是 `block`」或「是 `<br>`」。
- `should_group_into_pars` ≈ 「是 phrasing content」且「`display` 不是 `none`」。

`is_whitespace_collapsing` 当前**确实被调用**：在 `convert.rs` 的空白保护状态机 `Protector` 里，决定遇到某个元素时是把状态推向「会折叠」还是「支撑空白不折叠」。`should_group_into_pars` 当前**未被本 crate 内部调用**（见 4.4.3 的诚实清单），它是 `pub` API 的一部分，文档注释阐明了段落分组策略，并复用 `is_phrasing_content`。

`Protector` 的三态与 `is_whitespace_collapsing` / `is_replaced` 的配合逻辑可以概括为：

```
遇到元素 E：
  若 is_whitespace_collapsing(E.tag)  → 进入「会折叠」语义，前导单空格需保护
  否则若 is_replaced(E.tag)            → 视作不可拆整体，进入「支撑」态（不折叠相邻空格）
  否则（普通 inline 元素）             → 递归进入其子节点继续判定
```

#### 4.4.3 源码精读

`is_whitespace_collapsing`（[`tag.rs:498-502`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L498-L502)）——直接委托给 `Display::default_for`：

```rust
pub fn is_whitespace_collapsing(tag: HtmlTag) -> bool {
    // TODO: Reconsider this check. What about e.g. tables?
    property::Display::default_for(tag) == Some(property::Display::Block)
        || tag == self::br
}
```

`TODO` 注释表明作者对「表格等」是否应被算作折叠上下文仍有保留——这是真实工程里「查表函数也会带 TODO」的一个好例子。

`should_group_into_pars`（函数体 [`tag.rs:543-546`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L543-L546)，完整注释 [`tag.rs:504-546`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L504-L546)）：

```rust
pub fn should_group_into_pars(tag: HtmlTag) -> bool {
    is_phrasing_content(tag)
        && property::Display::default_for(tag) != Some(property::Display::None)
}
```

读法：能进 `<p>` 的前提是「phrasing content」（因为 `<p>` 的内容模型就是 phrasing），再用 `display != None` 把 `<script>` 这类「结构上算 phrasing、渲染上被隐藏」的元素排除掉——正好呼应 4.3.5 练习 2。

下游真实消费：`convert.rs` 的 `Protector::visit_nodes`（[`convert.rs:635-647`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L635-L647)）：

```rust
HtmlNode::Element(element) => {
    if tag::is_whitespace_collapsing(element.tag) {
        self.collapsing();
    } else if tag::is_replaced(element.tag) {
        self.supportive();
    } else if !element.pre_span {
        self.visit_nodes(&mut element.children);
    }
}
HtmlNode::Frame(_) => self.supportive(),
```

`is_replaced`（[`tag.rs:480-492`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L480-L492)）在这里的作用是：替换元素（`img/iframe/input/...`）像「一个不可拆的大字符」，它本身不折叠相邻空格，所以把状态机推到「支撑」态。

**诚实清单：当前 HEAD 中分类函数的调用情况**

为了让结论可复现，下表给出本讲涉及的所有 `is_*` / `should_*` 在**本 crate 内部**的调用情况（用 `tag::<fn>(` 在 `src/` 内检索，排除 `tag.rs` 自身定义行）：

| 函数 | 内部是否被调用 | 主要调用点 |
| --- | --- | --- |
| `is_void` | ✓ | `typed.rs:91,148`、`encode.rs:142` |
| `is_raw` | ✓ | `typed.rs:92,149`、`encode.rs:154`、`convert.rs:179` |
| `is_escapable_raw` | ✓ | `encode.rs:156`、`convert.rs:180` |
| `is_foreign_self_closing` | ✓ | `encode.rs:136,142` |
| `is_metadata_content` | ✓ | `encode.rs:362` |
| `is_whitespace_collapsing` | ✓ | `convert.rs:636` |
| `is_replaced` | ✓ | `convert.rs:638` |
| `mathml::is_mathml` | ✓ | 多处（如 `encode.rs:340,360`） |
| `mathml::is_token` | ✓ | `encode.rs:340` |
| `is_foreign` | ✗（仅定义） | —— |
| `is_flow_content` | ✗（仅定义） | —— |
| `is_sectioning_content` | ✗（仅定义） | —— |
| `is_heading_content` | ✗（仅定义） | —— |
| `is_phrasing_content` | 仅被 `should_group_into_pars` 内部复用 | `tag.rs:544` |
| `is_embedded_content` | ✗（仅定义） | —— |
| `is_interactive_content` | ✗（仅定义） | —— |
| `is_palpable_content` | ✗（仅定义） | —— |
| `is_script_supporting_element` | ✗（仅定义） | —— |
| `should_group_into_pars` | ✗（仅定义） | —— |

这些「仅定义」的函数之所以保留，正是文件顶部 `#![allow(dead_code)]` 的用意：它们是面向外部消费者（`pub mod tag`）和对规范忠实记录的参考表，也是未来扩展（例如更聪明的段落分组）的现成基础。

#### 4.4.4 代码实践

**实践目标**：体会 `is_whitespace_collapsing` 与 `is_replaced` 如何共同决定一个空格「保不保护」。

**操作步骤（源码阅读型）**：

1. 阅读 [`convert.rs:597-648`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L597-L648) 的 `Protector` 状态机，理清 `Collapsing` / `Supportive` / `Space` 三态的迁移。
2. 假设有如下节点序列（伪 DOM）：`[Text(" "), Element(span), Text(" "), Element(img), Text(" ")]`。
3. 用 [`tag.rs:498-502`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L498-L502) 和 [`tag.rs:480-492`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L480-L492) 判断：`span`（`display: inline`）会走哪个分支？`img`（replaced）会走哪个分支？

**需要观察的现象 / 预期结果**：

- `span` 的 `display` 是 `Inline`，不是 `Block`，所以 `is_whitespace_collapsing(span)` 为 `false`；它也不是 replaced，于是进入「递归子节点」分支。
- `img` 的 `is_replaced` 为 `true`，于是调用 `self.supportive()`，把它当作「支撑空白」的整体。
- 据此推断：哪些 `Text(" ")` 会被 `protect_space` 包进 `white-space: pre-wrap` 的 span，哪些不会被保护。

> 详细的状态迁移语义（`Space(prev)`、`collapsing()` 的回退保护）可结合 [u4-l1（HTML 空白保护机制）](u4-l1-whitespace-protection.md) 进一步学习，本讲只聚焦 `tag.rs` 提供的判定输入。

#### 4.4.5 小练习与答案

**练习 1**：`should_group_into_pars` 的两个条件各自排除的是哪类元素？各举一例。

**参考答案**：第一个条件 `is_phrasing_content(tag)` 排除「块级/分节/标题」类元素（如 `div`、`section`、`h1`），因为它们不能放进 `<p>`；第二个条件 `display != None` 排除「结构上是 phrasing 但渲染隐藏」的元素（如 `script`）。两条件相乘，留下的是「能在段落里出现且确实可见」的元素。

**练习 2**：`is_whitespace_collapsing` 为什么要在 `Display::Block` 之外额外加一句 `|| tag == self::br`？

**参考答案**：因为 `<br>` 的默认 `display` 是 `Inline`（见 [`property.rs:178`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L178)），单靠 `default_for == Block` 抓不到它；但 `<br>` 在排版上会强制换行、其相邻空格的折叠行为更接近块级上下文，所以特判补上。

## 5. 综合实践

把本讲的三组分类串起来，做一次「标签体检」。

**任务**：任选一个 HTML 标签（建议选 `input` 或 `video` 这样跨多类的），完成一份「体检报告」，包含：

1. **语法分类**：用 4.2 的四个函数判定它是否 void / raw / escapable raw / foreign，并据此预测 `encode.rs` 会走哪条编码路径（自闭合？`write_raw`？普通 `write_children`？）。
2. **内容模型**：用 4.3 的各 `is_*` 列出它属于哪些内容模型。
3. **渲染与分组**：查 [`property.rs:62-224`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L62-L224) 的 `Display::default_for` 写出它的默认 `display`，再据此回答：`is_whitespace_collapsing(it)` 和 `should_group_into_pars(it)` 各返回什么？为什么？
4. **下游影响**：综合 1～3，说明在 `convert.rs` 的 `Protector` 遇到这个元素时，状态机会走哪个分支。

**示例（以 `input` 为例）**：

- 语法：`is_void(input)` 为 `true`（[`tag.rs:137`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L137)）→ 编码时自闭合、不允许子节点；非 raw / escapable / foreign。
- 内容模型：flow、phrasing、embedded? 否；interactive（[`tag.rs:379`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L379)）。
- 渲染：`Display::default_for(input)` = `InlineBlock`（[`property.rs:138`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/property.rs#L138)）。`is_whitespace_collapsing` = `false`（既非 Block 也非 br）；`is_replaced` = `true`（[`tag.rs:488`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L488)）。
- 下游：`Protector` 遇到它走 `is_replaced` 分支 → `self.supportive()`。

> ⚠️ 第 2 步中「`input` 是否 embedded content」请自行到 [`is_embedded_content`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L353-L366) 核对（提示：embedded 列表里没有 `input`）。

## 6. 本讲小结

- `tag.rs` 分两层：上百个**预定义标签常量**（`HtmlTag::constant`，含 HTML 与 `mathml` 子模块）+ 一组按规范整理的**分类函数**。
- **语法分类**（§13.1.2）：`is_void` / `is_raw` / `is_escapable_raw` / `is_foreign(_self_closing)`，直接驱动 `encode.rs` 的自闭合与编码路径、`convert.rs` 的空白模式、`typed.rs` 的 body 参数生成。
- **内容模型分类**（§3.2.5.2）：`is_flow_content` / `is_phrasing_content` / `is_metadata_content` 等彼此不互斥；当前只有 `is_metadata_content`、`is_phrasing_content` 等少数被内部使用，其余作为忠实于规范的公共 API 保留（`#![allow(dead_code)]`）。
- **非标准分类**（`is_whitespace_collapsing` / `should_group_into_pars` / `is_replaced`）建立在 `Display::default_for` 之上，把「内容模型」与「渲染层级」粘合起来；前两者分别服务空白保护与段落分组策略。
- 必须区分「**规范说它是哪类**」（内容模型）与「**浏览器怎么渲染它**」（`display`）——二者正交，`should_group_into_pars` 排除 `script` 正是用了这条区分。
- 阅读查表函数时，**用 `tag::<fn>(` 在 `src/` 内检索**才能判断它当前是否真的驱动逻辑，避免把「定义好待用」误当成「正在生效」。

## 7. 下一步学习建议

- 想看 `is_void` / `is_raw` 如何影响**最终字符串**，进入 [u5-l1（DOM 到 HTML 字符串的编码）](u5-l1-dom-to-html-encoding.md)，精读 `encode.rs` 的 `write_element` 与 `write_raw`/`write_escapable_raw`。
- 想弄清 `is_whitespace_collapsing` / `is_replaced` 喂给的那个状态机到底怎么保护空格，进入 [u4-l1（HTML 空白保护机制）](u4-l1-whitespace-protection.md)，精读 `convert.rs` 的 `Protector` 与 `pre_wrap`。
- 想了解 `Display::default_for` 之外 `Display` 如何被用来做块级/行内提升，进入 [u4-l2（display 属性与块级/行内提升）](u4-l2-display-block-inline-promotion.md)。
- 如果你对 `html.div` 这类**类型化构造函数**如何根据 `is_void` 自动决定参数表感兴趣，可以先去 [u2-l5（类型化 HTML API 的生成机制）](u2-l5-typed-html-api-generation.md)。
