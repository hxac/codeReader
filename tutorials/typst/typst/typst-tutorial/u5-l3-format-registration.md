# 多目标架构：Format 注册与 HTML 导出

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `Format`（一个结构体，而非 trait）与 `FormatElement`（trait）如何把「一种导出格式」封装成可注册的库对象，以及 `Library.formats`、`std.format` 模块、全局 prelude 三者之间的装配关系。
2. 跟踪 `LibraryBuilder::build` 中规则注册与 prelude 注入的完整路径，理解「同一元素、不同目标、不同内置 show 规则」的 `(Element, Target)` 键控机制。
3. 理解同一套 `Content` 面向 paged 与 html 两种目标的分叉点：`Output` trait 的 `T::target()` / `T::create()`、`TargetElem` 样式字段，以及 CLI 中 `typst::compile::<PagedDocument>` 与 `typst::compile::<HtmlDocument>` 的两岔路口。
4. 画出 typst-html crate 的模块地图，描述 `HtmlDocument` 从 realize 到 DOM 再到字节串的生产流水线。

本讲是第五单元（专家层）的第三讲，承接 u5-l1（PDF 导出）——那时我们看到导出 crate「只消费 `PagedDocument` 抽象」；本讲回答一个更根本的问题：**这些导出格式是从哪里、以什么方式「插进」编译器的？**

## 2. 前置知识

本讲默认你已读过 u3-l2（Library 与 global 作用域）、u4-l1（编译主流程五阶段）、u4-l2（realize 与 NativeRuleMap）与 u5-l1（PDF 导出）。再用三段话补齐几个容易混淆的概念。

**三个名字相近但层次不同的概念。** 项目里 `Category::Html`、`Feature::Html`、`Target::Html` 都叫 "html"，但分工完全不同：

| 概念 | 定义处 | 回答的问题 |
|---|---|---|
| `Category`（如 `Html`、`Pdf`、`Format`） | typst-library 的 `lib.rs` | 「这个定义属于文档站里哪个分类？」——纯文档组织 |
| `Feature`（如 `Html`、`Bundle`、`A11yExtras`） | typst-library 的 `lib.rs` | 「这个实验性功能开了吗？」——运行时开关 |
| `Target`（`Paged` / `Html` / `Bundle`） | `foundations/target.rs` | 「这次编译为哪个目标？」——决定走哪条流水线 |

**什么是「格式元素」？** Typst 允许在文档里写 `#set html(pretty: true)` 来配置导出行为。`set` 规则只能作用于**元素**（elem），所以每种导出格式都需要一个元素来承载自己的选项字段——`html`、`pdf`、`svg`、`png`、`format.bundle` 这些名字背后的 Rust 类型统称格式元素。它们不能被用户手动构造（`construct` 直接报错），只为 `set` 规则而生。

**什么是「编译目标」？** `compile::<T>()` 是泛型入口（u4-l1 讲过）：`T` 可以是 `PagedDocument`（一叠页面帧，供 PDF/PNG/SVG 消费）或 `HtmlDocument`（一棵 HTML DOM）。`Target` 枚举与输出类型一一对应；pdf/png/svg 三种导出共享 `Target::Paged`，因为它们都是「对已排版页面帧的再编码」，而 html 是真正独立的第二目标。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `crates/typst-library/src/format.rs` | Format 抽象核心：`Format` 结构体、`FormatElement` trait、`std.format` 模块装配、`DocumentFormatOptions` 选项收集 |
| `crates/typst-library/src/lib.rs` | `Library`/`LibraryBuilder` 装配线、`Feature`/`Category` 枚举、`global()` 与 `prelude()` |
| `crates/typst-library/src/foundations/target.rs` | `Output` trait、`Target` 枚举、`TargetElem` 与脚本层 `target()` 函数 |
| `crates/typst-library/src/foundations/styles.rs` | `NativeRuleMap`：以 `(Element, Target)` 为键的内置 show 规则表 |
| `crates/typst/src/lib.rs` | `compile_impl` 的目标分叉、`LibraryExt`、总装 crate 的 `ROUTINES` 表 |
| `crates/typst-html/src/lib.rs` | typst-html 的模块清单与导出面 |
| `crates/typst-html/src/format.rs` | HTML 格式元素 `HtmlFormat`、`FORMAT` 常量、`html.elem` 与 `html.frame` |
| `crates/typst-html/src/document.rs` | `html_document`：realize → 转换 → DOM 收尾的流水线 |
| `crates/typst-html/src/dom.rs` | `HtmlDocument`、`HtmlNode` 与 `impl Output` |
| `crates/typst-html/src/convert.rs` | 把 realize 产物逐个转换为 HTML 节点 |
| `crates/typst-html/src/rules.rs` | HTML 目标的全部内置 show 规则及 `register` 入口 |
| `crates/typst-html/src/encode.rs` | `html()`：DOM 到字符串的序列化 |
| `crates/typst-cli/src/world.rs`、`compile.rs`、`args.rs` | CLI 侧：注册五种格式、推断输出格式、按目标分叉编译 |

## 4. 核心概念与源码讲解

### 4.1 Format 抽象：把「导出格式」变成库对象

#### 4.1.1 概念说明

一个导出格式要想被 Typst 认识，需要向编译器提供三样东西：

1. **一个格式元素**——让用户能 `#set pdf(...)`、`#set html(...)` 地配置它；
2. **一组可选的选项默认值**——导出器在写出文件时读取；
3. **若干目标专属的内置 show 规则**——比如 heading 在 HTML 目标下应变成 `<h1>`~`<h6>`。

在早期版本中，这些知识散落在编译器各处（硬编码的 match 分支）。如今的架构把它们收敛成一个普通的 Rust 值——`Format` 结构体，由**导出 crate 自己**定义并导出（如 `typst_html::FORMAT`），再由宿主在构建 `Library` 时传入。编译器核心（typst-library）不需要认识任何具体格式。

先澄清大纲中的一个说法：源码中**没有 `Format` trait**。「Format 抽象」实际由一对类型构成——`Format` 结构体（注册信息的载体）与 `FormatElement` trait（格式元素的契约）。这一点以源码为准。

#### 4.1.2 核心流程

一个格式从定义到可用，经过四步：

```text
导出 crate 定义格式元素 HtmlFormat（#[elem]，带 pretty 字段）
        │
        ▼
构造 Format 值：Format::new::<HtmlFormat>()
        .with_feature(Feature::Html)      ← 挂实验性开关
        .with_rules(typst_html::register) ← 挂目标专属规则注册函数
        │
        ▼
宿主调用 Library::builder([typst_html::FORMAT, ...]).build()
        │
        ├── build 内：格式元素被定义进 std.format 模块（html → format.html）
        ├── build 内：register_rules 把 HTML 目标规则写进 NativeRuleMap
        └── prelude：format.html 被提升为全局 html（继承 feature 开关）
```

运行期，文档里的 `#set html(pretty: true)` 会经 realize 阶段写入 `DocumentFormatOptions`，最终在导出时被 `document.options().get::<HtmlFormat>()` 取回——配置就这样从脚本层流到了导出层。

#### 4.1.3 源码精读

**`Format` 结构体**只有四个字段，全部围绕前述三样东西：

[crates/typst-library/src/format.rs:L105-L114](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/format.rs#L105-L114) —— `Format` 持有格式元素 `elem`、默认选项构造函数 `options`、可选的 feature 门 `feature` 与规则注册函数 `rules`。

[crates/typst-library/src/format.rs:L116-L150](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/format.rs#L116-L150) —— 构造器链：`new::<E>()` 关联格式元素与它的 `Options` 类型；`with_feature` 挂实验开关；`with_rules` 挂规则注册函数；`register_rules` 在 build 时被调用。注意这是一个用 `const fn` 与函数指针实现的「数据化插件」——没有任何 dyn trait 对象参与规则注册。

**`FormatElement` trait** 是格式元素的契约，只要求一个关联类型：

[crates/typst-library/src/format.rs:L152-L158](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/format.rs#L152-L158) —— `FormatElement: NativeElement` 带关联类型 `Options`，即该格式的选项结构体（须实现 `Populate + Default + Clone + Hash`）。

**HTML 侧的定义**是一个最小样本，导出 crate 只需两行：

[crates/typst-html/src/format.rs:L14](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/format.rs#L14) —— `pub const FORMAT: Format = Format::new::<HtmlFormat>().with_feature(Feature::Html);`。这是 typst-html 交给宿主的全部注册信息（HTML 的目标规则走的是另一条总装通道，见 4.2.3 的 `ROUTINES`）。

[crates/typst-html/src/format.rs:L127-L145](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/format.rs#L127-L145) —— `HtmlFormat` 元素本体：只有一个 `pretty` 字段（是否美化输出）；`Construct` 实现直接 `bail!`，禁止用户手动构造；`impl FormatElement` 把 `Options` 指向 `HtmlFormatOptions`。

**`std.format` 模块的装配**发生在 format.rs 自己的 `module` 函数：

[crates/typst-library/src/format.rs:L89-L103](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/format.rs#L89-L103) —— 遍历 `formats` 切片，把每个格式元素按名定义进 `format` 作用域；若格式带 feature（如 `Feature::Html`），绑定位也打上同样的门控——未启用 feature 时，`format.html` 在脚本层不可见。

**选项的流转**靠两个类型的接力：

[crates/typst-library/src/format.rs:L193-L232](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/format.rs#L193-L232) —— `DocumentFormatOptions` 是所有格式选项的类型擦除集合（内部 `EcoVec<FormatOptions>`）：`new` 按 `Library.formats` 生成默认值，`populate` 从文档样式链吸收 set 规则，`get::<T>()` 供导出器按格式元素类型取回（找不到就 panic 并提示「需要注册 `Library::formats`」）。

[crates/typst-html/src/encode.rs:L31-L36](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/encode.rs#L31-L36) —— 序列化入口 `html()` 第一行就调用 `document.options().get::<HtmlFormat>()`，把文档内 `#set html(pretty: true)` 与 CLI 传入的覆盖值合成最终选项。这就是「配置从脚本层流到导出层」的终点站。

#### 4.1.4 代码实践

**实践目标**：验证格式注册的存在性与 feature 门控效果。

**操作步骤**：

1. 总装 crate 里有一个现成单测，直接运行：

   ```bash
   cargo test -p typst formats
   ```

2. 阅读该测试源码：[crates/typst/src/lib.rs:L344-L357](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L344-L357) —— 它分别构造了「零格式」「只有 HTML」「只有 paged 格式」三种 Library，证明格式集合是完全可插拔的。

3. 写一个 `gate.typ`，内容为 `#html.elem("kbd")["F1"]`，先不带 feature 编译，再带 feature 编译：

   ```bash
   cargo run -p typst-cli -- compile gate.typ gate.pdf
   cargo run -p typst-cli -- compile --features html gate.typ gate.pdf
   ```

**需要观察的现象**：第一次编译应报「unknown variable: html」（绑定被 feature 门拦住）；第二次虽仍是 PDF 输出，但 `html.elem` 可用——feature 开关控制的是**库的可见性**，与编译目标独立。

**预期结果**：测试通过；两次编译的行为差异如上。具体报错文案待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Format` 用函数指针 `fn(&mut NativeRuleMap)` 而不是 `dyn trait` 来携带规则注册？

**答案**：规则注册函数无状态、无泛型参数、在 `const fn` 里就能挂上（`with_rules` 是 `const fn`），使整个 `FORMAT` 可以是编译期常量；同时避免了 trait 对象的动态分发与生命周期问题。这是「数据化插件」的典型手法——把行为装进普通数据，让 `Library` 保持可 `Hash`、可 `Clone`。

**练习 2**：若某个格式元素没有注册进 `Library.formats`，但导出器仍调用 `document.options().get::<E>()`，会发生什么？

**答案**：`DocumentFormatOptions::get` 会 panic，错误消息为 "format ... not found"，并附 hint「if you're a developer, you need to register `Library::formats`」。因为 `DocumentFormatOptions::new` 只按注册的格式生成默认值，未注册的格式在集合中不存在。

**练习 3**：`Format` 结构体的 `elem` 字段类型是 `Element` 而非泛型 `E`，这带来了什么灵活性？

**答案**：`Element` 是元素类型的运行时句柄（u3-l4 讲过），异构的格式元素集合可以统一放进 `Vec<Format>` / `Library.formats`，而不需要为每种格式做泛型参数化——`Library` 因此是具体类型而非泛型类型。

### 4.2 装配线：LibraryBuilder::build 中的规则注册与 prelude 注入

#### 4.2.1 概念说明

u3-l2 讲过 `Library` 是标准库的容器，但当时只展开了 `global()` 的模块注入。本讲补上与格式有关的另一半：`LibraryBuilder::build` 如何把「基础规则表 + 各格式专属规则」组装成 `Library.rules`，又如何通过 `prelude_path` 把 `format.html` 提升为全局 `html`。

关键在于理解 `NativeRuleMap` 的键：它不是只有元素，而是 **(元素, 目标)** 二元组。同一个 `HeadingElem`，在 paged 目标下的内置规则来自 typst-layout，在 html 目标下的规则来自 typst-html——两套规则并存于同一张表，realize 阶段按当前目标查表（u4-l2 讲过查表侧）。

#### 4.2.2 核心流程

`LibraryBuilder::build` 的完整装配次序：

```text
build()
  ├── math::module()                          数学模块
  ├── global(&self.formats, math, inputs)     全局作用域
  │     ├── foundations/model/text/layout/... 各模块 define
  │     ├── define("format", format::module(formats))   ← 格式元素进 std.format
  │     └── prelude(...)
  │           ├── 颜色/方向/对齐常量
  │           └── prelude_path(["format","html"])        ← 提升为全局 html
  │               prelude_path(["format","pdf"])         ← 提升为全局 pdf
  │               prelude_path(["format","bundle","asset"]) ← 提升 format.bundle.asset
  ├── rules = (self.routines.rules)()         基础规则表（来自 ROUTINES）
  └── for format in &self.formats:
        format.register_rules(&mut rules)     各格式追加专属规则
```

#### 4.2.3 源码精读

**build 的主体**：

[crates/typst-library/src/lib.rs:L231-L249](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/lib.rs#L231-L249) —— `build()` 先构造 `global`（把 `formats` 传进去），再调用 `(self.routines.rules)()` 取基础规则表，随后逐个 `format.register_rules(&mut rules)` 追加格式专属规则，最终组装 `Library`（注意 `formats` 字段原样保存，供运行期生成 `DocumentFormatOptions` 默认值）。

**基础规则表来自总装 crate 的 ROUTINES**（u1-l3 讲过这张函数指针表）：

[crates/typst/src/lib.rs:L329-L342](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L329-L342) —— `rules` 闭包先 `typst_layout::register`（paged 目标全部规则），再 `typst_html::register`（html 目标全部规则）。注意这里没有 typst-bundle 的 register——bundle 目标复用 html 规则，细节留给 u5-l4。

**`NativeRuleMap` 的键结构**：

[crates/typst-library/src/foundations/styles.rs:L1010-L1014](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/styles.rs#L1010-L1014) —— 内部是 `IndexMap<(Element, Target), NativeShowRule>`：同一元素在每个目标下各有一条独立规则。

[crates/typst-library/src/foundations/styles.rs:L1030-L1058](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/styles.rs#L1030-L1058) —— `NativeRuleMap::new` 为三个目标都注册了少数「跨目标公共规则」（如 `ContextElem`、计数器更新这类永远输出空的元素）。

**HTML 目标的 register**：

[crates/typst-html/src/rules.rs:L39-L92](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/rules.rs#L39-L92) —— 以 `rules.register(Html, XXX_RULE)` 的形式为模型、文本、可视化、数学各类元素登记 HTML 外观；最后一条很特别：给 **Paged** 目标注册 `FrameElem` 规则使其成为 no-op——`html.frame` 只在 HTML 目标下有意义（内嵌一段 paged 布局），若用户写了 `show math.equation: html.frame` 之类的规则导致 HTML 输出里嵌套了 frame，paged 规则保证嵌套帧不炸。

**规则长什么样**：

[crates/typst-html/src/rules.rs:L94-L101](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/rules.rs#L94-L101) —— `PAR_RULE` 把段落包进 `<p>`，`STRONG_RULE` 包进 `<strong>`，`EMPH_RULE` 包进 `<em>`。对比 u4-l2 讲过的 paged 侧同名规则（要做断行、分组），HTML 侧常常只是「换一个标签」——这正是「语义化 HTML」路线的体现：不做视觉排版，只做结构翻译。

**prelude 的格式提升**：

[crates/typst-library/src/lib.rs:L427-L431](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/lib.rs#L427-L431) —— 注释直白地写着「If available, add some items to the global prelude」：把 `format.html`、`format.pdf`、`format.bundle.asset` 提升为全局名字。

[crates/typst-library/src/foundations/scope.rs:L226-L260](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/scope.rs#L226-L260) —— `prelude_path` 沿路径（如 `["format", "html"]`）逐段下钻取出绑定，克隆后以末段名字重新绑定进全局作用域；路径上遇到的 feature 门会被继承到新绑定（若路径被两个不同 feature 门控则 panic）。效果：用户写 `html.elem(...)` 即可，不必写 `format.html.elem(...)`，且未开 `--features html` 时全局 `html` 同样不可见。

#### 4.2.4 代码实践

**实践目标**：观察 prelude 提升与 `pretty` 选项的效果。

**操作步骤**：

1. 写 `pretty.typ`：

   ```typst
   = 标题
   一段 *强调* 文字。
   #set html(pretty: true)
   ```

2. 分别编译两次（一次删掉 set 行，一次保留）：

   ```bash
   cargo run -p typst-cli -- compile --features html pretty.typ pretty-on.html
   ```

3. 用文本工具对比两次输出的缩进与换行（例如 `head -20`）。

**需要观察的现象**：开启 `pretty` 后输出是缩进的多行 HTML；未开启时是紧凑单行。`pretty` 从脚本层的 set 规则出发，经 `HtmlFormatOptions::populate`（[crates/typst-html/src/format.rs:L167-L172](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/format.rs#L167-L172)）进入 `DocumentFormatOptions`，最后在 encode.rs 的 `html()` 中生效——你可以把这条链与本节源码一一对照。

**预期结果**：两种输出的 HTML 结构相同、空白不同。具体样式待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：CLI 的 `SystemWorld` 一次注册了哪些格式？在哪里注册？

**答案**：[crates/typst-cli/src/world.rs:L67-L73](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/world.rs#L67-L73)：`typst_html::FORMAT`、`typst_pdf::FORMAT`、`typst_svg::FORMAT`、`typst_render::FORMAT`、`typst_bundle::FORMAT` 共五种，全部一次性传给 `Library::builder`。注册集合与编译目标无关——编译 PDF 时 HTML 格式也注册着，用户可以 `context` 查询或在双目标模板里引用。

**练习 2**：`#set html(pretty: true)` 为什么必须写在文档里、由 realize 收集，而不是让 CLI 直接传给导出器？

**答案**：CLI 也传（`html_options(config)` 里 `pretty: config.pretty`），但文档内 set 规则让**文档本身**可携带格式偏好（如仓库文档声明自己要美化输出），并且选项经 `DocumentFormatOptions` 统一存储、按「文档值 vs 导出器覆盖值」合成（`HtmlOptions::resolve`，CLI 传入的覆盖文档默认）。两条通道在 encode.rs 汇合。

**练习 3**：`prelude_path(["format","bundle","asset"])` 提升后，用户写 `asset(...)` 还是 `format.bundle.asset(...)` 都行吗？

**答案**：都行——提升是「复制绑定」而非「移动」：原路径下的绑定仍在 `format.bundle` 模块里，全局多了一份同值绑定（带继承的 feature 门 `Feature::Bundle`）。

### 4.3 HTML 目标组织：typst-html 的模块地图与 HtmlDocument 流水线

#### 4.3.1 概念说明

typst-html 是一个「迷你编译器后端」：它不做视觉排版（没有页面、没有断行），而是把 realize 产物中的语义元素翻译成结构化的 HTML DOM（元素树），再序列化为字符串。它的设计哲学写在 `HtmlFormat` 的文档注释里（[crates/typst-html/src/format.rs:L29-L54](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/format.rs#L29-L54)）：HTML 捕捉文档**结构**而非视觉，Typst 无法替你猜出最佳语义表示，因此提供 `html.elem` 给你完全控制权，让内容与目标解耦、在 PDF 和 HTML 间共享。

模块划分（大纲标注「待确认」，现按源码确认如下）：

| 模块 | 职责 |
|---|---|
| `dom` / `tag` / `attr` / `property` | DOM 数据结构：`HtmlElement`、`HtmlNode`、标签与属性的常量集 |
| `format` | 格式元素 `HtmlFormat`、`html.elem`、`html.frame`、`FORMAT` 常量 |
| `document` | `html_document` 流水线与 `HtmlDocument` 的组装 |
| `convert` | realize 产物 → HTML 节点的逐元素转换器 |
| `rules` | HTML 目标内置 show 规则（4.2 已读） |
| `encode` | DOM → 字符串 |
| `css` / `charsets` / `mathml` | 内联样式解析、文本编码处理、数学公式的 MathML 转换 |
| `introspect` / `link` / `fragment` | `HtmlIntrospector`、锚点生成、HTML 片段转换 |
| `typed` | 类型化 HTML API（`html.video(...)` 这类便捷函数） |

#### 4.3.2 核心流程

`HtmlDocument` 的生产流水线（与 paged 侧 `layout_document` 严格同构，都嵌在 u4-l1 的收敛循环里）：

```text
html_document(engine, content, styles)
  └── html_document_impl  [comemo::memoize]
        └── html_document_common
              ├── 组装子 Engine（继承 introspector/sink/route）
              ├── DocumentInfo::populate + DocumentFormatOptions::new/populate
              ├── (routines.realize)(RealizationKind::Document, ...)
              │        ← 与 paged 共用同一个 realize！目标差异体现在
              │          NativeRuleMap 按 (Element, Target::Html) 查到的规则
              ├── convert::convert_to_nodes(children, Block, Normal)
              │        ← Pair 流逐个翻译成 HtmlNode
              ├── finalize_dom  补 <html>/<head>/<body>、脚注容器
              ├── css::resolve_inline_styles
              └── 若含公式：向 <head> 注入公式 CSS 常量
        └── create_link_anchors → HtmlDocument::new（内建 HtmlIntrospector）
```

注意关键一点：**realize 是共享的**。HTML 编译不做「另一套排版」，而是复用同一条 realize 流水线；目标差异在 realize 内部查 `NativeRuleMap` 时就已经分岔（查到的是 `rules.rs` 里的 HTML 规则）。这正是「一套 Content、多个目标」的架构落点。

#### 4.3.3 源码精读

**入口与 memoize 层**：

[crates/typst-html/src/document.rs:L26-L41](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/document.rs#L26-L41) —— `html_document` 只是把 `Engine` 拆包转发给内部实现；注释点明流程「先做根级 realize，再把结果元素转成 HTML」。

[crates/typst-html/src/document.rs:L44-L74](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/document.rs#L44-L74) —— `html_document_impl` 带 `#[comemo::memoize]`，是增量编译的缓存边界；编译完成后还调用 `create_link_anchors` 给被链接元素分配 HTML 片段 ID——这是 u4-l7 内省机制在 HTML 目标下的对应物。

**共享主体**：

[crates/typst-html/src/document.rs:L158-L181](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/document.rs#L158-L181) —— 三步核心：先用 `DocumentFormatOptions::new(&library.formats)` 生成默认格式选项并从样式链 populate（`#set html(...)` 在此生效）；再调用 `(engine.library.routines.realize)` 做根级 realize（与 paged 完全同一条代码路径）；最后 `convert_to_nodes` 把 realize 输出的 Pair 流转成 `HtmlNode` 序列。

**convert 的逐元素分发**：

[crates/typst-html/src/convert.rs:L23-L31](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/convert.rs#L23-L31) —— `ConversionLevel` 区分块级/行内上下文：块级有独立的智能引号状态，行级共享上层状态——HTML 转换也要处理引号、空白这类排版细节，只是在 DOM 层面。

[crates/typst-html/src/convert.rs:L60-L90](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/convert.rs#L60-L90) 与 [crates/typst-html/src/convert.rs:L93-L110](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/convert.rs#L93-L110) —— `convert_to_nodes` 遍历 Pair 流逐个交给 `handle`；`handle` 是一个长 if-else 链：`TagElem` 原样透传（内省标签）、`HtmlElem` 走专门处理、空格变文本、`TextElem` 应用大小写后输出、零宽间距吞掉、换行依空白模式变 `<br>` 或 `\n`……未被任何分支命中的元素会走「用引擎对其应用 show 规则再递归」的通用路径。

**DOM 收尾**：

[crates/typst-html/src/document.rs:L262-L314](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/document.rs#L262-L314) —— `finalize_dom`：若用户已自己写了 `<html>` 或 `<body>` 元素（经 `html.elem`），则尊重之（脚注此时报不支持）；否则把节点包进 `<body>`（并追加脚注容器）再包进带 `lang` 属性的 `<html>`。

[crates/typst-html/src/document.rs:L317-L365](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/document.rs#L317-L365) —— `head_element` 由 `DocumentInfo`（标题、作者、关键词，来自 `#set document(...)`）生成 `<meta>`/`<title>`——又一次「脚本层配置流向输出层」。

**输出数据结构**：

[crates/typst-html/src/dom.rs:L116-L125](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/dom.rs#L116-L125) —— `HtmlNode` 四变体：`Tag`（内省标签，不产出可见 HTML）、`Text`、`Element`、`Frame`（内嵌的 paged 布局结果，将来序列化为 SVG）。对比 paged 侧 `FrameItem` 的六类图元（u5-l1/u5-l2），两套「输出树」的形状完全不同——这正是两种目标的本质差异。

**用户侧的两个元素**：

[crates/typst-html/src/format.rs:L204-L244](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/format.rs#L204-L244) —— `HtmlElem`（脚本名 `html.elem`）：原始 HTML 元素，tag 必须合法、可选属性与任意 Typst 内容体。

[crates/typst-html/src/format.rs:L276-L282](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/format.rs#L276-L282) —— `FrameElem`（脚本名 `html.frame`）：把一段内容按 paged 引擎完整排版后内嵌进 HTML——当内容本质是视觉性的（图表）时的逃生舱，也是「同一文档内目标可以局部切换」的体现（frame 内部 `target()` 返回 `paged`）。

#### 4.3.4 代码实践

**实践目标**：源码阅读型实践——跟踪一个 `<p>` 的诞生。

**操作步骤**：

1. 写 `p.typ`：`一段普通文字。`（无任何标题）。
2. 在源码中按下面链条依次定位并阅读（不需要运行）：
   - `ParElem` 的 HTML 规则：rules.rs 的 `PAR_RULE`（4.2.3 已引用，L94-L95）——段落被包进 `HtmlElem::new(tag::p)`；
   - realize 输出含 `HtmlElem` 的 Pair 流：convert.rs `handle` 的 `handle_html_elem` 分支（L100-L101 起）；
   - `finalize_dom` 补 `<body>`/`<html>`；
   - encode.rs `write_element` 输出标签。
3. 用 `cargo testit --stages html` 跑 HTML 阶段的集成测试，确认 HTML stage 可用（stage 说明见 [tests/README.md:L70](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/README.md#L70)）。

**需要观察的现象**：`cargo testit --stages html` 只执行到 HTML 输出、不生成 PDF/SVG，速度明显快于全 stage。

**预期结果**：能在源码中指出 `<p>` 出现的确切代码行（rules.rs 的 `PAR_RULE`）。测试执行输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：HTML 编译也做 introspection 迭代吗？证据在哪？

**答案**：做。`HtmlDocument::new` 内建 `HtmlIntrospector`（dom.rs L38-L50），`impl Output for HtmlDocument` 的 `introspector()` 返回它，于是 u4-l1 的收敛循环（`compile_impl` 里对 `document.introspector()` 的验证）对 HTML 同样生效；DOM 中的 `HtmlNode::Tag` 就是供内省器收集的标签。

**练习 2**：为什么 `HtmlDocument` 不实现 `Hash`，而 `PagedDocument` 需要？

**答案**：见 [crates/typst-html/src/dom.rs:L21-L25](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/dom.rs#L21-L25) 的注释：HTML introspector 不可哈希且（因 `root_mut` 交叉链接）不保证完全由输出派生。paged 侧需要 Hash 是因为 comemo 收敛验证要哈希 introspector；html 侧的收敛判定走 comemo 约束的其他证据。

**练习 3**：`html.frame` 里再套 `html.frame` 会怎样？

**答案**：不会炸——rules.rs L88-L91 特意为 **Paged** 目标注册了 `FrameElem` 的 no-op 规则（直接返回 body）。因为 frame 内部是以 paged 目标排版其中的内容的，若用户写了 `show math.equation: html.frame` 之类的规则，嵌套 frame 会出现在 paged 上下文里，这条规则让它退化为透明。

### 4.4 目标分叉点：Output、Target 与 CLI 的两岔路口

#### 4.4.1 概念说明

「同一套 Content 面向多个目标」的分叉不是一处 if，而是**三层机制**的配合：

1. **类型层**：`compile::<T>` 的泛型参数 `T: Output` 决定输出文档类型；`Output::target()` 把类型映射到 `Target` 枚举。
2. **样式层**：`compile_impl` 把目标写进 `TargetElem::target` 样式字段，随样式链流经 realize/layout/show 规则；脚本层 `context target()` 读的就是它。因此目标在**编译中途也可以局部不同**（`html.frame` 内是 paged）。
3. **规则层**：`NativeRuleMap` 按 `(Element, Target)` 提供每个目标的默认外观（4.2 已讲）。

CLI 则是最外层的岔口：`--format html` 或 `.html` 扩展名 → `OutputFormat::Html` → `typst::compile::<HtmlDocument>`。

#### 4.4.2 核心流程

```text
CLI: typst compile [--format html | 输出 .html] [--features html] doc.typ
  │
  ├─ CompileConfig::new_impl：推断 output_format（--format 优先，否则看扩展名，默认 PDF）
  ├─ SystemWorld::new：Library::builder([五种 FORMAT]) + with_features(--features)
  │
  └─ compile_and_export:
        match output_format
          Pdf/Png/Svg → typst::compile::<PagedDocument> → export_paged/export_image
          Html        → typst::compile::<HtmlDocument> → export_html（encode）
          Bundle      → （u5-l4 展开）

typst::compile::<T> → compile_impl:
  match T::target()
    Paged  → 无条件放行
    Html   → 未开 feature 则报错；开了则发"实验性"警告
  TargetElem::target.set(T::target()) → 写入样式链
  eval（一次，与目标无关）
  循环 { T::create(...) }  ← Paged: layout_document / Html: html_document
  收敛后返回 T
```

#### 4.4.3 源码精读

**`Output` trait——输出类型的契约**：

[crates/typst-library/src/foundations/target.rs:L13-L30](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/target.rs#L13-L30) —— 三个方法：`target()` 类型级的目标声明、`create()` 在收敛循环里被调用以生产文档、`introspector()` 供收敛判定。注释强调它与 `Target` 变体「一一对应」。

**`Target` 枚举与 `TargetElem`**：

[crates/typst-library/src/foundations/target.rs:L66-L91](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/target.rs#L66-L91) —— `Target` 三变体（`Paged` 为默认）；`TargetElem` 是「只为承载样式字段而存在」的隐形元素，永不构造、用户不可见。

**脚本层的 `target()` 函数**：

[crates/typst-library/src/foundations/target.rs:L134-L137](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/target.rs#L134-L137) —— `#[func(contextual)]`，从 `Context` 的样式链读 `TargetElem::target`。其文档（L101-L133）说明了取值规则，并给出 `kbd` 双目标模板的经典示例——内容与目标解耦的推荐写法。

**编译期的第一道岔口**：

[crates/typst/src/lib.rs:L100-L110](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L100-L110) —— `compile_impl` 开头就 `match T::target()`：Paged 直接放行；Html/Bundle 调用 feature 检查函数，未开 feature 时直接报错退出。

[crates/typst/src/lib.rs:L248-L267](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L248-L267) —— `warn_or_error_for_html`：开了 `Feature::Html` 发实验性警告（附跟踪 issue 链接），没开则 `bail!`「html export is only available when `--features html` is passed」。这解释了为什么格式注册（4.1）与 feature 开关是两道独立的门。

**目标写入样式链**：

[crates/typst/src/lib.rs:L112-L114](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L112-L114) —— `TargetElem::target.set(T::target()).wrap()` 把目标作为样式挂上基础样式链——此后 eval 产出的每份 Content、每条 show 规则都能从样式链读到当前目标。

**eval 与循环**：

[crates/typst/src/lib.rs:L124-L132](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L124-L132) —— 求值一次、与目标无关（脚本的 `#let`、函数定义对所有目标相同）；

[crates/typst/src/lib.rs:L134-L187](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L134-L187) —— 收敛循环里 [L157](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L157) 的 `T::create(&mut engine, &content, styles)` 是分叉的终点站：`PagedDocument` 的 `create` 调 layout，`HtmlDocument` 的 `create` 调 `html_document`。

**HtmlDocument 的 Output 实现**：

[crates/typst-html/src/dom.rs:L96-L112](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/dom.rs#L96-L112) —— `target()` 返回 `Target::Html`，`create()` 转发到 `crate::html_document`。加上 [L86-L94](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/dom.rs#L86-L94) 的 `impl Document`（提供 `info()`/`options()`），typst-html 对编译器核心的全部义务就履行完毕了。

**CLI 的两岔路口**：

[crates/typst-cli/src/compile.rs:L114-L130](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/compile.rs#L114-L130) —— 格式推断三级：`--format` 显式指定 > 输出路径扩展名（.pdf/.png/.svg/.html）> 默认 PDF。注意 CLI 参数就是 `--format html`（大纲中的「拼写待确认」在此确认；`--features html` 来自 [crates/typst-cli/src/args.rs:L519-L522](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/args.rs#L519-L522)，也支持环境变量 `TYPST_FEATURES`）。

[crates/typst-cli/src/compile.rs:L350-L355](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/compile.rs#L350-L355) —— **本讲最核心的分叉点**：paged 分支 `typst::compile::<PagedDocument>`，html 分支 `typst::compile::<HtmlDocument>`——同一个 `compile` 函数、同一个 world、同一个 eval 结果，只因泛型参数不同而走向完全不同的文档生产流水线。

[crates/typst-cli/src/compile.rs:L367-L378](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/compile.rs#L367-L378) —— `export_html`：调 `typst_html::html` 序列化后写文件；watch 模式下还会把 HTML 推给 live-reload 服务器（仅在 Html/Bundle 目标时创建，见 L211-L218 的 `#[cfg(feature = "http-server")]` 分支）。

**LibraryExt——宿主注册格式的唯一入口**：

[crates/typst/src/lib.rs:L290-L323](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L290-L323) —— `Library::new(formats)` / `Library::builder(formats)` 都要求传入格式集合；文档注释里的示例（传 `typst_html::FORMAT`、`typst_pdf::FORMAT` 等）就是 CLI 实际做的事。注意总装 crate `typst` 把 typst-html、typst-pdf 等作为依赖引入，而 typst-library 对它们一无所知——依赖箭头永远从总装指向部件，「格式注册」因此成为导出 crate 与库 crate 之间唯一需要交换的信息。

#### 4.4.4 代码实践

见第 5 节综合实践（本模块与综合实践共用一个双目标编译任务，避免重复）。

快速热身：读 [crates/typst/src/lib.rs:L344-L357](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L344-L357) 的 `formats` 测试，然后回答：`Library::new([])`（零格式）合法吗？编译能进行吗？

**预期结果**：合法——Library 可以不注册任何格式；编译 PDF 目标也能走完（`DocumentFormatOptions` 只是空集合）。但脚本里任何 `#set pdf(...)`/`#set html(...)` 都会因名字不存在而报错。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`Target::Paged` 对应 PDF、PNG、SVG 三种导出，为什么它们不各自成为一个 Target？

**答案**：因为三者消费的是同一种中间产物 `PagedDocument`（u5-l1/u5-l2 讲过：pdf/png/svg 只是同一叠 Frame 的三种编码），realize/layout 阶段的行为完全一致，规则表无需区分。Target 划分的依据是「中间产物与规则集是否不同」，而非「文件格式是否不同」。

**练习 2**：如果不传 `--features html` 而直接 `typst compile --format html doc.typ`，会在哪一层、以什么方式失败？

**答案**：在编译器核心的 `compile_impl`（typst/src/lib.rs L106-L110 → L259-L264）以错误诊断失败：「html export is only available when `--features html` is passed」。注意甚至更早：CLI 在 compile.rs L214 等处的 http-server 逻辑不受影响，但脚本若引用 `html` 变量会在 eval 期就报 unknown variable——两道门（库可见性、编译目标许可）独立存在。

**练习 3**：为什么 `target()` 是 contextual 函数而不能在编译开始前一次确定？

**答案**：因为 `html.frame` 会让目标在文档中途局部变化（frame 内部以 paged 目标排版），且 show 规则可以按上下文返回不同内容；目标存储在样式链（`TargetElem::target`）里，随内容位置流动，所以必须用 `context` 表达式在求值/realize 时点读取。

## 5. 综合实践

**任务：双目标文档编译对比，定位分叉调用点。**

1. **准备文档** `targets.typ`（内容与目标解耦的推荐写法，取材自 `target()` 函数文档示例）：

   ```typst
   #set document(title: "Dual-target demo")
   #let kbd(it) = context {
     if target() == "html" {
       html.elem("kbd", it)
     } else {
       box(fill: luma(240), inset: 5pt, radius: 3pt, raw(it))
     }
   }

   = 双目标文档

   Press #kbd["F1"] for help.
   ```

2. **两种目标各编译一次**（需先 `cargo build -p typst-cli`）：

   ```bash
   cargo run -p typst-cli -- compile --features html targets.typ targets.html
   cargo run -p typst-cli -- compile targets.typ targets.pdf
   ```

3. **观察输出结构差异**：
   - `targets.html`：`<html lang=...>`/`<head>`（含 `<title>Dual-target demo</title>`）/`<body>` 结构，标题是 `<h1>`，`kbd` 是 `<kbd>` 标签——来自 rules.rs 的 `HEADING_RULE` 与 `html.elem`；
   - `targets.pdf`：无 DOM 结构，`kbd` 是灰底圆角盒子——来自 paged 侧规则与样式。
   - 两份输出的**文字内容完全相同**：eval 只执行一次、内容共享，差异全部来自目标层。

4. **在源码中标出分叉路径**，依次是：
   - [crates/typst-cli/src/compile.rs:L114-L130](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/compile.rs#L114-L130)：`.html` 扩展名 → `OutputFormat::Html`；
   - [crates/typst-cli/src/compile.rs:L350-L355](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/compile.rs#L350-L355)：`compile::<PagedDocument>` vs `compile::<HtmlDocument>`；
   - [crates/typst/src/lib.rs:L112-L114](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L112-L114)：目标写入样式链，`context target()` 由此读到 `"html"` 或 `"paged"`；
   - realize 查表：`(HeadingElem, Target::Html)` 命中 rules.rs 的 `HEADING_RULE`，`(HeadingElem, Target::Paged)` 命中 typst-layout 的规则；
   - [crates/typst-html/src/dom.rs:L105-L111](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/dom.rs#L105-L111)：`HtmlDocument::create` → `html_document` 流水线。

5. **验证选项流转**（可选）：在文档里加一行 `#set html(pretty: true)`，重编 HTML，对比缩进变化；再对照 [crates/typst-html/src/document.rs:L163-L164](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/document.rs#L163-L164) 与 [crates/typst-html/src/encode.rs:L31-L36](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-html/src/encode.rs#L31-L36) 理解这条配置链。

编译输出与警告（「html export is under active development」）的具体表现待本地验证。

## 6. 本讲小结

- **格式即库对象**：`Format` 结构体（含格式元素、选项构造器、feature 门、规则注册函数）把导出格式的全部注册信息数据化；`FormatElement` trait 约束格式元素及其 `Options` 类型；导出 crate 导出 `FORMAT` 常量（如 `typst_html::FORMAT`），宿主经 `Library::builder(formats)` 注入——typst-library 不认识任何具体格式。
- **装配线三步**：`LibraryBuilder::build` 先经 `format::module` 把格式元素定义进 `std.format`，再对每个格式调 `register_rules` 追加目标专属规则，最后 `prelude_path` 把 `format.html` 等提升为全局名（继承 feature 门）。
- **规则表按 (元素, 目标) 键控**：`NativeRuleMap` 让同一元素在 paged/html/bundle 目标下各有一套内置 show 规则；基础表来自总装 crate `ROUTINES`（typst-layout + typst-html 两个 register）。
- **HTML 目标是「结构翻译器」而非排版器**：`html_document` 复用同一条 realize 流水线（目标差异在查表时分岔），经 `convert_to_nodes` 翻译成 `HtmlNode` DOM，`finalize_dom` 补文档骨架，`html()` 序列化输出；`html.elem`/`html.frame` 提供手控与局部 paged 逃生舱。
- **分叉是三层机制的配合**：类型层（`compile::<T>` + `Output::target()`）、样式层（`TargetElem::target` 写入样式链，脚本用 `context target()` 读）、规则层（NativeRuleMap 查表）；CLI 的两岔路口在 compile.rs L350/L354。
- **选项双向流动**：文档内 `#set html(pretty: true)` 经 realize 汇入 `DocumentFormatOptions`，导出器用 `options().get::<HtmlFormat>()` 取回，与 CLI 覆盖值合成——配置与代码同样走「Content → 文档 → 导出」的单行道。

## 7. 下一步学习建议

- **u5-l4（typst-bundle）**：本讲反复出现的 `Target::Bundle`、`Feature::Bundle`、`format.bundle.asset` 与 `html_document_for_bundle` 将在那里展开——bundle 目标如何在 html 流水线之上组织多文档与资源，是对本讲「目标分叉」机制的最佳复习材料。
- **对照阅读 paged 侧**：带着本讲的框架重读 [crates/typst-layout/src/lib.rs] 与 `PagedDocument` 的 `impl Output`（typst-layout 侧），比较两条流水线在 realize 入口、introspector、输出结构上的同构与差异。
- **深入 DOM 与序列化**：`typst-html/src/dom.rs` 的 `HtmlElement`/`HtmlFrame`、`encode.rs` 的 `Writer`、`css.rs` 的内联样式解析，适合作为「读懂一个中等规模数据结构 + 序列化器」的练习。
- **官方跟踪 issue**：`HtmlFormat` 文档注释中链接的 typst/typst#5512 是 HTML 导出的进度跟踪，能帮你把源码现状与路线图对上。
