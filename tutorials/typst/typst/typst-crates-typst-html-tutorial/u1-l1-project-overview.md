# 项目概览与定位

## 1. 本讲目标

本讲是 typst-html（Typst 的 HTML 导出器）学习手册的第一篇。读完本讲，你应当能够：

- 说清楚 **typst-html 是什么**：它在 Typst 工作区中扮演什么角色、解决什么问题。
- 了解它的 **版本背景**：HTML 导出能力是从 Typst `0.13.0` 引入的，而当前工作区版本是 `0.15.1`。
- 看懂它的 **依赖清单**：它依赖哪些 typst 兄弟 crate，特别是 `typst-svg` 和 `typst-assets` 各自承担什么职责。
- 区分它和 **typst-svg、typst-pdf** 等其他导出器的关系。
- 在源码层面找到三个最基础的“积木”：`module()`、`HtmlElem`、`FrameElem`。

本讲只读代码、建立全局认知，不会让你修改任何源码。

## 2. 前置知识

阅读本讲前，最好对以下概念有一点概念性了解（不熟悉也没关系，我们会顺带解释）：

- **Typst 是什么**：一个用标记语言写文档、再编译成 PDF/SVG/PNG 等格式的排版系统。你可以把它理解成“现代版的 LaTeX”。
- **crate（工作区子项目）**：Typst 源码被拆分成很多个 Rust crate（即 `crates/` 目录下的子文件夹），每个 crate 负责一块独立功能。typst-html 就是其中之一。
- **导出器（exporter）**：把 Typst 内部的文档模型转换成某一种输出格式的模块。例如 typst-pdf 导出 PDF，typst-svg 导出 SVG，typst-html 导出 HTML。
- **HTML 的基本结构**：知道 `<html>`、`<head>`、`<body>`、`<div>` 等标签的大致含义即可。

> 术语提示：本讲常出现 “DOM” 一词，它指“文档对象模型”，也就是用树状结构表示一份 HTML 文档的内存表示。typst-html 内部会先把 Typst 文档转换成一棵 HTML DOM 树，再把这棵树编码（encode）成最终的 HTML 字符串。

## 3. 本讲源码地图

本讲只涉及两个最核心的文件：

| 文件 | 作用 |
| --- | --- |
| [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs) | crate 的入口。声明所有子模块，定义了供用户使用的两个原生元素 `HtmlElem`（`html.elem`）和 `FrameElem`（`html.frame`），以及把所有 HTML 定义打包成标准库模块的 `module()` 函数。 |
| [Cargo.toml](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/Cargo.toml) | crate 的清单文件，记录了它的名称、定位描述和全部依赖。 |

本讲在需要时也会顺带提到下面这些文件的名字（它们会在后续讲义中深入讲解，本讲只用它们来印证依赖关系）：

- `src/document.rs`：把 Typst 内容编译成 HTML 文档（`HtmlDocument`）的主入口。
- `src/encode.rs`：把 HTML 文档编码成最终 HTML 字符串的入口。
- `src/typed.rs`：基于 HTML 规范数据，自动生成 `html.div`、`html.span` 等“类型化”构造函数。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先看 typst-html 的整体定位与依赖，再依次精读 `module()`、`HtmlElem`、`FrameElem`。

### 4.1 typst-html 的定位、依赖与版本背景

#### 4.1.1 概念说明

一句话定位（来自源码本身的描述）：

> typst-html 是 **Typst 的 HTML 导出器（Typst's HTML exporter）**。

它的输入是 **Typst 文档（已经过排版引擎处理的内容）**，输出是 **一段合法的 HTML 字符串**。换句话说：

```
Typst 源文件(.typ)
   │  (Typst 编译 / 排版引擎)
   ▼
Typst 文档模型(Content / Document)
   │  (typst-html 的工作就在这一步之后)
   ▼
HTML 字符串(<!DOCTYPE html> ...)
```

需要特别强调 typst-html 和两个“近亲”导出器的区别：

- **typst-pdf**：把文档导出为 PDF。typst-html **并不依赖** typst-pdf，它们是平级的兄弟导出器。
- **typst-svg**：把文档导出为 SVG。typst-html **依赖** typst-svg，但用途不是“导出整篇 SVG”，而是借用 typst-svg 的能力来处理两类局部内容（见 4.1.3）。

版本背景（务必记住）：

- HTML 导出能力是在 Typst **`0.13.0`** 引入的，`html.elem` 和 `html.frame` 两个原生元素都标注了 `since = "0.13.0"`。
- `html.div`、`html.span` 等“类型化”构造函数是在 **`0.14.0`** 加入的。
- 我们现在阅读的工作区版本是 **`0.15.1`**（见根 `Cargo.toml` 的 `version = "0.15.1"`），所以这些能力都已经稳定可用。

#### 4.1.2 核心流程

从“外部调用”的角度，typst-html 对外暴露一条主线（这两个函数分别在 `document.rs` 和 `encode.rs`，本讲只做轮廓介绍，后续讲义会逐行精读）：

1. **编译**：调用 `html_document(engine, content, styles)`，把内容 `Content` 变成一棵内存中的 HTML DOM 树，包装成 `HtmlDocument`。
2. **编码**：调用 `html(document, &options)`，把这棵树序列化成最终的 HTML 字符串（开头会自动写入 `<!DOCTYPE html>`）。

用伪代码表示：

```
HtmlDocument = html_document(Engine, Content, StyleChain)   // 编译
String       = html(&HtmlDocument, &HtmlOptions)            // 编码
```

而本讲的主角 `module()` 不在这条“导出主线”上，它负责的是另一件事：**把 `html.elem`、`html.frame`、`html.div`……这些名字注册进 Typst 的标准库里**，让用户在 `.typ` 脚本里能写出来。

#### 4.1.3 源码精读

crate 的定位写在两个地方，完全一致：

[src/lib.rs:1](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L1) —— 文件第一行的文档注释：

```rust
//! Typst's HTML exporter.
```

[Cargo.toml:2-3](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/Cargo.toml#L2-L3) —— 包描述：

```toml
name = "typst-html"
description = "Typst's HTML exporter."
```

依赖清单在 [Cargo.toml:15-30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/Cargo.toml#L15-L30)：

```toml
[dependencies]
typst-assets = { workspace = true }
typst-library = { workspace = true }
typst-macros = { workspace = true }
typst-syntax = { workspace = true }
typst-timing = { workspace = true }
typst-utils = { workspace = true }
typst-svg = { workspace = true }
az = { workspace = true }
bumpalo = { workspace = true }
comemo = { workspace = true }
ecow = { workspace = true }
palette = { workspace = true }
rustc-hash = { workspace = true }
time = { workspace = true }
unicode-math-class = { workspace = true }
```

重点看其中 **7 个 typst-* 兄弟 crate** 的职责：

| 依赖 crate | 在 typst-html 里的作用 |
| --- | --- |
| `typst-library` | Typst 的核心标准库（元素、类型、引擎等）。typst-html 大量复用其中的 `Content`、`Engine`、各种元素定义。 |
| `typst-syntax` | 提供源码定位信息（`Span`），用于在报错时指出出错位置。 |
| `typst-macros` | 提供过程宏（如 `#[elem]`、`#[cast]`），用来声明式地定义元素和类型转换。 |
| `typst-utils` | 通用工具，如 `LazyHash`（延迟哈希）、`Protected` 等。 |
| `typst-timing` | 性能计时（`#[typst_macros::time]`），用来统计各阶段耗时。 |
| `typst-assets` | 提供 HTML 规范数据（来自 HTML 标准），用于自动生成 `html.div` 等类型化构造函数。 |
| `typst-svg` | 借用其 SVG 能力处理局部内容（见下文）。 |

`typst-svg` 和 `typst-assets` 这两个尤其关键，本讲的实践任务就是围绕它们展开。先用源码印证它们各自的用途：

**typst-svg 的三处使用**（这些文件不在本讲精读范围内，但能证明依赖关系是真实的）：

- 图片转成 base64 内嵌：[src/rules.rs:778](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L778) 用 `typst_svg::WebImage::new(&image).to_base64_url()`。
- 图片缩放对应的 CSS：[src/rules.rs:796](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/rules.rs#L796) 调用 `typst_svg::convert_image_scaling(...)`。
- 把排版好的内容以 **内联 SVG** 嵌入 HTML：[src/encode.rs:392](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L392) 调用 `typst_svg::svg_in_html(...)`（这就是 `html.frame` 的底层实现，4.4 节会再提到）。

**typst-assets 的使用**：

- [src/typed.rs:13](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L13) 引入 HTML 规范数据：`use typst_assets::html as data;`。这块数据被 `typed.rs` 用来在运行时动态生成所有 HTML 标签的构造函数（详见 u2-l5、u6-l3 讲义）。

一句话总结两个依赖的差异：

- `typst-svg` 提供 **运行时能力**：把图像和难以转成纯 HTML 的内容渲染成 SVG。
- `typst-assets` 提供 **规范数据**：告诉 typst-html “存在哪些合法 HTML 标签、每个标签有哪些属性”，从而自动生成类型化 API。

#### 4.1.4 代码实践

> 实践目标：亲手从源码中梳理出依赖，并准确描述 typst-svg、typst-assets 的职责，以及 typst-html 的输入输出。

操作步骤（纯源码阅读型实践，不需要编译运行）：

1. 打开 [Cargo.toml:15-30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/Cargo.toml#L15-L30)，把 `[dependencies]` 段里所有以 `typst-` 开头的依赖抄成一张清单（应当是 7 个）。
2. 在 `src/` 目录下搜索 `typst_svg` 的使用点（你在上文已经看到了 3 处），确认它确实被用于图片 base64 化、图片缩放 CSS 和内联 SVG 嵌入。
3. 在 `src/typed.rs` 顶部找到 `use typst_assets::html as data;`，确认类型化 API 是由这块规范数据驱动的。
4. 用一句话写出 typst-html 的输入与输出（提示：输入是 Typst 内容/文档模型，输出是 HTML 字符串）。

需要观察的现象：

- `Cargo.toml` 里 **没有** `typst-pdf`，说明 HTML 导出与 PDF 导出互不依赖。
- 7 个 typst-* 依赖中，只有 `typst-svg` 和 `typst-assets` 是“数据/渲染资源型”的，其余都是核心能力支撑。

预期结果（自检）：

- typst-* 依赖共 7 个：`typst-assets`、`typst-library`、`typst-macros`、`typst-syntax`、`typst-timing`、`typst-utils`、`typst-svg`。
- typst-html 的输入输出一句话：**输入 Typst 文档内容，输出一段合法的 HTML 字符串。**

> 说明：如果你本地配置了完整的 Typst 工作区，可以尝试 `cargo build -p typst-html` 验证依赖能解析；但本实践不依赖编译，纯阅读即可完成。

#### 4.1.5 小练习与答案

**练习 1**：typst-html 依赖 typst-pdf 吗？为什么这很重要？

> **参考答案**：不依赖。`Cargo.toml` 的 `[dependencies]` 段里没有 `typst-pdf`。这很重要，因为它说明 PDF 导出和 HTML 导出是两条独立的转换路径，各自从 Typst 的文档模型出发，互不影响。

**练习 2**：typst-svg 既是“SVG 导出器”，又被 typst-html 依赖，二者关系是否矛盾？

> **参考答案**：不矛盾。typst-html 借用 typst-svg 的底层能力（把图像转 base64、把局部内容渲染成内联 SVG），并不是用 typst-svg 去导出整篇文档。可以理解为 typst-svg 既是独立导出器，又是 typst-html 的“工具箱”。

---

### 4.2 `module()` 函数：HTML 模块的组装入口

#### 4.2.1 概念说明

Typst 的标准库是由一个个 **模块（Module）** 组成的，比如 `math`、`html`、`text` 等。当你在 `.typ` 文件里写 `html.elem(...)` 时，那个 `html` 就是一个模块对象。

`module()` 就是 **把所有与 HTML 相关的定义打包成一个 `Module` 的工厂函数**。它本身不参与“把文档编译成 HTML”的主流程，而是负责“把 HTML 相关的名字注册进标准库，供用户脚本使用”。

#### 4.2.2 核心流程

`module()` 做三件事，流程如下：

```
1. 创建一个“去重作用域”（deduplicating scope）
2. 在作用域里注册：
     ├─ HtmlElem   → 对应用户的 html.elem
     ├─ FrameElem  → 对应用户的 html.frame
     └─ typed::define(...) → 注册 html.div / html.span / … 等类型化构造函数
3. 把作用域包装成名为 "html" 的 Module 返回
```

#### 4.2.3 源码精读

完整的 `module()` 函数在 [src/lib.rs:33-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L33-L41)：

```rust
/// Creates the module with all HTML definitions.
pub fn module() -> Module {
    let mut html = Scope::deduplicating();
    html.start_category(Category::Html);
    html.define_elem::<HtmlElem>();
    html.define_elem::<FrameElem>();
    crate::typed::define(&mut html);
    Module::new("html", html)
}
```

逐行说明：

- `Scope::deduplicating()`：创建一个允许去重的作用域，避免重复定义同名项时报错。
- `start_category(Category::Html)`：声明这个作用域里的内容属于 `Html` 分类（用于文档归类）。
- `define_elem::<HtmlElem>()`：注册 `HtmlElem`，它在脚本中暴露为 `html.elem`（注意元素名由宏 `#[elem(name = "elem", ...)]` 决定，见 4.3.3）。
- `define_elem::<FrameElem>()`：注册 `FrameElem`，在脚本中暴露为 `html.frame`。
- `typed::define(&mut html)`：把基于 HTML 规范自动生成的所有类型化构造函数（`html.div`、`html.span`、`html.a`……）批量注册进去。
- `Module::new("html", html)`：最终包装成名为 `"html"` 的模块返回。

> 小提示：`HtmlElem` 和 `FrameElem` 是用 `#[elem]` 宏声明的“原生元素”，所以用 `define_elem`；而 `html.div` 这类是函数，由 `typed::define` 用另一种方式注册（`define_func_with_data`）。这两套机制共同构成了用户看到的 `html.*` 命名空间。

#### 4.2.4 代码实践

> 实践目标：理解 `module()` 注册了哪些用户可见的名字。

操作步骤：

1. 阅读 [src/lib.rs:33-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L33-L41)，列出它直接注册的两个元素。
2. 跟进 `crate::typed::define` 的实现 [src/typed.rs:31-35](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L31-L35)，确认它会遍历 `FUNCS`（由 `typst-assets` 规范数据生成）批量注册函数。

需要观察的现象：

- `module()` 自身只显式注册了 2 个元素，其余大量 `html.*` 函数都来自 `typed::define`。

预期结果：

- 用户脚本里能用的 `html.*` = `html.elem`（来自 `HtmlElem`）+ `html.frame`（来自 `FrameElem`）+ 一大批 `html.<标签名>`（来自 `typed::define`）。

#### 4.2.5 小练习与答案

**练习 1**：如果用户在脚本里写 `html.elem`，这个名字是在哪一行代码里被注册的？

> **参考答案**：在 [src/lib.rs:37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L37) 的 `html.define_elem::<HtmlElem>();`。注意脚本里暴露的名字是 `elem`（由 `#[elem(name = "elem", ...)]` 指定），而不是结构体名 `HtmlElem`。

**练习 2**：为什么 `html.div`、`html.span` 没有出现在 `module()` 的源码里？

> **参考答案**：因为它们是 `typed::define(&mut html)` 批量注册的。这批函数不是手写的，而是由 `typst-assets` 提供的 HTML 规范数据在运行时动态生成的（详见 typed.rs）。

---

### 4.3 `HtmlElem`：用户自定义任意 HTML 元素

#### 4.3.1 概念说明

Typst 的 HTML 导出会 **自动** 为大多数内容生成合适的标签（例如把标题变成 `<h2>`，把加粗变成 `<strong>`）。但有时用户想要 **完全控制** 输出的 HTML，比如写博客时把每篇文章包进一个 `<article>` 标签。

`HtmlElem`（脚本里写作 `html.elem`）就是为此而生：**它让你手写任意一个 HTML 元素**，包括任意标签名、任意属性、任意子内容。Typst 会校验你写的标签和属性是否构成合法的 HTML。

它从 `0.13.0` 起可用。

#### 4.3.2 核心流程

用户写法（来自源码文档注释里的示例）：

```typ
#html.elem("div", attrs: (style: "background: aqua"))[
  A div with _Typst content_ inside!
]
```

执行时发生的事：

```
html.elem(tag, attrs, body)
   │
   ├─ tag   → 校验并驻留(intern)标签名，得到 HtmlTag
   ├─ attrs → 折叠(fold)成一个 HtmlAttrs 属性集合
   ├─ body  → 作为任意 Typst 内容(Content)保留
   │
   └─ 构造出一个 HtmlElem 元素，随后参与 HTML 编译
```

一个重要的副作用规则：**如果用户自己用 `html.elem` 创建了 `<html>` 或 `<body>`，Typst 就不会再自动生成这些外层标签**（见 4.3.3 引用的文档）。

#### 4.3.3 源码精读

`HtmlElem` 的定义在 [src/lib.rs:64-104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L64-L104)。先看宏标注和字段（节选关键部分）：

```rust
#[elem(name = "elem", since = "0.13.0")]
pub struct HtmlElem {
    /// The element's tag.
    #[required]
    pub tag: HtmlTag,

    /// The element's HTML attributes.
    #[fold]
    pub attrs: HtmlAttrs,

    /// The element's CSS properties. Currently only used for generated styles.
    #[internal]
    #[parse(Some(css::Properties::default()))]
    pub css: css::Properties,

    /// The contents of the HTML element.
    #[positional]
    pub body: Option<Content>,
    // ... 还有 internal 字段 parent、role
}
```

逐字段说明：

- 宏 `#[elem(name = "elem", since = "0.13.0")]`：声明这是一个原生元素，脚本里叫 `elem`，自 `0.13.0` 引入。
- `tag: HtmlTag`（`#[required]`）：标签名，必填。`HtmlTag` 是一个做了字符串驻留（intern）的类型，能高效表示并校验标签名。
- `attrs: HtmlAttrs`（`#[fold]`）：HTML 属性集合。`#[fold]` 表示当多层样式给出同名属性时，会按“折叠”规则合并（后续 u2-l3 讲义会详解）。
- `css: css::Properties`（`#[internal]`）：内部使用的 CSS 属性，普通用户不会直接接触。
- `body: Option<Content>`（`#[positional]`）：元素的内容，可以是任意 Typst 内容；因为是 `Option`，所以像 `<meta>` 这种不接受内容的标签可以不提供 body。
- `parent`、`role`（`#[internal]`）：内部字段，分别记录逻辑父级和 ARIA 角色（如脚注的无障碍角色），普通用户不直接使用。

文档注释 [src/lib.rs:55-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L55-L57) 里有一段很重要的行为说明：

> Normally, Typst will generate `html`, `head`, and `body` tags for you. If you instead create them with this function, Typst will omit its own tags.

`HtmlElem` 还提供了两个构造辅助方法 [src/lib.rs:106-124](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L106-L124)：

```rust
impl HtmlElem {
    /// Add an attribute to the element.
    pub fn with_attr(mut self, attr: HtmlAttr, value: impl Into<EcoString>) -> Self {
        self.attrs
            .as_option_mut()
            .get_or_insert_with(Default::default)
            .push(attr, value);
        self
    }

    /// Adds the attribute to the element if value is not `None`.
    pub fn with_optional_attr(
        self,
        attr: HtmlAttr,
        value: Option<impl Into<EcoString>>,
    ) -> Self {
        if let Some(value) = value { self.with_attr(attr, value) } else { self }
    }
}
```

这两个方法主要供 Rust 侧（比如 show 规则、表格/图片导出）以编程方式拼接属性时使用：`with_attr` 无条件添加，`with_optional_attr` 在值为 `None` 时跳过。

#### 4.3.4 代码实践

> 实践目标：亲手用 `html.elem` 生成一段自定义 HTML，并观察 Typst 自动生成外层标签的行为。

操作步骤：

1. 准备一个最小的 Typst 文件 `hello.typ`，内容如下：

   ```typ
   #html.elem("div", attrs: (style: "background: aqua"))[
     A div with _Typst content_ inside!
   ]
   ```

2. 用 Typst CLI 以 HTML 格式导出（命令形如）：

   ```bash
   typst compile --format html hello.typ hello.html
   ```

3. 打开生成的 `hello.html`，观察它的整体结构。

需要观察的现象：

- 由于你没有自己写 `<html>`/`<body>`，Typst 应当 **自动** 包裹了 `<html>`、`<head>`（含 `<meta>` 等）和 `<body>`。
- 你的 `div` 里 `_Typst content_` 的强调部分会被自动转成对应的行内 HTML（例如 `<em>`）。

预期结果：

- 生成的 HTML 顶部有 `<!DOCTYPE html>`，外层是 `<html>...<head>...</head><body>...`，body 内部包含你写的 `<div style="background: aqua">…</div>`。

> 说明：具体命令行参数以你本机 Typst 版本为准；若无法运行 CLI，可改为“源码阅读型实践”：阅读 [src/lib.rs:59-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L59-L63) 的文档示例，并据此推断输出结构。结果标注为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`HtmlElem` 的 `body` 字段为什么是 `Option<Content>` 而不是 `Content`？

> **参考答案**：因为有些 HTML 标签（如 `<meta>`、`<br>`）是 **void 元素**，不接受任何子内容。把 `body` 设为 `Option`，用户在给这类标签构造元素时就可以不提供 body。

**练习 2**：如果你在文档里用 `html.elem("html")[...]` 自己包了一层 `<html>`，Typst 还会再自动包一层吗？

> **参考答案**：不会。源码文档明确说明：一旦用户自己用 `html.elem` 创建了 `<html>`/`<body>`，Typst 会省略自己自动生成的那一层（这对应 `document.rs` 中 `finalize_dom` 的判定逻辑，后续 u3-l2 讲义会深入）。

---

### 4.4 `FrameElem`：把内容以 SVG 嵌入

#### 4.4.1 概念说明

并不是所有 Typst 内容都适合转成纯 HTML。比如复杂的图表、精确定位的图形，它们依赖 **位置和样式** 来传达信息，强行转成 HTML 会丢失排版效果。

`FrameElem`（脚本里写作 `html.frame`）解决的就是这个问题：**它把一段内容用 Typst 的排版引擎布局（就像导出 PDF/SVG/PNG 时那样），然后以内联 SVG 的形式嵌入到 HTML 里**。这样这部分内容在 HTML 里看起来和 PDF 里一模一样。

它同样从 `0.13.0` 起可用。

#### 4.4.2 核心流程

```
html.frame[ ...要精确渲染的内容... ]
   │
   ├─ body: Content  → 被包装的 Typst 内容
   │
   ├─ (导出 HTML 时) 切换到 Paged 目标，用排版引擎把内容布局成 Frame
   ├─ 调用 typst_svg::svg_in_html(...) 把 Frame 渲染成内联 SVG
   └─ 把这段 SVG 嵌入到 HTML 文档对应位置
```

关键点：`FrameElem` 本身只声明“我要把这段内容当 frame 处理”，真正的 SVG 生成发生在编码阶段（`encode.rs` 的 `write_frame`）。

#### 4.4.3 源码精读

`FrameElem` 的定义在 [src/lib.rs:126-142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L126-L142)，结构非常简洁：

```rust
/// An element that lays out its content as an inline SVG.
/// ...（文档说明：内容会以 PDF/SVG/PNG 同款排版引擎渲染，再以内联 SVG 嵌入）
#[elem(since = "0.13.0")]
pub struct FrameElem {
    /// The content that shall be laid out.
    #[positional]
    #[required]
    pub body: Content,
}
```

说明：

- 宏 `#[elem(since = "0.13.0")]`：自 `0.13.0` 引入。注意这里没有 `name = "..."`，所以脚本里直接用结构体名的小写形式 `frame`，即 `html.frame`。
- `body: Content`（`#[positional]` + `#[required]`）：必填的位置参数，就是要渲染的那段内容。

它和 typst-svg 的联系在编码端：[src/encode.rs:390-400](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L390-L400) 的 `write_frame` 函数最终调用 `typst_svg::svg_in_html(...)`，这正是 4.1.3 里提到的 typst-svg 的第三处用途：

```rust
/// Encode a laid out frame into the writer.
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
    // ... 把 svg 字符串写入输出（pretty 模式下还会做缩进处理）
}
```

这段代码就是“frame 内容最终变成内联 SVG”的落地点。它的细节（`text_size`、`anchors`、`id` 等）会在 u6-l1 讲义中专题讲解，本讲只需记住结论：**`html.frame` 的底层渲染依赖 typst-svg**。

#### 4.4.4 代码实践

> 实践目标：对比 `html.elem`（纯 HTML）和 `html.frame`（内联 SVG）的输出差异。

操作步骤：

1. 准备一个 Typst 文件，故意放一段依赖精确定位的内容（例如一个带绝对位置的图形，或一个复杂的 `place` 排版），分别用两种方式包裹：

   ```typ
   #html.elem("div")[ #place(dx: 10pt, dy: 5pt)[A] ]

   #html.frame[ #place(dx: 10pt, dy: 5pt)[A] ]
   ```

2. 以 HTML 格式导出：

   ```bash
   typst compile --format html frame.typ frame.html
   ```

3. 打开 `frame.html`，对比两段输出。

需要观察的现象：

- `html.frame` 包裹的那段，在 HTML 里应该表现为一段 **内联 `<svg>`**，位置信息被保留。
- `html.elem` 包裹的那段，内容被按普通 HTML 流处理，精确定位信息可能丢失或表现不同。

预期结果：

- `html.frame` 输出里能找到 `<svg ...>` 片段；`html.elem` 输出里则是普通 HTML 标签。

> 说明：若本地无法运行 CLI，可改为阅读 [src/encode.rs:390-414](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L390-L414)，确认 `write_frame` 会调用 `typst_svg::svg_in_html` 生成 SVG，结果标注为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`FrameElem` 为什么只有一个 `body` 字段，而 `HtmlElem` 有一堆字段？

> **参考答案**：因为 `FrameElem` 的职责单一——把内容渲染成 SVG 嵌入，它不需要标签名、属性这些 HTML 概念（最终产出的是 `<svg>`，由底层统一处理）。而 `HtmlElem` 要表达任意 HTML 元素，所以需要 tag、attrs、body 等字段。

**练习 2**：`html.frame` 最终把内容渲染成 SVG，这一步用到了哪个兄弟 crate？

> **参考答案**：用到了 `typst-svg`，具体是 [src/encode.rs:392](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L392) 调用的 `typst_svg::svg_in_html`。这正是 typst-html 依赖 typst-svg 的核心原因之一。

---

## 5. 综合实践

设计一个小任务，把本讲的知识串起来。

**任务：为 typst-html 写一份一页纸的“项目名片”。**

请你完成以下全部内容（纯阅读 + 文档型任务，不改源码）：

1. **定位**：从 [src/lib.rs:1](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L1) 和 [Cargo.toml:2-3](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/Cargo.toml#L2-L3) 中引用原文，写出 typst-html 的一句话定位。
2. **版本**：在 [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs) 中找到 `html.elem` 和 `html.frame` 的 `since` 标注，记录它们分别是哪个版本引入的；并在 [src/typed.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs) 中找到类型化 API 的 `since` 版本。
3. **依赖**：列出 [Cargo.toml:15-30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/Cargo.toml#L15-L30) 中的全部 7 个 typst-* 依赖，并给 `typst-svg`、`typst-assets` 各写一句“它在 typst-html 里干什么”（用源码行号佐证）。
4. **入口**：解释 `module()`（[src/lib.rs:33-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L33-L41)）和“导出主线”（`html_document` → `html`）的区别——一个负责注册用户可见的名字，一个负责把文档变成 HTML 字符串。
5. **元素**：用一句话分别说清 `HtmlElem` 和 `FrameElem` 的用途，并指出它们各自 `since` 哪个版本。

完成后，你应当拥有：一份能向新人介绍“typst-html 是什么、依赖谁、对外暴露什么、版本背景如何”的简明笔记。

## 6. 本讲小结

- typst-html 是 **Typst 的 HTML 导出器**，输入 Typst 文档内容，输出合法的 HTML 字符串。
- 它与 typst-pdf 互不依赖；但 **依赖 typst-svg**（用于图片 base64 化、图片缩放 CSS、把内容渲染成内联 SVG）和 **typst-assets**（提供 HTML 规范数据，驱动类型化 API 的生成）。
- 版本背景：`html.elem`/`html.frame` 自 `0.13.0` 引入，类型化 `html.div` 等自 `0.14.0` 引入，当前工作区版本 `0.15.1`。
- `module()` 是标准库模块的组装入口：注册 `HtmlElem`、`FrameElem`，并批量注册 `typed::define` 生成的类型化构造函数。
- `HtmlElem`（`html.elem`）让用户手写任意 HTML 元素；若用户自建 `<html>`/`<body>`，Typst 会省略自动生成的外层标签。
- `FrameElem`（`html.frame`）把内容用排版引擎布局后以内联 SVG 嵌入，底层依赖 `typst_svg::svg_in_html`。

## 7. 下一步学习建议

本讲建立了全局认知，接下来建议：

- 想看清“目录是怎么组织的”，请继续阅读 **u1-l2《目录结构与模块组织》**，把 `src/` 下全部 `.rs` 文件的职责摸清楚。
- 想走通“从命令行到 HTML 字符串”的完整调用链，请阅读 **u1-l3《导出调用链与 CLI 入口》**。
- 想直接上手试用 `html.elem`/`html.frame`，可阅读 **u1-l4《用户侧 API：html.elem 与 html.frame》**。
- 在进入后续讲义前，建议先回头确认你能脱口而出：typst-html 的输入输出、它的 7 个 typst-* 依赖、以及 `module()` 与导出主线的区别。
