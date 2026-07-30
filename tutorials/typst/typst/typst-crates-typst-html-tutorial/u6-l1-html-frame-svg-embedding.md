# html.frame 与 SVG 嵌入

## 1. 本讲目标

typst-html 的默认策略是把 Typst 内容「语义化」地翻译成 HTML 标签（如 `#strong[..]` → `<strong>`）。但有些内容——图表、精确定位的图形、复杂叠放——一旦翻译成 HTML 就会失真。`html.frame` 就是为此而生的「保真逃生舱」：它把内容交给和 PDF/SVG/PNG 完全相同的排版引擎去布局，然后把排版结果以**内联 SVG** 嵌进 HTML。

学完本讲，你应当能够：

1. 说出 `html.frame`（`FrameElem`）在用户脚本层、转换层（`handle`）、DOM 层（`HtmlFrame`）、编码层（`write_frame`）四个位置分别发生了什么，把它们串成一条完整链路。
2. 解释 `FrameElem` 为什么要**临时把 `Target` 切回 `Paged`** 来排版，以及为什么它本身又被注册为 Paged 目标下的 no-op。
3. 说明 `HtmlFrame` 的五个字段（`inner` / `text_size` / `id` / `css` / `anchors`）各自的用途，尤其是 `text_size` 如何让 SVG 用 em 单位随字号缩放、`anchors` 如何让文档内链接跳进 SVG 内部。
4. 理解 `write_frame` 的「先生成 SVG、再补缩进」设计为何有利于缓存复用，以及 `html_span_filled` 这个 HTML 专用着色工具为何是一个临时方案。

## 2. 前置知识

本讲是专家层讲义，默认你已经读过：

- **u3-l3 convert_to_nodes 内容转换器**：知道 `handle()` 是类型探测链，逐个把具象化的 `Content` 翻译成 `HtmlNode`；本讲的 `FrameElem` 分支正是这条链上的一环。
- **u5-l1 DOM 到 HTML 字符串的编码**：知道 `write_node` 按 `HtmlNode` 四变体（`Tag` / `Text` / `Element` / `Frame`）分派，其中 `Frame` 变体由 `write_frame` 处理。
- **u2-l1 HTML DOM 数据模型总览**：知道 `HtmlNode::Frame(HtmlFrame)` 是 DOM 节点的四种变体之一，本身不产生 HTML 标签，而是承载一段已排版的帧。

几个本讲会用到的关键术语，先用一句话复习：

- **`Target`**：编译目标枚举（`Paged` / `Html` / `Bundle`），决定 show 规则查哪张表。HTML 导出整体处于 `Target::Html`。
- **`Frame`**：Typst 排版引擎的产物，一棵带绝对坐标（单位 pt）的「绘图树」，PDF/SVG/PNG 导出都从它出发。
- **内联 SVG**：直接写在 HTML 里的 `<svg>...</svg>`，不需要外部图片文件，浏览器原地渲染。
- **em 单位**：CSS 中的相对长度，`1em` 等于当前元素的字号；用 em 而不是 px，元素会随字号一起放大缩小。

## 3. 本讲源码地图

本讲涉及四个 `typst-html` 源文件，外加一个兄弟 crate 的函数：

| 文件 | 作用 |
| --- | --- |
| `src/lib.rs` | 定义用户侧元素 `FrameElem`（`html.frame`），并在 `module()` 里注册它。 |
| `src/convert.rs` | `handle()` 中的 `FrameElem` 分支：切 Target、调排版、包成 `HtmlFrame`、设为块级。 |
| `src/dom.rs` | `HtmlFrame` 结构体与 `HtmlFrame::new`，承载排版结果及 `text_size` / `id` / `css` / `anchors`。 |
| `src/encode.rs` | `write_frame`：把 `HtmlFrame` 交给 `typst_svg::svg_in_html` 生成 SVG，并做缩进后处理。 |
| `crates/typst-svg/src/lib.rs`（兄弟 crate） | `svg_in_html`：真正把 `Frame` 渲染成 SVG 字符串，并用 `text_size` 折算 em 尺寸。 |

此外会顺带引用 `src/rules.rs`（`FrameElem` 的 Paged no-op 规则与 `html_span_filled`）、`src/link.rs`（`traverse_frame` 填充 `anchors`）。

## 4. 核心概念与源码讲解

### 4.1 FrameElem：用户侧的「保真逃生舱」

#### 4.1.1 概念说明

当 Typst 内容被导出为 HTML 时，绝大多数元素都会被翻译成语义化的 HTML 标签。但有些内容「依赖精确的定位与样式来传达信息」——比如一个精心摆放的图表——一旦拆成 HTML 标签，布局就崩了。

`html.frame` 提供了另一条路：**不翻译，而是排版**。它把内部内容交给和 PDF/SVG/PNG 完全相同的排版引擎，渲染得「和分页导出一模一样」，再以内联 SVG 嵌入 HTML。于是你可以在 HTML 文档里嵌进一段「像素级保真」的 Typst 排版结果。

`FrameElem` 的定义极其简单——它只持有一份待排版的 `body`：

[FrameElem 定义 — src/lib.rs:L136-L142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L136-L142)　这里 `body` 是 `#[positional] #[required]` 的 `Content`，即用户写的 `html.frame[ ... ]` 方括号里的内容。注意它本身不带任何「怎么排版」的参数——排版细节完全沿用 `body` 自身的样式。

> 顺带一提，`html.frame` 自 `0.13.0` 引入（见 `#[elem(since = "0.13.0")]`），与 `html.elem` 同期。它和 `html.elem` 分工明确：`html.elem` 语义优先（手写标签），`html.frame` 保真优先（排版成图）。这两个元素都在 `module()` 里注册：[module() 注册 — src/lib.rs:L34-L41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L34-L41)。

#### 4.1.2 核心流程

从用户脚本到最终 SVG，`FrameElem` 要走过四个阶段：

```
html.frame[ body ]                      ← 用户脚本（FrameElem）
        │  handle() 的 FrameElem 分支
        ▼
切换 Target=Paged → layout_frame(body)  ← 用分页排版引擎布局
        │
        ▼
HtmlFrame { inner, text_size, ... }     ← 包成 DOM 节点（HtmlNode::Frame）
        │  write_frame()
        ▼
typst_svg::svg_in_html(inner, ...)      ← 渲染成内联 <svg> 字符串
```

下面三节（4.2 / 4.3 / 4.4）分别拆开看后三个阶段。

### 4.2 handle 中的 FrameElem 分支：切换 Target 并排版

#### 4.2.1 概念说明

在 u3-l3 里我们看过 `handle()` 是一条「按元素类型分派」的探测链。当探测到 `FrameElem` 时，要做的事情很特别：**当前整个文档处于 `Target::Html`，但这块内容需要走分页排版**。因此这一分支的核心动作是「临时把 Target 切回 `Paged`，再调排版引擎」。

为什么必须切回 `Paged`？因为 show 规则是按 `(元素, Target)` 查表的。如果不切 Target，`body` 内部的元素仍会按 HTML 规则翻译成标签，那就不是「保真排版」了。切到 `Paged` 后，`body` 就会走和 PDF 完全一样的分页 show 规则，从而得到一份「真正的 `Frame`」。

#### 4.2.2 核心流程

[FrameElem 分支 — src/convert.rs:L140-L154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L140-L154)　关键步骤：

1. `converter.locator.next(&elem.span())`：为这块独立内容分配一个定位子节点（与 u3-l4 讲的 `SplitLocator` 一致）。
2. `TargetElem::target.set(Target::Paged).wrap()`：构造一条「把 Target 设为 Paged」的样式，挂到样式链上。这就是「切 Target」的实现——它不是函数参数，而是注入样式链，让下游 show 规则读到。
3. `(engine.library.routines.layout_frame)(...)`：调用核心排版例程，在**无限大区域** `Region::new(Size::splat(Abs::inf()), Axes::splat(false))` 里布局 `body`，得到一个 `Frame`。无限大区域意味着帧会取内容的自然尺寸（不强制分页、不强制拉伸）。
4. `HtmlFrame::new(frame, styles, elem.span())`：把 `Frame` 连同当前样式（用于读 `text_size`）和 span 包成 DOM 节点。
5. `make_block_level(&mut node).unwrap()`：默认设为块级（见下文）。

#### 4.2.3 源码精读：为什么 FrameElem 在 Paged 下是 no-op

注意第 2 步把 Target 切到了 `Paged`。这立刻引出一个问题：如果用户写了 `show math.equation: html.frame`，那么 `html.frame` 的 `body`（数学公式）在 Paged 排版时，又会遇到 `math.equation` 的 show 规则——万一那条规则又是 `html.frame`，就会无限嵌套。

typst-html 的解法是：**把 `FrameElem` 自己注册成 Paged 目标下的 no-op（直接返回 body）**。

[FrameElem 的 Paged no-op 规则 — src/rules.rs:L88-L91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L88-L91)　这条规则 `|elem, _, _| Ok(elem.body.clone())` 直接把 `body` 透传，等价于「在分页排版眼里，`html.frame` 是透明的」。于是嵌套的 `html.frame` 在内层排版时被剥掉，不会无限递归。这也是 u3-l5 提到的「`FrameElem` 是唯一注册在 `Target::Paged` 而非 `Html` 上的规则」的原因。

> 反差感：对 HTML 目标，`html.frame` 是「重武器」（触发整条 SVG 链路）；对 Paged 目标，它却是「隐身衣」（原样透传）。同一个元素，两个 Target 下语义截然不同。

#### 4.2.4 源码精读：默认块级

[make_block_level — src/convert.rs:L444-L486](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L444-L486)　其中 `HtmlNode::Frame(_) => Some(property::Display::Inline)` 说明帧的默认 display 是 `Inline`，于是落到提升分支被改成 `Some(property::Display::Block)`，最终经 `set_display` 写进 `frame.css`。源码注释点明：「A frame is block-level by default like a Typst `image`」（帧默认是块级，就像 Typst 的 `image`）。若用户用 `#box` 包裹 `html.frame`，则走 `make_inline_level` 路径、不写 `display`，让帧渲染为行内。

#### 4.2.5 代码实践

**实践目标**：确认 `html.frame` 默认块级、且 `body` 走的是分页排版而非 HTML 翻译。

**操作步骤**（需要本地有 `typst` CLI，版本 ≥ 0.13.0）：

1. 新建 `demo.typ`：

   ```typ
   #html.elem("p")[
     前文 #html.frame[
       #text(fill: red)[红] #text(fill: blue)[蓝] \ 第二行
     ] 后文
   ]
   ```

2. 编译为 HTML：`typst compile --format html demo.typ demo.html`（或 `typst compile demo.html`，靠 `.html` 后缀自动判定格式，见 u1-l3）。

3. 打开 `demo.html` 查看。

**需要观察的现象**：

- `html.frame` 内部出现一个内联 `<svg>`，其中的「红/蓝」是 SVG 文本，而不是 `<span>`——说明走了排版+SVG，而非 HTML 翻译。
- 该 `<svg>` 的 `style` 里带 `display: block`（来自 4.2.4 的默认块级），并会带 `width`/`height` 的 em 数值（见 4.4）。
- 「前文」「后文」与 SVG 不在同一行，印证块级。

> 如果无法本地运行，可改为「源码阅读型实践」：在 [convert.rs:L140-L154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L140-L154) 中找到 `Target::Paged` 与 `Abs::inf()` 两处，说明它们各自的作用。

#### 4.2.6 小练习与答案

**练习 1**：如果把 `Region::new(Size::splat(Abs::inf()), Axes::splat(false))` 里的 `Abs::inf()` 换成一个固定的小宽度（如 `100pt`），`html.frame` 的输出会有什么不同？

> **参考答案**：区域不再无限宽，`body` 会在 `100pt` 处折行/分页，生成的 `Frame`（以及最终 SVG）会变窄变高，外观接近「窄列排版」，而非取内容自然宽度。

**练习 2**：为什么 `FrameElem` 的 no-op 规则必须注册在 `Target::Paged`，而不是 `Target::Html`？

> **参考答案**：在 `Target::Html` 下，`FrameElem` 走的是 `handle()` 的专门分支（切 Target、排版、生成 SVG），不需要也不经过 show 规则表；no-op 规则是为了解决「`body` 在 Paged 排版时再次遇到 `html.frame`」的嵌套问题，所以只有 Paged 侧需要它。

### 4.3 HtmlFrame：承载排版结果的 DOM 节点

#### 4.3.1 概念说明

排版得到的 `Frame` 不能直接塞进 `HtmlNode::Element`——它不是标签，而是一张「位图般的绘图树」。所以 typst-html 专门设了 `HtmlNode::Frame(HtmlFrame)` 变体（见 u2-l1），用 `HtmlFrame` 这层结构把 `Frame` 与若干「编码时才用得上的元信息」打包在一起。

#### 4.3.2 核心流程：五个字段各司其职

[HtmlFrame 结构体 — src/dom.rs:L504-L521](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L504-L521)　逐字段说明：

| 字段 | 类型 | 用途 | 何时被填 |
| --- | --- | --- | --- |
| `inner` | `Frame` | 真正要渲染成 SVG 的排版结果 | `handle` 分支里 `layout_frame` 的返回值 |
| `text_size` | `Abs` | 定义帧时的字号，用于把尺寸折算成 em | `HtmlFrame::new` 里 `styles.resolve(TextElem::size)` |
| `id` | `Option<EcoString>` | 整个 `<svg>` 元素的 id | `create_link_anchors`（帧本身被链接时） |
| `css` | `css::Properties` | 编译器生成的 CSS（如 `display: block`） | `make_block_level` / `set_display` |
| `anchors` | `EcoVec<(Point, EcoString)>` | SVG 内部各跳转点的 `(坐标, id)` | `traverse_frame`（帧内某元素被链接时） |
| `span` | `Span` | 源码定位，用于报错 | `handle` 分支传入 |

[HtmlFrame::new — src/dom.rs:L523-L535](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L523-L535)　注意它把 `text_size` 初始化为 `styles.resolve(TextElem::size)`——也就是 **`html.frame` 调用点处的字号**，而 `id` / `css` / `anchors` 此刻都还是空的，要等后续阶段（display 提升、链接锚点分配）来填。这说明 `HtmlFrame` 是一个「随流水线逐步完善」的可变结构。

#### 4.3.3 源码精读：text_size 为什么重要

`text_size` 的字段注释写得很清楚：「This is used to size the frame with em units to make text in and outside of the frame sized consistently.」——它用来**以 em 单位给帧定尺寸，让帧内外的文字大小保持一致**。

直觉上：排版结果 `inner` 的宽高是绝对值（pt）。但如果 SVG 直接写 `width="100pt"`，那么当浏览器/用户改变字号时，这块 SVG 不会跟着缩放，就会和周围 HTML 文字的相对比例失调。解决办法是把绝对尺寸**除以 `text_size`** 折算成 em：

\[
\text{width}_{\text{em}} = \frac{\text{frame.width()}}{\text{text\_size}}, \qquad
\text{height}_{\text{em}} = \frac{\text{frame.height()}}{\text{text\_size}}
\]

由于帧是用 `text_size` 这个字号排出来的，除回去正好得到「以该字号为 1em」的相对尺寸。于是当外层 HTML 字号 = `text_size` 时，SVG 显示尺寸 = 帧的真实尺寸；当用户放大字号，SVG 也按比例放大，帧内外文字的视觉比例始终自洽。具体的除法发生在 4.4 节的 `svg_in_html` 里。

#### 4.3.4 源码精读：anchors 如何被填充

`anchors` 字段一开始是空的，它在「链接锚点分配」阶段（u5-l4）才被写入。入口在 `create_link_anchors` → `traverse` → 遇到 `HtmlNode::Frame` 时调用 `traverse_frame`：

[traverse 中的 Frame 分支 — src/link.rs:L99-L112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/link.rs#L99-L112)　当某个被链接的目标元素落在帧内部时，`traverse_frame` 会通过内省器算出它在帧内的坐标 `point`，把 `(point, id)` 推进 `frame.anchors`：

[traverse_frame — src/link.rs:L119-L147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/link.rs#L119-L147)　（若该链接行号不可用，请以本地 `src/link.rs` 中 `traverse_frame` 函数为准。）

这样，`anchors` 就记录了「SVG 内部哪些坐标点是文档内链接的落点」。编码时 `write_frame` 会把它原样传给 SVG 生成器，生成对应的可跳转锚点（见 4.4.3）。

#### 4.3.5 小练习与答案

**练习**：`HtmlFrame` 的 `id` 和 `anchors` 都与链接有关，它们的作用对象有何不同？

> **参考答案**：`id` 是给**整个 `<svg>` 元素**用的——当帧自身（作为一个整体）是被链接的目标时，分配一个 id 挂到 `<svg id="...">`；`anchors` 是给 **SVG 内部的若干跳转点**用的——当被链接的目标位于帧内部时，记录这些目标在帧内的坐标与 id，编码时渲染成 SVG 内部的锚点。前者是「链向这一整张图」，后者是「链向图里的某个位置」。

### 4.4 write_frame 与 svg_in_html：把 Frame 编码为内联 SVG

#### 4.4.1 概念说明

编码阶段（u5-l1）的 `write_node` 遇到 `HtmlNode::Frame` 时，调用 `write_frame`。这是「最后一公里」：把 `HtmlFrame` 里的 `Frame` 真正变成一段 `<svg>...</svg>` 字符串，拼进 HTML 输出。typst-html 自己并不懂怎么把 `Frame` 画成 SVG——这件事委托给了兄弟 crate `typst-svg` 的 `svg_in_html` 函数。

#### 4.4.2 核心流程：委托 + 缩进后处理

[write_frame — src/encode.rs:L390-L414](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L390-L414)　它做两件事：

1. 把 `HtmlFrame` 的字段原样转发给 `typst_svg::svg_in_html`，拿到 SVG 字符串。
2. 若开启了 pretty 打印，**按行重新缩进**这段 SVG；否则原样拼接。

转发时注意几个细节：`frame.css` 被 `to_inline()` 拼成内联样式字符串；`frame.id` 用 `as_deref()` 转成 `Option<&str>`；`frame.anchors` 直接传引用。也就是说，`HtmlFrame` 积累的全部元信息（尺寸依据、id、css、锚点）在这一刻一次性交给 SVG 生成器。

#### 4.4.3 源码精读：svg_in_html 如何用 text_size 折算 em

[svg_in_html — crates/typst-svg/src/lib.rs:L82-L120](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-svg/src/lib.rs#L82-L120)　这是兄弟 crate `typst-svg` 里的函数。它给 `<svg>` 写了一个 `style` 属性：

```
overflow: visible; width: {frame.width()/text_size}em; height: {frame.height()/text_size}em; {styles}
```

这正是 4.3.3 推导的折算公式落地处：`frame.width() / text_size` 得到 em 宽，`frame.height() / text_size` 得到 em 高。`overflow: visible` 允许内容溢出 SVG 边界（有些字形/描边可能略微出界）；末尾的 `styles` 是 `frame.css.to_inline()`（如 `display: block`）。若有 `id`，则同时写 `id` 属性。

接着 `render_frame` 把帧内容画进 SVG；最后遍历 `anchors`，对每个 `(pos, id)` 调 `render_anchor`：

[render_anchor — crates/typst-svg/src/lib.rs:L404-L408](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-svg/src/lib.rs#L404-L408)　它生成一个空 `<g>`，带 `id` 和把原点平移到 `pos` 的 `transform`：

```xml
<g id="..." transform="translate(pos.x, pos.y)"></g>
```

于是文档内链接（`#link(<label>)`）解析出的 fragment id（如 `#label`）就能精确跳到 SVG 内部的这个坐标——这就是 `frame.anchors` 在「文档内链接跳转」里的作用：**让被栅格化进 SVG 的内容，依然保有可被链接命中的坐标点**。

#### 4.4.4 源码精读：为什么先生成 SVG、再补缩进

回到 `write_frame` 的 pretty 分支。注意它**没有**把当前的缩进层级 `w.level` 传给 `svg_in_html`，而是先生成一份「与外层缩进无关」的 SVG，再逐行补缩进。源码注释道破用意：

> Indent the SVG after generation. This ensures the frame is cached no matter the current indentation of the outer HTML.
> （生成后再缩进 SVG。这样无论外层 HTML 当前缩进如何，帧都能被缓存。）

也就是说，SVG 的生成结果只取决于 `(frame, text_size, pretty, id, styles, anchors, link_resolver)`，**不取决于它被嵌在第几层缩进**。同一份帧无论出现在文档的浅层还是深层，生成的 SVG 字符串完全相同——这是任何缓存/复用机制的前提。把缩进推迟到生成之后单独做，就是为了不让「外层缩进」污染这个本可稳定的产物。

#### 4.4.5 代码实践

**实践目标**：亲眼看到 `text_size` 折算出的 em 尺寸，以及 `anchors` 生成的 `<g>` 锚点。

**操作步骤**：

1. 新建 `demo.typ`，刻意在帧内放一个带 label、会被链接的目标：

   ```typ
   #html.elem("p")[
     跳到 #link(<goto>)[这里]。
     #html.frame[
       这是一个图表占位。 <goto>
     ]
   ]
   ```

2. `typst compile --format html demo.typ demo.html`。

3. 在 `demo.html` 中搜索 `<svg`。

**需要观察的现象**：

- `<svg>` 的 `style` 含 `width: …em; height: …em;`，数值 = 帧尺寸 ÷ 字号（默认 11pt 时，若帧宽 110pt 则为 `10em`）。
- 帧被设为块级时，`style` 里还带 `display: block`。
- 帧内部应能找到形如 `<g id="goto" transform="translate(…)"></g>` 的锚点（若 label 命中了链接目标）——这正是 `anchors` 的产物。

> 若本地无法运行，改为阅读 [svg_in_html — typst-svg/src/lib.rs:L93-L109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-svg/src/lib.rs#L93-L109) 与 [render_anchor — typst-svg/src/lib.rs:L404-L408](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-svg/src/lib.rs#L404-L408)，口述这两段代码如何分别实现 em 折算与锚点渲染。

#### 4.4.6 小练习与答案

**练习 1**：假设帧宽 80pt、帧高 30pt、`text_size` 为 10pt，`<svg>` 的 `width`/`height` 会写成多少？若用户把外层字号改成 20pt，SVG 实际显示多大？

> **参考答案**：`width: 8em; height: 3em;`。外层字号 20pt 时，8em = 160pt、3em = 60pt——SVG 等比放大 2 倍，帧内外文字比例保持一致。

**练习 2**：`write_frame` 为什么不把 `w.level` 传进 `svg_in_html`？

> **参考答案**：为了让生成的 SVG 字符串只依赖于帧本身的数据、而与外层缩进无关，从而具备可缓存/可复用的稳定产物；缩进作为「纯格式化」步骤放到生成之后单独补。

### 4.5 html_span_filled：HTML 专用的着色 span 临时方案

#### 4.5.1 概念说明

学习目标里提到要理解 `html_span_filled` 这个「临时方案」。它和 `html.frame` 同属 `typst-html` 暴露给核心的「HTML 专用工具」，但解决的问题不同：**如何给一段 HTML 文本上色**。

在分页（Paged）目标下，给文字上色只需 `TextElem::fill`（文字填充色），这是 Typst 的标准机制。但在 HTML 目标下，纯文本节点没有「填充色」属性可设——颜色必须通过 CSS 表达。于是 typst-html 提供了 `html_span_filled`：把内容包进一个带 `color` 内联样式的 `<span>`。

[html_span_filled — src/rules.rs:L762-L769](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L762-L769)　它构造 `HtmlElem::new(tag::span)` 并用 `.with_css(...)` 写入 `color: <颜色>`，再把 `content` 作为 body。

#### 4.5.2 核心流程：谁在调用它

`html_span_filled` 通过依赖倒置暴露为核心例程（在 `typst-library` 的 `Routines` 里声明，由 `typst-html` 实现，见 u3-l5 的装配点模式），唯一调用点是**原始文本（raw / 代码块）的语法高亮**：

[raw.rs 中的着色分支 — crates/typst-library/src/text/raw.rs:L1008-L1013](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/raw.rs#L1008-L1013)　当某个 token 的高亮前景色与默认前景色不同时，按 Target 分流：Paged 用 `TextElem::fill`，Html 用 `routines.html_span_filled`。

#### 4.5.3 为什么说是「临时方案」

称它「临时」有两层含义：

1. **它产生了本不该有的 `<span>` 噪声**。理想情况下，HTML 文本上色应直接作用于文本，而不是额外包一层 span。但目前 typst-html 没有更原生的「文本级颜色」机制，只能用 span 模拟。
2. **它绕开了 `TextElem::fill` 的统一模型**。Paged 与 Html 两条路径各用一套上色方式，是典型的「为 HTML 开的特例」。源码里 `html_span_filled` 的注释与实现都很简短（「minimal for now」式），暗示未来若有更通用的「元素级 set 规则」会被取代。

它与本讲的呼应在于：`html.frame` 和 `html_span_filled` 都是 typst-html 为「HTML 表达力不足」而开的口子——前者解决「布局无法翻译」，后者解决「文本无法直接上色」，二者共同体现了 HTML 导出在「语义化」与「保真/可控」之间的持续取舍。

#### 4.5.4 小练习与答案

**练习**：为什么不直接在 `typst-library` 里写「包一个带 color 的 span」，而要绕一圈通过 `routines.html_span_filled` 调用 typst-html？

> **参考答案**：依赖方向不允许。`typst-library` 是被依赖的核心，不能反过来依赖 `typst-html`（否则循环依赖）。通过 `Routines` 把 `html_span_filled` 声明为例程、由 `typst-html` 实现，是依赖倒置：核心定义扩展点，导出器填充实现——与 `register()` 注册 show 规则是同一种装配模式（见 u3-l5）。

## 5. 综合实践

把整条链路串起来跑一遍，并回答两个核心问题。

**任务**：编写下面这段 Typst，编译为 HTML，然后对照源码解释你看到的一切。

```typ
#set page(width: auto, height: auto)
#set text(size: 12pt)

这是一段普通 HTML 文本，其中的 #strong[强调] 会被翻译成 <strong>。

#html.frame[
  #set text(size: 12pt)
  #text(fill: red)[红方块] #text(fill: blue)[蓝方块]
  #line(length: 100%)
  精确排版的图表区域。
]

#raw("let x = 1", lang: "rust")
```

**要求你完成**：

1. **追踪链路**：用一句话标注 `html.frame[...]` 这块内容依次经过 `FrameElem`（lib.rs）→ `handle` 分支（convert.rs，注意 `Target::Paged` 与 `Abs::inf()`）→ `HtmlFrame::new`（dom.rs）→ `make_block_level` → `write_frame`（encode.rs）→ `svg_in_html`（typst-svg）这六个站点，每个站点发生了什么。
2. **解释 text_size**：打开生成的 HTML，找到 `<svg>` 的 `style`，读出 `width`/`height` 的 em 值；用尺子或计算验证它等于「帧尺寸 ÷ 12pt」。然后回答：**为什么 `html.frame` 要记录 `text_size`？** （提示：把绝对 pt 折算成 em，让 SVG 随外层字号等比缩放，保持帧内外文字比例一致。）
3. **解释 anchors**：把帧内某元素加上 `<label>` 并在帧外用 `#link(<label>)[跳]` 指向它，重新编译。在 SVG 内部找到形如 `<g id="…" transform="translate(…)"></g>` 的锚点，回答：**`frame.anchors` 在文档内链接跳转中起什么作用？** （提示：它记录帧内被链接目标的坐标与 id，由 `traverse_frame` 填充、由 `render_anchor` 渲染成 SVG 内可命中的锚点，让链接能精确跳进 SVG 内部。）
4. **观察 html_span_filled**：`#raw(..., lang: "rust")` 的高亮 token 是否被包成了 `<span style="color: …">`？这印证了 4.5 讲的临时方案。

> 若无法本地运行 typst CLI，请把 1～4 改为纯源码阅读：对照本讲给出的永久链接，逐段口述代码行为，并把第 2、3 题的答案写成文字结论。**不要假装已运行命令**——不确定的输出标注「待本地验证」。

## 6. 本讲小结

- `html.frame`（`FrameElem`）是「保真优先」的逃生舱：不把内容翻译成标签，而是用分页排版引擎布局后以内联 SVG 嵌入，适合图表等依赖精确定位的内容。
- `handle` 的 `FrameElem` 分支会临时把 `Target` 切回 `Paged` 再调 `layout_frame`（在无限大区域布局）；为避免 `show …: html.frame` 造成帧无限嵌套，`FrameElem` 在 Paged 目标下被注册为 no-op（直接透传 body）。
- `HtmlFrame` 是承载排版结果的 DOM 节点，五个字段（`inner` / `text_size` / `id` / `css` / `anchors`）随流水线逐步填充：`text_size` 在 `new` 时取自调用点字号，`css` 由 `make_block_level` 设为块级，`id` 与 `anchors` 由链接锚点阶段填入。
- `text_size` 用于把帧的绝对尺寸折算成 em（`width = frame.width()/text_size` em），使 SVG 随外层字号等比缩放，帧内外文字比例自洽。
- `write_frame` 把 `HtmlFrame` 委托给 `typst_svg::svg_in_html` 生成 SVG；它故意不把外层缩进传进去，而是生成后再按行补缩进，以保证 SVG 产物稳定、利于缓存复用。
- `frame.anchors` 记录帧内被链接目标的 `(坐标, id)`，由 `traverse_frame` 填充、`render_anchor` 渲染成 SVG 内的 `<g id transform>` 锚点，使文档内链接能精确跳进 SVG 内部。
- `html_span_filled` 是 HTML 专用的「文本上色」临时方案（包 `<span style="color:…">`），服务于代码块语法高亮，体现了 HTML 导出在语义化与可控性之间的取舍。

## 7. 下一步学习建议

- **u6-l2 表格与图片导出**：`IMAGE_RULE` 与 `html.frame` 同属「可视化内容如何进入 HTML」的主题，可对比「位图 base64」与「矢量 SVG」两种嵌入策略。
- **u6-l4 缓存与 comemo memoization**：本讲提到「SVG 产物稳定利于缓存」，可进一步了解 typst-html 在哪些层面用 `comemo::memoize` 包裹、以及 `HtmlDocument` 为何不实现 `Hash`。
- **重读 u5-l3 / u5-l4**：本讲的 `anchors` 与 `id` 直接依赖内省器（`HtmlIntrospector`）与链接锚点分配（`create_link_anchors`），若对「帧内坐标如何算出」仍有疑问，建议回到这两讲梳理 `HtmlPosition` 的帧内坐标（`InnerHtmlPosition::Frame`）来源。
- **延伸阅读**：直接打开 `crates/typst-svg/src/lib.rs` 的 `svg_in_html` 与 `render_frame`，理解 `Frame` 这棵「绘图树」是如何被逐节点写成 SVG 元素的——这是 `html.frame` 保真能力的最终落点。
