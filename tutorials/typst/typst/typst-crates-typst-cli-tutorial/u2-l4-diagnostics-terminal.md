# 诊断与终端输出

## 1. 本讲目标

当你运行 `typst compile doc.typ` 失败时，终端会打印一段红色的、带源码位置的错误信息；当你敲错命令行参数时，你会看到另一种「error: ...」提示。这两种「错误」其实走的是**两条不同的输出通道**。

学完本讲，你将能够：

1. 区分 **应用级错误/提示**（来自 `main.rs`，与源码无关）和 **编译诊断**（来自 `print_diagnostics`，带源码位置）。
2. 读懂 `print_diagnostics` 如何把 errors 与 warnings 合并、再委派给 `typst_kit::diagnostics::emit`。
3. 理解 `terminal.rs` 的 `TermOut` 抽象：它如何统一彩色输出、清屏、清行，以及为何使用 singleton。
4. 掌握 `DiagnosticFormat`（`human` / `short`）的差异、`--diagnostic-format` 开关，以及颜色在非 TTY 下如何被自动禁用。

## 2. 前置知识

在进入本讲前，请确认你已经理解以下概念（它们在 u1-l2、u2-l2 已建立）：

- **应用级错误与软失败**：`main()` 三段式流程、`thread_local` 的 `EXIT` 退出码、`set_failed()` 把退出码改成 `FAILURE` 但仍返回 `Ok(())`。详见 [u1-l2](u1-l2-entry-dispatch.md)。
- **编译主流程**：`compile_once` 的软失败心脏——编译失败仍返回 `Ok(())`，靠 `set_failed()` 改退出码；静态警告用 `Span::detached()` 并入源码警告走同一管道。详见 [u2-l2](u2-l2-compile-config.md)。
- **clap 派生宏**：命令行参数如何用 `#[derive(Parser)]` / `ValueEnum` 定义成强类型枚举。详见 [u1-l3](u1-l3-args-model.md)。

此外，本讲会接触到几个新术语，先建立直觉：

| 术语 | 直觉解释 |
|------|----------|
| **诊断（diagnostic）** | 编译器对「源码里某段位置」给出的错误或警告，必须能指向 `file:line:col`。 |
| **codespan-reporting** | 一个 Rust 库，负责把「带源码片段 + 下划线 ^^^」的诊断漂亮地渲染到终端。 |
| **termcolor** | codespan 依赖的着色库，提供 `WriteColor` trait（既能写字节，也能设颜色）。 |
| **TTY** | 「电传打字机」的缩写，这里指真正的交互式终端；管道（`|`）和重定向（`>`）不是 TTY。 |
| **WriteColor trait** | `Write`（写字节）的超集，额外提供 `supports_color()` / `set_color()` / `reset()`。 |

## 3. 本讲源码地图

本讲涉及三个核心源文件（均在 `crates/typst-cli/src/`）与一个关键依赖文件：

| 文件 | 作用 |
|------|------|
| [src/terminal.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/terminal.rs) | 定义 `TermOut`——所有终端输出（诊断、应用错误、进度、状态）的统一出口，封装着色、清屏、清行。 |
| [src/main.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs) | 定义 `print_error` / `print_hint`——**应用级**错误与提示的打印，不涉及源码位置。 |
| [src/compile.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs) | 定义 `print_diagnostics`——把编译产生的 errors + warnings 合并后委派输出；`compile_once` 在成功/失败分支都调用它。 |
| [src/args.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs) | 定义 `DiagnosticFormat` 枚举（`Human` / `Short`）与 `--diagnostic-format` 参数。 |
| [crates/typst-kit/src/diagnostics.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs) | `print_diagnostics` 真正委派的目标——`emit()` 用 codespan-reporting 渲染每条诊断。 |

> 小贴士：typst-cli 是「薄壳」，真正的诊断渲染逻辑住在 typst-kit。`print_diagnostics` 只是把 CLI 的配置翻译一下，转手交给 typst-kit。

## 4. 核心概念与源码讲解

### 4.1 两条输出通道：应用级错误 vs 编译诊断

#### 4.1.1 概念说明

typst-cli 把「错误信息」分成两类，它们长得像、但来源和打印方式完全不同：

1. **应用级错误（application-level error）**：发生在 CLI 自身逻辑里，**与源码文件无关**。例如「找不到输入文件」「`--format` 与输出扩展名冲突」「self-updating 未启用」。这类错误由 `main()` 捕获 `dispatch()` 的 `Err`，再用 `print_error` / `print_hint` 打印。
2. **编译诊断（compile diagnostic）**：发生在编译器内部，**必须能指向源码里的某段位置**（`file:line:col` + 源码片段）。例如语法错误、类型错误、未定义变量。这类错误由 `print_diagnostics` 打印。

为什么分开？因为编译诊断需要 codespan-reporting 的「源码片段渲染」能力（画出 `^^^` 下划线、标注行列），而应用级错误只是一句话，用 codespan 反而是杀鸡用牛刀。两者**共用同一个 `TermOut` 出口**，所以颜色风格统一，但渲染路径不同。

#### 4.1.2 核心流程

两条通道的触发时机与数据流如下：

```text
【应用级错误通道】
  dispatch() 返回 Err(HintedStrResult)
    → main() 的 if let Err(msg) = res
      → set_failed()           // 退出码改 FAILURE
      → print_error(msg.message())   // 渲染 "error: ..."
      → print_hint(hint) (每条)      // 渲染 "hint: ..."

【编译诊断通道】
  compile_once() 内部
    → compile_and_export() 产出 Warned{ output, warnings }
    → Ok(_) 分支: print_diagnostics(world, &[], &warnings, fmt)
    → Err(errors) 分支: set_failed() + print_diagnostics(world, errors, &warnings, fmt)
```

关键点：编译诊断通道在**成功时也会调用** `print_diagnostics`——只不过此时 `errors` 是空数组 `&[]`，只打印 warnings。这与 u2-l2 讲过的「软失败」一脉相承：编译失败不会让 `dispatch()` 返回 `Err`，而是在 `compile_once` 内部 `set_failed()` + 打印诊断，最终仍 `Ok(())`。

#### 4.1.3 源码精读

先看 `main.rs` 中应用级错误的捕获与打印。`main()` 在 `dispatch()` 返回 `Err` 时统一处理，`HintedStrResult` 拆成 `message()` 与 `hints()` 两部分：

- [`main.rs:55-66`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L55-L66) —— 捕获 `dispatch()` 的错误，调用 `set_failed()`、`print_error`，再遍历每条 hint 调用 `print_hint`。

接着看 `print_error` 与 `print_hint` 的实现。两者结构几乎相同：取 codespan 默认样式，给「error」/「hint」字样上色，再 `reset` 后接正文：

- [`main.rs:89-111`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L89-L111) —— `print_error` 用 `styles.header_error` 给「error」上色；`print_hint` 用 `styles.header_help` 给「hint」上色。注意它们直接 `terminal::out()` 输出，**不经过 codespan 的 `emit`**，所以没有源码片段。

```rust
fn print_error(msg: &str) -> io::Result<()> {
    let styles = term::Styles::default();
    let mut output = terminal::out();
    output.set_color(&styles.header_error)?;
    write!(output, "error")?;
    output.reset()?;
    writeln!(output, ": {msg}")
}
```

对比：编译诊断走 `print_diagnostics`，它需要一个 `world` 参数（用来查源码位置），而 `print_error` 不需要——这正是两条通道最本质的区别。

#### 4.1.4 代码实践

**源码阅读型实践**：跟踪一次「应用级错误」的完整路径。

1. **实践目标**：确认应用级错误不经过 `print_diagnostics`，也不需要 `world`。
2. **操作步骤**：
   - 在 `compile.rs` 中找到 `new_impl` 里因为输出扩展名无法推断而 `bail!` 的地方（`compile.rs:119-123`，提示用 `--format/-f`）。
   - 顺着 `?` 冒泡，确认它会让 `CompileConfig::new` 返回 `Err`，进而让 `dispatch()` 里的 `compile(command)?` 返回 `Err`。
   - 最终落到 `main.rs:57-63` 的 `if let Err(msg) = res`。
3. **需要观察的现象**：这条路径上**没有**调用 `print_diagnostics`，也没有 `world` 参与。
4. **预期结果**：你会看到终端只打印一行 `error: could not infer output format ...`，不带任何 `^^^` 源码片段。
5. 复现命令（待本地验证）：`typst compile doc.typ -o out.xyz`（`.xyz` 是无法识别的扩展名）。

#### 4.1.5 小练习与答案

**练习 1**：如果编译产生了错误，`dispatch()` 最终返回的是 `Ok(())` 还是 `Err`？

> **参考答案**：返回 `Ok(())`。编译失败属于「软失败」：`compile_once` 内部已 `set_failed()` 并打印诊断，对外仍 `Ok(())`。只有**应用级**错误（如配置校验失败）才会让 `dispatch()` 返回 `Err`，进入 `main.rs:57` 的分支。

**练习 2**：`print_error` 里 `write!(output, "error")` 之后为什么要 `output.reset()`？

> **参考答案**：`set_color(&styles.header_error)` 会把后续所有输出都染上错误色（通常是红色）。`reset()` 把颜色恢复成默认，确保紧随其后的 `: {msg}` 正文用正常颜色打印，而不是整行变红。

---

### 4.2 print_diagnostics：合并错误与警告并委派输出

#### 4.2.1 概念说明

`print_diagnostics` 是 typst-cli 侧**唯一的**编译诊断出口。它的职责很薄——只做三件事：

1. 把 `errors` 与 `warnings` **合并**成一个迭代器。
2. 把 CLI 自己的 `DiagnosticFormat` 枚举**翻译**成 typst-kit 的同名枚举。
3. 把真正渲染的活儿**委派**给 `typst_kit::diagnostics::emit`，把 `TermOut` 当作着色输出目标传进去。

为什么 errors 和 warnings 要合并？因为它们在视觉上属于同一类信息（都指向源码位置），codespan-reporting 本身就按 severity 区分错误（红）与警告（黄），所以一次循环渲染即可。

#### 4.2.2 核心流程

```text
print_diagnostics(world, errors, warnings, format)
  │
  ├─ terminal::out()                          // 拿到着色输出目标 TermOut
  ├─ errors.iter().chain(warnings.iter())     // 先所有错误，后所有警告
  ├─ match format {
  │     Human => typst_kit Human,
  │     Short => typst_kit Short,
  │  }                                        // 枚举翻译
  └─ typst_kit::diagnostics::emit(out, world, diagnostics, fmt)
        │
        ├─ 构造 WorldFiles（codespan 的 Files trait 实现，按需缓存源码）
        ├─ 构造 term::Config { tab_width: 2, display_style: ... }
        └─ for diagnostic in diagnostics:
              codespan term::emit(out, config, files, diag)   // 逐条渲染
```

#### 4.2.3 源码精读

先看 `print_diagnostics` 本体，它就是「翻译 + 委派」：

- [`compile.rs:717-733`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L717-L733) —— 关键三步：`&mut terminal::out()` 取输出目标、`errors.iter().chain(warnings)` 合并、`match format` 翻译两个同名枚举，最后调 `typst_kit::diagnostics::emit`。

```rust
pub fn print_diagnostics(
    world: &dyn DiagnosticWorld,
    errors: &[SourceDiagnostic],
    warnings: &[SourceDiagnostic],
    format: DiagnosticFormat,
) -> Result<(), codespan_reporting::files::Error> {
    typst_kit::diagnostics::emit(
        &mut terminal::out(),
        world,
        errors.iter().chain(warnings),
        match format {
            DiagnosticFormat::Human => typst_kit::diagnostics::DiagnosticFormat::Human,
            DiagnosticFormat::Short => typst_kit::diagnostics::DiagnosticFormat::Short,
        },
    )
}
```

再看 `compile_once` 中两个调用点，注意成功分支传的是空 errors：

- [`compile.rs:289-290`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L289-L290) —— 成功分支：`print_diagnostics(world, &[], &warnings, ...)`，只打印 warnings。
- [`compile.rs:296-304`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L296-L304) —— 失败分支：先 `set_failed()`，再 `print_diagnostics(world, errors, &warnings, ...)`。

`print_diagnostics` 第一个参数类型是 `&dyn DiagnosticWorld`。这是一个扩展了 `World` 的 trait，多出一个把 `FileId` 翻译成人类可读路径的 `name` 方法：

- [typst-kit diagnostics.rs:22-28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L22-L28) —— `DiagnosticWorld` trait 定义：`fn name(&self, id: FileId) -> String`。

它的 CLI 实现在 `world.rs`：项目根内的文件用相对工作目录的路径，包内文件用包名前缀：

- [`world.rs:147-165`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L147-L165) —— `impl DiagnosticWorld for SystemWorld`，把 `FileId` 反向翻译成相对路径（`VirtualRoot::Project`）或 `package` 前缀路径（`VirtualRoot::Package`）。这就是你在诊断里看到 `doc.typ:3:5` 而非一串内部 id 的原因。

最后看 `emit` 内部如何逐条渲染：

- [typst-kit diagnostics.rs:40-102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L40-L102) —— `emit()`：构造 `WorldFiles`（codespan `Files` trait 的适配器）、按 format 设置 `term::Config` 的 `display_style`，然后循环把每条 `SourceDiagnostic` 转成 codespan 的 `Diagnostic`（含 message、hints 转成的 notes、span 转成的 labels），调 `term::emit` 输出。

#### 4.2.4 代码实践

**源码阅读型实践**：跟踪一条 warning 如何被「合并」并渲染。

1. **实践目标**：理解 warnings 与 errors 走的是同一条渲染管道。
2. **操作步骤**：
   - 在 `compile.rs:267` 看到 `compile_and_export` 返回 `Warned { output, mut warnings }`。
   - 在 `compile.rs:270-275` 看到静态警告（如 `--pages implies --no-pdf-tags`）被 `SourceDiagnostic::warning(Span::detached(), ...)` 包装后 `push` 进同一个 `warnings` 向量。
   - 在 `compile.rs:289`（成功分支）确认这些 warnings 最终交给 `print_diagnostics`，进而被 codespan 按 `Severity::Warning`（黄色）渲染。
3. **需要观察的现象**：静态警告和源码警告共享同一渲染器，唯一的区别是静态警告的 span 是 `detached`（无具体位置）。
4. **预期结果**：当 `--pages` 触发静态警告时，输出里会有一条黄色的 warning，但不指向具体行列。
5. 复现命令（待本地验证）：`typst compile doc.typ --pages 1 -o out.pdf`，应看到 `warning: using --pages implies --no-pdf-tags`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `print_diagnostics` 要把 `errors` 排在 `warnings` 前面（`errors.iter().chain(warnings)`）？

> **参考答案**：errors 比 warnings 更重要，排在前面能让用户最先看到真正的失败原因。codespan-reporting 不会自动按严重性排序，它按迭代顺序逐条打印，所以调用方负责决定顺序。

**练习 2**：`print_diagnostics` 把 CLI 的 `DiagnosticFormat` 用 `match` 翻译成 typst-kit 的 `DiagnosticFormat`，为什么不直接复用同一个类型？

> **参考答案**：CLI 的 `DiagnosticFormat` 派生了 clap 的 `ValueEnum`（要在 `--diagnostic-format` 里列出取值），而 typst-kit 的是纯逻辑枚举、不应依赖 clap。两个 crate 解耦后，typst-kit 可以被非 CLI 场景（如语言服务器）复用，无需拉入 clap。

---

### 4.3 TermOut：终端彩色输出抽象与单例

#### 4.3.1 概念说明

诊断、应用错误、下载进度、watch 状态——所有要往 stderr 写彩色文本的地方，都不该各自手搓 `ANSI` 转义码。`terminal.rs` 用 `TermOut` 把这些能力统一封装起来：

- **彩色写入**：实现 `WriteColor` trait，可以 `set_color` / `reset`。
- **清屏 / 清行**：`clear_screen` 清整屏、`clear_last_line` 清上一行（watch 与进度条靠它做「就地刷新」）。
- **非 TTY 自动禁色**：当输出被重定向（不是真终端）时，自动关闭颜色与清屏，避免把 `\x1B[2J` 这类乱码写进文件。

`TermOut` 用了 **singleton（单例）** 模式：全局只有一个 `TermOutInner`，所有 `TermOut` 句柄都指向它。这保证了「是否上色」这个决定在整个进程里只算一次、且处处一致。

#### 4.3.2 核心流程

```text
terminal::out()
  └─ singleton!(TermOutInner, TermOutInner::new())   // 全局唯一实例
        └─ TermOutInner::new()
              ├─ 读 ARGS.color (clap ColorChoice: Auto/Always/Never)
              ├─ 若 Auto 且 stderr.is_terminal() → ColorChoice::Auto   // 由 termcolor 探测
              ├─ 若 Always            → ColorChoice::Always
              └─ 其他（Never 或 Auto 但非 TTY）→ ColorChoice::Never
              └─ termcolor::StandardStream::stderr(choice)

clear_screen(): 仅当 supports_color() 才输出 "\x1B[2J\x1B[1;1H"
clear_last_line(): 仅当 supports_color() 才输出 "\x1B[1F\x1B[0J"
```

核心设计意图：**清屏 / 清行依赖颜色的判定**。代码里用 `if self.inner.stream.supports_color()` 作为清屏的前提——这不是巧合，而是把「是否真终端」这一信号复用：只有支持颜色的真终端，才可能正确解释光标移动转义码。

#### 4.3.3 源码精读

`out()` 是整个 crate 获取输出句柄的唯一入口，返回的 `TermOut` 持有一个 `&'static TermOutInner`：

- [`terminal.rs:9-22`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/terminal.rs#L9-L22) —— `out()` 调用 `singleton!` 宏拿到全局唯一 `TermOutInner`；`TermOut` 是可 `Clone` 的轻量句柄（只持有一个静态引用）。

清屏与清行都先检查 `supports_color()`：

- [`terminal.rs:25-48`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/terminal.rs#L25-L48) —— `clear_screen` 发送 `"\x1B[2J\x1B[1;1H"`（清屏并把光标移到左上角）；`clear_last_line` 发送 `"\x1B[1F\x1B[0J"`（光标上移一行，再清到屏幕末尾）。两者都被 `if supports_color()` 包裹。

```rust
pub fn clear_last_line(&mut self) -> io::Result<()> {
    if self.inner.stream.supports_color() {
        let mut stream = self.inner.stream.lock();
        write!(stream, "\x1B[1F\x1B[0J")?;
        stream.flush()?;
    }
    Ok(())
}
```

`TermOut` 同时实现 `Write` 与 `WriteColor`，内部都转发到 `self.inner.stream.lock()`：

- [`terminal.rs:51-73`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/terminal.rs#L51-L73) —— `Write`、`WriteColor` 的实现都加 `.lock()`，因为底层的 `StandardStream` 是可共享但需同步的；`supports_color()` 直接转发给底层流。

最关键的「是否上色」决策在 `TermOutInner::new`：

- [`terminal.rs:80-93`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/terminal.rs#L80-L93) —— 读取全局 `ARGS.color`：`Auto` 时**额外检查 `std::io::stderr().is_terminal()`**，只有真终端才让 termcolor 自己探测（`ColorChoice::Auto`）；否则一律 `Never`。`Always` 强制上色。

```rust
let color_choice = match ARGS.color {
    clap::ColorChoice::Auto if std::io::stderr().is_terminal() => ColorChoice::Auto,
    clap::ColorChoice::Always => ColorChoice::Always,
    _ => ColorChoice::Never,
};
```

> 注意：`is_terminal()` 来自标准库 `std::io::IsTerminal` trait（文件第 1 行 `use std::io::{self, IsTerminal, Write};`）。这正是颜色在非 TTY 下被禁用的根因。

#### 4.3.4 代码实践

**观察型实践**：验证非 TTY 下颜色与清屏被自动禁用。

1. **实践目标**：亲眼看到把输出重定向到文件后，ANSI 转义码消失。
2. **操作步骤**：
   - 准备一个有语法错误的 `doc.typ`（如写入 `#let x =` 不完整的语句）。
   - 直接运行：`typst compile doc.typ`（接终端），观察是否有红色「error」与 `^^^` 下划线。
   - 重定向运行：`typst compile doc.typ 2> err.txt`，再用 `cat -v err.txt`（`-v` 会显示不可见字符）查看。
3. **需要观察的现象**：
   - 终端直接运行：有颜色。
   - 重定向到文件：`err.txt` 里**没有** `\x1B[...` 转义码，是纯文本。
4. **预期结果**：对照 `terminal.rs:82-88`，重定向时 `stderr().is_terminal()` 为 `false`，`ColorChoice` 落到 `Never`，于是 `supports_color()` 为 `false`，颜色与清屏都被跳过。
5. 待本地验证（你也可以用 `--color always` 强制上色再重定向，对比看到转义码）。

#### 4.3.5 小练习与答案

**练习 1**：`TermOut` 用 `singleton!` 保证全局唯一 `TermOutInner`。如果不做成单例，每次 `out()` 都新建一个 `StandardStream`，会出现什么问题？

> **参考答案**：「是否上色」的探测结果可能不一致（如果中途 TTY 状态变化，虽然实际上不会）；更重要的是 `StandardStream` 内部带锁与缓冲，多个实例并发写 stderr 会交错乱序。单例确保整进程口径统一、且共享同一把锁。

**练习 2**：为什么 `clear_screen` / `clear_last_line` 要用 `supports_color()` 作为前提，而不是 `is_terminal()`？

> **参考答案**：`supports_color()` 实际反映了 termcolor 当前的着色决策（`Auto`/`Always`/`Never`）。当 `ColorChoice::Never`（含非 TTY 情形）时 `supports_color()` 为 `false`，此时清屏转义码对文件毫无意义，理应跳过。用同一个信号统一控制「颜色 + 清屏」，避免「颜色关了却还在输出 `\x1B[2J`」的不一致。

---

### 4.4 诊断格式 human / short 与 --diagnostic-format

#### 4.4.1 概念说明

typst 支持两种诊断渲染风格，由 `--diagnostic-format` 开关控制：

- **`human`（默认）**：富文本风格，会展开**源码片段**，画出带行号的源码行和 `^^^` 下划线，标注 file:line:col，并在末尾追加**调用栈式的 tracepoint**（形如 `  ... at doc.typ:2:5`）。
- **`short`**：精简风格，codespan 的 `DisplayStyle::Short` 只输出错误头（`error: ...`）与定位，**不展开源码片段**；并且 typst-kit 里的 tracepoint 输出**仅在 human 模式下触发**。

`short` 适合机器处理或 CI 日志里快速浏览；`human` 适合人坐在终端前调试。两者都**保留颜色**（受 `--color` 控制）。

#### 4.4.2 核心流程

```text
命令行 --diagnostic-format (human|short)
  └─ args.rs: DiagnosticFormat { Human(默认) | Short }
      └─ ProcessArgs.diagnostic_format
          └─ CompileConfig.diagnostic_format (compile.rs:69)
              └─ compile_once 传给 print_diagnostics
                  └─ emit() 里:
                        if format == Short → term::Config.display_style = Short
                        for diagnostic:
                            codespan term::emit(...)            // human/short 都执行
                            if format == Human:                  // 仅 human
                                for point in trace: emit_trace(...)
```

#### 4.4.3 源码精读

先看 CLI 的 `DiagnosticFormat` 枚举与 `--diagnostic-format` 参数定义：

- [`args.rs:636-642`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L636-L642) —— `DiagnosticFormat` 派生 `ValueEnum`，`Human` 标了 `#[default]`，所以不指定时默认 human。

```rust
#[derive(Debug, Default, Copy, Clone, Eq, PartialEq, Ord, PartialOrd, ValueEnum)]
pub enum DiagnosticFormat {
    #[default]
    Human,
    Short,
}
```

- [`args.rs:445-447`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L445-L447) —— `ProcessArgs` 里的 `#[clap(long, default_value_t)] pub diagnostic_format: DiagnosticFormat`，即 `--diagnostic-format`，缺省取 `Default`（Human）。

`CompileConfig` 把它原样存下，`compile_once` 调用 `print_diagnostics` 时透传：

- [`compile.rs:68-69`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L68-L69) —— `diagnostic_format: DiagnosticFormat` 字段；构造处 `diagnostic_format: args.process.diagnostic_format`（见 `compile.rs:243`）。

真正「风格切换」发生在 typst-kit 的 `emit` 里：

- [typst-kit diagnostics.rs:48-52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L48-L52) —— 当 `format == Short` 时，把 `term::Config` 的 `display_style` 设为 `term::DisplayStyle::Short`；否则保持默认（`Rich`），即 human 富文本。

```rust
let mut config = term::Config { tab_width: 2, ..Default::default() };
if format == DiagnosticFormat::Short {
    config.display_style = term::DisplayStyle::Short;
}
```

- [typst-kit diagnostics.rs:87-98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L87-L98) —— tracepoint 只在 `format == DiagnosticFormat::Human` 时输出，且若输出了 trace 会多打一个空行分隔。这是 short 比 human「更短」的第二个原因。

> 注意 human 与 short 的差异来自**两处**叠加：(1) codespan 的 `display_style`（控制源码片段是否展开）；(2) typst 自己的 tracepoint 守卫。两者都用同一个 `format` 判定，所以风格始终一致。

#### 4.4.4 代码实践

**动手实践**：制造语法错误，对比 human 与 short 两种格式的输出。

1. **实践目标**：直观感受两种格式的差异，并验证颜色在非 TTY 下被禁用。
2. **操作步骤**：
   - 新建 `doc.typ`，写入会触发错误的语句，例如：

     ```typst
     #let x =
     ```

   - human 模式（默认）：`typst compile doc.typ`。
   - short 模式：`typst compile doc.typ --diagnostic-format short`。
   - 强制无色：`typst compile doc.typ --color never`。
3. **需要观察的现象**：
   - human：能看到源码片段、行号、`^^^` 下划线，以及（若有）`... at doc.typ:1:9` 形式的 tracepoint。
   - short：只有 `error: ...` 与位置信息一行，**没有**源码片段和 tracepoint。
   - `--color never`：human 与 short 都变成纯文本，无任何颜色。
4. **预期结果**：
   - short 输出的行数明显少于 human。
   - 对照 [terminal.rs:80-93](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/terminal.rs#L80-L93)，`--color never` 让 `ARGS.color` 落到 `_ => ColorChoice::Never`，`supports_color()` 返回 `false`。
5. 待本地验证（不同版本错误信息措辞可能略有差异，但结构差异应如上）。

#### 4.4.5 小练习与答案

**练习 1**：一个诊断带有多层调用栈 trace。在 `--diagnostic-format short` 下，这些 tracepoint 会显示吗？

> **参考答案**：不会。tracepoint 的渲染被 `if format == DiagnosticFormat::Human` 守卫（diagnostics.rs:88），short 模式直接跳过整个 trace 循环。这正是 short 适合 CI 日志的原因之一。

**练习 2**：如果不传 `--diagnostic-format`，默认是哪种格式？由哪两处代码共同决定？

> **参考答案**：默认 `human`。决定点有二：(1) `args.rs` 的 `DiagnosticFormat::Human` 标了 `#[default]`，且 `--diagnostic-format` 用 `default_value_t`；(2) 不指定时 `term::Config.display_style` 保持默认 `Rich`，且 trace 守卫成立——两处都用 human 判定，保持一致。

---

## 5. 综合实践

把本讲的「两条通道 + TermOut + 两种格式」串起来，做一个完整的观察实验。

**任务**：分别用 human / short、彩色 / 无色组合，编译同一个错误文档，并把结果沉淀成一张「行为对照表」。

1. 准备 `doc.typ`：

   ```typst
   #let x =
   This is a test.
   ```

2. 依次运行并记录每条命令的输出行数、是否含源码片段、是否含 tracepoint、是否含 ANSI 转义码：

   | 命令 | 行数 | 源码片段 | tracepoint | ANSI 转义码 |
   |------|------|----------|------------|-------------|
   | `typst compile doc.typ` | ? | ? | ? | ? |
   | `typst compile doc.typ --diagnostic-format short` | ? | ? | ? | ? |
   | `typst compile doc.typ --color never` | ? | ? | ? | ? |
   | `typst compile doc.typ 2> out.txt`（重定向） | ? | ? | ? | ? |

3. 用 `cat -v out.txt` 检查重定向文件里是否还有 `\x1B[` 转义码。

4. **分析**：把观察到的现象分别对应到：
   - `--diagnostic-format` → `emit()` 的 `display_style` 与 trace 守卫（[diagnostics.rs:48-98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs#L48-L98)）。
   - `--color` 与重定向 → `TermOutInner::new` 的 `ColorChoice` 决策（[terminal.rs:80-93](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/terminal.rs#L80-L93)）。

5. **进阶**（源码阅读型）：`print_diagnostics` 同时被 `compile_once` 的成功与失败分支调用。请找出这两处调用（`compile.rs:289` 与 `compile.rs:303`），并解释为什么成功分支也要调用它。（提示：warnings。）

> 预期：这张表能让你一眼看清「格式开关控制内容多少、颜色开关控制是否有转义码」这两个正交维度。

## 6. 本讲小结

- typst-cli 有**两条输出通道**：应用级错误（`main.rs` 的 `print_error` / `print_hint`，无源码位置）与编译诊断（`compile.rs` 的 `print_diagnostics`，带源码位置）；两者共用同一个 `TermOut` 出口。
- `print_diagnostics` 本体很薄：合并 `errors` + `warnings`、翻译 `DiagnosticFormat`、委派给 `typst_kit::diagnostics::emit`；成功时 errors 为空 `&[]`，只打印 warnings。
- `TermOut` 封装彩色写入（`WriteColor`）、清屏（`clear_screen`）、清行（`clear_last_line`），用 singleton 保证整进程「是否上色」的决策一致。
- **非 TTY 自动禁色**：`TermOutInner::new` 在 `Auto` 模式下检查 `stderr().is_terminal()`，非终端或 `--color never` 时取 `ColorChoice::Never`，颜色与清屏都依赖 `supports_color()` 而被跳过。
- `DiagnosticFormat` 有 `human`（默认，富文本 + tracepoint）与 `short`（精简，无源码片段、无 tracepoint）两种，由 `--diagnostic-format` 控制，差异来自 codespan 的 `display_style` 与 typst-kit 的 trace 守卫两处叠加。
- 编译诊断与源码位置关联靠 `DiagnosticWorld::name`，`SystemWorld` 把 `FileId` 翻译成相对工作目录的路径，于是你能看到 `doc.typ:3:5`。

## 7. 下一步学习建议

本讲解的是「单次编译后如何打印诊断」。接下来建议：

- **u2-l5（Watch 模式与增量重编译）**：`TermOut` 的 `clear_screen` / `clear_last_line` 在 watch 模式下被重度使用——状态显示（Compiling/Success/Error）与重编译时的终端刷新都依赖它。学完你会明白这两个方法为何存在。
- **u3-l3（网络下载与进度报告）**：`PrintProgress` 用 `clear_last_line` 实现下载进度条的行内刷新，是 `TermOut` 的另一重要消费者。
- **源码延伸**：阅读 [typst-kit/src/diagnostics.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/diagnostics.rs) 的 `WorldFiles`（codespan `Files` trait 适配器）与 `emit_trace`（tracepoint 渲染），理解诊断如何从 `FileId` + span 翻译成屏幕上的行列与下划线。
