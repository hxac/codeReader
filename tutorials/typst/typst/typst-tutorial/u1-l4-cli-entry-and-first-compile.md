# 第 4 讲：CLI 入口——从命令行到一次编译的调用链

> 所属单元：单元 1「初识 Typst 与整体架构」
> 前置讲义：u1-l1（认识 Typst 与仓库文档）、u1-l2（构建工作区并跑通第一个编译）、u1-l3（仓库全景：17 个 crate 的分工地图）
> 本讲引用源码的版本：`64c5e8c02fe0394d913ead36e42ef8c1334d2107`

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 [crates/typst-cli/src/main.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/main.rs) 的模块组织方式，说出「一个子命令对应一个文件」的布局规律，并追踪一条命令从 `main()` 到具体子命令模块的分发路径。
2. 描述 `typst compile` 从解析命令行参数，到组装编译环境，再到调用 `typst::compile` 写出 PDF 的完整主干调用链。
3. 理解 [crates/typst-cli/src/args.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs) 中「声明式参数定义」的写法：Rust 的 struct/enum 如何通过 clap 的派生宏直接变成命令行接口。
4. 知道 `watch`、`query`、`eval`、`init` 等子命令在 CLI 中的存在与位置（`watch` 模块内部机制本讲不展开，留待后续讲义，标注待确认）。

承接 u1-l3 的比喻：上一讲我们看的是「图书馆」——17 个 crate 各自分管什么；本讲我们看「前台」——用户敲下一行命令，前台如何接待、查证、然后把任务递给图书馆完成一次编译。

## 2. 前置知识

本讲需要以下概念，用通俗语言逐一解释：

- **子命令式 CLI（命令行接口）**：像 `git commit`、`cargo build` 那样，程序名后面第一个词决定「做什么」。Typst 的写法是 `typst compile`、`typst watch` 等。
- **Rust 模块系统**：一个文件 `src/args.rs` 通过 `mod args;` 声明后，就成为 crate 内一个模块，用 `crate::args::...` 访问。`main.rs` 顶部的 `mod` 声明就是本 crate 的「目录页」。
- **clap 与派生宏（derive macro）**：clap 是 Rust 生态中最常用的参数解析库。它的思路是「数据结构即接口」——给 struct 加上 `#[derive(Parser)]`，字段名和文档注释就自动变成命令行参数和帮助文本，不需要手写解析代码。本讲会反复看到这种写法。
- **`LazyLock`（惰性初始化）**：`static ARGS: LazyLock<CliArguments>` 表示「这个全局变量第一次被用到时才初始化一次」。CLI 全程只需要解析一次参数，用它正合适。
- **`thread_local!` 与 `ExitCode`**：`ExitCode` 是 `main` 函数的返回类型，表示进程退出码（0 成功、非 0 失败）。因为子命令函数分散在各个模块里，Typst 用一个线程局部变量 `EXIT` 存放「最终的退出码」，结束时取出返回。
- **turbofish 语法 `::<T>`**：Rust 中显式指定泛型参数的写法，如 `typst::compile::<PagedDocument>(world)`，意思是「这次编译要产出分页文档（PagedDocument）」。
- **前置讲义的衔接**：u1-l2 中你已经用 `cargo run -p typst-cli -- compile` 编译过文件；u1-l3 中你知道了编译器核心流程 parse → eval → realize → layout → export 分布在多个 crate 中，也知道了编译器需要一个 `World`（对「读文件、找字体、取当前时间」的抽象）。本讲正是把这两点接起来：CLI 如何组装出一个 `World` 并启动那条流水线。

## 3. 本讲源码地图

本讲的主角是 `typst-cli` 这个二进制 crate 中的三个文件：

| 文件 | 行数（本版本） | 作用 |
| --- | --- | --- |
| [crates/typst-cli/src/main.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/main.rs) | 147 | 入口：声明全部子模块；解析一次命令行参数；把命令分发给对应模块；处理错误打印与退出码 |
| [crates/typst-cli/src/args.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs) | 894 | 全部命令行参数的「类型定义」：子命令枚举、各命令的参数结构体、输入/输出/格式的枚举与自定义解析 |
| [crates/typst-cli/src/compile.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs) | 约 800 | `compile`（以及 `watch` 复用的）编译主干：预处理配置 → 组装 World → 调 `typst::compile` → 导出文件 → 打印诊断 |

顺带认识 `main.rs` 里声明的其余模块（本讲只需知道存在与职责，除 `world` 外均不深入）：

| 模块（均在 `crates/typst-cli/src/` 下） | 对应子命令/职责 |
| --- | --- |
| `watch.rs` | `typst watch`：监听文件变化并重复编译（内部机制待确认，后续讲义） |
| `init.rs` | `typst init`：从模板初始化项目 |
| `query.rs` | `typst query`（已隐藏、被 `eval` 取代的旧命令） |
| `eval.rs` | `typst eval`：求值一段 Typst 代码 |
| `fonts.rs` | `typst fonts`：列出发现的字体 |
| `update.rs` | `typst update`：自更新（需 `self-update` feature） |
| `completions.rs` | `typst completions`：生成 shell 补全脚本 |
| `info.rs` | `typst info`：显示环境与默认值等调试信息 |
| `world.rs` | `SystemWorld`：u1-l3 提到的 `World` trait 在 CLI 侧的实现（下一讲主角） |
| `greet.rs` | 无参数时打印的欢迎信息 |
| `terminal.rs`、`download.rs`、`packages.rs`、`deps.rs` | 终端封装、网络下载、包存储、依赖文件写出等支撑设施 |

一个直接可感的规律：**`Command` 枚举的每个变体，几乎都对应 `src/` 下一个同名小写文件**。记住这个规律，读任何子命令都有入口。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：4.1 main.rs 的模块声明与命令分发；4.2 args.rs 的参数定义；4.3 compile.rs 的编译主干。三者恰好构成调用链的前三站。

### 4.1 main.rs：模块声明与命令分发

#### 4.1.1 概念说明

`main.rs` 是整个 `typst` 可执行文件的入口，但它刻意做得极薄：**不做任何编译工作，只做「接待」**。它解决三个问题：

1. 这个 crate 由哪些文件组成？（`mod` 声明一览）
2. 用户想干什么？（解析一次参数，存成全局的 `ARGS`）
3. 干完之后进程该以什么状态退出？（`EXIT` 线程局部变量 + 错误打印）

为什么要薄？因为 CLI 只是 u1-l3 讲过的那座「图书馆」（`typst` 库 crate 及其伙伴）的一个消费者。入口保持薄，业务逻辑才能沉到可复用的模块里——`compile.rs` 里的 `compile_once` 同时被 `compile` 和 `watch` 两个子命令复用，就是这种设计带来的好处（4.3 会看到）。

#### 4.1.2 核心流程

`typst compile hello.typ` 被执行时，`main.rs` 一侧的流程：

```text
进程启动
  └─ main()                          [main.rs:L49]
       ├─ sigpipe::reset()           恢复 SIGPIPE 默认行为（管道场景）
       ├─ dispatch()
       │    ├─ 访问 ARGS（此刻才真正解析命令行）
       │    │    ├─ 解析成功 → 得到 CliArguments（内含 Command::Compile(...)）
       │    │    └─ 解析失败 → 打印错误 / 无参数时 greet 后退出
       │    └─ match ARGS.command
       │         └─ Command::Compile(c) → crate::compile::compile(c)?   ← 进入 4.3
       ├─ 若返回 Err：set_failed() + print_error + 逐条 print_hint
       └─ 返回 EXIT 中保存的退出码
```

#### 4.1.3 源码精读

**模块目录页。** 文件开头一口气声明了全部 17 个子模块，每个子命令一个文件，一行一个：

[crates/typst-cli/src/main.rs:L1-L17](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/main.rs#L1-L17) 这段代码声明了 args、compile、watch、world 等全部子模块；注意 L14-L15 的 `#[cfg(feature = "self-update")] mod update;`——`update` 模块只在编译时启用 `self-update` feature 才存在，下面马上会看到没启用时的替代品。

**参数只在用到时解析一次。**

[crates/typst-cli/src/main.rs:L39-L47](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/main.rs#L39-L47) 这段代码定义全局 `ARGS`：用 `LazyLock` 包住 `CliArguments::try_parse()`，首次访问时解析一次命令行；解析出错时（比如用户什么都没敲）如果是「缺少参数或子命令」这种情形，先调用 `crate::greet::greet()` 打印欢迎信息，再 `error.exit()` 打印错误并以对应码退出。

**退出码的中转站。**

[crates/typst-cli/src/main.rs:L34-L37](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/main.rs#L34-L37) 这段代码用一个 `thread_local` 的 `Cell<ExitCode>` 存退出码，初值 `SUCCESS`。子命令深处出错时调用 `set_failed()` 把它改成 `FAILURE`，而无需把「退出码」一路层层返回。

**主函数本体。**

[crates/typst-cli/src/main.rs:L49-L66](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/main.rs#L49-L66) 这段代码是入口 `main`：先 `sigpipe::reset()`（Rust 默认忽略 SIGPIPE，会导致 `typst ... | head` 这类管道用法时打印错误而非安静退出，这里恢复 Unix 默认行为）；然后 `dispatch()` 执行命令；失败时红字打印错误与提示（hints）；最后从 `EXIT` 取出退出码返回。

**分发函数：一表定乾坤。**

[crates/typst-cli/src/main.rs:L68-L82](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/main.rs#L68-L82) 这段代码是 `dispatch`：对 `ARGS.command` 做 `match`，每个 `Command` 变体一行，直接转调对应模块的同名函数，例如 `Command::Compile(command) => crate::compile::compile(command)?`。要找任何子命令的实现，从这张表跳过去就行。

**feature 关闭时的「占位模块」。**

[crates/typst-cli/src/main.rs:L134-L147](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/main.rs#L134-L147) 这段代码是 `#[cfg(not(feature = "self-update"))]` 下的内联 `mod update`：当自更新 feature 未启用时，用这个占位实现顶替真实模块，调用即 `bail!` 报「此可执行文件未启用自更新」。结合 L14-L15 可以看到条件编译的对称用法：真实模块与占位模块恰好互补。

#### 4.1.4 代码实践

**实践 A：用 `--help` 验证「数据结构即接口」。**

1. **实践目标**：确认帮助文本确实由 args.rs 里的 struct/enum 生成，建立「改 struct 就能改 CLI」的直觉。
2. **操作步骤**（在仓库根目录，u1-l2 已配好 Rust 工具链）：
   - `cargo run -p typst-cli -- --help`
   - `cargo run -p typst-cli -- compile --help`
   - `cargo run -p typst-cli -- --version`
3. **需要观察的现象**：第一条应列出全部子命令及别名（`c`、`w`），末尾有 Tutorial/Reference 等资源链接；第二条应列出 `compile` 的全部选项，包括 `--format/-f`、`--pages`、`--font-path` 等；第三条打印版本号与提交哈希。
4. **预期结果**：帮助内容与 4.2.3 讲解的 `Command` 枚举和 `CompileArgs` 字段一一对应；`query` 不会出现在帮助里（它被 `hide` 了）。
   以上命令行为**待本地验证**（本讲义编写环境未执行构建）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ARGS` 用 `LazyLock`（惰性初始化）而不是在 `main()` 开头直接 `let args = CliArguments::parse();`？
**参考答案**：全局惰性初始化让任何模块都能通过 `ARGS` 访问已解析参数（如颜色选择），又保证只解析一次；同时解析失败的错误处理集中在一处（含 greet 逻辑）。如果放在 `main()` 里解析，就得把 `args` 手动传给所有需要的函数，或者在模块间层层透传，代码更啰嗦。

**练习 2**：`main()` 里为什么要调用 `sigpipe::reset()`？
**参考答案**：Rust 运行时默认将 SIGPIPE 设为忽略，这样当 stdout 接到 `| head` 这类提前关闭的管道时，程序不会按 Unix 惯例安静终止，而是收到写入错误甚至打印难看的报错。`sigpipe::reset()` 恢复默认行为，让 CLI 在管道场景表现符合用户预期。源码注释里也附了相关讨论链接。

**练习 3**：如果一个子命令执行到很深的函数才发现错误，退出码是怎么传回 `main` 的？
**参考答案**：不通过返回值层层传递，而是深处的代码调用 `set_failed()`（[main.rs:L84-L87](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/main.rs#L84-L87) 这段代码把线程局部的 `EXIT` 改成 `FAILURE`），`main` 结束时从 `EXIT` 取出返回。错误信息本身则通过 `Result` 的 `Err` 正常向上冒泡，由 `main` 统一打印。

### 4.2 args.rs：声明式的命令行参数定义

#### 4.2.1 概念说明

这个 894 行的文件回答一个问题：**「命令行长什么样」如何用类型表达？**

它使用 clap 的派生宏风格：定义结构体/枚举 → 打上 `#[derive(Parser)]` / `#[derive(Subcommand)]` / `#[derive(Args)]` → clap 由此生成解析代码、校验代码和帮助文本。这种「声明式」定义的好处：

- 参数、默认值、环境变量、帮助文档集中在字段声明处，一目了然；
- 解析结果是强类型的（`OutputFormat` 枚举而不是字符串），下游不用反复 `match` 字符串；
- 多个子命令共享参数组（如字体、包路径）时，用 `#[clap(flatten)]` 把一个 struct 嵌进另一个，天然复用。

文件最开头的注释还说明了一个工程细节：这个模块同时被 crate 本体和它的 build script（`build.rs`）引用，所以这里只能 import 两边都有的依赖。

#### 4.2.2 核心流程

命令行文本如何变成 Rust 值：

```text
argv（如 ["typst", "compile", "hello.typ", "--format", "pdf"]）
  └─ clap 按 CliArguments 的声明匹配
       ├─ 第一个词匹配 Command 枚举的子命令 → "compile" 命中 Command::Compile
       ├─ 位置参数 "hello.typ" 经 input_value_parser() → Input::Path("hello.typ")
       ├─ "--format pdf" 匹配 CompileArgs.format 字段 → OutputFormat::Pdf
       └─ 校验规则（如 required_if_eq("input", "-")）不通过则直接报错退出
  → 得到 CliArguments { command: Command::Compile(CompileCommand { args: CompileArgs {...} }) }
  → 交给 main.rs 的 dispatch()
```

其中 `-` 的转换发生在自定义解析器里：[crates/typst-cli/src/args.rs:L863-L874](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L863-L874) 这段代码是 `input_value_parser`：空路径报错，`-` 映射为 `Input::Stdin`，其余映射为 `Input::Path`。输出的 `output_value_parser`（L876 起）同理。

#### 4.2.3 源码精读

**顶层参数与子命令枚举。**

[crates/typst-cli/src/args.rs:L64-L76](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L64-L76) 这段代码定义顶层结构 `CliArguments`：一个必选子命令 `command`、全局选项 `--color`（默认 auto）和 `--cert`（自定义 CA 证书，可由环境变量 `TYPST_CERT` 提供）。

[crates/typst-cli/src/args.rs:L81-L112](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L81-L112) 这段代码定义子命令枚举 `Command`：`Compile`（别名 `c`）、`Watch`（别名 `w`）、`Init`、`Query`（`hide = true`，已废弃、让位于 `eval`）、`Eval`、`Fonts`、`Update`、`Completions`、`Info`。注意枚举每个变体的文档注释（`///`）就是该子命令在 `--help` 里的一行说明。

**compile 与 watch 共享一套参数。**

[crates/typst-cli/src/args.rs:L116-L120](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L116-L120) 这段代码定义 `CompileCommand`：只有一个字段 `args`，通过 `#[clap(flatten)]` 把 `CompileArgs` 整组嵌入。`WatchCommand`（L124 起）同样嵌入了 `CompileArgs`（L127），再额外加 `--no-fullscreen`（L134）和 HTTP server 参数（L139）——这就是 4.3 节 `compile_once` 能被两个子命令复用的参数层基础。

**CompileArgs：编译命令的全部旋钮。**

[crates/typst-cli/src/args.rs:L347-L471](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L347-L471) 这段代码定义 `CompileArgs`。关键字段速查：

| 字段（行号） | 命令行 | 含义 |
| --- | --- | --- |
| `input`（L350） | 位置参数 | 输入文件，`-` 表示 stdin |
| `output`（L365） | 位置参数 | 输出文件，`-` 表示 stdout；从 stdin 读时必填（`required_if_eq("input", "-")`） |
| `format`（L369） | `--format/-f` | 输出格式；缺省时按扩展名推断 |
| `world`（L373） | `--root`、`--input`、字体、包路径等 | 组装 World 的参数（flatten 自 `WorldArgs`） |
| `pages`（L399） | `--pages` | 导出哪些页，支持 `2,3-6,8-` 这类区间 |
| `pdf_standard`（L404） | `--pdf-standard` | 要求 PDF 符合的标准（如 `a-2b`） |
| `pdf_tagged`（L426） | `--pdf-tagged` | 是否写 PDF 无障碍标签 |
| `ppi`（L432） | `--ppi` | PNG 导出的像素密度（默认 144） |
| `deps`/`deps_format`（L447/L451） | `--deps` 等 | 把依赖列表写成文件 |
| `process`（L455） | `-j/--jobs` 等 | 编译过程参数（flatten 自 `ProcessArgs`） |
| `open`（L463） | `--open[=VIEWER]` | 编译完用默认查看器或指定程序打开结果 |
| `timings`（L471） | `--timings` | 输出性能计时 JSON（实验性） |

**可复用的参数组与环境变量。**

[crates/typst-cli/src/args.rs:L477-L511](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L477-L511) 这段代码定义 `WorldArgs`：`--root`（项目根目录，环境变量 `TYPST_ROOT`）、`--input/-i`（键值对，进入 `sys.inputs`）、字体与包参数（flatten）、`--creation-timestamp`（环境变量 `SOURCE_DATE_EPOCH`，用于可复现构建）。[L513-L529](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L513-L529) 则是 `ProcessArgs`：`-j/--jobs` 并行度、`--features`（环境变量 `TYPST_FEATURES`）、`--diagnostic-format`（环境变量 `TYPST_DIAGNOSTIC_FORMAT`）。[L547 起](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L547) 的 `FontArgs` 提供 `--font-path`（环境变量 `TYPST_FONT_PATHS`）与 `--ignore-system-fonts`。这些参数组也被 `eval`、`query` 等子命令复用——同一个 struct，多处 flatten。

**输入/输出的统一抽象。**

[crates/typst-cli/src/args.rs:L592-L599](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L592-L599) 和 [L610-L617](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L610-L617) 这两段代码定义 `Input`/`Output` 枚举：各只有两个变体——`-` 代表标准输入/输出，否则是文件路径。把「stdout 与文件」抽象成一个类型后，下游所有写文件的地方只需面对一个 `Output`。

[L619-L626](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L619-L626) 这段代码是 `Output::write`：写 stdout 就 `write_all`，写文件就 `std::fs::write`。4.3 节所有导出函数最终都落到这个小小的函数上。

**输出格式枚举。**

[crates/typst-cli/src/args.rs:L669-L684](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L669-L684) 这段代码定义 `OutputFormat`（Pdf/Png/Svg/Html/Bundle 五种）及辅助方法 `is_paged()`（区分「分页文档」与 HTML 等流式格式）。它就是 `--format` 的取值来源，也是 4.3 节选择编译分支的依据。

#### 4.2.4 代码实践

**实践 B：故意触发 clap 的报错，观察校验来自哪个字段。**

1. **实践目标**：把「字段声明」与「实际报错」对上号，体会声明式校验。
2. **操作步骤**：
   - `cargo run -p typst-cli -- compile`（不给输入）
   - `echo '= Hi' | cargo run -p typst-cli -- compile - `（stdin 输入但不给输出）
   - `cargo run -p typst-cli -- compile hello.typ -f docx`
3. **需要观察的现象**：第一条报「缺少 INPUT」；第二条报 output 相关错误（因为 `required_if_eq("input", "-")`，见 L360-L364）；第三条报非法取值并列出合法值（来自 `OutputFormat` 枚举）。
4. **预期结果**：三条错误信息措辞各不相同，分别对应 `input` 必填、`output` 的条件必填、`format` 的枚举校验；报错后进程以非零码退出（对照 4.1 的 `EXIT` 机制）。
   以上**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `output` 字段要标 `required_if_eq("input", "-")`？
**参考答案**：当输入来自 stdin 时，输出路径无法像「输入文件换扩展名」那样推断出来（见 4.3 的默认输出逻辑），也没有 stdin 对应的路径可换；所以必须显式给出输出，否则无从落盘。而普通文件输入时 output 可以省略，由 CLI 推断。

**练习 2**：`--font-path`、`--root`、`SOURCE_DATE_EPOCH` 分别通过什么机制与环境变量关联？
**参考答案**：都在字段的 `#[clap(...)]` 属性里用 `env = "..."` 声明（如 `WorldArgs.root` 的 `env = "TYPST_ROOT"`、`FontArgs.font_paths` 的 `env = "TYPST_FONT_PATHS"`、`WorldArgs.creation_timestamp` 的 `env = "SOURCE_DATE_EPOCH"`）。命令行显式传值优先；未传值时 clap 自动读取同名环境变量。

**练习 3**：`Compile` 与 `Watch` 两个子命令的参数为什么长得几乎一样？代码上是如何做到的？
**参考答案**：因为 watch 本质是「反复 compile」，两者共享 `CompileArgs`；代码上 `CompileCommand` 与 `WatchCommand` 都用 `#[clap(flatten)]` 嵌入同一个 `CompileArgs`（args.rs L116-L127），watch 只额外增加 `--no-fullscreen` 与 server 参数。这为 compile.rs 中「一份编译主干、两种调用方式」打下基础。

### 4.3 compile.rs：一次编译的主干

#### 4.3.1 概念说明

`compile.rs` 是 `typst compile` 的全部业务逻辑。它把「编译一次」组织成三步准备 + 一次执行：

- **Timer**：若用户传了 `--timings`，负责记录各阶段耗时（否则是占位实现，不影响主流程）；
- **CompileConfig**：对 `CompileArgs` 做预处理（「配置对象」）——推断输出格式、推断默认输出路径、归并 PDF 标准等，把「原始命令行」变成「编译过程真正要用的配置」；
- **SystemWorld**：u1-l3 讲过的 `World` 抽象在 CLI 侧的实现（找根目录、读源文件、发现字体、下载包……细节在 `world.rs`，下一讲专题）。

然后执行 `compile_once`：调一次 `typst::compile` 流水线（u1-l3 的 parse→eval→layout…），把结果导出成文件，打印诊断，必要时打开文件。**`compile_once` 是一个「单次编译单元」**：`typst compile` 调它一次；`typst watch`（watch 模块细节待确认）在每次文件变化后再调它——这就是 4.2 里两个子命令共享参数的深层原因。

#### 4.3.2 核心流程

一次 `typst compile hello.typ` 在本文件中的调用链：

```text
compile(command)                                  [compile.rs:L39]
 ├─ 1) Timer::new_or_placeholder(...)             计时器（可无）
 ├─ 2) CompileConfig::new(command)?               预处理配置
 │      └─ new_impl：推断 output_format、推断默认 output …  [L107]
 ├─ 3) SystemWorld::new(input, world_args, …)?    组装编译环境
 └─ 4) timer.record(|world| compile_once(...))
        └─ compile_once(world, config)            [compile.rs:L288]
             ├─ compile_and_export(world, config) [compile.rs:L344]
             │    ├─ Pdf/Png/Svg: typst::compile::<PagedDocument>(world)
             │    │      └─ export_paged ──┬─ Pdf → export_pdf ─→ typst_pdf::pdf() ─→ output.write()
             │    │                        └─ Png/Svg → export_image（每页一个文件）
             │    ├─ Html:   typst::compile::<HtmlDocument>(world) → export_html → output.write()
             │    └─ Bundle: typst::compile::<Bundle>(world)      → export_bundle（目录）
             ├─ 打印诊断（错误 / 警告）print_diagnostics
             ├─ open_output(config)?             --open 时打开产物
             └─ write_deps(...)                   --deps 时写依赖文件
```

这里能看到 u1-l3 分工地图的落点：`typst::compile::<T>` 是库 crate 的总入口，`T` 决定产出物类型；导出环节分别委托给 `typst-pdf`、`typst-html`、`typst-render`、`typst-svg` 等 crate。

#### 4.3.3 源码精读

**入口三步准备。**

[crates/typst-cli/src/compile.rs:L38-L49](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L38-L49) 这段代码是 `compile` 函数本体：依次创建 `Timer`、`CompileConfig`、`SystemWorld`，最后用 `timer.record` 包住 `compile_once` 的调用并返回其结果。`SystemWorld::new` 的错误被 `map_err` 转成带格式的字符串错误。整个函数只有 10 行——「准备环境」与「执行编译」的边界一目了然。

**配置对象。**

[crates/typst-cli/src/compile.rs:L52-L92](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L52-L92) 这段代码定义 `CompileConfig`：字段涵盖输出路径与格式、页码区间、PDF 标准、诊断格式、`--open`、依赖文件、PNG 的 ppi、watch 用到的缓存与 server 等。它是 `CompileArgs` 的「加工后形态」，例如 `output` 已从 `Option<Output>` 变成确定的 `Output`。

**两条构造路径与共享实现。**

[crates/typst-cli/src/compile.rs:L94-L103](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L94-L103) 这段代码给出两个构造函数：`new`（compile 用）与 `watching`（watch 用），都转调共享的 `new_impl`——又一个「一份主干、两种入口」。

**格式推断与默认输出路径。**

[crates/typst-cli/src/compile.rs:L114-L130](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L114-L130) 这段代码决定输出格式：显式 `--format` 最优先；否则看输出文件扩展名（大小写不敏感地匹配 pdf/png/svg/html）；都推断不出就 `bail!`，提示用 `--format/-f` 手动指定；若输出是 stdout（没有扩展名可看），默认 PDF。

[crates/typst-cli/src/compile.rs:L132-L145](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L132-L145) 这段代码推断默认输出路径：用户没给 output 时，把输入文件的扩展名换成与格式对应的扩展名（`hello.typ` → `hello.pdf`）；stdin 输入走不到这里（参数层已要求必填 output），代码用 `panic` 说明这一约定。

**单次编译单元。**

[crates/typst-cli/src/compile.rs:L288-L341](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L288-L341) 这段代码是 `compile_once`。逐段看：

- L297：`let Warned { output, mut warnings } = compile_and_export(world, config);` ——编译结果可能是「成功但带警告」，所以返回类型包了一层 `Warned`，这里解构出产物与警告两个通道；
- L300-L302：把配置阶段积累的静态警告（如废弃 flag 提示）合并进来；
- L304-L320（成功分支）：watch 模式下打印成功/部分成功状态；调用 `print_diagnostics` 打印警告；若指定了 `--open` 则 `open_output` 打开产物；
- L322-L332（失败分支）：`set_failed()` 置失败退出码（呼应 4.1 的 `EXIT` 机制），watch 模式打印 Error 状态，打印错误诊断；
- L335-L338：若指定 `--deps`，写出依赖清单文件。

**编译 + 导出的分叉点。**

[crates/typst-cli/src/compile.rs:L344-L364](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L344-L364) 这段代码是 `compile_and_export`，按 `output_format` 三路分发：Pdf/Png/Svg 都先 `typst::compile::<PagedDocument>(world)` 编译出分页文档再 `export_paged`；Html 用 `typst::compile::<HtmlDocument>`；Bundle 用 `typst::compile::<Bundle>`。同一个 `typst::compile` 入口靠泛型参数区分产出物——这正是 u1-l3「库 crate 对外窄接口」的体现。

**PDF 导出的最后一公里。**

[crates/typst-cli/src/compile.rs:L402-L415](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L402-L415) 这段代码是 `export_pdf`：用 `pdf_options(config)` 组装选项后，调 `typst_pdf::pdf(document, &options)` 得到字节缓冲，随后 `config.output.write(&buffer)` 落盘。写文件失败会转成带位置的错误。这个 `write` 正是 4.2 精读过的 [args.rs:L619-L626](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/args.rs#L619-L626) 的 `Output::write`——stdout 与文件在此合流。

[crates/typst-cli/src/compile.rs:L383-L399](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L383-L399) 这段代码是 `export_paged`：Pdf 走 `export_pdf`；Png/Svg 走 `export_image`（多页时会按 `{p}` 模板每页写一个文件，模板逻辑在同文件 L608 起的 `output_template` 模块）。HTML 导出（[L367-L380](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L367-L380)）同理：`typst_html::html` 生成字符串后 `output.write` 写出。

#### 4.3.4 代码实践

**实践 C（本讲主实践）：eprintln! 打点，亲眼看调用链。**

1. **实践目标**：在编译主干上加临时日志，验证 4.3.2 的调用链图，获得「这条链我真的走过」的实感。
2. **操作步骤**：
   - 建实验分支：`git checkout -b trace-cli`（保持 main 干净，方便还原）；
   - 在 `crates/typst-cli/src/compile.rs` 的四处各加一行 `eprintln!`（**示例代码**，非项目原有）：
     - `compile`（L39）函数体第一行：`eprintln!("[trace] 1. compile()");`
     - `compile_once`（L288）函数体第一行：`eprintln!("[trace] 2. compile_once()");`
     - `compile_and_export`（L344）函数体第一行：`eprintln!("[trace] 3. compile_and_export() fmt={:?}", config.output_format);`
     - `export_pdf`（L402）函数体第一行：`eprintln!("[trace] 4. export_pdf()");`
   - 构建：`cargo build -p typst-cli`（dev profile，u1-l2 讲过，调试优先）；
   - 准备测试文件 `hello.typ`（内容一行：`= Hello, Typst!`），运行：
     `./target/debug/typst compile hello.typ`
   - 观察完终端输出后还原：`git checkout -- crates/typst-cli/src/compile.rs && git branch -D trace-cli`，并用 `git status` 确认工作区干净。
3. **需要观察的现象**：stderr 上按顺序出现 1→2→3→4 四行打点（stdout 不受干扰）；第 3 行打印出 `fmt=Pdf`（因为输出路径是 `hello.pdf` 推断出的 PDF）；最后生成 `hello.pdf`。
4. **预期结果**：打点顺序与 4.3.2 调用链一致：`compile → compile_once → compile_and_export → export_pdf`。若把命令换成 `-f html`，第 3 行应变为 `fmt=Html` 且不再出现第 4 行（HTML 走 `export_html` 而非 `export_pdf`）。
   以上构建与运行**待本地验证**。
5. **注意**：按本手册规则，源码仓库不应留下实验改动；务必执行还原步骤后再继续下一讲。

#### 4.3.5 小练习与答案

**练习 1**：`compile_and_export` 为什么按 `output_format` 分发，而不是在拿到编译结果后再判断？
**参考答案**：因为「产出物类型」在编译之前就必须确定——`typst::compile::<PagedDocument>` 与 `typst::compile::<HtmlDocument>` 是对不同类型参数的调用，走的是不同的编译与导出管线。格式必须在编译前敲定，所以分发点放在 `compile_and_export` 的开头。

**练习 2**：`Warned { output, warnings }` 这个解构说明编译结果可能「成功但有警告」。在 `compile_once` 里警告是怎么被处理的？
**参考答案**：`compile_and_export` 返回 `Warned<SourceResult<...>>`；L297 解构出两个通道后，静态警告（如废弃 flag 的提示）被并入 `warnings`（L300-L302）；成功分支里即使产物正常，也调用 `print_diagnostics` 把警告打印给用户（L316），watch 模式下还会区分 Success 与 PartialSuccess 两种状态（L309-L313）。

**练习 3**：`typst compile hello.typ` 没有指定 `--output`，产物为什么会出现在 `hello.pdf`？
**参考答案**：见 [compile.rs:L132-L145](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L132-L145) 这段代码：`args.output` 为 `None` 时，取输入路径并 `with_extension` 换成与 `output_format` 匹配的扩展名；而 `output_format` 此前已按「显式 `--format` > 输出扩展名 > 默认 PDF」的顺序推断（L114-L130）。

## 5. 综合实践

**任务：给一条命令画出「参数落点 + 调用链」全图，并用打点验证。**

考虑命令（先纸面分析，再动手验证）：

```bash
./target/debug/typst compile essay.typ -f png --pages 1,3- --ppi 72 'page-{p}.png'
```

1. **纸面阶段**（不许看运行结果，只看源码回答）：
   - `-f png`、`--pages 1,3-`、`--ppi 72`、`page-{p}.png` 分别落到 `CompileArgs` 的哪个字段（对照 4.2.3 的字段表）？
   - `output_format` 的推断走 [compile.rs:L114-L130](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L114-L130) 的哪条分支？此时 output 已显式给出，`--format` 是否还有意义？
   - 编译走 `typst::compile::<PagedDocument>` 哪条分支？导出走 `export_paged` 里的哪条（提示：见 [compile.rs:L383-L399](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L383-L399)）？多页时文件名如何展开（提示：`output_template` 模块与 4.2.3 中 output 字段的文档注释）？
   - 画出你预期的打点顺序（参考实践 C 的四个点位）。
2. **验证阶段**：按实践 C 的步骤在实验分支加打点（可给 `export_image` 多加一个点位），用上面的命令实际运行，对照纸面答案。**待本地验证**。
3. **检查点**：若纸面结论与实际打印有任何出入，回到对应源码行号处弄清楚原因——这正是源码阅读最有效的纠错时刻。

## 6. 本讲小结

- `main.rs` 是一个刻意做薄的入口：`mod` 声明构成目录页，`ARGS`（LazyLock）只解析一次，`dispatch()` 用一个 `match` 把子命令转给同名模块，退出码通过线程局部 `EXIT` 中转。
- `args.rs` 用 clap 派生宏实现「数据结构即接口」：`Command` 枚举定义子命令，`CompileArgs` 等结构体定义参数，`flatten` 让 `compile`/`watch` 共享参数组，`Input`/`Output` 枚举把 `-`（stdin/stdout）统一进类型系统。
- `compile` 的主干是三步准备（Timer → CompileConfig → SystemWorld）加一次 `compile_once`；`CompileConfig::new_impl` 负责「显式 `--format` > 输出扩展名 > 默认 PDF」的格式推断与默认输出路径推断。
- `compile_and_export` 按 `output_format` 对 `typst::compile::<T>` 做三路泛型分发（PagedDocument / HtmlDocument / Bundle），导出最终收敛到 `Output::write` 这一个落点。
- `compile_once` 是可复用的「单次编译单元」，`typst watch` 在其上循环调用（watch 模块内部机制待确认，后续讲义展开）。

## 7. 下一步学习建议

- **下一讲（u1-l5，建议主题）**：精读 [crates/typst-cli/src/world.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/world.rs) 的 `SystemWorld`——本讲 L42-L47 处它只是「准备好了」，下一讲回答：项目根目录如何决定、源文件与字体如何被发现、包如何解析与缓存。这会把 u1-l3 的 `World` 概念落到实现。
- **回望库侧**：带着本讲的调用链再打开 u1-l3 的分工地图，用 `git grep "fn compile" crates/typst/src`（本地只读命令）找到 `typst::compile` 的库侧定义，看 CLI 传入的 world 如何进入 parse → eval → layout 流水线——这是单元 2 的主线。
- **顺带阅读**：`crates/typst-cli/src/greet.rs`（无参数时的欢迎输出）与 `crates/typst-cli/Cargo.toml`（看 `self-update`、`http-server` 等 feature 如何裁剪 CLI），都是 10 分钟量级的小阅读，能巩固本讲的 feature 条件编译与模块组织。
