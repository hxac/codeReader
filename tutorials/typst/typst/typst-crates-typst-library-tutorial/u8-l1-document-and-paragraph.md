# Document 与段落 ParElem

## 1. 本讲目标

本讲进入「文档模型」（`src/model/`）子系统的第一个主题：**文档的元信息载体与最基本的正文块——段落**。

读完本讲后，你应当能够：

- 说清 `DocumentElem` 作为「文档元信息单一真相源」的角色，理解它如何只通过 `set` 规则承载 title/author/keywords/date 等元信息，并区分它和编译产物 `Document` trait、`DocumentInfo` 结构。
- 解释 `ShowSet` 能力如何把「显式构造的 `document` 元素属性」提升为「上下文可见的样式」，从而让 `title` 元素和 `context document.title` 都能读到它。
- 逐字段读懂 `ParElem` 的段落配置：`leading`（行间距）、`spacing`（段间距）、`justify`（两端对齐）、`justification_limits`（对齐伸缩范围）、`linebreaks`（断行算法）、`first_line_indent` / `hanging_indent`（缩进）。
- 用真值表说明 `justify` 与 `linebreaks` 的联动关系——「`auto` + `justify:true` 自动启用优化断行」。
- 理解 `ParbreakElem` 为何是一个零字段、全局单例、`Unlabellable` 的元素，以及多个连续 `parbreak` 为何会「坍缩成一个」。

本讲承接 u6-l2（布局的 `Region`/`Frame`/`Fragment` 词汇）与 u3-l3（`#[elem]` 宏、字段标注、`Packed<T>`），把元素定义与布局行为之间的边界再次讲透。

## 2. 前置知识

在进入正文前，先用通俗语言回顾几个本讲会反复用到的概念。已学过 u3-l3、u4-l1、u6-l2 的读者可以跳读本节。

- **元素（Element）与字段标注（u3-l3）**：`#[elem(...)]` 宏把一个 Rust struct 变成 Typst 元素。字段用不同标注决定其存储方式：`#[required]`（必填，存进 struct）、`#[default(x)]`（有默认值的可设置字段）、`#[fold]`（取值沿样式链「折叠」而非「覆盖」）、`#[ghost]`（不进 struct，只活在样式链里）。本讲会看到 `leading`/`spacing` 用 `#[default]`，`justification_limits`/`first_line_indent` 用 `#[fold]`，`ParLine` 的字段全是 `#[ghost]`。

- **能力（Capability，u3-l2）**：`#[elem(...)]` 括号里列出的是元素具备的能力 trait，如 `Locatable`（可被定位，参与内省）、`Tagged`（被打上位置标签）、`ShowSet`（show 时能反向注入样式）、`Unlabellable`（不能持有 label）。括号里**没有** `Construct` 时，宏会生成默认的构造函数（按声明顺序收集必填与可设置字段）。

- **样式链与折叠（u4-l1）**：`StyleChain` 是零分配的链表视图，按「最内层优先」查询值。`Fold` 要求合并满足结合律（如 `FirstLineIndent` 用 `Option::or` 取首个非空）。本讲的 `JustificationLimits`、`FirstLineIndent` 都是折叠字段。

- **布局输入输出（u6-l2）**：`ParElem` 排版后产出的就是 `Frame`（一帧 = 一页上的一块矩形内容）。本讲只讲「元素如何定义与配置」，把 content 真正排成 `Frame` 的段落布局算法住在 `typst-layout`，运行期经 `Routines` 回调（这是贯穿全书的主轴）。

- **元信息 vs. 可见内容**：文档的「标题」「作者」通常**不**直接渲染在纸面上，而是嵌进 PDF 的元信息字典（document information dictionary / XMP）或 HTML 的 `<title>`/`<meta>`。Typst 把这类数据集中放在 `document` 元素里。

> 一个贯穿本讲的判断法：**本 crate（typst-library）只「定义元素 + 归一化配置数据」，真正把段落排成行、把元信息写进 PDF 的算法都在行为 crate（typst-layout / typst-pdf 等），运行期经 `Routines` 回调。** 每当看到「字段定义在本文件，但行为不在」，就回想起这句话。

## 3. 本讲源码地图

本讲涉及三个文件：

| 文件 | 作用 | 本讲引用的关键定义 |
|------|------|--------------------|
| [src/model/document.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/document.rs) | 文档元信息载体 | `DocumentElem`、`ShowSet` 实现、`DocumentFormat`、`Document` trait、`DocumentInfo`、`populate` |
| [src/model/par.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs) | 段落元素 | `ParElem` 及其全部字段、`Linebreaks`、`JustificationLimits`/`Limits`、`FirstLineIndent`、`ParbreakElem`、`ParLine`、`ParLineMarker` |
| [src/model/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/mod.rs) | model 子系统的注册总装 | `define(global, features)` 中对 `DocumentElem`/`ParElem`/`ParbreakElem` 的注册 |
| [src/foundations/styles.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs) | 原生 show 规则表 | `NativeRuleMap::new()` 注册 `DOCUMENT_UNSUPPORTED_RULE` |

> 行号与永久链接基于当前 HEAD `146a5832`。后续如遇代码改动，请用 `git log -p -- <文件>` 校准。

---

## 4. 核心概念与源码讲解

### 4.1 DocumentElem 与 DocumentInfo：文档元信息的单一真相源

#### 4.1.1 概念说明

一个排版文档除了「正文」之外，还有一类「元信息」：标题、作者、描述、关键词、创建日期。这些信息有两个去处——

1. **嵌进输出文件**：PDF 把它写进 document information dictionary 与 XMP；HTML 写进 `<title>`/`<meta>`；SVG/PNG 不支持。
2. **在文档内部上下文可见**：模板可以用 `context document.title` 读到它；内置的 `title` 元素也会自动取用它。

Typst 用一个元素来集中承载这些信息——`DocumentElem`（用户写作时写作 `document`）。它的关键设计是：

- 在**单文档导出**（Paged/Html 目标）里，`document` **只配合 `set` 规则使用**：`#set document(title: [...])`。`set` 规则只往样式链里写属性，**不创建可见的内容节点**。
- 也可以显式 `#document("out.html", title:[...])[...]` 构造一个文档元素，但这**只在 bundle 导出**（一个 Typst 源产出多个文件）里有意义——它代表 bundle 里的「一个文件」。

> 注意区分三个容易混淆的名字：
> - **`DocumentElem`**：用户可见的 `document` 元素（本节主角）。
> - **`Document` trait**：编译产物「一个排版好的文档」的抽象接口，只有 `fn info(&self) -> &DocumentInfo` 一个方法。它和 `DocumentElem` 是完全不同的东西——一个是「源端配置」，一个是「产物端句柄」。
> - **`DocumentInfo`**：元信息的纯数据结构（title/author/keywords/...），由编译期从样式链里「提取」出来，挂在产物 `Document` 上。

#### 4.1.2 核心流程

`DocumentElem` 的工作可以拆成两条流水线：

**A. 单文档元信息提取（最常见）**

```text
用户写: #set document(title: [...], author: "Alice")
   │
   │  set 规则 → styles 里插入 Property{ elem: DocumentElem, id: title, value: ... }
   ▼
编译驱动 (typst crate): DocumentInfo::default().populate(styles)
   │   逐字段 if styles.has(DocumentElem::X) { 从样式链读 }
   ▼
DocumentInfo { title, author, keywords, date, ... }  挂到产物 Document 上
   │
   ▼
typst-pdf / typst-html: 把 info 写进输出文件
```

**B. 显式 document 元素（bundle 导出）**

```text
用户写: #document("index.html", title:[Home])[ ... ]
   │
   │  默认 Construct: 收集 path(required) + body(required) + 可设置字段
   ▼
DocumentElem 节点进入 content 流
   │
   │  ShowSet: 把显式实参(format/title/author/...) copy_into styles → 上下文可见
   ▼
在 Bundle 目标: 正常渲染导出成子文件
在 Paged/Html 目标: 命中 DOCUMENT_UNSUPPORTED_RULE → bail!
```

这里有一个精妙的**目标门控**：显式构造一个 `document` 在普通导出里是被禁止的，这个禁止不是写在构造函数里，而是以**原生 show 规则**的形式注册。

#### 4.1.3 源码精读

**`DocumentElem` 的字段定义**——[src/model/document.rs:125-186](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/document.rs#L125-L186)：

```rust
#[elem(since = "forever", Locatable, ShowSet)]
pub struct DocumentElem {
    #[required]
    pub path: BundlePath,                 // bundle 里的输出路径（仅 bundle 目标）
    pub format: Smart<DocumentFormat>,    // auto→按扩展名推断
    pub title: Option<Content>,           // 标题（可为富文本，PDF 仅取纯文本）
    pub author: OneOrMultiple<EcoString>,
    pub description: Option<Content>,
    pub keywords: OneOrMultiple<EcoString>,
    pub date: Smart<Option<Datetime>>,    // auto=当前时间；none=不嵌入
    #[required]
    pub body: Content,                    // 文档内容（仅 bundle 目标）
}
```

要点解读：

- `#[elem(... Locatable, ShowSet)]` 列出两个能力，**没有** `Construct`——所以走默认构造。`Locatable` 让它可被定位（bundle 里多文档需要），`ShowSet` 让它在 show 时反向注入样式（见下）。
- `path` 与 `body` 是 `#[required]` 必填：显式构造 `#document("x.html")[...]` 时由默认构造按位置收集。其余都是可设置字段。
- `author`/`keywords` 用 `OneOrMultiple<EcoString>`——这个包装类型（见 [src/foundations/array.rs:1318-1356](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L1318-L1356)）允许「写一个字符串」或「写一个数组」两种输入，让 `author: "Alice"` 与 `author: ("Alice", "Bob")` 都合法。
- `title`/`description` 是 `Option<Content>`（富文本内容），但写进 PDF 时会调用 `content.plain_text()` 折成纯文本。

**`ShowSet` 实现**——[src/model/document.rs:229-251](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/document.rs#L229-L251)：

```rust
impl ShowSet for Packed<DocumentElem> {
    fn show_set(&self, _: StyleChain) -> Styles {
        // 把显式构造参数提升为上下文可见
        let mut styles = Styles::new();
        self.format.copy_into(&mut styles);
        self.title.copy_into(&mut styles);
        self.author.copy_into(&mut styles);
        // ...
        styles
    }
}
```

这段是理解「元信息从哪来」的钥匙。注意它的注释（[document.rs:231-241](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/document.rs#L231-L241)）直言：这种「show 时把属性写回样式」对普通元素而言**并不一致**，但和 `page` 元素一致，且是让 `title` 元素能读到 `document.title` 所必需的。也就是说，无论你是用 `#set document(title: ...)`，还是用 `#document("x", title: ...)[...]` 显式传参，最终元信息都会落到样式链上、对 `context` 可见。

**目标门控：`DOCUMENT_UNSUPPORTED_RULE`**——[src/model/document.rs:219-227](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/document.rs#L219-L227)：

```rust
pub const DOCUMENT_UNSUPPORTED_RULE: ShowFn<DocumentElem> = |elem, _, _| {
    bail!(elem.span(),
        "constructing a document is only supported in the bundle target";
        hint: "try enabling the bundle target";
        hint: "or use a `set document(..)` rule to configure metadata";
    )
};
```

这是一个 `ShowFn<DocumentElem>`——`document` 元素的**原生 show 函数**。它在 [src/foundations/styles.rs:1029-1032](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L1029-L1032) 被注册进 `NativeRuleMap`，**只为 `Paged` 和 `Html` 两个目标注册，不为 `Bundle` 注册**：

```rust
for target in [Target::Paged, Target::Html] {
    rules.register(target, crate::model::ASSET_UNSUPPORTED_RULE);
    rules.register(target, crate::model::DOCUMENT_UNSUPPORTED_RULE);
}
```

这就构成了门控：在普通 PDF/HTML 导出里，一旦 content 流中出现一个 `DocumentElem` 节点并进入 realization，show 阶段命中这条规则，立即 `bail!` 报错；而 bundle 导出不注册它，于是节点正常渲染成子文件。（这正是 u4-l2 讲过的 `NativeRuleMap`——「`(元素, Target) → Rust show 函数`」的内置规则表——的一个真实用例。）**而 `#set document(...)` 不会创建节点，所以不会触发 show，这就是「单文档导出只能用 set」的底层原因。**

**导出格式推断**——[src/model/document.rs:188-217](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/document.rs#L188-L217)：`determine_format` 优先用显式 `format`，否则按 `path` 扩展名推断（`.pdf`/`.svg`/`.png`/`.html`），都失败则报错。`DocumentFormat` 枚举只有两类：`Paged(PagedFormat)`（Pdf/Png/Svg）与 `Html`，其 `target()` 方法把格式映射回 `Target::Paged`/`Target::Html`（[document.rs:254-269](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/document.rs#L254-L269)）。

**产物侧：`Document` trait 与 `DocumentInfo`**——[src/model/document.rs:322-347](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/document.rs#L322-L347)：

```rust
pub trait Document {
    fn info(&self) -> &DocumentInfo;
}

#[derive(Debug, Default, Clone, PartialEq, Hash)]
pub struct DocumentInfo {
    pub title: Option<EcoString>,
    pub author: Vec<EcoString>,
    pub description: Option<EcoString>,
    pub keywords: Vec<EcoString>,
    pub date: Smart<Option<Datetime>>,
    pub locale: Smart<Locale>,   // 注意：多了一个 locale 字段
}
```

注意 `DocumentInfo` 比用户写的字段**多一个 `locale`**——它不是 `document` 元素的属性，而是从 `set text(lang: ..., region: ...)` 里提取的。提取逻辑分两个方法：

- `populate(styles)`（[document.rs:349-375](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/document.rs#L349-L375)）：逐字段用 `if styles.has(DocumentElem::X)` 守卫，**只有当对应 set 规则存在时才覆盖默认值**。`title`/`description` 经 `plain_text()` 折纯文本。
- `populate_locale(styles)`（[document.rs:377-391](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/document.rs#L377-L391)）：若 `locale` 已是 custom 就跳过；否则从 `TextElem::lang`/`TextElem::region` 读取拼出 `Locale`。

> 这里的 `if styles.has(...)` 守卫是关键：它实现了「未设置的属性保留默认值（往往是 `none`）」的语义。配合 u4-l1 的 `StyleChain`，这就是元信息提取的完整链路。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`set document` 只配元信息不产生可见内容」，并理解 `DocumentInfo::populate` 的字段守卫。

**操作步骤**（源码阅读型 + Typst 观察型）：

1. 用 Typst 写一个最小文件 `meta.typ`：
   ```typ
   #set document(title: [My Doc], author: "Alice", keywords: ("Typst", "Meta"))
   #set text(lang: "zh", region: "cn")

   你看不到标题，但它已嵌入 PDF 元信息。
   #context [作者: #document.author.join(", ")]
   ```
2. 编译：`typst compile meta.typ meta.pdf`（如本机未安装 Typst CLI，则跳到步骤 3 的纯阅读部分，标注「待本地验证」）。
3. 打开 [document.rs:349-375](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/document.rs#L349-L375) 的 `populate`，对照你写的 `set` 规则，逐字段走一遍：`styles.has(DocumentElem::title)` 为真 → 读出 `[My Doc]` → `plain_text()` → `"My Doc"`。
4. 把第 1 步的 `author` 那行 `set` 删掉，重新阅读 `populate`，预测 `info.author` 会是什么（答案：保持 `Default::default()` 即空 `Vec`）。

**需要观察的现象**：

- 正文里「你看不到标题……」照常渲染；`document` 元素**没有**在纸面上留下任何痕迹（印证它只配元信息）。
- `#context document.author` 能正确输出 `Alice`——证明元信息对上下文可见（这正是 `ShowSet`/样式链的功劳）。
- 用 `pdfinfo meta.pdf`（或 PDF 阅读器的「属性」面板）应能看到 Title/Author/Keywords 已嵌入。

**预期结果**：PDF 正文正常、元信息已嵌入、`context` 表达式输出 `Alice`。删除 `author` set 规则后，`document.author` 为空。若本机无法运行 Typst，以上行为「待本地验证」，但源码层面的字段守卫逻辑是确定的。

#### 4.1.5 小练习与答案

**练习 1**：为什么在单文档 PDF 导出里，`#document("x.pdf")[Hello]` 会报错，而 `#set document(title:[T])` 不会？

**参考答案**：显式 `#document(...)` 会构造一个 `DocumentElem` 节点进入 content 流；在 Paged 目标，该节点的 show 命中 `DOCUMENT_UNSUPPORTED_RULE`（仅对 Paged/Html 注册），于是 `bail!`。而 `#set document(...)` 只往样式链写 `Property`，不创建任何节点，不会触发 show——元信息通过 `DocumentInfo::populate(styles)` 在编译期被提取，与 show 无关。

**练习 2**：`DocumentInfo` 的 `locale` 字段从哪来？为什么它不放在 `DocumentElem` 上？

**参考答案**：`locale` 由 `populate_locale` 从 `TextElem::lang`/`TextElem::region` 读取（[document.rs:377-391](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/document.rs#L377-L391)）。语言/区域本就属于文本配置（`set text(lang:...)`），把它作为 `text` 的属性而非 `document` 的属性，符合「单一职责」——同一个文档里可以有不同语言的文本片段，而文档级 locale 取首个顶层 `set text` 规则。

**练习 3**：`author: OneOrMultiple<EcoString>` 是怎么做到既接受单个字符串又接受数组的？

**参考答案**：`OneOrMultiple` 的 `FromValue`（[array.rs:1342-1356](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L1342-L1356)）先判断值是否能 cast 成单个 `T`，能则包成单元素 `Vec`；否则若能 cast 成 `Array`，则逐元素 cast 收集成 `Vec`。

---

### 4.2 ParElem：段落元素与正文排版配置

#### 4.2.1 概念说明

**段落（paragraph）**是正文的逻辑单元。在 Typst 里，你通常**不需要**手写段落——连续的行内级内容（text、`h` 间距、`box`、行内公式）会被**自动**包裹进段落；用空行（或显式 `#parbreak()`）分隔段落；任何块级元素（`block`、`place` 等）会**自动打断**当前段落。

真正排成「一行一行的字」的算法住在 `typst-layout`（断行、对齐、行间距计算都在那里）。本 crate 里的 `ParElem` 只负责**定义这个元素 + 承载排版配置字段**。所以 `par` 元素「主要用于 `set` 规则」，但也能用 `#par[...]` 显式把某段内容强制作为段落。

> 「什么内容会变成段落」是个语义问题，源码注释 [par.rs:41-77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L41-L77) 给出了规则：文档根目录的文本会被包成段落；容器（如 `block`）里的文本**只有当容器含块级内容时**才包成段落。这关系到 `first-line-indent` 是否生效、PDF 是否生成 `<P>` 标签、HTML 是否生成 `<p>` 标签——区分「真段落」与「普通文本」对无障碍很重要。

#### 4.2.2 核心流程

段落从定义到排版的简略链路：

```text
源码: #set par(justify: true, leading: 0.8em) + 一段正文
   │
   │  set 规则 → styles 写入 ParElem 的 Property(justify/leading)
   ▼
realization (typst-realize): 把行内内容自动包成 ParElem 节点
   │
   │  ParElem.body = 这段行内 content
   ▼
段落布局 (typst-layout, 经 Routines.layout_frame 回调):
   - 读 linebreaks/justify 决定断行算法
   - 读 leading 决定行间距
   - 读 first_line_indent/hanging_indent 决定缩进
   - 读 justification_limits 限制对齐伸缩
   ▼
Frame（每行一个文本项 + 行间距）
```

本节聚焦**字段定义**与**字段语义**，算法细节（断行、对齐）由 u7-l3 与 typst-layout 讲解。

**`justify` 与 `linebreaks` 的联动**（本讲的核心问题之一）：

`linebreaks` 字段是 `Smart<Linebreaks>`——`auto`（默认）/`"simple"`/`"optimized"`。文档注释 [par.rs:356-359](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L356-L359) 明确：「当 `linebreaks` 为 `auto` 时，**两端对齐的段落**会使用优化断行」。由此可得真值表：

| `justify` | `linebreaks` | 实际断行算法 |
|-----------|--------------|--------------|
| `true` | `auto`（默认） | **optimized**（自动启用，因对齐需要全局权衡） |
| `false` | `auto`（默认） | simple（首适配，最快） |
| 任意 | `"optimized"` | optimized（强制） |
| 任意 | `"simple"` | simple（强制） |

换句话说：**`justify: true` 隐含了 `linebreaks: "optimized"`，除非你显式覆盖。** 这就是为什么「`linebreaks:"optimized"` 与 `justify` 的关系」——它们不是独立开关，`justify` 会拉动 `linebreaks` 的默认行为。理解这一点能避免「我明明没设 linebreaks，为什么断行很慢」的困惑。

**`leading` 与 `spacing` 如何影响行间距**（本讲另一核心问题）：

这两个字段都是 `Length`，默认都用 `Em`（相对字号）表达：

- `leading`（默认 `0.65em`）：**段内**相邻两行之间的间距，定义为「上一行 bottom edge 到下一行 top edge」。注意它不是「基线到基线」——基线间距还需加上字体的 top/bottom edge（见下文公式）。
- `spacing`（默认 `1.2em`）：**段间**间距，即上一段最后一行的 bottom edge 到下一段第一行的 top edge。它「上下都作用」，相邻两段的 spacing **坍缩为较大者**（类似 CSS 的 margin collapsing）。当一个段落紧邻一个 `block` 时，`block` 的 `above`/`below` 优先于段落 spacing（标题默认就是这样缩小下方间距的）。

设一行文字的视觉高度为 \(h\)（由字体 top/bottom edge 决定），段内 leading 为 \(l\)，则一段 \(n\) 行的段落，其纵向高度近似为：

\[
H_{\text{段}} \approx n\cdot h + (n-1)\cdot l
\]

两段之间的额外间距为 \(s_{\text{eff}} = \max(s_A,\, s_B)\)。文档注释 [par.rs:108-112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L108-L112) 还指出：通过同时设置 `leading`、text 的 `top-edge`/`bottom-edge`，可以精确控制「基线到基线」的距离——例如 leading `1em` + top-edge `0.8em` + bottom-edge `-0.2em` 得到恰好 `2em` 的基线间距。

#### 4.2.3 源码精读

**`ParElem` 结构与字段**——[src/model/par.rs:98-435](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L98-L435)：

```rust
#[elem(scope, title = "Paragraph", since = "forever", Locatable, Tagged)]
pub struct ParElem {
    #[default(Em::new(0.65).into())]
    pub leading: Length,                       // 段内行间距

    #[default(Em::new(1.2).into())]
    pub spacing: Length,                       // 段间距（坍缩取大）

    #[default(false)]
    pub justify: bool,                         // 两端对齐

    #[fold]
    pub justification_limits: JustificationLimits,  // 对齐伸缩范围

    pub linebreaks: Smart<Linebreaks>,         // 断行算法（auto/simple/optimized）

    #[fold]
    pub first_line_indent: FirstLineIndent,    // 首行缩进

    pub hanging_indent: Length,                // 悬挂缩进

    #[required]
    pub body: Content,                         // 段落正文
}
```

要点：

- 能力是 `Locatable, Tagged`——可被定位、被打标签，因而能参与 `query`（如查段落位置）和行号标注。括号无 `Construct` → 默认构造（`#par[...]` 收 `body` + settable 字段）。`scope` 表示它有子作用域（挂了 `par.line`，见 4.2 末尾）。
- `leading`/`spacing` 的默认值用 `Em::new(0.65).into()` / `Em::new(1.2).into()`，转成 `Length`——回想 u6-l1，`Length={abs,em}` 是关于字号的一次函数，故这两个间距会随字号缩放。
- `justify: bool` 用 `#[default(false)]`，默认不对齐。
- `justification_limits` 与 `first_line_indent` 标了 `#[fold]`——它们的取值会沿样式链折叠（见下文）。
- `linebreaks` 是 `Smart<Linebreaks>`，`Smart::Auto` 是默认，体现「`auto` 由 `justify` 决定」的设计。

**`Linebreaks` 枚举**——[src/model/par.rs:625-635](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L625-L635)：

```rust
pub enum Linebreaks {
    Simple,     // 首适配：逐词填，放不下就换行，快
    Optimized,  // 全段优化：考虑整段，行填充更均匀
}
```

`Simple` 是贪心的首适配（first-fit），快但可能把长词挤到下一行留下大空白；`Optimized` 是全局优化（与 u7-l3 的 `Costs` 配合），慢但更美观。

**`JustificationLimits` 与 `Limits<T>`——折叠字段如何约束对齐**——[src/model/par.rs:444-450](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L444-L450) 与 [par.rs:510-529](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L510-L529)：

```rust
pub struct JustificationLimits {
    spacing: Option<Limits<Rel>>,     // 词间距伸缩范围（相对值）
    tracking: Option<Limits<Length>>, // 字间距伸缩范围（绝对值）
}

impl Limits<Rel> {
    const SPACING_DEFAULT: Self = Self {
        min: Rel::new(Ratio::new(2.0/3.0), Length::zero()),  // 空格最小缩到 2/3
        max: Rel::new(Ratio::new(1.5), Length::zero()),       // 最大放到 1.5
    };
}
impl Limits<Length> {
    const TRACKING_DEFAULT: Self = Self { min: Length::zero(), max: Length::zero() };
}
```

`JustificationLimits` 把对齐时「词间距能缩/放到多少」「字间距能加多少」表达成两组 `min`/`max`。默认只允许调词间距（`spacing` 的 2/3~1.5 倍），不允许调字间距（`tracking` 默认全 0）——这就是为什么 Typst 默认两端对齐「只改词距不改字距」。想开启「字符级对齐」（CJK 或窄栏常用），就把 `tracking` 的 `min`/`max` 设成 `±0.01em` 量级。

它的 `Fold` 实现（[par.rs:492-499](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L492-L499)）对每个子项用 `fold_or`（即 `Option::or`）合并：内层 set 了就取内层，否则继承外层——所以「只设 `tracking` 不设 `spacing`」时，`spacing` 保留外层/默认值（呼应文档「If you only specify one of `spacing` or `tracking`, the other retains its previously set value」）。

**`FirstLineIndent`——首行缩进的折叠**——[src/model/par.rs:640-705](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L640-L705)：

```rust
pub struct FirstLineIndent {
    amount: Option<Length>,   // 缩进量
    all: Option<bool>,        // 是否对「所有」段落缩进（默认 false=仅连续段）
}

impl Fold for FirstLineIndent {
    fn fold(self, outer: Self) -> Self {
        Self { amount: self.amount.or(outer.amount), all: self.all.or(outer.all) }
    }
}
```

它的 `cast!`（[par.rs:648-664](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L648-L664)）接受两种输入：直接给一个长度（`first-line-indent: 1em` → amount=`1em`, all=None），或给一个字典（`first-line-indent: (amount: 1em, all: true)`）。默认行为是「仅连续段落缩进」——即文档/容器第一段、以及紧跟块级元素后的段落**不**缩进（排版惯例：用首行缩进或段间距之一来区分段落，二选一）。

**`ParLine` 子元素——行号配置（全是 ghost 字段）**——[src/model/par.rs:790-901](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L790-L901)。它通过 [par.rs:437-441](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L437-L441) 的 `#[scope] impl ParElem { #[elem] type ParLine; }` 挂成 `par.line`，所有字段（`numbering`/`number_align`/`number_margin`/`number_clearance`/`numbering_scope`）都是 `#[ghost]`——只活在样式链、不入 struct（呼应 u7-l1 的 ghost 字段）。它的 `Construct`（[par.rs:903-907](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L903-L907)）直接 `bail!`「cannot be constructed manually」——`par.line` 只能通过 `set par.line(numbering: "1")` 配置，不能放进 content。配套的 `ParLineMarker`（[par.rs:928-945](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L928-L945)）是编译器给每一行插入的内部标记元素，供后续检索定位行号位置。

#### 4.2.4 代码实践

**实践目标**：验证 `justify` 与 `linebreaks` 的联动，以及 `leading`/`spacing` 对行距的实际影响。

**操作步骤**（Typst 观察型）：

1. 写 `par.typ`：
   ```typ
   #set page(width: 207pt)
   #set par(linebreaks: "simple")
   Some texts feature many longer
   words. Those are often exceedingly
   challenging to break in a visually
   pleasing way.

   #set par(linebreaks: "optimized")
   Some texts feature many longer
   words. Those are often exceedingly
   challenging to break in a visually
   pleasing way.
   ```
   编译 `typst compile par.typ`，对比两段断行效果（这段取自源码示例 [par.rs:362-374](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L362-L374)）。
2. 新建 `justify.typ` 验证联动：
   ```typ
   #set page(width: 250pt)
   #set par(justify: true)
   // 注意：没有写 linebreaks，预期仍是 optimized
   #lorem(40)
   ```
3. 新建 `spacing.typ` 观察 leading/spacing：
   ```typ
   #set par(leading: 0.65em, spacing: 1.2em)
   第一段，默认行距。#lorem(8)

   第二段，与第一段之间应是 max(1.2em,1.2em)=1.2em。

   #set par(leading: 1.5em)
   第三段，行距明显变大（段内 1.5em）。
   ```
4. 阅读 [par.rs:100-225](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L100-L225)，用 `leading`/`spacing` 的默认值与本步骤对照。

**需要观察的现象**：

- 步骤 1：`optimized` 那段每行填充更均匀，`simple` 那段可能出现某行明显偏空。
- 步骤 2：即使没写 `linebreaks`，因 `justify: true` 自动走 optimized，断行同样均匀。
- 步骤 3：第三段段内行距明显变宽；段间间距是相邻 `spacing` 取大后的结果。

**预期结果**：如上。若本机无 Typst CLI，断行/行距的视觉差异「待本地验证」，但 `justify`→`linebreaks` 的默认联动关系由源码注释 [par.rs:356-359](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L356-L359) 直接给出，是确定的。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `leading` 和 `spacing` 的默认值用 `Em` 而不是 `Abs`？

**参考答案**：用 `Em`（相对字号）能让行距/段距随字号自动缩放——`0.65em` 在 10pt 字号下是 6.5pt，在 20pt 下是 13pt，保持视觉比例。`Length` 内部 `{abs, em}`（u6-l1）会把这些相对量延迟到布局时才解析成绝对值。

**练习 2**：用户写 `#set par(first-line-indent: 1em)`，然后又在内层写 `#set par(first-line-indent: (all: true))`，最终 `amount` 和 `all` 各是多少？

**参考答案**：`FirstLineIndent` 的 `Fold`（[par.rs:698-705](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L698-L705)）对每个字段用 `Option::or`——内层非空取内层，否则取外层。外层 `amount=1em`、`all=None`；内层 `amount=None`、`all=Some(true)`。折叠后 `amount` 取外层 `1em`、`all` 取内层 `true`，最终「缩进 1em 且对所有段落生效」。

**练习 3**：`ParLine` 为什么所有字段都是 `#[ghost]`，且 `Construct` 直接 `bail!`？

**参考答案**：`par.line` 纯粹是「行号配置的载体」，不是会被放进 content 的实体元素。它只通过 `set par.line(...)` 往样式链写配置（ghost 字段不进 struct），实际的行号绘制由布局阶段读取这些样式完成。因此禁止手动构造（`Construct` bail），避免用户把它当普通元素放进文档流。

---

### 4.3 ParbreakElem：段落分隔符

#### 4.3.1 概念说明

`ParbreakElem` 是「段落分隔符」——在两段行内内容之间插入它，就形成两个段落。在标记语法里，**一个空行**就等价于一次 `#parbreak()`。它还常用于代码结构里（如 `for` 循环中）显式分段。

它有两个有意思的设计：

1. **零字段、全局单例**：`ParbreakElem` 是个空 struct，且通过 `singleton!` 宏返回一个全局共享的 `Content` 实例。
2. **`Unlabellable`**：它不能持有 label——你写在它上面的 label 会「滑」到最近的非空白元素上。
3. **连续坍缩**：多个连续的 `parbreak` 坍缩成一个（因为它们其实是同一个共享实例，且 realization 会合并）。

#### 4.3.2 核心流程

```text
源码: 空行  或  #parbreak()
   │
   │  解析层: 产生一个 ParbreakElem 节点（取自单例 ParbreakElem::shared()）
   ▼
realization: 遇到 ParbreakElem → 结束当前段落、开启下一段
   │  （连续多个 parbreak 合并为一个分段）
   ▼
排版: 不产生任何可见输出（纯结构标记）
```

#### 4.3.3 源码精读

**`ParbreakElem` 定义**——[src/model/par.rs:725-735](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L725-L735)：

```rust
#[elem(title = "Paragraph Break", since = "forever", Unlabellable)]
pub struct ParbreakElem {}

impl ParbreakElem {
    /// Get the globally shared paragraph element.
    pub fn shared() -> &'static Content {
        singleton!(Content, ParbreakElem::new().pack())
    }
}

impl Unlabellable for Packed<ParbreakElem> {}
```

要点：

- **零字段空 struct**：没有任何 `#[required]`/`#[default]` 字段，括号里也无 `Construct` → 默认构造无参数，`#parbreak()` 即可。
- **`singleton!` 宏**（[par.rs:730-732](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L730-L732)）：用一个静态局部存储缓存 `ParbreakElem::new().pack()` 的 `Content`，首次调用构造、之后返回同一个 `&'static Content`。这是贯穿全 crate 的单例手段（u12-l2 会专题讲解，如 `Content::empty()`、`AlignPointElem::shared()` 也用它）。好处是：所有空行解析出的 parbreak 节点**指针相等**，比较、哈希近乎免费，且便于 realization 识别和合并连续分段。
- **`Unlabellable`**（[par.rs:735](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L735)，呼应 u2-l2 的 `Unlabellable` 标记 trait）：声明此元素的 `Packed` 实现该 trait，使得 `<label>` 不会挂在 parbreak 上，而是滑向最近的非空白元素——否则空行上的标签会「丢失」在不可见的位置。

文档注释 [par.rs:707-724](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L707-L724) 也明确：「多个连续段落分隔符坍缩成一个」，以及「在标记里插入空行即等价于调用此函数」。

#### 4.3.4 代码实践

**实践目标**：理解 `singleton!` 如何让 parbreak 成为全局共享、且 `Unlabellable` 如何影响 label 归属。

**操作步骤**（源码阅读型）：

1. 在本 crate 内搜索 `ParbreakElem::shared` 的调用点，看解析层/realization 在何处取用这个单例（例如标记解析把空行转成 `ParbreakElem::shared()` 的引用）。
2. 阅读 [par.rs:728-733](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L728-L733)，对照 `singleton!` 宏（在 `typst-utils` 中）的语义：它用 `OnceLock`/线程局部静态存储保证「同类型、同初值只构造一次」。
3. 思考：若 `ParbreakElem` **不**实现 `Unlabellable`，下面这段里 `<a>` 会附在谁身上？
   ```typ
   第一段
            <a>
   第二段
   ```
   预期：`<a>` 写在空行（parbreak）上，因 `Unlabellable` 它会滑到「第二段」所属的元素上，而非消失。

**需要观察的现象 / 预期结果**：连续空行只产生一次分段效果（坍缩）；空行上的 label 不会丢失。源码阅读部分的结果是确定的；Typst 侧的 label 归属「待本地验证」（可用 `#context query(<a>)` 确认其位置）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ParbreakElem` 适合用 `singleton!` 共享，而 `ParElem` 不适合？

**参考答案**：`ParbreakElem` 是**无状态**的空元素——所有 parbreak 在语义上完全相同（都是「此处分段」），共享一个实例不会丢失信息，还能省去重复分配、让指针比较成立。而 `ParElem` 携带 `body: Content` 及各排版字段，每段内容不同，必须各自独立存储，不能共享。

**练习 2**：`Unlabellable` 对 parbreak 解决了什么问题？

**参考答案**：用户常把 `<label>` 写在空行上（如段落之间的注释式标签）。parbreak 是不可见的结构标记，若 label 附在它身上，`query` 将定位到一个「看不见的东西」。`Unlabellable` 让 label 滑向最近的非空白元素，保证 label 总挂在有意义的内容上。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个小任务：**用源码知识解释一份带元信息的两段式文档是如何被装配与排版的**。

**任务**：阅读下面这份 `.typ` 文件，结合本讲源码，回答问题并补全一处配置。

```typ
#set document(title: [Demo], author: ("Alice", "Bob"))
#set page(width: 300pt)
#set par(justify: true, leading: 0.8em, first-line-indent: 1em)

这是第一段。它会被自动包裹成 ParElem，
两端对齐，行距 0.8em。注意它是文档第一段。

这是第二段。它会首行缩进 1em（因为它是连续段落）。
#context [本文作者: #document.author.join(", ")]
```

**请你完成**：

1. **元信息链路**：追踪 `title`/`author` 如何从 `set` 规则到达 PDF 元信息——指出涉及的三个关键点（set 写入 styles、`DocumentInfo::populate` 提取、产物 `Document::info`）。引用 [document.rs:349-375](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/document.rs#L349-L375)。
2. **段落配置**：第二段会首行缩进、第一段不会——引用 `FirstLineIndent` 的默认 `all=false`（[par.rs:692-696](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L692-L696)）解释原因。
3. **联动判断**：这份文档没写 `linebreaks`，实际用哪种断行算法？为什么？（提示：[par.rs:356-359](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L356-L359)）
4. **动手改**：若想让**所有**段落（含第一段）都首行缩进 1em，应把 `first-line-indent` 改成什么？（写出新的 `set` 规则）。
5. **边界判断**：把 `#set document(...)` 换成 `#document("demo.pdf", title:[Demo])[...]` 会发生什么？引用 `DOCUMENT_UNSUPPORTED_RULE` 的注册位置 [styles.rs:1029-1032](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L1029-L1032) 说明。

**参考答案要点**：

1. `set document` → styles 的 `Property(DocumentElem::title/author)`；编译期 `DocumentInfo::default().populate(styles)`（`if styles.has(...)` 守卫）读出；挂到产物 `Document.info()`；最后由 typst-pdf 写入 PDF。
2. `FirstLineIndent::default()` 的 `all=Some(false)`，意为「仅连续段落缩进」——第一段（文档/容器首段）不缩进，第二段是「紧跟前一段的连续段落」故缩进。
3. `optimized`。因为 `justify: true` 且 `linebreaks` 为默认 `auto`，按 [par.rs:356-359](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/par.rs#L356-L359)，`auto` 在两端对齐时自动启用优化断行。
4. `#set par(first-line-indent: (amount: 1em, all: true))`。
5. 会报错「constructing a document is only supported in the bundle target」——因为在 Paged 目标，`DOCUMENT_UNSUPPORTED_RULE` 已注册为 `document` 的 show 函数，显式构造的节点进入 realization 即 bail；bundle 目标不注册此规则，故能正常导出。

---

## 6. 本讲小结

- `DocumentElem` 是文档元信息的**单一真相源**：在单文档导出里只用 `set document(...)` 配置 title/author/keywords/date/format 等，不产生可见节点；元信息由 `DocumentInfo::populate(styles)` 在编译期从样式链提取，挂到产物 `Document` 上。
- 显式 `#document(...)` 构造只在 **bundle 导出**合法——在 Paged/Html 目标，`DOCUMENT_UNSUPPORTED_RULE`（注册于 `NativeRuleMap::new()`）作为 show 函数会 `bail!`。这是「`set` 不触发 show、构造才触发 show」的底层原因，也是 u4-l2 `NativeRuleMap` 的真实用例。
- `ShowSet` 把显式构造实参提升为上下文可见样式，使 `context document.title` 与内置 `title` 元素都能读到元信息。
- `ParElem` 是段落元素，主要靠 `set par(...)` 配置：`leading`（段内行距，默认 0.65em）、`spacing`（段间距，默认 1.2em，坍缩取大）、`justify`、`justification_limits`（`#[fold]`，默认只调词距 2/3~1.5、不调字距）、`linebreaks`、`first_line_indent`/`hanging_indent`。把内容排成 `Frame` 的算法在 typst-layout，经 `Routines` 回调。
- **`justify: true` 隐含 `linebreaks: "optimized"`**——这是 `justify` 与 `linebreaks` 的核心联动：`auto` 时由 `justify` 决定走 Simple 还是 Optimized。
- `ParbreakElem` 是零字段、`singleton!` 全局共享、`Unlabellable` 的结构标记；空行即等价于它，连续多个会坍缩成一个。
- 贯穿主线重申：本 crate 只「定义元素 + 归一化配置」，段落布局、断行、元信息写入等行为都在行为 crate，运行期经 `Routines` 回调。

## 7. 下一步学习建议

- **u8-l2（标题、列表、枚举与术语）**：继续文档模型，看 `HeadingElem`/`ListElem`/`EnumElem`/`TermsElem` 如何复用本讲的段落与 block 语义，并接入编号能力。
- **u8-l3（编号、引用、图表与目录）**：`numbering`/`RefElem`/`FigureElem`/`OutlineElem`——它们会用到本讲的 `ParElem`（图表 caption 是段落）与即将在 u9 深入的内省能力。
- **复习 u4-l2（NativeRuleMap）**：本讲的 `DOCUMENT_UNSUPPORTED_RULE` 是它的直接用例，回头读能加深对「`(元素, Target) → show 函数`」表的理解。
- **延伸阅读 typst-layout 的段落布局**：想看 `leading`/`justify`/`linebreaks` 真正如何影响 `Frame`，可去 `typst-layout` 的段落布局模块追踪 `Routines.layout_frame` 的实现（属后续/外部 crate，本讲止于定义层）。
