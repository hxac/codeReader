# 命令行参数模型

## 1. 本讲目标

学完本讲后，你应该能够：

- 看懂 `args.rs` 如何用 **clap 派生宏（derive macro）** 把一行命令（如 `typst compile doc.typ out.pdf --pages 2,4-6`）翻译成一组强类型的 Rust 结构体。
- 说出 `CliArguments`、`Command` 枚举、以及各子命令结构体之间的层级关系。
- 理解参数是如何被「分组复用」的：`CompileArgs` / `WorldArgs` / `ProcessArgs` / `FontArgs` / `PackageArgs` 这几个参数组被多个子命令共享。
- 掌握 `Input` / `Output` 两个枚举如何用**自定义值解析器（value parser）**区分「`-` 代表标准输入/输出」与「真实文件路径」。
- 认识 `OutputFormat` / `Target` / `PdfStandard` / `Feature` / `DiagnosticFormat` 等枚举类型，以及 `Pages` 页面范围的解析算法。

本讲只聚焦「参数定义」这一层，**不**涉及编译、导出、字体发现等具体执行逻辑——那些是后续讲义（u2、u3）的内容。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。

### 2.1 什么是命令行参数解析

当你敲下 `typst compile doc.typ`，操作系统把这一行字符串切成一个个片段传给程序：`["typst", "compile", "doc.typ"]`。程序需要把这些字符串「翻译」成自己能用的数据，比如「用户要执行 compile 子命令，输入文件是 `doc.typ`」。这个翻译过程就是**命令行参数解析**。

Rust 生态里最流行的解析库叫 **clap**。它有两种用法：

- **派生宏（derive）**：在结构体上加 `#[derive(Parser)]`，clap 根据字段名和注解自动生成解析逻辑。typst-cli 用的就是这种。
- **建造者（builder）**：手动用代码一点点拼出参数定义。

派生宏的好处是：**数据结构即文档**——你只要看结构体定义，就知道命令行长什么样。

### 2.2 clap 的几个关键派生

| 派生 trait | 作用 | 在 typst 中的对应 |
| --- | --- | --- |
| `Parser` | 标记「这是一个完整命令行入口」 | `CliArguments`、各子命令结构体 |
| `Subcommand` | 标记「这是一个枚举，每个变体是一个子命令」 | `Command` 枚举 |
| `Args` | 标记「这是一组可被复用的参数」 | `CompileArgs`、`WorldArgs` 等 |
| `ValueEnum` | 标记「这是一个取值有限的枚举，可从字符串解析」 | `OutputFormat`、`Target` 等 |

### 2.3 几个常见的 clap 注解

- `#[clap(long = "pages")]`：表示这个字段对应 `--pages` 长选项。
- `#[clap(flatten)]`：把另一个参数组「拍平」嵌进来，实现复用。
- `#[command(subcommand)]`：这个字段是一个子命令枚举。
- `#[command(visible_alias = "c")]`：给子命令起别名，`compile` 可以写成 `c`。
- `env = "TYPST_CERT"`：如果命令行没给这个选项，就去读这个环境变量。

### 2.4 `FromStr` 与值解析器

Rust 标准库有个 `FromStr` trait，描述「如何把字符串解析成某个类型」。clap 默认会用它。

但有时我们想要更精细的控制（比如报错信息更好），就会写一个**自定义值解析器（value parser）**：一个函数，接收原始字符串，返回解析后的值或错误。typst 的 `Input` / `Output` 就是这么做的。

> 名词速查：**TDD/TTY 不相关**、**stdin/stdout**（标准输入/输出流）、**OS 字符串**（`OsStr`，能表示任意文件名字节，不局限于合法 UTF-8）。这些后文会用到。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开：

| 文件 | 作用 |
| --- | --- |
| [`src/args.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs) | 用 clap 派生定义**全部**命令行参数：顶层入口、9 个子命令、若干参数组、输入/输出解析器、各类枚举与页面范围解析 |

辅助理解时还会顺带引用（这些是别的讲义的主角，这里只用它们做交叉验证）：

| 文件 | 作用 |
| --- | --- |
| [`src/main.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs) | 调用 `CliArguments::try_parse()` 解析参数，并把 `Command` 枚举分发到各模块（u1-l2 已讲） |
| [`build.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/build.rs) | 构建脚本里**也**导入 `args.rs`，用于生成 man 手册页与 shell 补全（u4-l5 会讲） |

> ⚠️ 注意 `args.rs` 顶部有一条特殊注释（见 4.1 节）：这个模块**同时被运行时和构建脚本导入**，因此它只能 import 那些既是运行时依赖、又是构建依赖的 crate。这是理解该文件「为什么写得这么克制」的关键。

## 4. 核心概念与源码讲解

### 4.1 clap 派生：CliArguments、Command 枚举与各子命令

#### 4.1.1 概念说明

整个命令行的顶层结构是一个 `CliArguments`，它包含两部分：

1. **一个子命令** `command`（`compile` / `watch` / `init` / ……）。
2. **若干全局选项**，比如 `--color`、`--cert`，这些对所有子命令都生效。

子命令用一个 `Command` **枚举**表示——枚举天然适合表达「N 选 1」。每个变体（如 `Compile(CompileCommand)`）内部又装着一个结构体，承载该子命令专属的参数。

这种「顶层 + 子命令枚举 + 子命令结构体」的三层结构，是 clap 子命令程序的标准骨架。

#### 4.1.2 核心流程

命令行解析的数据流可以画成：

```
typst compile doc.typ --pages 2
        │
        ▼
CliArguments (Parser)          ← 解析全局选项 color/cert
        │  .command
        ▼
Command::Compile(CompileCommand)   ← 选出子命令
        │  .args (flatten)
        ▼
CompileArgs                    ← 解析 input/output/pages/...
        │  .world (flatten)
        ▼
WorldArgs                      ← 复用的参数组
```

解析发生在 [`main.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs) 的 `ARGS` 惰性全局变量里，随后 `dispatch()` 用 `match` 把 `Command` 枚举分发到对应模块。

#### 4.1.3 源码精读

先看顶层的 `CliArguments`，它派生了 `Parser`：

[src/args.rs:50-76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L50-L76) —— 定义整个命令行入口；`#[command(subcommand)] pub command: Command` 是子命令入口，`color` 默认 `auto`，`cert` 可由 `TYPST_CERT` 环境变量提供。

注意 `version` 字段是用 `format!` 动态拼出版本号与 commit，所以 `typst --version` 的输出由这里决定。

再看 `Command` 枚举，它派生了 `Subcommand`：

[src/args.rs:79-112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L79-L112) —— 9 个子命令变体。其中 `compile` 有别名 `c`、`watch` 有别名 `w`；`Query` 标了 `hide = true`（已弃用，不出现在帮助里）；`Update` 在没开 `self-update` feature 时被 `hide`。

这段对应 [`main.rs:70-81`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L70-L81) 的 `dispatch()`：每个 `Command::Xxx(command)` 变体被解构出内层的 command 结构体，交给对应模块处理。

关于「共享导入」约束，看文件最顶部：

[src/args.rs:1-4](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L1-L4) —— 说明本模块同时被 crate 自身和 build script 导入，因此只能 import 同时是运行时和构建依赖的 crate。这就是为什么 `args.rs` 不会引入重型运行时依赖。

#### 4.1.4 代码实践

**实践目标**：验证「帮助文本与源码结构一一对应」。

**操作步骤**：

1. 在 `crates/typst-cli` 下执行 `cargo build`（u1-l1 已说明默认成员即 typst-cli）。
2. 运行 `./target/debug/typst --help`。
3. 运行 `./target/debug/typst compile --help`。

**需要观察的现象**：

- `--help` 列出的子命令应与 `Command` 枚举的可见变体一致（注意 `query` 因 `hide` 不出现；若未启用 `self-update`，`update` 也不出现）。
- 顶层帮助里能看到全局选项 `--color`、`--cert`。

**预期结果**：帮助输出里的子命令名、别名（`c`/`w`）、选项名，都能在 `args.rs` 对应结构体里找到出处。

> 若本地未构建，上述现象「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Command` 是枚举而不是结构体？

> **答案**：因为一次运行只能执行**一个**子命令，这是「N 选 1」语义，枚举（互斥）比结构体（字段并存）更贴切；clap 也用 `Subcommand` 派生支持这种模式。

**练习 2**：`typst --help` 里看不到 `query` 子命令，对应源码里哪一行注解？

> **答案**：对应 [src/args.rs:94](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L94) 的 `#[command(hide = true)]`。

---

### 4.2 参数组：CompileArgs / WorldArgs / ProcessArgs / FontArgs / PackageArgs

#### 4.2.1 概念说明

如果每个子命令都把所有参数重新写一遍，代码会大量重复。typst 用 clap 的 **`#[clap(flatten)]`** 把参数「分组」，按职责切成几个小组，再像搭积木一样拼到各子命令里：

- `CompileArgs`：编译/监听专属的核心参数（输入、输出、格式、页面、PDF 标准、PPI、依赖、打开方式……）。
- `WorldArgs`：构造「编译环境」需要的参数（项目根、`sys.inputs`、字体、包、创建时间）——compile/watch/eval/query 共用。
- `ProcessArgs`：编译过程本身的参数（并行 jobs、实验性 features、诊断格式）。
- `FontArgs`：字体相关（`--font-path`、忽略系统/内嵌字体）——被 `WorldArgs` 和 `FontsCommand` 复用。
- `PackageArgs`：包存储路径相关——被 `WorldArgs` 和 `InitCommand` 复用。

这种「小积木拼大积木」的方式，让参数定义集中、一处修改处处生效。

#### 4.2.2 核心流程

复用关系如下（箭头表示「包含/拍平进来」）：

```
CompileCommand ──flatten──> CompileArgs
                              ├─ flatten ─> WorldArgs
                              │              ├─ flatten ─> FontArgs
                              │              └─ flatten ─> PackageArgs
                              └─ flatten ─> ProcessArgs

FontsCommand ──flatten──> FontArgs          （单独复用 FontArgs）
InitCommand  ──flatten──> PackageArgs       （单独复用 PackageArgs）
EvalCommand / QueryCommand ──flatten──> WorldArgs + ProcessArgs
```

#### 4.2.3 源码精读

`CompileArgs` 是最「胖」的一组，承载 compile/watch 几乎全部选项：

[src/args.rs:292-394](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L292-L394) —— 注意几个要点：`input` 用自定义解析器 `input_value_parser()`（见 4.3）；`output` 标了 `required_if_eq("input", "-")`，意思是当输入是 stdin（`-`）时**必须**显式给输出；`pages` 用逗号分隔；`make_deps` 是 `hide = true` 的隐藏选项。

`WorldArgs` 是被四个子命令共享的环境参数组：

[src/args.rs:396-430](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L396-L430) —— `root` 可由 `TYPST_ROOT` 提供；`inputs` 是 `key=value` 对，用 `ArgAction::Append` 可重复出现（见 4.5 的 `parse_sys_input_pair`）；`creation_timestamp` 读 `SOURCE_DATE_EPOCH`（用于可复现构建）。

`ProcessArgs` 与两个字体/包参数组：

[src/args.rs:432-448](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L432-L448) —— `jobs` 控制并行度（`None` 时默认用 CPU 数）；`features` 是实验性开关，逗号分隔，也可用 `TYPST_FEATURES`。

[src/args.rs:466-490](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L466-L490) —— `FontArgs`：`font_paths` 在 Unix 用 `:` 分隔、Windows 用 `;` 分隔（由 `ENV_PATH_SEP` 常量决定，见文件第 23 行）；`ignore_embedded_fonts` 仅在 `embedded-fonts` feature 下存在。

[src/args.rs:450-464](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L450-L464) —— `PackageArgs`：本地包路径与缓存路径，均可由对应 `TYPST_*` 环境变量覆盖。

#### 4.2.4 代码实践

**实践目标**：把帮助选项映射回结构体字段。

**操作步骤**：

1. 运行 `./target/debug/typst compile --help`。
2. 运行 `./target/debug/typst fonts --help`。

**需要观察的现象**：

- `compile --help` 里的 `--root`、`--input`、`--font-path`、`--package-path`、`--jobs`、`--diagnostic-format` 分属 `WorldArgs`、`ProcessArgs`、`FontArgs`、`PackageArgs`，但因为 `flatten`，它们在帮助里被「拍平」混在一起。
- `fonts --help` 只显示 `FontArgs` 的选项（因为 `FontsCommand` 只 flatten 了 `FontArgs`），印证了「按需复用」。

**预期结果**：你能为帮助里每一个选项，指出它来自哪个参数组的哪个字段。

> 若本地未构建，上述现象「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`compile --help` 里同时出现 `--font-path`（来自 FontArgs）和 `--root`（来自 WorldArgs），但二者分属不同结构体，为什么在帮助里看不到分组边界？

> **答案**：因为它们都通过 `#[clap(flatten)]` 被拍平进 `CompileArgs`，clap 把 flatten 的字段当作直属字段对待，不再保留原始分组。

**练习 2**：`typst fonts` 能用 `--jobs` 吗？为什么？

> **答案**：不能。`FontsCommand` 只 flatten 了 `FontArgs`（[src/args.rs:229-239](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L229-L239)），没有 `ProcessArgs`，所以没有 `--jobs`。列出字体不需要并行编译。

---

### 4.3 Input / Output 枚举与自定义值解析器

#### 4.3.1 概念说明

很多命令行工具支持用 `-` 表示「从标准输入读」或「写到标准输出」，例如 `cat doc.typ | typst compile - out.pdf`。typst 用两个枚举来显式区分这两种情况：

- `Input::Stdin` 或 `Input::Path(PathBuf)`
- `Output::Stdout` 或 `Output::Path(PathBuf)`

区分它们的好处是：后续代码（compile/watch）可以 `match` 这两个枚举，对 stdin/stdout 走特殊处理（比如 stdin 不能被 watch、stdout 不能用 `--open`），而不用到处判断「字符串是不是 `-`」。

由于 clap 默认的解析逻辑做不到「`-` 特判」，typst 写了**自定义值解析器** `input_value_parser` / `output_value_parser`。

#### 4.3.2 核心流程

解析器逻辑（伪代码）：

```
对原始值 value:
    若 value 为空       → 报 InvalidValue 错误
    若 value == "-"     → Stdin / Stdout
    否则                → Path(value)
```

#### 4.3.3 源码精读

`Input` / `Output` 枚举定义：

[src/args.rs:512-519](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L512-L519) —— `Input` 枚举，`Stdin` 与 `Path`。

[src/args.rs:530-555](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L530-L555) —— `Output` 枚举及其 `write` / `open` 方法：`Output::Stdout` 写标准输出，`Output::Path` 写文件，后续导出逻辑直接调用这些方法而不用关心具体是哪种。

两个自定义解析器：

[src/args.rs:769-780](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L769-L780) —— `input_value_parser`：用 `OsStringValueParser` 处理任意字节路径，`try_map` 里做空字符串校验与 `-` 特判。

[src/args.rs:782-794](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L782-L794) —— `output_value_parser`：逻辑与 input 对称，产出 `Output::Stdout` 或 `Output::Path`。

解析器在字段上的绑定方式（以 `CompileArgs.input` 为例）：

[src/args.rs:295-297](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L295-L297) —— `value_parser = input_value_parser()` 把自定义解析器挂到 `input` 字段；`value_hint = ValueHint::FilePath` 给 shell 补全一个提示。

#### 4.3.4 代码实践

**实践目标**：观察 stdin 输入路径的解析行为。

**操作步骤**：

1. 准备一个最小文档，写入 `doc.typ`，内容如 `Hello typst`。
2. 运行 `echo 'Hello typst' | ./target/debug/typst compile - out.pdf`（输入用 `-`）。
3. 对照 `CompileArgs.output` 的 `required_if_eq("input", "-")`，尝试省略 `out.pdf`，观察是否报错。

**需要观察的现象**：

- 步骤 2 能正常生成 `out.pdf`，说明 `-` 被解析成 `Input::Stdin`。
- 步骤 3 应当报「缺少 output」之类的错，因为输入是 `-` 时 output 是必需的。

**预期结果**：与 `required_if_eq` 的语义一致。

> 若本地未构建，上述现象「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么解析器要用 `OsStringValueParser`（处理 `OsStr`）而不是普通的字符串解析？

> **答案**：文件名可能包含非 UTF-8 字节（在不同操作系统上）。`OsStr` 能承载任意文件名字节，避免在文件名编码上丢失信息或直接报错。

**练习 2**：如果用户传了空字符串作为输入（理论上），解析器会怎样？

> **答案**：会返回 `InvalidValue` 错误（见 [src/args.rs:772-773](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L772-L773)），因为空值不被允许。

---

### 4.4 枚举类型：OutputFormat / Target / PdfStandard / Feature / DiagnosticFormat 等

#### 4.4.1 概念说明

命令行里有大量「取值有限」的选项，比如输出格式只能是 pdf/png/svg/html/bundle。这类选项天然适合用 Rust 枚举 + clap 的 `ValueEnum` 派生：clap 自动帮你把字符串（`pdf`）转成枚举变体（`OutputFormat::Pdf`），并在帮助里列出所有可选值。

本节涉及的几个关键枚举：

| 枚举 | 含义 | 典型选项 |
| --- | --- | --- |
| `OutputFormat` | 输出文件格式 | `--format pdf/png/svg/html/bundle` |
| `Target` | 编译目标（决定走哪条编译流水线） | `--target paged/html/bundle` |
| `DiagnosticFormat` | 诊断信息输出格式 | `--diagnostic-format human/short` |
| `Feature` | 实验性特性开关 | `--features html,bundle,a11y-extras` |
| `PdfStandard` | PDF 合规标准 | `--pdf-standard 1.7,a-2b,ua-1` |
| `DepsFormat` | 依赖文件格式 | `--deps-format json/zero/make` |
| `SerializationFormat` | query/info 序列化格式 | `--format json/yaml` |

#### 4.4.2 核心流程

clap 对 `ValueEnum` 的处理：

```
命令行字符串 "a-2b"
        │  ValueEnum 派生（按 #[value(name = "a-2b")] 匹配）
        ▼
PdfStandard::A_2b
```

变体名（Rust 标识符 `A_2b`）和命令行名字（`a-2b`）不必相同——用 `#[value(name = "...")]` 显式指定映射。这对 `PdfStandard` 这种带点带横线的标准号尤其重要。

另外，`args.rs` 反复出现一行宏 `display_possible_values!(SomeEnum);`。它来自 `typst_utils`，作用是给枚举实现 `Display`，让 `format!("{}", OutputFormat::Pdf)` 输出 `"pdf"`——在错误提示里把枚举值变回可读字符串很有用。

#### 4.4.3 源码精读

`OutputFormat` 及其辅助方法：

[src/args.rs:589-606](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L589-L606) —— 5 种格式；`is_paged()` 区分「分页文档」格式（pdf/png/svg）与 html/bundle，这条判断在后续导出分发里很关键（u2-l3 会用）。

`Target` / `DiagnosticFormat` / `Feature`：

[src/args.rs:622-634](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L622-L634) —— `Target` 默认 `Paged`，另有 `Html`、`Bundle`。

[src/args.rs:636-644](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L636-L644) —— `DiagnosticFormat` 默认 `Human`，另有 `Short`（u2-l4 会讲二者输出差异）。

[src/args.rs:646-654](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L646-L654) —— `Feature` 同时派生了 `ValueEnum` 和 `Serialize`（因为 info 命令要把特性序列化输出，u4-l6 会用）。

`PdfStandard`（`#[value(name = ...)]` 的典型用例）：

[src/args.rs:656-713](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L656-L713) —— 注意变体名是 `V_1_7`、`A_2b`，而命令行名字是 `"1.7"`、`"a-2b"`；`#[expect(non_camel_case_types)]` 用来压制命名风格警告。

`DepsFormat` 与 `SerializationFormat`：

[src/args.rs:608-620](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L608-L620) —— 依赖文件格式，默认 `Json`。

[src/args.rs:715-723](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L715-L723) —— query/info 的序列化格式，默认 `Json`。

`display_possible_values!` 宏的定义（在 typst-utils 中）：

[crates/typst-utils/src/lib.rs:481-492](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/lib.rs#L481-L492) —— 给枚举实现 `Display`，输出当前变体对应的命令行名字。

#### 4.4.4 代码实践

**实践目标**：观察 `ValueEnum` 在帮助里的可选值列表。

**操作步骤**：

1. 运行 `./target/debug/typst compile --help`，找到 `--format`、`--pdf-standard`、`--diagnostic-format` 三行。
2. 运行 `./target/debug/typst compile doc.typ out.pdf --format html`（假设 `html` 是实验特性，可能需要额外 feature；若报错则记录报错信息）。

**需要观察的现象**：

- 帮助里 `--format` 后列出 `[possible values: pdf, png, svg, html, bundle]`，与 `OutputFormat` 变体一一对应。
- `--pdf-standard` 列出的名字是 `1.4`、`a-2b` 等（来自 `#[value(name)]`），而不是 `V_1_4`、`A_2b`。

**预期结果**：确认「命令行名字」由 `#[value(name = ...)]` 决定，而非 Rust 变体名。

> 若本地未构建，上述现象「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`OutputFormat::is_paged()` 把 html/bundle 排除在「分页」之外，为什么？

> **答案**：pdf/png/svg 都是从分页文档（`PagedDocument`）导出的；而 html/bundle 走另一条编译目标（`Target::Html` / `Target::Bundle`），产物结构不同，所以需要区分。

**练习 2**：`Feature` 枚举为什么额外派生了 `Serialize`，而 `OutputFormat` 没有？

> **答案**：`Feature` 的取值会被 `info` 命令收集并序列化成 JSON/YAML 输出（[src/args.rs:647](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L647) 派生了 `Serialize`）；`OutputFormat` 只用于命令行解析，不需要序列化。

---

### 4.5 Pages 页面范围解析与 parse_sys_input_pair 键值解析

#### 4.5.1 概念说明

本节讲两个「手写解析逻辑」的典型：

1. **`Pages`**：`--pages` 支持页面范围语法，如 `2,4-6,8-`，表示「第 2 页、第 4 到 6 页、第 8 页及以后」。这需要自定义解析算法，因此 `Pages` 实现了标准库的 `FromStr`，而不是用 clap 的值解析器。

2. **`parse_sys_input_pair`**：`--input key=value` 选项要求值是 `键=值` 形式（如 `--input name=typst`），这些键值对会通过 `sys.inputs` 暴露给文档。它用一个小函数解析「以第一个等号切分」。

#### 4.5.2 核心流程

**Pages 解析**：一段范围字符串先按 `-` 切分，根据切出来的片段数量判断是「单页」「起止区间」「开区间」还是「非法」。注意页码用 `NonZeroUsize`（非零正整数），因为页码从 1 开始。

```
"4-6"  → split('-') → ["4", "6"] → [start, end] → Some(4)..=Some(6)
"8-"   → split('-') → ["8", ""]  → [start, ""]  → Some(8)..=None   (开区间到末尾)
"-2"   → split('-') → ["", "2"]  → ["", end]    → None..=Some(2)   (从开头到第2页)
"3"    → split('-') → ["3"]      → [single]     → Some(3)..=Some(3)
"0"    → parse_page_number 报错 "page numbers start at one"
"4-2"  → start > end 报错 "must end at a page after the start"
```

`Pages` 内部存的是 `RangeInclusive<Option<NonZeroUsize>>`：`None` 表示「那一端无界」。

**键值对解析**：`"name=typst"` 用 `split_once('=')` 在第一个等号处切成 `("name", "typst")`，key 去空格后不能为空，value 同样去空格。

#### 4.5.3 源码精读

`Pages` 与 `FromStr` 实现：

[src/args.rs:725-758](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L725-L758) —— 这是本节的核心算法。注释说明：**故意用 `FromStr` 而非值解析器，是为了生成更好的错误信息**（链接到 clap 一个相关 issue）。

`parse_page_number` 辅助函数：

[src/args.rs:760-767](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L760-L767) —— 拒绝 `0`，其余用 `NonZeroUsize::from_str` 解析。

`Pages` 字段在 `CompileArgs` 中的声明：

[src/args.rs:340-341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L340-L341) —— `Option<Vec<Pages>>`，`value_delimiter = ','` 表示用逗号分隔成多个 `Pages`；不传则为 `None`（导出全部页）。

下游如何消费 `Pages`（在 compile.rs，u2-l2 主讲）：

[crates/typst-cli/src/compile.rs:144-148](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L144-L148) —— 把 `Vec<Pages>` 的内部范围收集成 `PageRanges`，并据此推导 `tagged`（`--pages` 隐含 `--no-pdf-tags`，会触发一条警告）。

键值对解析器：

[src/args.rs:796-810](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L796-L810) —— `parse_sys_input_pair`：`split_once('=')` 在第一个等号切分；key 为空时报错；key/value 都 `trim()`。

它在 `WorldArgs.inputs` 上的绑定：

[src/args.rs:405-411](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L405-L411) —— `--input key=value`，`ArgAction::Append` 允许多次出现，每次解析成一个 `(String, String)` 元组。

#### 4.5.4 代码实践（本讲主实践）

**实践目标**：用 `--pages 2,4-6` 编译文档，对照 `Pages::from_str` 验证解析逻辑。

**操作步骤**：

1. 创建一个会渲染成多页的文档 `multi.typ`，例如：

   ```typst
   #set page(width: auto, height: auto)
   #counter(page).update(1)
   #for i in range(0, 8) [
     = Page #i
     #pagebreak()
   ]
   ```

   （示例代码：用循环生成多页，实际页数「待本地验证」。）

2. 编译并限定页面范围：

   ```bash
   ./target/debug/typst compile multi.typ out.pdf --pages 2,4-6
   ```

3. 再尝试几个边界情况，记录是否报错：

   - `--pages 0`（第 0 页）
   - `--pages 6-4`（起 > 止）
   - `--pages 8-`（到末尾的开区间）

**需要观察的现象**：

- `--pages 2,4-6` 应只导出第 2、4、5、6 页，且终端出现 `using --pages implies --no-pdf-tags` 警告（对应 compile.rs:149-152）。
- `--pages 0` 报 `page numbers start at one`。
- `--pages 6-4` 报 `page export range must end at a page after the start`。

**预期结果**：每个边界情况的报错文案，都能在 [src/args.rs:736-757](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L736-L757) 找到对应字符串。

> 若本地未构建，上述现象「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`--pages 8-` 表示什么？它对应的 `Pages` 内部值是什么？

> **答案**：表示「第 8 页及以后所有页」。内部值是 `Pages(Some(8)..=None)`，`None` 表示上界无界（见 [src/args.rs:744](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L744)）。

**练习 2**：`--input =value`（key 为空）会发生什么？

> **答案**：`parse_sys_input_pair` 会在 `key.is_empty()` 时返回 `the key was missing or empty` 错误（见 [src/args.rs:805-807](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L805-L807)）。

**练习 3**：为什么 `Pages` 用 `FromStr` 而不像 `Input` 那样用 clap 值解析器？

> **答案**：源码注释（[src/args.rs:727-729](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L727-L729)）说明：用 `FromStr` 是为了生成更好的错误信息（参见引用的 clap issue #5065）。

---

## 5. 综合实践

把本讲全部知识串起来，完成一个「逆向工程」任务：**仅凭帮助文本，还原 `args.rs` 的结构**。

1. 运行 `./target/debug/typst --help`，列出全部可见子命令与全局选项，标注它们分别来自 `CliArguments` 的哪个字段（`command` / `color` / `cert`）。
2. 运行 `./target/debug/typst compile --help`，把所有选项分类填入下表：

   | 归属参数组 | 选项举例 |
   | --- | --- |
   | `CompileArgs` 直属 | `--format`、`--pages`、`--pdf-standard`、`--ppi`、`--open`…… |
   | `WorldArgs`（flatten） | `--root`、`--input`、`--creation-timestamp` |
   | `FontArgs`（flatten） | `--font-path`、`--ignore-system-fonts` |
   | `PackageArgs`（flatten） | `--package-path`、`--package-cache-path` |
   | `ProcessArgs`（flatten） | `--jobs`、`--features`、`--diagnostic-format` |

3. 用一条「真实」命令同时验证 `Input`/`Output` 解析器、`Pages`、`Feature` 与键值对解析：

   ```bash
   echo '#for i in range(0,5) { pagebreak() }' \
     | ./target/debug/typst compile - out.pdf \
         --pages 1,3- --input author=you --diagnostic-format short
   ```

   对照源码逐项解释：
   - 输入 `-` → `Input::Stdin`（此时 `output` 因 `required_if_eq` 必填）；
   - `--pages 1,3-` → 被 `value_delimiter = ','` 拆成 `Pages(Some(1)..=Some(1))` 和 `Pages(Some(3)..=None)`；
   - `--input author=you` → `parse_sys_input_pair` 切成 `("author", "you")`；
   - `--diagnostic-format short` → `DiagnosticFormat::Short`。

4. 最后，打开 `args.rs`，核对你的分类是否与源码的 `flatten` 关系一致。

> 目的：做完这个任务，你就拥有了「看一眼命令行就能脑补出 Rust 结构体」的能力，这是阅读后续所有子命令实现的前提。

## 6. 本讲小结

- `args.rs` 用 clap 派生宏把命令行定义成强类型结构：顶层 `CliArguments`（`Parser`）→ `Command` 枚举（`Subcommand`，9 个变体）→ 各子命令结构体。
- 参数通过 `#[clap(flatten)]` 分组复用：`CompileArgs` / `WorldArgs` / `ProcessArgs` / `FontArgs` / `PackageArgs` 像「积木」一样拼到不同子命令。
- `Input` / `Output` 两个枚举用自定义值解析器区分「`-` = stdin/stdout」与真实路径，并各自带 `write`/`open` 便捷方法。
- 取值有限的选项用 `ValueEnum` 枚举表达：`OutputFormat` / `Target` / `PdfStandard` / `Feature` / `DiagnosticFormat` 等；`PdfStandard` 用 `#[value(name = ...)]` 把命令行名字与 Rust 变体名解耦。
- `Pages` 页面范围手写 `FromStr` 实现，支持 `2`、`4-6`、`8-`、`-2` 等语法，并用 `NonZeroUsize` 保证页码从 1 开始。
- `--input key=value` 由 `parse_sys_input_pair` 按「第一个等号」切分；环境变量（如 `TYPST_CERT`、`SOURCE_DATE_EPOCH`）通过 clap 的 `env =` 注入。
- `args.rs` 同时被运行时与 build script 导入，因此对 import 有特殊约束（只能用既是运行时又是构建依赖的 crate）。

## 7. 下一步学习建议

本讲只定义了「参数长什么样」，还没有用到它们。建议下一步：

- **u2-l1（SystemWorld）**：看 `WorldArgs` 是如何被消费去构造编译环境的（`--root`、`--input`、字体、包如何变成 `SystemWorld`）。
- **u2-l2（编译配置与单次编译）**：看 `CompileArgs` 的 `output`/`format`/`pages`/`pdf_standard` 是如何在 `CompileConfig::new_impl` 里被预处理（推断格式、推导输出路径、校验 PDF 标准与 tagged 逻辑）的。
- **u4-l5（构建期产物与 completions）**：看 build script 与 `completions` 命令如何复用本讲的 `CliArguments` 生成 man 手册页与 shell 补全——这会加深你对「args.rs 为何要被共享导入」的理解。

阅读时，可以随时回到本讲，把遇到的结构体字段当作「参数字典」查阅。
