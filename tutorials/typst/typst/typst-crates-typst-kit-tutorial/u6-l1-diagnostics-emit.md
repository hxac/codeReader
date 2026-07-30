# diagnostics：诊断美化与输出

## 1. 本讲目标

Typst 编译失败或发出警告时，它产出的并不是一段纯文本字符串，而是一组结构化的「诊断」对象——里面带着严重级别、出错位置（span）、消息、调用栈（trace）和提示（hints）。可是终端需要的是带颜色、带源码上下文、人类可读的输出。**谁来完成这最后一步的「翻译与美化」？**

在 typst 工作区里，这件事就落在 `typst-kit` 的 `diagnostics` 模块上。它受 `emit-diagnostics` 特性门禁，是一个纯粹的「输出端」积木：既不参与编译，也不读文件系统之外的资源，只负责把 `typst-library` 产生的诊断对象，借助第三方库 `codespan-reporting` 渲染成漂亮的终端报告。

学完本讲，你应当能够：

1. 说清 `DiagnosticWorld` trait 在 `World` 之上多加了什么、为什么需要它。
2. 区分 `DiagnosticFormat` 的 `Human` 与 `Short` 两种格式，并理解它们如何影响输出。
3. 读懂 `emit()` 如何把一条 `SourceDiagnostic` 拆解成 codespan 的「主标签 / 次标签 / 备注 / tracepoint」。
4. 理解 `WorldFiles` 这个适配器如何把 typst 的 `World`「伪装」成 codespan 要求的 `Files` trait，以及它在缓存上的小心思。
5. 解释 tracepoint 为什么只在 `Human` 格式下输出、以怎样的 `file:line:column` 形式呈现。

## 2. 前置知识

本讲是 **advanced** 阶段内容，默认你已读过 [u1-l3 模块地图与 World 契约](u1-l3-modules-and-world-contract.md)，知道 `World` trait 是编译器与外界唯一的契约。下面补充几个本讲要用到的、可能你还陌生的概念。

### 2.1 codespan-reporting 与 termcolor

[`codespan-reporting`](https://github.com/brendanzab/codespan) 是 Rust 生态里一个成熟的「诊断渲染库」。你给它一组文件内容和一个 `Diagnostic` 结构（包含消息、严重级别、若干带范围的 `Label`），它就能渲染出类似下面这样带下划线、带行号侧栏的彩色报告：

```text
error: expected `]`
  ┌─ main.typ:3:7
  │
3 │ #let x = [1 2
  │           ^^^^
```

它对外只认一个核心 trait：`Files`——「给我一个文件 id，我就能告诉你它的文件名、源码内容、第 N 行的字节范围、某个字节偏移落在第几行第几列」。typst 的 `World` 并不直接长成这个样子，所以需要一个适配器（本讲的 `WorldFiles`）。

`termcolor` 是配合它的底层着色库，提供 `WriteColor`（一个既能写字节又能设置颜色的输出流）。`codespan-reporting` 把它重新导出为 `term::termcolor`。

### 2.2 SourceDiagnostic：编译器产出的「原料」

诊断的「原料」定义在 `typst-library`，核心结构是 `SourceDiagnostic`，它有五个字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `severity` | `Severity`（`Error` / `Warning`） | 是错误还是警告 |
| `span` | `DiagSpan` | 出错位置，可指向源码也可指向外部文件字节范围 |
| `message` | `EcoString` | 主消息 |
| `trace` | `EcoVec<Spanned<Tracepoint>>` | 类似调用栈，记录「是在调用哪个函数/show/import 时出的问题」 |
| `hints` | `EcoVec<Spanned<EcoString, DiagSpan>>` | 给用户的提示；带 span 的提示会标注在对应代码处，不带 span 的是通用备注 |

其中 `Tracepoint` 是一个枚举，表示调用栈的一帧（`Call` / `Show` / `Import` / `Include`），它的 `Display` 实现会输出形如 `` while calling `foo` `` 的文字。

### 2.3 DiagSpan：位置信息的紧凑表示

`DiagSpan` 定义在 `typst-syntax`，是一个仅 16 字节、null-optimized 的紧凑类型，用 `get()` 方法可以展开成三种 `DiagSpanKind` 之一：

- `Detached`：不指向任何文件（即「无位置」，常用于纯字符串错误）。
- `Number { id, num, sub_range }`：指向某个 Typst **源码**文件里的语法节点编号。
- `Range { id, range }`：直接给出某个文件（常是外部文件）的**字节范围**。

`id()` 方法返回它指向的 `FileId`（若 `Detached` 则为 `None`）。`emit()` 最核心的工作之一，就是把这些 span 翻译成 codespan 需要的「文件 id + 字节范围」。

> 关键术语回顾（来自 u1-l3）：`FileId` 是与磁盘路径解耦的文件标识；`FileResult<T>` 即 `Result<T, FileError>`。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [src/diagnostics.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs) | **本讲主角**。整个文件受 `emit-diagnostics` 门禁，定义 `DiagnosticWorld`、`DiagnosticFormat`、`emit()`、`emit_trace()`、`WorldFiles`。 |
| [../typst-cli/src/compile.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs) | 调用方。`print_diagnostics()` 把 CLI 的格式枚举映射到 typst-kit 的 `DiagnosticFormat`，再调用 `emit()`。 |
| [../typst-cli/src/world.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs) | `SystemWorld` 实现 `DiagnosticWorld`，提供 `name()`——把 `FileId` 渲染成给用户看的路径字符串。 |
| [../typst-library/src/diag.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs) | 定义 `SourceDiagnostic`、`Severity`、`Tracepoint`（原料）。 |
| [../typst-syntax/src/span.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/span.rs) | 定义 `DiagSpan`、`DiagSpanKind`（位置表示）。 |

整张图的因果链是：

```text
typst::compile(world)
  └─ 产出 Vec<SourceDiagnostic>（原料）
       └─ typst-kit::diagnostics::emit()
            ├─ WorldFiles 把 World 适配成 codespan 的 Files
            ├─ 把每条 SourceDiagnostic 拆成 codespan 的 Diagnostic
            └─ term::emit() / emit_trace() 渲染到带颜色的终端
```

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：`DiagnosticWorld` trait、`DiagnosticFormat`、`emit()`、`WorldFiles`。

### 4.1 DiagnosticWorld trait

#### 4.1.1 概念说明

`codespan-reporting` 渲染时需要知道「某个出错文件的**文件名**该显示成什么」。typst 的 `World` trait 只负责按 `FileId` 返回文件**内容**（`source` / `file`），它并不知道「这个 id 在用户终端里应该打印成 `main.typ` 还是 `/abs/path/main.typ`」——因为这是个**展示策略**，与「能否读到文件内容」无关，且不同集成方（CLI、LSP、web 编辑器）想展示的路径形式不同。

于是 typst-kit 定义了一个扩展 trait `DiagnosticWorld: World`，在 `World` 之上只多加一个方法 `name(id) -> String`，把「文件 id → 给人看的名字」这一展示职责单独抽出来。任何想用 `emit()` 的集成方，只要在自己的 `World` 实现上再补一个 `name()`，就能复用全部诊断美化逻辑。

这正是 typst-kit「积木库」定位的典型体现：编译逻辑归 `World`，展示逻辑归 `DiagnosticWorld`，各管一摊。

#### 4.1.2 核心流程

```text
集成方定义 MyWorld: World
  └─ 额外 impl DiagnosticWorld for MyWorld { fn name(id) -> String { ... } }
       └─ 之后即可把 &MyWorld 当作 &dyn DiagnosticWorld 传给 emit()
```

注意它是 `World` 的**子 trait**（`DiagnosticWorld: World`），所以 `&dyn DiagnosticWorld` 同时也满足 `&dyn World`——`emit()` 内部既能调用新增的 `name()`，也能调用 `World` 的 `source()` / `file()`。

#### 4.1.3 源码精读

trait 定义非常简短，只增加一个 `name` 方法：

[文件路径:L22-L28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L22-L28) —— `DiagnosticWorld` 继承 `World`，仅新增 `name(id) -> String`，文档点明「在 CLI 里它会把路径格式化成相对工作目录的形式」。

CLI 的 `SystemWorld` 正是这样补上 `name` 的：

[文件路径:L147-L165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L147-L165) —— `SystemWorld` 实现 `DiagnosticWorld`。对 `Project` 根，它把虚拟路径 realize 到磁盘后，用 `pathdiff::diff_paths` 尽量表达成「相对工作目录」的路径，失败时回退到纯 vpath；对 `Package` 根，则格式化为 `{package}{vpath}`。这条 `name` 决定了终端里 `file:line:column` 的 `file` 部分长什么样。

> 另一个实现见 [../typst-cli/src/eval.rs:L147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs#L147)：`typst eval` 子命令的 `ExpressionWorld` 也实现了 `DiagnosticWorld`，把表达式输入特判为 `<input-expression>`，其余转发给内部 world。可见同一份 `emit()` 被多个集成方复用。

#### 4.1.4 代码实践

**实践目标**：体会 `DiagnosticWorld` 与 `World` 的分工——展示职责可以被独立覆盖。

**操作步骤**：

1. 阅读 [src/diagnostics.rs:L22-L28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L22-L28)，确认 trait 只新增了 `name` 一个方法。
2. 阅读 [../typst-cli/src/world.rs:L147-L165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L147-L165)，看清 `Project` 与 `Package` 两种根各自的命名策略。
3. 假设你要写一个 web 编辑器后端，希望把所有路径显示成 `workspace://main.typ` 这种虚拟形式。在一张纸上写出：你需要实现哪个方法、返回什么字符串。

**需要观察的现象**：你会发现自己**完全不需要**改动 typst-kit 的 `emit()`，也**不需要**重新实现任何 codespan 逻辑——只要换一个 `name()` 实现即可。

**预期结果**：一个虚拟命名方案（例如返回 `format!("workspace://{}", id.vpath().get_without_slash())`）就足够，印证「展示职责被干净地隔离在 `DiagnosticWorld::name` 一处」。

> 待本地验证：若你真的搭建一个最小 `DiagnosticWorld` 实现，确认编译器只要求你提供 `name`，其余 `World` 方法沿用你已有的实现。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `name()` 不直接放进 `World` trait？

> **答案**：因为 `World` 描述的是「编译所需的运行环境」（提供 library / source / file / font 等），关心的是**能否拿到内容**；而 `name()` 是纯粹的**展示策略**，不同集成方想显示的路径形式不同。把展示职责塞进 `World` 会让所有不想做终端输出的实现者也被迫提供它，违背接口隔离。拆成子 trait `DiagnosticWorld` 后，只有需要诊断美化的集成方才实现它。

**练习 2**：`&dyn DiagnosticWorld` 能否被传给一个期待 `&dyn World` 的函数？

> **答案**：能。因为 `DiagnosticWorld: World` 是子 trait，任何 `DiagnosticWorld` 的实现必然也是 `World` 的实现，存在从前者到后者的解引用 / 类型转换路径。`emit()` 内部的 `WorldFiles` 正是靠这一点同时调用 `name()`（子 trait）和 `source()`/`file()`（父 trait）。

---

### 4.2 DiagnosticFormat

#### 4.2.1 概念说明

同样是「输出诊断」，用户在不同场景下要的东西不一样：

- **交互式编译**：想要带源码上下文、带颜色、带行号侧栏的丰富报告，便于人眼定位。
- **脚本 / CI / 编辑器解析**：只想要一行紧凑的 `error: ...`，方便 grep 或用正则切分。

`DiagnosticFormat` 枚举就是这两个意图的开关。它只有两个变体：`Human`（默认，丰富格式）和 `Short`（单行紧凑格式）。

#### 4.2.2 核心流程

```text
DiagnosticFormat::Human（默认）
  └─ codespan 用 term::DisplayStyle::Rich 渲染完整报告
  └─ 额外输出 tracepoint（见 4.3）

DiagnosticFormat::Short
  └─ codespan 用 term::DisplayStyle::Short 渲染单行
  └─ 不输出 tracepoint
```

`emit()` 里并没有为两种格式写两套渲染代码，而是**复用** codespan 自身的能力：只把 `config.display_style` 从默认的 `Rich` 切到 `Short`，渲染引擎就自动改成单行输出。

#### 4.2.3 源码精读

枚举定义极简，`Human` 带了 `#[default]`：

[文件路径:L30-L38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L30-L38) —— `DiagnosticFormat` 只有 `Human`（默认）/ `Short` 两个变体。

`emit()` 中根据格式切换 codespan 配置：

[文件路径:L49-L52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L49-L52) —— 构造 `term::Config`（`tab_width: 2`），并在 `Short` 时把 `display_style` 设为 `term::DisplayStyle::Short`。这是唯一影响 codespan 渲染风格的开关。

> 注意：CLI 有一份**自己的** `DiagnosticFormat` 枚举（[../typst-cli/src/args.rs:L638](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L638)，由命令行 `--diagnostic-format` 提供），typst-kit 也有一份。两者**不是同一个类型**，调用方需要手动 `match` 映射——见 4.3.3。这是 typst-kit 不强加 CLI 依赖、保持中立的体现。

#### 4.2.4 代码实践

**实践目标**：用 CLI 的 `--diagnostic-format short` 直观感受两种格式差异。

**操作步骤**：

1. 在临时目录写一个故意出错的 `bad.typ`，例如内容 `#let x = [1 2`（方括号未闭合）。
2. 分别运行（**待本地验证**）：
   - `typst compile bad.typ`（默认 Human）
   - `typst compile bad.typ --diagnostic-format short`

**需要观察的现象**：

- Human 模式输出多行，带 `┌─ bad.typ:line:col` 侧栏、源码片段与 `^^^` 下划线。
- Short 模式只输出类似 `error: expected `]`: bad.typ:line:col` 的单行。

**预期结果**：两者来自同一个 `emit()`，差别仅是 `display_style` 与是否打印 trace，印证 4.2.2 的流程。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `DiagnosticFormat` 的 `#[default]` 从 `Human` 改成 `Short`，对 CLI 用户的默认体验有什么影响？

> **答案**：那么不带 `--diagnostic-format` 时，用户将默认得到单行紧凑输出，丢失源码上下文与彩色高亮，对人眼定位错误很不友好。所以默认 `Human` 是面向「交互式编译」的人性化选择；`Short` 留给脚本/CI。

---

### 4.3 emit()

#### 4.3.1 概念说明

`emit()` 是整个模块的入口函数，承担「翻译」职责：遍历一组 `SourceDiagnostic`，把每条翻译成 codespan 能渲染的 `Diagnostic`，然后交给 `term::emit()` 画到带颜色的输出流上。

一条 `SourceDiagnostic` 会被拆解成 codespan `Diagnostic` 的四个部分：

| SourceDiagnostic 字段 | codespan Diagnostic 部分 | 说明 |
|---|---|---|
| `severity` | `Diagnostic::error()` / `warning()` | 决定前缀是 `error:` 还是 `warning:` |
| `message` | `with_message(...)` | 主消息 |
| `hints`（**detached** 的） | `with_notes(...)` | 无位置的通用提示，作为底部 `hint: ...` 备注 |
| `span` | `Label::primary(...)` | 主出错位置，下划线高亮 |
| `hints`（带 span 的） | `Label::secondary(...).with_message(...)` | 次要位置，附说明文字 |
| `trace`（仅 Human） | `emit_trace()` 自定义输出 | 类似调用栈，见下文 |

#### 4.3.2 核心流程

```text
emit(dest, world, diagnostics, format):
  files = WorldFiles { world, sources: {} }        # 适配器，见 4.4
  config = term::Config { tab_width: 2, ... }
  if format == Short: config.display_style = Short

  for diagnostic in diagnostics:
    diag = (error() 或 warning())
      .with_message(diagnostic.message)
      .with_notes( 通用 hints  → "hint: ..." )
      .with_labels( 主标签(span) 链上 次标签(带位置 hints) )
    term::emit(dest, config, files, diag)           # 交给 codespan 渲染

    if format == Human:                             # 仅丰富格式才画 trace
      for point in diagnostic.trace:
        emit_trace(dest, files, point)
      若画过 trace，再空一行分隔
```

两个值得记住的设计点：

1. **hints 被分成两路**：`span.is_detached()` 的 hint 走 `with_notes`（底部备注），带位置 hint 走 `with_labels`（标注在代码处）。同字段两条出路，全靠 span 是否 detached 区分。
2. **trace 与 codespan 是分离的**：tracepoint **不**塞进 codespan 的 `Diagnostic`，而是由 typst-kit 自己用 `emit_trace()` 直接往 `dest` 写文字。这也解释了为什么它只在 `Human` 下输出——`Short` 追求单行，多出来的 trace 行会破坏这一目标。

#### 4.3.3 源码精读

入口签名与适配器构造：

[文件路径:L40-L48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L40-L48) —— `emit()` 接收「可写着色的输出流 `dest`、诊断世界 `world`、诊断迭代器、格式」，内部构造 `WorldFiles`（带一个空的 source 缓存）。

逐条翻译的核心：

[文件路径:L54-L85](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L54-L85) —— 对每条诊断：用 `severity` 选 `Diagnostic::error()`/`warning()`；`with_message` 填主消息；`with_notes` 收集**所有 detached hint** 并前缀 `hint: `；`with_labels` 先用主 `span` 经 `files.range()` 拿到字节范围做成 `Label::primary`，再 `chain` 上每个带位置 hint 的 `Label::secondary().with_message(&hint.v)`；最后 `term::emit()` 渲染。

注意第 64 行的 `filter(|s| s.span.is_detached())` 与第 78 行的 `filter_map(|hint| hint.span.id()?)` 这一对——前者挑出「无位置」hints 进备注，后者挑出「有位置」hints 进次标签，互不重叠。

trace 仅在 Human 下输出：

[文件路径:L88-L98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L88-L98) —— 整个 trace 块被 `if format == DiagnosticFormat::Human` 包住；遍历 `diagnostic.trace` 调 `emit_trace`，只要画过至少一帧，就用 `writeln!(dest)?` 补一个空行做视觉分隔。

`emit_trace` 单帧渲染（这是本讲实践任务的焦点）：

[文件路径:L104-L125](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L104-L125) —— 对一个 `Spanned<Tracepoint>`：先取 `id`、`range`、`lines`、`name`、`line_index`、`line_number`、`column_number`（任一取不到就静默 `return Ok(())`）；然后写成两行——第一行 `  {point.v} at ` 接**带下划线**的 `{name}:{line}:{column}`，其中 `point.v` 是 `Tracepoint` 的 `Display`（如 `` while calling `foo` ``），`line` 是 1-based 行号、`column` 是 1-based 列号。

[文件路径:L127-L143](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L127-L143) —— 第二行是单行源码上下文：缩进 4 空格、用 `Ansi256(248)`（灰色）打印该 range 覆盖的文本；若文本跨多行，则取首行末尾接 `…` 再补上最后一个非空白字符（如 `…}`），从而把多行压成一行展示。

> 关于 `{name}:{line}:{column}` 三者的来源：`name` 来自 `files.name(id)`（最终走到 `world.name(id)`，即 4.1 里的展示策略）；`line = files.line_number(id, line_index)`（1-based）；`column = files.column_number(id, line_index, range.start)`（1-based）。它们都由 `WorldFiles` 提供，见 4.4。

调用方 `print_diagnostics`：

[文件路径:L718-L733](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L718-L733) —— CLI 把 `terminal::out()`（一个带颜色的输出流）作为 `dest`，把错误与警告 `errors.iter().chain(warnings)` 串成迭代器，并用一个 `match` 把 CLI 自己的 `DiagnosticFormat` 映射成 typst-kit 的 `DiagnosticFormat`，最后调用 `typst_kit::diagnostics::emit`。

而 `compile_once` 在编译成功（只打警告）与失败（打错误+警告）两条路径上都会调用它：

[文件路径:L289-L304](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L289-L304) —— 成功分支传 `&[]` 错误 + 警告；失败分支 `set_failed()` 后传错误 + 警告。

#### 4.3.4 代码实践（本讲指定实践）

**实践目标**：读懂 `emit_trace`，说清 tracepoint 在 `Human` 下如何以 `file:line:column` 单行展示源码上下文，并解释 `Short` 为何不输出 trace。

**操作步骤**：

1. 通读 [src/diagnostics.rs:L104-L146](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L104-L146)，对照下面的问题逐条作答。
2. 再回到 [src/diagnostics.rs:L88-L98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L88-L98)，看清 trace 块的格式门禁。

**需要回答的问题（建议手写）**：

1. tracepoint 的「位置行」由哪几个量拼成？分别是 0-based 还是 1-based？
2. 「上下文行」如何把多行文本压成一行？
3. 为什么 `Short` 格式完全不调用 `emit_trace`？

**预期结果（参考答案）**：

1. 位置行是 `  {point.v} at {name}:{line}:{column}`。`point.v` 是 `Tracepoint` 的 `Display`（如 `` while calling `foo` ``）；`name` 来自 `world.name(id)`；`line = files.line_number(...)`、`column = files.column_number(...)` 都是 **1-based**（codespan 的 `line_number` / `column_number` 约定即 1-based）。
2. 上下文行先打印 range 覆盖文本的**首行**；若文本跨多行，则用首行末尾接 `…` 再补上**最后一个非空白字符**（见 L134-L141 的 `lines.next_back()` 与 `last.chars().next_back()` 判断），从而把任意多行 range 压成单行灰色展示。
3. 因为整个 trace 循环被 [L88 的 `if format == DiagnosticFormat::Human`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L88) 包住。`Short` 格式的语义是「单行紧凑诊断」，而 tracepoint 会额外追加至少两行（位置行 + 上下文行），与「单行」目标冲突；并且 `Short` 下 codespan 自己也用 `DisplayStyle::Short` 只画一行摘要。所以 trace 被刻意只在 `Human` 下输出。

> 待本地验证：写一个会触发函数调用栈错误的 `.typ`（例如在一个被 `#include` 的文件里制造错误），用 `typst compile --diagnostic-format human` 观察出现的 `while importing ...` / `while calling ...` trace 行及其单行灰色上下文；再用 `--diagnostic-format short` 确认这些行消失。

#### 4.3.5 小练习与答案

**练习 1**：一条诊断有 3 个 hint，其中 2 个带 span、1 个是 detached。它们分别会出现在输出的哪里？

> **答案**：detached 的那个出现在报告底部，前缀 `hint: `（走 `with_notes`，L60-L67）；带 span 的两个会作为 `Label::secondary` 标注在各自代码位置并附上提示文字（走 `with_labels`，L77-L82）。

**练习 2**：为什么 tracepoint 不直接做成 codespan 的 `Label`，而要自己写 `emit_trace`？

> **答案**：codespan 的 `Label` 是「在源码某范围内画下划线并附文字」，适合标注出错点；而 tracepoint 是**调用栈帧**，要展示成 `while calling X at file:line:col` + 单行上下文的特殊两行格式，codespan 没有现成的「调用栈」渲染。所以 typst-kit 用 `emit_trace` 直接往输出流写自定义文字，绕过 codespan 的标签机制。

**练习 3**：`emit()` 在 `term::emit(...)?` 之后才执行 trace 循环。如果 `term::emit` 因为 IO 错误提前 `return`，trace 还会画吗？

> **答案**：不会。因为 `term::emit(dest, ...)?` 的 `?` 在出错时会立即从 `emit()` 返回错误，跳过后面的 trace 块。这是符合预期的：主诊断都没能写出，再画 trace 已无意义。

---

### 4.4 WorldFiles

#### 4.4.1 概念说明

`codespan-reporting` 不认识 typst 的 `World`，它只认识自己定义的 `Files` trait。`WorldFiles` 就是把 `DiagnosticWorld`（进而 `World`）**适配**成 `Files` 的适配器——这也是经典的「适配器模式」：让两个不兼容的接口（typst 的 `World` 与 codespan 的 `Files`）通过一个中间结构协作。

`WorldFiles` 同时还偷偷承担了一个性能职责：**缓存已加载的 `Source`**。因为渲染一条诊断往往要反复查询同一个文件的行号、列号、行范围，若每次都重新走 `World::source` 会很浪费。

#### 4.4.2 核心流程

`WorldFiles` 持有两样东西：`world: &dyn DiagnosticWorld`（数据来源）和 `sources: HashMap<FileId, Source>`（缓存）。它对外提供两类方法：

```text
供 emit() 使用的「写入」辅助（&mut self）：
  range(span) -> Option<Range<usize>>
    ├─ Detached          → None
    ├─ Number {id, ...}  → world.source(id) 算 range，并把 source 存进缓存
    └─ Range {id, range} → 直接返回 range（不触发 source 加载）

实现 codespan 的 Files trait（&self，只读）：
  name(id)        → world.name(id)
  source(id)      → lines(id)              # 注意：返回的是 Lines<String>
  line_index(id, byte) → lines.byte_to_line(byte)
  line_range(id, line) → lines.line_to_range(line)
  column_number(...)    → lines.byte_to_column(byte)

  其中 lines(id) 的取法：
    ├─ 缓存里有 Source → source.lines().clone()
    └─ 缓存里没有      → world.file(id) 再 .lines()（按原始字节算行信息）
```

这里有个**关键不对称**：`range()` 需要 `&mut self`（它会写缓存），而 `Files` trait 的方法只拿 `&self`（只能读缓存）。所以 `emit()` 在调 `term::emit` 之前，会先用 `&mut files` 把主 span 与各 hint span 的范围算出来（顺带把涉及文件的 Source 填进缓存），等控制权交给 codespan 时，缓存里通常已经有货了。codespan 再去查 `line_index` / `column_number` 时，走的就是只读快路径。

另一个细节：`lines()` 对「源码文件」用缓存的 `Source`，对「非源码文件」（比如诊断指向某个被加载的外部数据文件的字节范围）则回退到 `world.file(id)`，按原始字节计算行信息——因为这种文件从来没作为 `Source` 被加载过。错误会被映射：`FileError::NotFound` → codespan 的 `FileMissing`，其余 → `CodespanError::Io`。

#### 4.4.3 源码精读

结构体本身只有两个字段：

[文件路径:L148-L152](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L148-L152) —— `WorldFiles` 持有 `world` 引用与 `sources` 缓存表。

`range()`：把 span 展开成字节范围，顺便缓存 Source：

[文件路径:L154-L168](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L154-L168) —— `match span.into().get()`：`Detached` 返回 `None`；`Number` 分支调 `self.world.source(id)?` 取源码、用 `source.range(num, sub_range)` 算范围、再用 `self.sources.entry(id).or_insert(source)` 把源码**存进缓存**；`Range` 分支因为本身已带字节范围，直接返回 `Some(range)`，**不触发** source 加载也不写缓存。

`lines()`：优先复用缓存，否则回退到原始文件：

[文件路径:L170-L185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L170-L185) —— 缓存命中则 `source.lines().clone()`；否则 `self.world.file(id)` 取字节、`.lines()` 算行信息，并把 `FileError` 映射成 `CodespanError`（`NotFound → FileMissing`，其余 `→ Io`）。

`Files` trait 实现：把 codespan 的查询逐一转交给 `Lines`：

[文件路径:L187-L216](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L187-L216) —— `type FileId = FileId; type Name = String; type Source = Lines<String>;`；`name` 转交 `world.name`；`source` 转交 `lines`；`line_index` 用 `lines.byte_to_line` 并在越界时返回 `IndexTooLarge`；`line_range` 用 `lines.line_to_range` 并在越界时返回 `LineTooLarge`。

[文件路径:L218-L233](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L218-L233) —— `column_number` 的第二个参数（`line_index`）被显式忽略（写作 `_`），它直接用字节偏移 `given` 调 `lines.byte_to_column`；越界时按是否超出 `len_bytes` 区分返回 `InvalidCharBoundary` 或 `IndexTooLarge`。

> 想进一步了解 `Source::range`、`Lines::byte_to_line` 等行号/列号换算的底层实现，可阅读 `typst-syntax` 中 `Source` 与 `Lines` 的源码（本讲不展开）。

#### 4.4.4 代码实践

**实践目标**：体会 `range()` 的「写缓存」与 `lines()` 的「读缓存」之间的协作，理解 `&mut` / `&` 的分工。

**操作步骤（源码阅读型）**：

1. 阅读 [src/diagnostics.rs:L154-L168](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L154-L168)，找出 `range()` 里**唯一**会写 `self.sources` 的那一个分支（答案：`Number` 分支的 `self.sources.entry(id).or_insert(source)`）。
2. 回答：为什么 `Range` 分支不写缓存？因为它不调 `world.source(id)`，没有拿到 `Source` 可存；它的字节范围是 span 自带的。
3. 跟踪一次渲染：`emit()` 在 [L68-L83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L68-L83) 里对主 span 和每个带位置 hint 都调了 `files.range(...)`（借 `&mut files`），因此 codespan 后续在 `Files::line_index/column_number` 里查同一个文件时，`lines()` 大概率命中缓存——画出这条「先写后读」的时间线。

**需要观察的现象**：你会发现 `range()` 是**主动预热**缓存的入口，而 codespan 的 `Files` 方法只是**被动消费**缓存。这是一处隐蔽但重要的性能设计。

**预期结果**：能用自己的话讲清「为什么 `WorldFiles` 同时需要 `&mut self` 的 `range()` 和 `&self` 的 `Files` 实现，且前者必须先于后者被调用」。

> 待本地验证：若你在自己的集成里直接使用 `WorldFiles`，注意必须先用 `&mut` 调 `range()` 预热，再把它以 `&` 传给 codespan 的 `term::emit`；否则 `lines()` 会走 `world.file(id)` 回退路径，对源码文件而言多一次重复加载。

#### 4.4.5 小练习与答案

**练习 1**：`WorldFiles::source(id)`（即 `Files::source`）返回的类型是 `Source` 吗？

> **答案**：不是。它的 `type Source = Lines<String>`，`source()` 内部转交给 `lines(id)`，返回的是行信息结构 `Lines<String>` 而非 typst 的 `Source` 类型。这里 `Source` 只是 codespan `Files` trait 里关联类型的名字，与 typst 的 `typst_syntax::Source` 同名但不同物，别混淆。

**练习 2**：假设一条诊断的主 `span` 是 `Range`（外部文件字节范围），而没有任何 hint 用到该文件的编号 span。codespan 渲染它时，`lines()` 会走哪条路径？

> **答案**：走**回退路径**——`world.file(id)` 再 `.lines()`。因为 `Range` 分支的 `range()` 不写缓存（L166 直接 `Some(range)` 返回，未 `or_insert`），`sources` 里没有该文件，于是 `lines()` 命中失败，回退为按原始字节计算行信息。这正是 `lines()` 设计回退分支的原因。

**练习 3**：为什么把 `FileError::NotFound` 单独映射成 `CodespanError::FileMissing`，而其他错误映射成 `Io`？

> **答案**：因为 codespan 对「文件不存在」（`FileMissing`）和「普通 IO 失败」（`Io`）有不同的渲染语义与错误处理；把 NotFound 精确映射成 FileMissing，可以让上层（`emit()` 返回的 `Result`）和 codespan 内部都能区分「文件真的找不到」与「读取时发生其他 IO 错误」，给出更准确的反馈。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「**追踪一条诊断从产出到落屏的全链路**」。

**任务**：制造一条带 tracepoint 的错误，追踪它如何流经 typst-kit 的诊断管线。

**操作步骤**：

1. 准备一个最小可复现项目（**待本地验证**）：
   - `lib.typ`：定义一个会出错的函数，例如 `#let f() = panic`（或任何会触发编译期错误的表达式）。
   - `main.typ`：`#import "lib.typ": f` 后 `#f()`，让错误发生在被导入/调用的上下文中，从而产生 trace。
2. 运行 `typst compile main.typ`（默认 Human 格式），观察输出。
3. 对照本讲源码，为输出里的**每一行**标注它来自哪段代码：
   - `error: ...` 主消息 → [L54-L59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L54-L59) 的 severity + message；
   - 源码侧栏与 `^^^` 下划线 → `Label::primary` 经 `term::emit` 渲染（[L68-L85](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L68-L85)）；
   - `hint: ...` 备注（若有）→ [L60-L67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L60-L67)；
   - `while importing ...` / `while calling ...` 两行 → `emit_trace`（[L104-L146](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L104-L146)）。
4. 再运行 `typst compile main.typ --diagnostic-format short`，确认：输出变成单行，trace 完全消失（因 [L88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L88) 的 `if format == Human` 门禁），侧栏/下划线也退化为 `file:line:col` 后缀。
5. 把「输出行 → 代码段」的对照表写成一份笔记。

**预期结果**：你能完整复述一条 `SourceDiagnostic` 如何经 `emit()` 拆解、由 `WorldFiles` 适配、最终在终端呈现为人可读的报告，并能解释 Human 与 Short 的全部差异。这就达成了本讲的全部学习目标。

## 6. 本讲小结

- `diagnostics` 模块是 typst-kit 的「输出端」积木，受 `emit-diagnostics` 门禁，不参与编译，只把结构化诊断渲染成终端报告。
- `DiagnosticWorld: World` 在 `World` 之上只新增 `name(id)`，把「文件 id → 给人看的名字」这一**展示职责**干净隔离；CLI 的 `SystemWorld` 等多个实现各自决定命名策略。
- `DiagnosticFormat`（`Human` 默认 / `Short`）通过切换 codespan 的 `display_style` 改变渲染风格，并非两套代码。
- `emit()` 把每条 `SourceDiagnostic` 拆成 codespan 的 `Diagnostic`：severity→前缀、message→主消息、detached hints→底部备注、主 span→primary 标签、带位置 hints→secondary 标签；trace 由自定义的 `emit_trace` 处理。
- tracepoint 只在 `Human` 下输出，画成 `  {Tracepoint} at {name}:{line}:{column}` 加单行灰色上下文；`Short` 为保「单行」而完全跳过 trace。
- `WorldFiles` 是把 `World` 适配成 codespan `Files` 的适配器，并借 `range()`（`&mut`，写缓存）与 `lines()`（`&`，读缓存）的分工，实现「先预热、后只读」的渲染性能优化。

## 7. 下一步学习建议

本讲是「单元 6 终端诊断输出」的唯一一讲，也是 typst-kit「工具型能力」的开篇。建议接下来按以下方向继续：

1. **横向打通 CLI 调用链**：重读 [../typst-cli/src/compile.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs) 的 `compile_once`，看 `print_diagnostics` 如何与编译结果、`terminal::out()`、`--diagnostic-format` 串联，形成完整的「编译→诊断→输出」主链路。
2. **进入实时工具链**：继续学习 [u7-l1 HTTP 热重载服务器](u7-l1-http-server.md) 与 [u7-l2 文件监视 watcher](u7-l2-file-watcher.md)，了解 `typst watch` 如何在诊断输出的基础上叠加热重载。
3. **深入诊断原料**：若想理解 `trace` 与 `hints` 是如何在编译过程中被逐步填充的，可阅读 `typst-library/src/diag.rs` 中 `Trace` trait 的实现（`trace()` 方法如何把调用栈帧追加到错误上）。
4. **理解 span 体系**：本讲把 `DiagSpan` 当作输入，若想看清它的紧凑表示与 `get()` 解包逻辑，可深入 `typst-syntax/src/span.rs`。
