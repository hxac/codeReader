# 编译单个文档：compile_document 的格式推断与分派

## 1. 本讲目标

在上一讲（u2-l1）里，我们已经走完了 `bundle_impl` 的前半段：realize（实现化）→ collect（校验与查重）→ parallelize（并行编译各文档）。本讲把镜头推进到并行编译的**最小执行单元**——`compile_document`，也就是「把一个用户写的 `#document(...)` 元素真正编译成一份可导出文档」的函数。

读完本讲你应该能够：

- 说清楚 `compile_document` 是如何为一份文档决定导出格式的（显式 `format:` 优先，否则按路径扩展名推断）。
- 理解「目标切换」：bundle 整体是 `Target::Bundle`，但每个子文档要切到 `Target::Paged` 或 `Target::Html` 才能正确排版，这靠 `TargetElem::target.set(...).wrap()` 注入样式链实现。
- 看懂 `compile_document` 如何按 `PagedFormat` / `Html` 把任务分派给 `typst_layout::layout_document_for_bundle` 或 `typst_html::html_document_for_bundle`。
- 理解 PNG / SVG 只支持单页的约束及其报错，以及 HTML 导出受 `Feature::Html` 特性开关门控。

---

## 2. 前置知识

本讲承接 u2-l1，假定你已经知道：

- `bundle_impl` 在 collect 之后用 `engine.parallelize(...)` 并行处理每个 `Child`；其中 `Child::Document(document, styles, locator)` 会调用 `compile_document`。
- `Bundle` 里每个文档最终是 `BundleDocument::Paged(PagedDocument, PagedExtras)` 或 `BundleDocument::Html(HtmlDocument)` 两种形态之一。
- Typst 有三种编译目标 `Target`：`Paged`、`Html`、`Bundle`，三者与 `Output` trait 一一对应。

此外，本讲会用到一个 Typst 的通用概念：`Smart<T>`。它是一个两态枚举：

- `Smart::Auto`：表示「用户没有显式指定，请帮我自动推断」。
- `Smart::Custom(T)`：表示「用户已经明确给出了值」。

文档的 `format` 字段类型是 `Smart<DocumentFormat>`，默认为 `Auto`。格式推断的整套逻辑就是围绕「先看是不是 `Custom`，不是再去推断」展开的。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-bundle/src/lib.rs` | 定义 `compile_document`：格式推断 → 目标切换 → 分派 → 单页/特性校验。本讲的核心文件。 |
| `crates/typst-library/src/model/document.rs` | 定义 `DocumentElem`、`determine_format`、`determine_format_from_path`、`DocumentFormat`、`PagedFormat` 等用户侧与格式相关的数据结构。 |
| `crates/typst-library/src/foundations/target.rs` | 定义 `Target` 枚举与承载 `target` 样式字段的 `TargetElem`。 |
| `crates/typst-library/src/lib.rs` | 定义 `Feature` 枚举（含 `Feature::Html`）与 `is_enabled`。 |
| `crates/typst-layout/src/pages/mod.rs` | 提供 `layout_document_for_bundle`，bundle 里分页文档的排版入口。 |
| `crates/typst-html/src/document.rs` | 提供 `html_document_for_bundle`，bundle 里 HTML 文档的编译入口。 |

---

## 4. 核心概念与源码讲解

### 4.1 文档格式推断：determine_format / determine_format_from_path

#### 4.1.1 概念说明

bundle 里的一个文档最终会被导出成什么格式？Typst 给了用户两条路：

1. **显式指定**：在 `document` 元素上写 `format: "pdf"`（或 `"png"` / `"svg"` / `"html"`）。
2. **自动推断**：不写 `format:`（即默认 `auto`），Typst 从路径的扩展名猜。比如 `document("index.html", ...)` 推断成 HTML，`document("report.pdf", ...)` 推断成 PDF。

两条路有明确的优先级：**显式优先，推断兜底**。这一点很关键——如果你写了 `format: "pdf"`，即便路径是 `index.html`，也会按 PDF 导出。这种「显式覆盖自动」的设计在 Typst 里非常常见（`Smart<T>` 就是为此而生）。

#### 4.1.2 核心流程

格式推断可以用下面这段伪代码概括：

```
fn determine_format(document, styles):
    # 第 1 步：读样式链里的 format 字段（它可能是被 set document(format:) 改过的）
    smart = document.format.get(styles)        # Smart<DocumentFormat>
    # 第 2 步：如果显式指定了（Custom），直接用
    if smart 是 Custom(fmt):
        return fmt
    # 第 3 步：否则按路径扩展名推断
    fmt = determine_format_from_path(document.path)
    if fmt 存在:
        return fmt
    # 第 4 步：两条路都失败，报错
    error("unknown document format", hint="try specifying the `format` explicitly")
```

注意第 2 步用的是 `.custom()`，它只在 `Smart::Custom` 时返回 `Some`，`Auto` 时返回 `None`，正好实现「显式优先」。`.or_else(...)` 则把「推断」作为兜底接在后面。

#### 4.1.3 源码精读

`compile_document` 的第一步就是确定格式：

[crates/typst-bundle/src/lib.rs:294](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L294)：用 `.at(document.span())` 把「没有上下文位置」的推断结果转换成「带源码位置」的 `SourceResult`，这样报错能指向正确的 `document` 元素位置。

真正的推断逻辑在 `DocumentElem` 上：

[crates/typst-library/src/model/document.rs:191-205](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L191-L205)：`determine_format` 的实现。它先取 `self.format.get(styles)`（注意是从**样式链**读，意味着 `set document(format: ...)` 能覆盖），再 `.custom()` 判断是否显式，`.or_else` 兜底走 `determine_format_from_path`，最后 `.ok_or_else` 在全失败时报 `unknown document format`。

兜底的路径推断：

[crates/typst-library/src/model/document.rs:209-217](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L209-L217)：`determine_format_from_path` 按扩展名做 `match`。`pdf`/`svg`/`png` 三种映射到 `PagedFormat`（再 `into()` 成 `DocumentFormat::Paged`），`html` 直接映射到 `DocumentFormat::Html`，其它扩展名返回 `None`。

这里涉及两个枚举，理解它们的关系很重要：

- `PagedFormat`（[crates/typst-library/src/model/document.rs:290-298](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L290-L298)）：`Pdf` / `Png` / `Svg`，仅描述分页（位图/矢量/打印）格式。
- `DocumentFormat`（[crates/typst-library/src/model/document.rs:255-260](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L255-L260)）：`Paged(PagedFormat)` 或 `Html`，是 bundle 里**文档级别**的格式。它比 `PagedFormat` 多了 HTML 这一非分页格式。

也就是说，用户在 `format:` 里写 `"pdf"` 时，经过 `cast!`（见 [crates/typst-library/src/model/document.rs:277-286](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L277-L286)）会先变成 `PagedFormat::Pdf`，再包成 `DocumentFormat::Paged(PagedFormat::Pdf)`。写 `"html"` 则直接是 `DocumentFormat::Html`。

#### 4.1.4 代码实践

**实践目标**：验证「显式 `format:` 覆盖路径扩展名」的优先级。

**操作步骤**（源码阅读型 + 可选本地验证）：

1. 阅读上面的 `determine_format` 与 `determine_format_from_path`，预测下面两个文档各导出成什么格式：

   ```typ
   #document("index.html", format: "pdf")[
     会被当成 PDF 处理
   ]

   #document("cover.svg")[
     没写 format，按扩展名推断为 SVG
   ]
   ```

2. 如果你有本地 typst 编译环境（`--features bundle`），编译后看输出：第一个应产出 `index.html`（注意：**文件名仍是 `index.html`**，但内容是 PDF 字节流，因为 path 与 format 是两回事），第二个产出 `cover.svg`。

**预期结果**：`format:` 决定**导出编码格式**，`path` 只决定**文件落在哪个位置、叫什么名字**。二者独立——这正是「显式覆盖推断」的体现。

> 如果你无法运行，明确标注为「待本地验证」，但你对分支走向的判断应基于源码，而非猜测。

#### 4.1.5 小练习与答案

**练习 1**：`document("data.json")` 会导出成什么格式？

**答案**：会报 `unknown document format`。因为 `.json` 不在 `determine_format_from_path` 的 match 里（只支持 pdf/svg/png/html），且没有显式 `format:`，于是 `.ok_or_else` 触发报错，提示 `try specifying the format explicitly`。

**练习 2**：用户既没写 `format:`，又给了一个没有扩展名的路径 `document("readme")`，会发生什么？

**答案**：`path.extension()` 返回 `None`，`determine_format_from_path` 用 `?` 提前返回 `None`，最终同样报 `unknown document format`。

---

### 4.2 目标切换与样式链：TargetElem::target.set().wrap()

#### 4.2.1 概念说明

这是本讲最微妙、也最关键的设计点。

整个 bundle 编译的目标是 `Target::Bundle`。但 bundle 里的每份文档，其实要么是分页文档（PDF/SVG/PNG），要么是 HTML——它们的排版/渲染逻辑完全不同，分别属于 `Target::Paged` 和 `Target::Html` 世界。如果排版时仍然把目标当成 `Bundle`，那么那些依赖 `target()` 来分支的 show 规则、realize 规则就会全部走错分支。

所以 `compile_document` 必须在编译每个子文档前，把「当前目标」临时切到 `Paged` 或 `Html`。Typst 用样式链（style chain）来实现这种「临时切换」：往样式链最前面塞一条「把 `TargetElem.target` 设为 X」的样式，后续所有读取目标的地方都会读到这个新值。

> 类比：样式链像一层层的透明覆盖膜。`compile_document` 在最上面盖一张写着「target = Paged」的膜，于是这份文档内部看到的目标就是 Paged；文档之外的 bundle 上下文依然看到 Bundle。

#### 4.2.2 核心流程

```
fmt = determine_format(...)                    # DocumentFormat
new_target = fmt.target()                      # Paged(_) -> Target::Paged; Html -> Target::Html
style_entry = TargetElem::target.set(new_target)   # 一条「设置 target 字段」的属性
style_entry = style_entry.wrap()               # 包装成 LazyHash<Style>，可挂上样式链
local_styles = styles.chain(&style_entry)      # 在原 styles 前面挂上这条
# 之后 layout/html 编译都用 local_styles
```

需要特别说明「谁转成谁」：`DocumentFormat::target()`（[crates/typst-library/src/model/document.rs:262-269](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L262-L269)）负责把文档格式映射成编译目标——`Paged(_)` → `Target::Paged`，`Html` → `Target::Html`。

#### 4.2.3 源码精读

`compile_document` 里负责目标切换的就这两行：

[crates/typst-bundle/src/lib.rs:295-296](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L295-L296)：

- `TargetElem::target.set(format.target())`：`TargetElem::target` 是 `#[elem]` 宏为 `TargetElem` 生成的字段访问器，`.set(v)` 生成一条「把该字段设为 v」的 `Property`。
- `.wrap()`：把 `Property` 包成 `LazyHash<Style>`（见 [crates/typst-library/src/foundations/styles.rs:361-363](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/styles.rs#L361-L363)，`Property::wrap` → `Style::Property(self).wrap()`）。
- `styles.chain(&target)`：把这条样式挂到链头。`LazyHash<Style>` 实现了 `Chainable`（见 [crates/typst-library/src/foundations/styles.rs:794-799](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/styles.rs#L794-L799)），`chain` 把它作为 `head`（一个长度为 1 的切片），原来的 `styles` 作为 `tail`。

承载目标的元素本身：

[crates/typst-library/src/foundations/target.rs:87-91](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/target.rs#L87-L91)：`TargetElem` 是一个「只为承载 `target` 样式字段而存在、从不被用户构造」的元素。用户侧的 `target()` 函数（[crates/typst-library/src/foundations/target.rs:134-137](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/target.rs#L134-L137)）就是读 `context.styles()?.get(TargetElem::target)`。

一个重要的执行顺序细节：`determine_format(styles)` 用的是**原始** `styles`（第 294 行，在目标切换**之前**），而目标切换后的 `local_styles` 才喂给后面的 layout/html 编译。这没问题，因为 `determine_format` 读的是 `DocumentElem::format` 字段，与 `TargetElem::target` 是两个互不相干的字段。

#### 4.2.4 代码实践

**实践目标**：体会「目标切换」如何让同一份内容在不同文档里表现出不同形态。

**操作步骤**：

1. 在源码里把这条调用链标注出来：`compile_document` 第 295 行 `TargetElem::target.set(...)` → `chain` → 下游 `layout_document_for_bundle` 内部任何 `target()` / `Target::is_html()` 分支。

2. （可选本地验证）构造一个对目标敏感的内容，放进 bundle 的两份文档里：

   ```typ
   #let logo = context {
     if target() == "html" { [HTML 版本] }
     else { [分页版本] }
   }

   #document("a.html")[ #logo ]
   #document("b.pdf")[ #logo ]
   ```

**预期结果**：同一个 `logo` 内容，在 `a.html` 里应渲染为「HTML 版本」，在 `b.pdf` 里应渲染为「分页版本」。这正说明目标切换生效——`logo` 内的 `target()` 在两份文档里分别读到 `Html` 与 `Paged`，而非外层的 `Bundle`。

> 无法本地编译时标注「待本地验证」，但分支判断依据是源码中 `target()` 读取 `TargetElem::target` 这一事实。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉第 295-296 行的目标切换，bundle 里一份 HTML 文档会怎样？

**答案**：layout/html 编译时 `TargetElem::target` 仍是 `Bundle`，依赖 `target()` 分支的 show 规则（以及 typst-html 的目标判断）会走错分支，HTML 文档无法正确按 HTML 目标编译，行为不符合预期。这正是目标切换存在的原因。

**练习 2**：为什么 `set(...)` 之后必须 `.wrap()` 再 `.chain()`，而不能直接用？

**答案**：`set` 返回的是裸 `Property`，而样式链需要的是可 `Chainable` 的 `LazyHash<Style>`。`.wrap()` 完成这层包装（并计算懒哈希），`.chain()` 才能把单条样式挂到链头。

---

### 4.3 分派到排版：paged 走 layout，html 走 html

#### 4.3.1 概念说明

格式确定、目标切换完之后，`compile_document` 用一个 `match format` 把工作分派给两个「兄弟 crate」：

- `DocumentFormat::Paged(format)`：交给 `typst_layout::layout_document_for_bundle` 做分页排版，返回 `PagedDocument`，再连同 `format` 一起包成 `BundleDocument::Paged(doc, PagedExtras)`。
- `DocumentFormat::Html`：交给 `typst_html::html_document_for_bundle` 做网页编译，返回 `HtmlDocument`，包成 `BundleDocument::Html(doc)`。

bundle 自身不实现任何排版或字节编码，它只是编排层——这一点在 u1-l1 已经强调过，本处分派逻辑就是这一设计的直接体现。

#### 4.3.2 核心流程

```
match format:
  DocumentFormat::Paged(format):
      doc = typst_layout::layout_document_for_bundle(engine, &document.body, locator, styles)
      # 见 4.4：PNG/SVG 在此做单页校验
      return BundleDocument::Paged(doc, PagedExtras { format, anchors: Vec::new() })

  DocumentFormat::Html:
      # 见 4.4：先校验 Feature::Html 是否开启
      doc = typst_html::html_document_for_bundle(engine, &document.body, locator, styles)
      return BundleDocument::Html(doc)
```

注意两个分支返回的容器不同：

- Paged 分支额外带了一个 `PagedExtras { format, anchors: Vec::new() }`。`format` 记住「这份分页文档最终要导出成 PDF 还是 SVG 还是 PNG」（因为 `PagedDocument` 本身不携带这个信息）；`anchors` 暂时为空，留待后续 `create_link_anchors`（u5-l2）填入跨文档链接锚点。HTML 分支则不需要 `PagedExtras`。

#### 4.3.3 源码精读

`compile_document` 的整体 `match` 结构：

[crates/typst-bundle/src/lib.rs:297-339](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L297-L339)：`Ok(match format { ... })`。两个分支分别处理 Paged 与 Html。

Paged 分派：

[crates/typst-bundle/src/lib.rs:299-304](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L299-L304)：调用 `typst_layout::layout_document_for_bundle`。它的入口在 [crates/typst-layout/src/pages/mod.rs:78-95](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-layout/src/pages/mod.rs#L78-L95)，签名接收 `engine`、`content`、`locator`、`styles`，返回 `PagedDocument`；内部转给一个 `#[comemo::memoize]` 的 `layout_document_for_bundle_impl`（与本 crate 的 `bundle_impl` 一样采用「外壳 + 记忆化内核」模式）。

HTML 分派：

[crates/typst-bundle/src/lib.rs:331-336](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L331-L336)：调用 `typst_html::html_document_for_bundle`。入口在 [crates/typst-html/src/document.rs:78-95](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-html/src/document.rs#L78-L95)，结构与 layout 版对称：外壳转交 `#[comemo::memoize]` 的 `html_document_for_bundle_impl`。

两个分派目标都接收 `&document.body`（文档的内容）、`locator`（定位器，来自 collect 阶段分配，用于内省）、以及目标切换后的 `styles`。

#### 4.3.4 代码实践

**实践目标**：把四种扩展名与两条分派分支对应起来。

**操作步骤**（源码阅读型）：

按下表，对照 `determine_format_from_path` 与 `compile_document` 的 `match`，填出每个文档走过的路径：

| 用户写法 | `determine_format` 结果 | `match format` 分支 | 分派函数 | 返回容器 |
| --- | --- | --- | --- | --- |
| `document("a.pdf")[...]` | ？ | ？ | ？ | ？ |
| `document("b.svg")[...]` | ？ | ？ | ？ | ？ |
| `document("c.png")[...]` | ？ | ？ | ？ | ？ |
| `document("d.html")[...]` | ？ | ？ | ？ | ？ |

**预期结果**（参考答案）：

| 写法 | 格式 | 分支 | 分派函数 | 容器 |
| --- | --- | --- | --- | --- |
| `.pdf` | `Paged(Pdf)` | `DocumentFormat::Paged` | `layout_document_for_bundle` | `Paged(doc, PagedExtras{format:Pdf,...})` |
| `.svg` | `Paged(Svg)` | `DocumentFormat::Paged` | `layout_document_for_bundle` | `Paged(doc, PagedExtras{format:Svg,...})` |
| `.png` | `Paged(Png)` | `DocumentFormat::Paged` | `layout_document_for_bundle` | `Paged(doc, PagedExtras{format:Png,...})` |
| `.html` | `Html` | `DocumentFormat::Html` | `html_document_for_bundle` | `Html(doc)` |

关键观察：**四种里前三种都走同一个 `layout_document_for_bundle`**，区别只在于 `PagedExtras.format` 不同（导出阶段再据此分派编码，见 u4-l2）；只有 HTML 走另一条分支。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `.pdf` / `.svg` / `.png` 在 `compile_document` 里走的是同一条分派，而不是三条？

**答案**：因为它们都属于 `DocumentFormat::Paged`，排版阶段产出的都是 `PagedDocument`，排版逻辑不区分最终编码。真正区分 PDF/SVG/PNG 字节编码的是**导出阶段**（u4），`compile_document` 只在 `PagedExtras.format` 里把这个信息「记住」，供导出时使用。

**练习 2**：`PagedExtras` 为什么需要单独存 `format`，而 HTML 分支不需要对应物？

**答案**：`PagedDocument` 自身不携带「最终导出成 PDF 还是 PNG」的信息（它只是排版结果），所以必须由 `PagedExtras.format` 记下来；而 `HtmlDocument` 隐含就是 HTML，没有其它格式可选，无需额外字段。

---

### 4.4 PNG/SVG 单页约束与 HTML feature 门控

#### 4.4.1 概念说明

`compile_document` 在分派前后还埋了两个校验，分别针对两类约束：

1. **PNG / SVG 的单页约束**：位图（PNG）和单文件矢量（SVG）只能表达一页。如果一份 `.png` / `.svg` 文档排版出了多页，Typst 无法把它们塞进一个文件，于是报错。
2. **HTML 的特性门控**：HTML 导出本身也是实验性特性，需要 `--features html` 开启。在 bundle 里编译 HTML 文档时，必须先确认这个特性已启用。

这两个校验分别用 `delayed_error`（单页约束）和 `bail!`（特性门控）上报，体现了 u2-l1 讲过的「立刻致命 vs 延迟上报」分野——这里会再次看到它们的区别。

#### 4.4.2 核心流程

```
Paged 分支内（排版完成后）:
    num_pages = doc.pages().len()
    if num_pages != 1 且 format 是 Png 或 Svg:
        engine.sink.delayed_error("expected document to have a single page", ...)

Html 分支内（编译前）:
    if not engine.library.features.is_enabled(Feature::Html):
        bail!("html export is only available when the `html` feature is enabled", ...)
```

为什么单页约束用 `delayed_error` 而非 `bail!`？因为页数会随内省收敛而变化——一个文档在这一轮可能是多页，下一轮（链接锚点确定后重新排版）可能变成单页。用 `delayed_error` 把它推迟到收敛末轮，避免「假阳性」误报。这与 u2-l1 中「路径重复」用 `delayed_error` 的理由完全一致。

而 HTML 特性门控是「环境配置」问题，不会随内省改变，所以用 `bail!` 立刻致命。

#### 4.4.3 源码精读

单页约束：

[crates/typst-bundle/src/lib.rs:306-314](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L306-L314)：排版后取 `doc.pages().len()`，若 `!= 1` 且 `matches!(format, PagedFormat::Png | PagedFormat::Svg)`，则 `engine.sink.delayed_error(...)`。注意是 `delayed_error`（推入 sink，延迟到收敛末轮才致命），不是 `return Err`。错误文本为 `expected document to have a single page`，并带两条 hint：`the document resulted in {num_pages} pages` 与 `documents exported to an image format only support a single page`。

Paged 分支最后封装（即使在报错后也继续构造，便于后续统一处理）：

[crates/typst-bundle/src/lib.rs:316-319](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L316-L319)：返回 `BundleDocument::Paged(Box::new(doc), PagedExtras { format, anchors: Vec::new() })`。

HTML 特性门控：

[crates/typst-bundle/src/lib.rs:322-329](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L322-L329)：若 `!engine.library.features.is_enabled(Feature::Html)`，则 `bail!(...)` 立刻致命，提示 `html export is only available when the html feature is enabled`，并给出开启命令 hint `--features bundle,html`。

特性开关的定义：

[crates/typst-library/src/lib.rs:270-277](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/lib.rs#L270-L277)：`Feature` 枚举包含 `Html`、`Bundle`、`A11yExtras`。`is_enabled` 的实现在 [crates/typst-library/src/lib.rs:255-257](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/lib.rs#L255-L257)，查一个位集是否包含对应位。

#### 4.4.4 代码实践

**实践目标**：观察单页约束报错与 HTML 特性门控的不同表现。

**操作步骤**：

1. **多页 PNG**：构造一个会排版成两页的 `.png` 文档（用 `#pagebreak()` 强制分页）：

   ```typ
   #document("two.png")[
     第一页
     #pagebreak()
     第二页
   ]
   ```

   如果有本地编译环境（`--features bundle`），编译它，观察报错。

2. **HTML 门控**：写一个 `.html` 文档，但只开 `--features bundle`、**不开** `html`：

   ```typ
   #document("index.html")[ hello ]
   ```
   用 `typst compile --features bundle index.typ`（不带 `,html`）编译，观察报错。

**预期结果**：

- 多页 PNG：按源码文本，应报 `expected document to have a single page`，并提示页数与「image format only support a single page」。由于是 `delayed_error`，它在收敛末轮才升级为致命（实践中通常表现为编译结束时一次性报出）。
- HTML 门控：应报 `html export is only available when the html feature is enabled`，并给出 `--features bundle,html` 提示。由于是 `bail!`，它是**立刻致命**的。

> 两条命令的具体输出请以本地实际运行为准；上面引用的错误文本直接取自源码，未运行时标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：多页的 `.pdf` 文档会触发单页约束吗？

**答案**：不会。约束条件是 `matches!(format, PagedFormat::Png | PagedFormat::Svg)`，PDF 不在其中。PDF 天然支持多页，所以 `document("book.pdf")` 可以是任意页数。

**练习 2**：为什么单页约束用 `delayed_error`，而 HTML 门控用 `bail!`？

**答案**：页数是内省依赖量，会随收敛轮次变化，可能本轮多页、下轮变单页，故需延迟到收敛末轮再判定，避免假阳性；而「html 特性是否开启」是编译开始时就确定的配置，不会因内省改变，故应立即失败。这正是 u2-l1 引入的「延迟上报 vs 立刻致命」原则的两个实例。

---

## 5. 综合实践

把本讲的「格式推断 + 目标切换 + 分派 + 校验」串起来。

**任务**：写一个最小 bundle，包含四份文档（扩展名 `.pdf` / `.svg` / `.png` / `.html` 各一），逐一说明每份文档在 `compile_document` 里走了哪条路径。

```typ
#document("a.pdf")[
  PDF 文档：可多页。
]

#document("b.svg")[
  SVG 文档：只能单页。
]

#document("c.png")[
  PNG 文档：只能单页。
]

#document("d.html", title: [网页])[
  HTML 文档：需要 html 特性。
]
```

**步骤**：

1. 对每份文档，按本讲 4.3 的表格，写出：`determine_format` 的结果 → `match format` 分支 → 分派函数 → 返回容器。
2. 在 `d.html` 一行旁注明：进入 `Html` 分支前会先过 `Feature::Html` 门控；在 `b.svg` / `c.png` 旁注明：排版后会过单页约束校验。
3. （可选）在 `c.png` 的内容里加一个 `#pagebreak()` 制造第二页，重新编译，确认会报 `expected document to have a single page`。
4. 在源码 [crates/typst-bundle/src/lib.rs:288-340](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L288-L340) 上，用注释把上述四份文档分别指向 `compile_document` 内对应的代码行。

**预期产出**：一张「文档 → 走向」的对应表，以及对两个校验点位置（单页约束、HTML 门控）的说明。若本地无法编译，则纯做源码标注与走向推理，并明确标注「待本地验证」。

---

## 6. 本讲小结

- `compile_document` 是 bundle 并行编译每个 `#document(...)` 的最小执行单元，接收 `engine`、`document`、`styles`、`locator`。
- 格式推断遵循「显式 `format:` 优先、否则按路径扩展名推断」：`determine_format` 用 `Smart::custom()` + `or_else(determine_format_from_path)` 实现，全失败时报 `unknown document format`。
- 目标切换通过 `TargetElem::target.set(format.target()).wrap()` 往样式链注入一条「把目标设为 Paged/Html」的样式，使子文档内的 `target()` 读到正确目标，而非外层的 Bundle。
- 分派用 `match format`：`Paged` 走 `layout_document_for_bundle` 返回 `BundleDocument::Paged(doc, PagedExtras)`；`Html` 走 `html_document_for_bundle` 返回 `BundleDocument::Html(doc)`。PDF/SVG/PNG 共享同一条分派，差异留到导出阶段。
- 两个校验：PNG/SVG 单页约束用 `delayed_error`（页数会随内省变化），HTML 导出用 `bail!`（特性开关是静态配置）——对应「延迟上报 vs 立刻致命」两种错误通道。
- `PagedExtras.format` 记住「分页文档最终导出成哪种编码」，为后续导出阶段（u4）埋下伏笔；`anchors` 暂空，待 `create_link_anchors`（u5-l2）填充。

---

## 7. 下一步学习建议

- 下一讲 u3-l1 会落到 `compile_document` 产出的数据模型：`BundleDocument`、`PagedExtras`、`Bundle`、`BundleFile` 是如何组织的，建议顺读。
- 如果想立刻看到这些 `BundleDocument` 如何被序列化成字节文件，可跳读 u4 的 `export.rs`（导出阶段才真正按 `PagedExtras.format` 分派 PDF/SVG/PNG 编码）。
- 想深入「目标切换」的下游效果，可阅读 `typst-layout` 与 `typst-html` 中依赖 `Target` / `target()` 的 realize 规则，体会切到 Paged/Html 后规则分发的差异。
- 想理解 `delayed_error` 与内省收敛的关系，回顾 u2-l1 的 collect 一节，并预习 u5-l1 的 `BundleIntrospector`。
