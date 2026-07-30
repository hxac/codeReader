# 文档编译主链路 html_document

## 1. 本讲目标

本讲是「编译与转换主流程」单元的第一讲，目标是带你走通 typst-html 的**编译主链路**：一份 Typst 文档内容（`Content`）是怎样一步步变成 `HtmlDocument` 的。

学完后你应该能做到：

- 读懂 `html_document` → `html_document_impl` → `html_document_common` 这个**三层函数结构**的分工，并解释为什么要这样拆。
- 理解 `comemo::memoize` 为什么**只包在中间一层**，以及它带来的缓存边界。
- 掌握 `RealizationKind::Document` 的作用，以及为什么根级样式要经 `styles.outside()` 标记。
- 理解 `DocumentInfo`（标题、作者、语言等元信息）是如何被填充的。
- 理解当文档含数学公式时，`EQUATION_CSS_STYLES` 是如何被注入到 `<head>` 里的。

学完本讲，你应能独立画出从 `Content` 到 `HtmlDocument` 的数据流图，并标注每一步的输入与输出类型。

## 2. 前置知识

本讲建立在 **u1-l3（导出调用链）** 和 **u2-l1（DOM 数据模型）** 之上。如果你还没读，建议先看。这里回顾几个关键概念：

- **`Content`**：Typst 排版内容的通用表示，可以是文本、段落、标题、数学公式等任意元素。它是编译主链路的**输入**。
- **`HtmlDocument`**（u2-l1）：typst-html 编译的**最终产物**，内部封装了三块——`output`（扁平节点数组 `HtmlOutput`）、`info`（`DocumentInfo` 元信息）、`introspector`（内省器，`Arc` 包裹）。
- **`Engine` / `Route` / `Sink` / `Tracked` / `TrackedMut`**（u1-l3）：Typst 引擎在 `comemo` 增量计算框架下使用的「被追踪引用」。简单说，`Tracked<T>` 是 `T` 的只读代理，`TrackedMut<T>` 是可写代理，它们让 `comemo` 能追踪依赖关系从而做缓存。
- **`Target::Html`**（u1-l3）：编译目标之一（另有 `Paged`、`Bundle`）。本讲讨论的就是 `Target::Html` 下的编译路径。
- **`Output` trait**（u1-l3）：`HtmlDocument` 实现了它，其 `create()` 转发到本讲的 `html_document`，这是 typst-html 与 typst 核心引擎的**唯一耦合点**。

> 一个术语提醒：本讲的「编译」特指 `Content → HtmlDocument`（DOM 树），与后续「编码」`HtmlDocument → HTML 字符串`（u5-l1）是两个分离的阶段。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-html/src/document.rs` | **本讲主文件**。三层函数 `html_document` / `html_document_impl` / `html_document_common` 全部在此，主链路的每一步都在这里。 |
| `crates/typst-html/src/mathml.rs` | 定义 `EQUATION_CSS_STYLES` 常量（数学公式 CSS 覆盖样式）。 |
| `crates/typst-library/src/routines.rs` | 定义 `realize` 函数签名与 `RealizationKind` 枚举（含 `Document` 变体）。 |
| `crates/typst-library/src/foundations/styles.rs` | 定义 `Styles::outside()`，把样式标记为「文档级」。 |
| `crates/typst-library/src/model/document.rs` | 定义 `DocumentInfo` 及其 `populate` / `populate_locale` 方法。 |

本讲主要围绕 `document.rs` 这一个文件展开，其余四个文件只是为了讲清楚「跨 crate 调用点」与「被复用的核心类型」。

## 4. 核心概念与源码讲解

### 4.1 三层函数结构与 comemo::memoize

#### 4.1.1 概念说明

`html_document` 这个名字其实对应**三个**函数，叠成三层：

1. **外层 `html_document`**：公开入口，把 `Engine` 里的字段拆成 `Tracked`/`TrackedMut` 引用，转发给内层。
2. **中层 `html_document_impl`**：用 `#[comemo::memoize]` 包裹，是**缓存边界**；它调用共享核心，再做「链接锚点」两步后处理。
3. **内层 `html_document_common`**：**真正干活**的共享核心，既被独立文档编译用，也被 bundle（多文档打包）编译用。

为什么要拆成三层？核心矛盾有两个：

- **缓存复用 vs 副作用**：编译很贵，希望用 `comemo` 缓存；但 `Engine` 这种「带可变状态」的结构无法直接作为缓存键。所以外层把 `Engine` 拆成一堆**值类型/被追踪引用**（`Tracked<...>`），交给中层做缓存。
- **复用 vs 差异**：独立文档和 bundle 编译的**核心流程完全相同**（realize → convert → finalize → ...），只有「是否需要顶层链接锚点」这点差异。所以把核心抽到 `html_document_common`，差异留在各自的中层。

#### 4.1.2 核心流程

```
            外层 html_document(engine, content, styles)
                 │  拆解 Engine 字段为 Tracked/TrackedMut
                 ▼
   中层 html_document_impl(world, library, introspector, traced,
            │      sink, route, content, styles)   ← #[comemo::memoize]
            │      ┌──────────────────────────────────────────┐
            │      │ ① html_document_common(...) → HtmlDocument │  （共享核心，干活）
            │      │ ② create_link_anchors(&mut doc, targets)   │  （两步后处理）
            │      │ ③ doc.introspector_mut().set_anchors(...)  │
            │      └──────────────────────────────────────────┘
            ▼
        返回 HtmlDocument
```

注意：bundle 编译走的是平行的 `html_document_for_bundle` → `html_document_for_bundle_impl`（同样 memoize），但它的中层**只调用 `html_document_common`，跳过两步后处理**——这正是把核心抽出来的收益。

#### 4.1.3 源码精读

外层入口，把 `Engine` 拆解为被追踪引用后转发（注意它本身**不 memoize**，因为参数里带了 `&mut Engine`，无法做缓存键）：

[document.rs:24-40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L24-L40) — 外层 `html_document`：`#[typst_macros::time]` 计时，把 `engine.world / library / introspector / traced / sink / route` 逐个拆出来转发。

中层，`#[comemo::memoize]` 是缓存边界，内部先调核心、再做两步锚点后处理：

[document.rs:42-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L42-L73) — `html_document_impl`：先 `html_document_common(...)` 得到 `HtmlDocument`；接着 `link_targets()` 取出所有「被链接的目标」，`create_link_anchors` 给它们分配人类可读的 fragment ID，最后 `set_anchors` 把 `Location → ID` 映射写回内省器。

bundle 的平行中层，证明核心是被复用的：

[document.rs:97-123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L97-L123) — `html_document_for_bundle_impl`：只 `LocatorLink::new` 后直接转发给 `html_document_common`，**没有**锚点两步。

> **缓存与副作用为何不打架**：`comemo` 用**输入参数**做缓存键，缓存的是函数**返回值**。锚点两步对返回值的就地修改（`create_link_anchors(&mut document, ...)`）发生在冷路径（缓存未命中时）的函数体内，修改完的 `document` 才被存进缓存；命中时直接克隆缓存值。因此缓存的永远是「已挂锚点」的文档。这与 u2-l1 提到的「`HtmlDocument` 不实现 `Hash`、`root_mut` 会事后改 DOM、故缓存只包在 `html_document_impl` 层」完全一致——再往里就不缓存了，避免把可变状态卷入缓存。

#### 4.1.4 代码实践

**实践目标**：确认三层函数的调用与被调用关系，以及 bundle 路径的差异。

**操作步骤**：

1. 打开 `crates/typst-html/src/document.rs`。
2. 分别定位 `pub fn html_document`、`fn html_document_impl`、`fn html_document_common`、`fn html_document_for_bundle_impl`。
3. 在 `html_document_impl` 中找出「调核心」与「两步后处理」之间的分界线。

**需要观察的现象**：

- `html_document_impl` 在 `html_document_common(...)?` 之后，紧接着是 `link_targets()` / `create_link_anchors` / `set_anchors` 三行——这就是「创建 `HtmlDocument` 后的两步后处理」。
- `html_document_for_bundle_impl` 里**找不到**这三行。

**预期结果**：你能说清「独立文档编译会做锚点后处理，bundle 编译不会」，并能解释这是因为核心流程被抽到了 `html_document_common`。

> 待本地验证：如果你想实际观察 memoize 命中行为，可在 `html_document_impl` 顶部临时加一行 `eprintln!("html_document_impl called");`，用同一文档连续编译两次，观察打印次数（注意：本实践为源码阅读型，修改源码仅用于本地学习，不要提交）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `html_document`（外层）没有标 `#[comemo::memoize]`，而 `html_document_impl`（中层）标了？

**参考答案**：外层接收 `&mut Engine`，`Engine` 含可变状态（`sink`、`route` 等），无法作为 `comemo` 的缓存键；`comemo` 要求参与哈希的参数是值或被追踪引用。所以外层把 `Engine` 拆解成 `Tracked`/`TrackedMut` 引用后，由中层以这些可哈希参数为键做缓存。

**练习 2**：如果把「锚点两步后处理」从 `html_document_impl` 搬进 `html_document_common`，会有什么后果？

**参考答案**：bundle 编译（`html_document_for_bundle_impl`）也复用 `html_document_common`，搬进去后 bundle 文档也会被迫执行顶层锚点逻辑，破坏了「核心共享、差异分离」的设计；同时把就地修改卷进更深的共享层，会进一步模糊缓存边界。

---

### 4.2 html_document_common 主链路总览

#### 4.2.1 概念说明

`html_document_common` 是整条主链路的「干活者」。它把一份 `Content` 经过 **realize（具象化）→ convert（转 DOM）→ finalize（套骨架）→ resolve（写内联样式）→ 注入方程 CSS** 五大阶段，最终装进 `HtmlDocument`。

直觉上可以这么理解整个流水线：

- **realize**：Typst 的 `Content` 是「什么都有可能」的通用树，先把**用户自定义的 show 规则全部展开**，得到一组「Typst 已知的、带样式的元素」清单（标题、段落、强调、列表……）。
- **convert**：把这份清单逐类翻译成 **HTML DOM 节点**（`HtmlNode`）。
- **finalize**：给这堆节点**套上 `<html>/<head>/<body>` 骨架**，并附加脚注容器。
- **resolve**：把编译过程中累积的 CSS 写成**内联 `style` 属性**。
- **方程 CSS**：若文档含数学公式，往 `<head>` 注入一段覆盖 MathML 默认样式的 `<style>`。

#### 4.2.2 核心流程

`html_document_common` 的处理步骤（每步标注输入→输出类型）：

```
输入: content: &Content, styles: StyleChain, locator: Locator, (engine 组件)

 ① 重建 Engine
      └─ Protected::from_raw(introspector) + locator.split() + Route::extend(..).unnested()
 ② 预留 footnote_locator = locator.next(&())   （脚注专用定位器，提前分配以求稳定）
 ③ styles = styles.to_map().outside()           （标记外部样式为文档级，见 4.3）
 ④ info = DocumentInfo::default()
      info.populate(styles)        （标题/作者/描述/关键字/日期）
      info.populate_locale(styles) （语言/地区）
 ⑤ children ← routines.realize(RealizationKind::Document { info }, …, content, styles)
      输入: &Content            输出: Vec<Pair>   （已具象化的带样式元素清单）
 ⑥ nodes ← convert::convert_to_nodes(children, ConversionLevel::Block, Whitespace::Normal)
      输入: Vec<Pair>          输出: EcoVec<HtmlNode>
 ⑦ output ← finalize_dom(engine, nodes, &info, footnote_locator, footnote_styles)
      输入: EcoVec<HtmlNode>   输出: HtmlOutput  （套好 <html>/<head>/<body> 骨架）
 ⑧ css::resolve_inline_styles(output.root_mut())
      把元素 css 字段写成内联 style 属性
 ⑨ 若 has_equations → 向 <head> 注入 <style>EQUATION_CSS_STYLES</style>
 ⑩ HtmlDocument::new(output, info)   （内部还构造了 HtmlIntrospector）
      输入: HtmlOutput + DocumentInfo   输出: HtmlDocument
返回 → 上层（html_document_impl）再做两步锚点后处理
```

一个关键细节：第 ⑧ 步（写内联样式）**必须放在 `finalize_dom` 之后**，因为 `finalize_dom` 会插入新的带样式 DOM 节点（例如脚注容器），这些新增节点的样式也要一并解析。源码注释明确说明了这一点。

#### 4.2.3 源码精读

整个 `html_document_common` 的函数体（含文档注释）：

[document.rs:125-218](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L125-L218) — 共享核心，串联 realize → convert_to_nodes → finalize_dom → resolve_inline_styles → 方程 CSS 注入 → `HtmlDocument::new`。

其中「写内联样式必须最后做」的依据，是源码中的注释：

[document.rs:188-190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L188-L190) — 注释说明 `finalize_dom` 可能插入了更多带样式的 DOM 节点，因此样式解析必须放到最后。

最后一步 `HtmlDocument::new` 的语义（来自 u2-l1）：

[dom.rs:32-39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L32-L39) — `HtmlDocument::new` 内部用 `output.nodes()` 构造 `HtmlIntrospector`，并用 `Arc` 包裹（因此后续 `root_mut` 改 DOM 是「事后副作用」，这也是 HtmlDocument 不实现 Hash 的原因，详见 u2-l1 / u6-l4）。

#### 4.2.4 代码实践

**实践目标**：把 `html_document_common` 的 10 个步骤与源码行号一一对应。

**操作步骤**：

1. 打开 `document.rs`，从第 138 行函数体开始逐行阅读。
2. 对照 4.2.2 的流程图，给每一步（① 重建 Engine … ⑩ `HtmlDocument::new`）找到对应的代码行范围。
3. 特别留意：`info` 变量在 **第 ④ 步先创建填充**，又在 **第 ⑤ 步作为 `RealizationKind::Document { info: &mut info }` 传入 realize**，最后在 **第 ⑩ 步与 output 一起交给 `HtmlDocument::new`**——也就是说 `DocumentInfo` 既在 realize 阶段被继续填充，又作为最终元信息保留。

**需要观察的现象**：`info` 是 `let mut info`，它的可变引用在 realize 期间被借用，借出后又用于构造最终文档。

**预期结果**：你能准确说出 `info` 的「三次出场」（创建填充 / 传入 realize / 构造 HtmlDocument），并理解它是一条贯穿主链路的数据线。

> 待本地验证：可在每个步骤之间临时插入 `eprintln!` 打印类型名，确认数据流；本实践为源码阅读型。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `resolve_inline_styles` 要放在 `finalize_dom` 之后，而不是 `convert_to_nodes` 之后立刻做？

**参考答案**：`finalize_dom` 会向 DOM 追加新的带样式节点（如脚注容器），如果在它之前解析内联样式，这些后插入节点的样式就不会被写入 `style` 属性。因此样式解析必须放在所有结构改动完成之后。

**练习 2**：`convert_to_nodes` 调用时传入了 `ConversionLevel::Block` 和 `Whitespace::Normal` 两个参数，它们分别暗示了什么？

**参考答案**：`ConversionLevel::Block` 表示从文档根开始按「块级」层级转换内容（块级/行内的层级判定详见 u4-l2）；`Whitespace::Normal` 表示根级采用「正常空白折叠」模式（空白保护机制详见 u4-l1）。本讲只需知道这是主链路的「默认起点配置」。

---

### 4.3 realize 与 RealizationKind::Document、styles.outside()

#### 4.3.1 概念说明

**realize（具象化）** 是 Typst 引擎的核心步骤：把任意 `Content`（可能包含用户写的 show 规则、函数调用）展开为一组「Typst 已知、可直接处理」的带样式元素清单。对 HTML 导出而言，它产出的是 `Vec<Pair>`——一组「元素 + 样式」配对，随后交给 `convert_to_nodes` 逐个翻译成 HTML。

`RealizationKind` 是一个枚举，告诉 realize「我们现在在哪一层做具象化」，因为不同层级的处理规则不同：

- `Document`：文档根级，需要**边具象化边收集 `set document(...)` 的元信息**。
- `Bundle`：bundle 打包根级。
- `Fragment`：容器内的嵌套具象化（如 `block`、`html.div` 内部）。
- `Par`：段落内的具象化。
- `Math`：数学公式内部。

本讲只关心 **`Document`** 变体。

#### 4.3.2 核心流程

realize 的调用形式（伪代码）：

```
children = routines.realize(
    RealizationKind::Document { info: &mut info },   // 把 info 可变借用交给 realize
    engine, locator, arenas,
    content,   // 输入: 待具象化的 Content
    styles,    // 输入: 已标记为 outside 的文档级样式
)
// 返回: Vec<Pair> —— 带样式元素清单
```

两件关键的事：

1. **`RealizationKind::Document { info: &mut info }`**：把 `DocumentInfo` 的可变引用借给 realize。这样 realize 在遇到 `set document(title: "...", author: "...")` 等 set 规则时，能**就地**把值写进 `info`。这也是为什么 `info` 必须在 realize **之前**就创建好（第 ④ 步），并在 realize **之后**还能用于构造最终文档（第 ⑩ 步）。
2. **`styles.outside()`**：标记外部样式为「在文档级生效」。Typst 的某些 set 规则（如文档级配置）只有被标记为 outside 才允许提升到文档根层；否则会被当作「show 规则内部样式」而拒绝在文档级使用。

#### 4.3.3 源码精读

`RealizationKind` 枚举与各变体注释：

[routines.rs:153-169](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L153-L169) — `RealizationKind`：`Document { info: &'a mut DocumentInfo }` 变体的注释明确写道「需要一份指向文档元数据的可变引用，它会被 `set document` 规则填充」。

`realize` 函数的 trait 签名：

[routines.rs:81-89](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L81-L89) — `realize(kind, engine, locator, arenas, content, styles) -> SourceResult<Vec<Pair>>`。注意它返回的是 `Vec<Pair>`，即「带样式元素清单」。

主链路里 realize 的实际调用点（在第 ⑤ 步）：

[document.rs:163-170](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L163-L170) — `html_document_common` 调用 `engine.library.routines.realize`，传入 `RealizationKind::Document { info: &mut info }`。

`styles.outside()` 的标记（在第 ③ 步）：

[document.rs:153-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L153-L156) — 先 `styles.to_map()` 转成 `Styles` 值，再 `.outside()` 标记，最后重新包成 `StyleChain`。注释解释：把外部样式标记为「outside」才能在文档级合法使用。

`Styles::outside()` 的实现：

[styles.rs:92-102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L92-L102) — 遍历所有样式项，给 `Property` 和 `Recipe` 设置 `outside = true`（`Revocation` 不受影响）。其文档注释说：标记为「在任意 show 规则之外被应用」。

#### 4.3.4 代码实践

**实践目标**：理解 `RealizationKind::Document` 如何让 realize 反向填充 `DocumentInfo`。

**操作步骤**：

1. 在 `document.rs` 找到 `let mut info = DocumentInfo::default();`（约第 159 行）。
2. 顺着 `info` 往下看：第 160–161 行先用 `populate` / `populate_locale` 填一遍；第 164 行又把 `&mut info` 交给 realize。
3. 思考：为什么需要「先填一遍」再「交给 realize 继续填」？

**需要观察的现象**：`info` 的填充分两个来源——`populate` 直接从样式链读 `DocumentElem` 的字段（title/author/...），而 realize 会在展开 `set document(...)` 规则时再次更新它。

**预期结果**：你能解释「`populate` 处理已存在的文档 set 规则，realize 处理在具象化过程中遇到的文档 set 规则」，二者互补，保证 `info` 最终完整。

> 待本地验证：本实践为源码阅读型。若想验证 `outside()` 的作用，可尝试在本地构造一个被 show 规则包裹的文档级 set 规则，对比有无 `.outside()` 时是否报错（需谨慎构造示例）。

#### 4.3.5 小练习与答案

**练习 1**：`RealizationKind` 有五个变体，本讲用的是 `Document`。请说明它的「特别之处」是什么。

**参考答案**：`Document` 变体携带 `info: &'a mut DocumentInfo`，是唯一一个**需要借出文档元信息**的变体。realize 在展开 `set document(...)` 规则时会向这个 `info` 写入标题、作者等值；其他变体（`Bundle` / `Fragment` / `Par` / `Math`）不涉及文档级元信息收集。

**练习 2**：去掉 `styles.to_map().outside()` 这一行会怎样？

**参考答案**：外部样式不会被标记为「文档级」，Typst 会认为这些样式是在某个 show 规则内部应用的，从而拒绝把它们提升到文档根层使用，可能导致本应在文档级生效的配置（如 `set document`、`set text` 的部分作用）失效或报错。

---

### 4.4 DocumentInfo 填充与 EQUATION_CSS_STYLES 注入

#### 4.4.1 概念说明

本模块讲主链路的两个「数据填充」细节：

1. **`DocumentInfo` 填充**：从样式链读取用户通过 `set document(title: "...", author: (...), ...)` 和 `set text(lang: "...", region: "...")` 设置的元信息，存进 `DocumentInfo`。这些信息随后会变成 `<head>` 里的 `<title>`、`<meta name="description">`、`<meta name="authors">` 等标签（详见 u3-l2）。
2. **方程 CSS 注入**：如果文档里出现了数学公式（`EquationElem`），浏览器的 MathML 默认用户代理样式表（UA stylesheet）渲染效果会与 Typst 的分页导出有偏差。typst-html 因此在 `<head>` 注入一段 `<style>`，覆盖这些默认样式，让浏览器渲染尽量贴近 Typst。这段 CSS 就是 `EQUATION_CSS_STYLES`。

#### 4.4.2 核心流程

**DocumentInfo 填充**：

```
info = DocumentInfo::default()
info.populate(styles)        // 读 DocumentElem 的 title/author/description/keywords/date
info.populate_locale(styles) // 读 TextElem 的 lang/region
```

`populate` 对每个字段都用 `styles.has(...)` 先判断「是否显式 set 过」：set 过才覆盖默认值，避免误覆盖。`populate_locale` 则只在尚未自定义 locale 时，从 `set text(lang:, region:)` 提取语言与地区。

**方程 CSS 注入**（主链路第 ⑨ 步）：

```
has_equations = introspect(EquationElem::ELEM).is_empty() 取反   // 查询文档中是否有方程元素
if has_equations:
    root = output.root_mut()
    head = root.children 中找 tag == <head> 的元素
    head.children.push( <style>EQUATION_CSS_STYLES</style> )      // 在 <head> 末尾追加样式
```

注意判定方式是用**内省查询**（`EquationElem::ELEM.select()`）检查文档里是否存在任意方程元素，而不是静态地假设「有数学就一定有」。没有方程就不注入，避免无谓的 CSS。

#### 4.4.3 源码精读

主链路中 `DocumentInfo` 的创建与填充（第 ④ 步）：

[document.rs:159-161](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L159-L161) — `DocumentInfo::default()` 后调用 `populate(styles)` 与 `populate_locale(styles)`。

`DocumentInfo` 结构体与 `populate` / `populate_locale` 实现：

[model/document.rs:330-391](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/document.rs#L330-L391) — `DocumentInfo` 的字段（title/author/description/keywords/date/locale）及两个填充方法。`populate` 用 `styles.has(DocumentElem::title)` 等逐字段判断；`populate_locale` 在 `locale.is_custom()` 时提前返回，避免重复设置。

方程检测与 CSS 注入（第 ⑨ 步）：

[document.rs:192-215](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L192-L215) — 用 `engine.introspect(QueryIntrospection(EquationElem::ELEM.select(), ...))` 查询方程；若非空，则 `output.root_mut()` 拿到根元素，在它的 `children` 里 `find_map` 出 `tag == tag::head` 的 `<head>`，向其 `children` 追加一个 `<style>` 元素，内容为 `EQUATION_CSS_STYLES`。

`EQUATION_CSS_STYLES` 的定义与设计说明：

[mathml.rs:27-99](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/mathml.rs#L27-L99) — 顶部大段注释解释了这段 CSS 的目的（覆盖 [MathML Core UA 样式表](https://www.w3.org/TR/mathml-core/#user-agent-stylesheet)，让浏览器渲染尽量贴近 Typst 分页导出），分 Alignment / Tables / Equations 等几类规则；第 99 行是 `pub(crate) static EQUATION_CSS_STYLES: LazyLock<EcoString>`，用 `LazyLock` 延迟到首次使用时才生成字符串。

> 这段 CSS 的具体规则与数学公式到 MathML 的映射详见 **u5-l5（数学公式到 MathML 的转换）**；本讲只关心「它在主链路何时、如何被注入」。

#### 4.4.4 代码实践

**实践目标**：验证「有方程才注入 CSS、无方程不注入」的条件分支。

**操作步骤**：

1. 阅读 [document.rs:192-215](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L192-L215)，定位 `has_equations` 的判定与 `if has_equations { ... }` 分支。
2. 准备两份最小 Typst 文档（示例代码，非项目原有文件）：
   - 文档 A：含一行数学，如 `$ a^2 + b^2 = c^2 $`。
   - 文档 B：纯文本，无任何 `$ ... $`。
3. 分别编译为 HTML（`typst compile --format html`），打开生成的 HTML 查看 `<head>`。

**需要观察的现象**：

- 文档 A 的 `<head>` 里应能找到一段 `<style>...</style>`，内容是 `EQUATION_CSS_STYLES`（含 `/* Alignment */`、`mtable` 等规则）。
- 文档 B 的 `<head>` 里**没有**这段 `<style>`。

**预期结果**：你确认了「方程 CSS 的注入是条件性的，由内省查询决定」，而非无条件注入。

> 待本地验证：具体输出取决于本地 typst 版本（当前工作区为 0.15.1）。若无法运行 typst，本实践也可退化为源码阅读型——只追踪 `has_equations` 的取值如何决定是否进入 `if` 分支即可。

#### 4.4.5 小练习与答案

**练习 1**：`DocumentInfo` 的 `populate` 为什么对每个字段都用 `styles.has(...)` 先判断，而不是直接 `styles.get(...)`？

**参考答案**：`styles.has(field)` 判断该字段是否被显式 set 过。只有 set 过才覆盖默认值，避免用 `get` 返回的默认值去误覆盖（例如用户没设 author，就不该把空的默认 author 当成真实值写入）。这是一种「按需填充、保留默认」的保护逻辑。

**练习 2**：方程 CSS 注入用的是「内省查询」，而不是「编译时静态标记」。这种做法的好处是什么？

**参考答案**：内省查询 `EquationElem::ELEM.select()` 直接反映**最终文档里是否真的存在方程元素**（经过 show 规则展开、条件判断之后的结果），比编译期静态标记更准确——能正确处理「源码里有 `$...$` 但被 show 规则消掉了」或「方程是动态生成」的情况。没有方程时就不注入 CSS，保持输出精简。

---

## 5. 综合实践

**任务**：画出 `html_document_common` 的处理步骤流程图，标出每一步的**输入与输出类型**，并指出**创建 `HtmlDocument` 后还做了哪两步后处理**。

**操作步骤**：

1. 通读 [document.rs:125-218](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L125-L218) 的 `html_document_common` 全函数。
2. 仿照本讲 4.2.2 的流程图，自己画一份，但要把**每一步的输入类型 → 输出类型**标注清楚。至少应包含：
   - 重建 `Engine`（无显式输出，副作用）
   - `DocumentInfo` 填充（输入 `StyleChain` → 输出 `DocumentInfo`）
   - `realize`（输入 `&Content` → 输出 `Vec<Pair>`）
   - `convert_to_nodes`（输入 `Vec<Pair>` → 输出 `EcoVec<HtmlNode>`）
   - `finalize_dom`（输入 `EcoVec<HtmlNode>` → 输出 `HtmlOutput`）
   - `resolve_inline_styles`（输入 `&mut HtmlElement`，无返回值）
   - 方程 CSS 注入（条件分支，修改 `<head>`）
   - `HtmlDocument::new`（输入 `HtmlOutput + DocumentInfo` → 输出 `HtmlDocument`）
3. 在流程图末尾，标出**这两步是在 `html_document_common` 之外（即 `html_document_impl` 里）完成的**，并写出它们的函数名与输入输出。

**预期结果（参考答案）**：

创建 `HtmlDocument`（`html_document_common` 的 `Ok(HtmlDocument::new(output, info))`）之后，`html_document_impl` 还做了**两步后处理**：

1. **`create_link_anchors(&mut document, &targets)`**（见 [document.rs:69](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L69)）：遍历 DOM，为所有「被链接的目标元素」（`targets = document.introspector().link_targets()`）分配人类可读的 HTML fragment ID。输入是 `&mut HtmlDocument` 与 `&FxHashSet<Location>`，会就地修改 DOM。
2. **`document.introspector_mut().set_anchors(anchors)`**（见 [document.rs:70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L70)）：把上一步生成的 `Location → fragment ID` 映射（`FxHashMap<Location, EcoString>`）写回内省器，使得后续链接解析能找到目标。

> 为什么这两步必须在 `html_document_common` 之外？因为 bundle 编译路径（`html_document_for_bundle_impl`）复用了 `html_document_common` 但**不需要**顶层锚点逻辑；把锚点留在各自的 memoize 中层，既保持了核心共享，又能正确区分两种调用方。锚点生成的细节（空元素如何插入 `<span>`、帧目标如何处理）详见 **u5-l4（链接锚点与文档内跳转）**。

## 6. 本讲小结

- typst-html 的编译主链路由**三层函数**构成：外层 `html_document` 拆解 `Engine`、中层 `html_document_impl`（`#[comemo::memoize]`）做缓存边界、内层 `html_document_common` 真正干活并被 bundle 路径复用。
- `comemo::memoize` **只包在中层**：外层带 `&mut Engine` 无法做缓存键，内层要把副作用与可变状态隔在缓存之外。
- `html_document_common` 的主线是 **realize → convert_to_nodes → finalize_dom → resolve_inline_styles → 方程 CSS 注入 → `HtmlDocument::new`**，其中写内联样式必须放在 `finalize_dom` 之后（因其会新增带样式节点）。
- realize 用 `RealizationKind::Document { info: &mut info }` **边具象化边收集文档元信息**；根级样式须经 `styles.outside()` 标记才能在文档级合法使用。
- `DocumentInfo` 由 `populate`（读 `DocumentElem` 字段）与 `populate_locale`（读 `TextElem` 的 lang/region）填充，每字段按 `has(...)` 判断避免误覆盖。
- 含数学公式时，主链路用**内省查询**判定 `has_equations`，条件性地向 `<head>` 注入 `EQUATION_CSS_STYLES` 以覆盖 MathML UA 样式；创建 `HtmlDocument` 之后，中层还做 `create_link_anchors` 与 `set_anchors` 两步后处理。

## 7. 下一步学习建议

本讲只走通了主链路的「骨架」，每一步的内部细节都留给后续讲义：

- 想深入了解 **`finalize_dom` 如何决定套 `<html>/<body>`、生成 `<head>` 的 meta 标签、处理脚注容器** → 阅读 **u3-l2（finalize_dom 与文档骨架生成）**。
- 想了解 **`convert_to_nodes` 如何把各类 Content 逐类翻译成 `HtmlNode`** → 阅读 **u3-l3（convert_to_nodes 内容转换器）**。
- 想了解 **块级/行内片段的递归编译与缓存策略** → 阅读 **u3-l4（块级/行内/数学片段的递归编译）**。
- 想了解 **show 规则如何把 Typst 元素映射成 HTML 元素**（realize 背后的映射表）→ 阅读 **u3-l5（内建 show 规则注册机制）**。
- 想了解 **`EQUATION_CSS_STYLES` 的具体规则与 MathML 转换** → 阅读 **u5-l5（数学公式到 MathML 的转换）**。
- 想了解 **comemo 缓存的更多设计取舍**（为何 HtmlDocument 不实现 Hash、root_mut 的副作用）→ 阅读 **u6-l4（缓存与 comemo memoization）**。

建议按 u3-l2 → u3-l3 → u3-l4 → u3-l5 的顺序继续，把主链路每一步的内部机制逐一吃透。
