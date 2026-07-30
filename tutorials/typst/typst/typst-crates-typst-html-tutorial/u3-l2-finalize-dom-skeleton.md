# finalize_dom 与文档骨架生成

## 1. 本讲目标

上一讲（u3-l1）我们走通了 `html_document` 的主链路：`realize → convert_to_nodes → finalize_dom → resolve_inline_styles → 方程 CSS 注入`。其中 `convert_to_nodes` 产出的是一组**「裸」的 DOM 节点**——它只是正文内容的 HTML 片段，既没有 `<html>` 外壳，也没有 `<head>` 里的 `<meta>`、`<title>`，更没有脚注容器。

本讲就专门聚焦这之间缺失的一环：`finalize_dom`。读完本讲，你应当能够：

- 说清 `finalize_dom` 如何根据用户是否手写了 `<html>` / `<body>` 来决定**要不要**自动包裹文档骨架，以及 `needs_body` 判定逻辑的三个分支。
- 逐条列出 `head_element` 会生成哪些 `<meta>` 标签，以及它们各自依赖 `DocumentInfo` 的哪个字段。
- 解释当用户使用「自定义 DOM」（即手写 `<html>` 或 `<body>`）时，为什么默认脚注会不可用，并能在源码中定位到那条报错路径。
- 理解 `HtmlOutput` 这个「扁平数组 + 根下标」的设计，能说清 `root_index` 在不同分支下取值为何不同。

---

## 2. 前置知识

本讲是 u3-l1 的直接延续，下面这些概念默认你已经掌握（来自前置讲义）：

- **HtmlNode / HtmlElement**：typst-html 内部的 DOM 节点模型。`HtmlNode` 是四变体枚举（`Tag` / `Text` / `Element` / `Frame`），其中 `Tag` 只是内省用的元数据，**不会产生任何 HTML 输出**（u2-l1）。这一点在本讲计数逻辑里非常关键。
- **HtmlElement 的 builder 链**：`HtmlElement::new(tag)` 创建空元素，`.with_children(...)` / `.with_attr(key, value)` 是消费 `self` 的链式构造方法（u2-l1、u2-l3）。
- **DocumentInfo**：在 `html_document_common` 里由 `info.populate(styles)` 与 `info.populate_locale(styles)` 填充的文档元信息，字段含 `title` / `author` / `description` / `keywords` / `locale` 等（u3-l1）。
- **Target::Html 与导出主线**：`html_document` 把 `Content` 编译成 `HtmlDocument`，`html` 再把它编码成字符串（u1-l3）。

一个直觉性的比喻：`convert_to_nodes` 像是把正文写好的一沓散页，`finalize_dom` 则负责给这沓散页**装订成一个完整的 HTML 文档**——套上信封（`<html>`）、贴上目录页（`<head>`）、在末尾钉上附录（脚注）。但如果你自己已经带来了一个信封，装订工会换一种工作方式。

---

## 3. 本讲源码地图

本讲几乎全部集中在**一个文件**里：

| 文件 | 作用 |
| --- | --- |
| [src/document.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs) | 编译主链路与文档骨架生成。本讲的四个最小模块 `finalize_dom` / `head_element` / `footnotes_unsupported_with_custom_dom` / `HtmlOutput` 全部在此。 |

为辅助理解，还会少量引用：

| 文件 | 作用 |
| --- | --- |
| [src/dom.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs) | `HtmlElement`、`HtmlNode`、`HtmlDocument` 的数据结构定义。 |
| [src/attr.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/attr.rs) | `attr::charset` / `attr::name` / `attr::content` / `attr::lang` 等属性名编译期常量。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应规格里的 `finalize_dom`、`head_element`、`footnotes_unsupported_with_custom_dom`、`HtmlOutput`。它们之间的关系是：

```
html_document_common
        │
        │  convert_to_nodes 产出的裸节点 EcoVec<HtmlNode>
        ▼
   finalize_dom ───────────┐  (4.1 主调度)
        │                  │
        ├─ 需要 <head>？──► head_element        (4.2 meta 标签集合)
        │                  │
        └─ 自定义 DOM？──► footnotes_unsupported_with_custom_dom  (4.3 脚注限制)
                           │
                           ▼
                      HtmlOutput { nodes, root_index }   (4.4 结果容器)
```

### 4.1 `finalize_dom`：是否包裹 `<html>` / `<body>` 的总调度

#### 4.1.1 概念说明

`convert_to_nodes` 产出的节点列表（`EcoVec<HtmlNode>`）只是正文。浏览器要正确渲染一份完整 HTML，通常还需要：

- 一个 `<html>` 根元素，带上 `lang` 属性声明语言；
- 一个 `<head>`，里面放 `<meta charset>`、`<meta viewport>`、`<title>` 等；
- 一个 `<body>` 包住正文，并把脚注追加到 `<body>` 末尾。

但 typst-html 也允许用户用 `html.elem` 自己手写 `<html>` 或 `<body>`（参见 u1-l4）。这就出现了一个分叉：

- **默认情况**：用户没写外壳 → 编译器自动生成完整骨架（`<html><head>...</head><body>正文+脚注</body></html>`）。
- **自定义 `<body>`**：用户已写 `<body>` → 编译器不再生成 `<body>`、不再注入脚注，但仍会补上 `<html>` 和 `<head>`。
- **自定义 `<html>`**：用户连 `<html>` 都写了 → 编译器**完全不再加工**，原样返回。

`finalize_dom` 就是实现这套「智能装订」逻辑的函数。

#### 4.1.2 核心流程

`finalize_dom` 的判定核心是一个 `count`（统计「真实可见」的元素个数）和对每个元素标签的 `match`。用伪代码描述：

```
fn finalize_dom(nodes, info, footnote_locator, footnote_styles):
    count = nodes 中「非 Tag 节点」的个数   # Tag 不产生输出，不计入

    needs_body = True
    对 nodes 里每个 (idx, node):
        如果 node 不是 Element: 跳过
        根据 (node.tag, count) 分支:
            (html, 1):   # 用户写了唯一的 <html>
                检查脚注（有则报错）
                直接返回，root_index = idx，不再加工
            (body, 1):   # 用户写了唯一的 <body>
                检查脚注（有则报错）
                needs_body = False
            (html 或 body, 其它):   # 写了但不是「唯一元素」
                报错：该元素必须是文档中唯一元素

    # 默认 / 自定义 body 分支
    if needs_body:
        body = 新建 <body>，children = 原始 nodes
        footnotes = html_block_fragment(脚注容器, ...)
        body.children 末尾追加 footnotes
    else:   # 用户已自定义 <body>
        body = 原始 nodes

    html = 新建 <html lang="...">，children = [head_element(info)] + body
    返回 HtmlOutput { nodes: [html], root_index: 0 }
```

三个要点：

1. **`count` 用「非 Tag」过滤**：因为内省用的 `HtmlNode::Tag` 不产生 HTML 输出（u2-l1），它不应参与「文档里到底有几个元素」的计数。
2. **「唯一元素」约束**：用户写的 `<html>` / `<body>` 必须是整个文档里**唯一**的真实元素（`count == 1`），否则报错。这避免了「半个外壳」这种 ambiguous 结构。
3. **`needs_body` 的语义**：它只决定「要不要新建 `<body>` 并注入脚注」，**不**决定要不要 `<html>` / `<head>`。即便用户自定义了 `<body>`，编译器依然会补上 `<html>` 和 `<head>`。

#### 4.1.3 源码精读

先看函数签名与 `count` 的计算：

[src/document.rs:259-268](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L259-L268) —— `finalize_dom` 接收裸节点与文档信息；第 266 行用 `filter` 排除 `HtmlNode::Tag` 来统计真实元素个数 `count`，并初始化 `needs_body = true`。

接着是核心的 `match (tag, count)` 三分支判定：

[src/document.rs:269-288](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L269-L288) —— 遍历每个元素节点，按 `(标签, count)` 分派：

- `(tag::html, 1)`：用户写了唯一的 `<html>` → 调脚注检查后**立即 `return`**，把 `root_index` 记为该元素的原始下标 `idx`。这是唯一一条「完全不加工」的路径。
- `(tag::body, 1)`：用户写了唯一的 `<body>` → 调脚注检查，把 `needs_body` 置为 `false`，**继续往下走**（仍会补 `<html>` / `<head>`）。
- `(tag::html | tag::body, _)`：写了 `<html>` / `<body>` 但不是唯一元素 → 用 `bail!` 报错，错误信息为 `` "`{}` element must be the only element in the document" ``。

注意三种返回方式的区别：第一条分支用 `return` 直接结束；第二条只是改一个布尔量后继续；第三条用 `bail!`（来自 `typst_library::diag`）把错误带 span 地抛回上层。

再看默认分支的 `<body>` 构造与脚注注入：

[src/document.rs:290-303](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L290-L303) —— 当 `needs_body` 为真，新建一个 `<body>` 把原始 `nodes` 全部塞进去；然后调用 `crate::fragment::html_block_fragment`（u3-l4）把 `FootnoteContainer::shared()` 这个**全局单例脚注容器**编译成脚注节点，追加到 `<body>` 末尾。这就是「默认脚注」的来源。若 `needs_body` 为假（用户自定义了 `<body>`），`body` 直接等于原始 `nodes`，**脚注完全不注入**。

最后是 `<html>` 外壳的包裹：

[src/document.rs:305-311](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L305-L311) —— 新建 `<html>`，用 `attr::lang` 设置 `lang` 属性（值来自 `info.locale`，见 4.2），把 `head_element(info)` 的结果作为第一个孩子，再 `extend` 上 `body`。最终返回的 `HtmlOutput` 中 `nodes` 只含这一个 `<html>` 元素，`root_index = 0`。

> 小结 `root_index` 的两种取值：自定义 `<html>` 时为 `idx`（用户元素在原数组中的位置，可能不是 0，因为前面可能排着内省 `Tag`）；其余情况为 `0`（编译器新建的 `<html>` 必在数组首位）。这正是 u2-l1 讲过的「扁平数组 + root_index」设计在这里的体现。

#### 4.1.4 代码实践

**实践目标**：亲手触发「自定义 `<body>`」分支，观察 `finalize_dom` 跳过了哪些步骤、保留了哪些步骤。

**操作步骤**：

1. 准备两个 Typst 文件。

   `default.typ`（默认情况，脚注可用）：
   ```typ
   This is a paragraph with a footnote.#footnote[Hi from the note.]
   ```

   `custom-body.typ`（自定义 `<body>`）：
   ```typ
   #html.elem("body")[
     This is a paragraph with a footnote.#footnote[Hi from the note.]
   ]
   ```

2. 分别用 CLI 导出 HTML（命令格式见 u1-l3）：
   ```bash
   typst compile --format html default.typ default.html
   typst compile --format html custom-body.typ custom-body.html
   ```

**需要观察的现象**：

- `default.html` 应当包含编译器自动生成的 `<html lang="...">`、`<head>`（含 `<meta charset>`、`<meta name="viewport">`）、`<body>`，且 `<body>` 末尾有脚注内容。
- `custom-body.html`：根据 4.1.3 的源码，自定义 `<body>` 分支会**先**调 `footnotes_unsupported_with_custom_dom`。因为你里面用了 `#footnote[...]`，预期**编译会报错**：`footnotes are not currently supported in combination with a custom <html> or <body> element`。

**预期结果**：

- `default.typ` 成功导出，骨架完整、脚注追加在 `<body>` 末尾。
- `custom-body.typ` 编译失败并给出上述脚注报错（这正是 4.3 要讲的路径）。

> 说明：本实践的具体输出「待本地验证」——作者未在本环境实跑命令，结论基于 [src/document.rs:269-303](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L269-L303) 的源码逻辑推断。去掉 `custom-body.typ` 里的 `#footnote[...]` 后，它应能成功导出，且产物里**仍有** `<html>` 和 `<head>`，但 `<body>` 是你手写的那一个、末尾没有脚注。

#### 4.1.5 小练习与答案

**练习 1**：如果用户在文档里写了两个 `<body>` 元素，`finalize_dom` 会怎样？

**参考答案**：`count` 会大于 1。遍历时遇到第一个 `<body>` 走 `(tag::body, 1)` 之外的分支（因为此时 `count != 1`），命中 `(tag::html | tag::body, _)` 分支，`bail!` 报错 `` `<body> element must be the only element in the document` ``（[src/document.rs:281-285](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L281-L285)）。

**练习 2**：为什么 `count` 要用 `filter(|node| !matches!(node, HtmlNode::Tag(_)))` 过滤掉 `Tag`？

**参考答案**：`HtmlNode::Tag` 只承载内省元数据，不产生任何 HTML 输出（u2-l1）。它出现在节点数组里是为了让内省器对齐位置，并不代表一个「真实可见的 HTML 元素」。若不过滤，纯内省标记会干扰「文档里有几个真实元素」的判定，导致「唯一元素」约束失真。

---

### 4.2 `head_element`：`<head>` 与 meta 标签集合

#### 4.2.1 概念说明

`<head>` 里放的是文档级元数据：字符编码、视口设置、标题、描述、关键词、作者等。typst-html 并非凭空捏造这些值，而是从 `DocumentInfo`（由 `set document(...)` 等 set 规则填充）里取。`head_element` 就负责把 `DocumentInfo` 的字段翻译成一组 `<meta>` / `<title>` 元素。

#### 4.2.2 核心流程

`head_element` 按固定顺序往 `children` 里 push 元素，**部分无条件、部分有条件**：

| 顺序 | 元素 | 条件 | 数据来源 |
| --- | --- | --- | --- |
| 1 | `<meta charset="utf-8">` | 总是生成 | 硬编码 `"utf-8"` |
| 2 | `<meta name="viewport" content="width=device-width, initial-scale=1">` | 总是生成 | 硬编码 |
| 3 | `<title>...</title>` | `info.title` 为 `Some` | `info.title` |
| 4 | `<meta name="description" content="...">` | `info.description` 为 `Some` | `info.description` |
| 5 | `<meta name="authors" content="...">` | `!info.author.is_empty()` | `info.author.join(", ")` |
| 6 | `<meta name="keywords" content="...">` | `!info.keywords.is_empty()` | `info.keywords.join(", ")` |

最后把这些 `children` 装进一个 `<head>` 元素返回。注意第 5 项的属性名是 **`authors`**（复数），不是 `author`——作者列表用逗号拼接成一个 `content` 值。

> 补充：`<html>` 上的 `lang` 属性并不在 `head_element` 里生成，而是在 4.1 的 `finalize_dom` 里直接写到 `<html>` 上，值来自 `info.locale`（见 4.1.3 第 306 行）。

#### 4.2.3 源码精读

无条件生成的两个 meta：

[src/document.rs:317-324](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L317-L324) —— 永远先生成 `<meta charset="utf-8">` 和 `<meta name="viewport" content="width=device-width, initial-scale=1">`。注意 void 标签 `meta` 不需要孩子，只用 `.with_attr(...)` 链式加属性（`attr::charset` / `attr::name` / `attr::content` 都是 [src/attr.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/attr.rs) 里的编译期常量）。

有条件生成的 `title` / `description`：

[src/document.rs:326-341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L326-L341) —— `info.title` 为 `Some` 时生成 `<title>`，孩子是一个 `HtmlNode::Text`；`info.description` 为 `Some` 时生成对应的 description meta。`title` 用的是元素孩子而非属性，符合 HTML 规范。

作者与关键词（注意复数 `authors` 与 `join`）：

[src/document.rs:343-359](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L343-L359) —— 当作者数组非空时生成 `<meta name="authors">`，`content` 用 `info.author.join(", ")` 把多个作者拼成逗号分隔字符串；关键词同理。这两项用 `!is_empty()` 判断（而非 `Option`），因为 `author` / `keywords` 字段类型本身就是 `Vec`。

最终的 `<head>` 装配：

[src/document.rs:361](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L361) —— `HtmlElement::new(tag::head).with_children(children)`，把上述列表整体塞进 `<head>`。

这些 `DocumentInfo` 字段是从哪来的？它们在 `html_document_common` 里由 `info.populate(styles)` 填充，而 `populate` 读取的是 `DocumentElem::title` 等 set 规则（参见 [crates/typst-library/src/model/document.rs:349-375](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/document.rs#L349-L375)）。所以用户在 Typst 里写 `#set document(title: "...", author: (...))`，最终就会经 `DocumentInfo` 流到这里的 `<title>` / `<meta name="authors">`。

#### 4.2.4 代码实践

**实践目标**：验证 `set document(...)` 的各字段如何映射到 `<head>` 里的 meta 标签。

**操作步骤**：

1. 编写 `meta.typ`：
   ```typ
   #set document(
     title: "My HTML Doc",
     author: ("Alice", "Bob"),
     description: "A demo for head_element.",
     keywords: ("typst", "html"),
   )
   #set text(lang: "en")

   Hello, HTML.
   ```
2. 导出：`typst compile --format html meta.typ meta.html`。
3. 打开 `meta.html`，对照 4.2.2 的表格逐项核对 `<head>` 内容。

**需要观察的现象**：`<head>` 里应依次出现 charset、viewport、`<title>My HTML Doc</title>`、`<meta name="description" content="A demo for head_element.">`、`<meta name="authors" content="Alice, Bob">`、`<meta name="keywords" content="typst, html">`；`<html>` 标签上应有 `lang="en"`。

**预期结果**：六项 meta/title 全部出现且顺序与表格一致；若删掉 `author` 字段，则 `authors` meta 应消失（因 `Vec` 为空）。具体输出「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `title` 用 `<title>文本</title>` 的孩子形式，而 `description` 用 `<meta content="...">` 的属性形式？

**参考答案**：这是 HTML 规范要求——`<title>` 的内容必须是其孩子文本节点；`<meta>` 是 void 元素，不能有孩子，描述只能放进 `content` 属性。`head_element` 忠实反映了规范：`title` 分支用 `.with_children(eco_vec![HtmlNode::Text(...)])`（[src/document.rs:328-331](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L328-L331)），description 分支用 `.with_attr(attr::content, ...)`。

**练习 2**：`info.locale` 是 `Smart<Locale>` 类型，第 306 行 `info.locale.unwrap_or_default().rfc_3066()` 在「用户没设语言」时会产生什么？

**参考答案**：`Smart<Locale>` 未设时 `unwrap_or_default()` 得到默认 `Locale`，`rfc_3066()`（[crates/typst-library/src/text/lang.rs:144](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/lang.rs#L144)）把语言（和可能的 region）拼成形如 `en` 或 `en-US` 的 BCP 47 字符串写到 `<html lang="...">`。

---

### 4.3 `footnotes_unsupported_with_custom_dom`：自定义 DOM 下的脚注限制

#### 4.3.1 概念说明

回看 4.1：默认情况下脚注由 `html_block_fragment(FootnoteContainer::shared(), ...)` 编译并追加到自动生成的 `<body>` 末尾。但当用户自定义了 `<html>` 或 `<body>`，这条注入路径被跳过了——编译器不会往用户手写的元素里强行塞脚注。

可是用户仍可能在正文里写 `#footnote[...]`。这些脚注标记（`FootnoteMarker`）如果既不被注入、又不报错，就会「静默丢失」，造成内容缺失。因此 `finalize_dom` 在两条自定义分支里都先调用 `footnotes_unsupported_with_custom_dom` 做一次检查：**有脚注标记就报错，明确告诉用户当前限制**。

#### 4.3.2 核心流程

```
fn footnotes_unsupported_with_custom_dom(engine):
    markers = 查询文档里所有的 FootnoteMarker
    if markers 为空:
        return Ok(())        # 没用脚注，放行
    else:
        对每个 marker 生成一条 error!（带 hint）
        返回 Err(这些错误)
```

关键点：

- 它用**内省查询**（`engine.introspect(QueryIntrospection(FootnoteMarker::ELEM.select(), ...))`）来发现脚注标记，而不是在节点里硬找。
- 报错时**每个标记一条错误**（`.iter().map(...).collect()`），并附 hint 提示「你仍可用自定义脚注 show 规则」。

#### 4.3.3 源码精读

[src/document.rs:365-384](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L365-L384) —— `footnotes_unsupported_with_custom_dom`：先查询 `FootnoteMarker`；为空则 `Ok(())` 放行；否则用 `error!`（带 `marker.span()` 与 hint）为每个标记生成错误并 `collect()` 成 `SourceResult` 的 `Err`。错误信息明确说明「footnotes are not currently supported in combination with a custom `<html>` or `<body>` element」，并给出绕过方案 hint：用自定义脚注 show 规则。

它在 `finalize_dom` 的两个自定义分支被调用：

- 自定义 `<html>`：[src/document.rs:274](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L274)（紧跟在 `return` 之前）。
- 自定义 `<body>`：[src/document.rs:278](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L278)（紧跟在设 `needs_body = false` 之前）。

> 为什么「自定义 `<body>`」也要查？因为虽然这条分支仍会生成 `<html>` / `<head>`，但它**跳过了 4.1 中 `needs_body` 为真时的脚注注入**（`body` 直接等于原始 `nodes`）。脚注无处安放，所以同样必须报错。

#### 4.3.4 代码实践

**实践目标**：触发并解读自定义 DOM 下的脚注报错。

**操作步骤**：

1. 准备 `fn-html.typ`（自定义 `<html>` + 脚注）：
   ```typ
   #html.elem("html")[
     #html.elem("body")[
       Text.#footnote[A note.]
     ]
   ]
   ```
2. 导出：`typst compile --format html fn-html.typ fn-html.html`。

**需要观察的现象**：编译失败，错误定位到 `#footnote[...]` 所在 span，信息为「footnotes are not currently supported in combination with a custom `<html>` or `<body>` element」，并带 hint「you can still use footnotes with a custom footnote show rule」。

**预期结果**：与现象一致。这就是 4.3.3 里 `error!` 的产物。具体报错文本「待本地验证」。

**进一步思考**：去掉 `#footnote[...]` 后重编译，此时 `markers.is_empty()` 成立，函数 `Ok(())` 放行，自定义 `<html>` 路径会原样返回你的 DOM（参见 4.1.3 第一条分支）。

#### 4.3.5 小练习与答案

**练习 1**：`footnotes_unsupported_with_custom_dom` 为什么用内省查询 `FootnoteMarker`，而不是去 `nodes` 数组里找脚注元素？

**参考答案**：脚注标记是 Typst 侧的内省元素，`finalize_dom` 拿到的 `nodes` 是已经转换过的 HTML 节点，里面未必保留可识别的脚注标记形态。通过 `engine.introspect(QueryIntrospection(...))` 查询 `FootnoteMarker::ELEM`，能在文档语义层可靠地发现「用户是否用了脚注」，这正是内省器（u1-l3、u5-l3）的用途。

**练习 2**：如果用户自定义了 `<body>` 但完全没用 `#footnote`，会发生什么？

**参考答案**：`footnotes_unsupported_with_custom_dom` 查询为空，返回 `Ok(())` 放行；`needs_body` 置 `false`，跳过脚注注入；最终仍生成 `<html lang="...">` + `<head>` + 用户手写的 `<body>`，`root_index = 0`。即「自定义 body 但无脚注」是完全合法的用法。

---

### 4.4 `HtmlOutput`：扁平数组与根下标

#### 4.4.1 概念说明

`finalize_dom` 的返回值是 `HtmlOutput`。回顾 u2-l1：typst-html 不用显式的树指针表示 DOM，而是用一个**扁平的 `EcoVec<HtmlNode>`** 加一个 `root_index` 指向根元素。`HtmlOutput` 就是这个容器的具体定义。

为什么根元素不一定是 `nodes[0]`？因为自定义 `<html>` 分支里，用户元素在原数组中的位置 `idx` 可能不是 0——它前面可能排着若干内省用的 `HtmlNode::Tag`（u2-l1 讲过 `Tag` 不产生输出）。所以需要一个独立的 `root_index` 字段来记录根到底在哪。

#### 4.4.2 核心流程

`HtmlOutput` 的接口很薄：

| 方法 | 作用 |
| --- | --- |
| `nodes()` | 返回全部节点的切片 |
| `root()` | 返回根 `HtmlElement` 的不可变引用（按 `root_index` 取） |
| `root_mut()` | 返回根 `HtmlElement` 的可变引用（用 `make_mut` 处理写时复制） |
| `root_node()` | 返回根所在的 `HtmlNode` 包装（含 `Tag`/`Text`/`Element`/`Frame` 区分） |

`root_mut` 之所以重要，是因为 `html_document_common` 在 `finalize_dom` 之后还要用它做两件事：`resolve_inline_styles(output.root_mut())`（解析内联样式）和方程 CSS 注入时往 `<head>` 里找/加 `<style>`（u3-l1）。

#### 4.4.3 源码精读

结构定义与注释：

[src/document.rs:220-225](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L220-L225) —— `HtmlOutput { nodes: EcoVec<HtmlNode>, root_index: usize }`。文档注释称其为「The introspectible output of HTML compilation」（HTML 编译的可内省输出）。

取根元素的方法：

[src/document.rs:234-247](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L234-L247) —— `root()` / `root_mut()` 都用 `self.nodes[self.root_index]` 取值并 `match` 出 `HtmlNode::Element`，若不是则 panic（`expected HTML element`）。`root_mut` 用 `self.nodes.make_mut()` 触发 `EcoVec` 的写时复制（u2-l1），保证改 root 时不影响共享同一份 `HtmlOutput` 的其他引用。

它在主链路里的两个消费点（回顾 u3-l1，便于衔接）：

- [src/document.rs:190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L190) —— `css::resolve_inline_styles(output.root_mut())`：在 `finalize_dom` 之后解析内联样式（因为 finalize 可能新增带样式的节点）。
- [src/document.rs:196-215](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L196-L215) —— 方程 CSS 注入：若文档含数学公式，用 `root_mut()` 找到 `<head>` 并追加一个含 `EQUATION_CSS_STYLES` 的 `<style>`。

#### 4.4.4 代码实践

**实践目标**：通过源码阅读，理解 `root_index` 在两种 `finalize_dom` 分支下的不同来源。

**操作步骤**（源码阅读型实践）：

1. 打开 [src/document.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs)。
2. 找到 `finalize_dom` 的三处 `HtmlOutput` 构造点：
   - 自定义 `<html>` 分支：`return Ok(HtmlOutput { nodes, root_index: idx })`（[L275](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L275)）——`idx` 来自 `enumerate`，是用户元素在原数组的下标。
   - 默认 / 自定义 body 分支：`Ok(HtmlOutput { nodes: eco_vec![html.into()], root_index: 0 })`（[L310](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L310)）——新建的 `<html>` 必在数组首位。
3. 思考：为什么第一种情况不能用 `0`？因为用户元素前可能排着内省 `Tag`。

**需要观察的现象**：两处构造点的 `root_index` 取值来源不同。

**预期结果**：能口头解释「自定义 `<html>` 时 `root_index = idx`（可能非 0），其余为 `0`」的原因。

#### 4.4.5 小练习与答案

**练习 1**：`root()` 和 `root_mut()` 在匹配失败时为什么用 `panic!` 而不是返回 `Result`？

**参考答案**：`root_index` 指向的节点「必须是 Element」是 `finalize_dom` 保证的不变量——`finalize_dom` 的两条返回路径分别把根设为用户的 `<html>` 元素（`Element`）或编译器新建的 `<html>` 元素（`Element`）。如果这个不变量被破坏，属于内部逻辑错误而非用户输入错误，用 `panic!`（`expected HTML element`）直接暴露 bug 比让上层处理 `Result` 更合适。

**练习 2**：`root_mut` 为什么调用 `self.nodes.make_mut()` 而不是 `&mut self.nodes[self.root_index]`？

**参考答案**：`HtmlOutput` 派生了 `Clone`，`nodes` 是写时复制的 `EcoVec`。`HtmlDocument` 又把 `HtmlOutput` 包在 `Arc<HtmlIntrospector>` 等结构里可能被共享（u2-l1 提到 `root_mut` 用于交叉链接）。`make_mut()` 确保在修改前做必要的深拷贝，避免改 root 时影响到共享同一份数据的其他所有者。

---

## 5. 综合实践

把本讲四个模块串起来，做一个**端到端的「自定义 `<body>`」分析任务**。

**任务**：解释「当用户已用 `html.elem` 手写 `<body>` 时，`finalize_dom` 跳过了哪些步骤、保留了哪些步骤，并说明为何此时不能使用默认脚注」。

请按以下步骤完成：

1. **画出调用路径**。从 [src/document.rs:269-288](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L269-L288) 的 `match` 出发，标注自定义 `<body>` 命中的是 `(tag::body, 1)` 分支，并说明它**没有** `return`、只是把 `needs_body` 置 `false`。

2. **列出「跳过」与「保留」**。对照 4.1.2 的伪代码：
   - **跳过**：新建 `<body>`（`needs_body` 为假，`body` 直接等于原始 `nodes`）、**注入脚注**（`html_block_fragment(FootnoteContainer::shared(), ...)` 整段不执行）。
   - **保留**：调用 `footnotes_unsupported_with_custom_dom` 检查脚注、新建 `<html lang="...">`、生成 `head_element(info)` 并作为 `<html>` 第一个孩子、返回 `root_index = 0` 的 `HtmlOutput`。

3. **解释脚注为何不可用**。引用 [src/document.rs:278](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L278) 与 [src/document.rs:365-384](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L365-L384)：默认脚注依赖编译器自动生成的 `<body>` 来承载 `FootnoteContainer::shared()` 编译出的脚注节点；自定义 `<body>` 跳过了这条注入路径，脚注无处安放。为避免「静默丢失」，`finalize_dom` 在设 `needs_body = false` **之前**先调检查函数：只要文档里存在任何 `FootnoteMarker`，就报错，并提示可用「自定义脚注 show 规则」绕过。

4. **（可选，待本地验证）实跑验证**。用 4.1.4 的 `custom-body.typ`（含脚注）与去掉脚注的版本分别编译，确认前者报错、后者成功且产物仍含 `<html>` / `<head>`。

完成本任务后，你应当能向别人讲清「默认 DOM」与「自定义 `<body>`」两条路径在 `finalize_dom` 里的全部差异。

---

## 6. 本讲小结

- `finalize_dom` 是「装订工」：决定要不要给裸节点套上 `<html>` / `<head>` / `<body>` 并注入脚注，核心判定量是「非 Tag 节点个数」`count` 与每个元素标签的 `match (tag, count)`。
- 三条分支：默认（全量生成骨架 + 注入脚注）、自定义 `<body>`（跳过 `<body>` 新建与脚注注入，仍补 `<html>` / `<head>`）、自定义 `<html>`（原样返回、完全不加工）。`<html>` / `<body>` 必须是文档里唯一真实元素，否则报错。
- `head_element` 按 charset → viewport → title → description → authors → keywords 的固定顺序生成 `<head>` 内容，值全部来自 `DocumentInfo`；注意作者 meta 名是复数 `authors`、用逗号 join；`lang` 写在 `<html>` 上而非 head 里。
- `footnotes_unsupported_with_custom_dom` 用内省查询 `FootnoteMarker`：自定义 DOM 下只要用了脚注就报错（每个标记一条 error + hint），防止脚注静默丢失；这是「默认脚注依赖自动 `<body>`」这一设计的直接后果。
- `HtmlOutput { nodes, root_index }` 用扁平数组 + 根下标表示文档；`root_index` 在自定义 `<html>` 分支为用户元素下标 `idx`（可能非 0），其余为 `0`；`root_mut()` 用 `make_mut()` 处理写时复制，供后续 `resolve_inline_styles` 与方程 CSS 注入使用。

---

## 7. 下一步学习建议

本讲结束意味着 `finalize_dom` 这块骨架生成已吃透，编译主链路（u3 单元）也就完整了。建议下一步：

- **u3-l3 `convert_to_nodes`**：`finalize_dom` 处理的是 `convert_to_nodes` 的产出。去读 `convert.rs` 的 `handle()` 调度器，搞清正文 `Content` 是如何逐类变成 `HtmlNode` 的——这正是 `finalize_dom` 接收到的 `nodes` 的来源。
- **u3-l4 片段的递归编译**：本讲多次提到 `html_block_fragment(FootnoteContainer::shared(), ...)`，去 `fragment.rs` 看 block / inline / math 片段的入口与缓存策略，理解脚注容器是如何被编译成脚注节点的。
- **u5-l3 / u5-l4 内省与链接锚点**：`footnotes_unsupported_with_custom_dom` 用的 `engine.introspect(QueryIntrospection(...))` 与 `HtmlOutput` 的「可内省输出」定位，都指向内省子系统。学完 u5-l3 能更深理解 `root_index` 与 `Tag` 在内省里的角色。
