# convert_to_nodes 内容转换器

## 1. 本讲目标

上一讲（u3-l1）我们把编译主链路从 `html_document` 一直走到了 `finalize_dom`，但中间有一个最关键的「黑盒」没有打开：`convert_to_nodes`。它夹在 `realize`（把 Typst 元素具象化）和 `finalize_dom`（装订成完整文档骨架）之间，负责把一段已经具象化的 `Content` 序列**逐个翻译成 HTML DOM 节点**。

本讲学完后，你应当能够：

- 说清 `ConversionLevel`（Block / Inline）和 `Whitespace`（Normal / Pre）这两个枚举为什么是转换器的「两个旋钮」，以及它们各自的含义。
- 读懂 `convert_to_nodes` 这个总入口如何组织「逐个处理 → 收尾」的两段式流程。
- 沿着 `handle()` 这个**类型分派调度器**，追踪任意一种 Typst 元素（文本、空格、换行、智能引号、box、block、frame、`html.elem`）分别落到哪条分支、产出什么样的 `HtmlNode`。
- 理解 `handle_html_elem` 如何根据标签的默认 `display` 决定走 block / inline / math 三条递归路径，从而把嵌套结构一层层展开。
- 解释「无法转换的元素」是如何被优雅地降级为一条警告，而不是让整个导出崩溃的。

> 范围说明：本讲聚焦调度骨架。空白保护状态机（`protect_spaces` / `Protector`）的深层细节在 u4-l1，display 提升（`make_block_level` / `make_inline_level`）的细节在 u4-l2，三个 fragment 入口的缓存策略在 u3-l4。本讲会把它们当作「下游工序」点到即止，并给出跳转提示。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（来自前置讲义）：

- **`HtmlNode` 四变体**（u2-l1）：`Tag`（内省元数据，不产生 HTML 输出）、`Text`（纯文本）、`Element`（嵌套元素 `HtmlElement`）、`Frame`（排版后的内容，最终以内联 SVG 嵌入）。它们是 `convert_to_nodes` 的唯一产出。
- **`HtmlElement` 七字段**（u2-l1）：`tag` / `attrs` / `css` / `children` / `parent` / `span` / `pre_span`。`handle_html_elem` 最终就是手工拼装一个 `HtmlElement`。
- **realize 具象化**（u3-l1）：编译主链路先用 `engine.library.routines.realize(...)` 把 Typst `Content` 跑过一遍 show 规则，产出一串 `Pair`（即 `(Content, StyleChain)` 对），再喂给 `convert_to_nodes`。也就是说，**像 `_emph_` → `<em>` 这样的映射，是在 realize 阶段由 show 规则完成的**，到 `convert_to_nodes` 时已经是一个 `HtmlElem`（`html.elem`）了。
- **`Packed<T>` 与 `to_packed`**：Typst 里一个被具象化的元素会被打包成 `Packed<XxxElem>`；`handle()` 大量使用 `child.to_packed::<XxxElem>()` 来判断当前元素的类型。
- **`StyleChain`**：只读的样式链，`handle()` 里读取诸如 `TextElem::case`、`SmartQuoteElem::double` 等样式都靠它。

一个关键直觉：`convert_to_nodes` 是一个**递归的、流式的翻译器**。它一边消费 `Pair` 序列，一边把翻译结果累积进一个 `Converter` 状态机；遇到 `html.elem` / `block` / `box` 这种容器时，它会**递归地再调一次自己**（经由 fragment 入口），从而把整棵 Typst 内容树摊平成一棵 HTML DOM 树。

## 3. 本讲源码地图

本讲几乎全部内容集中在一个文件里，辅以少量跨文件引用：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/convert.rs` | 内容转换器主体 | `ConversionLevel`、`Whitespace`、`convert_to_nodes`、`handle`、`handle_html_elem`，以及 `Converter` 状态机 |
| `src/fragment.rs` | 三个递归入口 | `html_block_fragment` / `html_inline_fragment` / `html_math_fragment`——它们是 `handle_html_elem` 递归调用的桥梁 |
| `src/dom.rs` | DOM 数据模型 | `HtmlNode` 枚举、`HtmlElement` / `HtmlFrame` 结构体（被翻译出的产物） |
| `src/document.rs` | 编译主链路 | 第 172 行对 `convert_to_nodes` 的调用，说明它在主链路里的位置 |
| `src/rules.rs` | show 规则注册 | `EMPH_RULE` / `STRONG_RULE` 等，用于理解实践任务里 `_emph_` 为何会变成 `<em>` |

## 4. 核心概念与源码讲解

### 4.1 ConversionLevel：块级与行内的两种转换语义

#### 4.1.1 概念说明

同样是「把一段 Content 翻译成 HtmlNode 序列」，在 HTML 的不同位置需要的语义并不一样：

- **块级（Block）上下文**：例如文档顶层、或者 `<body>`、`<div>` 的直接子级。这里的转换是**自包含的**——它拥有自己独立的智能引号状态、自己独立的空白保护。
- **行内（Inline）上下文**：例如 `<em>`、`<strong>` 这种行内元素内部的文本。这里的转换必须和**外层共享**智能引号状态和空白保护，否则一句跨过 `<em>` 边界的英文会引号错乱、空格丢失。

`ConversionLevel` 就是用来表达这个区别的参数。它有两个变体：

```rust
pub enum ConversionLevel<'a> {
    Block,
    Inline(&'a mut SmartQuoter),
}
```

注意 `Inline` 变体**携带一个 `&mut SmartQuoter`**——这是它和 `Block` 最本质的差异：行内转换必须把外层的智能引号机（`SmartQuoter`）以可变引用的形式传进来，让「开/闭引号」的状态在多个相邻行内元素之间连续传递。

#### 4.1.2 核心流程

- 文档顶层、block 容器、block 级 HTML 元素 → 用 `ConversionLevel::Block`，转换器**自己 `SmartQuoter::new()` 一个新的**引号机。
- 行内 HTML 元素、box、math → 用 `ConversionLevel::Inline(&mut quoter)`，**借用并持续更新**外层的引号机。
- 一个 block 上下文里如果嵌套了行内元素，行内元素内部共享同一个 `quoter`；当遇到下一个 block 级元素时，`quoter` 会被重置（见 4.5 节）。

#### 4.1.3 源码精读

枚举定义与文档注释见 [src/convert.rs:22-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L22-L31)。注释明确写出：Block 「有自己的本地智能引号状态和空白保护」，Inline 「作为更大上下文的一部分，共享智能引号状态和共享空白保护」。

`convert_to_nodes` 如何根据 `level` 取出 `quoter`，见 [src/convert.rs:67-78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L67-L78)：

```rust
let block = matches!(level, ConversionLevel::Block);
let mut converter = Converter {
    // ...
    quoter: match level {
        ConversionLevel::Inline(quoter) => quoter,
        ConversionLevel::Block => &mut SmartQuoter::new(),
    },
    // ...
};
```

这段代码也说明了一个重要的设计后果：**Inline 分支借用外部的 `quoter`，而 Block 分支临时 new 一个**——这正是 u3-l4 会讲到的「为什么 inline 片段不能像 block 片段那样用 `comemo::memoize` 缓存」的根因（可变借用无法参与哈希）。

#### 4.1.4 代码实践

打开 [src/fragment.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs#L70-L76)，分别看 `html_block_fragment_impl`（第 70 行）和 `html_inline_fragment`（第 101 行）对 `convert_to_nodes` 的调用。

- 观察现象：block 路径传的是 `ConversionLevel::Block`，inline 路径传的是 `ConversionLevel::Inline(quoter)`。
- 预期结果：你能清楚看到「block 自带引号机、inline 借用引号机」在调用点的体现。

#### 4.1.5 小练习与答案

**练习**：为什么 `ConversionLevel::Inline` 必须携带 `&mut SmartQuoter`，而不能像 Block 那样在转换器内部 new 一个？

**参考答案**：因为智能引号（开引号还是闭引号）取决于**前一个字符**，而这个前一个字符可能位于上一个兄弟行内元素里（例如 `a <em>b</em> "c"` 中的引号状态要跨过 `<em>`）。如果每个行内元素各自 new 一个 `SmartQuoter`，状态就会被切断，导致引号方向判断错误。Block 上下文则相反——块级元素天然隔断文本流，所以重置引号机是正确的。

---

### 4.2 Whitespace：Normal 与 Pre 两种空白模式

#### 4.2.1 概念说明

HTML 规范规定，浏览器在渲染时会**折叠**普通元素里的连续空白（多个空格合成一个、换行被忽略）。但 Typst 的文本可能含**有意义的连续空格、制表符、换行**（比如代码、诗歌排版）。如果直接把这些字符写进 HTML，它们会被浏览器吃掉，导致视觉走样。

为此，转换器需要两种空白处理模式：

- **`Normal`**：默认模式。尽量按 HTML 折叠规则处理，但对**会被错误折叠的空白**（连续空格/制表符）包一层 `<span style="white-space: pre-wrap">` 来保护。
- **`Pre`**：原样输出模式。空白一个字符都不动，交给已经有 `white-space: pre` 的容器（如 `<pre>`、`<script>`、raw 文本）去呈现。

#### 4.2.2 核心流程

- 顶层文档用 `Whitespace::Normal` 转换。
- 进入一个 HTML 元素时，`handle_html_elem` 会**重新判定**这个元素的空白模式：如果当前已经是 `Pre`，或标签是 `pre` / raw / escapable-raw，就用 `Pre`，否则沿用 `Normal`。
- 在 `Normal` 模式下，单字符空格的处理被**推迟**到一个独立的 `protect_spaces` 后处理 pass（因为它需要前后看相邻元素）；连续空格/制表符则在 `handle_text` 里**当场**包进 pre-wrap span。
- 在 `Pre` 模式下，`handle_text` 直接把整段文本原样作为一个 `Text` 节点 push 出去，不做任何保护。

#### 4.2.3 源码精读

`Whitespace` 枚举及其详尽文档见 [src/convert.rs:33-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L33-L57)。注释里把 Normal 模式「哪些当场保护、哪些延后保护」讲得很清楚，并引用了 W3C 的 white-space 规则。

`convert_to_nodes` 收尾时只在「Block + Normal」组合下才跑 `protect_spaces`，见 [src/convert.rs:84-87](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L84-L87)：

```rust
let mut nodes = converter.finish();
if block && whitespace == Whitespace::Normal {
    protect_spaces(&mut nodes);
}
```

这个 `block &&` 条件很关键：**单空格保护只在块级上下文做一次**，行内上下文不重复做（它们的空白由所在的块级上下文统一在那一遍里处理），避免重复包裹。

`handle_html_elem` 里对 `Pre` 的判定见 [src/convert.rs:176-185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L176-L185)：一旦进入 `Pre`，子树就**黏住** `Pre` 不再回到 `Normal`。

> 深入 `handle_text` 的字符分类与 `protect_spaces` / `Protector` 状态机三态（Collapsing / Supportive / Space）留待 u4-l1。

#### 4.2.4 代码实践

阅读 [src/convert.rs:279-282](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L279-L282)（`handle_text` 开头对 `Pre` 的短路处理）。

- 观察现象：`Pre` 模式下 `handle_text` 第一行就 `return`，完全跳过后续字符扫描。
- 预期结果：理解「Pre = 原样、Normal = 扫描保护」的分流就发生在这里。

#### 4.2.5 小练习与答案

**练习**：假设一个 `<pre>` 元素里嵌套了一个普通 `<span>`，`<span>` 内部的空白会用哪种模式？

**参考答案**：仍是 `Pre`。因为 `handle_html_elem` 的判定是 `converter.whitespace == Pre || tag == pre || is_raw || is_escapable_raw`——只要外层传进来的 `whitespace` 已经是 `Pre`，内层就继承 `Pre`。这种「一旦 Pre 永远 Pre」的单向性，保证了 `<pre>` 子树里的空白绝不会被折叠。

---

### 4.3 convert_to_nodes：转换入口与收尾

#### 4.3.1 概念说明

`convert_to_nodes` 是整个翻译器的**唯一公共入口**。它的职责很薄：构造一个 `Converter` 状态机，把输入的 `Pair` 序列逐个喂给 `handle()`，最后收尾并（必要时）跑一遍空白保护。真正的翻译逻辑都在 `handle()` 和各个 `handle_xxx` 里。

它的签名值得逐字读：

```rust
pub fn convert_to_nodes<'a>(
    engine: &mut Engine,
    locator: &mut SplitLocator,
    children: impl IntoIterator<Item = Pair<'a>>,
    level: ConversionLevel,
    whitespace: Whitespace,
) -> SourceResult<EcoVec<HtmlNode>>
```

四个入参分别承担：`engine`（驱动排版/警告）、`locator`（给递归排版分配定位器）、`children`（待翻译的 `Pair` 序列）、`level` + `whitespace`（两个「旋钮」）。返回值是一串 `HtmlNode`，可能为空。

#### 4.3.2 核心流程

```
convert_to_nodes(engine, locator, children, level, whitespace)
  │
  ├─ 1. 由 level 决定 block 标志 + 取出 quoter
  ├─ 2. new 一个 Converter 状态机（持有 engine、locator、quoter、whitespace、output、trailing）
  ├─ 3. for (child, styles) in children:  handle(converter, child, styles)?
  ├─ 4. converter.finish()   // flush 残留的尾随空白
  └─ 5. if block && Normal:  protect_spaces(&mut nodes)  // 单空格保护 pass
       → Ok(nodes)
```

#### 4.3.3 源码精读

完整入口见 [src/convert.rs:59-90](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L59-L90)。`Converter` 状态机本身的定义见 [src/convert.rs:512-528](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L512-L528)，注意它除了累积 `output`，还维护一个 `trailing: Option<TrailingWhitespace>`，用来追踪「刚刚 push 出去的、尚未确定要不要保护的尾随空白」。

`finish` 与 `push` 是理解收尾的关键，见 [src/convert.rs:532-584](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L532-L584)。`push` 在遇到空格/制表符文本节点时**先暂存**到 `trailing`（不立即决定保护与否），遇到下一个非 Tag 节点时再 `flush_whitespace` 把连续空白收拢成一个 pre-wrap span。`finish` 则在序列末尾做最后一次 flush。

调用点定位：在主链路里，`html_document_common` 在 realize 之后、`finalize_dom` 之前调用它，见 [src/document.rs:172-178](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L172-L178)，传入的是 `ConversionLevel::Block` 与 `Whitespace::Normal`——这正是「整个文档」的初始上下文。

#### 4.3.4 代码实践

在 `src/document.rs` 第 163–178 行，你能看到「realize 产出 children → `convert_to_nodes` 消费 children」的衔接。

- 操作步骤：对照 4.3.2 的流程图，给这段调用标注每一步。
- 需要观察的现象：`children.iter().copied()` 把 `Vec<Pair>` 转成迭代器喂进去；返回的 `nodes` 紧接着被传给 `finalize_dom`。
- 预期结果：你应当能说清「realize 的产物就是 convert_to_nodes 的唯一输入」。

#### 4.3.5 小练习与答案

**练习**：为什么 `convert_to_nodes` 的返回类型是 `EcoVec<HtmlNode>` 而不是单个 `HtmlNode`？

**参考答案**：因为一段 Content 翻译出来通常是一串平铺的节点（多个文本、多个行内元素、混入的内省 Tag），而不是恰好一个元素。比如一个段落顶层就是 `[Text, Element(em), Text, ...]`。即使是「单个 block」，也可能伴随前后若干内省 `Tag`。用 `EcoVec` 才能忠实地表达这种「序列」语义，也方便上层 `finalize_dom` 统计非 Tag 节点个数来决定如何包裹外壳。

---

### 4.4 handle()：类型分派调度器

#### 4.4.1 概念说明

`handle()` 是转换器的心脏。它接收**一个**`Content`（连同其 `StyleChain`），用一连串 `if let Some(elem) = child.to_packed::<XxxElem>()` 的「类型探测链」判断它到底是哪种元素，然后分派到对应的处理逻辑。它是一个**线性优先级匹配**：排在前面的分支先命中。

#### 4.4.2 核心流程

`handle()` 的分支顺序（自上而下，先命中先生效）如下表：

| 序号 | 探测的类型 | 处理方式 | 产出的 HtmlNode |
| --- | --- | --- | --- |
| 1 | `TagElem` | 直接 `push` 内省 tag | `Tag` |
| 2 | `HtmlElem`（`html.elem`） | 调 `handle_html_elem` | `Element`（含递归子节点） |
| 3 | `SpaceElem` | push 一个单空格文本 | `Text(" ")` |
| 4 | `TextElem` | 读 `case` 样式，调 `handle_text` | `Text(...)`（可能多个） |
| 5 | `HElem` 且 amount 为零 | 什么都不做（「空格销毁」hole） | 无 |
| 6 | `LinebreakElem` | Normal→`<br>`，Pre→`Text("\n")` | `Element(br)` 或 `Text` |
| 7 | `SmartQuoteElem` | 用 `quoter` 选引号字符，调 `handle_text` | `Text(quote)` |
| 8 | `BoxElem` | 调 `handle_box` | `Element(span)` 或被提升的行内节点 |
| 9 | `BlockElem` | 调 `handle_block` | `Element(div)` 或被提升的块级节点 |
| 10 | `FrameElem`（`html.frame`） | 切回 Paged 排版，包成 `HtmlFrame` | `Frame`（并 `make_block_level`） |
| 兜底 | 以上都不匹配 | `engine.sink.warn(...)` | 无（发一条警告） |

#### 4.4.3 源码精读

调度器本体见 [src/convert.rs:92-163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L92-L163)。几个值得精读的点：

1. **TagElem 最优先**（[L98-99](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L98-L99)）：内省 tag 不产生 HTML 文本，只作为 DOM 树里的「定位锚点」被原样保留。
2. **文本的 `case` 处理**（[L104-110](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L104-L110)）：如果样式里设了 `upper`/`lower`，会先 `case.apply(&elem.text)` 再交给 `handle_text`。
3. **零宽 `HElem` 的「销毁」**（[L111-115](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L111-L115)）：Typst 用零宽 `HElem` 来「吃掉」空格（如脚注里），导出时直接忽略，注释指向 `HElem::hole`。
4. **LinebreakElem 的双模输出**（[L116-120](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L116-L120)）：同一个换行元素，在 Normal 下变成 `<br>`，在 Pre 下变成字面量 `\n`。
5. **SmartQuoteElem 的上下文感知**（[L121-135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L121-L135)）：先取 `last_char(&converter.output)`（即「前一个非忽略字符」），再结合语言/区域/是否双引号，调用 `converter.quoter.quote(...)` 选出正确的引号；若 `enabled=false` 则退化为 `SmartQuotes::fallback`。
6. **兜底警告**（[L155-161](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L155-L161)）：任何没被识别的元素，不会让导出崩溃，而是发一条 `"{name} was ignored during HTML export"` 警告。这就是 typst-html 对「无法 HTML 化的内容」的优雅降级策略。

`last_char` 辅助函数见 [src/convert.rs:685-697](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L685-L697)：它反向遍历节点、跳过 default-ignorable 字符、并递归进 `Element` 的 children 去找「真正的上一个字符」——这正是智能引号能跨元素边界正确工作的原因。

#### 4.4.4 代码实践

下面这段 Typst 同时触发表中的好几条分支：

```typst
#set text(lang: "en")
A line\break, and "quotes" here.
```

- 操作步骤：阅读 `handle()` 的 [L98-135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L98-L135)，逐一对照：文本 `A line` → 第 4 行；`\` 换行 → 第 6 行（产出 `<br>`）；`"quotes"` 的两个引号 → 第 7 行（SmartQuote，开/闭由 `quoter` 状态决定）。
- 需要观察的现象：换行前后、引号前后的 `TextElem` 如何各自变成 `Text` 节点，而引号本身不是 `TextElem` 而是 `SmartQuoteElem`。
- 预期结果：你能复述出「同一句话里的文本、换行、引号走了三条不同分支」。

#### 4.4.5 小练习与答案

**练习 1**：如果一个元素既不是上面十种，也不在兜底之外（比如某个用户自定义且没有 HTML show 规则的元素），导出会怎样？

**参考答案**：命中 [L155-161](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L155-L161) 的兜底分支，发一条 `"{name} was ignored during HTML export"` 警告，该元素被静默跳过，**导出继续进行**，不会 panic。

**练习 2**：为什么 `SmartQuoteElem` 要在 `handle()` 里**就地**处理，而不是像 `emph` 那样在 realize 阶段就被 show 规则转成 HTML 文本？

**参考答案**：因为智能引号的方向取决于「前一个字符」这个**动态运行时上下文**，show 规则在 realize 时无法知道（它看不到前后兄弟节点）。所以必须推迟到 `handle()` 这一步，借助 `last_char(&converter.output)` 和共享的 `quoter` 状态来决定。这也解释了为什么 `ConversionLevel::Inline` 必须共享 `quoter`。

---

### 4.5 handle_html_elem：用户 HTML 元素的递归转换

#### 4.5.1 概念说明

`html.elem`（以及它派生出的 `html.div` / `html.span` / `html.a` 等类型化函数）是用户直接往文档里插 HTML 的通道。它本身是一个 `HtmlElem`，带着 `tag`、`attrs`、`body`、`css`、`role` 等字段（见 u1-l4）。`handle_html_elem` 的工作是：**把这个 `HtmlElem` 翻译成一个真正的 `HtmlElement` DOM 节点，并递归地把它的 `body` 也转换成 children**。

它的核心难点在于：**body 该用哪条递归路径转换？** 答案取决于标签的默认 `display`。

#### 4.5.2 核心流程

```
handle_html_elem(elem)
  │
  ├─ 1. 计算 role（注意：<p> 不带 role，见注释；role 只作用于最外层）
  ├─ 2. 若有 body，判定 whitespace（Pre 还是 Normal）
  ├─ 3. 若 role 已设置，临时把它从 styles 里 unset，使 children 不继承 role
  ├─ 4. 按 display 分三条路径转换 body：
  │     • Display::default_for(tag) == Block  → html_block_fragment（并重置 quoter）
  │     • tag 是 MathML 元素                  → html_math_fragment
  │     • 其它（行内）                         → html_inline_fragment
  ├─ 5. 组装 attrs（用户 attrs + 追加 role）
  └─ 6. 手工 new 一个 HtmlElement{tag, attrs, css, children, parent, span, pre_span:false} 并 push
```

注意第 4 步的递归会经由 fragment 入口**再次进入 `convert_to_nodes`**——这就是「树被一层层展开」的机制。

#### 4.5.3 源码精读

`handle_html_elem` 全函数见 [src/convert.rs:165-248](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L165-L248)。几个关键点：

- **`<p>` 不带 role**（[L171-173](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L171-L173)）：`.filter(|_| elem.tag != tag::p)`，原因写在 `HtmlElem::role` 的文档里——`<p>` 的语义已足够，强行加 role 反而语义错误。
- **role 只作用于第一层**（[L187-195](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L187-L195)）：若外层设了 role，给 children 临时 chain 一个 `role.set(None)`，确保 role 不向下泄漏。
- **三路分派**（[L197-229](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L197-L229)）：用 `property::Display::default_for(elem.tag)` 判断是否块级；块级走 `html_block_fragment`，**并在之后 `*converter.quoter = SmartQuoter::new()` 重置引号机**（块级隔断文本流）；MathML 标签走 `html_math_fragment`（用数学 realize，跳过段落分组）；其余走 `html_inline_fragment`（共享外层 `quoter`）。
- **最终装配**（[L232-247](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L232-L247)）：把用户 `attrs`（经 `#[fold]` 合并后）取出，若有 role 则 `attrs.push(attr::role, role)`，然后 `converter.push(HtmlElement{ ... pre_span: false })`。注意这里直接构造结构体，而不是用 `HtmlElement::new`——因为要同时设置 `parent`、`css`、`pre_span` 等字段。

> 关于 `Display::default_for` 为什么能把 `<div>`/`<p>` 判为 Block、把 `<span>`/`<em>` 判为 Inline，以及 `make_block_level`/`make_inline_level` 如何微调 display，详见 u4-l2。三个 fragment 入口的 realize 与缓存差异详见 u3-l4。

#### 4.5.4 代码实践

追踪 `html.div[A _B_ C]` 的转换：

- 操作步骤：
  1. realize 阶段，`div` 本身已是 `HtmlElem(tag::div, body="A _B_ C")`。
  2. `handle()` 第 2 分支命中，调 `handle_html_elem`。
  3. `Display::default_for(div) == Some(Block)`，走 `html_block_fragment`。
  4. block fragment 内部 realize `A _B_ C`：`_B_` 经 `EMPH_RULE`（见 [src/rules.rs:100-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L100-L101)）变成 `HtmlElem(tag::em, body="B")`；于是 children = `[Text("A "), HtmlElem(em,"B"), Text(" C")]`。
  5. `convert_to_nodes` 处理它们：`Text("A ")`→`handle_text`；`HtmlElem(em)`→再次进 `handle_html_elem`，em 是行内，走 `html_inline_fragment` 把 `"B"` 转成 `Text("B")`，包成 `HtmlElement(em)`；`Text(" C")`→`handle_text`。
  6. 最终 push 一个 `HtmlElement{tag:div, children:[Text("A "), Element(em, [Text("B")]), Text(" C")]}`。
- 需要观察的现象：`<div>` 和 `<em>` 走了**不同**的递归路径（block vs inline），但最终都嵌进同一棵 DOM 树。
- 预期结果：你能画出这棵两层 DOM 树，并标注每层用的 `ConversionLevel`。

#### 4.5.5 小练习与答案

**练习 1**：为什么 block 分支结束后要 `*converter.quoter = SmartQuoter::new()`，而 inline 分支不需要？

**参考答案**：block 级元素（如 `<div>`、`<p>`）在 HTML 里会换行、隔断文本流，其前后的引号状态不应连贯，所以重置 `quoter` 是正确的；inline 级元素（如 `<em>`）不换行，引号状态必须和前后兄弟连续，所以借用并更新外层 `quoter`、不重置。（代码注释也坦言 block 这段目前「unfortunately untested」，因为在 Typst 里很难不触发自动段落而造出 block/inline 混排。）

**练习 2**：`handle_html_elem` 最后为什么用结构体字面量 `HtmlElement { ... }` 而不是 `HtmlElement::new(tag).with_children(...)`？

**参考答案**：因为它需要一次性设置 `parent`（来自 `elem.parent`）、`css`（来自 `elem.css.get_cloned`）和 `pre_span: false` 等 `HtmlElement::new` 不便设置的内部字段；而 `with_attr`/`with_children` 等 builder 主要面向构造 `Content` 阶段（见 u2-l3 对两个 `with_attr` 的区分）。直接构造结构体更直接、也更符合「这里是在拼装 DOM 而非 Content」的语义。

## 5. 综合实践

把本讲所有模块串起来，追踪一句含强调文本和智能引号的段落。

**实践目标**：验证你已经掌握 `ConversionLevel`、`Whitespace`、`convert_to_nodes`、`handle`、`handle_html_elem` 五个模块如何协作。

**Typst 源码**（保存为 `input.typ`）：

```typst
#set text(lang: "en")
She said "hello _world_" twice.
```

**操作步骤**：

1. 用 CLI 编译为 HTML（来自 u1-l3 的调用链）：

   ```bash
   typst compile --format html input.typ output.html
   ```

   （该命令的执行结果待本地验证；若你的 typst 版本/参数名不同，以本地 `typst compile --help` 为准。）

2. 对照下面的「源码追踪表」，从文档顶层一路追到最内层文本，逐步标注每一步命中的分支与产出。

**源码追踪表**（自顶向下，标注行号供你核对）：

| 阶段 | 发生的事 | 命中的代码 |
| --- | --- | --- |
| ① 文档顶层 realize | 段落被 `PAR_RULE` 包成 `HtmlElem(tag::p, body=...)` | [rules.rs:95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L95) |
| ② 顶层 convert_to_nodes | 用 `Block` + `Normal` 启动 | [document.rs:172-178](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L172-L178) |
| ③ handle 命中 HtmlElem(p) | 进入 `handle_html_elem`，p 是 Block → `html_block_fragment` | [convert.rs:100-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L100-L101)、[197-204](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L197-L204) |
| ④ 段落 body realize | `_world_` 经 `EMPH_RULE` → `HtmlElem(em, body="world")` | [rules.rs:100-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L100-L101) |
| ⑤ 子级 convert_to_nodes | 处理 `TextElem("She said ")` | `handle_text`，见 [convert.rs:104-110](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L104-L110) |
| ⑥ SmartQuoteElem（开引号 `"`） | `last_char` 取到 `' '`，`quoter.quote` 选开引号 `“` | [convert.rs:121-135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L121-L135) |
| ⑦ handle 命中 HtmlElem(em) | em 是行内 → `html_inline_fragment`（共享 `quoter`） | [convert.rs:220-228](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L220-L228) |
| ⑧ em body | `TextElem("world")` → `Text("world")`，包成 `HtmlElement(em)` | [convert.rs:237-245](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L237-L245) |
| ⑨ SmartQuoteElem（闭引号 `"`） | `quoter` 状态已是「已开」，选闭引号 `”` | 同 ⑥ |
| ⑩ 收尾 | `finish` flush 空白；因 Block+Normal 跑 `protect_spaces` | [convert.rs:84-87](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L84-L87) |

**预期结果**：产出的 DOM 大致形如（具体缩进/pretty 以本地 `output.html` 为准）：

```html
<p>She said “hello <em>world</em>” twice.</p>
```

**需要观察的现象**：

- `TextElem`、`SmartQuoteElem` 走了 `handle()` 里**不同**的分支（第 4 vs 第 7），但最后都经 `handle_text` 落成 `Text` 节点。
- 两个智能引号一个开、一个闭，靠的是同一个 `quoter` 在 `ConversionLevel::Block` 内部的状态推进。
- `<em>` 是行内元素，所以它的 body 用 `html_inline_fragment` 与外层**共享** `quoter`；若把它换成 `<div>`（块级），第 ⑦ 步会改走 `html_block_fragment` 并重置 `quoter`。
- 段落里的单词间单空格被前后普通文本夹着，`protect_spaces` 不会给它们加 pre-wrap（这正是 Normal 模式「尽量不加保护」的体现）。

**如果无法确定运行结果**：引号的确切字形（`“”` vs `""`）取决于 `SmartQuotes::get` 返回的语言引号表，上表按英文预期；请以本地编译输出为准，标注「待本地验证」。

## 6. 本讲小结

- `ConversionLevel`（Block / Inline）和 `Whitespace`（Normal / Pre）是转换器的两个旋钮：前者决定**智能引号状态是否自包含**，后者决定**空白是否需要保护**。
- `convert_to_nodes` 是唯一入口，职责很薄：构造 `Converter` 状态机 → 逐个 `handle` → `finish` 收尾 →（Block+Normal 时）跑一遍 `protect_spaces`。
- `handle()` 是一条**类型探测链**，把十类常见元素分派到不同分支，并用兜底 `warn` 优雅降级无法 HTML 化的内容，保证导出不崩溃。
- `TextElem` 走 `handle_text`、`SpaceElem` 直接变成单空格 `Text`、`LinebreakElem` 在 Normal/Pre 下分别产出 `<br>`/`\n`、`SmartQuoteElem` 借 `quoter` + `last_char` 选引号字符——它们最终大多落成 `Text` 节点。
- `handle_html_elem` 按标签默认 `display` 把 body 分流到 block / inline / math 三条递归路径，再手工装配出一个 `HtmlElement`；这是整棵 DOM 树「层层展开」的机制所在。
- 内省 `Tag` 在最优先分支被原样保留、不产生 HTML 文本，是后续内省/链接（u5-l3、u5-l4）得以工作的基础。

## 7. 下一步学习建议

- **u3-l4（块级/行内/数学片段的递归编译）**：深入 `html_block_fragment` / `html_inline_fragment` / `html_math_fragment` 三个入口，理解为什么 block 片段能 `memoize` 而 inline 片段不能，以及 `route.check_html_depth()` 如何防递归过深。
- **u3-l5（内建 show 规则注册）**：精读 `rules.rs`，弄清 `emph`→`<em>`、`strong`→`<strong>`、`heading`→`<h2>`~、`footnote` 的 ARIA role 等映射，补全本讲「realize 阶段发生了什么」的另一半。
- **u4-l1（HTML 空白保护机制）**：把本讲点到即止的 `protect_spaces` / `Protector` 三态状态机、`flush_whitespace`、`pre_wrap` 彻底搞懂。
- **u4-l2（display 属性与块级/行内提升）**：精读 `Display::default_for`、`make_block_level` / `make_inline_level` / `to_lone_element`，理解 `<div>`/`<span>` 是如何被「提升」而非总是新建的。

继续阅读时，建议把本讲的「源码追踪表」方法沿用下去——对任何一段 Typst 源码，先想清楚它在 realize 后变成什么 `Pair`，再用 `handle()` 的分支表对照它会落到哪里，这是阅读 typst-html 转换器最有效的肌肉记忆。
