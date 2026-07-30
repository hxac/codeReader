# 导出调用链与 CLI 入口

## 1. 本讲目标

前两篇（u1-l1、u1-l2）我们已经知道 typst-html 是 Typst 的 HTML 导出器，也看清了它 18 个源文件是怎么组织的。但有一个关键问题还没回答：**当你在命令行敲下 `typst compile doc.typ --format html` 时，代码到底是怎么一步步把一个 Typst 文档变成一个 HTML 文件的？** 本讲就专门把这条 **调用链** 走通。读完本讲，你应当能够：

- 说清楚 **`Target::Html`** 这个“编译目标”是如何驱动整个编译过程的：它不是某个 if 分支，而是被注入到 **样式链** 里、并被泛型函数 `typst::compile::<T>` 读出来。
- 理解 **`Output` trait 抽象**：为什么 PDF/SVG/PNG 走一套、HTML 走另一套，`HtmlDocument` 是如何通过实现 `Output` trait 把自己“插”进编译主循环的。
- 准确定位两个核心函数的位置：编译入口 **`html_document`** 与编码入口 **`html`**，并能写出“从 world 到最终 HTML 字符串”的完整函数调用序列。

本讲仍是“只读源码、建立认知”，不修改任何代码。重点是 **把跨 crate 的调用关系串成一条线**。

## 2. 前置知识

本讲假设你读过 u1-l1、u1-l2，熟悉下列概念（不熟悉的也会顺带复习）：

- **导出主线**（u1-l1 已建立）：typst-html 把 Typst 文档内容编译成 `HtmlDocument`（一棵 HTML DOM 树），再编码成 HTML 字符串；两步分别是 `html_document`（编译）和 `html`（编码）。
- **门面与重导出**（u1-l2 已建立）：`html_document` 来自私有模块 `document`，`html`/`HtmlOptions` 来自私有模块 `encode`，它们都通过 `src/lib.rs` 的 `pub use` 暴露到 crate 根，所以外部（如 typst-cli）写 `typst_html::html(...)` 即可调用。
- **Rust 泛型与 trait**：`typst::compile::<T>` 是一个泛型函数，`T` 必须实现 `Output` trait。本讲会看到 `HtmlDocument` 如何实现这个 trait。
- **Typst 的“内省—重排”迭代**：Typst 编译不是一遍完成的，因为像“这里到底是不是第 3 页”这种问题要等排版完才知道，而排版又依赖这些答案。所以编译主循环会 **反复重排直到内省结果稳定**。本讲会指出 HTML 导出同样运行在这个循环里。

> 术语提示：本讲会出现 `Target`（编译目标：Paged / Html / Bundle）、`Output`（导出结果的抽象 trait）、`Engine`（编译引擎，携带 world/library/introspector 等）、`Warned`（包装结果与警告的容器）。它们大多定义在 `typst-library` 或 `typst` 核心里，typst-html 只是 **消费** 它们。

## 3. 本讲源码地图

本讲跨越三个 crate，涉及的文件如下：

| 文件 | 所属 crate | 本讲中的作用 |
| --- | --- | --- |
| [crates/typst-cli/src/compile.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs) | typst-cli | 命令行入口：判定格式、调 `typst::compile`、调 `export_html` 写文件 |
| [crates/typst-cli/src/args.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs) | typst-cli | `OutputFormat` 枚举与 `--format/-f` 参数定义 |
| [crates/typst/src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs) | typst（核心） | `compile::<T>` 泛型入口、`compile_impl` 主循环 |
| [crates/typst-library/src/foundations/target.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/target.rs) | typst-library | `Output` trait、`Target` 枚举的定义 |
| [crates/typst-html/src/dom.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs) | typst-html | `HtmlDocument` 及其 `impl Output` |
| [crates/typst-html/src/document.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs) | typst-html | 编译入口 `html_document` 与主流程 |
| [crates/typst-html/src/encode.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs) | typst-html | 编码入口 `html` 与选项 `HtmlOptions` |

> 提示：本讲会按“调用顺序”而不是“文件顺序”展开。你会发现 typst-html 的入口函数（`html_document`、`html`）其实是由 typst-cli 和 typst 核心 **反向调用** 的——这正是“导出器作为插件被主程序驱动”的典型形态。

## 4. 核心概念与源码讲解

本讲拆成五个最小模块，完全顺着数据流推进：CLI 判定格式 → `Target::Html` 驱动编译 → `HtmlDocument` 实现 `Output` → `html_document` 编译出 DOM → `html` 编码成字符串。

### 4.1 CLI 入口与输出格式判定

#### 4.1.1 概念说明

一切从命令行开始。`typst-cli` 是 Typst 的命令行前端，它把“编译 + 导出”封装在 [crates/typst-cli/src/compile.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs) 里。关键问题是：**typst-cli 怎么知道用户想要 HTML？**

答案有两种触发方式：

1. **显式指定** `--format html`（短选项 `-f html`）。
2. **隐式推断**：若没给 `--format`，但输出路径后缀是 `.html`，就推断为 HTML。

无论哪种，最终都会得到一个枚举值 `OutputFormat::Html`。typst-cli 的命令行参数解析由 `clap` 完成，`--format` 参数的定义在 [crates/typst-cli/src/args.rs:314-316](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L314-L316)，其类型 `OutputFormat` 是个简单枚举（[crates/typst-cli/src/args.rs:591-597](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L591-L597)）：

```rust
pub enum OutputFormat {
    Pdf,
    Png,
    Svg,
    Html,
    Bundle,
}
```

> 注意区分两个名字相近的枚举：typst-cli 这里的 `OutputFormat`（PDF/PNG/SVG/**HTML**/Bundle）描述的是 **CLI 这一侧的文件格式**；而 typst-library 里的 `Target`（Paged/**Html**/Bundle）描述的是 **编译引擎那一侧的编译目标**。本节讲前者，4.2 节讲后者，两者在编译流程里会被关联起来。

#### 4.1.2 核心流程

typst-cli 的编译调用栈是三层函数嵌套：

```
compile(command)              // 入口：建 world、建 config
  └─ compile_once(world, config)   // 编译一次 + 打印诊断 + 打开输出
       └─ compile_and_export(world, config)  // 按格式分支：编译 + 导出
```

`compile_and_export` 是“格式分叉”的关键：它根据 `config.output_format` 走不同分支——PDF/PNG/SVG 一支，HTML 一支，Bundle 一支。对于 HTML，它会 **先编译出 `HtmlDocument`，再调 `export_html` 写文件**。

#### 4.1.3 源码精读

**格式判定逻辑** 在 `CompileConfig::new_impl` 里（[crates/typst-cli/src/compile.rs:111-127](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L111-L127)）：优先用 `--format` 指定值；否则看输出文件后缀，`.html` 对应 `OutputFormat::Html`：

```rust
let output_format = if let Some(specified) = args.format {
    specified
} else if let Some(Output::Path(output)) = &args.output {
    match output.extension() {
        ...
        Some(ext) if ext.eq_ignore_ascii_case("html") => OutputFormat::Html,
        ...
    }
} else {
    OutputFormat::Pdf
};
```

`--format` 参数本身定义在 [crates/typst-cli/src/args.rs:314-316](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L314-L316)，是 `Option<OutputFormat>`（不填就靠推断）。

**格式分叉** 发生在 `compile_and_export`（[crates/typst-cli/src/compile.rs:317-341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L317-L341)）。HTML 分支只有三行核心逻辑（[crates/typst-cli/src/compile.rs:327-334](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L327-L334)）：

```rust
OutputFormat::Html => {
    let Warned { output, warnings } = typst::compile::<HtmlDocument>(world);
    let result = output.and_then(|document| export_html(&document, config));
    Warned { output: result.map(|()| vec![config.output.clone()]), warnings }
}
```

这三行是整条调用链的“总闸”，值得逐行拆：

- 第 1 行 `typst::compile::<HtmlDocument>(world)`：调用 typst 核心的泛型编译函数，**指定产物类型为 `HtmlDocument`**。这一步把“我要 HTML”这个意图通过 **类型参数** 传给了编译引擎（机制见 4.2、4.3）。返回 `Warned<SourceResult<HtmlDocument>>`。
- 第 2 行 `output.and_then(|document| export_html(...))`：编译成功后才执行 `export_html`，把 `HtmlDocument` 变成磁盘上的 `.html` 文件。返回 `SourceResult<()>`。
- 第 3 行把结果包成统一的 `Warned<SourceResult<Vec<Output>>>`，与 PDF/PNG 等分支保持同构。

注意 `typst::compile::<HtmlDocument>` 这一行有个 `use`：文件顶部 [crates/typst-cli/src/compile.rs:16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L16) `use typst_html::{HtmlDocument, HtmlOptions};`——这正是 typst-cli 依赖 typst-html 的直接证据。

#### 4.1.4 代码实践

> 实践目标：亲手定位 HTML 导出的分叉点，并验证“类型参数 `HtmlDocument` 就是 HTML 意图的载体”。

操作步骤（源码阅读型实践）：

1. 打开 [crates/typst-cli/src/compile.rs:317](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L317) 的 `compile_and_export`，确认它用 `match config.output_format` 分三支。
2. 对比 HTML 分支（[L327](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L327)）和 Paged 分支（[L322](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L322)）：前者写 `typst::compile::<HtmlDocument>`，后者写 `typst::compile::<PagedDocument>`。体会“同一函数、不同类型参数 → 不同产物”的设计。
3. 翻到 [crates/typst-cli/src/compile.rs:16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L16)，确认 typst-cli 确实 `use typst_html::{HtmlDocument, HtmlOptions};`。

需要观察的现象：

- HTML 与 Paged 两个分支结构几乎对称，差别只在“编译的产物类型”和“导出函数”。
- `HtmlDocument` 这个类型名同时出现在 `use`、`compile::<...>`、`export_html` 形参里，它是贯穿 HTML 分支的“主线类型”。

预期结果：

- 能指出：触发 HTML 导出的分支在 `compile_and_export` 的 `OutputFormat::Html` 分支；意图通过 `typst::compile::<HtmlDocument>` 的类型参数传入。

#### 4.1.5 小练习与答案

**练习 1**：如果不加 `--format`，但用 `typst compile doc.typ -o out.html`，会导出 HTML 吗？为什么？

> **参考答案**：会。因为 [compile.rs:118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L118) 的后缀推断分支会把 `.html` 映射成 `OutputFormat::Html`。`--format` 只在显式指定时优先生效。

**练习 2**：`compile_and_export` 里 HTML 分支为什么不直接写文件，而要先 `typst::compile` 再 `export_html` 两步走？

> **参考答案**：因为“编译”（把 Typst 源码变成结构化的 `HtmlDocument`）和“导出”（把 `HtmlDocument` 序列化成字符串并写盘）是两个关注点。分成两步后，编译结果还可以被复用（例如 watch 模式下同一次编译结果可能既要写文件又要推给 http-server），也便于独立测试。

---

### 4.2 `Target::Html` 与 `typst::compile` 的泛型驱动

#### 4.2.1 概念说明

上一节看到 `typst::compile::<HtmlDocument>(world)` 用类型参数表达了“我要 HTML”。那么 **编译引擎内部是怎么从这个类型参数得知该走 HTML 流程的？** 这涉及两个核心抽象，都定义在 `typst-library` 里：

- **`Target` 枚举**（[crates/typst-library/src/foundations/target.rs:67-76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/target.rs#L67-L76)）：编译目标，有三个变体 `Paged`（PDF/PNG/SVG）、`Html`、`Bundle`。
- **`Output` trait**（[crates/typst-library/src/foundations/target.rs:13-30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/target.rs#L13-L30)）：所有导出产物（`PagedDocument`、`HtmlDocument`、`Bundle`）都要实现的 trait，它告诉编译引擎三件事——产物对应的 `Target`、如何 `create`（创建产物）、如何拿到 `introspector`（内省器）。

关键洞察：`Target::Html` **不是一个简单的开关**。编译引擎会把它 **作为一个样式值** 注入到全局样式链里（`TargetElem::target.set(Target::Html)`）。这样，文档里的每一个元素在 realize（物化）时都能查到“当前是 HTML 目标”，从而选择 HTML 专属的 show 规则（由 `typst_html::register` 注册，详见 u3-l5）。这就是 typst-html 能“接管”映射逻辑的根本机制。

#### 4.2.2 核心流程

`typst::compile::<T>` 的内部 `compile_impl` 大致流程：

```
typst::compile::<HtmlDocument>(world)              返回 Warned<SourceResult<HtmlDocument>>
  └─ compile_impl::<HtmlDocument>(world, traced, sink)
       ① 读 T::target() → Target::Html            （由 Output trait 提供）
       ② 按目标做特性检查（HTML 未稳定会警告/报错）
       ③ 把 Target::Html 注入样式链：TargetElem::target.set(Html)
       ④ eval 主源文件 → Content
       ⑤ 循环直到内省稳定：
            T::create(&mut engine, &content, styles)  ← 这里才真正进入 typst-html
            constraint.validate(document.introspector())
```

注意第 ⑤ 步的循环：Typst 的内省（如“这个标题是第几个”“链接指向哪页”）需要排版结果才能确定，而排版又依赖内省，所以引擎会 **反复调用 `T::create` 直到内省结果不再变化**（最多若干轮）。HTML 导出同样运行在这个循环里——每一轮都会重新构建 `HtmlDocument`。

#### 4.2.3 源码精读

`compile` 泛型入口在 [crates/typst/src/lib.rs:74-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L74-L82)，约束 `T: Output`：

```rust
pub fn compile<T>(world: &dyn World) -> Warned<SourceResult<T>>
where
    T: Output,
{
    let mut sink = Sink::new();
    let output = compile_impl::<T>(world.track(), Traced::default().track(), &mut sink)
        .map_err(deduplicate);
    Warned { output, warnings: sink.warnings() }
}
```

真正的逻辑在 `compile_impl`（[crates/typst/src/lib.rs:99-194](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L99-L194)）。三个与本讲最相关的片段：

**① 读出目标并做特性检查**（[crates/typst/src/lib.rs:105-109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L105-L109)）——这里通过 `T::target()` 把“类型参数 `T`”翻译成“运行时的 `Target` 值”：

```rust
match T::target() {
    Target::Paged => {}
    Target::Html => warn_or_error_for_html(&library.features, sink)?,
    Target::Bundle => warn_or_error_for_bundle(&library.features, sink)?,
}
```

> 对 `HtmlDocument` 而言，`T::target()` 返回 `Target::Html`（见 4.3 节）。`warn_or_error_for_html` 会在 HTML 特性未启用时报错或警告。

**② 把目标注入样式链**（[crates/typst/src/lib.rs:111-113](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L111-L113)）——这是 `Target::Html` 真正“流进”文档的瞬间：

```rust
let base = StyleChain::new(&library.styles);
let target = TargetElem::target.set(T::target()).wrap();
let styles = base.chain(&target);
```

`TargetElem::target.set(Target::Html)` 把目标写进一个样式，再 `chain` 到全局样式链上。后续所有元素在物化时读到的“当前 target”就是 `Html`。`TargetElem` 是一个仅为承载这个样式字段而存在的内部元素（[crates/typst-library/src/foundations/target.rs:87-91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/target.rs#L87-L91)）。

**⑤ 内省收敛循环**（[crates/typst/src/lib.rs:138-185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L138-L185)），核心两行（[L156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L156)、[L158](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L158)）：

```rust
document = T::create(&mut engine, &content, styles)?;

if timed!("check stabilized", constraint.validate(document.introspector())) {
    sink.extend_from_sink(subsink);
    break;
}
```

`T::create` 就是 4.3 节要讲的 `HtmlDocument::create`——它最终调用 typst-html 的 `html_document`。`constraint.validate(...)` 判断这一轮的内省结果和上一轮是否一致，一致才跳出循环。

#### 4.2.4 代码实践

> 实践目标：看清“类型参数 `T` 如何被翻译成运行时的 `Target` 值并注入样式”。

操作步骤（源码阅读型实践）：

1. 打开 [crates/typst/src/lib.rs:99](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L99) 的 `compile_impl`，找到 [L105](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L105) 的 `T::target()`，确认它返回的是 `Target` 枚举值。
2. 顺着 [L112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L112) 看 `TargetElem::target.set(T::target())`，理解“目标”被包装成样式。
3. 打开 [crates/typst-library/src/foundations/target.rs:67-76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/target.rs#L67-L76)，确认 `Target` 只有 `Paged`/`Html`/`Bundle` 三个变体。

需要观察的现象：

- `Target::Html` 并没有出现在某个 `if target == Html { ... }` 的硬编码分支里来改变编译路径；它是被 **注入样式**、由下游的 show 规则读取的。

预期结果：

- 能解释：`Target::Html` 通过 `TargetElem::target.set(...)` 进入样式链，从而让文档元素的物化走 HTML 路径；编译主循环对 Paged/Html/Bundle 是 **统一** 的，差异由“产物类型 T”和“注入的 target 样式”共同表达。

#### 4.2.5 小练习与答案

**练习 1**：`Target` 枚举有 `Paged`/`Html`/`Bundle` 三个变体，而 typst-cli 的 `OutputFormat` 有 `Pdf`/`Png`/`Svg`/`Html`/`Bundle` 五个。为什么前者的粒度更粗（`Paged` 合并了 PDF/PNG/SVG）？

> **参考答案**：因为 PDF、PNG、SVG 三者都基于 **同一种产物** `PagedDocument`（先分页排版，再各自序列化），它们在“编译目标”层面没有区别，只是导出时的编码不同，所以共用 `Target::Paged`。而 HTML 产物（`HtmlDocument`）和 Bundle 的编译流程与 Paged 根本不同，因此各自有独立的 `Target`。

**练习 2**：假如把 `compile_impl` 里 [L112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L112) 的 `TargetElem::target.set(T::target())` 删掉，HTML 导出还会正常工作吗？

> **参考答案**：很可能不会正确工作。删掉后，文档元素读到的“当前 target”会退回默认值 `Target::Paged`，于是物化时会走 Paged 的 show 规则而不是 typst-html 注册的 HTML 规则，`HtmlDocument` 里就得不到正确的 HTML 元素。这印证了 target 是“通过样式链传播”而非“靠 if 分支选择”的。

---

### 4.3 `HtmlDocument` 如何实现 `Output` trait

#### 4.3.1 概念说明

上一节反复提到 `T::target()` 和 `T::create`——它们都来自 `Output` trait。为了让 `typst::compile::<HtmlDocument>` 能工作，`HtmlDocument` 必须实现这个 trait。这一节就看 typst-html 是怎么实现它的。

`Output` trait 定义在 [crates/typst-library/src/foundations/target.rs:13-30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/target.rs#L13-L30)，要求三个关联项：

- `fn target() -> Target`：声明产物对应的编译目标（`HtmlDocument` 返回 `Target::Html`）。
- `fn create(engine, content, styles) -> SourceResult<Self>`：**创建产物的工厂方法**——编译引擎每轮循环就调它一次。
- `fn introspector(&self) -> &dyn Introspector`：提供内省器，供收敛判断和后续查询使用。

`HtmlDocument` 的实现极其简洁：`create` 直接转交给 typst-html 的编译入口 `html_document`。也就是说，`Output` trait 在这里扮演的是 **适配器** 的角色——把 typst-html 的函数签名“翻译”成编译引擎期望的 trait 方法。

#### 4.3.2 核心流程

```
compile_impl 的循环每轮调用：
  T::create(&mut engine, &content, styles)   ← T = HtmlDocument
       │  (Output::create 的实现)
       └─ crate::html_document(engine, content, styles)   ← 进入 typst-html（4.4 节）
              └─ 返回 HtmlDocument
```

#### 4.3.3 源码精读

`HtmlDocument` 的 `impl Output` 在 [crates/typst-html/src/dom.rs:81-97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L81-L97)，完整且很短：

```rust
impl Output for HtmlDocument {
    fn introspector(&self) -> &dyn Introspector {
        self.introspector.as_ref()
    }

    fn target() -> Target {
        Target::Html
    }

    fn create(
        engine: &mut Engine,
        content: &Content,
        styles: StyleChain,
    ) -> SourceResult<Self> {
        crate::html_document(engine, content, styles)
    }
}
```

逐个方法说明：

- `target()` 返回 `Target::Html`（[L86-88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L86-L88)）：这正是 4.2 节 `compile_impl` 里 `T::target()` 读到的值。**一行代码就把 `HtmlDocument` 与 `Target::Html` 绑定起来**，是整个 HTML 流程的“身份声明”。
- `create()`（[L90-96](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L90-L96)）：直接 `crate::html_document(engine, content, styles)`。注意它把 `engine`、`content`、`styles` 原样转发——所有“怎么把内容变成 HTML”的复杂逻辑都在 `html_document` 里（4.4 节）。
- `introspector()`（[L82-84](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L82-L84)）：返回 `HtmlDocument` 内部持有的 `HtmlIntrospector`（[crates/typst-html/src/dom.rs:29](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L29) 字段 `introspector: Arc<HtmlIntrospector>`），供 4.2 节的收敛判断 `constraint.validate(...)` 使用。

`HtmlDocument` 本身的结构（[crates/typst-html/src/dom.rs:25-30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L25-L30)）也值得一看，它有三块：

```rust
pub struct HtmlDocument {
    output: HtmlOutput,
    info: DocumentInfo,
    introspector: Arc<HtmlIntrospector>,
}
```

- `output`：真正的 DOM 树（`HtmlOutput`，u2-l1 详解）。
- `info`：文档元信息（标题、作者等，用于生成 `<head>`）。
- `introspector`：内省器，用于查询/跳转（u5-l3 详解）。

> 小结：`impl Output for HtmlDocument` 是 typst-html 与 typst 核心之间的 **唯一耦合点之一**。正因为有它，typst 核心不需要 `if target == Html`，只要 `T::create()` 就能驱动 HTML 编译。这是典型的“依赖倒置”——核心定义接口（trait），导出器实现接口。

#### 4.3.4 代码实践

> 实践目标：确认 `Output::create` 只是 `html_document` 的薄包装，并理解 trait 在这里起的“适配器”作用。

操作步骤（源码阅读型实践）：

1. 打开 [crates/typst-html/src/dom.rs:81](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L81) 的 `impl Output for HtmlDocument`，确认 `create` 的函数体只有一行 `crate::html_document(...)`。
2. 对照 [crates/typst-library/src/foundations/target.rs:19-26](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/target.rs#L19-L26) 的 trait 声明，确认 `create` 的签名（`engine`/`content`/`styles`）与 `html_document` 完全对得上，所以才能一行转发。
3. 跟着 `use typst_library::foundations::{... Output ...}`（[crates/typst-html/src/dom.rs:7](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L7)）确认 `Output` 来自 `typst-library`。

需要观察的现象：

- `create` 没有任何 HTML 专属逻辑，纯粹是“把参数原样交给 `html_document`”。

预期结果：

- 能说出：`HtmlDocument` 通过 `impl Output` 把 `target() = Html` 和 `create = html_document` 两件事告诉编译引擎；真正的编译逻辑在 `html_document`，trait 只是适配层。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `create` 是关联函数（`fn create(...)`，无 `self`）而不是方法？

> **参考答案**：因为 `create` 的职责是 **构造** 一个全新的 `HtmlDocument`，调用时产物还不存在，自然没有 `self`。编译引擎在循环里每轮都通过 `T::create(...)` 重新构造一个 `HtmlDocument`（[lib.rs:156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L156)），它是“工厂方法”。

**练习 2**：`HtmlDocument` 还实现了 `impl Document for HtmlDocument`（[dom.rs:75-79](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L75-L79)），它的 `info()` 返回文档元信息。这与 `Output::introspector()` 有什么分工？

> **参考答案**：`Document::info()` 提供的是 **文档级元数据**（标题/作者/关键词，用于生成 `<head>` 和查询 `sys.inputs` 之类），而 `Output::introspector()` 提供的是 **结构内省能力**（某个元素在第几页、链接目标在哪）。两者职责不同，编译引擎分别使用。

---

### 4.4 `html_document`：文档编译入口

#### 4.4.1 概念说明

`Output::create` 把控制权交给了 [crates/typst-html/src/document.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs) 里的 **`html_document`**——这是 typst-html 真正的“编译主入口”。它要做的事情可以概括为一句话：**把 Typst 的 `Content` 变成一棵 HTML DOM 树（`HtmlDocument`）**。

注意区分两个阶段：

- **编译阶段**（本节，`html_document`）：`Content` → `HtmlDocument`（内存中的 DOM 树）。
- **编码阶段**（4.5 节，`html`）：`HtmlDocument` → `String`（HTML 文本）。

`html_document` 内部又分两层：

1. 一个公开的 `html_document` 薄壳，负责把 `Engine` 里的字段拆解成可哈希的 `Tracked` 参数。
2. 一个被 `comemo::memoize` 缓存的 `html_document_impl`，真正的活儿在 `html_document_common` 里。

> 为什么要把参数逐个拆开传给一个 `_impl` 函数？因为 `comemo` 的缓存要求函数参数都可哈希，而 `Engine` 是个包含不可哈希字段的聚合体，所以必须把它拆成若干 `Tracked`/`TrackedMut` 句柄分别传入。这个设计取舍在 u6-l4 专门讨论，本讲只需知道“有这么一层缓存壳”即可。

#### 4.4.2 核心流程

`html_document` 的完整处理流水线（[crates/typst-html/src/document.rs:128-218](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L128-L218)）：

```
html_document(engine, content, styles)
  └─ html_document_impl(world, library, introspector, ..., content, styles)   [memoize 缓存]
       └─ html_document_common(...)   共享实现
            ① 用 content/styles 组装 Engine；填充 DocumentInfo
            ② realize：物化文档 → children（一堆 Content）
            ③ convert_to_nodes：children → Vec<HtmlNode>
            ④ finalize_dom：决定是否包 <html>/<body>、生成 <head>、附加脚注 → HtmlOutput
            ⑤ resolve_inline_styles：把 css 字段写成内联 style 属性
            ⑥ 若有方程：往 <head> 注入 <style>EQUATION_CSS_STYLES</style>
            ⑦ HtmlDocument::new(output, info)
       └─ （回到 html_document_impl）create_link_anchors + set_anchors
       → HtmlDocument
```

本讲只要求你抓住“这条流水线从 ② 到 ⑦ 把内容变成了 DOM 树”，每一格的细节都对应后续讲义（realize/convert/finalize 在第三单元，CSS 在第四单元，链接锚点在 u5-l4，方程 CSS 在 u5-l5）。

#### 4.4.3 源码精读

**公开入口** `html_document` 在 [crates/typst-html/src/document.rs:25-40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L25-L40)，它把 `Engine` 的字段拆成 `Tracked` 句柄转发：

```rust
#[typst_macros::time(name = "html document")]
pub fn html_document(
    engine: &mut Engine,
    content: &Content,
    styles: StyleChain,
) -> SourceResult<HtmlDocument> {
    html_document_impl(
        engine.world, engine.library, engine.introspector.into_raw(),
        engine.traced, TrackedMut::reborrow_mut(&mut engine.sink),
        engine.route.track(), content, styles,
    )
}
```

> `#[typst_macros::time(name = "html document")]` 给这一步打上计时标签，能在 `--timings` 输出里看到“html document”耗时。

**被缓存的实现** `html_document_impl` 在 [crates/typst-html/src/document.rs:43-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L43-L73)，关键两步：先调 `html_document_common` 得到 `document`，再做链接锚点收尾（[L67-70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L67-L70)）：

```rust
let mut document = html_document_common(world, library, introspector, ...)?;

// Assigns HTML fragment IDs to linked-to elements.
let targets = document.introspector().link_targets();
let anchors = crate::link::create_link_anchors(&mut document, &targets);
document.introspector_mut().set_anchors(anchors);

Ok(document)
```

> 为什么锚点分配要放在 `html_document_common` **之外**？因为它依赖 `introspector().link_targets()`（内省结果），而 `html_document_common` 又依赖内省收敛——把锚点单拎出来便于管理这个先后顺序（详见 u5-l4、u6-l4）。

**共享实现** `html_document_common` 在 [crates/typst-html/src/document.rs:128-218](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L128-L218)，对应流水线的 ② ~ ⑦。本讲引用其中两段作为“骨架证据”：

②③④ 物化 + 转换 + 定型（[L163-186](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L163-L186)）：

```rust
let children = (engine.library.routines.realize)(
    RealizationKind::Document { info: &mut info },
    &mut engine, &mut locator, &arenas, content, styles,
)?;

let nodes = crate::convert::convert_to_nodes(
    &mut engine, &mut locator, children.iter().copied(),
    ConversionLevel::Block, Whitespace::Normal,
)?;

let mut output = finalize_dom(
    &mut engine, nodes, &info, footnote_locator,
    StyleChain::new(&Styles::root(&children, styles)),
)?;
```

⑦ 组装并返回（[L217](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L217)）：

```rust
Ok(HtmlDocument::new(output, info))
```

> 注意 `RealizationKind::Document { info: &mut info }`（[L164](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L164)）：这里把 `info`（标题/作者等）以可变引用传给 `realize`，因为物化过程中元素会往里写元信息。物化走哪套 show 规则，则由 4.2 节注入的 `Target::Html` 样式决定——本节不展开（u3-l1、u3-l5 详解）。

#### 4.4.4 代码实践

> 实践目标：把 `html_document` 的三层结构（公开壳 / 缓存壳 / 共享实现）和它的流水线对上号。

操作步骤（源码阅读型实践）：

1. 打开 [crates/typst-html/src/document.rs:25](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L25) 的 `html_document`，确认它把 `Engine` 拆成 7 个参数转发给 `html_document_impl`。
2. 看 [L43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L43) 的 `#[comemo::memoize]` 标注，理解 `html_document_impl` 是被缓存的（输入相同时直接返回缓存结果）。
3. 顺着 [L128](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L128) 的 `html_document_common`，依次找到 `realize`（[L163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L163)）、`convert_to_nodes`（[L172](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L172)）、`finalize_dom`（[L180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L180)）、`HtmlDocument::new`（[L217](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L217)）四个关键调用点。

需要观察的现象：

- 从 `html_document` 到 `HtmlDocument::new` 是一条单向流水线：`Content` → realize → `Vec<HtmlNode>` → `HtmlOutput` → `HtmlDocument`，类型逐步“具体化”。
- `html_document_impl` 与 `html_document_common` 是分开的两个函数：前者管缓存与锚点收尾，后者管纯粹的 DOM 构建。

预期结果：

- 能复述 `html_document` 的三层结构和“realize → convert_to_nodes → finalize_dom → new”的主干顺序，并知道每一步返回的中间类型（`children`/`nodes`/`output`/`HtmlDocument`）。

#### 4.4.5 小练习与答案

**练习 1**：`html_document_impl` 为什么要用 `#[comemo::memoize]`，而 `html_document_common` 不用？

> **参考答案**：缓存要起作用，函数参数必须“可哈希且完整描述输出”。`html_document_impl` 的参数都是 `Tracked`/`TrackedMut` 句柄和 `Content`/`StyleChain`，可以参与缓存键；而 `html_document_common` 里还混着锚点收尾等依赖内省结果、不易哈希的副作用，所以把“可缓存的纯计算”和“不可缓存的收尾”拆开——前者 memoize，后者不 memoize（u6-l4 详解）。

**练习 2**：`finalize_dom`（[L259](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L259)）会判断 `needs_body`。结合 u1-l1 提到的“若用户自建 `<html>`/`<body>` 则 Typst 省略自动生成的外层标签”，能猜到这个布尔值控制什么吗？

> **参考答案**：`needs_body` 控制 typst-html 是否要 **自动** 包一层 `<body>`（以及 `<html>`/`<head>`）。如果用户已经用 `html.elem("body")` 手写了 `<body>`，`finalize_dom` 就会把 `needs_body` 置为 `false`，跳过自动包裹（但仍可能报“脚注不可用”的错，详见 u3-l2）。

---

### 4.5 `html` 与 `HtmlOptions`：编码为字符串

#### 4.5.1 概念说明

到 4.4 节为止，我们手里有了一棵 `HtmlDocument`（内存中的 DOM 树）。但它还不是网页——浏览器需要的是 HTML **文本**。把 DOM 树序列化成 HTML 字符串，就是 **`html`** 函数的职责（注意小写，它是函数名，不是模块名）。

这一步在 typst-cli 里由 `export_html` 触发。`export_html` 先用配置构造一个 **`HtmlOptions`**（目前只有一个 `pretty` 选项，控制是否美化输出），再调 `typst_html::html(document, &options)` 拿到字符串，最后写盘。

`HtmlOptions` 非常小，但它的存在体现了 typst-html 的一个设计原则：**编译（`html_document`）与编码（`html`）分离，且编码是可配置的**。同一个 `HtmlDocument`，既可以用 `pretty: true` 编码成带缩进的易读 HTML，也可以用 `pretty: false` 编码成紧凑的单行 HTML，而无需重新编译。

#### 4.5.2 核心流程

```
export_html(&HtmlDocument, &CompileConfig)        compile.rs:344
  ├─ HtmlOptions { pretty: config.pretty }        构造选项
  ├─ typst_html::html(document, &options)         encode.rs:23
  │    └─ html_impl(Writer, root)                 encode.rs:43
  │         ├─ 写入 "<!DOCTYPE html>"
  │         ├─ write_element(root)                递归遍历整棵 DOM 树
  │         └─ 返回 String
  └─ config.output.write(html.as_bytes())         写入 .html 文件
```

#### 4.5.3 源码精读

**`HtmlOptions`** 定义在 [crates/typst-html/src/encode.rs:16-20](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L16-L20)，只有一个字段：

```rust
#[derive(Debug, Default, Clone, Eq, PartialEq, Hash)]
pub struct HtmlOptions {
    /// Whether to format the HTML in a human-readable way.
    pub pretty: bool,
}
```

typst-cli 里构造它的辅助函数在 [crates/typst-cli/src/compile.rs:595-597](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L595-L597)（bundle 路径用），HTML 分支则直接内联构造（[crates/typst-cli/src/compile.rs:345](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L345)）。

**`export_html`** 在 [crates/typst-cli/src/compile.rs:344-357](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L344-L357)，是编码 + 写盘的“收口”：

```rust
fn export_html(document: &HtmlDocument, config: &CompileConfig) -> SourceResult<()> {
    let options = HtmlOptions { pretty: config.pretty };
    let html = typst_html::html(document, &options)?;
    let result = config.output.write(html.as_bytes());

    #[cfg(feature = "http-server")]
    if let Some(server) = &config.server {
        server.set_html(html);
    }

    result
        .map_err(|err| eco_format!("failed to write HTML file ({err})"))
        .at(Span::detached())
}
```

值得注意：除了写文件，在启用 `http-server` 特性时（`typst watch` 到 HTML），同一份 `html` 字符串还会推给本地服务器（`server.set_html(html)`），实现浏览器实时刷新。这也是 4.1 节练习提到的“编译与导出分离”的好处之一。

**`html`** 在 [crates/typst-html/src/encode.rs:23-27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L23-L27)：

```rust
pub fn html(document: &HtmlDocument, options: &HtmlOptions) -> SourceResult<String> {
    let link_resolver = LateLinkResolver::new(None, document.introspector().as_ref());
    let w = Writer::new(link_resolver.track(), options.pretty);
    html_impl(w, document.root())
}
```

它构造一个 `Writer`（输出缓冲 + 缩进级别 + 链接解析器 + pretty 开关，[encode.rs:54-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L54-L64)），再交给共享实现 `html_impl`。

**`html_impl`** 在 [crates/typst-html/src/encode.rs:43-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L43-L51)，做三件事：写 `<!DOCTYPE html>`、递归编码根元素、返回字符串：

```rust
fn html_impl(mut w: Writer, root: &HtmlElement) -> SourceResult<String> {
    w.buf.push_str("<!DOCTYPE html>");
    write_indent(&mut w);
    write_element(&mut w, root)?;
    if w.pretty {
        w.buf.push('\n');
    }
    Ok(w.buf)
}
```

> `write_element`（[encode.rs:112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L112)）会递归地把整棵 DOM 树（标签、属性、文本、子元素、frame/SVG）序列化进 `w.buf`。它的细节（void/raw 元素、转义、pretty 缩进）是 u5-l1 的主题，本讲只要知道“它是把树变字符串的递归引擎”即可。

至此，从命令行到字符串的链路就完整闭合了：`html_impl` 返回的 `String` 被 `export_html` 写进 `.html` 文件，用户得到最终产物。

#### 4.5.4 代码实践

> 实践目标：亲手让 typst 编译出一个 HTML 文件，并对比 `--pretty` 开关的效果，直观感受“同一 `HtmlDocument`、不同 `HtmlOptions`”。

操作步骤（可运行实践，待本地验证）：

1. 在 typst 仓库根目录准备一个最小 Typst 文件 `hello.typ`：
   ```typ
   #set document(title: "Demo", author: "Typst")
   Hello, *HTML* export!
   ```
2. 用工作区里的 typst-cli 编译（在仓库根目录执行），先不开美化：
   ```bash
   cargo run -p typst-cli -- compile hello.typ --format html -o hello.html
   ```
3. 再开美化，输出到另一个文件：
   ```bash
   cargo run -p typst-cli -- compile hello.typ --format html --pretty -o hello_pretty.html
   ```
4. 打开两个文件对比：观察 `<!DOCTYPE html>` 是否都存在、`hello_pretty.html` 是否有换行和缩进。

需要观察的现象：

- 两份输出都应以 `<!DOCTYPE html>` 开头（来自 [encode.rs:44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L44)）。
- `--pretty` 版本应在块级元素之间有换行和两个空格的缩进（来自 `write_indent`，[encode.rs:79-86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L79-L86)），非 pretty 版本则是紧凑的单行。
- 文档里应能看到自动生成的 `<html>`/`<head>`（含 `<title>Demo</title>`、`authors` meta）/`<body>`，以及 `_HTML_` 被包成 `<strong>HTML</strong>`。

预期结果：

- 直观理解 `HtmlOptions { pretty }` 的作用：**编译结果（DOM 树）不变，仅编码格式不同**。若本地无法编译，可改为阅读型实践——直接对照 `html_impl` 与 `write_indent` 推断两种输出的差异，并标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`html` 函数签名是 `fn html(document: &HtmlDocument, options: &HtmlOptions) -> SourceResult<String>`，它不接收 `Engine`。这说明编码阶段和编译阶段在依赖上有什么区别？

> **参考答案**：编码阶段 **不再需要** 编译引擎（`Engine`/`World`/introspector 等），它只依赖已经构建好的 `HtmlDocument`。也就是说，编码是“纯函数式”的：同一棵 DOM 树 + 同一 `HtmlOptions` → 同一字符串。编译阶段才需要引擎去读源码、跑物化、做内省。这种分离让编码可以独立测试、独立缓存。

**练习 2**：为什么 `<!DOCTYPE html>` 写在 `html_impl` 里（[encode.rs:44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L44)），而不是作为 DOM 树里的一个节点？

> **参考答案**：因为 `<!DOCTYPE html>` 是 HTML 文档的 **文档类型声明**，不是元素，不属于 DOM 元素树。DOM 树的根是 `<html>` 元素（由 `finalize_dom` 生成），而 DOCTYPE 是序列化时附加在最前面的固定前缀，所以由编码器（`html_impl`）写入最合适。

---

## 5. 综合实践

把本讲知识串成一张图。**任务：亲手追踪并写出“从 world 到最终 HTML 字符串”的完整函数调用序列，标注每步的返回类型，再用一次真实编译验证关键节点。**

操作步骤：

1. **源码追踪（必做）**：以本讲四个关键文件为线索，把下面的“骨架”补全成完整调用链。每一步都要写明：所在文件:行、返回类型。

   起点提示（在 [crates/typst-cli/src/compile.rs:327](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L327) 的 HTML 分支）：
   ```
   typst::compile::<HtmlDocument>(world)            → Warned<SourceResult<HtmlDocument>>
     └─ compile_impl::<HtmlDocument>(...)             → SourceResult<HtmlDocument>
          └─ T::create(&mut engine, content, styles)  → SourceResult<HtmlDocument>
               └─ ???                                 → ???     （4.3、4.4 节）
   ... 接着补 export_html → html → html_impl ...
   ```

2. **运行验证（待本地验证）**：按 4.5.4 的步骤真实编译 `hello.typ`，确认产物以 `<!DOCTYPE html>` 开头、含 `<head>`/`<title>`/`<body>`。这验证了 `finalize_dom` 生成骨架 + `html_impl` 写 DOCTYPE 的两个节点。

下面给出一份 **参考答案**（建议你先自己补全，再对照）：

```
用户: typst compile doc.typ --format html
 │
 │ compile.rs:327  OutputFormat::Html 分支
 ├─ typst::compile::<HtmlDocument>(world)                 [typst/lib.rs:74]
 │     → Warned<SourceResult<HtmlDocument>>
 │   └─ compile_impl::<HtmlDocument>(world, traced, sink)  [typst/lib.rs:99]
 │        → SourceResult<HtmlDocument>
 │      ├─ T::target() → Target::Html                      [dom.rs:86]   → Target
 │      ├─ TargetElem::target.set(Html) 注入样式链          [lib.rs:112]
 │      ├─ eval 主源 → Content                             [lib.rs:123]  → Content
 │      └─ 循环 T::create(&mut engine, content, styles)    [lib.rs:156]
 │           → SourceResult<HtmlDocument>
 │         └─ HtmlDocument::create                         [dom.rs:90]
 │              └─ html_document(engine, content, styles)  [document.rs:25]
 │                   → SourceResult<HtmlDocument>
 │                 └─ html_document_impl(...) [memoize]    [document.rs:43]
 │                      ├─ html_document_common(...)        [document.rs:128]
 │                      │    ├─ realize                     [document.rs:163] → children
 │                      │    ├─ convert_to_nodes            [document.rs:172] → Vec<HtmlNode>
 │                      │    ├─ finalize_dom                [document.rs:180] → HtmlOutput
 │                      │    ├─ resolve_inline_styles       [document.rs:190]
 │                      │    └─ HtmlDocument::new           [document.rs:217] → HtmlDocument
 │                      └─ create_link_anchors/set_anchors  [document.rs:68]
 │
 │ compile.rs:329  output.and_then(export_html)
 ├─ export_html(&document, config)                         [compile.rs:344]
 │      → SourceResult<()>
 │   ├─ HtmlOptions { pretty: config.pretty }              [compile.rs:345] → HtmlOptions
 │   ├─ typst_html::html(document, &options)               [encode.rs:23]  → SourceResult<String>
 │   │    └─ html_impl(Writer, root)                        [encode.rs:43] → String
 │   │         ├─ "<!DOCTYPE html>"                         [encode.rs:44]
 │   │         └─ write_element(root)                       [encode.rs:46] （递归编码 DOM）
 │   └─ config.output.write(html.as_bytes())                [compile.rs:347] → 写入 .html
 │
 └─ 最终产物：磁盘上的 .html 文件（字符串）
```

要点自检：

- 编译引擎对 Paged/Html/Bundle 是 **统一循环**，靠类型参数 `T` 区分；`HtmlDocument` 通过 `impl Output`（`target()=Html`、`create=html_document`）接入。
- `html_document` 是 **编译**（Content→DOM），`html` 是 **编码**（DOM→String），二者由 `HtmlOptions` 解耦。
- 整条链跨 3 个 crate：typst-cli（驱动）、typst（核心循环）、typst-html（编译+编码）。

> 说明：源码追踪部分可纯阅读完成；运行验证部分标注「待本地验证」，需在仓库根目录用 `cargo run -p typst-cli` 实际执行。

## 6. 本讲小结

- typst-cli 在 [compile.rs:327-334](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L327-L334) 的 `OutputFormat::Html` 分支触发导出：先 `typst::compile::<HtmlDocument>(world)` 编译，再 `export_html` 写文件；格式由 `--format/-f` 或 `.html` 后缀判定（[compile.rs:111-127](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L111-L127)）。
- **`Target::Html` 驱动编译** 靠的是“把目标注入样式链”（`TargetElem::target.set(Target::Html)`，[typst/lib.rs:112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L112)），而非 if 分支；编译主循环对三种目标统一，靠 `T::target()`/`T::create()` 区分，并反复重排直到内省收敛（[lib.rs:138-185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L138-L185)）。
- **`HtmlDocument` 实现 `Output` trait**（[dom.rs:81-97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L81-L97)）是 typst-html 与 typst 核心的耦合点：`target()→Target::Html`、`create()→html_document`、`introspector()` 提供内省器。
- **编译入口 `html_document`**（[document.rs:25](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L25)）走三层（公开壳 / `memoize` 缓存壳 / `html_document_common` 共享实现），主干是 realize → convert_to_nodes → finalize_dom → `HtmlDocument::new`，把 `Content` 变成 DOM 树。
- **编码入口 `html`**（[encode.rs:23](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L23)）用 `HtmlOptions { pretty }`（[encode.rs:16-20](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L16-L20)）配置，经 `html_impl` 写 `<!DOCTYPE html>` + 递归 `write_element`，返回 `String`；`export_html`（[compile.rs:344](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L344)）把它写盘。
- 编译（DOM 构建）与编码（字符串化）被刻意分离：编码不依赖 `Engine`，同一 `HtmlDocument` 可用不同 `HtmlOptions` 编码出不同格式的 HTML。

## 7. 下一步学习建议

本讲把“从命令行到字符串”的宏观链路走通了，接下来建议：

- 想直接上手用户侧能力，请阅读 **u1-l4《用户侧 API：html.elem 与 html.frame》**，学完你就能在 Typst 脚本里手写 HTML 元素、控制 DOM 骨架（呼应本讲 `finalize_dom` 的 `needs_body` 判定）。
- 想深入理解本讲里一笔带过的 **`html_document_common` 流水线**（realize/convert/finalize），进入第三单元 **u3-l1《文档编译主链路 html_document》**，届时会逐段拆解 4.4 节的每一步。
- 想理解 **`Target::Html` 如何让元素走 HTML show 规则**（本讲只点了“注入样式链”），阅读 **u3-l5《内建 show 规则注册机制》**，看 `typst_html::register` 如何把 `strong→<strong>` 等映射挂上去。
- 想看 **编码细节**（void/raw 元素、转义、pretty 缩进），阅读 **u5-l1《DOM 到 HTML 字符串的编码》**，它是对本讲 `html_impl`/`write_element` 的逐函数精读。
- 在进入后续讲义前，建议确认你能脱口而出：`Output` trait 的三个方法分别是什么、`html_document` 和 `html` 各自的输入输出类型、为什么编码阶段不需要 `Engine`。
