# 用户侧 API：html.elem 与 html.frame

## 1. 本讲目标

在前三讲里，我们认识了 typst-html 的定位（[u1-l1](u1-l1-project-overview.md)）、模块组织（[u1-l2](u1-l2-module-structure.md)）和完整导出调用链（[u1-l3](u1-l3-export-pipeline-and-cli.md)）。那些讲义都站在「编译器内部」的视角，本讲我们换一个视角，站到**使用者**这一边。

Typst 在做 HTML 导出时，会把绝大多数内置元素（标题、列表、表格……）自动翻译成对应的 HTML 标签。但有时候你想自己掌控输出哪些标签——比如写博客时把每篇文章包进 `<article>`，或者把一段需要精确定位的内容原样「截图」嵌进网页。为此，typst-html 暴露了**两个用户侧原生元素**。

学完本讲，你应当能够：

- 学会用 `html.elem` 手写任意 HTML 元素及其属性。
- 理解 `HtmlElem` 的 `tag` / `attrs` / `body` / `role` 等字段各自的含义与用途。
- 学会用 `with_attr` / `with_optional_attr` 在 Rust 构造期给元素添加属性。
- 了解 `html.frame` 的用途与语义，明白它为什么要把内容渲染成内联 SVG。

---

## 2. 前置知识

在开始之前，你需要具备以下基础概念。如果你已经熟悉，可以跳过本节。

**Typst 侧：**

- **函数调用与内容块**：Typst 里调用函数形如 `#func(arg)[主体]`，方括号 `[...]` 包起来的是「内容（content）」，可以再嵌套 Typst 标记（比如 `_斜体_`）。
- **命名参数与字典**：`attrs: (style: "color: red")` 表示给参数 `attrs` 传一个字典，键值对用冒号分隔。
- **原生元素（element）**：用 `#[elem]` 宏定义、注册进标准库模块的结构体，用户在 Typst 脚本里可以直接当函数调用。`html.elem` 和 `html.frame` 就是两个原生元素（参见 [u1-l1](u1-l1-project-overview.md)）。

**HTML 侧：**

- **元素、标签、属性**：`<div class="x">文本</div>` 里 `div` 是标签名，`class="x"` 是属性，`文本` 是子内容。
- **void 元素**：像 `<meta>`、`<img>`、`<br>` 这类**没有闭合标签、不能有子内容**的元素叫 void 元素。这点对 `html.elem` 很重要——给它们传 body 会被拒绝。
- **inline SVG**：把矢量图直接写进 HTML 的 `<svg>...</svg>`，浏览器无需额外请求文件就能渲染。

**承接前讲：**

本讲默认你已经知道：typst-html 把 Typst 文档先编译成 HTML DOM 树（`HtmlDocument`），再编码成字符串（[u1-l3](u1-l3-export-pipeline-and-cli.md)）；`module()` 是把 HTML 相关定义组装成标准库模块的入口（[u1-l1](u1-l1-project-overview.md)）。本讲要回答的问题是：**用户手里的两个原生元素，在 DOM 树里变成了什么？**

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs) | 定义 `module()`、`HtmlElem`、`FrameElem`，以及 `with_attr`/`with_optional_attr` 两个构造器方法。这是本讲的核心文件。 |
| [src/convert.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs) | `handle()` 调度器，负责把 `HtmlElem`/`FrameElem` 转成 DOM 节点。 |
| [src/dom.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs) | 定义 `HtmlFrame` 数据结构（`html.frame` 在 DOM 里的形态）。 |
| [src/encode.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs) | `write_frame()` 把 `HtmlFrame` 渲染成内联 SVG 字符串。 |
| [src/typed.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs) | 展示 `html.div`/`html.span` 等类型化构造函数本质上就是构造一个 `HtmlElem`。 |
| ../typst-cli/src/compile.rs | CLI 的 `export_html()`，告诉你如何把 `.typ` 编译成 HTML 文件。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **`html.elem` 与 `HtmlElem`**：手写任意 HTML 元素。
2. **`HtmlElem` 的字段**：`tag` / `attrs` / `body` / `role` 等各自含义。
3. **`with_attr` 与 `with_optional_attr`**：在 Rust 构造期加属性。
4. **`html.frame` 与 `FrameElem`**：把内容以 SVG 嵌入。

---

### 4.1 html.elem：手写任意 HTML 元素

#### 4.1.1 概念说明

Typst 的 HTML 导出虽然「智能」，但它只能把**已知的内置元素**映射成 HTML。当你想输出一个 Typst 本身不认识的标签（例如 `<article>`、`<details>`、自定义的 `<my-button>`），或者想精确控制某个元素的属性时，就需要一个「万能逃生舱」——`html.elem`。

`html.elem` 让你**直接说出要哪个标签、带哪些属性、装什么内容**。它的设计哲学是：Typst 只负责校验你写的东西是不是合法 HTML（标签名合法、void 元素没有 body），然后把内容体（body）里仍然可以继续用 Typst 标记。

> 一个关键点：`html.elem` 是**一切类型化构造函数的底层基础**。你后面会学到 `html.div(...)`、`html.span(...)` 这些更方便的函数，它们在内部其实都是构造一个 `HtmlElem`（见 4.1.3 的 typed.rs 佐证）。所以理解 `html.elem` 等于理解了整个用户 API 的根基。

#### 4.1.2 核心流程

当你在 Typst 脚本里写下 `#html.elem("div", attrs: (...))[...]` 后，它经历的简化流程是：

1. **构造**：`#[elem]` 宏生成的构造代码读取位置参数 `tag`（这里是 `"div"`）、命名参数 `attrs`、可选位置参数 `body`。
2. **校验标签**：把 `"div"` 经 `HtmlTag::intern` 做合法性校验（非空、字符合法、自定义元素需含连字符等），并做字符串驻留（intern）节省内存。
3. **打包**：生成一个 `Packed<HtmlElem>`，进入 Typst 的内容流（content stream）。
4. **转换**（在 convert.rs 的 `handle()` 里）：识别到这是一个 `HtmlElem`，调用 `handle_html_elem`，递归把 body 转成子节点，最终生成一个 `HtmlElement` DOM 节点。
5. **编码**：`HtmlElement` 被编码成 `<div ...>子节点</div>` 字符串。

可以把它画成一条单向链：

```
#html.elem("div", attrs:(style:"..."))[ ... ]
        │  (构造 + 校验)
        ▼
   Packed<HtmlElem>  ──进入内容流──▶  handle_html_elem()
        │                                     │ (递归转换 body)
        ▼                                     ▼
   DOM 节点 HtmlElement { tag:"div", attrs, children } ──▶ "<div ...>…</div>"
```

#### 4.1.3 源码精读

**① 定义与注册。** `html.elem` 由 `HtmlElem` 结构体定义，并被 `module()` 注册进标准库：

[src/lib.rs:33-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L33-L41) —— `module()` 创建 `html` 作用域，注册 `HtmlElem`、`FrameElem`，再调 `typed::define` 批量注册类型化函数：

```rust
pub fn module() -> Module {
    let mut html = Scope::deduplicating();
    html.start_category(Category::Html);
    html.define_elem::<HtmlElem>();
    html.define_elem::<FrameElem>();
    crate::typed::define(&mut html);
    Module::new("html", html)
}
```

**② 结构体定义。** 注意 `#[elem(name = "elem", ...)]`：它在脚本里暴露成 `html.elem`，`since = "0.13.0"` 说明从 Typst 0.13.0 起可用。文档注释里给了一个最经典的用例：

[src/lib.rs:64-104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L64-L104) —— `HtmlElem` 结构体本体：

```rust
#[elem(name = "elem", since = "0.13.0")]
pub struct HtmlElem {
    #[required]
    pub tag: HtmlTag,
    #[fold]
    pub attrs: HtmlAttrs,
    #[internal]
    #[parse(Some(css::Properties::default()))]
    pub css: css::Properties,
    #[positional]
    pub body: Option<Content>,
    #[internal]
    #[synthesized]
    pub parent: Location,
    #[internal]
    #[ghost]
    pub role: Option<EcoString>,
}
```

> 文档注释中的示例（[src/lib.rs:59-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L59-L63)）就是你在 Typst 里写 `html.elem` 的标准姿势：

```typ
#html.elem("div", attrs: (style: "background: aqua"))[
  A div with _Typst content_ inside!
]
```

**③ 校验与「自定义标签」语义。** `tag` 字段是 `HtmlTag` 类型。它在被赋值时会通过 `cast!` 触发 `HtmlTag::intern`，做一系列 HTML 合法性校验。其中最有意思的是：**标签名里只要出现连字符 `-`，就被当作「自定义元素（custom element）」**，需要满足更严格规则（小写字母开头、不含大写字母、不得是 SVG/MathML 保留名）。详见 [src/dom.rs:255-310](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L255-L310)。这条规则决定了你能不能写出 `<my-widget>` 这样的标签。

**④ 类型化函数是 HtmlElem 的包装。** 想确认 `html.div` 等和 `html.elem` 是一回事？看 typed.rs 的 `construct()`：它先建好属性列表，最后一句就是 `let mut elem = HtmlElem::new(tag);`：

[src/typed.rs:142-146](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L142-L146)：

```rust
let tag = HtmlTag::constant(element.name);
let mut elem = HtmlElem::new(tag);
if !attrs.0.is_empty() {
    elem.attrs.set(attrs);
}
```

也就是说，`#html.div(class: "x")[内容]` 在底层等价于 `#html.elem("div", attrs: (class: "x"))[内容]`，只是 `html.div` 帮你做了标签名校验、并给每个属性做了类型检查。

#### 4.1.4 代码实践

**实践目标**：亲手用 `html.elem` 写一个带属性的元素，编译为 HTML，观察输出。

**操作步骤**：

1. 新建一个文件 `hello.typ`，内容如下（**示例代码**）：

```typ
#html.elem("div", attrs: (style: "background: aqua", class: "box"))[
  这是一个 #emph[自定义] 的 div，里面可以写 _Typst 标记_。
]

#html.elem("article")[
  == 小节标题
  这段会被递归转换成 <h2> 和段落。
]
```

2. 用 typst-cli 编译为 HTML。两种方式都可以：

   - 依靠后缀自动识别格式：`typst compile hello.typ hello.html`
   - 显式指定格式：`typst compile --format html hello.typ hello.html`

> CLI 在判断输出格式时，看到 `.html` 后缀就会走 HTML 分支（[../typst-cli/src/compile.rs:118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L118)），最终调用 `export_html`（[../typst-cli/src/compile.rs:344-347](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L344-L347)）。

**需要观察的现象**：

- `div` 元素同时带上了 `style` 和 `class` 两个属性。
- `div` 里的 `#emph[自定义]` 被翻译成了 `<em>自定义</em>`，证明 body 仍经过完整的 Typst 转换。
- `article` 里的 `== 小节标题` 变成了 `<h2>`（标题级别从 `<h2>` 起步，原因见后续 [u3-l5](u3-l5-show-rule-registration.md)）。

**预期结果**（pretty 模式下大致形如）：

```html
<div style="background: aqua" class="box">
  这是一个 <em>自定义</em> 的 div，里面可以写 <em>Typst 标记</em>。
</div>
<article>
  <h2>小节标题</h2>
  <p>这段会被递归转换成 &lt;h2&gt; 和段落。</p>
</article>
```

> 注意 `<h2>`、`<` 等会被 HTML 转义，这是后续 [u5-l2](u5-l2-charsets-and-escaping.md) 的内容，这里只需留意即可。若你本地尚未安装 typst，可把本步骤标注为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：用 `html.elem` 写一个 void 元素（如 `<meta>`），并尝试给它传 body，会发生什么？

**参考答案**：void 元素不接受 body。当你写 `#html.elem("meta")[]` 时它是合法的；但 void 标签在 `html.elem` 的转换路径中不会为它生成子内容（参见 [u2-l4](u2-l4-tag-constants-content-models.md) 的 `is_void`）。给一个本应自闭合的元素强行塞 body 会导致输出的 HTML 不合规范，Typst 会提示你正在生成不合法的 HTML。

**练习 2**：为什么 `#html.elem("My-Widget")[...]` 会报错，而 `#html.elem("my-widget")[...]` 不会？

**参考答案**：因为标签名含有连字符 `-`，被判定为自定义元素。自定义元素名**不得包含大写字母**且**必须以小写字母开头**（[src/dom.rs:284-290](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L284-L290)）。`My-Widget` 含大写字母且大写开头，故被拒绝；`my-widget` 全小写且小写开头，合法。

---

### 4.2 HtmlElem 的字段：tag / attrs / body / role

#### 4.2.1 概念说明

`HtmlElem` 一共有六个字段。理解每个字段是「给谁用的」「用户能不能碰到」，你就能彻底掌握这个元素。我们按「用户可见性」给它们分组：

| 字段 | 类型 | 用户可写？ | 作用 |
| --- | --- | --- | --- |
| `tag` | `HtmlTag` | ✅ 必填 | 元素的标签名。 |
| `attrs` | `HtmlAttrs` | ✅ | 元素的 HTML 属性集合（`class`、`style` 等）。 |
| `body` | `Option<Content>` | ✅ 可选 | 元素的子内容，可以是任意 Typst 内容。 |
| `css` | `css::Properties` | ❌ `#[internal]` | 编译器生成的 CSS 属性，仅内部用。 |
| `parent` | `Location` | ❌ `#[internal]` | 元素的逻辑父节点位置，供内省用。 |
| `role` | `Option<EcoString>` | ❌ `#[internal]` | 给顶层元素附加的 ARIA role。 |

带 `#[internal]` 的字段对用户不可见，用户在脚本里碰不到，但它们在编译器内部承担关键职责。

#### 4.2.2 核心流程

字段是怎么从「用户的输入」变成「DOM 节点」的？关键在 `#[elem]` 宏的属性标注和 convert.rs 的 `handle_html_elem`：

- `#[required]` 标注的字段（`tag`）必须由用户提供。
- `#[positional]` 标注的字段（`body`）是位置参数，可不提供（`Option<Content>`）。
- `#[fold]` 标注的字段（`attrs`）会把多层样式链上同名字段**折叠合并**——这正是属性可以「内外层叠加」的原因（折叠语义详见 [u2-l3](u2-l3-htmlattr-attrs-system.md)）。
- `#[internal]` 字段由编译器自己填充：`#[parse(...)]` 给 `css` 一个默认值；`#[synthesized]` 让 `parent` 在合成阶段被赋值；`#[ghost]` 让 `role` 不进入查询树。

#### 4.2.3 源码精读

**① `#[fold]` 让 attrs 可叠加。** 注意 `attrs` 字段的 `#[fold]`：

[src/lib.rs:70-72](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L70-L72)：

```rust
/// The element's HTML attributes.
#[fold]
pub attrs: HtmlAttrs,
```

`#[fold]` 意味着当 `HtmlElem` 出现在 set 规则或样式链中时，内外层的 `attrs` 会通过 `Fold for HtmlAttrs` 合并（默认行为是「外层同名属性不覆盖内层已有属性」，详见 [src/dom.rs:397-410](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L397-L410)）。

**② body 的转换分叉。** `body` 是 `Option<Content>`，在 `handle_html_elem` 里被递归处理。注意它会根据标签是否块级、是否 MathML，选择不同的片段编译入口：

[src/convert.rs:176-230](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L176-L230)：

```rust
if let Some(body) = elem.body.get_ref(styles) {
    let whitespace = if converter.whitespace == Whitespace::Pre
        || elem.tag == tag::pre
        || tag::is_raw(elem.tag)
        || tag::is_escapable_raw(elem.tag)
    { Whitespace::Pre } else { Whitespace::Normal };
    // ...
    if property::Display::default_for(elem.tag) == Some(property::Display::Block) {
        children = html_block_fragment(/* ... */)?;
    } else if tag::mathml::is_mathml(elem.tag) {
        children = html_math_fragment(/* ... */)?;
    } else {
        children = html_inline_fragment(/* ... */)?;
    }
}
```

这段同时揭示了 `role` 字段的用途：它只会作用于「最顶层」那个元素，对子元素会被显式 `unset`（[src/convert.rs:187-195](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L187-L195)），并且对 `<p>` 元素会被过滤掉（避免错误地把分组产生的段落当成目标，见 [src/convert.rs:173](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L173)）。

**③ 组装成 DOM 节点。** 最后所有字段被装配成一个 `HtmlElement`：

[src/convert.rs:237-245](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L237-L245)：

```rust
converter.push(HtmlElement {
    tag: elem.tag,
    attrs,
    css: elem.css.get_cloned(styles),
    children,
    parent: elem.parent,
    span: elem.span(),
    pre_span: false,
});
```

> 注意：`HtmlElement`（DOM 节点）的字段与 `HtmlElem`（原生元素）的字段并不完全相同——例如 `HtmlElement` 多了 `children`、`pre_span`，少了 `role`（role 已被「物化」进 `attrs` 里，见 [src/convert.rs:232-235](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L232-L235)）。区分「源码层元素」与「DOM 层元素」是后续 [u2-l1](u2-l1-dom-data-model.md) 的主题。

#### 4.2.4 代码实践

**实践目标**：通过观察「自定义 `<body>`」的行为，理解 `body` 与全局文档骨架的关系。

**操作步骤**：

1. 写一个 `body.typ`（**示例代码**）：

```typ
#html.elem("body")[
  这里我手动写了 body 标签。
  Typst 会因此省略它自己生成的 <html>/<body> 外壳。
]
```

2. 编译：`typst compile --format html body.typ body.html`。

**需要观察的现象 / 预期结果**：

输出里**不会**再出现 Typst 自动生成的 `<html>...</html>`、`<head>`、`<body>` 外壳——因为你已经手写了 `<body>`。这正是结构体文档注释里那句「Normally, Typst will generate `html`, `head`, and `body` tags for you. If you instead create them with this function, Typst will omit its own tags.」的来源（[src/lib.rs:56-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L56-L57)）。其判定逻辑在 `finalize_dom`，详见 [u3-l2](u3-l2-finalize-dom-skeleton.md)。

> 关于自定义 DOM 的限制（比如此时不能用默认脚注）也留到 [u3-l2](u3-l2-finalize-dom-skeleton.md) 讲。本步若无 typst 可执行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`attrs` 字段标注了 `#[fold]`，`tag` 字段没有。请用一句话解释为什么。

**参考答案**：`tag` 是「标签名」，一个元素只能有一个标签，没有合并的必要，所以是 `#[required]`；`attrs` 是属性集合，可能来自样式链的多层叠加，需要 `#[fold]` 把它们合并成一个最终属性列表。

**练习 2**：`role` 字段标了 `#[internal]` 又标了 `#[ghost]`，它会给用户脚本带来可见的属性吗？

**参考答案**：不会。`#[internal]` 使它对用户不可见、不可写；`#[ghost]` 使它不进入查询/内省树。它只是编译器用来给某个元素临时附加 ARIA `role` 属性的内部通道，最终在 `handle_html_elem` 里被物化进 `attrs`。

---

### 4.3 with_attr 与 with_optional_attr：构造期加属性

#### 4.3.1 概念说明

`with_attr` 和 `with_optional_attr` 是 `HtmlElem` 的两个**Rust builder 方法**（不是 Typst 脚本函数）。它们的受众是**写 Rust 代码扩展 typst-html 的人**——也就是 typst-html 自己内部的 show 规则（`rules.rs`）、类型化构造函数，或未来二次开发的开发者。

为什么需要它们？因为在 Rust 里构造一个 `HtmlElem` 时，比起手动操作 `attrs` 字段，用 builder 方法更简洁、更不易出错。它们体现了 Typst 代码里常见的「builder pattern（建造者模式）」：每个 `with_*` 方法消费 `self`、修改后返回 `self`，从而可以链式调用。

#### 4.3.2 核心流程

两个方法的差别很小，只在一个 `Option` 上：

- `with_attr(attr, value)`：**无条件**添加一个属性。
- `with_optional_attr(attr, Some(value))`：仅当值是 `Some` 时才添加；若是 `None` 则什么都不做（原样返回 `self`）。

伪代码：

```
with_attr(attr, value):
    self.attrs 取可变引用（若无则新建空列表）
    push(attr, value)
    return self

with_optional_attr(attr, opt):
    if opt 是 Some(value):
        return with_attr(attr, value)
    else:
        return self          # 不改动
```

#### 4.3.3 源码精读

**① `with_attr`。** 注意它对 `attrs` 用了 `as_option_mut()` + `get_or_insert_with`，这是因为 `attrs` 字段在宏展开后可能是 `Option`-like 的延迟字段，需要先确保存在一个可变列表再 push：

[src/lib.rs:107-114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L107-L114)：

```rust
/// Add an attribute to the element.
pub fn with_attr(mut self, attr: HtmlAttr, value: impl Into<EcoString>) -> Self {
    self.attrs
        .as_option_mut()
        .get_or_insert_with(Default::default)
        .push(attr, value);
    self
}
```

**② `with_optional_attr`。** 它直接复用 `with_attr`，只是包了一层 `if let Some`：

[src/lib.rs:116-123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L116-L123)：

```rust
/// Adds the attribute to the element if value is not `None`.
pub fn with_optional_attr(
    self,
    attr: HtmlAttr,
    value: Option<impl Into<EcoString>>,
) -> Self {
    if let Some(value) = value { self.with_attr(attr, value) } else { self }
}
```

**③ 它们到底被谁用？** 这两个 builder 是「为 show 规则服务」的。比如 typst-html 在把 Typst 的图片、表格转成 HTML 时，需要程序化地拼装属性，就会用这类 builder。你可以把它们理解为：**用户在脚本里写 `attrs: (class: "x")`，等价于编译器在 Rust 里调用 `elem.with_attr(attr::class, "x")`**。（DOM 层的 `HtmlElement` 也有一个同名 `with_attr`，见 [src/dom.rs:230-234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L230-L234)，两者是平行设计。）

#### 4.3.4 代码实践

**实践目标**：用源码阅读的方式，确认 builder 方法在真实调用链里的位置。

**操作步骤**：

1. 打开 [src/typed.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs)，阅读 `construct()` 函数（[src/typed.rs:118-160](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L118-L160)）。
2. 用 `grep` 在整个 `src/` 下搜索 `with_attr` 和 `with_optional_attr` 的调用点（例如 `rg "with_optional_attr" crates/typst-html/src`）。

**需要观察的现象 / 预期结果**：

- 你会发现 `typed.rs::construct()` **没有**用 `with_attr`，而是直接 `elem.attrs.set(attrs)`（因为属性已批量收集好）。
- 真正的 `with_attr`/`with_optional_attr` 调用集中在 `rules.rs`（show 规则）等需要「逐个、按条件」加属性的地方。这印证了它们是「程序化构造」场景的便利工具。

> 这是「源码阅读型实践」：无需运行，重点是看清 builder 在调用图里的定位。

#### 4.3.5 小练习与答案

**练习 1**：下面两段（伪）Rust 代码效果是否等价？

```rust
// A
elem = elem.with_optional_attr(attr::alt, Some("logo"));
// B
elem = elem.with_attr(attr::alt, "logo");
```

**参考答案**：在这个 `Some` 的情况下二者等价，都会加上 `alt="logo"`。差别只在值为 `None` 时：A 原样返回（不加属性），B 无法表达「不加」的意图（`with_attr` 总是会加）。

**练习 2**：为什么 `with_attr` 要消费并返回 `self`（`mut self -> Self`），而不是 `&mut self`？

**参考答案**：为了支持链式调用，如 `elem.with_attr(a, "1").with_attr(b, "2")`。这是 Rust builder 模式的常见写法，配合 `#[elem]` 生成的不可变字段访问，能保持构造过程清爽。

---

### 4.4 html.frame：把内容以内联 SVG 嵌入

#### 4.4.1 概念说明

并非所有 Typst 内容都适合翻译成 HTML 文本。**图表、精确定位的图形、复杂排版**这类内容，它们的「意义」就依赖于像素级的位置和样式——一旦拆成 HTML 元素就面目全非。`html.frame` 就是为此而生的「截图式」导出：它用 Typst 排版引擎把内容**像导出 PDF/SVG 那样布局成一张 Frame（版面帧）**，再把这张帧渲染成**内联 SVG** 直接嵌进 HTML。

换句话说：

- `html.elem` 的输出是**结构化 HTML**（标签 + 文本），可被 CSS、屏幕阅读器、搜索引擎理解。
- `html.frame` 的输出是**一张矢量图**，保证视觉与 PDF/SVG 导出完全一致，但失去了语义结构。

> 文档原文说得很直白：它「embeds the content as an inline SVG」（[src/lib.rs:135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L135)）。所以 `html.frame` 是「保真优先」的逃生舱，与 `html.elem` 的「语义优先」正好互补。

#### 4.4.2 核心流程

`html.frame` 的旅程比 `html.elem` 多一步「排版」：

1. **构造**：`FrameElem` 只有一个必填位置参数 `body`（要被排版的内容）。
2. **切换 Target**：在 convert.rs 里，遇到 `FrameElem` 时，会把编译目标临时切回 `Target::Paged`（分页目标），让内容**走和 PDF 一样的排版路径**。
3. **排版成 Frame**：调用引擎的 `layout_frame` 把 body 布局成一张 `Frame`。
4. **包装成 HtmlFrame**：把 Frame 连同 `text_size`（用于 em 单位缩放）等元数据封装成 DOM 的 `HtmlFrame` 节点。
5. **渲染成 SVG**：在 encode 阶段，`write_frame` 调 `typst_svg::svg_in_html` 把帧转成 `<svg>...</svg>` 字符串写入输出。

用一张图概括：

```
#html.frame[ 复杂内容 ]
        │  (切回 Paged 目标 + layout_frame)
        ▼
      Frame（版面帧）
        │  HtmlFrame::new(...)  包装元数据
        ▼
   DOM 节点 HtmlFrame { inner: Frame, text_size, anchors, ... }
        │  write_frame → typst_svg::svg_in_html
        ▼
   "<svg ...> ... </svg>"  直接嵌入 HTML
```

**一个关于字号一致性的细节**：`HtmlFrame` 会记录它被定义处的 `text_size`（字号），目的是让 SVG 内部和外部的文字用 **em（相对于字号）** 统一缩放，使图内图外文字大小看起来一致。设帧内某文字在 Frame 坐标系中的绝对高度为 \(h_{\text{abs}}\)，定义处字号为 \(s\)，则浏览器里以 em 表示就是：

\[
h_{\text{em}} = \frac{h_{\text{abs}}}{s}
\]

这样无论外层字号怎么变，帧内外文字的相对比例都保持一致。

#### 4.4.3 源码精读

**① FrameElem 定义。** 它只有一个字段 `body`，既是 `#[positional]` 又是 `#[required]`，且 `since = "0.13.0"`：

[src/lib.rs:126-142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L126-L142)：

```rust
/// An element that lays out its content as an inline SVG.
#[elem(since = "0.13.0")]
pub struct FrameElem {
    /// The content that shall be laid out.
    #[positional]
    #[required]
    pub body: Content,
}
```

**② 转换分支：切回 Paged 并排版。** 这是理解 `html.frame` 最重要的几行。注意 `TargetElem::target.set(Target::Paged).wrap()`——它把目标从 HTML 改成分页，于是 body 走的是 PDF/SVG 那套布局：

[src/convert.rs:140-154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L140-L154)：

```rust
} else if let Some(elem) = child.to_packed::<FrameElem>() {
    let locator = converter.locator.next(&elem.span());
    let style = TargetElem::target.set(Target::Paged).wrap();
    let frame = (converter.engine.library.routines.layout_frame)(
        converter.engine,
        &elem.body,
        locator,
        styles.chain(&style),
        Region::new(Size::splat(Abs::inf()), Axes::splat(false)),
    )?;
    let mut node = HtmlFrame::new(frame, styles, elem.span()).into();
    // A frame is block-level by default like a Typst `image`. It can
    // be wrapped in a `box` to omit the `display` annotation.
    make_block_level(&mut node).unwrap();
    converter.push(node);
}
```

注意倒数几行的注释：帧默认是**块级**的（像图片一样），如果你用 `box` 包起来就可以去掉 display 标注（`display` 机制见 [u4-l2](u4-l2-display-block-inline-promotion.md)）。

**③ HtmlFrame 数据结构。** DOM 里的 `HtmlFrame` 比 Frame 多了几项元数据：

[src/dom.rs:504-521](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L504-L521)：

```rust
pub struct HtmlFrame {
    pub inner: Frame,        // 要渲染成 SVG 的帧
    pub text_size: Abs,      // 定义处字号，用于 em 缩放
    pub id: Option<EcoString>,   // 给 SVG 本身的 id
    pub css: css::Properties,
    pub anchors: EcoVec<(Point, EcoString)>, // 帧内跳转锚点
    pub span: Span,
}
```

其中 `text_size` 在构造时取自当前样式的字号（[src/dom.rs:525-535](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L525-L535)），`anchors` 用于文档内跳转（详见 [u5-l4](u5-l4-link-anchors-jumps.md) 与 [u6-l1](u6-l1-html-frame-svg-embedding.md)）。

**④ 渲染成 SVG。** 最后一步把帧交给 typst-svg：

[src/encode.rs:390-414](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L390-L414)：

```rust
fn write_frame(w: &mut Writer, frame: &HtmlFrame) {
    let svg = typst_svg::svg_in_html(
        &frame.inner,
        frame.text_size,
        w.pretty,
        frame.id.as_deref(),
        &eco_format!("{}", frame.css.to_inline()),
        &frame.anchors,
        w.link_resolver,
    );
    // pretty 模式下做缩进后处理；否则直接拼接
    ...
}
```

这里把 `text_size`、`id`、`css`、`anchors` 全数透传给 `typst_svg::svg_in_html`，由它产出最终的 SVG 字符串。

#### 4.4.4 代码实践

**实践目标**：对比 `html.elem` 与 `html.frame` 对同一段内容的输出差异，直观体会「结构 vs. 截图」。

**操作步骤**：

1. 新建 `compare.typ`（**示例代码**）：

```typ
// ① 用 html.elem：输出结构化 HTML
#html.elem("div")[
  #box baseline: 0pt)[A] #box baseline: 0pt)[B]
]

// ② 用 html.frame：输出一张 SVG
#html.frame[
  #box(baseline: 0pt)[A] #box(baseline: 0pt)[B]
]
```

> 上面的 `box` 精确基线定位在普通 HTML 文本流里很难还原，正好用来凸显 `html.frame` 的「保真」价值。注意：示例里第一处为演示对照，实际书写时请修正括号使其为合法 Typst 代码（见下方说明）。

2. 编译：`typst compile --format html compare.typ compare.html`。
3. 用文本编辑器打开 `compare.html`，对比两段输出。

**需要观察的现象 / 预期结果**：

- 第①段：你能看到 `<div>...A...B...</div>` 这样的**纯文本/标签**结构。
- 第②段：你看到的是一大块 `<svg xmlns="..."> ... </svg>`，里面的 A、B 文字是以 `<text>`、路径或坐标定位的形式出现的。

> 若第一段示例代码括号书写导致编译报错，请把第一处改为合法写法后再编译；这正说明 `html.elem` 的 body 仍走 Typst 解析，而 `html.frame` 则把一切交给排版引擎。若无 typst 可执行，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`html.frame` 的转换为什么要先把目标切回 `Target::Paged`？

**参考答案**：因为只有 Paged 目标才会触发「布局成 Frame」的排版路径（PDF/SVG 用的就是它）。HTML 目标默认是把元素转成 DOM 节点，不会产生 Frame。切换目标是为了复用同一套排版引擎来「截图」（[src/convert.rs:142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L142)）。

**练习 2**：`HtmlFrame` 为什么要保存 `text_size`？不保存会怎样？

**参考答案**：为了让 SVG 用 em 单位缩放，使帧内文字与帧外文字字号视觉一致（见公式 \(h_{\text{em}}=h_{\text{abs}}/s\)）。不保存的话，SVG 只能用绝对像素，当外层 CSS 改变字号时，图内文字不会随之缩放，导致内外字号不协调。

---

## 5. 综合实践

把本讲的两个元素串起来，完成一个小任务：**用 `html.elem` 搭一个带语义的「卡片」外壳，用 `html.frame` 嵌入一段需要精确对齐的小图形。**

**示例代码** `card.typ`：

```typ
#html.elem("article", attrs: (class: "card"))[
  #html.elem("h2")[ 实验对比 ]

  // 结构化内容：由 Typst 自动翻译
  这是一段普通文字，会被翻译成段落。

  // 保真内容：排版后以 SVG 嵌入
  #html.frame[
    #align(center)[
      #box baseline: 0pt)[精确对齐的] #box(baseline: 0pt)[两段文字]
    ]
  ]
]
```

**任务步骤**：

1. 把上面的代码存为 `card.typ`，修正其中明显的括号笔误使其成为合法 Typst 代码。
2. 执行 `typst compile --format html card.typ card.html`。
3. 打开 `card.html`，确认：
   - 外层是 `<article class="card">`，里面有 `<h2>`；
   - `html.frame` 那段输出是一个 `<svg>`；
4. 思考：如果把 `html.frame` 换成 `html.elem("div")[...]`，那段「精确对齐」还能保持吗？为什么？

**预期结果 / 结论**：

你会得到一个语义清晰的 `<article>` 外壳，里面混合了结构化文本和一张保真 SVG。换用 `html.elem` 后，精确对齐通常会丢失，因为 HTML 文本流不提供 Typst 那样的像素级基线控制——这正是 `html.frame` 存在的意义。若本地无 typst，请标注「待本地验证」并侧重完成第 4 步的源码推理。

---

## 6. 本讲小结

- `html.elem`（`HtmlElem`）是「手写任意 HTML 元素」的万能逃生舱，由 `#[elem]` 定义、`module()` 注册；它是所有类型化函数（`html.div` 等）的底层基础。
- `HtmlElem` 的字段分两组：用户可见的 `tag`（必填）、`attrs`（`#[fold]` 可叠加）、`body`（可选内容）；内部字段 `css`/`parent`/`role` 对用户不可见，分别承担生成样式、逻辑父节点、ARIA role 的职责。
- `attrs` 的 `#[fold]` 使属性可在样式链中合并；`body` 在 `handle_html_elem` 中按块级/MathML/行内分叉递归转换。
- `with_attr` / `with_optional_attr` 是面向 Rust 开发者的 builder 方法，用于程序化、可条件地添加属性，常见于 show 规则。
- `html.frame`（`FrameElem`）把内容切回 `Target::Paged` 排版成 Frame，再以内联 SVG 嵌入 HTML，是「视觉保真优先」的逃生舱，与 `html.elem` 的「语义优先」互补。
- 两个元素共同决定了用户在 HTML 导出时对「结构 vs. 图像」的全部控制权。

---

## 7. 下一步学习建议

本讲你掌握了用户侧的两个入口元素。接下来建议：

- **进入第二单元（DOM 数据模型）**：先读 [u2-l1 HTML DOM 数据模型总览](u2-l1-dom-data-model.md)，系统认识 `HtmlElement`、`HtmlNode`、`HtmlFrame` 等 DOM 层数据结构——本讲里它们只是「闪现」，那里会逐字段讲透。
- **接着读 [u2-l2 HtmlTag 与字符串驻留](u2-l2-htmltag-interning.md)**：深入理解本讲提到的标签名校验与自定义元素规则。
- **若对属性系统好奇**：读 [u2-l3 HtmlAttr 与 HtmlAttrs 属性系统](u2-l3-htmlattr-attrs-system.md)，看清 `attrs` 的 `#[fold]` 合并细节。
- **想搞清标题为何从 `<h2>` 起步、图片如何变成 base64**：留到第三单元的 [u3-l5 内建 show 规则注册机制](u3-l5-show-rule-registration.md)。
- **想深入 `html.frame` 的 SVG 渲染与跳转锚点**：留到第六单元的 [u6-l1 html.frame 与 SVG 嵌入](u6-l1-html-frame-svg-embedding.md)。
