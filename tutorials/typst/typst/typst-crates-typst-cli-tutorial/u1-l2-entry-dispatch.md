# 入口与命令分发

## 1. 本讲目标

本讲从 `typst` 命令的程序入口出发，带你走完「命令行输入 → 解析 → 分发到子命令 → 处理结果与退出码」的完整一程。学完后你应该能够：

- 说清 `main()` 的三段式流程：SIGPIPE 重置、`dispatch()` 执行、错误打印与退出码返回。
- 理解 `ARGS` 为什么用 `LazyLock` 惰性解析，以及用户「没输入子命令」时如何触发首次问候 `greet()`。
- 解释应用级错误 / 提示（error / hint）的彩色打印方式。
- 讲清 `thread_local EXIT` 退出码机制，以及为什么子命令能「返回成功」却让进程以失败码退出。
- 理解 `greet()` 如何做到「每个版本只问候一次」，以及在 Windows 上为何要暂停等待。

## 2. 前置知识

本讲承接 u1-l1：你已经知道 typst-cli 是产出 `typst` 命令的**二进制 crate**，核心编译逻辑在 `typst` crate 里，CLI 是一层「薄壳」。这里再补充几个读懂 Rust 入口代码需要的基础概念：

- **进程退出码（exit code）**：程序结束时返回给操作系统的一个数字。约定上 `0` 表示成功（`ExitCode::SUCCESS`），非 `0` 表示失败（`ExitCode::FAILURE`）。Shell 脚本和 CI 用 `$?` 读取它来判断命令是否成功。
- **`Result<T, E>` 与 `?` 运算符**：`Result` 是「可能失败」的返回类型，`Ok(v)` 表示成功、`Err(e)` 表示失败。`?` 写在表达式后面，表示「如果是 `Err` 就立刻把这个错误向上返回（冒泡）」。
- **`LazyLock<T>`**：Rust 标准库提供的「惰性全局变量」。声明时不初始化，**第一次被访问时**才执行初始化闭包，之后一直复用结果。
- **`thread_local!`**：声明「每个线程各有一份独立副本」的存储。本讲的退出码就存在一个线程局部变量里。
- **`!`（never 类型）**：函数返回 `!` 表示它「永不正常返回」，比如一定会调用 `process::exit()` 直接结束进程。

后面遇到时我会再结合代码点明。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/main.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs) | 程序入口。包含模块声明、`ARGS` 惰性解析、`main()`、`dispatch()`、`set_failed()`、`print_error()`、`print_hint()` |
| [src/greet.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/greet.rs) | 首次问候逻辑。包含欢迎文本 `GREETING`、`greet()`、`print_and_exit()`、`pause()` |

本讲会顺带引用两个相关文件来佐证机制：[src/args.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs) 的 `Command` 枚举，以及 [src/compile.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs) 中对 `set_failed()` 的调用。

## 4. 核心概念与源码讲解

### 4.1 main() 的执行流程：SIGPIPE、dispatch、退出码

#### 4.1.1 概念说明

`main()` 是二进制的入口函数。它本身并不「懂」任何 Typst 业务，它只做三件事：做一点系统层面的准备、把控制权交给「命令分发器」、最后根据结果决定打印什么并返回退出码。

这里有两个系统层面的概念值得先讲清楚：

- **SIGPIPE**：当你把 typst 的输出通过管道交给另一个命令（例如 `typst compile … | head`），而下游提前关闭了管道，操作系统会向 typst 发送 `SIGPIPE` 信号。Rust 程序默认行为有时会让进程带着 panic 退出，看起来像崩溃。`sigpipe::reset()` 把这个信号恢复成「正常忽略」的默认状态，避免这种无谓的报错。
- **退出码**：见前置知识。`main()` 的返回值 `ExitCode` 会成为整个进程的退出码。

#### 4.1.2 核心流程

`main()` 的执行可以画成下面这条流水线：

```
main() 开始
  1. sigpipe::reset()              ← 处理 SIGPIPE 管道信号
  2. res = dispatch()              ← 执行子命令，返回 HintedStrResult<()>
  3. 若 res 是 Err:
       set_failed()                ← 把退出码标记为 FAILURE
       print_error(主错误信息)
       对每条 hint: print_hint(提示)
  4. 返回 EXIT 线程局部变量的值      ← 这就是进程退出码
```

这里有一个关键设计：`main()` 只看 `dispatch()` 的返回值是 `Ok` 还是 `Err` 来决定要不要打印错误。但是——**有些子命令即使遇到了失败，也会自己把错误打印好，然后返回 `Ok(())`**（典型的就是编译失败时仍要把诊断信息打印出来）。为了让这种「软失败」也能反映到进程退出码上，这些子命令会自己调用 `set_failed()` 把退出码标记为失败。这就是为什么退出码要存在一个可被各处改写的全局位置（`EXIT`），而不是简单地由 `main()` 的返回值决定。

#### 4.1.3 源码精读

`main()` 函数本体如下，三段式结构一目了然：

[src/main.rs:49-66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L49-L66) —— 程序入口：`sigpipe::reset()` 处理管道信号；调用 `dispatch()`；若返回 `Err` 则 `set_failed()` 标记失败、用 `print_error` 打印主信息、用 `print_hint` 逐条打印提示；最后返回 `EXIT` 线程局部变量里存的退出码。

注意 `HintedStrResult` 是 typst 自定义的错误类型：一条错误 = 一条主消息（`message()`）+ 零到多条提示（`hints()`）。所以第 57–62 行才会先打印 message，再循环打印每一条 hint。

退出码本身存在这个线程局部变量里，初始值是 `SUCCESS`：

[src/main.rs:34-37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L34-L37) —— `EXIT` 是一个 `thread_local` 的 `Cell<ExitCode>`，初值为 `ExitCode::SUCCESS`。`Cell` 提供内部可变性，使得 `set_failed()` 能在别处改写它，而 `main()` 第 65 行 `EXIT.with(|cell| cell.get())` 读出最终值作为进程退出码。

#### 4.1.4 代码实践

**实践目标**：用伪代码画出 `main()` 的控制流，并验证退出码来源。

**操作步骤**：
1. 打开 [src/main.rs:49-66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L49-L66)，按 4.1.2 的流水线手绘一张流程图。
2. 在 `crates/typst-cli` 目录构建：`cargo build`。
3. 故意触发一个「应用级错误」——编译一个不存在的输入文件：`./target/debug/typst compile nope.typ; echo $?`。

**需要观察的现象**：
- 终端会打印一行红色的 `error: ...`（来自 `print_error`），可能还跟着 `hint: ...`。
- `echo $?` 输出的退出码应该是非 0（FAILURE）。

**预期结果**：退出码为 `1`。这条路径走的是 `dispatch()` 返回 `Err` → `main()` 第 58 行 `set_failed()`，最终 `EXIT` 被读出为 FAILURE。

#### 4.1.5 小练习与答案

**练习 1**：假设某个子命令执行时内部调用了 `set_failed()`，但最后向 `dispatch()` 返回的是 `Ok(())`。`main()` 最终返回什么退出码？

> **答案**：返回 `FAILURE`。因为 `main()` 第 65 行返回的是 `EXIT` 线程局部变量的当前值，而 `set_failed()` 已经把它改写成 `FAILURE`。`main()` 自己并没有感知到这次「软失败」，但退出码是对的。

**练习 2**：如果把 `sigpipe::reset()` 这一行删掉，在什么场景下用户会看到「莫名其妙」的报错？

> **答案**：在把 typst 输出通过管道传给会提前退出的下游命令时（如 `typst compile … | head`），可能触发 `SIGPIPE`，导致进程异常退出或打印 panic 信息。`sigpipe::reset()` 就是为了避免这种情况。

---

### 4.2 ARGS 惰性解析与命令分发

#### 4.2.1 概念说明

`dispatch()` 是「命令分发器」：它读出用户选了哪个子命令，然后调用对应模块的处理函数。u1-l1 已经把它描述成一张「子命令 → 处理函数」的映射表，本讲深入它的两个底层细节：参数何时被解析，以及分发的不对称性。

`ARGS` 是被 `LazyLock` 包装的全局变量，类型是 `CliArguments`（用 clap 派生宏定义的、与命令行参数一一对应的结构）。`LazyLock` 的意义：

- **惰性**：声明时不解析，第一次被访问时才运行初始化闭包（即用 clap 解析命令行）。
- **只解析一次**：整个进程生命周期内，后续访问都复用第一次的结果。

在本讲场景里，`dispatch()` 第一次读 `ARGS.command` 时就会触发解析。

#### 4.2.2 核心流程

```
ARGS 第一次被访问（dispatch 读 ARGS.command 时）
  → LazyLock 闭包执行: CliArguments::try_parse()
      成功                     → 返回解析结果
      失败，且是 "缺少子命令/请求帮助":
          greet()              ← 可能打印欢迎信息并直接 exit
      error.exit()             ← 打印帮助/错误信息并 exit 进程

dispatch()
  match ARGS.command {
    Compile    → compile::compile(...)   ?
    Watch      → watch::watch(...)       ?
    Init       → init::init(...)         ?
    Query      → query::query(...)       ?
    Eval       → eval::eval(...)         ?
    Fonts      → fonts::fonts(...)       （无 ?）
    Update     → update::update(...)     ?
    Completions→ completions::completions(...) （无 ?）
    Info       → info::info(...)         ?
  }
  Ok(())
```

两个要点需要特别留意：

1. **「无子命令」时进程在解析阶段就结束了**。如果用户直接敲 `typst`（不带子命令），clap 会返回一个 `DisplayHelpOnMissingArgumentOrSubcommand` 错误，于是触发 `greet()`，随后 `error.exit()` 直接结束进程。这意味着此时 `main()` 第 55 行 `dispatch()` 根本不会正常返回——进程在 `ARGS` 初始化阶段就已经 exit 了。
2. **各子命令的返回类型不对称**。带 `?` 的命令（compile、watch、init、query、eval、update、info）返回 `Result`，`?` 把它们的错误冒泡给 `main()`；而 `Fonts` 和 `Completions` 这两支没有 `?`，它们返回非 `Result` 类型，自己处理一切（包括必要时调用 `set_failed()`）。

#### 4.2.3 源码精读

`ARGS` 的惰性解析定义如下：

[src/main.rs:39-47](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L39-L47) —— `ARGS` 用 `LazyLock::new` 包裹 `CliArguments::try_parse()`。若解析出错，且错误种类是 `DisplayHelpOnMissingArgumentOrSubcommand`（缺少子命令或请求帮助），先调用 `crate::greet::greet()`；随后 `error.exit()` 打印帮助/错误并结束进程。只有解析成功时，`ARGS` 才真正持有一份可用的 `CliArguments`。

`dispatch()` 把 9 个 `Command` 变体分发到对应模块：

[src/main.rs:68-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L68-L82) —— `match &ARGS.command`，9 个分支对应 9 个子命令处理函数。注意 `Fonts` 与 `Completions` 这两支不带 `?`（直接返回），其余 7 支都用 `?` 把错误冒泡。

这 9 个分支与 `Command` 枚举一一对应：

[src/args.rs:81-112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L81-L112) —— `Command` 枚举定义了 9 个变体：`Compile` / `Watch` / `Init` / `Query` / `Eval` / `Fonts` / `Update` / `Completions` / `Info`。其中 `Query` 标了 `hide = true`（已弃用，默认 `--help` 里不显示）；`Update` 在未启用 `self-update` feature 时也 `hide = true`。这与 u1-l1 讲到的「默认 feature 下 query 与 update 被隐藏」一致。

关于 `update` 还有一个小细节：当 `self-update` feature **没**启用时，main.rs 顶部那个 `mod update` 不会编译，而是改用文件末尾的回退实现：

[src/main.rs:134-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L134-L147) —— `#[cfg(not(feature = "self-update"))]` 下的 `mod update`，其 `update()` 直接 `bail!`，提示「此可执行文件未启用自更新，请用包管理器更新」。这样 `dispatch()` 里 `crate::update::update(command)` 在任何 feature 组合下都能编译通过。

#### 4.2.4 代码实践

**实践目标**：验证「无子命令」时的两种行为，并对照 `--help` 与 `Command` 枚举。

**操作步骤**：
1. 运行 `./target/debug/typst --help`，把列出的**可见**子命令逐一对应到 [src/args.rs:81-112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L81-L112) 的变体。
2. 不带任何参数运行 `./target/debug/typst`（第一次运行），观察输出是「欢迎信息」还是「标准帮助」。
3. 再次运行 `./target/debug/typst`，对比输出变化（参见 4.4 的实践）。

**需要观察的现象**：`--help` 里**看不到** `query`（已隐藏）；首次无参数运行会打印欢迎信息，第二次会变成标准帮助。

**预期结果**：可见子命令为 compile / watch / init / eval / fonts / completions / info（以及启用 self-update 时的 update）。具体「首次 vs 再次」的差异需要本地验证（见 4.4）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Fonts` 这一支在 `dispatch()` 里没有 `?`？

> **答案**：因为 `fonts::fonts()` 的返回类型不是 `Result`（它返回 `()`），自己内部完成所有工作（包括必要时调用 `set_failed()`）。`?` 只能作用于 `Result`，所以这里直接调用、不需要 `?`。其余返回 `Result` 的命令用 `?` 把错误冒泡给 `main()`。

**练习 2**：用户敲 `typst`（无参数）时，`dispatch()` 会被执行吗？

> **答案**：不会正常执行到函数体。在 `dispatch()` 第一次访问 `ARGS` 时触发惰性解析，clap 判定「缺少子命令」，先调用 `greet()`，再 `error.exit()` 结束进程。所以 `main()` 第 55 行 `dispatch()` 不会正常返回。

---

### 4.3 应用级错误 / 提示打印 与 EXIT 退出码机制

#### 4.3.1 概念说明

typst CLI 把「打印给用户看的红字」分成两类：

1. **编译诊断（compile diagnostics）**：来自源码文件，带有文件名、行号、代码片段，由 `codespan-reporting` 库排版（这部分在 u2-l4 详讲）。
2. **应用级错误 / 提示（application-level error / hint）**：与源码文件无关，例如「找不到字体目录」「网络下载失败」「输入文件不存在」。这些由本讲的 `print_error()` / `print_hint()` 打印，格式简单：彩色的 `error: <消息>` 与 `hint: <消息>`。

而「退出码」机制（`EXIT` + `set_failed()`）则把上面两类失败统一反映到进程退出码上。如 4.1 所述，`set_failed()` 不只是 `main()` 在用，`compile.rs`、`query.rs`、`eval.rs`、`info.rs` 等子命令都会通过 `crate::set_failed` 调用它——它们能在「向 dispatch 返回 `Ok(())`」的同时，把退出码标记为失败。

> 小知识：`set_failed()` 在 main.rs 里没有加 `pub`，为什么别的模块能用 `crate::set_failed` 访问？因为 Rust 的可见性规则是「私有项可被其所在模块的后代模块访问」。main.rs 是 crate 根，所有子模块都是它的后代，所以都能访问这个「根级私有」函数。

#### 4.3.2 核心流程

```
子命令内部遇到「软失败」（如编译有错，但仍想打印诊断、输出部分结果）:
  set_failed()        ← 标记 EXIT = FAILURE
  打印诊断 / 输出
  return Ok(())       ← 不冒泡错误给 main

main 收到 Ok → 不打印 error
但 EXIT 已是 FAILURE → 进程退出码非 0
```

这样的好处：在 CI 里用退出码判断成败时，即使 typst 已经尽力产出了部分结果（例如带警告地生成了 PDF），只要存在问题，退出码就是非 0，CI 步骤会正确地「失败」。

#### 4.3.3 源码精读

`set_failed()` 把线程局部退出码改成 `FAILURE`：

[src/main.rs:84-87](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L84-L87) —— `set_failed()` 通过 `EXIT.with(|cell| cell.set(ExitCode::FAILURE))` 把退出码标记为失败。

`print_error()` 打印彩色的 `error: <消息>`：

[src/main.rs:89-99](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L89-L99) —— 取 `codespan_reporting` 的默认 `term::Styles`，用 `header_error` 颜色风格写出红色的 `error`，再 `reset()` 后写 `: <msg>`。输出走 `terminal::out()`（terminal.rs 提供的统一终端输出抽象，会自动在非彩色终端禁用颜色，u2-l4 详讲）。

`print_hint()` 同理，只是颜色风格换成 `header_help`：

[src/main.rs:101-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L101-L111) —— `print_hint()` 与 `print_error()` 结构完全一样，用 `styles.header_help` 上色写出 `hint: <msg>`。

最有说服力的「软失败」例子在 compile.rs：编译失败时，`compile_once` 不会把错误冒泡，而是自己打印诊断、并把退出码标记为失败：

[src/compile.rs:295-304](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L295-L304) —— `Err(errors)` 分支里先 `set_failed()`，再打印错误诊断。注意它没有 `return Err(...)`，而是把诊断打印完就走完函数返回 `Ok(())`。这就是「返回成功、退出码失败」的典型场景。

#### 4.3.4 代码实践

**实践目标**：区分「硬失败」（冒泡 `Err`）与「软失败」（`set_failed` + 返回 `Ok`）两种退出码非 0 的路径。

**操作步骤**：
1. **硬失败**：运行 `./target/debug/typst compile nope.typ; echo $?`。这条路径走 `dispatch` 返回 `Err` → `main` 调 `set_failed()`。
2. **软失败**：写一个带语法错误的 `bad.typ`（例如内容就是 `#let x =` 这种不完整的语句），运行 `./target/debug/typst compile bad.typ; echo $?`。这条路径走 compile 内部 `set_failed()`（见 [src/compile.rs:295-304](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L295-L304)），然后返回 `Ok(())`。

**需要观察的现象**：两种情况下退出码都应该是非 0，但打印的「错误外观」不同——硬失败是简短的应用级 `error:` 行，软失败是带文件名/行号/代码片段的编译诊断。

**预期结果**：两次 `echo $?` 都输出 `1`。

#### 4.3.5 小练习与答案

**练习 1**：`print_error` 打印的 `error` 二字的颜色是从哪里来的？在不支持颜色的环境（如管道重定向到文件）下会怎样？

> **答案**：颜色来自 `codespan_reporting::term::Styles::default().header_error`，经 `terminal::out()` 输出。`terminal::out()` 会检测终端是否支持颜色，不支持时自动禁用，所以重定向到文件时不会出现颜色转义码。

**练习 2**：为什么需要 `set_failed()` 这种「全局可写退出码」，而不是让所有失败都通过 `return Err(...)` 冒泡？

> **答案**：因为有些子命令（如 compile）在失败时仍要完成副作用——打印诊断、在 watch 模式下更新状态、甚至输出部分产物——然后才返回。如果用 `Err` 冒泡，这些收尾动作就要全部塞进 `main()`，职责会变得混乱。用 `set_failed()` 让子命令自己掌握「打印 + 标记失败」的完整流程，同时保证退出码正确。

---

### 4.4 greet：首次问候、版本标记与 Windows 暂停

#### 4.4.1 概念说明

`greet()` 处理「用户第一次敲 `typst` 却没给子命令」的场景。它**不是每次都打印**欢迎信息，而是「每个版本只问候一次」。实现办法是在操作系统规定的「应用数据目录」里写一个标记文件，内容是当前版本号。

- **数据目录**：`dirs::data_dir()` 返回各操作系统规定的应用数据目录。Linux 上类似 `~/.local/share`，macOS 上类似 `~/Library/Application Support`，Windows 上类似 `%LOCALAPPDATA%`。typst 在其下建 `typst/greeted` 文件。
- **版本号**：通过 `typst::utils::version().raw()` 拿到当前版本的字符串。只要版本变了，标记文件内容就对不上，就会重新问候。

#### 4.4.2 核心流程

```
greet():
  data_dir = dirs::data_dir()      ← 取不到就直接返回（不问候）
  path = data_dir/typst/greeted
  version = typst 当前版本字符串
  prev = 读取 path 的内容（读取失败当 None）
  若 prev == Some(version):        ← 这个版本已经问候过
      return                       ← 什么都不做，回到 ARGS 闭包继续 error.exit()
  否则:
      把 version 写入 path
      print_and_exit(GREETING)     ← 打印欢迎信息并直接 exit 进程

print_and_exit(message):           ← 返回类型 `!`（永不返回）
  借用 clap 做约 80 列自动换行排版
  打印信息
  Windows 上调用 pause()           ← 等用户按回车，防止双击 exe 终端闪退
  std::process::exit(err.exit_code())
```

两个巧妙之处：

1. **每版本只问候一次**：靠比较标记文件内容与当前版本号实现。升级版本后会自动再问候一次。
2. **复用 clap 做排版**：`print_and_exit()` 不自己写「按 80 列换行」的逻辑，而是构造一个临时的 clap `Command`，把欢迎信息塞进 `about`，借用 clap 的帮助排版能力自动换行（见源码 4.4.3）。

#### 4.4.3 源码精读

欢迎信息本体（用 `color_print::cstr!` 宏内嵌颜色与样式标签）：

[src/greet.rs:3-24](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/greet.rs#L3-L24) —— `GREETING` 常量。其中 `<s>` 表示加粗、`<u>` 表示下划线、`<c!>` 表示高亮命令，列出了最常用的三条命令（compile / watch / init）和教程、模板、论坛链接。

`greet()` 的「每版本只问候一次」逻辑：

[src/greet.rs:26-39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/greet.rs#L26-L39) —— 取数据目录，拼出 `typst/greeted` 路径；读取该文件，若内容等于当前版本号就直接 `return`（已问候过）；否则把当前版本号写入该文件，再 `print_and_exit(GREETING)`。注意所有文件操作都用 `.ok()` / `.unwrap()` 容错——问候机制失败不应影响主流程。

`print_and_exit()` 借 clap 排版并退出：

[src/greet.rs:41-59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/greet.rs#L41-L59) —— 返回类型是 `!`（永不返回）。构造一个 `max_term_width(80)`、`help_template("{about}")` 的 clap `Command`，把 `message` 放进 `about`，再用 `try_get_matches_from(["typst", "--help"])` 触发 clap 的「请求帮助」错误路径，从而拿到经过 80 列换行排版的文本并打印。Windows 下额外调用 `pause()` 等待用户按回车，最后 `std::process::exit()` 结束进程。

`pause()` 仅在 Windows 上有意义：

[src/greet.rs:61-67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/greet.rs#L61-L67) —— 打印「Press enter to continue...」并阻塞读 stdin。注释解释：Windows 用户可能是双击 `.exe` 启动的，问候信息一闪而过没机会看清，所以等一个回车再退出。

#### 4.4.4 代码实践

**实践目标**：亲手验证「每版本只问候一次」的标记机制。

**操作步骤**：
1. 第一次运行 `./target/debug/typst`（不带参数），看到欢迎信息。
2. 找到标记文件：Linux 上是 `~/.local/share/typst/greeted`（macOS / Windows 路径见 4.4.1）。用 `cat` 查看其内容——应该是一串版本号。
3. 再次运行 `./target/debug/typst`，这次打印的是**标准帮助**（来自 `ARGS` 闭包里的 `error.exit()`），而不是欢迎信息。
4. 删除标记文件 `rm ~/.local/share/typst/greeted`，再运行 `./target/debug/typst`，欢迎信息会再次出现。

**需要观察的现象**：标记文件存在且内容=版本号时只显示帮助；删除后重新显示欢迎信息。

**预期结果**：步骤 3 看到标准帮助；步骤 4 重新看到欢迎信息。不同操作系统的数据目录路径可能不同，需在本地确认（macOS / Windows 路径见 4.4.1）。

#### 4.4.5 小练习与答案

**练习 1**：`greet()` 如何判断「这个版本已经问候过」？

> **答案**：读取数据目录下 `typst/greeted` 文件的内容（`prev_greet`），与 `typst::utils::version().raw()` 得到的当前版本字符串比较。二者相等就认为已问候过，直接返回；不等则写入新版本号并打印欢迎信息。

**练习 2**：`print_and_exit()` 的返回类型为什么是 `!`？为什么它要在 Windows 上调用 `pause()`？

> **答案**：返回 `!` 是因为它最后一定调用 `std::process::exit()` 结束进程，永远不会正常返回。Windows 上调用 `pause()` 是因为用户可能双击 `.exe` 启动 typst，终端窗口会在进程退出后立刻关闭，欢迎信息一闪而过；`pause()` 等用户按回车，给其阅读机会。

## 5. 综合实践

把本讲的知识串成一张完整的控制流图，并动手在 `dispatch()` 里加一行调试日志验证你对分发过程的理解。

**实践目标**：画图 + 动手改一行 + 重新构建验证。

**操作步骤**：
1. **画图**：在纸上或文档里画出从「命令行 argv」到「进程退出」的完整流程：
   ```
   argv
     → ARGS 惰性解析（CliArguments::try_parse）
         ├─ 缺少子命令 → greet()（首次打印欢迎 / 再次走 error.exit 打印帮助）→ 进程结束
         └─ 解析成功
     → dispatch() 按 ARGS.command 分发到 9 个子命令之一
         ├─ 硬失败：返回 Err → main 调 set_failed + print_error/hint
         ├─ 软失败：子命令内部 set_failed + 打印诊断，返回 Ok
         └─ 成功：返回 Ok
     → main 返回 EXIT 线程局部变量 → 进程退出码
   ```
2. **改一行**：编辑本地副本 [src/main.rs:68-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L68-L82)，在 `Command::Fonts(command) => crate::fonts::fonts(command),` 这一行**前面**加一行调试输出：
   ```rust
   Command::Fonts(command) => {
       eprintln!("[debug] dispatching fonts");
       crate::fonts::fonts(command)
   }
   ```
3. **重新构建并运行**：在 `crates/typst-cli` 下 `cargo build`，然后运行 `./target/debug/typst fonts > /dev/null`（把正常输出丢到 `/dev/null`，只看 stderr）。

**需要观察的现象**：stderr 里出现 `[debug] dispatching fonts`，且 `fonts` 的正常字体列表仍照常输出到 stdout、不受影响。

**预期结果**：每运行一次 `typst fonts`，`[debug] dispatching fonts` 出现一次——这验证了 `dispatch()` 确实按 `Command::Fonts` 这一支进入了 `fonts::fonts`。

> ⚠️ 这是修改**本地副本**用于学习观察，请勿提交。本讲禁止修改源码，此改动仅在你的工作副本中用于验证理解，验证后请还原。

> 备注：若你无法本地构建（缺少 Rust 工具链或网络依赖），上述运行结果标注为「待本地验证」。你仍然可以完成「画图」与「阅读源码」部分。

## 6. 本讲小结

- `main()` 是三段式：`sigpipe::reset()` 处理管道信号 → `dispatch()` 执行子命令 → 按 `Ok/Err` 打印错误并返回 `EXIT` 退出码。
- `ARGS` 用 `LazyLock` 惰性解析，整个进程只解析一次；用户「没给子命令」时触发 `greet()`，随后 `error.exit()` 结束进程。
- `dispatch()` 用 `match` 把 9 个 `Command` 变体分发到对应模块；其中 `Fonts` / `Completions` 不带 `?`，其余 7 支用 `?` 冒泡错误。
- 退出码来自 `thread_local EXIT`：`set_failed()` 把它改成 `FAILURE`，且被 compile / query / eval / info 等多个子命令复用，实现「返回 `Ok(())` 但退出码非 0」的软失败。
- `print_error` / `print_hint` 打印与源码无关的应用级错误 / 提示，颜色取自 `codespan_reporting` 的默认样式，经 `terminal::out()` 输出。
- `greet()` 靠数据目录下的 `typst/greeted` 标记文件做到「每版本只问候一次」；Windows 上额外 `pause()` 防止双击启动时终端闪退。

## 7. 下一步学习建议

到这里你已经掌握了 `typst` 命令「从入口到分发」的骨架。下一讲 **u1-l3 命令行参数模型** 会深入 [src/args.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs)，讲清楚 `CliArguments` / `Command` / 各子命令参数结构如何用 clap 派生宏定义，以及 `Input` / `Output` 枚举、`OutputFormat` / `Pages` 等自定义值解析器的工作方式。

理解完参数模型，就可以进入第二单元 **u2 核心编译流水线**：从 `SystemWorld`（CLI 与编译器核心的桥梁）开始，逐步走到 `compile` / 导出 / 诊断 / `watch`。建议在进入 u2 之前，先把本讲的「综合实践」流程图亲手画一遍，它会成为后续阅读的导航图。
