# TextElem 与字体变体

## 1. 本讲目标

本讲聚焦 Typst 文本系统最核心的一个元素：`TextElem`。读完本讲，你应当能够：

1. 解释 `TextElem` 为什么是一个「几乎全是 ghost 字段」的元素，以及它的 struct 实际只存了什么。
2. 说明 `#text(...)` 函数为何不直接创建文本元素，而是返回「带样式的 body」。
3. 看懂字体回退列表 `FontList`、单族描述 `FontFamily` 与覆盖集 `Covers` 的数据结构与作用。
4. 解释 `variant()` 如何把 `style / weight / stretch` 三个用户字段，连同内部的 `delta / emph`，综合成一个用于匹配字体的 `FontVariant`。
5. 理解 `TextSize`、`WeightDelta`、`ItalicToggle` 这几个 `#[fold]` 字段是如何折叠的。

本讲直接承接 u4-l1（`StyleChain` / `Fold` / `Resolve`），是进入 u7（文本系统）其余讲义（字体管理、OpenType 特性等）的入口。

## 2. 前置知识

在开始前，请确认你已经理解以下概念（它们在前序讲义中讲过）：

- **`Content` 与 `RawContent`**（u3-l1）：所有标记与函数调用的产物都是 `Content`。
- **`#[elem]` 宏与字段标注**（u3-l3）：尤其是 `#[required]`、`#[ghost]`、`#[fold]`、`#[parse]`、`#[external]`、`#[internal]`、`#[default]` 的语义。一句话回顾：
  - `#[required]`：必填字段，**进入 struct** 被真正存储。
  - `#[ghost]`：**不进 struct**，只活在 `StyleChain` 里（按需自定义 `Construct`）。
  - `#[fold]`：取值时多层样式**折叠**而非覆盖。
  - `#[external]`：只出现在文档里，**不进 struct 也不参与样式**。
- **`StyleChain` / `Fold` / `Resolve`**（u4-l1）：样式沿栈查询，`Fold` 同类型合并、`Resolve` 吃整条链把相对值解析成绝对值。
- **`cast!` 宏**（u2-l3）：把 `Value` 与具体 Rust 类型互转。

**两个你需要先建立的直觉：**

- 在 Typst 里，「**样式**」和「**内容**」是分开的。一段文字的颜色、字号、字重属于样式；文字本身属于内容。`TextElem` 把这条边界体现得极其彻底。
- Typst 选字体是一个两步过程：先用「字体家族列表（family list）」圈定候选族，再用一个「字体变体（`FontVariant`）」在每个族内挑最接近的那一款。本讲同时讲清这两步的输入是怎么来的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/text/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs) | 文本模块总入口。定义 `TextElem`、`FontList`/`FontFamily`/`Covers`、`variant()`、`families()`、`features()`、`TextSize`/`WeightDelta`/`ItalicToggle` 等折叠类型。 |
| [src/text/font/variant.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variant.rs) | `FontVariant` 及其三个分量 `FontStyle` / `FontWeight` / `FontStretch` 的定义与「距离」度量。 |
| [src/text/font/book.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs) | `FontBook`：把「族名 → 字体索引」组织起来，并提供 `select` / `select_family` / `find_best_variant`。本讲只在「字体如何被匹配」一节引用它作为下游。 |
| [src/model/strong.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/strong.rs) | `StrongElem`，其 `delta` 字段是 `variant()` 里 `WeightDelta` 的最终来源（经 show 规则桥接）。 |

> 本讲的所有源码链接都指向当前 HEAD `146a58329a30f6cd38978c22c6bf0e430d8962a1`，行号已逐一核对。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1** `TextElem`：一个「几乎全是 ghost」的元素（字段系统 + 特殊构造）。
- **4.2** 字体回退列表：`FontList` / `FontFamily` / `Covers`。
- **4.3** `FontVariant` 与 `variant()`：`style/weight/stretch` 如何被综合，以及 `emph/delta` 如何反向影响字体选择。

### 4.1 TextElem：一个「几乎全是 ghost」的元素

#### 4.1.1 概念说明

`TextElem` 看起来像 Typst 里参数最多的元素之一——`font`、`size`、`fill`、`style`、`weight`、`stretch`、`lang`、`dir`、`features`……几十个字段。但它有一个反直觉的设计：

> **`TextElem` 的 struct 实际上只存了「文本字符串本身」。所有样式字段都是 ghost 字段，只活在 `StyleChain` 里。**

为什么？因为同一个文本元素节点上「挂」的颜色、字号、字重其实并不是这个节点的属性，而是**外层 `set text(...)` / `#text(...)` 建立的样式环境**施加在它身上的。样式属于环境、不属于单个字符。把样式做成 ghost 字段，可以让一条样式链一次性套在成千上万个文本节点上，而不必在每个节点里复制一份样式数据。

这个事实在源码里被一个单元测试钉死：

```rust
#[test]
fn test_text_elem_size() {
    assert_eq!(std::mem::size_of::<TextElem>(), std::mem::size_of::<EcoString>());
}
```

——见 [src/text/mod.rs:1594-1597](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1594-L1597)：`TextElem` 的大小等于一个 `EcoString`。也就是说 struct 里就一个字段。

#### 4.1.2 核心流程

`TextElem` 的字段按「是否进 struct」分成三组：

```
TextElem 的字段
├─ 【真正存进 struct】
│   └─ text: EcoString          ← #[required]，唯一的真实数据（一个文本 run 的字符串）
│
├─ 【ghost：不进 struct，只进 StyleChain（本讲的主角）】
│   ├─ 用户可见：font / size / fill / style / weight / stretch
│   │            / lang / region / dir / features / ... 几十个
│   └─ 内部：    span_offset / delta / emph / deco / case / smallcaps / shift_settings
│
└─ 【external：只在文档里出现，既不进 struct 也不进 StyleChain】
    └─ body: Content            ← 提示文档系统「text 函数接收一段 body」
```

`text` 函数与 `TextElem` 元素因此是**解耦**的：

- `text` 函数（`#[func]` 风格的 `Construct`）不创建 `TextElem` 节点，只把样式套在 body 上。
- 真正的 `TextElem` 节点（带 `text` 字符串）是在文本处理阶段，把正文拆成一个个文本 run 时，用 `TextElem::packed("...")` 创建出来的。

#### 4.1.3 源码精读

**(a) struct 定义：满眼 `#[ghost]`。** 看 `font` 字段（族列表）：

```rust
#[parse({
    let font_list: Option<Spanned<FontList>> = args.named("font")?;
    if let Some(list) = &font_list {
        check_font_list(engine, list);   // 对每个族名做一次存在性校验，缺失则发 warning
    }
    font_list.map(|font_list| font_list.v)
})]
#[default(FontList(vec![FontFamily::new("Libertinus Serif")]))]
#[ghost]
pub font: FontList,
```

见 [src/text/mod.rs:173-182](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L173-L182)。注意三件事：

1. `#[ghost]` 表示该字段不进 struct，只参与样式链。
2. `#[default(...)]` 给出默认值（默认族是 `Libertinus Serif`）。
3. `#[parse(...)]` 覆盖了参数解析：除了把 `font` 参数解析成 `FontList`，还顺手调用 `check_font_list` 对每个族名在 `FontBook` 里查一遍，查不到就 `engine.sink.warn(...)`（见 [src/text/mod.rs:1577-1588](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1577-L1588)）。这是「字体名写错」时 Typst 给你警告的源头。

类似地，`size` 字段带 `#[fold]`：

```rust
#[parse(args.named_or_find("size")?)]
#[fold]
#[default(TextSize(Abs::pt(11.0).into()))]
#[ghost]
pub size: TextSize,
```

见 [src/text/mod.rs:290-294](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L290-L294)。`#[fold]` 意味着多层 `set text(size: ...)` 不是覆盖，而是折叠（详见 4.1 的折叠小节）。

**(b) 唯一进 struct 的字段：`text`。** 在 struct 末尾：

```rust
/// Content in which all text is styled according to the other arguments.
#[external]
#[required]
pub body: Content,

/// The text.
#[required]
pub text: EcoString,
```

见 [src/text/mod.rs:852-859](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L852-L859)。`body` 标了 `#[external]`（只进文档），`text` 才是 `#[required]` 的真实存储。这也解释了为什么 `size_of::<TextElem>() == size_of::<EcoString>()`。

**(c) 内部 ghost 字段：`delta` 与 `emph`。** 这两个是本讲后半「反向影响字体选择」的关键：

```rust
/// A delta to apply on the font weight.
#[internal]
#[fold]
#[ghost]
pub delta: WeightDelta,

/// Whether the font style should be inverted.
#[internal]
#[fold]
#[default(ItalicToggle(false))]
#[ghost]
pub emph: ItalicToggle,
```

见 [src/text/mod.rs:866-878](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L866-L878)。它们都标了 `#[internal]`（不暴露给用户）、`#[fold]`（多层叠加）、`#[ghost]`（不进 struct）。普通用户从不会直接写 `#text(delta: 300)`；这两个字段是给 `#strong` / `#emph` 这类元素「悄悄」写入样式的。

**(d) 特殊的 `Construct`：不建元素，只套样式。**

```rust
impl Construct for TextElem {
    fn construct(engine: &mut Engine, args: &mut Args) -> SourceResult<Content> {
        // The text constructor is special: It doesn't create a text element.
        // Instead, it leaves the passed argument structurally unchanged, but
        // styles all text in it.
        let styles = Self::set(engine, args)?;
        let body = args.expect::<Content>("body")?;
        Ok(body.styled_with_map(styles))
    }
}
```

见 [src/text/mod.rs:923-932](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L923-L932)。这段注释是本讲的「文眼」。流程是：

1. `Self::set(engine, args)` 把用户传入的 `font / size / fill / ...` 等参数转换成一张 `Styles` 映射（就是 `set text(...)` 规则会产生的那张表，见 u4-l1）。
2. `args.expect::<Content>("body")` 取出 body。
3. `body.styled_with_map(styles)` 把这张样式表套在 body 上，**原样返回 body 的结构**。

所以 `#text(font: "Arial")[Hello]` 的返回值，结构上就是 `Hello` 这段 content，只不过外面裹了一层「font = Arial」的样式。它**不会**产生一个新的 `TextElem` 节点。

**(e) 那么 `TextElem` 节点从哪儿来？** 来自 `TextElem::packed`：

```rust
impl TextElem {
    /// Creates a new text element and directly packs it into type-erased
    /// content.
    pub fn packed(text: impl Into<EcoString>) -> Content {
        Self::new(text.into()).pack()
    }
}
```

见 [src/text/mod.rs:903-909](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L903-L909)。在文本处理阶段（拆分正文为 run、shape 每段文本时，发生在行为 crate `typst-layout`/`typst-realize`），每个文本片段都会用这种方式变成一个真正带 `text` 字符串的 `TextElem`，再继承外层 `#text(...)` 留下的样式链。

#### 4.1.4 代码实践

**实践目标：** 亲手验证「`#text(...)` 不创建文本元素，而是给 body 套样式」。

**操作步骤（源码阅读型 + 本地验证）：**

1. 打开 [src/text/mod.rs:923-932](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L923-L932)，确认 `construct` 的返回值是 `body.styled_with_map(styles)`。
2. 在 Typst 里写两个等价写法，观察它们应当产出**相同**的 PDF：
   - 写法 A：`#set text(font: "PT Sans"); This is sans.`
   - 写法 B：`#text(font: "PT Sans")[This is sans.]`
3. （可选）再写一个能看出「text 只套样式」的对比：把 `#text(red)[Hello *world*]` 与 `#set text(fill: red); Hello *world*` 比较，注意 `*world*` 的加粗依然生效——因为 `text` 没有替换 body 的结构，`StrongElem` 仍然在 body 里。

**需要观察的现象：**

- 写法 A 和 B 渲染结果一致（字号、字体相同）。
- `text` 包裹不会「吞掉」内部的 `*strong*` / `_emph_` 标记。

**预期结果：** 两种写法产出像素级相同的输出。这印证了源码注释：text 函数在结构上等价于一条作用域受限的 `set` 规则。

> 若本地没有装 Typst CLI，标注「待本地验证」；源码阅读部分本身已足以得出结论。

#### 4.1.5 小练习与答案

**练习 1.** `TextElem` 的 struct 只有一个字段。它是哪一个？为什么 `body` 字段不算？

> **答案：** 是 `text: EcoString`（[src/text/mod.rs:857-859](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L857-L859)）。`body` 标了 `#[external]`，只用于生成文档，不进 struct（u3-l3）。

**练习 2.** 为什么 `font`、`size`、`fill` 这些字段都要标 `#[ghost]`？

> **答案：** 因为它们是「环境施加的样式」而非「单个文本节点的固有属性」。做成 ghost 后它们只活在 `StyleChain` 中，一条样式链可复用于海量文本节点，避免每个节点都复制一份样式（u4-l1）。

**练习 3.** `check_font_list` 在什么时刻被调用？它发的是错误还是警告？

> **答案：** 在 `font` 字段的 `#[parse]` 里被调用（[src/text/mod.rs:173-179](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L173-L179)）；对每个族名用 `FontBook::select_family` 查一遍，查不到就 `engine.sink.warn(...)` 发**警告**（不是错误），见 [src/text/mod.rs:1577-1588](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1577-L1588)。

---

### 4.2 字体回退列表：FontList / FontFamily / Covers

#### 4.2.1 概念说明

Typst 选字体的第一步是「圈定候选族」。用户通过 `font` 字段提供一个**优先级列表**：Typst 会按顺序尝试每个族，找到第一个能覆盖当前字符（glyph）的字体就用它。这就是为什么你可以写：

```typst
#set text(font: ("Inria Serif", "Noto Sans Arabic"))
```

——拉丁字符用 Inria Serif，遇到阿拉伯字符（Inria Serif 没有）就回退到 Noto Sans Arabic。

这套机制由三个类型构成：

- **`FontFamily`**：单个族的描述 = 「族名 + 可选的覆盖集 `Covers`」。
- **`Covers`**：限定这个族**只负责**哪些码点（一个正则，或预定义集 `latin-in-cjk`）。
- **`FontList`**：`Vec<FontFamily>` 的新类型，必须非空。

`Covers` 是个很有用的进阶特性：它让你「按字符范围分配字体」。例如让数字单独用某字体，或在中英混排时区分拉丁字符与 CJK 字符。

#### 4.2.2 核心流程

```
用户输入 font 字段
   │  （字符串 / 字典 / 数组，由 cast! 归一化）
   ▼
FontList(Vec<FontFamily>)
   │   每个 FontFamily = { name: 小写族名, covers: Option<Covers> }
   ▼
families(styles)  ← 把用户列表 +（可选）全局 emoji 兜底族 串成一个迭代器
   │
   ▼
对当前字符 c：按顺序找第一个 covers 匹配 c 且拥有该 glyph 的族
```

关于「全局兜底族」：当 `fallback: true`（默认）时，会在用户列表后面追加一组 emoji 字体（`twitter color emoji`、`noto color emoji` 等），见下文 `families()` 源码。

#### 4.2.3 源码精读

**(a) `FontFamily`：族名 + 覆盖集。**

```rust
pub struct FontFamily {
    name: EcoString,           // 小写族名
    covers: Option<Covers>,    // 可选：本族负责哪些码点
}

impl FontFamily {
    pub fn new(string: &str) -> Self { Self::with_coverage(string, None) }

    pub fn with_coverage(string: &str, covers: Option<Covers>) -> Self {
        Self { name: string.to_lowercase().into(), covers }
    }
    // ...
}
```

见 [src/text/mod.rs:940-969](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L940-L969)。注意族名**统一转小写**存储——`FontBook` 的索引也用小写族名建（见 [src/text/font/book.rs:43-44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L43-L44)），所以查找是大小写不敏感的。

`cast!` 块同时支持三种输入：纯字符串、`{name: ..., covers: ...}` 字典、以及数组里的混用，见 [src/text/mod.rs:971-989](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L971-L989)。

**(b) `Covers`：两种覆盖来源。**

```rust
pub enum Covers {
    LatinInCjk,         // 预定义集：除「拉丁与 CJK 都有的标点」外的所有码点
    Regex(Regex),       // 自定义正则
}

impl Covers {
    pub fn as_regex(&self) -> &Regex {
        match self {
            Self::LatinInCjk => singleton!(Regex, Regex::new("[^\u{00B7}\u{2013}...]").unwrap()),
            Self::Regex(regex) => regex,
        }
    }
}
```

见 [src/text/mod.rs:991-1015](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L991-L1015)。`LatinInCjk` 用 `singleton!`（u12 会详讲）把那条「排除若干标点」的正则缓存成全局单例。自定义 `Regex` 分支在 `cast!` 里被严格限制：只允许「单个点、字母或字符类」形式的正则（见 [src/text/mod.rs:1017-1044](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1017-L1044)），因为这条正则是**逐字符**应用的。

**(c) `FontList`：非空族列表。**

```rust
pub struct FontList(pub Vec<FontFamily>);

impl FontList {
    pub fn new(fonts: Vec<FontFamily>) -> StrResult<Self> {
        if fonts.is_empty() { bail!("font fallback list must not be empty") }
        else { Ok(Self(fonts)) }
    }
}
```

见 [src/text/mod.rs:1046-1080](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1046-L1080)。它的 `cast!` 优雅地处理了「单个族 vs 数组」：传单个 `FontFamily` 就包成单元素列表，传 `Array` 就逐元素 `cast`。

**(d) `families()`：把用户列表和兜底族串起来。**

```rust
pub fn families(styles: StyleChain<'_>) -> impl Iterator<Item = &'_ FontFamily> + Clone {
    let fallbacks = singleton!(Vec<FontFamily>, {
        ["libertinus serif", "twitter color emoji", "noto color emoji",
         "apple color emoji", "segoe ui emoji"]
        .into_iter().map(FontFamily::new).collect()
    });

    let tail = if styles.get(TextElem::fallback) { fallbacks.as_slice() } else { &[] };
    styles.get_ref(TextElem::font).into_iter().chain(tail.iter())
}
```

见 [src/text/mod.rs:1082-1099](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1082-L1099)。注意 `fallback` 字段（默认 `true`）控制是否追加这组全局 emoji 兜底族；关掉它后，若用户列表里没有能覆盖某字符的字体，就会显示「豆腐块（tofu）」（见 `fallback` 字段文档 [src/text/mod.rs:184-203](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L184-L203)）。

> 字体「**真正**被选定」发生在 `FontBook` 的下游：对每个候选族调用 `FontBook::select(family, variant)`，再在族内用 `find_best_variant` 按 `variant` 距离挑最优款，详见 [src/text/font/book.rs:75-78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L75-L78) 与 [src/text/font/book.rs:139-163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L139-L163)。`variant` 从哪来，正是 4.3 的主题。

#### 4.2.4 代码实践

**实践目标：** 用 `covers` 实现「数字单独用一个字体」。

**操作步骤（本地 Typst 验证）：**

```typst
#set text(font: (
  (name: "PT Sans", covers: regex("[0-9]")),   // 数字用 PT Sans
  "Libertinus Serif",                            // 其余用 Libertinus Serif
))

The number 123 and some words.
```

**需要观察的现象：** `123` 与正文 `The number ... and some words.` 呈现不同字体（数字是 sans，其余是 serif）。

**预期结果：** 数字字符被 PT Sans 覆盖；其余字符由列表里第二个族 `Libertinus Serif` 承接。

> 若本地未装上述字体，可换成你系统里一定有的两个族（如 `"DejaVu Sans Mono"` 与 `"DejaVu Serif"`）做对照。源码侧的依据是 [src/text/mod.rs:1017-1044](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1017-L1044) 的 `Covers::Regex` 分支。

#### 4.2.5 小练习与答案

**练习 1.** 为什么 `FontFamily` 存的是「小写族名」？

> **答案：** 因为 `FontBook` 用小写族名建索引（[src/text/font/book.rs:43-44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L43-L44)）。两端都小写，查找就是大小写不敏感的。

**练习 2.** `FontList::new` 为什么拒绝空列表？

> **答案：** 见 [src/text/mod.rs:1053-1059](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1053-L1059)。族列表是选字体的起点，空列表意味着没有任何候选，文本无法被渲染，故直接 `bail!`。

**练习 3.** 关掉 `fallback` 后，遇到列表里没有的字符会怎样？

> **答案：** 不追加全局兜底族（[src/text/mod.rs:1097](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1097)），找不到 glyph 就显示「豆腐块」小方框（见 `fallback` 字段文档）。

---

### 4.3 FontVariant 与 variant()：style/weight/stretch 的综合

#### 4.3.1 概念说明

选字体的第二步：在候选族内挑最合适的一「款」。一个字体族往往有多款——regular、bold、italic、condensed…… Typst 用一个三元组来描述「想要哪一款」：

```rust
pub struct FontVariant {
    pub style: FontStyle,     // Normal / Italic / Oblique
    pub weight: FontWeight,   // 100 ~ 900
    pub stretch: FontStretch, // 0.5 ~ 2.0（用千分比整数存）
}
```

见 [src/text/font/variant.rs:10-27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variant.rs#L10-L27)。`FontBook::find_best_variant` 会计算每个候选字体与目标 `variant` 的「距离」，距离越小越优先（[src/text/font/book.rs:139-163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L139-L163)）。

本讲的精彩之处在于：传给字体匹配的 `variant` **并不等于**用户直接写的 `style/weight/stretch`。它还要叠加两个**内部修饰量**：

- `delta`（字重增量）：`#strong` 通过它「加重」字重。
- `emph`（斜体开关）：`#emph` 通过它「翻转」斜体。

所以 `variant()` 是一个**综合函数**：把用户可见的三字段当作基线，再用 `delta` 微调字重、用 `emph` 翻转字形，最后才得到用于选字体的 `FontVariant`。这就是练习任务里说的「`emph/delta` 反向影响字体选择」。

#### 4.3.2 核心流程

`variant()` 的逻辑可以写成：

```
1. 从 StyleChain 读出基线：style, weight, stretch
      → 构造初始 variant
2. 读出 delta（WeightDelta，已折叠求和）
      → variant.weight = variant.weight.thicken(delta)
3. 若 emph（ItalicToggle，已折叠 XOR）为真：
      → Normal ⇄ Italic；Oblique → Normal
4. 返回最终的 variant（交给 FontBook 选字体）
```

这里的关键直觉是 **`delta / emph` 是「可折叠的修饰」，而 `weight / style` 是「被修饰的基线」**。两者都通过 `#[fold]` 累积：

| 字段 | 类型 | Fold 语义 | 谁来写入 |
| --- | --- | --- | --- |
| `weight` | `FontWeight` | 覆盖式（取最内层） | 用户 `#set text(weight: ...)` |
| `delta` | `WeightDelta(i64)` | **求和** `outer + self` | `#strong` 的 show 规则 |
| `style` | `FontStyle` | 覆盖式 | 用户 `#set text(style: ...)` |
| `emph` | `ItalicToggle(bool)` | **异或** `self ^ outer` | `#emph` 的 show 规则 |

`delta` 用求和：嵌套的 `#strong[#strong[x]]` 会让字重增量叠加。`emph` 用异或：嵌套的 `#emph[#emph[x]]` 会抵消（外层斜体里再斜一次回到正常）。这两个折叠规则精确对应了人们的排版直觉。

#### 4.3.3 源码精读

**(a) `variant()`：综合函数本体。**

```rust
pub fn variant(styles: StyleChain) -> FontVariant {
    let mut variant = FontVariant::new(
        styles.get(TextElem::style),
        styles.get(TextElem::weight),
        styles.get(TextElem::stretch),
    );

    let WeightDelta(delta) = styles.get(TextElem::delta);
    variant.weight = variant
        .weight
        .thicken(delta.clamp(i16::MIN as i64, i16::MAX as i64) as i16);

    if styles.get(TextElem::emph).0 {
        variant.style = match variant.style {
            FontStyle::Normal => FontStyle::Italic,
            FontStyle::Italic => FontStyle::Normal,
            FontStyle::Oblique => FontStyle::Normal,
        }
    }

    variant
}
```

见 [src/text/mod.rs:1101-1123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1101-L1123)。三步与 4.3.2 一一对应。

**(b) `delta` 如何变重：`FontWeight::thicken`。**

```rust
/// Add (or remove) weight, saturating at the boundaries of 100 and 900.
pub fn thicken(self, delta: i16) -> Self {
    Self((self.0 as i16).saturating_add(delta).clamp(100, 900) as u16)
}
```

见 [src/text/font/variant.rs:132-135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variant.rs#L132-L135)。注意 `clamp(100, 900)`：字重被饱和在标准区间内。`StrongElem` 的 `delta` 默认是 `300`（[src/model/strong.rs:29-30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/strong.rs#L29-L30)），所以 regular(400) + 300 = 700 = Bold，正好是「加粗」的效果。

**(c) `WeightDelta` 与 `ItalicToggle` 的 Fold。**

```rust
pub struct ItalicToggle(pub bool);
impl Fold for ItalicToggle {
    fn fold(self, outer: Self) -> Self { Self(self.0 ^ outer.0) }
}

pub struct WeightDelta(pub i64);
impl Fold for WeightDelta {
    fn fold(self, outer: Self) -> Self { Self(outer.0 + self.0) }
}
```

见 [src/text/mod.rs:1483-1500](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1483-L1500)。注意 `WeightDelta::fold` 的参数顺序是 `outer.0 + self.0`（外层在前），但加法可交换，语义就是「所有层 delta 求和」；`ItalicToggle` 用异或实现「奇数次开关为开」。

**(d) `StrongElem` / `EmphElem` 如何把字段塞进样式链。**

`StrongElem` 自身只有一个用户可见的 `delta` 字段（[src/model/strong.rs:22-35](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/strong.rs#L22-L35)）；`EmphElem` 连字段都没有，只有一个 `body`（[src/model/emph.rs:26-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/emph.rs#L26-L31)）。它们并不直接写 `TextElem` 的 `delta`/`emph`。这条桥接是通过**内置 show 规则（`ShowSet`）**完成的：

- `StrongElem` 的内置规则等价于 `show strong: set text(delta: strong.delta)`；
- `EmphElem` 的内置规则等价于 `show emph: set text(emph: true)`。

这两个规则属于 `NativeRuleMap`（u4-l2），由 `Library.rules`（即 `Routines.rules`，u5-l4）在行为 crate（`typst-realize`）里注册。于是数据流是：

```
#strong[x]                  StrongElem{ delta: 300, body: x }
   │  （内置 ShowSet 规则，经 routines.rules 注册）
   ▼
set text(delta: +300) 套在 x 上   → StyleChain 里的 WeightDelta 折叠求和
   │
   ▼
variant() 读出 delta，thicken(300) → weight 变重 → 选到 bold 那款字体
```

这正是「反向影响」：用户写的是 `*x*`（语义化的强调），系统把它翻译成对内部 `delta` 字段的修饰，再由 `variant()` 综合后影响**最终选哪一款字体**。

**(e) 字号折叠（顺带）：`TextSize`。** 它与 `variant` 无关，但同属 `#[fold]` 文本字段，值得一并理解：

```rust
impl Fold for TextSize {
    fn fold(self, outer: Self) -> Self {
        // Multiply the two linear functions.
        Self(Length {
            em: Em::new(self.0.em.get() * outer.0.em.get()),
            abs: self.0.em.get() * outer.0.abs + self.0.abs,
        })
    }
}
```

见 [src/text/mod.rs:1129-1137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1129-L1137)。字号以「关于字号的一次函数」\( f(t) = a \cdot t + b \) 表示（\( t \) 是上层字号），折叠就是把两个线性函数复合。这正是 u4-l1 里讲过的 `TextSize::fold` 的真身所在。它的 `Resolve` 还会读 `EquationElem::size` 来给上下标缩放（[src/text/mod.rs:1139-1152](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1139-L1152)），这一点会在 u10（数学公式）用到。

#### 4.3.4 代码实践

**实践目标：** 解释 `variant()` 中 `emph / delta` 如何反向影响字体选择（对应规格里的实践任务）。

**操作步骤（源码追踪型）：**

1. 打开 [src/text/mod.rs:1101-1123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1101-L1123)，确认 `variant()` 读取 `style/weight/stretch` 后，还额外读了 `delta` 与 `emph`。
2. 跟踪一个具体例子 `#strong[_word_]`（强+斜）：
   - `EmphElem` 的内置规则写 `emph = true` → `ItalicToggle` 折叠后为 `true` → `style: Normal → Italic`。
   - `StrongElem` 的内置规则写 `delta = 300` → `WeightDelta` 折叠后为 `300` → `weight: 400 → thicken(300) → 700`。
   - 最终 `variant() = { Italic, 700, Normal }`，字体匹配会选「粗斜体」那款。
3. 用纸笔验证 `#emph[#emph[x]]`：两次 `ItalicToggle` 异或 → `true ^ true = false` → 回到 `Normal`，即「双重强调抵消」。

**需要观察的现象 / 预期结果：**

- `*word*`（strong）渲染为粗体；`_word_`（emph）渲染为斜体；`#strong[_word_]` 渲染为粗斜体——前提是字体族里真有对应款，否则 Typst 会按 `distance` 选最接近的（[src/text/font/book.rs:139-163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L139-L163)）。
- 嵌套两层 `_..._` 回到正体。

> 字体匹配的「距离」算法本身（`FontStyle::distance`、`FontWeight::distance`）在 [src/text/font/variant.rs:49-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variant.rs#L49-L63) 与 [src/text/font/variant.rs:137-140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variant.rs#L137-L140)，u7-l2 会深入。

#### 4.3.5 小练习与答案

**练习 1.** `variant()` 综合了哪几个字段？其中哪几个是用户可见、哪几个是内部 `#[internal]`？

> **答案：** 综合 `style / weight / stretch`（用户可见）与 `delta / emph`（`#[internal]`，分别见 [src/text/mod.rs:866-871](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L866-L871) 与 [src/text/mod.rs:873-878](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L873-L878)）。

**练习 2.** 为什么 `emph` 用 `ItalicToggle` + 异或折叠，而 `delta` 用 `WeightDelta` + 求和折叠？

> **答案：** 因为语义不同。「强调」是开关，重复两次应抵消（回到正常），故用异或（[src/text/mod.rs:1486-1489](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1486-L1489)）；「加重」是累加，嵌套加重应叠加，故用求和（[src/text/mod.rs:1496-1499](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1496-L1499)）。

**练习 3.** 假设当前 `weight = 400`，外层有 `#set strong(delta: 500)`，再写 `*x*`。`variant()` 算出的字重是多少？

> **答案：** `delta` 折叠求和为 `500`，`400.thicken(500) = 900`（饱和到上界，见 [src/text/font/variant.rs:132-135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variant.rs#L132-L135)），故字重为 `900`（Black）。

---

## 5. 综合实践

**任务：** 设计一份最小文档，把本讲三个模块串起来——验证「ghost 字段 + 字体列表 + variant 综合」如何共同决定一段文字最终用哪款字体。

**要求：**

1. 用 `font` 字段配置一个带 `covers` 的中英混排列表：
   ```typst
   #set text(
     font: (
       (name: "Inria Serif", covers: "latin-in-cjk"),
       "Noto Serif CJK SC",
     ),
     weight: "regular",
   )
   分别设置“中文”与 English 字体。
   ```
2. 在文档里写 `*加粗*`、`_斜体_`、`#strong[_粗斜_]` 各一段。
3. **源码追踪（写在纸上）：** 对 `#strong[_粗斜_]` 这段，画出从用户输入到最终 `FontVariant` 的完整数据流，标出：
   - `EmphElem` 与 `StrongElem` 经内置 `ShowSet` 规则分别写入了哪个内部字段；
   - `WeightDelta` 与 `ItalicToggle` 折叠后的值；
   - `variant()` 返回的 `{ style, weight, stretch }`；
   - 该 variant 会经 `FontBook::select` → `find_best_variant`（[src/text/font/book.rs:75-78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L75-L78)）选出哪一款。
4. **思考题：** 若把 `#set strong(delta: 0)` 加在前面，`*加粗*` 还会变粗吗？为什么？（提示：`WeightDelta::fold` 求和 + `variant()` 读 delta。）

**预期产出：** 一张数据流图 + 一段用源码行号佐证的文字说明。重点是讲清「`#text(...)` 只套样式、不建元素」「字体由 family 列表 + variant 共同决定」「strong/emph 通过内部 delta/emph 字段反向影响 variant」这三件事。

## 6. 本讲小结

- `TextElem` 是一个「几乎全是 ghost」的元素：struct 只存 `text: EcoString` 一个字段（[src/text/mod.rs:857-859](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L857-L859)），其余几十个样式字段都是 `#[ghost]`，只活在 `StyleChain`。
- `text` 函数的 `Construct` 是**特殊**的：它不创建 `TextElem` 节点，而是把参数转成 `Styles` 套在 body 上返回（[src/text/mod.rs:923-932](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L923-L932)）；真正的文本节点由 `TextElem::packed` 在文本处理阶段创建。
- 字体回退由 `FontList`（非空族列表）→ `FontFamily`（族名 + 可选 `Covers` 覆盖集）→ `families()`（拼上 emoji 兜底族）三级构成（[src/text/mod.rs:1082-1099](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1082-L1099)）。
- `variant()` 把用户可见的 `style/weight/stretch` 当基线，叠加内部 `delta`（`WeightDelta` 求和折叠，`thicken` 调字重）与 `emph`（`ItalicToggle` 异或折叠，翻转斜体），综合出用于选字体的 `FontVariant`（[src/text/mod.rs:1101-1123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1101-L1123)）。
- `#strong` / `#emph` 通过内置 `ShowSet` 规则（`Library.rules`）把语义化强调翻译成对 `delta` / `emph` 的写入，从而「反向影响」最终选哪款字体。
- 同属 `#[fold]` 的还有 `TextSize`（线性函数复合）与 `Costs`（按字段 `or` 合并），它们都体现了「同类型字段如何沿样式链累积」的统一模式。

## 7. 下一步学习建议

- **u7-l2（字体管理：FontBook、FontInfo、metrics、variations）**：本讲只用了 `FontBook::select` 的结果，下一讲深入「族内如何按 `variant` 距离挑最优款」、字体元数据与变量字体轴（`wght`/`wdth`/`slnt`/`ital`/`opsz`）。
- **u7-l3（OpenType 特性、语言、断行…）**：本讲的 `features()` 函数（[src/text/mod.rs:1396-1469](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/mod.rs#L1396-L1469)）把 `kerning`/`ligatures`/`number-type` 等字段映射成 OpenType tag，下一讲会逐条展开。
- **回顾 u4-l1（StyleChain / Fold / Resolve）**：如果你对 `TextSize::fold`、`WeightDelta::fold` 的折叠方向还有疑惑，重读 u4-l1 的 `get_folded`/`get_unfolded` 与 `reduce(fold)` 部分。
- **u10-l1（数学公式）**：本讲提到 `TextSize::resolve` 会读 `EquationElem::size` 给上下标缩放，这一联系在数学模式讲义中闭环。
