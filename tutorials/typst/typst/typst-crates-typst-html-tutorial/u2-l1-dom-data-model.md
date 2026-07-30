# HTML DOM 数据模型总览

## 1. 本讲目标

本讲是「进阶：HTML DOM 数据模型」单元的第一篇。上一篇（u1-l3）我们走通了从 `typst::compile::<HtmlDocument>` 到 `html()` 编码的整条调用链，知道了**编译**产出 `HtmlDocument`、**编码**把它变成字符串。但那条链上最关键的一个问题被我们刻意跳过了：编译阶段到底产出了什么样的中间数据结构？`HtmlDocument` 里装的究竟是什么？

读完本讲，你应当能够：

- 说出 `HtmlNode` 枚举的四种变体（`Tag` / `Text` / `Element` / `Frame`）各自代表什么、为什么需要这四种。
- 逐字段解释 `HtmlElement` 的每一个字段（`tag` / `attrs` / `css` / `children` / `parent` / `span` / `pre_span`）的作用。
- 理解 `HtmlOutput` 为什么用「扁平节点表 + `root_index`」而不是一棵显式的树。
- 理解 `HtmlSliceExt::iter_with_dom_indices` 这个状态机在做什么、为什么需要它。
- 把 `HtmlDocument` 放回 `Output` / `Document` trait 的抽象里，理解它为何不实现 `Hash`。

本讲是后续所有讲义（转换器、编码器、内省器、链接锚点）的地基：它们操作的全都是这里定义的类型。

## 2. 前置知识

在进入源码前，先澄清三个容易混淆的概念。

**DOM 树 vs. 字符串。** 浏览器里的「DOM」是一棵对象树；而 `.html` 文件是一段带尖括号的文本。typst-html 的做法是：先用一套 Rust 结构体把「DOM 树」在内存里建好（本讲的主题），最后一遍再把它**编码**（encode）成字符串（u5-l1 的主题）。这样做的好处是建树阶段可以反复修改、查询，而编码阶段是纯函数、无副作用。

**`EcoVec` / `EcoString`。** typst 全家桶大量使用 ecow crate 提供的 `EcoVec`（写时复制的向量）和 `EcoString`（写时复制的字符串）。你可以暂时把它们当成 `Vec` 和 `String`，只是克隆成本很低——这对「建树阶段反复克隆子树」非常友好。本讲里 `children: EcoVec<HtmlNode>`、`HtmlAttrs.0: EcoVec<(HtmlAttr, EcoString)>` 都是这种用法。

**`PicoStr`（字符串驻留）。** `HtmlTag` 和 `HtmlAttr` 的内部都是一个 `PicoStr`——一个全局驻留（intern）的字符串句柄。同一个标签名（如 `"div"`）在整棵树里只存一份，比较时只需比句柄、不必逐字符比较。这是 u2-l2 的主题，本讲你只需知道「`HtmlTag` 本质是一个被驻留的标签名」即可。

**`Tag`（大写，来自 `typst_library::introspection`）。** 注意它和 `HtmlTag`（小写 h）完全不同：`HtmlTag` 是 HTML 标签名（`div`/`span`），而 `Tag` 是 Typst 内省系统打在内容流里的「定位标记」，用来回答「这个元素最终落在了 DOM 的哪个位置」。这正是 `HtmlNode::Tag(Tag)` 变体存在的原因。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们是整个 crate 的数据骨架：

| 文件 | 作用 | 本讲关注 |
| --- | --- | --- |
| `src/dom.rs` | 定义 DOM 的全部数据结构：`HtmlNode`、`HtmlElement`、`HtmlTag`、`HtmlAttrs`、`HtmlAttr`、`HtmlFrame`、`HtmlSliceExt`，以及顶层 `HtmlDocument` | 全部 |
| `src/document.rs` | 编译主链路 + `HtmlOutput` 容器 + `finalize_dom` 文档骨架生成 | `HtmlOutput`、`finalize_dom`、`html_document` |

可以先把 `dom.rs` 理解成「积木定义」，`document.rs` 里的 `HtmlOutput` 理解成「装积木的盒子」、`HtmlDocument` 理解成「打好包、贴好内省标签的成品」。

## 4. 核心概念与源码讲解

本讲按「叶子节点 → 元素 → 节点容器 → 索引工具 → 顶层文档」的顺序拆成五个最小模块：`HtmlNode`、`HtmlElement`、`HtmlOutput`、`HtmlSliceExt`、`HtmlDocument`。

### 4.1 HtmlNode：DOM 节点的四种形态

#### 4.1.1 概念说明

`HtmlNode` 是 typst-html DOM 树里**所有节点的统一类型**——它是「一个 HTML 元素的孩子」的总和。源码用一句注释点明了它的定位：

> `An HTML element's child.`（HTML 元素的孩子。）

它是一个枚举，共四个变体，分别对应四种性质完全不同的「孩子」：

| 变体 | 内部数据 | 角色 |
| --- | --- | --- |
| `Tag(Tag)` | Typst 内省标记 | 元数据，**不产生任何 HTML 输出**，只用于定位/内省 |
| `Text(EcoString, Span)` | 文本 + 源码位置 | 纯文本节点 |
| `Element(HtmlElement)` | 一个完整元素 | 结构性子节点（可嵌套） |
| `Frame(HtmlFrame)` | 排版后的 Frame | 以内联 SVG 嵌入的内容（见 u6-l1） |

理解这四者的关键在于：DOM 树里既有「会被渲染的东西」（文本、元素、SVG），也有「只给编译器看、不进 HTML 的东西」（`Tag`）。把它们放进同一个枚举，转换器就可以把内省标记和真实内容**按出现顺序混排**在同一个 `children` 列表里，编码时再各自分流。

#### 4.1.2 核心流程

`HtmlNode` 的生命周期大致是：

1. **产生**：`convert_to_nodes`（u3-l3）遍历 realization 产出的 Typst `Content`，根据内容类型选择性地把 `Tag`、`Text`、`Element`、`Frame` 推入 `children`。
2. **携带**：作为 `HtmlElement::children` 的元素，随元素一起被克隆、移动、嵌套。
3. **分流消费**：
   - 编码阶段（`encode.rs`）：`Tag` 被忽略；`Text` 走文本转义；`Element` 递归写出；`Frame` 调 `typst_svg` 写成 SVG。
   - 内省阶段（`introspect.rs`）：`Tag` 是主角，用于建立「Typst 元素 → DOM 位置」的映射。

`HtmlNode` 还提供两个便利：

- `HtmlNode::text(..)`：快速构造文本节点。
- `From<Tag/HtmlElement/HtmlFrame>`：让这三种可以直接 `.into()` 成 `HtmlNode`，写 `children.push(element.into())` 时更顺手。

#### 4.1.3 源码精读

枚举定义本身极其简洁，四个变体一一对应上表：

[`HtmlNode` 枚举定义 — dom.rs:L99-L110](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L99-L110) — 定义 `Tag` / `Text` / `Element` / `Frame` 四变体，注意 `Tag` 的注释明确说它是「产出了某些东西的可内省元素」，即内省元数据。

`span()` 方法按变体取出源码位置：`Tag` 没有源码位置（返回 detached），其余三者各自携带 `Span`：

[`HtmlNode::span` — dom.rs:L119-L126](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L119-L126) — 用 `match` 分派取 `Span`，错误诊断时用来指向源码。

三个 `From` 实现让 `Tag` / `HtmlElement` / `HtmlFrame` 能直接 `.into()` 成节点（注意没有 `Text` 的 `From`，因为文本节点用 `HtmlNode::text` 构造、还需附带 `Span`）：

[`From 实现三连 — dom.rs:L129-L145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L129-L145) — 这也是 `children.push(some_element.into())` 这种写法能成立的原因。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认四种 `HtmlNode` 变体各自是从哪里被生产出来的。
2. **步骤**：打开 `document.rs`，找到 `convert_to_nodes` 的调用处：

   [`convert_to_nodes 调用 — document.rs:L172-L178](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L172-L178) — 注意它返回的就是 `EcoVec<HtmlNode>`（确切说是节点序列），入口层级是 `ConversionLevel::Block`、空白模式是 `Whitespace::Normal`。

   再到 `src/convert.rs` 里搜索 `HtmlNode::Text`、`HtmlNode::Element`、`HtmlNode::Frame`、`HtmlNode::Tag` 的构造点，分别记录它们出现在哪个 `handle_*` 函数里。
3. **观察现象**：你会发现 `Text` 在处理文本/空格/智能引号时产生，`Element` 在处理 `html.elem`、标题、列表等时产生，`Frame` 在处理 `html.frame` 时产生，`Tag` 由 realization 阶段插入。
4. **预期结果**：你能画出一张「Typst 内容类型 → `HtmlNode` 变体」的对照表。具体的 `handle()` 分派细节是 u3-l3 的主题，本讲只需建立直觉。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `HtmlNode` 里要有 `Tag(Tag)` 这个「不产生 HTML 输出」的变体？能不能在转换阶段就把它删掉？

> **答案**：不能。`Tag` 携带的是 Typst 内省系统的定位信息，编码阶段确实忽略它，但内省阶段（`introspect.rs`）要靠它建立「Typst 元素落在 DOM 第几个孩子」的映射，从而支撑 `query`、文档内链接跳转等功能。若转换阶段就删掉，内省信息会丢失。

**练习 2**：`HtmlNode::text` 为什么不实现 `From<EcoString>`？

> **答案**：因为文本节点需要同时携带 `Span`（源码位置）用于错误诊断；`From` trait 只接受单个参数，无法同时传入文本和 `Span`，所以专门提供了 `HtmlNode::text(text, span)` 构造器。

---

### 4.2 HtmlElement：元素的核心结构

#### 4.2.1 概念说明

`HtmlElement` 是 DOM 树里**真正承重**的类型——每一个 `<div>`、`<span>`、`<ul>` 在内存里都是一个 `HtmlElement`。它的 `children` 字段是 `EcoVec<HtmlNode>`，于是元素和节点互相嵌套，自然构成一棵树：

```
HtmlElement (ul)
└── children: [HtmlNode::Element(li), HtmlNode::Element(li), ...]
                       └── children: [HtmlNode::Text("文本"), ...]
```

换句话说：**节点（`HtmlNode`）是「槽位类型」，元素（`HtmlElement`）是「有标签、有属性、有孩子的实体」**。整棵 DOM 树就是「根元素的孩子里嵌着更多元素」的递归结构。

#### 4.2.2 核心流程

一个 `HtmlElement` 的字段可以分成三组，对应它的三种用途：

| 分组 | 字段 | 用途 |
| --- | --- | --- |
| 内容组 | `tag`、`attrs`、`children` | 决定渲染成什么 HTML |
| 样式组 | `css` | 编译器生成的 CSS 属性，最终写入 `style` 属性 |
| 元信息组 | `parent`、`span`、`pre_span` | 内省定位、错误诊断、空白保护 |

典型构造流程是 builder 风格：`HtmlElement::new(tag)` 建空壳 → `with_attr(..)` / `with_children(..)` / `with_css(..)` / `spanned(..)` 链式填充。这套 builder 都消费 `self` 并返回 `Self`，所以可以一路点下去。

#### 4.2.3 源码精读

结构体定义把七个字段一字排开，注释详尽，是本讲最值得逐行读的代码：

[`HtmlElement` 结构体定义 — dom.rs:L182-L206](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L182-L206) — 逐字段含义：

- `tag: HtmlTag`：标签名（驻留字符串），如 `div`。
- `attrs: HtmlAttrs`：属性列表，详见 4.2.4 与 u2-l3。
- `css: css::Properties`：**编译器生成的** CSS 属性（注意注释「Currently only used for generated styles」），与用户写的 `style` 属性分开存放，最后由 `resolve_inline_styles` 合并。`css::Properties` 本质是 `EcoVec<Property>`，每个 `Property` 是 `{ name: &'static str, value: EcoString }`（见 `css/encode.rs`）。
- `children: EcoVec<HtmlNode>`：孩子节点列表，树的递归就靠它。
- `parent: Option<Location>`：逻辑父元素的位置，**仅供内省**——注释说明「在逻辑上，本元素紧跟在 parent 的起始位置之后排序」。
- `span: Span`：本元素的源码位置，用于报错指路。
- `pre_span: bool`：标记这是否是编译器为了**防止空白被折叠**而自动生成的 `white-space: pre-wrap` span；这类 span 里的空格/制表符会以转义序列输出，避免格式化工具破坏空白（详见 u4-l1）。

`new` 给出「空壳」的默认值（无属性、无孩子、无 css、无 parent、detached span、非 pre_span）：

[`HtmlElement::new — dom.rs:L208-L220](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L208-L220) — 一切元素都从这个空壳开始。

四个 builder 方法都遵循「消费 self → 修改 → 返回 self」的模式：

[`HtmlElement` 的 builder 方法 — dom.rs:L222-L247](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L222-L247) — 注意 `with_children` 的注释：「会覆盖既有孩子」；`with_css` 是 `pub(crate)`，只供 crate 内部（编译器）使用，用户无法直接设置 `css` 字段。

#### 4.2.4 代码实践（动手构造）

这是本讲的主实践任务：用 Rust 伪代码手动构造一棵 `<ul><li>文本</li></ul>`。

1. **目标**：用 `HtmlElement` 的真实 API 拼出一棵两层的 DOM 子树，并描述其内存结构。
2. **操作步骤**：以下是基于真实 API 的示例代码（**示例代码**，非项目原有代码，不能直接编译运行——它省略了 `use` 和 `tag::li`/`tag::ul` 常量的来源，且 `tag` 模块在 u2-l4 才细讲）：

   ```rust
   // 示例代码：手动构造 <ul><li>文本</li></ul>
   use typst_html::{HtmlElement, HtmlNode};
   use typst_html::tag;
   use ecow::eco_vec;

   // 1) 先造文本节点 "文本"
   let text = HtmlNode::text("文本", Span::detached());

   // 2) 造 <li>，把文本塞进去
   let li = HtmlElement::new(tag::li).with_children(eco_vec![text]);

   // 3) 造 <ul>，把 <li> 塞进去
   let ul = HtmlElement::new(tag::ul).with_children(eco_vec![li.into()]);
   ```
3. **观察现象 / 内存结构**：`ul` 的 `children` 是一个长度为 1 的 `EcoVec`，其唯一元素是 `HtmlNode::Element(li)`；而那个 `li` 的 `children` 又是长度为 1 的 `EcoVec`，元素是 `HtmlNode::Text("文本", ..)`。整体就是一棵深度为 2 的树：

   ```
   HtmlElement { tag: ul, children: [
       HtmlNode::Element( HtmlElement { tag: li, children: [
           HtmlNode::Text("文本")
       ]})
   ]}
   ```
4. **预期结果**：你能口述出「`children` 字段是树的递归边、`tag`/`attrs`/`css` 是节点的本地数据」。
5. **待本地验证**：若想真正运行，需要在一个能引用 `typst-html` crate 的工程里编译；本练习以「理解 API 与内存结构」为目的，不强求运行。

#### 4.2.5 小练习与答案

**练习 1**：`HtmlElement::css` 和用户在 Typst 里用 `html.elem` 写的 `style` 属性有什么关系？为什么分开存放？

> **答案**：`css` 字段只存放**编译器自动生成**的 CSS（如 `display`、`white-space: pre-wrap`），而用户手写的 `style` 是 `attrs` 里的一个普通属性。分开存放是为了让编译器生成的样式不与用户样式互相覆盖；最终在 `resolve_inline_styles`（u4-l3）里把 `css` 合并进 `style` 属性。

**练习 2**：`with_css` 为什么是 `pub(crate)` 而不是 `pub`？

> **答案**：因为 `css` 字段语义上是「编译器内部用的生成样式」，不希望外部使用者直接乱写；只在 crate 内部（转换器、规则等）需要设置它，所以收窄为 `pub(crate)`。

---

### 4.3 HtmlOutput：扁平节点表与 root_index

#### 4.3.1 概念说明

建好根元素后，自然会想用「根元素 + 它的孩子递归」来表示整份文档。但 typst-html 没有这么做——它定义了一个看起来很「扁」的容器 `HtmlOutput`：

```rust
pub struct HtmlOutput {
    nodes: EcoVec<HtmlNode>,   // 扁平的节点列表
    root_index: usize,         // 根元素在这个列表里的下标
}
```

也就是说，**顶层节点是平铺在一个一维数组里的，再用一个 `root_index` 指出哪个是根**。为什么要这样？

原因是：用户内容在 realization 后、包外层 `<html>`/`<body>` 之前，节点流里可能夹着若干**内省 `Tag` 标记**。当用户自己提供了 `<html>` 元素时（见 `finalize_dom`），这些标记会排在 `<html>` 元素前面，导致根元素并不在数组的第 0 位。`root_index` 就是为了精确指出「不管标记把根挤到哪里，根都在这个下标」。

> 注意：`HtmlOutput` 没有 `pub fn new`——它只在 `finalize_dom` 内部用结构体字面量直接构造，外部拿到的都是只读引用。

#### 4.3.2 核心流程

`HtmlOutput` 由 `finalize_dom`（document.rs）产出，有两条路径：

1. **用户已提供 `<html>`**：直接 `HtmlOutput { nodes, root_index: idx }`，`idx` 是用户 `<html>` 元素在原 `nodes` 里的下标。
2. **默认路径**：把用户内容包进 `<body>`、再包进 `<html>`，于是根元素是新生成的 `<html>`，放在列表第 0 位，`root_index: 0`。

读取侧的四个方法分别给出只读/可变的根元素引用，以及「连包装节点一起」的根节点引用。

#### 4.3.3 源码精读

结构体与字段，注意只有两个字段、且都非 `pub`（强制通过方法访问）：

[`HtmlOutput` 结构体定义 — document.rs:L220-L225](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L220-L225) — 注释把它定位为「HTML 编译的可内省产物」。

访问方法里藏着两个关键细节：`root()` 用 `match` 断言根必须是 `Element`（否则 panic）；`root_mut()` 通过 `nodes.make_mut()` 拿到写时复制后的可变引用：

[`HtmlOutput` 的访问方法 — document.rs:L227-L253](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L227-L253) — `root()` / `root_mut()` / `root_node()` 三者区别：前两个剥掉 `HtmlNode` 包装直接给 `&HtmlElement`，第三个保留包装。

`finalize_dom` 里能直接看到两种构造路径。重点看这条「用户自带 `<html>`」的提前返回分支，它把 `root_index` 设成了 `idx`（而非 0），正是 `root_index` 字段存在的直接证据：

[`finalize_dom 中用户自带 html 的分支 — document.rs:L269-L288](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L269-L288) — `(tag::html, 1)` 分支提前 `return Ok(HtmlOutput { nodes, root_index: idx })`；而默认路径在函数末尾构造 `HtmlOutput { nodes: eco_vec![html.into()], root_index: 0 }`。

默认路径的收尾（包 `<body>`、包 `<html>`、`root_index: 0`）：

[`finalize_dom 默认路径收尾 — document.rs:L290-L311](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L290-L311) — 默认情况下根元素 `<html>` 总在第 0 位，故 `root_index: 0`。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：验证「用户自带 `<html>` 时根元素不在第 0 位」这一论断。
2. **步骤**：在 `finalize_dom`（document.rs:L255-L311）里，对照阅读 `count` 的计算（L266：统计非 `Tag` 的节点数）与 `match (tag, count)` 的三个分支。
3. **观察现象**：当用户文档里只有一个 `<html>` 元素时走 `(tag::html, 1)` 分支提前返回，`root_index = idx`；若用户写的是 `<body>` 而非 `<html>`，则 `needs_body = false` 但仍会外包一层 `<html>`，最终 `root_index: 0`。
4. **预期结果**：你能解释「为什么 `HtmlOutput` 必须存 `root_index` 而不是假设根永远在 `nodes[0]`」——因为内省 `Tag` 可能排在用户根元素前面。`finalize_dom` 的完整逻辑（含脚注）是 u3-l2 的主题。

#### 4.3.5 小练习与答案

**练习 1**：`HtmlOutput::root()` 为什么用 `match` + `panic!("expected HTML element")` 而不是返回 `Option`？

> **答案**：这是内部不变式（invariant）：`root_index` 指向的节点在构造时就被保证是 `Element`。这是一个「外部无法触发的程序员错误」而非「运行时可能缺失的正常情况」，所以用 panic 而非 `Option` 更符合 Rust 习惯。

**练习 2**：假设没有 `root_index` 字段、固定取 `nodes[0]`，会在什么场景下出错？

> **答案**：当用户自己写了 `<html>` 元素、且其前面排有内省 `Tag` 节点时，真正的根元素不在 `nodes[0]`，固定取第 0 位会取到一个 `Tag`，`root()` 会 panic。

---

### 4.4 HtmlSliceExt：把内部节点表对齐到真实 DOM 序号

#### 4.4.1 概念说明

这是个容易一眼略过、但其实非常精巧的工具。问题背景：内省系统想知道「某个 Typst 元素落在了它祖先的第几个 DOM 孩子上」。但 `children: EcoVec<HtmlNode>` 的**数组下标并不等于真实 DOM 的孩子序号**，原因有二：

1. **`Tag` 节点不产生 DOM 孩子**：它在 HTML 里什么都不输出，所以不该占用一个 DOM 序号。
2. **相邻文本节点会被合并**：浏览器会把连续的文本节点视作一个 DOM 文本孩子，所以多个相邻 `Text` 应共用同一个 DOM 序号。

`HtmlSliceExt::iter_with_dom_indices` 就是来解决这个「数组下标 → DOM 序号」映射的。它是一个针对 `[HtmlNode]`（节点切片）的扩展 trait。

#### 4.4.2 核心流程

它本质上是一个带状态的遍历，维护两个状态：

- `cursor`：下一个待分配的 DOM 序号。
- `was_text`：上一个节点是不是文本（用来判断是否正处于一个「文本组」中）。

对每个孩子，按下表分派（设进入本步前 `i` 已被初始化为 `cursor`）：

| 当前节点 | 动作 | 返回序号 |
| --- | --- | --- |
| `Tag` | 什么都不做 | `cursor`（不前进） |
| `Text` | 置 `was_text = true` | `cursor`（不前进，与相邻文本共享） |
| `Element` / `Frame` | 若 `was_text` 则 `cursor += 1`（结清上一文本组的占用）；`i = cursor`；`cursor += 1`；清 `was_text` | 更新后的 `cursor`（前进） |

把游标更新写成公式（仅对结构节点）：

\[ \text{cursor}' \;=\; \text{cursor} \;+\; \mathbb{1}_{\text{was\_text}} \;+\; 1 \]

其中 \(\mathbb{1}_{\text{was\_text}}\) 表示「前一个是文本」时多加的 1——它是在为「刚结束的那个文本组」补占一个 DOM 序号。直观理解：**文本组「借用」了一个序号却不立即推进游标，由紧随其后的第一个结构节点代为推进。**

#### 4.4.3 源码精读

trait 定义，注释把两条规则讲得很清楚（`Tag` 不前进；相邻文本共享序号）：

[`HtmlSliceExt trait — dom.rs:L147-L159](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L147-L159) — 注意它返回 `impl Iterator<Item = (&HtmlNode, usize)>`，把每个节点和它的 DOM 序号配对送出。

实现就是上文那张状态表的直译：

[`iter_with_dom_indices 实现 — dom.rs:L161-L180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L161-L180) — `cursor += usize::from(was_text)` 这一句正是「为上一文本组补占序号」的体现。

模块自带的单元测试用一个 9 节点序列验证了映射结果：

[`test_iter_with_dom_indices — dom.rs:L545-L567](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L545-L567) — 输入 `[文本A, span, 文本"hi", 文本" you", Tag, 文本" there", span, 文本" my", 文本" friend!"]`，断言 DOM 序号为 `[0, 1, 2, 2, 2, 2, 3, 4, 4]`。

对照这个断言验证你的理解：`文本A→0`；`span→1`（结清文本A，占用 1）；`hi/你/Tag/there` 这「三文本夹一Tag」共享 `2`；第二个 `span→3`；`my/friend!` 共享 `4`。

#### 4.4.4 代码实践（预测型）

1. **目标**：用自己的节点序列预测 DOM 序号，再与算法对照。
2. **步骤**：取测试里的节点序列，先**手算**一遍每个节点应得的 DOM 序号，再与断言 `[0,1,2,2,2,2,3,4,4]` 比对。
3. **观察现象**：重点体会「`Tag` 夹在两个文本之间」时——它和前后的文本共享同一个序号（测试里 Tag 得到 `2`，与相邻文本一致），因为它本身不产生 DOM 孩子。
4. **预期结果**：你能不看代码、仅凭状态表规则，正确预测任意节点序列的 DOM 序号。

#### 4.4.5 小练习与答案

**练习 1**：若一个元素的 `children` 是 `[Tag, Tag, Element]`，三个节点分别得到什么 DOM 序号？

> **答案**：`Tag→0`（不前进）、`Tag→0`（不前进）、`Element`：`was_text` 为 false 故 `cursor` 不补加，`i = cursor = 0`，随后 `cursor = 1`。所以三者分别得到 `0, 0, 0`。前导的纯 `Tag` 不占用序号。

**练习 2**：为什么 `Text` 不自己推进 `cursor`，而要等下一个结构节点来「结清」？

> **答案**：因为无法在遇到文本时就知道后面还有没有相邻文本。把推进延迟到「下一个结构节点出现」，才能让一整组相邻文本共享同一个序号——这正对应浏览器把连续文本节点合并为一个 DOM 文本孩子的行为。

---

### 4.5 HtmlDocument：顶层产物与 Output 抽象

#### 4.5.1 概念说明

`HtmlDocument` 是 typst-html 编译的**最终产物**，也是 `typst::compile::<HtmlDocument>` 的返回类型。它把三样东西打包在一起：

- `output: HtmlOutput`：DOM 本体（上一节的盒子）。
- `info: DocumentInfo`：文档元信息（标题、作者、描述、关键字、语言等）。
- `introspector: Arc<HtmlIntrospector>`：基于 `output.nodes()` 构建的内省器，支撑 `query` 与跳转。

它同时实现两个 trait：

- `Document`（提供 `info()`）：让它能被通用代码当作「一份文档」对待。
- `Output`（提供 `target()` / `create()` / `introspector()`）：这是 typst-html 与 typst 核心的**唯一耦合点**（依赖倒置），`create()` 转发到 `html_document`。

#### 4.5.2 核心流程

`HtmlDocument` 的诞生分两步：

1. `html_document_common` 建好 `HtmlOutput` 与 `info`，调用 `HtmlDocument::new(output, info)`。
2. `new` 内部立刻用 `output.nodes()` 构造 `HtmlIntrospector` 并包进 `Arc`。

一个**非常重要**的设计约束写在结构体注释里：`HtmlDocument` **不实现 `Hash`**。原因有二：

- 内省器本身不可哈希；
- 由于 `root_mut` 的存在（用于交叉链接），内省器并不保证 100% 由 output 派生。

这直接影响了 comemo 缓存的设计（u6-l4）：memoize 的壳包在 `html_document_impl` 这一层，而 `HtmlDocument` 作为返回值不能进哈希。

#### 4.5.3 源码精读

结构体与「为何不实现 Hash」的权威注释：

[`HtmlDocument 结构体定义 — dom.rs:L20-L30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L20-L30) — 只 `derive(Debug, Clone)`，没有 `Hash`；注释点名了 `root_mut` 与 issue #7951。

`new` 的全部逻辑：构造 output、收下 info、用 `output.nodes()` 建内省器：

[`HtmlDocument::new — dom.rs:L36-L39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L36-L39) — 内省器在构造时就建好并 `Arc` 包裹，后续 `introspector()` 只读共享、`introspector_mut()` 用 `Arc::make_mut` 写时复制。

`Output` trait 实现：`target()` 返回 `Target::Html`、`create()` 转发到 `crate::html_document`——这就是 u1-l3 里「`T::create()` 转发到 `html_document`」的落点：

[`Output for HtmlDocument — dom.rs:L81-L97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L81-L97) — 这是 typst-html 与核心引擎的耦合点；`introspector()` 把 `Arc<HtmlIntrospector>` 解引用为 `&dyn Introspector`。

而 `html_document`（document.rs）只是把 `Engine` 拆成若干 `Tracked` 参数后转给带 memoize 缓存的 `html_document_impl`：

[`html_document 入口 — document.rs:L24-L40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L24-L40) — 注意它把 `engine.introspector.into_raw()`、`TrackedMut::reborrow_mut(&mut engine.sink)` 等逐个传入，这是 comemo 缓存要求所有「可变/跟踪」参数都必须显式列出的体现（u6-l4 细讲）。

#### 4.5.4 代码实践（跟踪调用链）

1. **目标**：把 `HtmlDocument` 的诞生与 `Output::create` 串起来。
2. **步骤**：从 `Output for HtmlDocument::create`（dom.rs:L90-L96）出发 → 它调 `crate::html_document`（document.rs:L25-L40）→ 后者调 `html_document_impl`（document.rs:L45）→ 内部 `html_document_common`（document.rs:L128）→ 末尾 `HtmlDocument::new(output, info)`（document.rs:L217）。
3. **观察现象**：注意 `html_document_impl` 在拿到 `HtmlDocument` 后还做了两件事——`create_link_anchors`（document.rs:L69）和 `introspector_mut().set_anchors`（document.rs:L70）——这正是「`root_mut` 会改动 DOM、所以不能 Hash」的一个具体源头。
4. **预期结果**：你能画出「`create` → `html_document` → `html_document_impl` → `html_document_common` → `HtmlDocument::new` + 链接锚点后处理」的调用序列。

#### 4.5.5 小练习与答案

**练习 1**：`HtmlDocument` 为什么不 `derive(Hash)`？这对缓存有什么影响？

> **答案**：因为内省器不可哈希，且 `root_mut`（用于交叉链接锚点）会让 DOM 在构造后被修改，使内省器不保证 100% 由 output 派生。影响是：comemo 的 memoize 只能包在 `html_document_impl`（参数都可哈希）这一层，而不能把 `HtmlDocument` 本身作为缓存键。

**练习 2**：`HtmlDocument::introspector_mut` 是如何做到「只读时共享、写入时独占」的？

> **答案**：内省器存在 `Arc<HtmlIntrospector>` 里；`introspector_mut` 用 `Arc::make_mut`——若引用计数为 1 则原地可变借用，否则先克隆再改。这样多数字文档共享同一份只读内省器，只有真正需要修改（如 `set_anchors`）时才复制。

---

## 5. 综合实践

把本讲的五个最小模块串成一个综合任务：**手工「假装」编译器，从零拼出一份最小的 `HtmlDocument`，并预测它的 DOM 序号与编码结果。**

1. **任务背景**：假设有一份 Typst 文档，内容大致是「一个无序列表，含一项文本」。我们手动模拟转换器产出 DOM、再用 `HtmlOutput` 与 `HtmlDocument` 装箱。
2. **操作步骤（示例代码，非项目原有代码，仅用于理解结构）**：

   ```rust
   // 示例代码：手工拼装一份最小 HtmlDocument 的 DOM 部分
   use typst_html::{HtmlElement, HtmlNode, HtmlDocument};
   use typst_html::tag;
   use ecow::eco_vec;

   // (a) 用 4.2 的方法造 <ul><li>Hi</li></ul>
   let li = HtmlElement::new(tag::li)
       .with_children(eco_vec![HtmlNode::text("Hi", Span::detached())]);
   let ul = HtmlElement::new(tag::ul).with_children(eco_vec![li.into()]);

   // (b) 包一层 <body>，再包 <html>（模拟 finalize_dom 默认路径）
   let body = HtmlElement::new(tag::body).with_children(eco_vec![ul.into()]);
   let html = HtmlElement::new(tag::html).with_children(eco_vec![body.into()]);

   // (c) 这就是 HtmlOutput 默认路径的形态：根 <html> 在 nodes[0]
   //     HtmlOutput { nodes: eco_vec![html.into()], root_index: 0 }
   //
   // (d) 最终 HtmlDocument::new(output, info) 会用 output.nodes() 建内省器
   ```
3. **需要回答的问题**：
   - 画出这棵 DOM 的树状结构（提示：`html > body > ul > li > Text("Hi")`）。
   - 对 `<ul>` 的 `children`（即 `[Element(li)]`）调用 `iter_with_dom_indices`，得到的序号是什么？（答：`[0]`，因为单个元素节点得序号 0。）
   - 若在 `<li>` 的文本前再插一个内省 `Tag`，使 `children = [Tag, Text("Hi")]`，序号会变成什么？（答：`[0, 0]`——`Tag` 不前进，文本与之共享序号 0。）
   - `html()` 编码时，`Tag` 会被输出到 HTML 字符串里吗？（答：不会，编码阶段忽略 `Tag`。）
4. **预期结果**：你能把 `HtmlNode`（四种变体）、`HtmlElement`（字段与 builder）、`HtmlOutput`（扁平表 + root_index）、`HtmlSliceExt`（DOM 序号映射）、`HtmlDocument`（顶层封装 + 不 Hash）这五者在脑海里连成一张「从内容到成品」的数据流图。
5. **待本地验证**：真正的转换流程由 `convert_to_nodes`（u3-l3）自动完成，本实践是「手工模拟」以加深对数据结构的理解；若要运行需在能引用 `typst-html` 的工程中进行。

## 6. 本讲小结

- `HtmlNode` 是 DOM 节点的统一枚举，四种变体 `Tag` / `Text` / `Element` / `Frame` 分别承担「内省元数据 / 文本 / 嵌套元素 / 内联 SVG」四种角色。
- `HtmlElement` 是承重类型，七个字段分三组：内容组（`tag`/`attrs`/`children`）、样式组（`css`，编译器生成）、元信息组（`parent`/`span`/`pre_span`）；用 builder 链构造。
- `HtmlOutput` 用「扁平 `nodes` + `root_index`」而非显式树，是为了应对用户自带 `<html>` 时根元素不在第 0 位（前面可能排着内省 `Tag`）。
- `HtmlSliceExt::iter_with_dom_indices` 是一个状态机，把内部数组下标对齐到真实 DOM 孩子序号：`Tag` 不前进、相邻 `Text` 共享序号、结构节点结清文本组并前进。
- `HtmlDocument` 是顶层产物，封装 `output` + `info` + `introspector`；它实现 `Output`（转发 `html_document`）但**不实现 `Hash`**，因为内省器不可哈希且 `root_mut` 会事后改 DOM。
- `HtmlFrame` 作为 `HtmlNode` 的一个变体，承载「排版后以内联 SVG 嵌入」的内容，其细节（`text_size`/`anchors`/`id`）将在 u6-l1 展开。

## 7. 下一步学习建议

本讲只读了数据结构本身，还没有讲「这些结构是怎么从 Typst 内容造出来的」。建议按以下顺序继续：

1. **u2-l2（HtmlTag 与字符串驻留）**：深入 `HtmlTag` 的 `intern`/`constant` 校验与 `PicoStr` 驻留机制，理解本讲里反复出现的 `tag::ul`、`tag::li` 等常量是怎么来的。
2. **u2-l3（HtmlAttr 与 HtmlAttrs）**：精读 `HtmlAttrs` 的 `push`/`get`/`Fold` 合并语义，补齐 `HtmlElement::attrs` 字段的细节。
3. **u3-l1（文档编译主链路 html_document）**：把本讲的 `HtmlDocument` 放回编译主流程，看 `html_document_common` 如何一步步产出 `output` 与 `info`。
4. 若你想提前看「DOM 如何变成字符串」，可跳读 u5-l1（DOM 到 HTML 字符串的编码），但建议先完成 u2 系列以建立完整的字段认知。
